"""Operational issue ledger API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.modules.portfolio.issue_ledger import issue_summary, transition_issue
from src.platform.persistence.database import get_db
from src.platform.persistence.models import SystemIssue

router = APIRouter()


class Transition(BaseModel):
    status: str
    root_cause: str | None = None
    fix_commit: str | None = None
    regression_test: str | None = None


def _row(issue: SystemIssue) -> dict:
    return {"issue_id": issue.issue_id, "error_signature": issue.error_signature,
            "category": issue.category, "severity": issue.severity,
            "status": issue.status, "title": issue.title,
            "first_seen": issue.first_seen, "last_seen": issue.last_seen,
            "occurrence_count": issue.occurrence_count,
            "counts_by_day": issue.counts_by_day, "contexts": issue.contexts,
            "root_cause": issue.root_cause, "fix_commit": issue.fix_commit,
            "regression_test": issue.regression_test, "resolved_at": issue.resolved_at}


@router.get("")
def list_issues(status: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(SystemIssue)
    if status:
        query = query.filter_by(status=status)
    return [_row(r) for r in query.order_by(SystemIssue.last_seen.desc()).limit(min(max(limit, 1), 200)).all()]


@router.get("/summary/daily")
def daily_summary(db: Session = Depends(get_db)):
    return issue_summary(db, days=1)


@router.get("/summary/weekly")
def weekly_summary(db: Session = Depends(get_db)):
    return issue_summary(db, days=7)


@router.get("/{issue_id}")
def get_issue(issue_id: str, db: Session = Depends(get_db)):
    row = db.get(SystemIssue, issue_id)
    if row is None:
        raise HTTPException(404, "issue_missing")
    return _row(row)


@router.post("/{issue_id}/transition")
def transition(issue_id: str, body: Transition, db: Session = Depends(get_db)):
    try:
        return _row(transition_issue(db, issue_id, body.status,
                                     root_cause=body.root_cause,
                                     fix_commit=body.fix_commit,
                                     regression_test=body.regression_test))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
