"""Export a public, allowlisted record of one A-share trading day.

The local SQLite database contains credentials and account details. This script
never copies the database or serializes whole ORM objects or workflow payloads.
"""

from __future__ import annotations

import argparse
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
    ActionableSignal, DailyPortfolioPlan, ModelRun, PortfolioTruthSnapshot,
    PortfolioWorkflowRun, SignalEvent, SignalPolicyDecision, SystemIssue,
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
        policies = [p for p in db.query(SignalPolicyDecision).order_by(SignalPolicyDecision.id)
                    if p.signal_id in signal_ids]
        actionable = [a for a in db.query(ActionableSignal) if a.signal_id in signal_ids]
        models = db.query(ModelRun).filter(ModelRun.started_at >= since,
                                           ModelRun.started_at < until).order_by(ModelRun.started_at).all()
        issues = db.query(SystemIssue).filter(SystemIssue.last_seen >= since,
                                              SystemIssue.first_seen < until).all()
        review = review_snapshot(db, since=since, cadence="PUBLIC_AFTER_CLOSE")

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
        "missing_close_steps": missing_close_steps,
        "counts": {"truth_snapshots": len(truth_data), "workflow_runs": len(run_data),
                   "daily_plans": len(plan_data), "signals": len(signal_data),
                   "policy_decisions": len(policy_data), "actionable_signals": len(actionable_data),
                   "model_runs": len(model_data), "issues": len(issue_data),
                   "minute_symbols": len(market)},
        "run_statuses": dict(Counter(r.status for r in runs)),
        "acceptance_gate": review["gate"]["status"],
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
