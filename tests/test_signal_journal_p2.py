"""P2 journal contracts: evidence identity, approval boundary and reconciliation."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from src.modules.automation import suggestion_pool
from src.modules.portfolio.discipline import capture_truth
from src.modules.portfolio.signal_journal import (
    record_manual_execution, record_signal, reconcile_execution, transition_signal,
)
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    Account, ActionableSignal, DisciplineEvent, EvidenceSnapshot, NextDayAction, Position,
    SignalEvent, SignalLifecycleEvent, Stock, StockSuggestion,
)
from src.platform.scheduling import trading_calendar


@pytest.mark.parametrize("rating,expected", [
    ("buy", "OPEN"), ("overweight", "ADD"), ("hold", "HOLD"),
    ("underweight", "REDUCE"), ("sell", "EXIT"),
])
def test_five_tier_rating_preserved_in_journal(rating, expected):
    assert suggestion_pool._journal_action("sell", {"rating_raw": rating}) == expected


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_signal_dedupe_frozen_evidence_and_policy_boundary():
    engine, db = _db()
    try:
        now = datetime.now(timezone.utc)
        signal, created = record_signal(
            db, market="CN", symbol="sh600001", action="OPEN",
            source="agent", evidence={"quote": {"price": 10}},
            ttl_seconds=300, generated_at=now,
        )
        duplicate, created_again = record_signal(
            db, market="CN", symbol="sh600001", action="OPEN",
            source="agent", evidence={"quote": {"price": 10}},
            ttl_seconds=300, generated_at=now,
        )
        assert created and not created_again
        assert duplicate.signal_id == signal.signal_id
        assert db.query(EvidenceSnapshot).count() == 1
        assert db.query(SignalLifecycleEvent).count() == 1
        assert signal.status == "GENERATED"
        with pytest.raises(ValueError, match="policy_gate_required"):
            transition_signal(db, signal.signal_id, "APPROVED", reason="manual")
        transition_signal(db, signal.signal_id, "REVIEW_REQUIRED", reason="P3_PENDING")
        assert signal.status == "REVIEW_REQUIRED"
        deviation = record_manual_execution(
            db, signal_id=signal.signal_id, actual_action="OPEN",
            actual_qty=100, actual_price=10, executed_at=now,
        )
        assert deviation.result == "USER_REPORTED_OUT_OF_POLICY"
        assert db.query(DisciplineEvent).one().event_type == "UNAUTHORIZED_ACTION"
    finally:
        db.close()
        engine.dispose()


def test_reduce_execution_is_bounded_and_reconciled_without_broker_claim(monkeypatch):
    engine, db = _db()
    try:
        account = Account(name="账户", available_funds=1000, enabled=True)
        stock = Stock(symbol="sh600001", name="测试", market="CN")
        db.add_all([account, stock])
        db.flush()
        held = Position(account_id=account.id, stock_id=stock.id, quantity=500, cost_price=10)
        db.add(held)
        db.commit()
        before = capture_truth(db, phase="MANUAL")
        now = datetime.now(timezone.utc)
        signal, _ = record_signal(
            db, market="CN", symbol="sh600001", action="REDUCE",
            source="test", evidence={"before": before.logical_hash},
            ttl_seconds=300, qty_hint=100, generated_at=now,
        )
        db.add(ActionableSignal(signal_id=signal.signal_id, approved_qty=100, policy_version="fixture", input_hash="x", approved_at=now.replace(tzinfo=None)))
        db.flush()
        transition_signal(db, signal.signal_id, "APPROVED", reason="fixture")
        event = record_manual_execution(
            db, signal_id=signal.signal_id, actual_action="REDUCE",
            actual_qty=100, actual_price=11, executed_at=now,
        )
        assert event.reconcile_status == "PENDING"
        held.quantity = 400
        db.commit()
        after = capture_truth(db, phase="MANUAL")
        reconciled = reconcile_execution(db, event.execution_id, after.id)
        assert reconciled.reconcile_status == "QUANTITY_MATCHED_UNVERIFIED"
        assert db.query(DisciplineEvent).count() == 2
        assert db.query(NextDayAction).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_new_buy_creates_next_day_action_only_with_confirmed_calendar(monkeypatch):
    engine, db = _db()
    try:
        now = datetime.now(timezone.utc)
        day = now.astimezone(trading_calendar.ZoneInfo("Asia/Shanghai")).date()
        next_day = day + timedelta(days=1)
        monkeypatch.setattr(trading_calendar, "_CN_TRADING_DATES", frozenset({day, next_day}))
        monkeypatch.setattr(trading_calendar, "_CN_RANGE", (day, next_day))
        signal, _ = record_signal(
            db, market="CN", symbol="sh600001", action="OPEN", source="test",
            evidence={"price": 10}, ttl_seconds=300, generated_at=now,
        )
        db.add(ActionableSignal(signal_id=signal.signal_id, approved_qty=100, policy_version="fixture", input_hash="x", approved_at=now.replace(tzinfo=None)))
        db.flush()
        transition_signal(db, signal.signal_id, "APPROVED", reason="fixture")
        record_manual_execution(
            db, signal_id=signal.signal_id, actual_action="OPEN",
            actual_qty=100, actual_price=10, executed_at=now,
        )
        next_action = db.query(NextDayAction).one()
        assert next_action.due_trade_date == next_day.isoformat()
        assert next_action.pending_qty == 100
        assert next_action.status == "OPEN"
    finally:
        db.close()
        engine.dispose()


def test_saved_suggestion_and_signal_commit_together(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(suggestion_pool, "SessionLocal", sessionmaker(bind=engine))
    try:
        assert suggestion_pool.save_suggestion(
            stock_symbol="sh600001", stock_name="测试", stock_market="CN",
            action="reduce", action_label="减仓", agent_name="intraday_monitor",
            reason="结构转弱", prompt_context="frozen input", ai_response="frozen output",
        )
        with Session(engine) as db:
            suggestion = db.query(StockSuggestion).one()
            signal = db.query(SignalEvent).one()
            evidence = db.get(EvidenceSnapshot, signal.evidence_snapshot_id)
            assert signal.source_suggestion_id == suggestion.id
            assert signal.action == "REDUCE"
            assert signal.status == "REVIEW_REQUIRED"
            assert evidence.payload["prompt_context"] == "frozen input"
            assert evidence.payload["ai_response"] == "frozen output"
    finally:
        engine.dispose()
