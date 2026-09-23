"""One Lark batch per plan and throttled hard-risk alerts; no trading action."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.modules.portfolio.issue_ledger import record_issue
from src.platform.notifications.notifier import NotifierManager
from src.platform.persistence.models import NotifyChannel, NotifyThrottle


async def send_portfolio_notice(*, key: str, title: str, content: str,
                                db_factory, timeout_seconds: int = 10) -> dict:
    """Persist attempt before sending so timeout/uncertain results are not resent blindly."""
    with db_factory() as db:
        channel = (db.query(NotifyChannel).filter_by(type="lark", enabled=True, is_default=True)
                   .order_by(NotifyChannel.id).first())
        if channel is None:
            record_issue(db, category="NOTIFY_FAILED", code="NO_DEFAULT_LARK",
                         source="portfolio", title="Portfolio Lark channel unavailable")
            return {"status": "FAILED", "reason": "NO_DEFAULT_LARK"}
        prior = db.query(NotifyThrottle).filter_by(agent_name="portfolio_workflow", stock_symbol=key).first()
        if prior is not None:
            return {"status": "SKIPPED", "reason": "ALREADY_ATTEMPTED"}
        manager = NotifierManager()
        manager.add_channel(channel.type, channel.config or {})
        db.add(NotifyThrottle(agent_name="portfolio_workflow", stock_symbol=key,
                              last_notify_at=datetime.now(timezone.utc).replace(tzinfo=None), notify_count=1))
        db.commit()
    try:
        result = await asyncio.wait_for(manager.notify_with_result(title, content), timeout=timeout_seconds)
        if result.get("success"):
            return {"status": "SENT"}
        reason = "CHANNEL_SEND_FAILED"
    except asyncio.TimeoutError:
        reason = "SEND_TIMEOUT_UNKNOWN"
    except Exception as exc:
        reason = type(exc).__name__
    with db_factory() as db:
        record_issue(db, category="NOTIFY_FAILED", code=reason, source="portfolio",
                     title="Portfolio Lark notification failed or uncertain",
                     context={"key": key})
    return {"status": "FAILED", "reason": reason}
