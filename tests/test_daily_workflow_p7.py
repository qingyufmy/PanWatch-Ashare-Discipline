"""P7 simulated trading day, idempotency and missed-slot recovery."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.daily_workflow import (
    _all_due_slots, _premarket_plan, _simple_step, recover_missed, run_step,
)
from src.modules.portfolio.model_router import ModelResult
from src.modules.portfolio.prompt_registry import PortfolioActionPlan
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    DailyPortfolioPlan, ModelRun, PortfolioTruthPosition, PortfolioTruthSnapshot,
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
    assert ("HARD_RISK", datetime(2026, 9, 23, 14, 55, tzinfo=SH)) in slots
    assert ("HARD_RISK", datetime(2026, 9, 23, 15, 0, tzinfo=SH)) in slots
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


def test_risk_group_a_then_a_b_then_a_does_not_realert_a(monkeypatch):
    from src.modules.portfolio import daily_workflow

    sequence = [["A"], ["A", "B"], ["A"]]
    sent = set()
    attempts = []

    def risk(*_args, **_kwargs):
        members = sequence.pop(0)
        return {"status": "SUCCEEDED", "positions": [{
            "symbol": symbol, "color": "RED", "reason": "HARD_STOP", "price": 10.0,
            "current_stop": 10.1, "market_data_asof": "2026-09-23T10:00:00+08:00",
            "market_data_source": "fixture", "plan_version": 1,
        } for symbol in members]}

    async def notify(**kwargs):
        attempts.append(kwargs["key"])
        if kwargs["key"] in sent:
            return {"status": "SKIPPED"}
        sent.add(kwargs["key"])
        return {"status": "SENT"}

    monkeypatch.setattr(daily_workflow, "_risk_scan", risk)
    monkeypatch.setattr(daily_workflow, "send_portfolio_notice", notify)
    async def replay():
        return [await _simple_step("HARD_RISK", "2026-09-23", lambda: None,
                                   datetime(2026, 9, 23, 10, minute, tzinfo=SH)) for minute in (0, 1, 2)]
    results = asyncio.run(replay())
    assert len(attempts) == 4
    assert len(sent) == 2
    assert [r["notification"]["status"] for r in results] == ["SENT", "SENT", "SKIPPED"]


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
        with factory() as db:
            db.add(ModelRun(
                run_id=result.run_id, trace_id="fixture-trace", role="FAST", profile_role="FAST",
                requested_model="fixture-model", input_hash="fixture-input", latency_ms=30000,
                status="OK", schema_valid=True, prompt_id="flash", prompt_version="1.0.0",
                started_at=datetime(2026, 9, 23, 0, 50),
                finished_at=datetime(2026, 9, 23, 0, 50, 30),
            ))
            db.commit()
        return plan, result

    async def fake_market(*_args, **_kwargs):
        return {"trade_date": "2026-09-23", "risk_tone": "UNVERIFIED",
                "completed_at": "2026-09-23T08:50:00+08:00", "holdings": []}

    monkeypatch.setattr("src.modules.portfolio.daily_workflow.collect_market_context", fake_market)
    monkeypatch.setattr("src.modules.portfolio.daily_workflow.run_portfolio_prompt", fake_prompt)
    result = asyncio.run(_premarket_plan("2026-09-23", factory,
                                         datetime(2026, 9, 23, 8, 50, tzinfo=SH),
                                         decision_clock=lambda: datetime(2026, 9, 23, 0, 51, tzinfo=timezone.utc)))
    assert result["status"] == "REVIEW" and result["coverage"] == 11
    with factory() as db:
        assert db.query(DailyPortfolioPlan).count() == 1
        signals = db.query(SignalEvent).all()
        assert len(signals) == 11
        assert all(s.daily_plan_version == 1 and s.status == "REVIEW_REQUIRED" for s in signals)
        assert all(s.generated_at >= datetime(2026, 9, 23, 0, 50, 30) for s in signals)
    engine.dispose()
