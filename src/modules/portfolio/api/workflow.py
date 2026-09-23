"""Read-only P7 schedule and frozen daily-plan evidence."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.modules.portfolio.daily_workflow import FIXED_STEPS
from src.platform.persistence.database import get_db
from src.platform.persistence.models import DailyPortfolioPlan, PortfolioWorkflowRun

router = APIRouter()


@router.get("/schedule")
def schedule():
    return {"timezone": "Asia/Shanghai", "fixed_steps": [
        {"step": step, "time": hhmm} for step, hhmm in FIXED_STEPS],
        "monitor": {"hard_risk": "60s", "feature_refresh": "5m",
                    "windows": ["09:30-11:30", "13:00-14:30"]},
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
    return [{"id": r.id, "trade_date": r.trade_date, "version": r.version,
             "status": r.status, "truth_snapshot_id": r.truth_snapshot_id,
             "model_run_id": r.model_run_id, "prompt_id": r.prompt_id,
             "prompt_version": r.prompt_version, "input_hash": r.input_hash,
             "output_hash": r.output_hash, "payload": r.payload,
             "created_at": r.created_at} for r in rows]
