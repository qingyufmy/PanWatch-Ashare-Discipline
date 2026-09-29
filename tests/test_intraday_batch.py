"""Batch monitoring must cover every holding with one model request."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.modules.automation.base import AgentContext, AnalysisResult, PortfolioInfo
from src.modules.automation.intraday_monitor import IntradayMonitorAgent
from src.modules.automation.suggestion_pool import SuggestionWriteResult
from src.platform.marketdata.models import MarketCode
from src.platform.runtime.config import StockConfig


def _context(response, expected=None):
    stocks = [
        StockConfig(symbol="sh600001", name="甲", market=MarketCode.CN),
        StockConfig(symbol="sz000002", name="乙", market=MarketCode.CN),
    ]

    class Client:
        calls = 0

        async def chat(self, system_prompt, user_content):
            self.calls += 1
            assert {x["symbol"] for x in json.loads(user_content)["stocks"]} == (
                expected if expected is not None else {"sh600001", "sz000002"}
            )
            return response

    class Notifier:
        calls = 0

        async def notify_with_result(self, title, content):
            self.calls += 1
            assert "sh600001" in content
            return {"success": True}

    client, notifier = Client(), Notifier()
    context = AgentContext(
        ai_client=client,
        notifier=notifier,
        config=SimpleNamespace(watchlist=stocks),
        portfolio=PortfolioInfo(),
    )
    return context, client, notifier


def _patch_market(monkeypatch):
    from src.modules.automation import intraday_monitor
    from src.modules.strategy import intraday_event_gate

    monkeypatch.setattr(intraday_monitor, "is_market_trading", lambda market: True)
    monkeypatch.setattr(
        intraday_event_gate,
        "check_and_update",
        lambda **kwargs: SimpleNamespace(reasons=["price_threshold"], should_analyze=True),
    )

    class Builder:
        async def build_for_symbols(self, *, symbols, **kwargs):
            return {
                symbol: SimpleNamespace(
                    quote=SimpleNamespace(
                        symbol=symbol,
                        name=name,
                        current_price=10.0,
                        change_pct=-3.5,
                        volume=1000,
                        turnover=10000,
                    ),
                    technical={"volume_ratio": 2.1},
                    news=None,
                    missing=[],
                )
                for symbol, _, name in symbols
            }

    monkeypatch.setattr(intraday_monitor, "SignalPackBuilder", Builder)


def test_batch_calls_model_once_and_dispatches_only_outbox(monkeypatch):
    from src.modules.automation import intraday_monitor

    _patch_market(monkeypatch)
    saved, throttled = [], []
    def save(**kwargs):
        saved.append(kwargs)
        return SuggestionWriteResult(True, signal_id="fixture", decision_status="APPROVED",
                                     notification_id="notice-1" if kwargs["stock_symbol"] == "sh600001" else None)
    monkeypatch.setattr(intraday_monitor, "save_suggestion_result", save)
    deliveries = []
    async def dispatch(notification_id, **kwargs):
        deliveries.append(notification_id)
        return {"status": "SENT"}
    monkeypatch.setattr(intraday_monitor, "dispatch_portfolio_notice", dispatch)
    response = json.dumps({"items": [
        {"symbol": "sh600001", "action": "reduce", "action_label": "减仓", "signal": "跌幅扩大", "reason": "量价走弱", "triggers": [], "invalidations": [], "risks": []},
        {"symbol": "sz000002", "action": "hold", "action_label": "持有", "signal": "", "reason": "", "triggers": [], "invalidations": [], "risks": []},
    ]}, ensure_ascii=False)
    context, client, notifier = _context(response)
    agent = IntradayMonitorAgent()
    monkeypatch.setattr(agent, "_check_throttle", lambda symbol: True)
    monkeypatch.setattr(agent, "_update_throttle", lambda symbol: throttled.append(symbol))

    result = asyncio.run(agent.run(context))

    assert client.calls == 1
    assert notifier.calls == 0
    assert deliveries == ["notice-1"]
    assert {x["stock_symbol"] for x in saved} == {"sh600001", "sz000002"}
    assert throttled == []
    assert result.raw_data["batch_size"] == 2
    assert result.raw_data["notified"] is True


def test_legacy_base_publisher_cannot_send_intraday_model_body():
    result = AnalysisResult(agent_name="intraday_monitor", title="模型建议",
                            content="unverified model action",
                            raw_data={"stock": {"symbol": "sh600001"}, "should_alert": True})
    assert asyncio.run(IntradayMonitorAgent().should_notify(result)) is False


def test_batch_rejects_missing_stock_before_side_effects(monkeypatch):
    from src.modules.automation import intraday_monitor

    _patch_market(monkeypatch)
    saved = []
    monkeypatch.setattr(intraday_monitor, "save_suggestion_result", lambda **kwargs: saved.append(kwargs))
    response = json.dumps({"items": [
        {"symbol": "sh600001", "action": "hold", "action_label": "持有"}
    ]})
    context, client, notifier = _context(response)

    with pytest.raises(ValueError, match="未覆盖全部持仓"):
        asyncio.run(IntradayMonitorAgent().run(context))

    assert client.calls == 1
    assert notifier.calls == 0
    assert saved == []


def test_twenty_quiet_positions_do_not_call_model_or_notify(monkeypatch):
    from src.modules.automation import intraday_monitor
    from src.modules.strategy import intraday_event_gate

    _patch_market(monkeypatch)
    monkeypatch.setattr(intraday_event_gate, "check_and_update",
                        lambda **kwargs: SimpleNamespace(reasons=[], should_analyze=False))
    context, client, notifier = _context("never")
    context.config.watchlist = [StockConfig(symbol=f"sh{600000+i}", name=str(i), market=MarketCode.CN)
                                for i in range(20)]
    monkeypatch.setattr(intraday_monitor, "save_suggestion_result",
                        lambda **kwargs: pytest.fail("quiet portfolio must not persist a proposal"))
    result = asyncio.run(IntradayMonitorAgent().run(context))
    assert result.raw_data["batch_size"] == 20
    assert result.raw_data["model_coverage"] == 0
    assert client.calls == notifier.calls == 0


def test_only_changed_position_enters_batch_model(monkeypatch):
    from src.modules.automation import intraday_monitor
    from src.modules.strategy import intraday_event_gate

    _patch_market(monkeypatch)
    monkeypatch.setattr(intraday_event_gate, "check_and_update",
                        lambda **kwargs: SimpleNamespace(reasons=["price"] if kwargs["symbol"] == "sh600001" else [],
                                                          should_analyze=kwargs["symbol"] == "sh600001"))
    context, client, notifier = _context(json.dumps({"items": [{
        "symbol": "sh600001", "action": "hold", "action_label": "持有", "signal": "", "reason": ""
    }]}), expected={"sh600001"})
    monkeypatch.setattr(intraday_monitor, "save_suggestion_result",
                        lambda **kwargs: SuggestionWriteResult(True, signal_id="fixture", decision_status="REVIEW_REQUIRED"))
    result = asyncio.run(IntradayMonitorAgent().run(context))
    assert client.calls == 1 and notifier.calls == 0
    assert result.raw_data["batch_size"] == 2 and result.raw_data["model_coverage"] == 1


def test_failed_persistence_and_policy_review_never_publish(monkeypatch):
    from src.modules.automation import intraday_monitor

    _patch_market(monkeypatch)
    response = json.dumps({"items": [
        {"symbol": "sh600001", "action": "sell", "action_label": "清仓", "signal": "risk"},
        {"symbol": "sz000002", "action": "reduce", "action_label": "减仓", "signal": "risk"},
    ]})
    context, _, notifier = _context(response)
    monkeypatch.setattr(intraday_monitor, "save_suggestion_result",
                        lambda **kwargs: SuggestionWriteResult(False, reason="DB_ERROR") if kwargs["stock_symbol"] == "sh600001"
                        else SuggestionWriteResult(True, signal_id="review", decision_status="REVIEW_REQUIRED"))
    result = asyncio.run(IntradayMonitorAgent().run(context))
    assert notifier.calls == 0 and result.raw_data["notified"] is False
