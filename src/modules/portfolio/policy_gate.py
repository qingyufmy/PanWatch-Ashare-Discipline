"""Deterministic policy gate for portfolio proposals.

No model call, notification, or broker order occurs here. Every rule is
persisted before an actionable envelope can be created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import current_freshness, current_plan, logical_hash
from src.modules.portfolio.signal_journal import transition_signal, utc_naive
from src.platform.persistence.models import (
    ActionableSignal, EvidenceSnapshot, NextDayAction,
    PortfolioTruthSnapshot, SignalEvent, SignalPolicyDecision,
)
from src.platform.scheduling.trading_calendar import next_confirmed_cn_trading_day


POLICY_VERSION = "p3-v1"
RULE_IDS = (
    "SIGNAL_TTL", "SCHEMA_INVALID", "MODEL_CONFLICT", "DATA_FRESHNESS",
    "PORTFOLIO_TRUTH", "MAX_POSITION", "THESIS_INVALID", "STOP_WIDENING",
    "HARD_STOP", "T_PLUS_ONE", "DUPLICATE",
)
CHANGING_ACTIONS = frozenset({"OPEN", "ADD", "REDUCE", "EXIT"})


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc_naive(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_signal(
    db: Session, signal_id: str, *, now: datetime | None = None,
    quote_ttl_seconds: int = 120, truth_ttl_seconds: int = 300,
    commit: bool = True,
) -> SignalEvent:
    """Run every rule and fail closed when any required proof is absent."""
    signal = db.get(SignalEvent, signal_id)
    if signal is None:
        raise ValueError("signal_missing")
    if signal.status != "GENERATED":
        return signal
    timestamp = utc_naive(now or datetime.now(timezone.utc))
    evidence = db.get(EvidenceSnapshot, signal.evidence_snapshot_id)
    if evidence is None or not isinstance(evidence.payload, dict):
        raise ValueError("evidence_missing")
    payload = evidence.payload
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    quote = meta.get("quote") if isinstance(meta.get("quote"), dict) else {}
    plan = current_plan(db, signal.market, signal.symbol)
    truth = db.get(PortfolioTruthSnapshot, signal.truth_snapshot_id) if signal.truth_snapshot_id else None
    truth_position = next(
        (p for p in truth.positions if p.market == signal.market and p.symbol == signal.symbol),
        None,
    ) if truth else None
    position_plan = (plan.plan or {}) if plan else {}
    risk = position_plan.get("risk") if isinstance(position_plan.get("risk"), dict) else {}
    plan_position = position_plan.get("position") if isinstance(position_plan.get("position"), dict) else {}
    old_stop = _number(risk.get("current_stop"))
    new_stop = _number(payload.get("new_stop", meta.get("new_stop")))
    current_price = _number(quote.get("current_price"))
    market_asof = _datetime(payload.get("market_data_asof") or meta.get("market_data_asof"))
    market_source = payload.get("market_data_source") or meta.get("market_data_source")
    changing = signal.action in CHANGING_ACTIONS
    verdicts: dict[str, tuple[str, str]] = {}

    def decide(rule: str, verdict: str, reason: str = "") -> None:
        verdicts[rule] = (verdict, reason)

    decide("SIGNAL_TTL", "EXPIRED" if timestamp >= signal.expires_at or timestamp < signal.valid_from else "PASS",
           "SIGNAL_OUTSIDE_VALID_WINDOW" if timestamp >= signal.expires_at or timestamp < signal.valid_from else "")
    schema_valid = payload.get("schema_valid", meta.get("schema_valid")) is True
    decide("SCHEMA_INVALID", "PASS" if schema_valid else "REVIEW", "SCHEMA_NOT_ATTESTED" if not schema_valid else "")
    conflict = bool(payload.get("model_conflict", meta.get("model_conflict", False)))
    decide("MODEL_CONFLICT", "REVIEW" if conflict else "PASS", "MODEL_CONFLICT" if conflict else "")
    fresh_quote = bool(
        market_asof and market_source and current_price and current_price > 0
        and -30 <= (timestamp - market_asof).total_seconds() <= quote_ttl_seconds
    )
    decide("DATA_FRESHNESS", "PASS" if fresh_quote else "REVIEW", "QUOTE_UNPROVEN_OR_STALE" if not fresh_quote else "")

    trusted = bool(
        truth and truth.truth_status == "TRUSTED"
        and not truth.anomaly_flags
        and current_freshness(truth, now=timestamp.replace(tzinfo=timezone.utc), ttl_seconds=truth_ttl_seconds) == "FRESH"
        and plan and plan.version == signal.plan_version
        and truth_position
    )
    decide("PORTFOLIO_TRUTH", "PASS" if trusted or not changing else "REVIEW",
           "TRUTH_OR_PLAN_UNTRUSTED" if changing and not trusted else "")

    if signal.action in {"OPEN", "ADD"}:
        max_weight = _number(plan_position.get("max_weight"))
        target = _number(signal.target_weight)
        if max_weight is None or target is None or max_weight <= 0 or target < 0 or signal.qty_hint is None:
            decide("MAX_POSITION", "REVIEW", "WEIGHT_LIMIT_UNKNOWN")
        elif target > max_weight:
            decide("MAX_POSITION", "BLOCK", "MAX_WEIGHT_EXCEEDED")
        else:
            decide("MAX_POSITION", "PASS")
    else:
        decide("MAX_POSITION", "PASS")

    thesis = plan.thesis_state if plan else "UNKNOWN"
    if signal.action in {"OPEN", "ADD"} and thesis == "INVALID":
        decide("THESIS_INVALID", "BLOCK", "THESIS_INVALID_NO_EXPANSION")
    elif signal.action in {"OPEN", "ADD"} and thesis != "VALID":
        decide("THESIS_INVALID", "REVIEW", "THESIS_NOT_VALID")
    else:
        decide("THESIS_INVALID", "PASS")

    if new_stop is not None and old_stop is not None and new_stop < old_stop:
        decide("STOP_WIDENING", "BLOCK", "STOP_WIDENING_NOT_ALLOWED")
    else:
        decide("STOP_WIDENING", "PASS")
    if old_stop is not None and current_price is not None and current_price <= old_stop and signal.action in {"HOLD", "OPEN", "ADD"}:
        decide("HARD_STOP", "BLOCK", "HARD_STOP_CANNOT_HOLD_OR_ADD")
    elif old_stop is not None and current_price is None:
        decide("HARD_STOP", "REVIEW", "HARD_STOP_PRICE_UNKNOWN")
    else:
        decide("HARD_STOP", "PASS")

    approved_qty: int | None = signal.qty_hint
    defer_qty = 0
    if signal.action in {"REDUCE", "EXIT"}:
        wanted = signal.qty_hint
        if signal.action == "EXIT" and wanted is None and truth_position:
            wanted = truth_position.total_qty
        sellable = truth_position.sellable_qty if truth_position else None
        if wanted is None or wanted <= 0 or sellable is None:
            decide("T_PLUS_ONE", "REVIEW", "SELL_QUANTITY_OR_SELLABLE_UNKNOWN")
        else:
            approved_qty = min(wanted, sellable)
            defer_qty = wanted - approved_qty
            decide("T_PLUS_ONE", "PASS" if approved_qty > 0 else "REVIEW",
                   "PARTIAL_SELL_DEFERRED" if defer_qty else "")
    else:
        decide("T_PLUS_ONE", "PASS")

    duplicate = False
    if changing:
        duplicate = db.query(ActionableSignal).join(SignalEvent, ActionableSignal.signal_id == SignalEvent.signal_id).filter(
            SignalEvent.signal_id != signal.signal_id,
            SignalEvent.market == signal.market,
            SignalEvent.symbol == signal.symbol,
            SignalEvent.action == signal.action,
            SignalEvent.trade_date == signal.trade_date,
            SignalEvent.expires_at > timestamp,
        ).first() is not None
    decide("DUPLICATE", "BLOCK" if duplicate else "PASS", "DUPLICATE_ACTIONABLE_SIGNAL" if duplicate else "")

    assert set(verdicts) == set(RULE_IDS)
    frozen_input = {
        "signal_id": signal.signal_id, "signal_dedupe_key": signal.dedupe_key,
        "evidence_hash": evidence.logical_hash,
        "truth_hash": truth.logical_hash if truth else None,
        "plan_hash": plan.logical_hash if plan else None,
        "at": timestamp.isoformat(), "policy_version": POLICY_VERSION,
    }
    input_hash = logical_hash(frozen_input)
    for rule in RULE_IDS:
        verdict, reason = verdicts[rule]
        db.add(SignalPolicyDecision(
            signal_id=signal_id, rule_id=rule, input_hash=input_hash,
            decision=verdict, reason_codes=[reason] if reason else [],
        ))
    db.flush()
    if any(verdict == "EXPIRED" for verdict, _ in verdicts.values()):
        transition_signal(db, signal_id, "EXPIRED", reason="SIGNAL_TTL", now=timestamp, commit=False)
    elif any(verdict == "BLOCK" for verdict, _ in verdicts.values()):
        transition_signal(db, signal_id, "POLICY_REJECTED", reason="POLICY_BLOCK", now=timestamp, commit=False)
    elif any(verdict == "REVIEW" for verdict, _ in verdicts.values()):
        transition_signal(db, signal_id, "REVIEW_REQUIRED", reason="POLICY_REVIEW", now=timestamp, commit=False)
    else:
        if defer_qty:
            due_date = timestamp.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
            due = next_confirmed_cn_trading_day(due_date)
            db.add(NextDayAction(
                signal_id=signal_id, market=signal.market, symbol=signal.symbol,
                due_trade_date=due.isoformat() if due else "UNRESOLVED",
                pending_qty=defer_qty,
                reason="REDUCE_REMAINING_POSITION" if signal.action == "REDUCE" else "EXIT_REMAINING_POSITION",
                status="OPEN" if due else "CALENDAR_REQUIRED",
            ))
        db.add(ActionableSignal(
            signal_id=signal_id, approved_qty=approved_qty,
            approved_weight=signal.target_weight,
            policy_version=POLICY_VERSION, input_hash=input_hash,
            approved_at=timestamp,
        ))
        db.flush()
        transition_signal(db, signal_id, "APPROVED", reason="ALL_POLICY_RULES_PASS", now=timestamp, commit=False)
    if commit:
        db.commit()
    return signal
