"""Read-only P7 schedule and frozen daily-plan evidence."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.modules.portfolio.daily_workflow import FIXED_STEPS
from src.platform.persistence.database import get_db
from src.platform.persistence.models import (
    DailyPortfolioPlan, EvidenceSnapshot, PortfolioDecision, PortfolioNotification, PortfolioWorkflowRun,
    SignalEvent,
)

router = APIRouter()


@router.get("/schedule")
def schedule():
    return {"timezone": "Asia/Shanghai", "fixed_steps": [
        {"step": step, "time": hhmm} for step, hhmm in FIXED_STEPS],
        "monitor": {"hard_risk": "60s", "feature_refresh": "5m",
                    "windows": ["09:30-11:30", "13:00-15:00"]},
        "weekly_review": "Friday 20:30"}


@router.get("/runs")
def runs(trade_date: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(PortfolioWorkflowRun)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    rows = query.order_by(PortfolioWorkflowRun.started_at.desc()).limit(min(max(limit, 1), 500)).all()
    return [{"run_id": r.run_id, "trade_date": r.trade_date, "step": r.step,
             "slot": r.slot, "status": r.status, "payload": r.payload,
             "input_hash": r.input_hash, "output_hash": r.output_hash,
             "started_at": r.started_at, "finished_at": r.finished_at} for r in rows]


@router.get("/plans")
def plans(trade_date: str | None = None, limit: int = 30, db: Session = Depends(get_db)):
    query = db.query(DailyPortfolioPlan)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    rows = query.order_by(DailyPortfolioPlan.id.desc()).limit(min(max(limit, 1), 100)).all()
    macro = {r.id: db.get(EvidenceSnapshot, r.macro_evidence_snapshot_id)
             if r.macro_evidence_snapshot_id else None for r in rows}
    return [{"id": r.id, "trade_date": r.trade_date, "version": r.version,
             "status": r.status, "truth_snapshot_id": r.truth_snapshot_id,
             "macro_evidence_snapshot_id": r.macro_evidence_snapshot_id,
             "market_context": macro[r.id].payload if macro[r.id] else None,
             "model_run_id": r.model_run_id, "prompt_id": r.prompt_id,
             "prompt_version": r.prompt_version, "input_hash": r.input_hash,
             "output_hash": r.output_hash, "payload": r.payload,
             "created_at": r.created_at} for r in rows]


@router.get("/notifications")
def notifications(trade_date: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(PortfolioNotification)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    rows = query.order_by(PortfolioNotification.queued_at.desc()).limit(min(max(limit, 1), 500)).all()
    return [{"notification_id": r.id, "trade_date": r.trade_date, "symbol": r.symbol,
             "decision_id": r.decision_id, "decision_revision": r.decision_revision,
             "title": r.title, "reason": r.reason, "priority": r.priority,
             "delivery_status": r.delivery_status, "suppression_reason": r.suppression_reason,
             "queued_at": r.queued_at, "provider_accepted_at": r.provider_accepted_at,
             "ack_status": r.ack_status, "retry_count": r.retry_count,
             "content_hash": r.rendered_content_hash} for r in rows]


@router.get("/decisions")
def decisions(trade_date: str | None = None, current_only: bool = True,
              limit: int = 100, symbol: str | None = None, db: Session = Depends(get_db)):
    query = db.query(PortfolioDecision)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    if symbol:
        query = query.filter_by(symbol=symbol)
    if current_only:
        query = query.filter_by(current=True)
    rows = query.order_by(PortfolioDecision.id.desc()).limit(min(max(limit, 1), 500)).all()
    signal_ids = [r.signal_id for r in rows]
    signals = {r.signal_id: r for r in db.query(SignalEvent).filter(SignalEvent.signal_id.in_(signal_ids)).all()} if signal_ids else {}
    evidence_ids = [r.evidence_snapshot_id for r in signals.values()]
    evidence = {r.id: r for r in db.query(EvidenceSnapshot).filter(EvidenceSnapshot.id.in_(evidence_ids)).all()} if evidence_ids else {}
    return [{"decision_id": r.id, "trade_date": r.trade_date, "symbol": r.symbol,
             "revision": r.revision, "signal_id": r.signal_id, "action": r.action,
             "decision_status": r.decision_status, "risk_status": r.risk_status,
             "data_status": r.data_status, "execution_status": r.execution_status,
             "level_snapshot_id": r.level_snapshot_id, "current": r.current,
             "created_at": _utc_iso(r.created_at), "expires_at": _utc_iso(r.expires_at),
             "rationale": (evidence[signals[r.signal_id].evidence_snapshot_id].payload or {}).get("rationale")
             if r.signal_id in signals and signals[r.signal_id].evidence_snapshot_id in evidence else None,
             "target_weight": signals[r.signal_id].target_weight if r.signal_id in signals else None,
             "qty_hint": signals[r.signal_id].qty_hint if r.signal_id in signals else None,
             "source_agent": signals[r.signal_id].source_agent if r.signal_id in signals else None,
             } for r in rows]


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


@router.get("/advice")
def current_advice(db: Session = Depends(get_db)):
    """One current intraday advice per held CN symbol, with fresh hard risk taking precedence."""
    now = datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    rows = (db.query(PortfolioDecision).filter_by(trade_date=day, current=True)
            .order_by(PortfolioDecision.id.asc()).all())
    result = {row.symbol: {
        "symbol": row.symbol, "action": row.action, "decision_id": row.id,
        "decision_status": row.decision_status, "created_at": _utc_iso(row.created_at),
        "expires_at": _utc_iso(row.expires_at), "source": "portfolio_decision",
    } for row in rows if row.expires_at > now.replace(tzinfo=None)}
    risk = (db.query(PortfolioWorkflowRun).filter_by(trade_date=day, step="HARD_RISK")
            .order_by(PortfolioWorkflowRun.started_at.desc()).first())
    if (risk and risk.status in {"SUCCEEDED", "REVIEW"} and risk.finished_at
            and timedelta(0) <= now.replace(tzinfo=None) - risk.finished_at <= timedelta(seconds=180)):
        for position in (risk.payload or {}).get("positions", []):
            if position.get("color") == "RED" and position.get("reason") == "HARD_STOP":
                result[position["symbol"]] = {
                    "symbol": position["symbol"], "action": "RISK_REVIEW",
                    "decision_id": None, "decision_status": "REVIEW_REQUIRED",
                    "created_at": _utc_iso(risk.finished_at),
                    "expires_at": _utc_iso(risk.finished_at + timedelta(seconds=180)),
                    "source": "hard_risk", "reason": "观察价触及，请人工核对行情与持仓计划",
                }
    return {"trade_date": day, "items": result}
