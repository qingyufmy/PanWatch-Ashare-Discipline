"""Durable portfolio Lark outbox with explicit acceptance and unknown states."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.issue_ledger import record_issue
from src.platform.notifications.notifier import NotifierManager
from src.platform.persistence.models import NotifyChannel, PortfolioDecision, PortfolioNotification

SH = ZoneInfo("Asia/Shanghai")
TEMPLATE_VERSION = "portfolio-text-v2-candidate"
TERMINAL = {"SENT_ACCEPTED", "DELIVERY_UNKNOWN", "CANCELLED", "EXPIRED", "SUPPRESSED"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def recover_notification_outbox(db_factory) -> dict:
    """On restart, preserve uncertainty; never auto-retry a webhook call."""
    counts = {"unknown": 0, "expired": 0, "cancelled": 0}
    with db_factory() as db:
        for row in db.query(PortfolioNotification).filter(PortfolioNotification.delivery_status.in_(
            ("SENDING", "PENDING", "FAILED_RETRYABLE"))).all():
            if row.delivery_status == "SENDING":
                row.delivery_status = "DELIVERY_UNKNOWN"
                row.suppression_reason = "PROCESS_RESTART_DURING_SEND"
                counts["unknown"] += 1
            elif row.expires_at and row.expires_at <= _now():
                row.delivery_status = "EXPIRED"
                counts["expired"] += 1
            elif row.decision_id:
                decision = db.get(PortfolioDecision, row.decision_id)
                if decision is None or not decision.current:
                    row.delivery_status = "CANCELLED"
                    row.suppression_reason = "DECISION_SUPERSEDED"
                    counts["cancelled"] += 1
        db.commit()
        if counts["unknown"]:
            record_issue(db, category="NOTIFY_FAILED", code="PROCESS_RESTART_DURING_SEND",
                         source="portfolio", title="Portfolio delivery status unknown after restart",
                         context={"count": counts["unknown"]})
    return counts


def enqueue_portfolio_notice(db, *, key: str, title: str, content: str,
                             trade_date: str | None = None, symbol: str | None = None,
                             decision: PortfolioDecision | None = None,
                             signal_ids: list[str] | None = None,
                             priority: str = "NORMAL", reason: str = "PLAN",
                             expires_at: datetime | None = None) -> tuple[PortfolioNotification, bool]:
    """Call in the signal/decision transaction, then commit before dispatch."""
    existing = db.query(PortfolioNotification).filter_by(semantic_key=key).first()
    if existing:
        return existing, False
    notification = PortfolioNotification(
        id=uuid.uuid4().hex, semantic_key=key,
        trade_date=trade_date or datetime.now(SH).date().isoformat(), symbol=symbol,
        decision_id=decision.id if decision else None, signal_ids=signal_ids or [],
        channel_id=None, template_version=TEMPLATE_VERSION,
        title=title, body=content, rendered_content_hash=logical_hash({"title": title, "body": content}),
        decision_revision=decision.revision if decision else None,
        priority=priority, reason=reason, queued_at=_now(),
        delivery_status="PENDING", ack_status="NOT_OBSERVED", retry_count=0,
        expires_at=expires_at,
    )
    db.add(notification)
    db.flush()
    return notification, True


async def dispatch_portfolio_notice(notification_id: str, *, db_factory,
                                    timeout_seconds: int = 10, retry: bool = False) -> dict:
    """Never blindly resend an accepted, uncertain, superseded or expired message."""
    with db_factory() as db:
        row = db.get(PortfolioNotification, notification_id)
        if row is None:
            return {"status": "FAILED", "reason": "OUTBOX_MISSING"}
        if row.delivery_status == "SENDING":
            row.delivery_status = "DELIVERY_UNKNOWN"
            db.commit()
            return {"status": "UNKNOWN", "reason": "PRIOR_SEND_INTERRUPTED"}
        if row.delivery_status in TERMINAL or (row.delivery_status == "FAILED_RETRYABLE" and not retry):
            return {"status": "SKIPPED", "reason": row.delivery_status}
        if row.expires_at and row.expires_at <= _now():
            row.delivery_status = "EXPIRED"
            db.commit()
            return {"status": "SKIPPED", "reason": "EXPIRED"}
        if row.decision_id:
            decision = db.get(PortfolioDecision, row.decision_id)
            if decision is None or not decision.current or decision.expires_at <= _now():
                row.delivery_status = "CANCELLED"
                row.suppression_reason = "DECISION_SUPERSEDED_OR_EXPIRED"
                db.commit()
                return {"status": "SKIPPED", "reason": row.suppression_reason}
        channel = (db.query(NotifyChannel).filter_by(type="lark", enabled=True, is_default=True)
                   .order_by(NotifyChannel.id).first())
        if channel is None:
            row.delivery_status = "FAILED_RETRYABLE"
            row.suppression_reason = "NO_DEFAULT_LARK"
            record_issue(db, category="NOTIFY_FAILED", code="NO_DEFAULT_LARK",
                         source="portfolio", title="Portfolio Lark channel unavailable")
            db.commit()
            return {"status": "FAILED", "reason": "NO_DEFAULT_LARK"}
        manager = NotifierManager()
        manager.add_channel(channel.type, channel.config or {})
        row.channel_id = channel.id
        row.delivery_status = "SENDING"
        row.attempt_started_at = _now()
        if retry:
            row.retry_count += 1
        title, content = row.title, row.body
        db.commit()
    try:
        result = await asyncio.wait_for(manager.notify_with_result(title, content), timeout=timeout_seconds)
        if result.get("success"):
            status, reason = "SENT_ACCEPTED", None
        elif result.get("skipped"):
            status, reason = "FAILED_RETRYABLE", str(result["skipped"])[:80]
        else:
            status, reason = "FAILED_RETRYABLE", "CHANNEL_SEND_FAILED"
    except Exception as exc:
        # A transport exception may happen after the provider accepted the body.
        status, reason = "DELIVERY_UNKNOWN", type(exc).__name__
    with db_factory() as db:
        row = db.get(PortfolioNotification, notification_id)
        row.delivery_status = status
        if status == "SENT_ACCEPTED":
            row.provider_accepted_at = _now()
            if isinstance(result, dict):
                row.provider_message_id = result.get("message_id")
        else:
            row.suppression_reason = reason
            record_issue(db, category="NOTIFY_FAILED", code=reason or status, source="portfolio",
                         title="Portfolio Lark notification failed or uncertain",
                         context={"notification_id": notification_id, "status": status})
        db.commit()
    return {"status": "SENT" if status == "SENT_ACCEPTED" else "UNKNOWN" if status == "DELIVERY_UNKNOWN" else "FAILED",
            "delivery_status": status, "reason": reason, "notification_id": notification_id}


async def send_portfolio_notice(*, key: str, title: str, content: str,
                                db_factory, timeout_seconds: int = 10,
                                trade_date: str | None = None, symbol: str | None = None,
                                priority: str = "NORMAL", reason: str = "PLAN",
                                expires_at: datetime | None = None) -> dict:
    with db_factory() as db:
        row, created = enqueue_portfolio_notice(
            db, key=key, title=title, content=content, trade_date=trade_date,
            symbol=symbol, priority=priority, reason=reason, expires_at=expires_at,
        )
        notification_id = row.id
        db.commit()
    if not created:
        return {"status": "SKIPPED", "reason": "ALREADY_QUEUED", "notification_id": notification_id}
    return await dispatch_portfolio_notice(notification_id, db_factory=db_factory,
                                           timeout_seconds=timeout_seconds)
