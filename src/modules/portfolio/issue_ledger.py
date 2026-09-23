"""P9 operational faults aggregated by stable error signature."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import logical_hash
from src.platform.persistence.models import SystemIssue

CATEGORIES = frozenset({
    "DATA_MISSING", "DATA_STALE", "SOURCE_DRIFT", "API_TIMEOUT", "API_RATE_LIMIT",
    "MODEL_TIMEOUT", "MODEL_SCHEMA_ERROR", "MODEL_CONFLICT", "PROMPT_REGRESSION",
    "POLICY_CONFLICT", "DUPLICATE_SIGNAL", "LATE_SIGNAL", "EXECUTION_MISMATCH",
    "SCHEDULER_MISSED", "JOB_STUCK", "JOB_FAILED", "NOTIFY_FAILED", "POSITION_RECONCILE_ERROR",
})
STATUSES = frozenset({"OPEN", "INVESTIGATING", "FIXED", "MONITORING", "REGRESSION"})
SEVERITIES = frozenset({"INFO", "WARNING", "ERROR", "CRITICAL"})


def record_issue(db: Session, *, category: str, code: str, source: str,
                 title: str, severity: str = "ERROR", context: dict | None = None,
                 now: datetime | None = None) -> SystemIssue:
    if category not in CATEGORIES or severity not in SEVERITIES or not code or not source or not title:
        raise ValueError("invalid_issue")
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    signature = logical_hash({"category": category, "code": code, "source": source})
    row = (db.query(SystemIssue).filter_by(error_signature=signature)
           .order_by(SystemIssue.first_seen.desc()).first())
    entry = {"at": timestamp.isoformat() + "Z", "code": code, "source": source,
             **(context or {})}
    day = timestamp.date().isoformat()
    if row is None:
        row = SystemIssue(
            issue_id=uuid.uuid4().hex, error_signature=signature,
            category=category, severity=severity, status="OPEN", title=title[:200],
            first_seen=timestamp, last_seen=timestamp, occurrence_count=1,
            counts_by_day={day: 1}, contexts=[entry],
        )
        db.add(row)
    else:
        row.last_seen = timestamp
        row.occurrence_count += 1
        counts = dict(row.counts_by_day or {})
        counts[day] = counts.get(day, 0) + 1
        row.counts_by_day = counts
        row.contexts = (list(row.contexts or []) + [entry])[-100:]
        if row.status in {"FIXED", "MONITORING"}:
            row.status = "REGRESSION"
            row.resolved_at = None
        if severity in {"CRITICAL", "ERROR"} and row.severity in {"INFO", "WARNING"}:
            row.severity = severity
    db.commit()
    db.refresh(row)
    return row


def transition_issue(db: Session, issue_id: str, status: str, *,
                     root_cause: str | None = None, fix_commit: str | None = None,
                     regression_test: str | None = None) -> SystemIssue:
    row = db.get(SystemIssue, issue_id)
    if row is None:
        raise ValueError("issue_missing")
    allowed = {"OPEN": {"INVESTIGATING"}, "INVESTIGATING": {"FIXED"},
               "FIXED": {"MONITORING"}, "MONITORING": set(),
               "REGRESSION": {"INVESTIGATING"}}
    if status not in allowed[row.status]:
        raise ValueError("issue_transition_invalid")
    if status == "FIXED" and not all((root_cause, fix_commit, regression_test)):
        raise ValueError("fix_evidence_required")
    row.status = status
    if root_cause is not None:
        row.root_cause = root_cause
    if fix_commit is not None:
        row.fix_commit = fix_commit
    if regression_test is not None:
        row.regression_test = regression_test
    row.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None) if status == "FIXED" else None
    db.commit()
    db.refresh(row)
    return row


def issue_summary(db: Session, *, days: int = 1, now: datetime | None = None) -> dict:
    if days not in {1, 7}:
        raise ValueError("unsupported_summary_window")
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    included = {(today - timedelta(days=offset)).isoformat() for offset in range(days)}
    rows = db.query(SystemIssue).all()
    counts = {r.issue_id: sum(n for d, n in (r.counts_by_day or {}).items() if d in included) for r in rows}
    rows = [r for r in rows if counts[r.issue_id] > 0]
    return {"days": days, "issue_count": len(rows),
            "occurrences": sum(counts[r.issue_id] for r in rows),
            "by_category": {c: sum(counts[r.issue_id] for r in rows if r.category == c)
                            for c in sorted({r.category for r in rows})},
            "unresolved": sum(r.status != "FIXED" for r in rows)}
