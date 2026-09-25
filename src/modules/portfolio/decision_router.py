"""One held-position decision and outbox entry from Policy-reviewed signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.notification_renderer import render_position_decision
from src.modules.portfolio.notifications import enqueue_portfolio_notice
from src.platform.persistence.models import (
    ActionableSignal, EvidenceSnapshot, ExecutionEvent, PortfolioDecision,
    PortfolioFeatureSnapshot, PortfolioLevelSnapshot,
    PortfolioNotification, PortfolioTruthSnapshot, SignalEvent,
)

HELD_ACTIONS = {"ADD", "REDUCE", "HOLD", "EXIT"}


def record_position_decision(db: Session, signal: SignalEvent, *,
                             now: datetime | None = None,
                             queue_notification: bool = True) -> tuple[PortfolioDecision | None, PortfolioNotification | None]:
    """Append a revision only when the authoritative action semantics changed.

    This function neither calls a model nor sends a message. The caller commits
    proposal, Policy result, decision and outbox in one transaction.
    """
    if signal.action not in HELD_ACTIONS or signal.status not in {"APPROVED", "REVIEW_REQUIRED"}:
        return None, None
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    if signal.expires_at <= timestamp or signal.generated_at > timestamp:
        return None, None
    truth = db.get(PortfolioTruthSnapshot, signal.truth_snapshot_id) if signal.truth_snapshot_id else None
    if truth is None or truth.trade_date != signal.trade_date:
        return None, None
    bare_symbol = signal.symbol[-6:] if len(signal.symbol) == 8 else signal.symbol
    position = next((p for p in truth.positions if p.market == signal.market and p.symbol == bare_symbol), None) if truth else None
    if position is None:
        return None, None
    approved = db.get(ActionableSignal, signal.signal_id) if signal.status == "APPROVED" else None
    if signal.status == "APPROVED" and approved is None:
        return None, None
    evidence = db.get(EvidenceSnapshot, signal.evidence_snapshot_id)
    evidence_meta = ((evidence.payload or {}).get("meta") or {}) if evidence else {}
    level_row = (db.query(PortfolioLevelSnapshot).filter_by(symbol=signal.symbol if len(signal.symbol) == 8 else
                 ("sh" if bare_symbol.startswith(("6", "9")) else "bj" if bare_symbol.startswith(("4", "8")) else "sz") + bare_symbol,
                 trade_date=signal.trade_date).filter(PortfolioLevelSnapshot.market_asof <= timestamp)
                 .order_by(PortfolioLevelSnapshot.id.desc()).first())
    feature_row = db.get(PortfolioFeatureSnapshot, level_row.feature_snapshot_id) if level_row else None
    level = (level_row.payload if level_row and feature_row and feature_row.quality_status == "OK"
             and not (feature_row.payload or {}).get("missing_minutes")
             and evidence_meta.get("feature_snapshot_id") == feature_row.id
             and evidence_meta.get("level_snapshot_id") == level_row.id
             and timestamp - level_row.market_asof <= timedelta(seconds=120) else None)
    executions = db.query(ExecutionEvent).filter_by(signal_id=signal.signal_id).all()
    execution_status = "USER_REPORTED_UNRECONCILED" if executions else "NOT_EXECUTED"
    if any(e.reconcile_status == "MISMATCH" for e in executions):
        execution_status = "RECONCILE_MISMATCH"
    elif executions and all(e.reconcile_status == "RECONCILED" for e in executions):
        execution_status = "RECONCILED"
    qty = approved.approved_qty if approved else None
    broken = (level or {}).get("broken_prior_support")
    semantic = logical_hash({
        "action": signal.action, "qty": qty, "plan": signal.plan_version,
        "status": signal.status, "execution": execution_status,
        "broken_prior_support": broken,
    })
    current = (db.query(PortfolioDecision).filter_by(trade_date=signal.trade_date,
               symbol=bare_symbol, current=True).order_by(PortfolioDecision.id.desc()).first())
    if current:
        # An older model response cannot replace a newer decision or hard risk.
        if signal.generated_at <= current.created_at:
            return current, None
        if current.decision_status == "APPROVED" and signal.status != "APPROVED":
            return current, None
        if current.plan_version is not None and signal.plan_version is not None and signal.plan_version < current.plan_version:
            return current, None
        if current.semantic_hash == semantic and current.expires_at > timestamp:
            return current, None
        current.current = False
        for pending in db.query(PortfolioNotification).filter_by(decision_id=current.id).filter(
            PortfolioNotification.delivery_status.in_(("PENDING", "FAILED_RETRYABLE"))):
            pending.delivery_status = "CANCELLED"
            pending.suppression_reason = "DECISION_SUPERSEDED"
    revision = current.revision + 1 if current else 1
    row = PortfolioDecision(
        trade_date=signal.trade_date, market=signal.market, symbol=bare_symbol,
        revision=revision, signal_id=signal.signal_id, plan_version=signal.plan_version,
        level_snapshot_id=level_row.id if level else None,
        action=signal.action, approved_qty=qty, decision_status=signal.status,
        risk_status="HARD_RISK" if signal.action == "EXIT" else "UNVERIFIED",
        data_status="FRESH" if level else "UNVERIFIED",
        execution_status=execution_status, semantic_hash=semantic, current=True,
        created_at=timestamp, expires_at=signal.expires_at,
    )
    db.add(row)
    db.flush()
    # Directional review stays in the private decision ledger. Approved action
    # needs a source-consistent frozen level; deterministic hard risk uses its
    # independent alert path and is never held hostage by the model.
    if (not queue_notification or signal.status != "APPROVED" or level is None
            or truth.truth_status != "TRUSTED" or truth.anomaly_flags):
        return row, None
    if signal.action == "HOLD" and (current is None or current.action == "HOLD"):
        return row, None
    title, content = render_position_decision(
        symbol=bare_symbol, name=position.name, action=signal.action,
        decision_status=signal.status, execution_status=execution_status,
        level=level, approved_qty=qty, total_qty=position.total_qty,
        sellable_qty=position.sellable_qty,
        truth_trusted=truth.truth_status == "TRUSTED" and not truth.anomaly_flags,
    )
    notification, _ = enqueue_portfolio_notice(
        db, key=f"decision:{signal.trade_date}:{bare_symbol}:{revision}",
        title=title, content=content, trade_date=signal.trade_date,
        symbol=bare_symbol, decision=row, signal_ids=[signal.signal_id],
        priority="CRITICAL" if signal.action == "EXIT" else "NORMAL",
        reason="DECISION_CHANGE", expires_at=signal.expires_at,
    )
    return row, notification
