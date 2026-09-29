"""Append-only P2 signal and manual execution evidence.

Recording a signal does not approve it or place an order. P3 owns approval.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import current_plan, logical_hash
from src.platform.scheduling.trading_calendar import next_confirmed_cn_trading_day
from src.platform.persistence.models import (
    ActionableSignal, EvidenceSnapshot, SignalEvent, SignalLifecycleEvent,
    ExecutionEvent, DisciplineEvent, NextDayAction, PortfolioDecision,
    PortfolioTruthSnapshot,
    DailyPortfolioPlan,
)


ACTIONS = frozenset({"OPEN", "ADD", "HOLD", "REDUCE", "EXIT", "REVIEW"})
TERMINAL = frozenset({"POLICY_REJECTED", "EXECUTED", "IGNORED", "EXPIRED", "OVERRIDDEN", "CANCELLED"})
LIFECYCLE = frozenset({
    "GENERATED", "POLICY_REJECTED", "REVIEW_REQUIRED", "APPROVED",
    "EXECUTION_PENDING", "PARTIALLY_EXECUTED", "EXECUTED",
    "IGNORED", "EXPIRED", "OVERRIDDEN", "CANCELLED",
})
ALLOWED_NEXT = {
    "GENERATED": {"POLICY_REJECTED", "REVIEW_REQUIRED", "APPROVED", "EXPIRED", "IGNORED"},
    "REVIEW_REQUIRED": {"POLICY_REJECTED", "APPROVED", "IGNORED", "EXPIRED"},
    "APPROVED": {"EXECUTION_PENDING", "IGNORED", "EXPIRED", "CANCELLED"},
    "EXECUTION_PENDING": {"PARTIALLY_EXECUTED", "EXECUTED", "IGNORED", "EXPIRED", "CANCELLED"},
    "PARTIALLY_EXECUTED": {"EXECUTED", "CANCELLED", "EXPIRED"},
}


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def record_signal(
    db: Session, *, market: str, symbol: str, action: str,
    source: str, evidence: dict[str, Any], ttl_seconds: int,
    trace_id: str | None = None, source_agent: str = "",
    source_suggestion_id: int | None = None,
    generated_at: datetime | None = None, valid_from: datetime | None = None,
    qty_hint: int | None = None, current_weight: float | None = None,
    target_weight: float | None = None, raw_action: str | None = None,
    prompt_id: str | None = None, prompt_version: str | None = None,
    model_role: str | None = None, requested_model: str | None = None,
    reported_model: str | None = None,
    commit: bool = True,
) -> tuple[SignalEvent, bool]:
    """Freeze evidence and one deduplicated signal in GENERATED state."""
    if action not in ACTIONS or not market or not symbol or not source:
        raise ValueError("invalid_signal")
    if not isinstance(evidence, dict) or not evidence or ttl_seconds <= 0:
        raise ValueError("invalid_signal_evidence_or_ttl")
    if qty_hint is not None and qty_hint <= 0:
        raise ValueError("invalid_qty_hint")
    now = utc_naive(generated_at or datetime.now(timezone.utc))
    valid = utc_naive(valid_from or now)
    if valid < now - timedelta(seconds=30) or valid > now + timedelta(seconds=ttl_seconds):
        raise ValueError("invalid_valid_from")
    trade_date = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    evidence_hash = logical_hash(evidence)
    plan = current_plan(db, market, symbol)
    daily_plan = (db.query(DailyPortfolioPlan).filter_by(trade_date=trade_date)
                  .order_by(DailyPortfolioPlan.version.desc()).first())
    truth = db.query(PortfolioTruthSnapshot).order_by(PortfolioTruthSnapshot.id.desc()).first()
    identity = {
        "trade_date": trade_date, "market": market, "symbol": symbol,
        "source": source, "source_agent": source_agent,
        "source_suggestion_id": source_suggestion_id, "action": action,
        "evidence_hash": evidence_hash, "plan_version": plan.version if plan else None,
        "daily_plan_version": daily_plan.version if daily_plan else None,
        "truth_snapshot_id": truth.id if truth else None,
    }
    dedupe_key = logical_hash(identity)
    existing = db.query(SignalEvent).filter_by(dedupe_key=dedupe_key).first()
    if existing:
        return existing, False
    frozen = EvidenceSnapshot(
        captured_at=now, source=source, logical_hash=evidence_hash,
        payload=evidence, truth_snapshot_id=truth.id if truth else None,
    )
    db.add(frozen)
    db.flush()
    signal = SignalEvent(
        signal_id=uuid.uuid4().hex, trace_id=trace_id or uuid.uuid4().hex,
        trade_date=trade_date, market=market, symbol=symbol,
        source=source, source_agent=source_agent,
        source_suggestion_id=source_suggestion_id,
        plan_version=plan.version if plan else None,
        daily_plan_version=daily_plan.version if daily_plan else None,
        truth_snapshot_id=truth.id if truth else None,
        evidence_snapshot_id=frozen.id, action=action,
        raw_action=raw_action or action, current_weight=current_weight,
        target_weight=target_weight,
        delta_weight=(target_weight - current_weight) if target_weight is not None and current_weight is not None else None,
        qty_hint=qty_hint, generated_at=now, valid_from=valid,
        expires_at=now + timedelta(seconds=ttl_seconds),
        dedupe_key=dedupe_key, status="GENERATED", reason_codes=[],
        prompt_id=prompt_id, prompt_version=prompt_version,
        model_role=model_role, requested_model=requested_model,
        reported_model=reported_model,
    )
    db.add(signal)
    db.add(SignalLifecycleEvent(
        signal_id=signal.signal_id, from_status=None, to_status="GENERATED",
        reason="SIGNAL_RECORDED", occurred_at=now,
    ))
    if commit:
        db.commit()
        db.refresh(signal)
    else:
        db.flush()
    return signal, True


def transition_signal(
    db: Session, signal_id: str, to_status: str, *, reason: str,
    now: datetime | None = None, commit: bool = True,
) -> SignalEvent:
    """Record lifecycle changes; approval requires the future deterministic gate."""
    signal = db.get(SignalEvent, signal_id)
    if signal is None:
        raise ValueError("signal_missing")
    if to_status not in LIFECYCLE or to_status not in ALLOWED_NEXT.get(signal.status, set()):
        raise ValueError("invalid_signal_transition")
    if to_status == "APPROVED" and db.get(ActionableSignal, signal_id) is None:
        raise ValueError("policy_gate_required")
    timestamp = utc_naive(now or datetime.now(timezone.utc))
    if timestamp > signal.expires_at and to_status != "EXPIRED":
        raise ValueError("signal_expired")
    previous = signal.status
    signal.status = to_status
    db.add(SignalLifecycleEvent(
        signal_id=signal_id, from_status=previous, to_status=to_status,
        reason=reason[:255], occurred_at=timestamp,
    ))
    if commit:
        db.commit()
    else:
        db.flush()
    return signal


def record_manual_execution(
    db: Session, *, signal_id: str, actual_action: str,
    actual_qty: int, actual_price: float | None,
    executed_at: datetime, notes: str = "", client_request_id: str | None = None,
) -> ExecutionEvent:
    """Record a user's claimed trade for later reconciliation, never send an order."""
    signal = db.get(SignalEvent, signal_id)
    if signal is None:
        raise ValueError("signal_missing")
    if actual_action not in {"OPEN", "ADD", "REDUCE", "EXIT"} or actual_qty <= 0:
        raise ValueError("invalid_execution")
    if actual_price is not None and actual_price <= 0:
        raise ValueError("invalid_execution_price")
    if client_request_id:
        if len(client_request_id) > 64 or len(client_request_id) < 8:
            raise ValueError("invalid_client_request_id")
        prior_request = db.query(ExecutionEvent).filter_by(client_request_id=client_request_id).first()
        if prior_request:
            if (prior_request.signal_id, prior_request.actual_action, prior_request.actual_qty,
                    prior_request.actual_price) != (signal_id, actual_action, actual_qty, actual_price):
                raise ValueError("client_request_id_payload_mismatch")
            return prior_request
    approved = db.get(ActionableSignal, signal_id)
    prior_qty = sum(e.actual_qty for e in db.query(ExecutionEvent).filter_by(signal_id=signal_id).all()
                    if e.actual_action == actual_action and e.result == "USER_REPORTED")
    authorized = bool(
        approved and signal.status in {"APPROVED", "EXECUTION_PENDING"}
        and actual_action == signal.action
        and approved.approved_qty is not None
        and prior_qty + actual_qty <= approved.approved_qty
    )
    executed = utc_naive(executed_at)
    event = ExecutionEvent(
        execution_id=uuid.uuid4().hex, signal_id=signal_id,
        client_request_id=client_request_id,
        planned_action=signal.action,
        planned_qty=approved.approved_qty if approved else signal.qty_hint,
        planned_weight=approved.approved_weight if approved else signal.target_weight,
        actual_action=actual_action, actual_qty=actual_qty,
        actual_price=actual_price, execution_source="USER",
        executed_at=executed,
        result="USER_REPORTED" if authorized else "USER_REPORTED_OUT_OF_POLICY",
        notes=notes,
        reconcile_status="PENDING",
    )
    db.add(event)
    if authorized:
        current_decision = (db.query(PortfolioDecision).filter_by(
            signal_id=signal_id, current=True).order_by(PortfolioDecision.id.desc()).first())
        if current_decision and current_decision.expires_at > utc_naive(datetime.now(timezone.utc)):
            from src.modules.portfolio.notifications import enqueue_portfolio_notice
            remaining = approved.approved_qty - prior_qty - actual_qty
            current_decision.execution_status = "USER_REPORTED_UNRECONCILED"
            action_label = {"OPEN": "建仓", "ADD": "加仓", "REDUCE": "减仓", "EXIT": "清仓离场"}[actual_action]
            enqueue_portfolio_notice(
                db, key=f"execution:{event.execution_id}",
                title=f"{action_label}｜{signal.symbol}｜用户执行登记",
                content=(f"用户报告本次{actual_action} {actual_qty}股；累计报告"
                         f"{prior_qty + actual_qty}股；原批准上限内尚余{remaining}股。\n"
                         "这是用户登记，尚未经券商成交核对；再次行动前需重新核对持仓、可卖数量、价格和当前决策。"),
                trade_date=signal.trade_date, symbol=signal.symbol,
                decision=current_decision, signal_ids=[signal_id],
                reason="EXECUTION_UPDATE", expires_at=current_decision.expires_at,
            )
    db.add(DisciplineEvent(
        signal_id=signal_id,
        event_type="MANUAL_EXECUTION_RECORDED" if authorized else "UNAUTHORIZED_ACTION",
        details={"execution_id": event.execution_id, "reconcile_status": "PENDING",
                 "authorized": authorized, "signal_status": signal.status},
    ))
    if signal.market == "CN" and actual_action in {"OPEN", "ADD"}:
        trade_day = executed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
        due = next_confirmed_cn_trading_day(trade_day)
        db.add(NextDayAction(
            signal_id=signal_id, market=signal.market, symbol=signal.symbol,
            due_trade_date=due.isoformat() if due else "UNRESOLVED",
            pending_qty=actual_qty, reason="T_PLUS_ONE_NEW_BUY",
            status="OPEN" if due else "CALENDAR_REQUIRED",
        ))
    db.commit()
    db.refresh(event)
    return event


def reconcile_execution(
    db: Session, execution_id: str, snapshot_id: int,
) -> ExecutionEvent:
    """Compare user-reported quantity with a later truth snapshot."""
    event = db.get(ExecutionEvent, execution_id)
    after = db.get(PortfolioTruthSnapshot, snapshot_id)
    if event is None or after is None:
        raise ValueError("execution_or_truth_missing")
    signal = db.get(SignalEvent, event.signal_id)
    before = db.get(PortfolioTruthSnapshot, signal.truth_snapshot_id) if signal.truth_snapshot_id else None
    if before is None or snapshot_id <= before.id or event.reconcile_status != "PENDING":
        raise ValueError("invalid_reconciliation_snapshot")
    before_position = next((p for p in before.positions if p.market == signal.market and p.symbol == signal.symbol), None)
    after_position = next((p for p in after.positions if p.market == signal.market and p.symbol == signal.symbol), None)
    before_qty = before_position.total_qty if before_position else 0
    after_qty = after_position.total_qty if after_position else 0
    expected = before_qty + event.actual_qty if event.actual_action in {"OPEN", "ADD"} else before_qty - event.actual_qty
    matched = expected >= 0 and after_qty == expected
    event.reconcile_status = "QUANTITY_MATCHED_UNVERIFIED" if matched else "MISMATCH"
    event.reconcile_truth_snapshot_id = snapshot_id
    db.add(DisciplineEvent(
        signal_id=signal.signal_id,
        event_type="EXECUTION_RECONCILED" if matched else "EXECUTION_MISMATCH",
        details={
            "execution_id": execution_id, "snapshot_id": snapshot_id,
            "expected_qty": expected, "observed_qty": after_qty,
            "status": event.reconcile_status,
        },
    ))
    db.commit()
    return event
