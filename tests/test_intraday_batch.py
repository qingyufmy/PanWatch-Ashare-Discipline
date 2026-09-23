"""Batch monitoring must cover every holding with one model request."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.modules.automation.base import AgentContext, PortfolioInfo
from src.modules.automation.intraday_monitor import IntradayMonitorAgent
from src.platform.marketdata.models import MarketCode
from src.platform.runtime.config import StockConfig


def _context(response):
    stocks = [
        StockConfig(symbol="sh600001", name="甲", market=MarketCode.CN),
        StockConfig(symbol="sz000002", name="乙", market=MarketCode.CN),
    ]

    class Client:
        calls = 0

        async def chat(self, system_prompt, user_content):
            self.calls += 1
            assert {x["symbol"] for x in json.loads(user_content)["stocks"]} == {
                "sh600001", "sz000002"
            }
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
        lambda **kwargs: SimpleNamespace(reasons=["price_threshold"]),
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


def test_batch_calls_model_once_and_sends_one_alert(monkeypatch):
    from src.modules.automation import intraday_monitor

    _patch_market(monkeypatch)
    saved, throttled = [], []
    monkeypatch.setattr(intraday_monitor, "save_suggestion", lambda **kwargs: saved.append(kwargs))
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
    assert notifier.calls == 1
    assert {x["stock_symbol"] for x in saved} == {"sh600001", "sz000002"}
    assert throttled == ["sh600001"]
    assert result.raw_data["batch_size"] == 2
    assert result.raw_data["notified"] is True


def test_batch_rejects_missing_stock_before_side_effects(monkeypatch):
    from src.modules.automation import intraday_monitor

    _patch_market(monkeypatch)
    saved = []
    monkeypatch.setattr(intraday_monitor, "save_suggestion", lambda **kwargs: saved.append(kwargs))
    response = json.dumps({"items": [
        {"symbol": "sh600001", "action": "hold", "action_label": "持有"}
    ]})
    context, client, notifier = _context(response)

    with pytest.raises(ValueError, match="未覆盖全部持仓"):
        asyncio.run(IntradayMonitorAgent().run(context))

    assert client.calls == 1
    assert notifier.calls == 0
    assert saved == []
