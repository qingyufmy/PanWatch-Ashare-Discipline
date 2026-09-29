"""Private ex-post price path for Sep24 risk episodes; never infer actual fills.

The minute archive was fetched after close. Its bars are outcome evidence only,
not facts available to the original decision or executable prices.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

SH = ZoneInfo("Asia/Shanghai")
HORIZONS = (5, 15, 30, 60)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 5) if pd.notna(number) else None


def episode_path(frame: pd.DataFrame, event_at: datetime) -> dict:
    """Use only bars closed strictly after the event as later outcome samples."""
    after = frame[pd.to_datetime(frame["timestamp"], utc=True) > event_at].sort_values("timestamp")
    result = {"future_bar_count": len(after), "first_future_bar": None,
              "first_future_close": None, "last_available_bar": None,
              "last_available_close": None, "official_1500_close": None,
              "downside_excursion_pct": None, "upside_excursion_pct": None}
    for horizon in HORIZONS:
        result[f"bar_{horizon}_change_pct"] = None
        result[f"bar_{horizon}_asof"] = None
    if after.empty:
        return result
    first = after.iloc[0]
    last = after.iloc[-1]
    baseline = _num(first["close"])
    result.update({"first_future_bar": pd.Timestamp(first["timestamp"]).isoformat(),
                   "first_future_close": baseline,
                   "last_available_bar": pd.Timestamp(last["timestamp"]).isoformat(),
                   "last_available_close": _num(last["close"])})
    if baseline is None or baseline <= 0:
        return result
    for horizon in HORIZONS:
        if len(after) >= horizon:
            bar = after.iloc[horizon - 1]
            close = _num(bar["close"])
            result[f"bar_{horizon}_asof"] = pd.Timestamp(bar["timestamp"]).isoformat()
            result[f"bar_{horizon}_change_pct"] = round(100 * (close / baseline - 1), 4) if close else None
    window = after.iloc[:60]
    result["downside_excursion_pct"] = round(100 * (baseline - float(window["low"].min())) / baseline, 4)
    result["upside_excursion_pct"] = round(100 * (float(window["high"].max()) - baseline) / baseline, 4)
    return result


def replay(snapshot: Path, episodes_csv: Path, minute_root: Path, output: Path) -> dict:
    if not snapshot.is_file() or not episodes_csv.is_file():
        raise FileNotFoundError("frozen_snapshot_or_episode_csv_missing")
    uri = snapshot.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    rows = list(csv.DictReader(episodes_csv.open(encoding="utf-8-sig", newline="")))
    if not rows:
        raise ValueError("risk_episode_csv_empty")
    frames = {}
    outputs = []
    for episode in rows:
        symbol = episode["symbol"]
        first_id = episode["signal_ids"].split("|")[0]
        signal = db.execute(
            "SELECT signal_id, trade_date, truth_snapshot_id, plan_version, status "
            "FROM signal_events WHERE signal_id=?", (first_id,)
        ).fetchone()
        if signal is None or signal["trade_date"] != "2026-09-24":
            raise ValueError("risk_episode_signal_mismatch")
        truth = db.execute("SELECT truth_status, source FROM portfolio_truth_snapshots WHERE id=?",
                           (signal["truth_snapshot_id"],)).fetchone()
        vendor = ("sh" if symbol.startswith(("6", "9")) else
                  "bj" if symbol.startswith(("4", "8")) else "sz") + symbol
        path = minute_root / f"symbol={vendor}" / "bars.parquet"
        if vendor not in frames:
            frames[vendor] = pd.read_parquet(path) if path.is_file() else pd.DataFrame()
        frame = frames[vendor]
        event_at = _utc(episode["first_at"])
        base = {"symbol": symbol, "first_at_utc": event_at.isoformat(),
                "last_proposal_at_utc": _utc(episode["last_at"]).isoformat(),
                "proposal_count": int(episode["proposal_count"]),
                "proposal_actions": episode["actions"], "proposal_status": episode["statuses"],
                "truth_status": truth["truth_status"] if truth else None,
                "truth_source": truth["source"] if truth else None,
                "plan_version": signal["plan_version"],
                "sellable_quality": "USER_ATTESTED_UNRECONCILED",
                "event_input_price": None,
                "event_input_price_quality": "UNPROVEN_OLD_AGENT_REFERENCE",
                "archive_source": "sina" if not frame.empty and set(frame["source"]) == {"sina"} else "MISSING_OR_MIXED",
                "archive_fetched_after_event": (all(_utc(str(v)) > event_at for v in frame["fetched_at"])
                                                 if not frame.empty else None),
                "execution_eligibility": "UNPROVEN",
                "broker_reconciled_execution": None,
                "estimated_fees": None, "estimated_slippage": None,
                "path_basis": "EX_POST_FIRST_SUBSEQUENT_CLOSED_BAR",
                "official_eod_status": "MISSING_1500_BAR"}
        path_result = episode_path(frame, event_at) if not frame.empty else {
            "future_bar_count": 0, "first_future_bar": None, "first_future_close": None,
            "last_available_bar": None, "last_available_close": None,
            "official_1500_close": None, "downside_excursion_pct": None,
            "upside_excursion_pct": None,
            **{f"bar_{n}_change_pct": None for n in HORIZONS},
            **{f"bar_{n}_asof": None for n in HORIZONS},
        }
        outputs.append({**base, **path_result})
    db.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(outputs[0]))
        writer.writeheader()
        writer.writerows(outputs)
    summary = {
        "trade_date": "2026-09-24", "episode_count": len(outputs),
        "with_future_bar": sum(bool(r["first_future_bar"]) for r in outputs),
        "with_60_trading_bars": sum(r["bar_60_change_pct"] is not None for r in outputs),
        "with_official_1500_close": 0,
        "archive_fetched_after_event": sum(r["archive_fetched_after_event"] is True for r in outputs),
        "broker_reconciled_executions": None,
        "fees_or_slippage_estimable": False,
        "private_csv_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "private_csv_records": len(outputs),
        "interpretation": "Ex-post path only; no trade, alpha, or causal opportunity-cost claim",
    }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--episodes", type=Path, required=True)
    parser.add_argument("--minute-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.snapshot, args.episodes, args.minute_root, args.output),
                     ensure_ascii=False, indent=2))
