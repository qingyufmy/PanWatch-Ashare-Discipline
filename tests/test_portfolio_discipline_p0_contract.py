"""P2 contract for distinct REDUCE and EXIT behavior in the paper engine."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.platform.persistence.models import (
    PaperTradingAccount, PaperTradingPosition, PaperTradingTrade, StrategySignalRun,
)
from src.modules.paper_trading.paper_trading_engine import (
    PaperTradingEngine, _resolve_reduce_quantity,
)


def _fixture(action: str, payload: dict | None = None):
    engine = create_engine("sqlite:///:memory:")
    for model in (StrategySignalRun, PaperTradingAccount, PaperTradingPosition, PaperTradingTrade):
        model.__table__.create(engine)
    db = Session(engine)
    account = PaperTradingAccount(
        id=1, initial_capital=100000, current_capital=90000,
        peak_capital=100000, total_pnl=0, total_trades=0,
        winning_trades=0, max_drawdown_pct=0,
    )
    signal = StrategySignalRun(
        snapshot_date="2026-09-22", stock_symbol="600519", stock_market="CN",
        strategy_code="p2_fixture", action=action, payload=payload or {},
        status="active", score=1, rank_score=1,
    )
    db.add_all([account, signal])
    db.flush()
    position = PaperTradingPosition(
        stock_symbol="600519", stock_market="CN", quantity=1000,
        entry_price=10, signal_run_id=signal.id,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(position)
    db.commit()
    paper = PaperTradingEngine()
    paper._fetch_quotes_map = lambda _: {("CN", "600519"): {"current_price": 10.5}}
    return engine, db, account, signal, position, paper


def test_sell_closes_entire_position():
    engine, db, account, signal, position, paper = _fixture("sell")
    try:
        closed, events = paper._check_exits(db, account)
        assert closed == 1
        assert position.status == "closed"
        assert position.quantity == 1000
        assert events[0][1].quantity == 1000
        assert events[0][1].exit_reason == "signal_reversal"
    finally:
        db.close()
        engine.dispose()


def test_reduce_sells_only_requested_quantity_and_is_idempotent():
    engine, db, account, signal, position, paper = _fixture("reduce", {"reduce_qty": 300})
    try:
        closed, events = paper._check_exits(db, account)
        assert closed == 0
        assert position.status == "open"
        assert position.quantity == 700
        assert events[0][1].quantity == 300
        assert events[0][1].exit_reason == "signal_reduce"
        assert events[0][1].meta["trigger_signal_run_id"] == signal.id
        capital_after = account.current_capital
        closed, events = paper._check_exits(db, account)
        assert closed == 0 and events == []
        assert position.quantity == 700
        assert account.current_capital == capital_after
    finally:
        db.close()
        engine.dispose()


def test_reduce_without_target_does_not_close():
    engine, db, account, signal, position, paper = _fixture("reduce")
    try:
        closed, events = paper._check_exits(db, account)
        assert closed == 0 and events == []
        assert position.status == "open" and position.quantity == 1000
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("payload,expected", [
    ({"target_qty": 700}, 300),
    ({"target_weight": 0.2}, 800),
    ({"reduce_qty": 1000}, None),
    ({"target_qty": 0}, None),
    ({"target_weight": 0}, None),
])
def test_reduce_target_resolution(payload, expected):
    signal = StrategySignalRun(payload=payload)
    assert _resolve_reduce_quantity(signal, 1000, 10, 10000) == expected
