"""Write a private, local-only message/decision review packet for one day."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    ActionableSignal, AppSettings, EvidenceSnapshot, ExecutionEvent, ModelRun, PortfolioDecision,
    PortfolioFeatureSnapshot, PortfolioLevelSnapshot, PortfolioNotification,
    PortfolioWorkflowRun, SignalEvent, SignalPolicyDecision, SystemIssue,
    PortfolioRiskObservation,
)


def export_review(day: str, root: Path | None = None) -> dict:
    datetime.strptime(day, "%Y-%m-%d")
    since = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).replace(tzinfo=None)
    until = since + timedelta(days=1)
    target = (root or ROOT / "data" / "notification_reviews") / day
    target.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        notifications = db.query(PortfolioNotification).filter_by(trade_date=day).order_by(
            PortfolioNotification.queued_at, PortfolioNotification.id).all()
        observations = db.query(PortfolioRiskObservation).filter_by(trade_date=day).order_by(
            PortfolioRiskObservation.observed_at, PortfolioRiskObservation.id).all()
        decisions = db.query(PortfolioDecision).filter_by(trade_date=day).order_by(
            PortfolioDecision.symbol, PortfolioDecision.revision).all()
        levels = db.query(PortfolioLevelSnapshot).filter_by(trade_date=day).order_by(
            PortfolioLevelSnapshot.id).all()
        features = db.query(PortfolioFeatureSnapshot).filter_by(trade_date=day).order_by(
            PortfolioFeatureSnapshot.id).all()
        signals = db.query(SignalEvent).filter_by(trade_date=day).all()
        signal_ids = {s.signal_id for s in signals}
        evidence_ids = {s.evidence_snapshot_id for s in signals if s.evidence_snapshot_id}
        evidence = db.query(EvidenceSnapshot).filter(EvidenceSnapshot.id.in_(evidence_ids)).all()
        executions = [e for e in db.query(ExecutionEvent).all() if e.signal_id in signal_ids]
        policies = [p for p in db.query(SignalPolicyDecision).all() if p.signal_id in signal_ids]
        approvals = [a for a in db.query(ActionableSignal).all() if a.signal_id in signal_ids]
        runs = db.query(PortfolioWorkflowRun).filter_by(trade_date=day).all()
        models = db.query(ModelRun).filter(ModelRun.started_at >= since,
                                            ModelRun.started_at < until).all()
        issues = db.query(SystemIssue).filter(SystemIssue.last_seen >= since,
                                               SystemIssue.first_seen < until).all()
        mode = db.query(AppSettings).filter_by(key="portfolio_discipline_mode").first()
        payload = {
            "trade_date": day, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "PRIVATE_LOCAL_ONLY_DO_NOT_PUBLISH",
            "effective_mode": mode.value if mode else "disabled",
            "metrics": {
                "model_proposals": len(signals), "decision_revisions": len(decisions),
                "queued_notifications": len(notifications),
                "risk_observations": len(observations),
                "delivery_statuses": dict(Counter(n.delivery_status for n in notifications)),
                "user_reported_executions": len(executions),
                "quantity_matched_unverified": sum(e.reconcile_status == "QUANTITY_MATCHED_UNVERIFIED" for e in executions),
                "broker_verified_executions": sum(e.reconcile_status == "RECONCILED" for e in executions),
                "user_reads": None,
            },
            "workflow_runs": [{"run_id": r.run_id, "step": r.step, "slot": r.slot,
                               "status": r.status, "input_hash": r.input_hash,
                               "output_hash": r.output_hash, "started_at": str(r.started_at),
                               "finished_at": str(r.finished_at)} for r in runs],
            "model_runs": [{"run_id": m.run_id, "role": m.role, "status": m.status,
                            "schema_valid": m.schema_valid, "requested_model": m.requested_model,
                            "reported_model": m.reported_model, "prompt_id": m.prompt_id,
                            "prompt_version": m.prompt_version, "prompt_hash": m.prompt_hash,
                            "input_hash": m.input_hash, "output_hash": m.output_hash,
                            "output_text": m.output_text, "error": m.error,
                            "latency_ms": m.latency_ms, "input_tokens": m.input_tokens,
                            "output_tokens": m.output_tokens, "cost_usd": m.cost_usd}
                           for m in models],
            "evidence": [{"id": e.id, "source": e.source, "logical_hash": e.logical_hash,
                          "captured_at": str(e.captured_at), "payload": e.payload}
                         for e in evidence],
            "signals": [{"signal_id": s.signal_id, "symbol": s.symbol,
                         "action": s.action, "status": s.status,
                         "truth_snapshot_id": s.truth_snapshot_id,
                         "evidence_snapshot_id": s.evidence_snapshot_id,
                         "plan_version": s.plan_version,
                         "prompt_id": s.prompt_id, "prompt_version": s.prompt_version,
                         "generated_at": str(s.generated_at), "expires_at": str(s.expires_at)}
                        for s in signals],
            "policy_decisions": [{"signal_id": p.signal_id, "rule_id": p.rule_id,
                                  "decision": p.decision, "reason_codes": p.reason_codes,
                                  "input_hash": p.input_hash} for p in policies],
            "approvals": [{"signal_id": a.signal_id, "approved_qty": a.approved_qty,
                           "policy_version": a.policy_version, "input_hash": a.input_hash}
                          for a in approvals],
            "issues": [{"issue_id": i.issue_id, "category": i.category,
                        "severity": i.severity, "status": i.status,
                        "occurrence_count": i.occurrence_count}
                       for i in issues],
            "notifications": [{
                "id": n.id, "semantic_key": n.semantic_key, "symbol": n.symbol,
                "decision_id": n.decision_id, "signal_ids": n.signal_ids,
                "template_version": n.template_version, "title": n.title, "body": n.body,
                "content_hash": n.rendered_content_hash, "priority": n.priority,
                "reason": n.reason, "suppression_reason": n.suppression_reason,
                "queued_at": str(n.queued_at), "attempt_started_at": str(n.attempt_started_at),
                "provider_accepted_at": str(n.provider_accepted_at),
                "delivery_status": n.delivery_status, "ack_status": n.ack_status,
                "retry_count": n.retry_count, "supersedes_id": n.supersedes_id,
            } for n in notifications],
            "risk_observations": [{
                "id": r.id, "episode_key": r.episode_key,
                "symbol": r.symbol, "observation_type": r.observation_type,
                "severity": r.severity, "source_signal_ids": r.source_signal_ids,
                "market_evidence_snapshot_id": r.market_evidence_snapshot_id,
                "level_snapshot_id": r.level_snapshot_id,
                "data_quality": r.data_quality,
                "execution_readiness": r.execution_readiness,
                "notice_outcome": r.notice_outcome, "notice_reason": r.notice_reason,
                "notification_id": r.notification_id,
                "observed_at": str(r.observed_at), "expires_at": str(r.expires_at),
            } for r in observations],
            "decisions": [{
                "id": d.id, "symbol": d.symbol, "revision": d.revision,
                "signal_id": d.signal_id, "action": d.action, "approved_qty": d.approved_qty,
                "decision_status": d.decision_status, "risk_status": d.risk_status,
                "data_status": d.data_status, "execution_status": d.execution_status,
                "level_snapshot_id": d.level_snapshot_id, "semantic_hash": d.semantic_hash,
                "current": d.current, "created_at": str(d.created_at), "expires_at": str(d.expires_at),
            } for d in decisions],
            "levels": [{"id": l.id, "feature_snapshot_id": l.feature_snapshot_id,
                        "prior_level_snapshot_id": l.prior_level_snapshot_id,
                        "market_asof": str(l.market_asof), "quality_status": l.quality_status,
                        "payload": l.payload} for l in levels],
            "features": [{"id": f.id, "symbol": f.symbol, "market_asof": str(f.market_asof),
                          "fetched_at": str(f.fetched_at), "source_hash": f.source_hash,
                          "quality_status": f.quality_status, "payload": f.payload} for f in features],
            "executions": [{"execution_id": e.execution_id, "signal_id": e.signal_id,
                            "actual_action": e.actual_action, "actual_qty": e.actual_qty,
                            "result": e.result, "reconcile_status": e.reconcile_status,
                            "executed_at": str(e.executed_at)} for e in executions],
        }
    output = target / "review.json"
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(output)
    return {"path": str(output), "notifications": len(notifications),
            "decisions": len(decisions), "signals": len(signals),
            "delivery_statuses": dict(Counter(n.delivery_status for n in notifications))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    args = parser.parse_args()
    print(json.dumps(export_review(args.trade_date), ensure_ascii=False))
