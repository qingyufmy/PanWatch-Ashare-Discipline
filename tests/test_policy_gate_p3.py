"""P3 deterministic approval and fail-closed boundaries."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import create_plan_version
from src.modules.portfolio.policy_gate import RULE_IDS, evaluate_signal
from src.modules.automation.suggestion_pool import _journal_action
from src.modules.portfolio.signal_journal import record_signal
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    ActionableSignal, NextDayAction, PortfolioTruthPosition,
    PortfolioTruthSnapshot, SecurityRule, SignalPolicyDecision,
)
from src.platform.scheduling import trading_calendar


def _case(*, sellable=800, thesis="VALID", stop=9.0, now=None):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    now = now or datetime.now(timezone.utc)
    naive = now.replace(tzinfo=None)
    truth = PortfolioTruthSnapshot(
        trade_date=now.astimezone(trading_calendar.ZoneInfo("Asia/Shanghai")).date().isoformat(),
        phase="PREMARKET", source="fixture_broker", fetched_at=naive,
        source_asof=naive, freshness="FRESH", truth_status="TRUSTED",
        logical_hash="truth-fixture", anomaly_flags=[], account_details=[], cash=10000, nav=100000,
    )
    db.add(truth)
    db.flush()
    db.add(PortfolioTruthPosition(
        snapshot_id=truth.id, market="CN", symbol="sh600001", name="测试",
        total_qty=1000, sellable_qty=sellable,
        today_locked_qty=1000 - sellable, avg_cost=10, account_details=[],
    ))
    db.add(SecurityRule(market="CN", symbol="sh600001", price_tick=0.01,
                        min_buy_qty=100, buy_step=100, min_sell_qty=100, sell_step=100,
                        source="fixture_broker",
                        source_asof=naive, quality_status="VERIFIED"))
    db.commit()
    create_plan_version(
        db, market="CN", symbol="sh600001", position_state="CORE",
        thesis_state=thesis,
        plan={"position": {"max_weight": 0.2}, "risk": {"current_stop": stop}},
        reason="fixture", truth_snapshot_id=truth.id, expected_version=0,
    )
    return engine, db, now


def _signal(db, now, action, *, qty=None, target=None, price=10.5, new_stop=None, asof=None, schema=True):
    evidence = {
        "schema_valid": schema,
        "market_data_asof": (asof or now).isoformat(),
        "market_data_source": "tencent",
        "meta": {"quote": {"current_price": price}},
    }
    if new_stop is not None:
        evidence["new_stop"] = new_stop
    return record_signal(
        db, market="CN", symbol="sh600001", action=action, source="fixture",
        evidence=evidence, ttl_seconds=300, qty_hint=qty,
        target_weight=target, generated_at=now,
    )[0]


def test_all_policy_rules_persist_before_approval():
    engine, db, now = _case()
    try:
        signal = _signal(db, now, "REDUCE", qty=999, target=0.0945)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "APPROVED"
        envelope = db.get(ActionableSignal, signal.signal_id)
        assert envelope.approved_qty == 100
        decisions = db.query(SignalPolicyDecision).filter_by(signal_id=signal.signal_id).all()
        assert {d.rule_id for d in decisions} == set(RULE_IDS)
        assert all(d.decision == "PASS" and d.input_hash for d in decisions)
        evaluate_signal(db, signal.signal_id, now=now)
        assert db.query(SignalPolicyDecision).count() == len(RULE_IDS)
    finally:
        db.close()
        engine.dispose()


def test_t_plus_one_splits_sellable_and_deferred_quantity(monkeypatch):
    engine, db, now = _case(sellable=300)
    try:
        day = now.astimezone(trading_calendar.ZoneInfo("Asia/Shanghai")).date()
        next_day = day + timedelta(days=1)
        monkeypatch.setattr(trading_calendar, "_CN_TRADING_DATES", frozenset({day, next_day}))
        monkeypatch.setattr(trading_calendar, "_CN_RANGE", (day, next_day))
        signal = _signal(db, now, "EXIT", qty=1000)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "APPROVED"
        assert db.get(ActionableSignal, signal.signal_id).approved_qty == 300
        next_action = db.query(NextDayAction).one()
        assert next_action.pending_qty == 700
        assert next_action.reason == "EXIT_REMAINING_POSITION"
    finally:
        db.close()
        engine.dispose()


def test_stop_widening_and_invalid_thesis_block_add():
    engine, db, now = _case(thesis="INVALID")
    try:
        signal = _signal(db, now, "ADD", qty=100, target=0.1, new_stop=8.5)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "POLICY_REJECTED"
        assert db.get(ActionableSignal, signal.signal_id) is None
        reasons = [r for d in db.query(SignalPolicyDecision).all() for r in d.reason_codes]
        assert "STOP_WIDENING_NOT_ALLOWED" in reasons
        assert "THESIS_INVALID_NO_EXPANSION" in reasons
    finally:
        db.close()
        engine.dispose()


def test_stale_quote_or_untrusted_truth_never_approves():
    engine, db, now = _case()
    try:
        signal = _signal(db, now, "REDUCE", qty=100, target=0.0945, asof=now - timedelta(minutes=10))
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "REVIEW_REQUIRED"
        assert db.get(ActionableSignal, signal.signal_id) is None
    finally:
        db.close()
        engine.dispose()


def test_missing_schema_fails_closed():
    engine, db, now = _case()
    try:
        signal = _signal(db, now, "REDUCE", qty=100, target=0.0945, schema=False)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "REVIEW_REQUIRED"
        assert db.get(ActionableSignal, signal.signal_id) is None
    finally:
        db.close()
        engine.dispose()


def test_missing_verified_security_rule_blocks_quantity_approval():
    engine, db, now = _case()
    try:
        db.query(SecurityRule).delete()
        db.commit()
        signal = _signal(db, now, "EXIT", qty=1000)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "REVIEW_REQUIRED"
        assert db.get(ActionableSignal, signal.signal_id) is None
        assert db.query(SignalPolicyDecision).filter_by(signal_id=signal.signal_id,
                                                         rule_id="SECURITY_RULES").one().decision == "REVIEW"
    finally:
        db.close()
        engine.dispose()


def test_add_quantity_comes_from_trusted_nav_cash_and_security_step_not_model_hint():
    engine, db, now = _case()
    try:
        signal = _signal(db, now, "ADD", qty=9999, target=0.12)
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "APPROVED"
        assert db.get(ActionableSignal, signal.signal_id).approved_qty == 100
    finally:
        db.close()
        engine.dispose()


def test_unresolved_agent_rating_is_not_an_approved_hold():
    assert _journal_action("alert", {"rating_raw": "review"}) == "REVIEW"
    assert _journal_action("watch") == "REVIEW"
    engine, db, now = _case()
    try:
        signal = _signal(db, now, "REVIEW")
        evaluate_signal(db, signal.signal_id, now=now)
        assert signal.status == "REVIEW_REQUIRED"
        assert db.get(ActionableSignal, signal.signal_id) is None
        decision = db.query(SignalPolicyDecision).filter_by(
            signal_id=signal.signal_id, rule_id="ACTION_RESOLUTION").one()
        assert decision.reason_codes == ["ACTION_DIRECTION_UNRESOLVED"]
    finally:
        db.close()
        engine.dispose()
