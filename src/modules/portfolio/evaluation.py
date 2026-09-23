"""P8 frozen-evidence replay. No live DB, market feed, or model calls are permitted."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

AXES = ("signal", "position", "discipline", "model", "data", "system")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def load_frozen_cases(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    cases = json.loads(raw)
    if not isinstance(cases, list) or len({c.get("case_id") for c in cases}) != len(cases):
        raise ValueError("invalid_or_duplicate_eval_cases")
    for case in cases:
        cutoff = _time(case["cutoff"])
        frozen = case["frozen"]
        for key in ("evidence", "truth", "plan"):
            item = frozen[key]
            if _time(item["asof"]) > cutoff:
                raise ValueError(f"future_data:{case['case_id']}:{key}")
        if "expected" not in case or "proposals" not in case:
            raise ValueError("case_missing_expected_or_proposals")
    return cases, hashlib.sha256(raw).hexdigest()


def replay_case(case: dict, variant: str) -> dict:
    """Re-evaluate a frozen proposal using only case content, not current state."""
    frozen = case["frozen"]
    proposal = case["proposals"][variant]
    action = proposal.get("action")
    if action not in {"OPEN", "ADD", "HOLD", "REDUCE", "EXIT"}:
        verdict, reason = "BLOCK", "SCHEMA_INVALID"
    elif not frozen["truth"].get("trusted") or not frozen["evidence"].get("fresh"):
        verdict, reason = "REVIEW", "TRUTH_OR_DATA_STALE"
    elif action in {"OPEN", "ADD"} and frozen["plan"].get("thesis") in {"INVALID", "UNKNOWN"}:
        verdict, reason = "BLOCK", "THESIS_NOT_VALID"
    elif action in {"REDUCE", "EXIT"} and proposal.get("quantity", 0) > frozen["truth"].get("sellable_qty", 0):
        verdict, reason = "BLOCK", "T_PLUS_ONE"
    elif action in {"OPEN", "ADD"} and proposal.get("target_weight", 0) > frozen["plan"].get("max_weight", 0):
        verdict, reason = "BLOCK", "MAX_POSITION"
    elif proposal.get("new_stop") is not None and frozen["plan"].get("current_stop") is not None and proposal["new_stop"] < frozen["plan"]["current_stop"]:
        verdict, reason = "BLOCK", "STOP_WIDENING"
    elif proposal.get("fast_deep_conflict"):
        verdict, reason = "REVIEW", "FAST_DEEP_CONFLICT"
    else:
        verdict, reason = "PASS", ""
    expected = case["expected"]
    checks = {
        "signal": verdict == expected["verdict"],
        "position": reason not in {"MAX_POSITION", "THESIS_NOT_VALID"} or verdict == "BLOCK",
        "discipline": reason not in {"STOP_WIDENING", "T_PLUS_ONE"} or verdict == "BLOCK",
        "model": reason != "SCHEMA_INVALID" or verdict == "BLOCK",
        "data": reason != "TRUTH_OR_DATA_STALE" or verdict == "REVIEW",
        "system": True,
    }
    return {"case_id": case["case_id"], "variant": variant, "verdict": verdict,
            "reason": reason, "expected": expected, "checks": checks,
            "pass": all(checks.values())}


def compare(cases: list[dict], *, champion: dict, challenger: dict, input_hash: str) -> dict:
    """Challenger is strictly shadow output; this function never activates it."""
    for label, spec in (("champion", champion), ("challenger", challenger)):
        if not all(spec.get(key) for key in ("prompt_version", "model_version", "policy_version")):
            raise ValueError(f"{label}_version_missing")
    results = {name: [replay_case(c, name) for c in cases]
               for name in ("champion", "challenger")}
    metrics = {}
    for name, rows in results.items():
        metrics[name] = {"cases": len(rows), "passed": sum(r["pass"] for r in rows),
                         "axis_pass": {axis: sum(r["checks"][axis] for r in rows) for axis in AXES}}
    return {"input_hash": input_hash, "champion": champion, "challenger": challenger,
            "challenger_mode": "SHADOW_ONLY", "metrics": metrics, "results": results}


def write_comparison(result: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prompt_comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 冻结证据 Champion / Challenger 对比", "",
             f"证据 SHA-256：`{result['input_hash']}`；Challenger 仅写 Shadow 结果。", "",
             "| 版本 | 用例通过 | Signal | Position | Discipline | Model | Data | System |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name in ("champion", "challenger"):
        m = result["metrics"][name]
        lines.append("| " + name + " | " + " | ".join(str(x) for x in
                     [f"{m['passed']}/{m['cases']}", *(m['axis_pass'][a] for a in AXES)]) + " |")
    lines += ["", "此结果只覆盖冻结用例的确定性不变量，不能证明实盘收益或 PROD 门禁。", ""]
    (directory / "prompt_comparison.md").write_text("\n".join(lines), encoding="utf-8")
