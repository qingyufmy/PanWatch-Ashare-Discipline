"""Read signal/policy history and record user-reported executions."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.portfolio.signal_journal import record_manual_execution, reconcile_execution, transition_signal
from src.modules.portfolio.notifications import dispatch_portfolio_notice
from src.platform.persistence.database import SessionLocal, get_db
from src.platform.persistence.models import (
    ActionableSignal, DisciplineEvent, ExecutionEvent, NextDayAction,
    SignalEvent, SignalLifecycleEvent, SignalPolicyDecision, PortfolioNotification,
    PortfolioRiskObservation,
)


router = APIRouter()


class IgnoreRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=255)


class ManualExecutionRequest(BaseModel):
    actual_action: str
    actual_qty: int = Field(gt=0)
    actual_price: float | None = Field(default=None, gt=0)
    executed_at: datetime
    notes: str = ""
    client_request_id: str | None = Field(default=None, min_length=8, max_length=64)


class ReconcileRequest(BaseModel):
    snapshot_id: int


def _signal(row: SignalEvent) -> dict:
    return {
        "signal_id": row.signal_id, "trace_id": row.trace_id,
        "trade_date": row.trade_date, "market": row.market,
        "symbol": row.symbol, "source": row.source,
        "source_agent": row.source_agent,
        "source_suggestion_id": row.source_suggestion_id,
        "plan_version": row.plan_version,
        "daily_plan_version": row.daily_plan_version,
        "truth_snapshot_id": row.truth_snapshot_id,
        "evidence_snapshot_id": row.evidence_snapshot_id,
        "action": row.action, "raw_action": row.raw_action,
        "current_weight": row.current_weight,
        "target_weight": row.target_weight,
        "qty_hint": row.qty_hint, "generated_at": row.generated_at,
        "expires_at": row.expires_at, "status": row.status,
        "reason_codes": row.reason_codes or [],
    }


def _execution(row: ExecutionEvent) -> dict:
    return {
        "execution_id": row.execution_id, "signal_id": row.signal_id,
        "planned_action": row.planned_action, "planned_qty": row.planned_qty,
        "actual_action": row.actual_action, "actual_qty": row.actual_qty,
        "actual_price": row.actual_price, "execution_source": row.execution_source,
        "executed_at": row.executed_at, "result": row.result,
        "reconcile_status": row.reconcile_status,
        "reconcile_truth_snapshot_id": row.reconcile_truth_snapshot_id,
        "notes": row.notes,
    }


@router.get("/signals")
def list_signals(limit: int = 50, db: Session = Depends(get_db)):
    rows = (db.query(SignalEvent)
            .order_by(SignalEvent.generated_at.desc())
            .limit(min(max(limit, 1), 200)).all())
    return [_signal(row) for row in rows]


@router.get("/risk-observations")
def list_risk_observations(trade_date: str | None = None, limit: int = 50,
                           db: Session = Depends(get_db)):
    """Authenticated Shadow ledger; observations never grant trade approval."""
    query = db.query(PortfolioRiskObservation)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    rows = query.order_by(PortfolioRiskObservation.observed_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [{
        "id": row.id, "trade_date": row.trade_date, "symbol": row.symbol,
        "observation_type": row.observation_type, "severity": row.severity,
        "source_signal_ids": row.source_signal_ids,
        "market_evidence_snapshot_id": row.market_evidence_snapshot_id,
        "level_snapshot_id": row.level_snapshot_id,
        "data_quality": row.data_quality,
        "execution_readiness": row.execution_readiness,
        "notice_outcome": row.notice_outcome,
        "notice_reason": row.notice_reason,
        "notification_id": row.notification_id,
        "observed_at": row.observed_at, "expires_at": row.expires_at,
    } for row in rows]


@router.get("/signals/{signal_id}")
def get_signal(signal_id: str, db: Session = Depends(get_db)):
    row = db.get(SignalEvent, signal_id)
    if row is None:
        raise HTTPException(404, "信号不存在")
    policy = db.query(SignalPolicyDecision).filter_by(signal_id=signal_id).order_by(SignalPolicyDecision.id).all()
    lifecycle = db.query(SignalLifecycleEvent).filter_by(signal_id=signal_id).order_by(SignalLifecycleEvent.id).all()
    executions = db.query(ExecutionEvent).filter_by(signal_id=signal_id).order_by(ExecutionEvent.created_at).all()
    discipline = db.query(DisciplineEvent).filter_by(signal_id=signal_id).order_by(DisciplineEvent.id).all()
    approved = db.get(ActionableSignal, signal_id)
    return {
        **_signal(row),
        "actionable": {
            "approved_qty": approved.approved_qty,
            "approved_weight": approved.approved_weight,
            "policy_version": approved.policy_version,
        } if approved else None,
        "policy_decisions": [{
            "rule_id": item.rule_id, "decision": item.decision,
            "reason_codes": item.reason_codes or [], "input_hash": item.input_hash,
        } for item in policy],
        "lifecycle": [{
            "from_status": item.from_status, "to_status": item.to_status,
            "reason": item.reason, "occurred_at": item.occurred_at,
        } for item in lifecycle],
        "executions": [_execution(item) for item in executions],
        "discipline": [{
            "event_type": item.event_type, "details": item.details or {},
            "created_at": item.created_at,
        } for item in discipline],
    }


@router.post("/signals/{signal_id}/ignore")
def ignore_signal(signal_id: str, body: IgnoreRequest, db: Session = Depends(get_db)):
    try:
        row = transition_signal(db, signal_id, "IGNORED", reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _signal(row)


@router.post("/signals/{signal_id}/executions")
async def create_manual_execution(signal_id: str, body: ManualExecutionRequest, db: Session = Depends(get_db)):
    try:
        row = record_manual_execution(
            db, signal_id=signal_id, actual_action=body.actual_action,
            actual_qty=body.actual_qty, actual_price=body.actual_price,
            executed_at=body.executed_at, notes=body.notes,
            client_request_id=body.client_request_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    notice = db.query(PortfolioNotification).filter_by(semantic_key=f"execution:{row.execution_id}").first()
    if notice and notice.delivery_status == "PENDING":
        await dispatch_portfolio_notice(notice.id, db_factory=SessionLocal)
    return _execution(row)


@router.post("/executions/{execution_id}/reconcile")
def reconcile_manual_execution(execution_id: str, body: ReconcileRequest, db: Session = Depends(get_db)):
    try:
        row = reconcile_execution(db, execution_id, body.snapshot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _execution(row)


@router.get("/next-day")
def list_next_day_actions(db: Session = Depends(get_db)):
    rows = db.query(NextDayAction).filter(NextDayAction.status != "CLOSED").order_by(NextDayAction.due_trade_date).all()
    return [{
        "id": row.id, "signal_id": row.signal_id,
        "market": row.market, "symbol": row.symbol,
        "due_trade_date": row.due_trade_date,
        "pending_qty": row.pending_qty, "reason": row.reason,
        "status": row.status,
    } for row in rows]
