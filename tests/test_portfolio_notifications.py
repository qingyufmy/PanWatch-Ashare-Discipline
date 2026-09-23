import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.notifications import send_portfolio_notice
from src.platform.persistence.database import Base
from src.platform.persistence.models import NotifyChannel, NotifyThrottle


def test_batch_notification_attempt_is_deduped(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(NotifyChannel(name="test", type="lark", config={"webhook_token": "fixture"},
                             enabled=True, is_default=True))
        db.commit()
    calls = []

    class FakeNotifier:
        def add_channel(self, channel_type, config):
            assert channel_type == "lark"

        async def notify_with_result(self, title, content):
            calls.append(title)
            return {"success": True}

    monkeypatch.setattr("src.modules.portfolio.notifications.NotifierManager", FakeNotifier)

    async def run():
        first = await send_portfolio_notice(key="plan:2026-09-23", title="plan", content="11", db_factory=factory)
        second = await send_portfolio_notice(key="plan:2026-09-23", title="plan", content="11", db_factory=factory)
        return first, second

    first, second = asyncio.run(run())
    assert first["status"] == "SENT"
    assert second["status"] == "SKIPPED"
    assert calls == ["plan"]
    with factory() as db:
        assert db.query(NotifyThrottle).count() == 1
    engine.dispose()
