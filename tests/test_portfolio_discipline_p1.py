"""P1 truth, plan versioning and migration contracts."""

from datetime import datetime, timedelta, timezone
from itertools import product

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import (
    ALLOWED_TRANSITIONS, POSITION_STATES, aggregate_positions,
    capture_truth, create_plan_version, current_freshness, current_plan, initialize_plans_from_truth,
)
from src.platform.persistence.database import Base
from src.platform.persistence.migrations import _m127_portfolio_truth_and_position_plans
from src.platform.persistence.models import (
    Account, Position, PositionPlan, PositionStateEvent, Stock,
)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_multiactount_weighted_cost_t_plus_one_and_stable_hash():
    engine, db = _db()
    try:
        a = Account(name="账户甲", available_funds=1000, enabled=True)
        b = Account(name="账户乙", available_funds=2000, enabled=True)
        stock = Stock(symbol="sh600001", name="测试", market="CN")
        db.add_all([a, b, stock])
        db.flush()
        db.add_all([
            Position(account_id=a.id, stock_id=stock.id, quantity=100, cost_price=10),
            Position(account_id=b.id, stock_id=stock.id, quantity=300, cost_price=20),
        ])
        db.commit()
        first = capture_truth(db, phase="PREMARKET")
        second = capture_truth(db, phase="EOD")
        assert first.id != second.id
        assert first.logical_hash == second.logical_hash
        assert first.truth_status == "REVIEW_ONLY"
        assert "SELLABLE_UNKNOWN" in first.anomaly_flags
        assert first.freshness == "UNKNOWN"
        assert len(first.positions) == 1
        row = first.positions[0]
        assert row.total_qty == 400
        assert row.avg_cost == 17.5
        assert row.sellable_qty is None and row.today_locked_qty is None
        assert len(row.account_details) == 2
        assert db.query(Position).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_explicit_sellable_aggregation_and_anomaly_detection():
    rows = [
        {"account_id": 1, "account_name": "甲", "market": "CN", "symbol": "sh600001", "name": "测试", "quantity": 100, "cost_price": 10, "sellable_qty": 80, "today_locked_qty": 20, "market_value": 1100},
        {"account_id": 2, "account_name": "乙", "market": "CN", "symbol": "sh600001", "name": "测试", "quantity": 200, "cost_price": 20, "sellable_qty": 150, "today_locked_qty": 50, "market_value": 2200},
    ]
    positions, flags = aggregate_positions(rows)
    assert positions[0]["total_qty"] == 300
    assert positions[0]["sellable_qty"] == 230
    assert positions[0]["today_locked_qty"] == 70
    assert positions[0]["market_value"] == "3300"
    assert flags == []
    rows[1]["today_locked_qty"] = 60
    _, flags = aggregate_positions(rows)
    assert "T_PLUS_ONE_QUANTITY_MISMATCH" in flags


def test_user_attested_sellable_is_append_only_and_expires():
    engine, db = _db()
    try:
        account = Account(name="账户", available_funds=1000, enabled=True)
        stock = Stock(symbol="sh600001", name="测试", market="CN")
        db.add_all([account, stock])
        db.flush()
        db.add(Position(account_id=account.id, stock_id=stock.id, quantity=100, cost_price=10))
        db.commit()
        before = capture_truth(db, phase="MANUAL")
        at = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        attested = capture_truth(db, phase="MANUAL", fetched_at=at, sellable_equals_quantity=True)
        assert attested.id != before.id
        assert before.positions[0].sellable_qty is None
        assert attested.source == "user_attested"
        assert attested.freshness == "FRESH"
        assert attested.truth_status == "REVIEW_ONLY"
        assert "USER_ATTESTED_UNRECONCILED" in attested.anomaly_flags
        assert attested.positions[0].sellable_qty == 100
        assert attested.positions[0].today_locked_qty == 0
        assert current_freshness(attested, now=at + timedelta(seconds=301)) == "STALE"
    finally:
        db.close()
        engine.dispose()


def test_freshness_and_beijing_trade_date():
    engine, db = _db()
    try:
        now = datetime(2026, 9, 22, 16, 30, tzinfo=timezone.utc)
        row = capture_truth(db, phase="MANUAL", source_asof=now - timedelta(minutes=6), fetched_at=now)
        assert row.trade_date == "2026-09-23"
        assert row.freshness == "STALE"
        assert "SOURCE_STALE" in row.anomaly_flags
    finally:
        db.close()
        engine.dispose()


def test_bootstrap_plans_are_review_only_and_idempotent():
    engine, db = _db()
    try:
        account = Account(name="账户", available_funds=0, enabled=True)
        stock = Stock(symbol="sh600001", name="测试", market="CN")
        db.add_all([account, stock])
        db.flush()
        db.add(Position(account_id=account.id, stock_id=stock.id, quantity=100, cost_price=10))
        db.commit()
        snapshot = capture_truth(db, phase="PREMARKET")
        first = initialize_plans_from_truth(db, snapshot.id)
        second = initialize_plans_from_truth(db, snapshot.id)
        assert len(first) == 1 and second == []
        assert first[0].position_state == "WATCH"
        assert first[0].thesis_state == "UNKNOWN"
        assert first[0].plan["review_status"] == "REVIEW_ONLY"
        assert first[0].plan["position"]["sellable_qty"] is None
    finally:
        db.close()
        engine.dispose()


def test_plan_versions_and_rejected_transition_audit():
    engine, db = _db()
    try:
        first, event = create_plan_version(
            db, market="CN", symbol="sh600001", position_state="CORE",
            thesis_state="UNKNOWN", plan={"risk": {"current_stop": None}},
            reason="人工持仓导入", expected_version=0,
        )
        assert first.version == 1 and event.decision == "APPROVED"
        second, event = create_plan_version(
            db, market="CN", symbol="sh600001", position_state="REDUCE_REQUIRED",
            thesis_state="WEAKENING", plan={"risk": {"current_stop": 9.5}},
            reason="人工复核", expected_version=1,
        )
        assert second.version == 2 and event.decision == "APPROVED"
        rejected, event = create_plan_version(
            db, market="CN", symbol="sh600001", position_state="ADD_ALLOWED",
            thesis_state="VALID", plan={}, reason="非法跃迁", expected_version=2,
        )
        assert rejected is None and event.decision == "REVIEW_REQUIRED"
        assert event.reason == "ILLEGAL_STATE_TRANSITION"
        assert current_plan(db, "CN", "sh600001").version == 2
        assert db.query(PositionStateEvent).count() == 3
        assert db.query(PositionPlan).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_unreviewed_bootstrap_core_can_be_corrected_to_watch_with_audit():
    engine, db = _db()
    try:
        seed = PositionPlan(
            market="CN", symbol="sh600001", version=1,
            position_state="CORE", thesis_state="UNKNOWN",
            plan={"review_status": "REVIEW_ONLY", "entry_thesis": []}, logical_hash="seed",
        )
        db.add(seed)
        db.commit()
        plan, event = create_plan_version(
            db, market="CN", symbol="sh600001", position_state="WATCH",
            thesis_state="UNKNOWN", plan=seed.plan,
            reason="BOOTSTRAP_CORRECTION", expected_version=1,
        )
        assert plan.version == 2
        assert event.decision == "APPROVED"
        assert event.from_state == "CORE" and event.to_state == "WATCH"
        rejected, event = create_plan_version(
            db, market="CN", symbol="sh600001", position_state="CORE",
            thesis_state="UNKNOWN", plan={}, reason="unreviewed", expected_version=2,
        )
        assert rejected is None and event.reason == "ILLEGAL_STATE_TRANSITION"
    finally:
        db.close()
        engine.dispose()


def test_transition_matrix_all_legal_and_illegal_pairs():
    engine, db = _db()
    try:
        for index, (source, target) in enumerate(product(sorted(POSITION_STATES), repeat=2)):
            symbol = f"test{index:03d}"
            db.add(PositionPlan(
                market="CN", symbol=symbol, version=1,
                position_state=source, thesis_state="UNKNOWN", plan={}, logical_hash="seed",
            ))
            db.commit()
            created, event = create_plan_version(
                db, market="CN", symbol=symbol, position_state=target,
                thesis_state="UNKNOWN", plan={}, reason="matrix", expected_version=1,
            )
            allowed = target in ALLOWED_TRANSITIONS[source]
            assert (created is not None) == allowed, (source, target)
            assert event.decision == ("APPROVED" if allowed else "REVIEW_REQUIRED")
    finally:
        db.close()
        engine.dispose()


def test_migration_adds_p1_tables_without_changing_legacy_positions():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, quantity INTEGER)"))
        conn.execute(text("INSERT INTO positions(id,quantity) VALUES (1,100)"))
        _m127_portfolio_truth_and_position_plans(conn)
        assert conn.execute(text("SELECT quantity FROM positions WHERE id=1")).scalar_one() == 100
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert {"portfolio_truth_snapshots", "portfolio_truth_positions", "position_plans", "position_state_events"} <= names
    engine.dispose()
