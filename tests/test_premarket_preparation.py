import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.modules.portfolio.market_context import _quote, context_quality_matrix
from src.modules.portfolio.premarket_briefing import build_briefing
from src.modules.portfolio.notification_renderer import render_premarket_plan
from src.platform.scheduling import trading_calendar as tc

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(tc, "_CN_TRADING_DATES", frozenset({date(2026, 9, 24), date(2026, 9, 28),
        date(2026, 9, 30), date(2026, 10, 8)}))
    monkeypatch.setattr(tc, "_CN_RANGE", (date(2026, 9, 24), date(2026, 12, 31)))


def test_holiday_gap_previous_session_is_valid_for_plan_but_not_intraday(sessions):
    row = {"source_asof": "20260924161400", "current_price": 10}
    before = _quote(row, "fixture", collected_at=datetime(2026, 9, 28, 8, 50, tzinfo=SH), phase="PREMARKET")
    during = _quote(row, "fixture", collected_at=datetime(2026, 9, 28, 10, tzinfo=SH), phase="INTRADAY")
    assert before["quality"] == "PREVIOUS_CLOSE"
    assert during["quality"] == "STALE"
    long_gap = _quote({**row, "source_asof": "20260930150000"}, "fixture",
        collected_at=datetime(2026, 10, 8, 8, 50, tzinfo=SH), phase="PREMARKET")
    assert long_gap["quality"] == "PREVIOUS_CLOSE"
    assert _quote(row, "fixture", collected_at=datetime(2026, 10, 8, 8, 50, tzinfo=SH), phase="PREMARKET")["quality"] == "STALE"


def test_us_native_date_is_retained_without_inventing_timezone():
    row = {"current_price": 10, "source_time_raw": "2026-09-25 16:00:01"}
    quote = _quote(row, "fixture", collected_at=datetime(2026, 9, 28, 8, 50, tzinfo=SH), phase="PREMARKET", market="US")
    assert quote["quality"] == "DATED_REFERENCE"
    assert quote["source_session_date"] == "2026-09-25"
    assert quote["source_asof"] is None
    assert quote["timezone_status"] == "UNVERIFIED"


def test_planning_coverage_does_not_mislabel_previous_close_as_live():
    matrix = context_quality_matrix({"phase": "PREMARKET", "indices": [{"quality": "PREVIOUS_CLOSE"}],
        "holdings": [{"quality": "PREVIOUS_CLOSE", "change_pct": -2}]})
    assert matrix["indices"]["fresh"] == 0
    assert matrix["indices"]["usable_for_planning"] == 1
    assert matrix["holding_breadth"]["decliners"] == 1


def fixture_market():
    return {"trade_date": "2026-09-28", "indices": [
        {"name": str(i), "price": 3000, "change_pct": -1, "source_asof": "2026-09-24T16:00:00+08:00"}
        for i in range(4)], "holdings": [{"symbol": "600001", "price": 10, "change_pct": -5,
        "low_price": 9, "high_price": 11, "source_asof": "2026-09-24T15:00:00+08:00"}]}


def test_briefing_has_calculated_weights_and_source_backed_conditions_not_orders(sessions):
    briefing = build_briefing(market=fixture_market(), positions=[{"symbol": "600001", "name": "测试", "total_qty": 100}],
        limits=[{"symbol": "600001", "max_weight": .2, "plan_version": 1}], nav=10000, truth_source="user_attested")
    assert briefing["risk_tone"] == "DEFENSIVE"
    assert briefing["exposure"]["current_weight"] == .1
    assert briefing["exposure"]["suggested_min"] == .075
    row = briefing["positions"][0]
    assert "9.00" in row["reduce_condition"] and "11.00" in row["add_condition"]
    assert "qty_hint" not in row and "action" not in row
    title, body = render_premarket_plan([], [], [], briefing)
    assert "条件预案" in title and "10.0%" in body and "7.5%" in body
    assert "全部持有" not in body and "09:25" in body


def test_missing_nav_or_stale_holding_cannot_produce_target(sessions):
    market = fixture_market()
    market["holdings"][0]["source_asof"] = "2026-09-23T15:00:00+08:00"
    result = build_briefing(market=market, positions=[{"symbol": "600001", "total_qty": 100}],
        limits=[], nav=None, truth_source="user_attested")
    assert result["exposure"]["suggested_min"] is None
    assert result["positions"][0]["conditional_target_weight"] is None
    assert result["positions"][0]["reference_low"] is None


def test_cost_allocation_is_not_portfolio_weight():
    from types import SimpleNamespace
    from src.modules.research.context_builder import ContextBuilder
    portfolio = SimpleNamespace(accounts=[], total_cost=20000, total_available_funds=0,
        get_aggregated_position=lambda _: {"market": "CN", "total_cost": 10000,
                                         "total_quantity": 100, "positions": []})
    unknown = ContextBuilder._build_portfolio_constraints(portfolio, "600001")
    assert unknown["cost_allocation_ratio"] == .5
    assert unknown["single_position_ratio"] is None
    assert unknown["risk_budget_hint"] == "unknown"
    marked = ContextBuilder._build_portfolio_constraints(portfolio, "600001", mark_price=20, declared_nav=100000)
    assert marked["single_position_ratio"] == .02
    assert "用户声明" in marked["single_position_ratio_text"]


@pytest.mark.parametrize("closed", [False, None])
def test_cn_holiday_skips_all_automatic_scans_before_work(monkeypatch, closed):
    from src.modules.research import context_scheduler as context
    from src.modules.paper_trading import paper_trading_scheduler as paper
    from src.modules.market import price_alert_scheduler as price
    monkeypatch.setattr(tc, "confirmed_cn_trading_day", lambda _day: closed)
    def forbidden(*args, **kwargs):
        pytest.fail("Closed CN session must not execute business work")
    monkeypatch.setattr(context, "evaluate_pending_prediction_outcomes", forbidden)
    monkeypatch.setattr(context, "refresh_strategy_signals", forbidden)
    monkeypatch.setattr(paper.ENGINE, "scan_once", forbidden)
    monkeypatch.setattr(price.ENGINE, "scan_once", forbidden)
    async def run():
        c = context.ContextMaintenanceScheduler()
        p = paper.PaperTradingScheduler()
        await c._evaluate_job()
        await c._refresh_opportunities_job()
        await p._scan_job()
        await p._premarket_job()
        await p._summary_job()
        await p._portfolio_nav_job()
        await price.PriceAlertScheduler()._scan_job()
    asyncio.run(run())
