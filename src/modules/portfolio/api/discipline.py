"""P1 read and manual capture endpoints for portfolio truth and position plans."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import capture_truth, create_plan_version, current_freshness, current_plan, initialize_plans_from_truth
from src.platform.persistence.database import get_db
from src.platform.persistence.models import PortfolioTruthSnapshot, PositionPlan, PositionStateEvent

router = APIRouter()


class TruthCaptureRequest(BaseModel):
    phase: str = "MANUAL"
    sellable_equals_quantity: bool = False


class PlanCreateRequest(BaseModel):
    position_state: str
    thesis_state: str = "UNKNOWN"
    plan: dict = Field(default_factory=dict)
    reason: str = ""
    truth_snapshot_id: int | None = None
    expected_version: int | None = None


def _snapshot_response(row: PortfolioTruthSnapshot) -> dict:
    return {
        "id": row.id, "trade_date": row.trade_date, "phase": row.phase,
        "source": row.source, "fetched_at": row.fetched_at,
        "source_asof": row.source_asof, "freshness": row.freshness,
        "current_freshness": current_freshness(row),
        "truth_status": row.truth_status, "logical_hash": row.logical_hash,
        "anomaly_flags": row.anomaly_flags or [], "account_details": row.account_details or [],
        "cash": row.cash, "nav": row.nav,
        "positions": [{
            "market": p.market, "symbol": p.symbol, "name": p.name,
            "total_qty": p.total_qty, "sellable_qty": p.sellable_qty,
            "today_locked_qty": p.today_locked_qty, "avg_cost": p.avg_cost,
            "market_value": p.market_value, "account_details": p.account_details or [],
        } for p in row.positions],
    }


def _plan_response(row: PositionPlan) -> dict:
    return {
        "id": row.id, "market": row.market, "symbol": row.symbol,
        "version": row.version, "position_state": row.position_state,
        "thesis_state": row.thesis_state, "plan": row.plan,
        "logical_hash": row.logical_hash, "truth_snapshot_id": row.truth_snapshot_id,
        "created_at": row.created_at,
    }


@router.post("/truth/capture")
def capture_local_truth(body: TruthCaptureRequest, db: Session = Depends(get_db)):
    if body.phase not in {"PREMARKET", "EOD", "MANUAL"}:
        raise HTTPException(400, "phase 必须是 PREMARKET、EOD 或 MANUAL")
    row = capture_truth(db, phase=body.phase, sellable_equals_quantity=body.sellable_equals_quantity)
    return _snapshot_response(row)


@router.get("/truth/latest")
def latest_truth(db: Session = Depends(get_db)):
    row = db.query(PortfolioTruthSnapshot).order_by(PortfolioTruthSnapshot.id.desc()).first()
    if not row:
        raise HTTPException(404, "没有持仓真相快照")
    return _snapshot_response(row)


@router.get("/truth/history")
def truth_history(limit: int = 30, db: Session = Depends(get_db)):
    rows = (db.query(PortfolioTruthSnapshot)
            .order_by(PortfolioTruthSnapshot.id.desc()).limit(min(max(limit, 1), 100)).all())
    return [_snapshot_response(row) for row in rows]


@router.post("/truth/{snapshot_id}/initialize-plans")
def initialize_plans(snapshot_id: int, db: Session = Depends(get_db)):
    try:
        rows = initialize_plans_from_truth(db, snapshot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": len(rows), "plans": [_plan_response(row) for row in rows]}


@router.get("/plans/{market}/{symbol}")
def get_current_plan(market: str, symbol: str, db: Session = Depends(get_db)):
    row = current_plan(db, market, symbol)
    if not row:
        raise HTTPException(404, "没有有效持仓计划")
    return _plan_response(row)


@router.get("/plans/{market}/{symbol}/history")
def get_plan_history(market: str, symbol: str, db: Session = Depends(get_db)):
    rows = (db.query(PositionPlan).filter_by(market=market, symbol=symbol)
            .order_by(PositionPlan.version.desc()).all())
    return [_plan_response(row) for row in rows]


@router.get("/plans/{market}/{symbol}/events")
def get_plan_events(market: str, symbol: str, db: Session = Depends(get_db)):
    rows = (db.query(PositionStateEvent).filter_by(market=market, symbol=symbol)
            .order_by(PositionStateEvent.id.desc()).all())
    return [{
        "id": e.id, "from_state": e.from_state, "to_state": e.to_state,
        "from_thesis": e.from_thesis, "to_thesis": e.to_thesis,
        "decision": e.decision, "reason": e.reason,
        "plan_version": e.plan_version, "created_at": e.created_at,
    } for e in rows]


@router.post("/plans/{market}/{symbol}")
def add_plan_version(market: str, symbol: str, body: PlanCreateRequest, db: Session = Depends(get_db)):
    try:
        created, event = create_plan_version(
            db, market=market, symbol=symbol,
            position_state=body.position_state, thesis_state=body.thesis_state,
            plan=body.plan, reason=body.reason,
            truth_snapshot_id=body.truth_snapshot_id,
            expected_version=body.expected_version,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if created is None:
        raise HTTPException(409, f"计划未创建，已记录审计事件: {event.reason}")
    return _plan_response(created)
