"""One-authority decision and deterministic message fixtures; no external sending."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.decision_router import record_position_decision
from src.modules.portfolio.notification_renderer import render_position_decision
from src.modules.portfolio.signal_journal import record_manual_execution, record_signal
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    ActionableSignal, PortfolioDecision, PortfolioFeatureSnapshot, PortfolioLevelSnapshot,
    PortfolioNotification, PortfolioTruthPosition, PortfolioTruthSnapshot,
    SignalEvent,
)


def test_action_reversal_appends_revision_and_per_position_outbox():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    day = base.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with factory() as db:
        truth = PortfolioTruthSnapshot(
            trade_date=day, phase="INTRADAY", source="fixture_broker",
            fetched_at=base, source_asof=base, freshness="FRESH", truth_status="TRUSTED",
            logical_hash="fixture", anomaly_flags=[], account_details=[],
        )
        db.add(truth)
        db.flush()
        db.add(PortfolioTruthPosition(snapshot_id=truth.id, market="CN", symbol="600001",
                                      name="样例", total_qty=1000, sellable_qty=800,
                                      today_locked_qty=200, avg_cost=10, account_details=[]))
        db.commit()

        notices = []
        for index, action in enumerate(("ADD", "EXIT", "ADD", "ADD")):
            generated = base + timedelta(minutes=index * 2)
            market_asof = generated - timedelta(seconds=20)
            feature = PortfolioFeatureSnapshot(
                symbol="sh600001", trade_date=day, version="fixture", source_vendor="fixture",
                market_asof=market_asof, fetched_at=generated, source_hash=f"feature-{index}",
                quality_status="OK", payload={"missing_minutes": []},
            )
            db.add(feature)
            db.flush()
            level = PortfolioLevelSnapshot(
                symbol="sh600001", trade_date=day, version="fixture", feature_snapshot_id=feature.id,
                market_asof=market_asof, source_hash=f"level-{index}", quality_status="CONFIRMED",
                payload={"symbol": "sh600001", "trade_date": day, "current_price": 20.18,
                         "market_data_asof": market_asof.replace(tzinfo=timezone.utc).isoformat(),
                         "vwap": 20.45, "broken_prior_support": 20.30,
                         "levels": {"S1": None, "S2": None, "R1": None, "R2": None}},
            )
            db.add(level)
            db.flush()
            signal, _ = record_signal(
                db, market="CN", symbol="sh600001", action=action, source="fixture",
                evidence={"nonce": index, "meta": {"feature_snapshot_id": feature.id,
                                                   "level_snapshot_id": level.id}},
                ttl_seconds=3600, qty_hint=100,
                generated_at=generated.replace(tzinfo=timezone.utc), commit=False,
            )
            signal.status = "APPROVED"
            db.add(ActionableSignal(signal_id=signal.signal_id, approved_qty=100,
                                    policy_version="fixture", input_hash="fixture", approved_at=generated))
            db.flush()
            decision, notice = record_position_decision(db, signal, now=(generated + timedelta(seconds=1)).replace(tzinfo=timezone.utc))
            notices.append(notice)
            db.commit()
        assert [n is not None for n in notices] == [True, True, True, False]
        decisions = db.query(PortfolioDecision).order_by(PortfolioDecision.revision).all()
        assert [d.action for d in decisions] == ["ADD", "EXIT", "ADD"]
        assert [d.current for d in decisions] == [False, False, True]
        outbox = db.query(PortfolioNotification).order_by(PortfolioNotification.decision_revision).all()
        assert [n.delivery_status for n in outbox] == ["CANCELLED", "CANCELLED", "PENDING"]
        assert all(n.symbol == "600001" for n in outbox)
        script = Path(__file__).resolve().parents[1] / "scripts" / "export-public-daily.py"
        spec = importlib.util.spec_from_file_location("portfolio_public_export_linkage", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        by_signal = {s.signal_id: s for s in db.query(SignalEvent).all()}
        by_decision = {d.id: d for d in decisions}
        assert module._price_evidence_linked(db, outbox[-1], by_decision, by_signal)
        wrong_ref = SimpleNamespace(decision_id=outbox[-1].decision_id,
                                    decision_revision=outbox[-1].decision_revision,
                                    signal_ids=["unrelated-signal"])
        assert not module._price_evidence_linked(db, wrong_ref, by_decision, by_signal)
        current_signal_id = decisions[-1].signal_id
        execution = record_manual_execution(
            db, signal_id=current_signal_id, actual_action="ADD", actual_qty=40,
            actual_price=20.18, executed_at=datetime.now(timezone.utc),
            client_request_id="fixture-execution-001",
        )
        assert execution.reconcile_status == "PENDING"
        assert decisions[-1].execution_status == "USER_REPORTED_UNRECONCILED"
        update = db.query(PortfolioNotification).filter_by(
            semantic_key=f"execution:{execution.execution_id}").one()
        assert update.delivery_status == "PENDING"
        assert "尚余60股" in update.body and "尚未经券商成交核对" in update.body
    engine.dispose()


def test_renderer_rejects_wrong_symbol_and_does_not_guess_quantity_or_model_price():
    with pytest.raises(ValueError, match="level_symbol_mismatch"):
        render_position_decision(symbol="600001", name="样例", action="REDUCE",
                                 decision_status="APPROVED", execution_status="NOT_EXECUTED",
                                 level={"symbol": "sh600002", "current_price": 20.18},
                                 approved_qty=300, total_qty=1000, sellable_qty=800,
                                 truth_trusted=True)
    title, body = render_position_decision(
        symbol="600001", name="样例", action="REDUCE",
        decision_status="REVIEW_REQUIRED", execution_status="NOT_EXECUTED",
        level={"symbol": "sh600001", "current_price": float("nan"),
               "market_data_asof": "2026-09-23T10:16:00+08:00", "vwap": None,
               "levels": {}},
        approved_qty=300, total_qty=1000, sellable_qty=800, truth_trusted=False,
    )
    assert title.startswith("减仓")
    assert "未授权具体股数" in body
    assert "300股" not in body and "nan" not in body.lower()


def test_decision_rejects_prior_day_truth_for_new_day_signal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    prior = datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)
    with factory() as db:
        truth = PortfolioTruthSnapshot(
            trade_date="2026-09-24", phase="EOD", source="user_attested",
            fetched_at=prior.replace(tzinfo=None), source_asof=prior.replace(tzinfo=None),
            freshness="FRESH", truth_status="REVIEW_ONLY", logical_hash="prior",
            anomaly_flags=[], account_details=[],
        )
        db.add(truth)
        db.flush()
        db.add(PortfolioTruthPosition(snapshot_id=truth.id, market="CN", symbol="600001",
                                      name="样例", total_qty=100, sellable_qty=100,
                                      today_locked_qty=0, avg_cost=10, account_details=[]))
        signal, _ = record_signal(
            db, market="CN", symbol="600001", action="HOLD", source="fixture",
            evidence={"schema_valid": True}, ttl_seconds=3600,
            generated_at=prior + timedelta(days=1), commit=False,
        )
        signal.status = "REVIEW_REQUIRED"
        db.flush()
        assert signal.trade_date == "2026-09-25"
        assert record_position_decision(db, signal, now=prior + timedelta(days=1, seconds=1)) == (None, None)
        assert db.query(PortfolioDecision).count() == 0
    engine.dispose()
