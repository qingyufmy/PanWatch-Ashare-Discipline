from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.review_gate import acceptance_gate, acceptance_metrics
from src.modules.portfolio.upstream_watch import collect
from src.platform.persistence.database import Base
from src.platform.persistence.models import ActionableSignal, ModelRun, PortfolioWorkflowRun
from src.modules.portfolio.signal_journal import record_signal, transition_signal


def test_empty_runtime_does_not_promote():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        metrics = acceptance_metrics(db, since=datetime(2026, 9, 22))
        gate = acceptance_gate(metrics)
        assert gate["status"] == "FAIL_CLOSED"
        assert all(not passed for passed in gate["checks"].values())
    engine.dispose()


def test_fast_latency_uses_success_status_from_model_router():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for run_id, role, status, latency_ms in (
            ("fast-ok", "FAST", "OK", 1800),
            ("fast-failed", "FAST", "FAILED", 90000),
            ("deep-ok", "DEEP", "OK", 70000),
        ):
            db.add(ModelRun(
                run_id=run_id, trace_id=run_id, role=role, profile_role=role,
                requested_model="test-model", input_hash=run_id,
                latency_ms=latency_ms, status=status,
                started_at=datetime(2026, 9, 23, 0),
                finished_at=datetime(2026, 9, 23, 0, 1),
            ))
        db.commit()
        metrics = acceptance_metrics(db, since=datetime(2026, 9, 22))
        assert metrics["fast_p95_seconds"] == 1.8
        assert acceptance_gate(metrics)["checks"]["FAST_P95<20s"] is True
    engine.dispose()


def test_expired_after_valid_approval_is_not_counted_as_stale_at_approval():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        generated = datetime(2026, 9, 23, 2, tzinfo=timezone.utc)
        signal, _ = record_signal(db, market="CN", symbol="sh600001", action="REDUCE",
                                  source="fixture", evidence={"fixture": True},
                                  ttl_seconds=3600, generated_at=generated)
        approved_at = generated + timedelta(minutes=1)
        db.add(ActionableSignal(signal_id=signal.signal_id, approved_qty=100,
                                policy_version="fixture", input_hash="fixture",
                                approved_at=approved_at.replace(tzinfo=None)))
        db.flush()
        transition_signal(db, signal.signal_id, "APPROVED", reason="fixture", now=approved_at)
        transition_signal(db, signal.signal_id, "EXPIRED", reason="fixture",
                          now=generated + timedelta(hours=2))
        metrics = acceptance_metrics(db, since=datetime(2026, 9, 22))
        assert metrics["critical_stale_actionable"] == 0
    engine.dispose()


def test_upstream_changes_reported_without_activation():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    previous = {"repositories": [{"repo": "TNT-Likely/PanWatch", "sha": "old"}]}
    with Session(engine) as db:
        result = collect(db, previous=previous,
                         fetch_head=lambda repo: {"repo": repo, "sha": "new", "url": "https://example.test"})
    assert result["changed_repositories"] == ["TNT-Likely/PanWatch"]
    assert result["action"] == "REVIEW_ONLY_NO_UPGRADE"
    engine.dispose()


def test_missed_hard_risk_slot_counts_as_critical_scheduler_miss():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(PortfolioWorkflowRun(
            run_id="missed-hard-risk", trade_date="2026-09-23", step="HARD_RISK",
            slot="14:55", status="MISSED", payload={"reason": "SERVICE_DOWN"},
            started_at=datetime(2026, 9, 23, 6, 55),
        ))
        db.commit()
        metrics = acceptance_metrics(db, since=datetime(2026, 9, 23))
        assert metrics["critical_scheduler_missed"] == 1
    engine.dispose()
