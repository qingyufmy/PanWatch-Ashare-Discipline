"""Export a public, allowlisted record of one A-share trading day.

The local SQLite database contains credentials and account details. This script
never copies the database or serializes whole ORM objects or workflow payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules.portfolio.review_gate import review_snapshot
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    ActionableSignal, AgentRun, DailyPortfolioPlan, EvidenceSnapshot, ExecutionEvent, ModelRun, PortfolioTruthSnapshot,
    PortfolioWorkflowRun, SignalEvent, SignalPolicyDecision, SystemIssue,
    PortfolioNotification, PortfolioDecision, PaperPortfolioFill, PaperPortfolioNav,
    PortfolioFeatureSnapshot, PortfolioLevelSnapshot,
)

SH = ZoneInfo("Asia/Shanghai")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXPECTED_CLOSE_STEPS = {"EOD_TRUTH", "DAILY_REVIEW"}


def _stamp(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _write_json(directory: Path, name: str, data: object) -> None:
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _source_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _price_evidence_linked(db, notice, decisions: dict, signals: dict) -> bool:
    """Only a same-signal, same-feature, timely confirmed level earns this flag."""
    decision = decisions.get(notice.decision_id)
    if (decision is None or decision.level_snapshot_id is None
            or decision.decision_status != "APPROVED"
            or notice.decision_revision != decision.revision
            or decision.signal_id not in (notice.signal_ids or [])):
        return False
    signal = signals.get(decision.signal_id)
    level = db.get(PortfolioLevelSnapshot, decision.level_snapshot_id)
    if signal is None or level is None or level.quality_status != "CONFIRMED":
        return False
    feature = db.get(PortfolioFeatureSnapshot, level.feature_snapshot_id)
    evidence = db.get(EvidenceSnapshot, signal.evidence_snapshot_id)
    if feature is None or feature.quality_status != "OK" or evidence is None:
        return False
    meta = (evidence.payload or {}).get("meta") or {}
    if (not isinstance(meta, dict)
            or meta.get("feature_snapshot_id") != feature.id
            or meta.get("level_snapshot_id") != level.id
            or level.trade_date != signal.trade_date
            or feature.trade_date != signal.trade_date
            or feature.symbol != level.symbol
            or level.symbol[-6:] != decision.symbol
            or (feature.payload or {}).get("missing_minutes")):
        return False
    return bool(level.market_asof <= decision.created_at
                and timedelta(0) <= decision.created_at - level.market_asof <= timedelta(seconds=120))


def _market_coverage(day: str) -> list[dict]:
    base = ROOT / "data" / "minute_bars" / f"trade_date={day}"
    rows: list[dict] = []
    if not base.is_dir():
        return rows
    import pandas as pd

    for path in sorted(base.glob("symbol=*/bars.parquet")):
        frame = pd.read_parquet(path, columns=["timestamp", "source"])
        if frame.empty:
            continue
        times = pd.to_datetime(frame["timestamp"])
        rows.append({
            "symbol": path.parent.name.removeprefix("symbol="),
            "bars": len(frame),
            "first_bar": times.min().isoformat(),
            "last_bar": times.max().isoformat(),
            "sources": sorted(set(frame["source"].dropna().astype(str))),
        })
    return rows


def export_day(day: str, output_root: Path) -> dict:
    if not DAY_RE.fullmatch(day) or datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
        raise ValueError("invalid_trade_date")
    since = datetime.fromisoformat(day) - timedelta(hours=8)
    until = since + timedelta(days=1)
    generated = datetime.now(timezone.utc)

    with SessionLocal() as db:
        truths = db.query(PortfolioTruthSnapshot).filter_by(trade_date=day).order_by(
            PortfolioTruthSnapshot.id).all()
        runs = db.query(PortfolioWorkflowRun).filter_by(trade_date=day).order_by(
            PortfolioWorkflowRun.started_at, PortfolioWorkflowRun.step).all()
        plans = db.query(DailyPortfolioPlan).filter_by(trade_date=day).order_by(
            DailyPortfolioPlan.version).all()
        signals = db.query(SignalEvent).filter_by(trade_date=day).order_by(
            SignalEvent.symbol, SignalEvent.generated_at).all()
        signal_ids = {s.signal_id for s in signals}
        executions = db.query(ExecutionEvent).filter(ExecutionEvent.signal_id.in_(signal_ids)).all()
        policies = [p for p in db.query(SignalPolicyDecision).order_by(SignalPolicyDecision.id)
                    if p.signal_id in signal_ids]
        actionable = [a for a in db.query(ActionableSignal) if a.signal_id in signal_ids]
        models = db.query(ModelRun).filter(ModelRun.started_at >= since,
                                           ModelRun.started_at < until).order_by(ModelRun.started_at).all()
        model_by_id = {m.run_id: m for m in models}
        daily_review_complete = any(
            r.step == "DAILY_REVIEW" and r.status in {"SUCCEEDED", "REVIEW"} and
            (model := model_by_id.get((r.payload or {}).get("model_run_id"))) is not None and
            model.schema_valid is True and model.status in {"OK", "MODEL_DEGRADED"}
            for r in runs
        )
        issues = db.query(SystemIssue).filter(SystemIssue.last_seen >= since,
                                              SystemIssue.first_seen < until).all()
        notifications = db.query(PortfolioNotification).filter_by(trade_date=day).order_by(
            PortfolioNotification.queued_at).all()
        decision_ids = {n.decision_id for n in notifications if n.decision_id}
        decisions = {d.id: d for d in db.query(PortfolioDecision).filter(PortfolioDecision.id.in_(decision_ids)).all()}
        signals_by_id = {s.signal_id: s for s in signals}
        review = review_snapshot(db, since=since, cadence="PUBLIC_AFTER_CLOSE")
        paper_nav = db.get(PaperPortfolioNav, day)
        prior_paper_nav = (db.query(PaperPortfolioNav)
                           .filter(PaperPortfolioNav.trade_date < day)
                           .order_by(PaperPortfolioNav.trade_date.desc()).first())
        paper_fills = (db.query(PaperPortfolioFill).filter_by(trade_date=day)
                       .order_by(PaperPortfolioFill.filled_at).all())
        paper_source_qualified = bool(
            paper_nav and paper_nav.source_asof_min and
            paper_nav.source_asof_min.replace(tzinfo=timezone.utc).astimezone(SH).date().isoformat() == day and
            paper_nav.source_asof_min.replace(tzinfo=timezone.utc).astimezone(SH).hour >= 15
        )
        paper_summary = {
            "status": "CAPTURED" if paper_source_qualified else "SOURCE_TIME_UNVERIFIED" if paper_nav else "MISSING",
            "scope": "PAPER_ONLY", "baseline": "SYNTHETIC_USER_ATTESTED",
            "source": paper_nav.source if paper_nav else None,
            "source_asof_min_utc": _stamp(paper_nav.source_asof_min) if paper_nav else None,
            "captured_at_utc": _stamp(paper_nav.captured_at) if paper_nav else None,
            "position_count": paper_nav.position_count if paper_nav else None,
            "prior_nav_date": prior_paper_nav.trade_date if prior_paper_nav else None,
            "daily_return_pct": round(100 * (paper_nav.equity / prior_paper_nav.equity - 1), 4)
            if paper_source_qualified and prior_paper_nav and prior_paper_nav.equity > 0 else None,
            "fill_count": len(paper_fills),
            "fill_actions": dict(Counter(f.action for f in paper_fills)),
        }
        paper_fill_data = [{
            "signal_id": f.signal_id, "symbol": f.symbol, "action": f.action,
            "filled_at_utc": _stamp(f.filled_at), "quote_asof_utc": _stamp(f.quote_asof),
            "policy_scope": f.policy_scope,
        } for f in paper_fills]
        analysis_data = []
        for run in runs:
            if run.step not in {"PREMARKET_RECOVERY", "OPEN_CONFIRM", "MORNING_ADJUST",
                                "AFTERNOON_ADJUST", "DAILY_REVIEW"}:
                continue
            payload = run.payload or {}
            macro_id = payload.get("macro_evidence_snapshot_id")
            macro = db.get(EvidenceSnapshot, macro_id) if macro_id else None
            market_context = macro.payload if macro and isinstance(macro.payload, dict) else {}
            linked_ids = set(payload.get("signal_ids") or [])
            linked = [s for s in signals if s.signal_id in linked_ids]
            breadth = market_context.get("candidate_breadth") or {}
            analysis_data.append({
                "step": run.step, "status": run.status, "reason_code": payload.get("reason"),
                "started_at_utc": _stamp(run.started_at), "finished_at_utc": _stamp(run.finished_at),
                "model_run_id": payload.get("model_run_id"), "macro_evidence_snapshot_id": macro_id,
                "auction_evidence_snapshot_id": payload.get("auction_evidence_snapshot_id"),
                "risk_tone": market_context.get("risk_tone"),
                "index_observations": [{
                    "symbol": q.get("symbol"), "change_pct": q.get("change_pct"),
                    "quality": q.get("quality"), "source_asof": q.get("source_asof"),
                } for q in market_context.get("indices", []) if isinstance(q, dict)],
                "global_tech_observations": [{
                    "symbol": q.get("symbol"), "change_pct": q.get("change_pct"),
                    "quality": q.get("quality"), "source_asof": q.get("source_asof"),
                } for q in market_context.get("global_tech", []) if isinstance(q, dict)],
                "board_observations": [{
                    "symbol": q.get("symbol"), "change_pct": q.get("change_pct"),
                    "quality": q.get("quality"), "source_asof": q.get("source_asof"),
                } for q in market_context.get("boards", []) if isinstance(q, dict)],
                "candidate_breadth": {k: breadth.get(k) for k in
                                      ("snapshot_date", "breadth_up_pct", "sample_size", "scope",
                                       "regime", "confidence")},
                "fresh_holding_quote_count": sum(q.get("quality") == "FRESH" for q in
                                                 market_context.get("holdings", []) if isinstance(q, dict)),
                "action_counts": dict(Counter(s.action for s in linked)) or payload.get("action_counts") or {},
                "notification_status": (payload.get("notification") or {}).get("status"),
            })
        agent_runs = db.query(AgentRun).filter(AgentRun.created_at >= since,
                                              AgentRun.created_at < until).all()
        agent_analysis = [{
            "agent_name": name, "run_count": len(rows),
            "success_count": sum(r.status == "success" for r in rows),
            "failed_count": sum(r.status == "failed" for r in rows),
            "notifications_sent_count": sum(bool(r.notify_sent) for r in rows),
        } for name in sorted({r.agent_name for r in agent_runs})
           for rows in [[r for r in agent_runs if r.agent_name == name]]]

        truth_data = [{
            "id": t.id, "phase": t.phase, "source": t.source,
            "source_asof_utc": _stamp(t.source_asof), "fetched_at_utc": _stamp(t.fetched_at),
            "truth_status": t.truth_status, "freshness_at_capture": t.freshness,
            "anomaly_flags": t.anomaly_flags,
            "position_count": len(t.positions),
            "symbols": sorted({p.symbol for p in t.positions}),
        } for t in truths]
        run_data = []
        for r in runs:
            payload = r.payload or {}
            run_data.append({
                "run_id": r.run_id, "step": r.step, "slot": r.slot,
                "status": r.status, "started_at_utc": _stamp(r.started_at),
                "finished_at_utc": _stamp(r.finished_at),
                "reason_code": payload.get("reason"),
                "coverage": payload.get("coverage"), "ok": payload.get("ok"),
                "red": payload.get("red"), "yellow": payload.get("yellow"),
                "model_run_id": payload.get("model_run_id"),
                "daily_plan_id": payload.get("daily_plan_id"),
                "signal_count": len(payload.get("signal_ids") or []),
                "notification_status": (payload.get("notification") or {}).get("status"),
            })
        plan_data = [{
            "id": p.id, "version": p.version, "status": p.status,
            "truth_snapshot_id": p.truth_snapshot_id, "model_run_id": p.model_run_id,
            "prompt_id": p.prompt_id, "prompt_version": p.prompt_version,
            "input_hash": p.input_hash, "output_hash": p.output_hash,
            "proposals": [{
                "market": x.get("market"), "symbol": x.get("symbol"),
                "action": x.get("action"), "confidence": x.get("confidence"),
            } for x in (p.payload or {}).get("proposals", [])],
        } for p in plans]
        signal_data = [{
            "signal_id": s.signal_id, "trace_id": s.trace_id,
            "symbol": s.symbol, "market": s.market, "action": s.action,
            "status": s.status, "reason_codes": s.reason_codes,
            "truth_snapshot_id": s.truth_snapshot_id,
            "plan_version": s.plan_version, "daily_plan_version": s.daily_plan_version,
            "evidence_snapshot_id": s.evidence_snapshot_id,
            "prompt_id": s.prompt_id, "prompt_version": s.prompt_version,
            "model_role": s.model_role, "requested_model": s.requested_model,
            "reported_model": s.reported_model,
            "generated_at_utc": _stamp(s.generated_at), "expires_at_utc": _stamp(s.expires_at),
        } for s in signals]
        policy_data = [{
            "signal_id": p.signal_id, "rule_id": p.rule_id,
            "decision": p.decision, "reason_codes": p.reason_codes,
            "input_hash": p.input_hash,
        } for p in policies]
        model_data = [{
            "run_id": m.run_id, "role": m.role, "profile_role": m.profile_role,
            "status": m.status, "schema_valid": m.schema_valid,
            "requested_model": m.requested_model, "reported_model": m.reported_model,
            "latency_ms": m.latency_ms, "fallback_used": m.fallback_used,
            "started_at_utc": _stamp(m.started_at),
        } for m in models]
        issue_data = [{
            "issue_id": i.issue_id, "category": i.category,
            "severity": i.severity, "status": i.status,
            "title": i.title, "occurrence_count": i.occurrence_count,
        } for i in issues]
        actionable_data = [{
            "signal_id": a.signal_id, "policy_version": a.policy_version,
            "approved_at_utc": _stamp(a.approved_at),
        } for a in actionable]
        notification_data = [{
            "notification_id": n.id,
            "anonymous_position_key": hashlib.sha256(f"{day}:{n.symbol}".encode()).hexdigest()[:16] if n.symbol else None,
            "action_type": decisions[n.decision_id].action if n.decision_id in decisions else "RISK_OR_PLAN",
            "decision_revision": n.decision_revision,
            "price_evidence_linked": _price_evidence_linked(db, n, decisions, signals_by_id),
            "priority": n.priority, "reason_code": n.reason,
            "template_version": n.template_version, "content_hash": n.rendered_content_hash,
            "delivery_status": n.delivery_status, "suppression_reason_code": n.suppression_reason,
            "retry_count": n.retry_count,
            "queued_at_utc": _stamp(n.queued_at), "provider_accepted_at_utc": _stamp(n.provider_accepted_at),
            "ack_status": n.ack_status,
        } for n in notifications]

    market = _market_coverage(day)
    completed = {r.step for r in runs if r.status in {"SUCCEEDED", "REVIEW"}}
    missing_close_steps = sorted(EXPECTED_CLOSE_STEPS - completed)
    manifest = {
        "trade_date": day, "generated_at_utc": generated.isoformat(),
        "source_revision": _source_revision(),
        "publication_scope": "PUBLIC_ALLOWLISTED_SUMMARY",
        "account_data": "EXCLUDED: account IDs, cash, NAV, position quantity, cost, and market value",
        "secret_data": "EXCLUDED: database, credentials, webhook URLs, and model response text",
        "after_close_steps_present": not missing_close_steps,
        "daily_review_complete": daily_review_complete,
        "missing_close_steps": missing_close_steps,
        "counts": {"truth_snapshots": len(truth_data), "workflow_runs": len(run_data),
                   "daily_plans": len(plan_data), "signals": len(signal_data),
                   "policy_decisions": len(policy_data), "actionable_signals": len(actionable_data),
                   "model_runs": len(model_data), "issues": len(issue_data),
                   "minute_symbols": len(market)},
        "notification_summary": {
            "count": len(notification_data),
            "delivery_statuses": dict(Counter(n["delivery_status"] for n in notification_data)),
            "user_reported_executions": len(executions),
            "broker_reconciled_executions": sum(e.reconcile_status == "RECONCILED" for e in executions),
        },
        "run_statuses": dict(Counter(r.status for r in runs)),
        "acceptance_gate": review["gate"]["status"],
        "paper_nav_status": paper_summary["status"],
        "paper_fill_count": paper_summary["fill_count"],
        "analysis_steps": len(analysis_data),
    }
    if not run_data:
        return manifest
    target = output_root.resolve() / day
    target.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("manifest.json", manifest), ("truth_summary.json", truth_data),
        ("workflow_runs.json", run_data), ("daily_plans.json", plan_data),
        ("signals.json", signal_data), ("policy_decisions.json", policy_data),
        ("actionable_signals.json", actionable_data), ("model_runs.json", model_data),
        ("issues.json", issue_data), ("minute_coverage.json", market),
        ("notification_summary.json", notification_data),
        ("paper_summary.json", paper_summary), ("paper_fills.json", paper_fill_data),
        ("intraday_analysis.json", analysis_data), ("agent_analysis_summary.json", agent_analysis),
        ("acceptance.json", {"metrics": review["metrics"], "gate": review["gate"]}),
    ):
        _write_json(target, name, data)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=datetime.now(SH).date().isoformat())
    parser.add_argument("--output-root", type=Path, default=ROOT / "daily_archive")
    args = parser.parse_args()
    print(json.dumps(export_day(args.trade_date, args.output_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
