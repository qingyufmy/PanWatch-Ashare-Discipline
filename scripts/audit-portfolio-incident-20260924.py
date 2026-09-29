"""Rebuild the 2026-09-24 proposal-to-notice funnel from a frozen SQLite copy.

All per-signal output is private and belongs under ignored ``data/``. The script
does not mutate the source database or reinterpret historical policy outcomes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-09-24"


def _json(value, default=None):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _stamp(value):
    return datetime.fromisoformat(value) if value else None


def _rows(db, query, args=()):
    return [dict(row) for row in db.execute(query, args)]


def _write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _csv(path: Path, rows: list[dict], columns: list[str]):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def audit(snapshot: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    signals = _rows(db, "SELECT * FROM signal_events WHERE trade_date=? ORDER BY generated_at, signal_id", (DAY,))
    ids = {row["signal_id"] for row in signals}
    policies = defaultdict(list)
    for row in _rows(db, "SELECT * FROM signal_policy_decisions ORDER BY created_at, id"):
        if row["signal_id"] in ids:
            policies[row["signal_id"]].append(row)
    evidence = {row["id"]: row for row in _rows(db, "SELECT * FROM evidence_snapshots")}
    models = {row["run_id"]: row for row in _rows(db, "SELECT * FROM model_runs")}
    truth = {row["id"]: row for row in _rows(db, "SELECT * FROM portfolio_truth_snapshots")}
    plans = {(row["trade_date"], row["version"]): row for row in
             _rows(db, "SELECT * FROM daily_portfolio_plans")}
    decisions = defaultdict(list)
    for row in _rows(db, "SELECT * FROM portfolio_decisions WHERE trade_date=? ORDER BY revision, id", (DAY,)):
        decisions[row["signal_id"]].append(row)
    notices = defaultdict(list)
    for row in _rows(db, "SELECT * FROM portfolio_notifications WHERE trade_date=?", (DAY,)):
        for signal_id in _json(row["signal_ids"], []):
            notices[signal_id].append(row)
    executions = defaultdict(list)
    for row in _rows(db, "SELECT * FROM execution_events"):
        if row["signal_id"] in ids:
            executions[row["signal_id"]].append(row)
    actionable = {row["signal_id"]: row for row in _rows(db, "SELECT * FROM actionable_signals")}

    attribution = []
    timeline = []
    reason_counts = Counter()
    missing = Counter()
    for signal in signals:
        sid = signal["signal_id"]
        ev = evidence.get(signal["evidence_snapshot_id"])
        payload = _json(ev["payload"], {}) if ev else {}
        model_id = payload.get("model_run_id") if isinstance(payload, dict) else None
        model = models.get(model_id)
        verdicts = policies[sid]
        reasons = sorted({reason for item in verdicts for reason in _json(item["reason_codes"], []) if reason})
        reason_counts.update(reasons)
        notice_rows = notices[sid]
        decision_rows = decisions[sid]
        plan = plans.get((signal["trade_date"], signal["daily_plan_version"]))
        tr = truth.get(signal["truth_snapshot_id"])
        has_direction = signal["action"] in {"REDUCE", "EXIT"}
        if has_direction and not notice_rows:
            missing[(signal["source"], signal["status"], "NO_NOTICE_LINK")]+=1
        if model_id and model is None:
            missing[(signal["source"], signal["status"], "MODEL_REFERENCE_MISSING")]+=1
        if signal["source"] == "AGENT" and not model_id:
            missing[(signal["source"], signal["status"], "MODEL_NOT_RECORDED")]+=1
        first_policy = min((r["created_at"] for r in verdicts), default=None)
        last_policy = max((r["created_at"] for r in verdicts), default=None)
        approved = actionable.get(sid)
        record = {
            "signal_id": sid, "symbol": signal["symbol"], "source": signal["source"],
            "raw_action": signal["raw_action"], "resolved_action": signal["action"],
            "status": signal["status"], "signal_time": signal["generated_at"],
            "created_at": signal["created_at"], "model_run_id": model_id,
            "model_started": model["started_at"] if model else None,
            "model_finished": model["finished_at"] if model else None,
            "truth_id": signal["truth_snapshot_id"],
            "truth_source": tr["source"] if tr else None,
            "truth_status": tr["truth_status"] if tr else None,
            "daily_plan_id": plan["id"] if plan else None,
            "plan_version": signal["plan_version"],
            "daily_plan_version": signal["daily_plan_version"],
            "evidence_snapshot_id": signal["evidence_snapshot_id"],
            "feature_snapshot_id": (payload.get("meta") or {}).get("feature_snapshot_id") if isinstance(payload, dict) else None,
            "level_snapshot_id": (payload.get("meta") or {}).get("level_snapshot_id") if isinstance(payload, dict) else None,
            "prompt_id": signal["prompt_id"], "prompt_version": signal["prompt_version"],
            "policy_count": len(verdicts), "policy_first_at": first_policy,
            "policy_last_at": last_policy, "policy_reasons": "|".join(reasons),
            "decision_revision": max((r["revision"] for r in decision_rows), default=None),
            "notice_count": len(notice_rows),
            "notice_statuses": "|".join(sorted({r["delivery_status"] for r in notice_rows})),
            "approved_at": approved["approved_at"] if approved else None,
            "execution_count": len(executions[sid]),
            "broker_reconciled_count": sum(r["reconcile_status"] == "RECONCILED" for r in executions[sid]),
        }
        attribution.append(record)
        if model and (signal["source"] in {"daily_portfolio_plan", "intraday_portfolio_plan"}):
            anomaly = bool(
                (_stamp(signal["generated_at"]) or datetime.min) < (_stamp(model["finished_at"]) or datetime.min)
                or approved and _stamp(approved["approved_at"]) < _stamp(model["finished_at"])
                or first_policy and _stamp(first_policy) < _stamp(model["finished_at"])
            )
            timeline.append({**{k: record[k] for k in (
                "signal_id", "symbol", "source", "signal_time", "created_at", "model_run_id",
                "model_started", "model_finished", "policy_first_at", "policy_last_at", "approved_at")},
                "causality_violation": anomaly})

    risk_rows = [r for r in attribution if r["resolved_action"] in {"REDUCE", "EXIT"}]
    episodes = []
    by_symbol = defaultdict(list)
    for row in risk_rows:
        by_symbol[row["symbol"]].append(row)
    for symbol, items in sorted(by_symbol.items()):
        current = []
        for row in items:
            if current and _stamp(row["signal_time"]) - _stamp(current[-1]["signal_time"]) > timedelta(minutes=30):
                episodes.append(current)
                current = []
            current.append(row)
        if current:
            episodes.append(current)
    episode_rows = [{
        "symbol": group[0]["symbol"], "first_at": group[0]["signal_time"],
        "last_at": group[-1]["signal_time"], "proposal_count": len(group),
        "actions": "|".join(sorted({r["resolved_action"] for r in group})),
        "statuses": "|".join(sorted({r["status"] for r in group})),
        "notice_count": sum(r["notice_count"] for r in group),
        "signal_ids": "|".join(r["signal_id"] for r in group),
    } for group in episodes]
    summary = {
        "trade_date": DAY, "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "source_revision": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "signals": len(signals), "by_source_action_status": [
            {"source": source, "action": action, "status": status, "count": count}
            for (source, action, status), count in sorted(Counter(
                (r["source"], r["resolved_action"], r["status"]) for r in attribution).items())],
        "approved_envelopes": sum(r["approved_at"] is not None for r in attribution),
        "approved_directional": sum(r["approved_at"] is not None and r["resolved_action"] in {"ADD", "REDUCE", "EXIT"} for r in attribution),
        "directional_proposals": len(risk_rows), "directional_episodes": len(episodes),
        "notifications": len(_rows(db, "SELECT id FROM portfolio_notifications WHERE trade_date=?", (DAY,))),
        "paper_fills": db.execute("SELECT count(*) FROM paper_portfolio_fills WHERE trade_date=?", (DAY,)).fetchone()[0],
        "execution_events": sum(map(len, executions.values())),
        "time_violations": sum(r["causality_violation"] for r in timeline),
        "policy_reason_counts_nonexclusive": dict(sorted(reason_counts.items())),
    }
    _write(output / "execution_funnel.json", summary)
    _write(output / "notification_missing_attribution.json", [
        {"source": source, "status": status, "reason": reason, "count": count}
        for (source, status, reason), count in sorted(missing.items())])
    _csv(output / "signal_attribution.csv", attribution, list(attribution[0]) if attribution else ["signal_id"])
    _csv(output / "risk_episode_review.csv", episode_rows, list(episode_rows[0]) if episode_rows else ["symbol"])
    _csv(output / "timeline_audit.csv", timeline, list(timeline[0]) if timeline else ["signal_id"])
    _write(output / "provenance_validation.json", {
        "signal_count": len(signals),
        "missing_truth": sum(r["truth_id"] not in truth for r in attribution),
        "missing_evidence": sum(r["evidence_snapshot_id"] not in evidence for r in attribution),
        "missing_policy": sum(r["policy_count"] == 0 for r in attribution),
        "recorded_model_reference_missing": sum(r["model_run_id"] is not None and r["model_run_id"] not in models for r in attribution),
        "model_reference_not_recorded": sum(r["model_run_id"] is None for r in attribution),
        "timeline_violations": summary["time_violations"],
    })
    db.close()
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.snapshot.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))
