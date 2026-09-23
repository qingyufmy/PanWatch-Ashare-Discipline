"""P7 simulated trading day, idempotency and missed-slot recovery."""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.daily_workflow import (
    _all_due_slots, _premarket_plan, recover_missed, run_step,
)
from src.modules.portfolio.model_router import ModelResult
from src.modules.portfolio.prompt_registry import PortfolioActionPlan
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    DailyPortfolioPlan, PortfolioTruthPosition, PortfolioTruthSnapshot,
    PortfolioWorkflowRun, SignalEvent, SystemIssue,
)

SH = ZoneInfo("Asia/Shanghai")


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_simulated_day_all_slots_are_unique_and_replay_is_idempotent():
    engine, factory = _db()
    day = date(2026, 9, 23)
    slots = _all_due_slots(day)
    assert len(slots) > 250
    assert len({(step, at.strftime("%H:%M") if step in {"HARD_RISK", "FEATURE_REFRESH"} else "DAILY")
                for step, at in slots}) == len(slots)
    invoked = []

    async def fake(step, trade_date, db_factory, now):
        invoked.append((step, now.strftime("%H:%M")))
        return {"status": "SUCCEEDED", "step": step, "trade_date": trade_date}

    async def replay():
        for step, at in slots:
            await run_step(step, now=at, db_factory=factory, handler=fake,
                           calendar_check=lambda _: True)
        first = await run_step("PREMARKET_PLAN", now=datetime(2026, 9, 23, 8, 50, tzinfo=SH),
                               db_factory=factory, handler=fake, calendar_check=lambda _: True)
        assert first["status"] == "SUCCEEDED"

    asyncio.run(replay())
    assert len(invoked) == len(slots)
    with factory() as db:
        assert db.query(PortfolioWorkflowRun).count() == len(slots)
    engine.dispose()


def test_restart_records_missed_slots_without_backfilling_model_calls():
    engine, factory = _db()
    recover_missed(now=datetime(2026, 9, 22, 22, 0, tzinfo=SH), db_factory=factory,
                   calendar_check=lambda _: True)
    result = recover_missed(now=datetime(2026, 9, 23, 8, 10, tzinfo=SH), db_factory=factory,
                            calendar_check=lambda _: True)
    assert result["missed"] == 4  # 06:50, 07:20, 07:30, 08:00
    with factory() as db:
        assert db.query(PortfolioWorkflowRun).filter_by(trade_date="2026-09-23", status="MISSED").count() == 4
        issue = db.query(SystemIssue).filter_by(category="SCHEDULER_MISSED").one()
        assert issue.contexts[-1]["count"] == 4
    engine.dispose()


def test_batch_plan_creates_eleven_review_signals_with_daily_plan_link(monkeypatch):
    engine, factory = _db()
    with factory() as db:
        truth = PortfolioTruthSnapshot(
            trade_date="2026-09-23", phase="PREMARKET", source="fixture",
            fetched_at=datetime(2026, 9, 23, 0, 0), source_asof=datetime(2026, 9, 23, 0, 0),
            freshness="FRESH", truth_status="USER_ATTESTED_UNRECONCILED",
            logical_hash="fixture", anomaly_flags=[], account_details=[], cash=1000,
        )
        db.add(truth)
        db.flush()
        for i in range(11):
            db.add(PortfolioTruthPosition(snapshot_id=truth.id, market="CN", symbol=f"{600001+i}",
                                          name="fixture", total_qty=100, sellable_qty=100,
                                          today_locked_qty=0, avg_cost=10, account_details=[]))
        db.commit()

    async def fake_prompt(prompt_id, payload, **kwargs):
        assert len(payload["positions"]) == 11
        plan = PortfolioActionPlan.model_validate({
            "trade_date": payload["trade_date"],
            "portfolio_rationale": "Insufficient verified data",
            "proposals": [{"market": "CN", "symbol": p["symbol"], "action": "HOLD",
                           "confidence": 0.5, "rationale": "Unverified market evidence",
                           "evidence_refs": ["truth:fixture"]} for p in payload["positions"]],
        })
        result = ModelResult(content=plan.model_dump_json(), run_id="fixture-run", requested_role="FAST",
                             profile_role="FAST", requested_model="fixture-model",
                             reported_model="fixture-model", degraded=False)
        return plan, result

    monkeypatch.setattr("src.modules.portfolio.daily_workflow.run_portfolio_prompt", fake_prompt)
    result = asyncio.run(_premarket_plan("2026-09-23", factory,
                                         datetime(2026, 9, 23, 8, 50, tzinfo=SH)))
    assert result["status"] == "REVIEW" and result["coverage"] == 11
    with factory() as db:
        assert db.query(DailyPortfolioPlan).count() == 1
        signals = db.query(SignalEvent).all()
        assert len(signals) == 11
        assert all(s.daily_plan_version == 1 and s.status == "REVIEW_REQUIRED" for s in signals)
    engine.dispose()
