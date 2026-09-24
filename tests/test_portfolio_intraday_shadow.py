"""Frozen intraday replay keeps risk review and post-model HOLD distinct."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio import daily_workflow
from src.modules.portfolio.model_router import ModelResult
from src.modules.portfolio.prompt_registry import PortfolioActionPlan
from src.modules.portfolio.signal_journal import record_signal
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    ActionableSignal, ModelRun, PortfolioDecision, PortfolioNotification,
    PortfolioRiskObservation, PortfolioTruthPosition, PortfolioTruthSnapshot, SignalEvent,
)

SH = ZoneInfo("Asia/Shanghai")


def test_risk_off_old_reduce_and_new_hold_have_real_causal_times(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    slot = datetime(2026, 9, 24, 13, 30, tzinfo=SH)
    model_finished = datetime(2026, 9, 24, 5, 31, tzinfo=timezone.utc)
    decision_at = datetime(2026, 9, 24, 5, 31, 10, tzinfo=timezone.utc)
    with factory() as db:
        truth = PortfolioTruthSnapshot(
            trade_date="2026-09-24", phase="PREMARKET", source="user_attested",
            fetched_at=slot.astimezone(timezone.utc).replace(tzinfo=None),
            source_asof=slot.astimezone(timezone.utc).replace(tzinfo=None),
            freshness="FRESH", truth_status="REVIEW_ONLY", logical_hash="truth",
            anomaly_flags=[], account_details=[],
        )
        db.add(truth)
        db.flush()
        db.add(PortfolioTruthPosition(snapshot_id=truth.id, market="CN", symbol="600001",
                                      name="样例", total_qty=100, sellable_qty=100,
                                      today_locked_qty=0, avg_cost=10, account_details=[]))
        old, _ = record_signal(
            db, market="CN", symbol="600001", action="REDUCE", source="AGENT",
            evidence={"reason": "unverified old direction"}, ttl_seconds=3600,
            generated_at=datetime(2026, 9, 24, 5, 25, tzinfo=timezone.utc), commit=False,
        )
        old.status = "REVIEW_REQUIRED"
        db.commit()
        old_signal_id = old.signal_id

    async def market_context(*_args, **_kwargs):
        return {
            "trade_date": "2026-09-24", "phase": "INTRADAY", "risk_tone": "RISK_OFF",
            "indices": [{"quality": "FRESH", "change_pct": -1.0} for _ in range(4)],
            "holdings": [{"symbol": "600001", "quality": "FRESH", "source": "fixture",
                          "source_asof": "2026-09-24T13:30:00+08:00", "price": 10.0}],
            "candidate_breadth": {"scope": "candidate_sample_not_market_wide"},
            "global_tech": [], "boards": [], "limits": [],
        }

    async def model_prompt(_prompt_id, payload, **_kwargs):
        plan = PortfolioActionPlan.model_validate({
            "trade_date": payload["trade_date"], "portfolio_rationale": "Hold pending evidence",
            "proposals": [{"market": "CN", "symbol": "600001", "action": "HOLD",
                           "confidence": 0.7, "rationale": "No stock-level exit proof",
                           "evidence_refs": ["macro:fixture"]}],
        })
        result = ModelResult(content=plan.model_dump_json(), run_id="model-1330",
                             requested_role="FAST", profile_role="FAST",
                             requested_model="fixture", reported_model="fixture", degraded=False)
        with factory() as db:
            db.add(ModelRun(run_id=result.run_id, trace_id="fixture", role="FAST",
                            profile_role="FAST", requested_model="fixture", input_hash="input",
                            latency_ms=60000, status="OK", schema_valid=True,
                            prompt_id="review", prompt_version="1.0.3",
                            started_at=datetime(2026, 9, 24, 5, 30),
                            finished_at=model_finished.replace(tzinfo=None)))
            db.commit()
        return plan, result

    monkeypatch.setattr(daily_workflow, "collect_market_context", market_context)
    monkeypatch.setattr(daily_workflow, "run_portfolio_prompt", model_prompt)
    result = asyncio.run(daily_workflow._intraday_adjustment(
        "2026-09-24", factory, slot, "AFTERNOON_ADJUST",
        decision_clock=lambda: decision_at,
    ))
    assert result["status"] == "REVIEW"
    assert len(result["risk_observation_ids"]) == 1
    with factory() as db:
        signal = db.query(SignalEvent).filter_by(source="intraday_portfolio_plan").one()
        approved = db.get(ActionableSignal, signal.signal_id)
        decision = db.query(PortfolioDecision).filter_by(signal_id=signal.signal_id).one()
        observation = db.query(PortfolioRiskObservation).one()
        notice = db.get(PortfolioNotification, observation.notification_id)
        assert signal.action == "HOLD" and signal.generated_at >= model_finished.replace(tzinfo=None)
        assert approved.approved_at >= signal.generated_at
        assert decision.created_at >= approved.approved_at
        assert observation.source_signal_ids == [old_signal_id]
        assert notice.delivery_status == "SUPPRESSED"
        assert db.get(SignalEvent, old_signal_id).status == "REVIEW_REQUIRED"
    engine.dispose()
