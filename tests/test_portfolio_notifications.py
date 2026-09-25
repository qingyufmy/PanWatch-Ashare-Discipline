import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.notifications import (
    send_portfolio_notice, dispatch_portfolio_notice, enqueue_portfolio_notice,
    recover_notification_outbox,
)
from src.platform.persistence.database import Base
from src.platform.persistence.models import NotifyChannel, PortfolioNotification


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
        row = db.query(PortfolioNotification).one()
        assert row.delivery_status == "SENT_ACCEPTED"
        assert row.ack_status == "NOT_OBSERVED"
        assert row.provider_accepted_at is not None
    engine.dispose()


def test_timeout_is_unknown_and_restart_does_not_resend(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(NotifyChannel(name="test", type="lark", config={"webhook_token": "fixture"},
                             enabled=True, is_default=True))
        db.commit()
    calls = []

    class TimeoutNotifier:
        def add_channel(self, *_args):
            pass

        async def notify_with_result(self, *_args):
            calls.append(1)
            await asyncio.sleep(0.05)
            return {"success": True}

    monkeypatch.setattr("src.modules.portfolio.notifications.NotifierManager", TimeoutNotifier)
    first = asyncio.run(send_portfolio_notice(key="risk:fixture", title="风险", content="fixture",
                                               db_factory=factory, timeout_seconds=0.001))
    assert first["status"] == "UNKNOWN"
    second = asyncio.run(dispatch_portfolio_notice(first["notification_id"], db_factory=factory))
    assert second["status"] == "SKIPPED" and len(calls) == 1
    with factory() as db:
        assert db.query(PortfolioNotification).one().delivery_status == "DELIVERY_UNKNOWN"
    engine.dispose()


def test_failed_send_requires_explicit_retry(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(NotifyChannel(name="test", type="lark", config={"webhook_token": "fixture"},
                             enabled=True, is_default=True))
        db.commit()
    calls = []

    class FakeNotifier:
        def add_channel(self, *_args):
            pass

        async def notify_with_result(self, *_args):
            calls.append(1)
            return {"success": len(calls) > 1}

    monkeypatch.setattr("src.modules.portfolio.notifications.NotifierManager", FakeNotifier)
    first = asyncio.run(send_portfolio_notice(key="risk:retry", title="风险", content="fixture", db_factory=factory))
    assert first["status"] == "FAILED"
    assert asyncio.run(dispatch_portfolio_notice(first["notification_id"], db_factory=factory))["status"] == "SKIPPED"
    assert asyncio.run(dispatch_portfolio_notice(first["notification_id"], db_factory=factory,
                                                  retry=True))["status"] == "SENT"
    with factory() as db:
        row = db.query(PortfolioNotification).one()
        assert row.retry_count == 1 and row.delivery_status == "SENT_ACCEPTED"
    engine.dispose()


def test_restart_and_expiration_are_audited_without_send(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        interrupted, _ = enqueue_portfolio_notice(db, key="restart", title="风险", content="fixture")
        interrupted.delivery_status = "SENDING"
        expired, _ = enqueue_portfolio_notice(db, key="expired", title="风险", content="fixture",
                                              expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1))
        db.commit()
        interrupted_id, expired_id = interrupted.id, expired.id
    recovered = recover_notification_outbox(factory)
    assert recovered == {"unknown": 1, "expired": 1, "cancelled": 0}
    result = asyncio.run(dispatch_portfolio_notice(interrupted_id, db_factory=factory))
    assert result["status"] == "SKIPPED"
    result = asyncio.run(dispatch_portfolio_notice(expired_id, db_factory=factory))
    assert result == {"status": "SKIPPED", "reason": "EXPIRED"}
    with factory() as db:
        assert db.get(PortfolioNotification, interrupted_id).delivery_status == "DELIVERY_UNKNOWN"
        assert db.get(PortfolioNotification, expired_id).delivery_status == "EXPIRED"
    engine.dispose()
