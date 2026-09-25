"""Scheduled CN summary agents must not create holiday signals or notifications."""

import asyncio
from types import SimpleNamespace

import pytest

from src.modules.automation import agent_scheduler


@pytest.mark.parametrize("name", ["daily_report", "premarket_outlook"])
@pytest.mark.parametrize("calendar_result,reason", [
    (False, "NON_TRADING_DAY"), (None, "CALENDAR_UNVERIFIED"),
])
def test_cn_summary_scheduler_skips_without_context_or_model(
    monkeypatch, name, calendar_result, reason,
):
    scheduler = agent_scheduler.AgentScheduler(timezone="Asia/Shanghai")
    scheduler.agents[name] = SimpleNamespace(name=name, display_name=name)
    scheduler.context_builder = lambda _name: pytest.fail("Holiday must skip before context/model")
    monkeypatch.setattr(agent_scheduler, "confirmed_cn_trading_day", lambda _day: calendar_result)
    recorded = []
    monkeypatch.setattr(agent_scheduler, "record_agent_run", lambda **kw: recorded.append(kw))

    asyncio.run(scheduler._run_agent(name))

    assert recorded == [{"agent_name": name, "status": "skipped",
                         "result": reason, "trigger_source": "schedule"}]
