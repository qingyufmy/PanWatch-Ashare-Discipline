"""P10 evidence-based release gate and recurring review snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.platform.persistence.models import (
    ActionableSignal, ExecutionEvent, ModelRun, PortfolioWorkflowRun,
    PromptVersion, SignalEvent, SignalPolicyDecision, SystemIssue,
    SignalLifecycleEvent,
)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) * .95 + .999999)) - 1))]


def acceptance_metrics(db: Session, *, since: datetime) -> dict:
    """Null means unproven. No empty sample can pass an evidence gate."""
    rows = db.query(PortfolioWorkflowRun).filter(PortfolioWorkflowRun.started_at >= since).all()
    risk = [r for r in rows if r.step == "HARD_RISK" and r.status in {"SUCCEEDED", "REVIEW"}]
    colors = [p.get("color") for r in risk for p in (r.payload or {}).get("positions", [])]
    fixed = [r for r in rows if r.step not in {"HARD_RISK", "FEATURE_REFRESH", "ACTIVATED"}]
    signals = db.query(SignalEvent).filter(SignalEvent.generated_at >= since).all()
    actionable = db.query(ActionableSignal).filter(ActionableSignal.approved_at >= since).all()
    by_id = {s.signal_id: s for s in signals}
    policies = {p.signal_id for p in db.query(SignalPolicyDecision).all()}
    models = db.query(ModelRun).filter(ModelRun.started_at >= since).all()
    executions = db.query(ExecutionEvent).filter(ExecutionEvent.created_at >= since).all()
    hard_latency = [(r.finished_at - r.started_at).total_seconds() for r in risk if r.finished_at]
    fast_latency = [m.latency_ms / 1000 for m in models if m.role == "FAST" and m.status == "OK"]
    duplicate = len({a.signal_id for a in actionable}) != len(actionable)
    traceable = sum(bool((s := by_id.get(a.signal_id)) and s.trace_id and s.truth_snapshot_id
                         and s.plan_version and s.evidence_snapshot_id and s.prompt_version
                         and s.requested_model and a.signal_id in policies) for a in actionable)
    approved_lifecycle = {e.signal_id: e.occurred_at for e in db.query(SignalLifecycleEvent)
                          .filter_by(to_status="APPROVED").all()}
    stale_actionable = sum(bool(
        (s := by_id.get(a.signal_id)) is None
        or s.valid_from > a.approved_at or s.expires_at <= a.approved_at
        or a.signal_id not in approved_lifecycle
        or approved_lifecycle[a.signal_id] > a.approved_at
    ) for a in actionable)
    stop_widening = sum(bool(s.reason_codes and "STOP_WIDENING" in s.reason_codes) for s in signals
                        if s.signal_id in {a.signal_id for a in actionable})
    return {
        "sample_counts": {"risk_positions": len(colors), "risk_runs": len(risk),
                          "fixed_runs": len(fixed), "actionable": len(actionable),
                          "model_runs": len(models), "executions": len(executions)},
        "freshness": _ratio(sum(c in {"GREEN", "RED"} for c in colors), len(colors)),
        "critical_stale_actionable": stale_actionable,
        "traceability": _ratio(traceable, len(actionable)),
        "policy_bypass": sum(a.signal_id not in policies for a in actionable),
        "duplicate_actionable": int(duplicate),
        "schema_success": _ratio(sum(m.schema_valid is True for m in models), len(models)),
        "critical_scheduler_missed": sum(r.status in {"MISSED", "FAILED"} for r in rows
                                         if r.step != "ACTIVATED"),
        "eod_reconcile": _ratio(sum(e.reconcile_status == "RECONCILED" for e in executions), len(executions)),
        "stop_widening": int(stop_widening),
        "fast_p95_seconds": _p95(fast_latency),
        "hard_risk_p95_seconds": _p95(hard_latency),
        "eod_run_count": sum(r.step == "EOD_TRUTH" and r.status == "SUCCEEDED" for r in fixed),
    }


def acceptance_gate(metrics: dict) -> dict:
    n = metrics["sample_counts"]
    checks = {
        "freshness>=99.5%": n["risk_positions"] > 0 and metrics["freshness"] is not None and metrics["freshness"] >= .995,
        "critical_stale_actionable=0": n["actionable"] > 0 and metrics["critical_stale_actionable"] == 0,
        "traceability=100%": metrics["traceability"] == 1,
        "policy_bypass=0": n["actionable"] > 0 and metrics["policy_bypass"] == 0,
        "duplicate_actionable=0": n["actionable"] > 0 and metrics["duplicate_actionable"] == 0,
        "schema_success>=99%": n["model_runs"] > 0 and metrics["schema_success"] is not None and metrics["schema_success"] >= .99,
        "critical_scheduler_missed=0": (n["fixed_runs"] + n["risk_runs"] > 0
                                         and metrics["critical_scheduler_missed"] == 0),
        "eod_reconcile=100%": n["executions"] > 0 and metrics["eod_reconcile"] == 1 and metrics["eod_run_count"] > 0,
        "stop_widening=0": n["actionable"] > 0 and metrics["stop_widening"] == 0,
        "FAST_P95<20s": metrics["fast_p95_seconds"] is not None and metrics["fast_p95_seconds"] < 20,
        "HardRisk_P95<30s": metrics["hard_risk_p95_seconds"] is not None and metrics["hard_risk_p95_seconds"] < 30,
    }
    return {"checks": checks, "passed": all(checks.values()),
            "status": "ELIGIBLE_FOR_HUMAN_APPROVAL" if all(checks.values()) else "FAIL_CLOSED"}


def review_snapshot(db: Session, *, since: datetime, cadence: str) -> dict:
    metrics = acceptance_metrics(db, since=since)
    issues = db.query(SystemIssue).filter(SystemIssue.last_seen >= since).all()
    prompts = db.query(PromptVersion).all()
    return {"cadence": cadence, "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": since.replace(tzinfo=timezone.utc).isoformat(), "metrics": metrics,
            "gate": acceptance_gate(metrics),
            "issues": [{"issue_id": i.issue_id, "severity": i.severity,
                        "status": i.status, "occurrences": i.occurrence_count,
                        "signature": i.error_signature} for i in issues],
            "prompt_candidates": [{"prompt_id": p.prompt_id, "version": p.version,
                                   "status": p.status} for p in prompts if p.status == "CANDIDATE"],
            "shadow_due": cadence == "BIWEEKLY", "model_data_threshold_review_due": cadence == "MONTHLY",
            "promotion": "NONE"}
