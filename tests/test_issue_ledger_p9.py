"""P9 issue aggregation and remediation evidence."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.portfolio.issue_ledger import issue_summary, record_issue, transition_issue
from src.platform.persistence.database import Base
from src.platform.persistence.models import SystemIssue


def test_same_signature_aggregates_and_fixed_issue_regresses():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)
        first = record_issue(db, category="API_TIMEOUT", code="ETIMEDOUT", source="sina",
                             title="minute fetch timeout", context={"symbol": "600001"}, now=now)
        second = record_issue(db, category="API_TIMEOUT", code="ETIMEDOUT", source="sina",
                              title="minute fetch timeout", context={"symbol": "000001"}, now=now)
        assert first.issue_id == second.issue_id
        assert second.occurrence_count == 2
        assert len(second.contexts) == 2
        assert issue_summary(db, days=1, now=now)["occurrences"] == 2
        assert issue_summary(db, days=1, now=now + timedelta(days=1))["occurrences"] == 0
        transition_issue(db, first.issue_id, "INVESTIGATING")
        with pytest.raises(ValueError, match="fix_evidence_required"):
            transition_issue(db, first.issue_id, "FIXED")
        transition_issue(db, first.issue_id, "FIXED", root_cause="timeout too short",
                         fix_commit="abc123", regression_test="tests/test_minute_features_p6.py")
        again = record_issue(db, category="API_TIMEOUT", code="ETIMEDOUT", source="sina",
                             title="minute fetch timeout", now=now + timedelta(days=1))
        assert again.status == "REGRESSION" and again.occurrence_count == 3
        assert db.query(SystemIssue).count() == 1
        assert issue_summary(db, days=7, now=now + timedelta(days=1))["occurrences"] == 3
