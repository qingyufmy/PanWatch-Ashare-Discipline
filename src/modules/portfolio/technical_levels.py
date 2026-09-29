"""Versioned technical levels from closed, source-timestamped bars only."""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.minute_features import SH, aggregate_bars

LEVEL_VERSION = "levels-v1-candidate"


def _finite_price(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) and number > 0 else None


def _confirmed_swings(bars: pd.DataFrame, timeframe: str) -> list[dict]:
    """A pivot needs two already closed bars on both its left and right."""
    candidates = []
    if len(bars) < 5:
        return candidates
    for index in range(2, len(bars) - 2):
        row = bars.iloc[index]
        left_right = bars.iloc[index - 2:index + 3]
        for kind, column, extreme in (("support", "low", min), ("resistance", "high", max)):
            value = _finite_price(row[column])
            if value is None:
                continue
            neighbors = left_right[column].drop(left_right.index[2])
            strict = value < min(neighbors) if kind == "support" else value > max(neighbors)
            if value != extreme(left_right[column]) or not strict:
                continue
            candidates.append({
                "kind": kind, "value": value, "timeframe": timeframe,
                "method": "confirmed_pivot_2x2", "lookback": 5,
                "available_sample_count": len(bars), "warmup_required": 5,
                "source_bar_ids": [bar_id for ids in left_right.source_bar_ids for bar_id in ids],
                "source_asof": left_right.timestamp.iloc[-1].isoformat(),
                "bar_closed": True,
            })
    return candidates


def derive_level_snapshot(frame: pd.DataFrame, feature: dict, *, prior: dict | None = None,
                          decision_time: datetime | None = None,
                          approved_hard_exit: float | None = None) -> dict:
    if frame.empty or feature["symbol"] != str(frame.symbol.iloc[-1]):
        raise ValueError("feature_frame_mismatch")
    asof = pd.Timestamp(feature["market_data_asof"])
    cutoff = pd.Timestamp(decision_time) if decision_time else asof
    cutoff = cutoff.tz_localize(SH) if cutoff.tzinfo is None else cutoff.tz_convert(SH)
    if asof > cutoff:
        raise ValueError("feature_from_future")
    current = _finite_price(feature.get("latest_close"))
    if current is None:
        raise ValueError("latest_price_invalid")
    candidates = []
    previous = frame[frame.timestamp.dt.date < asof.date()]
    previous_day_high = previous_day_low = previous_day_close = None
    if not previous.empty:
        day = previous.timestamp.dt.date.max()
        day_frame = previous[previous.timestamp.dt.date == day]
        from src.modules.portfolio.minute_features import missing_minutes

        if day_frame.timestamp.max().strftime("%H:%M") == "15:00" and not missing_minutes(day_frame):
            previous_day_high = _finite_price(day_frame.high.max())
            previous_day_low = _finite_price(day_frame.low.min())
            previous_day_close = _finite_price(day_frame.close.iloc[-1])
            for kind, value in (("support", previous_day_low), ("resistance", previous_day_high)):
                if value is not None:
                    candidates.append({"kind": kind, "value": value, "timeframe": "1d",
                                       "method": "previous_complete_trading_day", "lookback": 1,
                                       "available_sample_count": len(day_frame), "warmup_required": 1,
                                       "source_bar_ids": day_frame.source_hash.tolist(),
                                       "source_asof": day_frame.timestamp.iloc[-1].isoformat(),
                                       "source_vendor": str(day_frame.source.iloc[-1]), "bar_closed": True})
    for interval in (15, 60):
        bars = aggregate_bars(frame[frame.timestamp <= cutoff], interval)
        closed = bars[(bars.complete) & (bars.timestamp <= cutoff)]
        candidates.extend(_confirmed_swings(closed, f"{interval}m"))
    supports = sorted((x for x in candidates if x["kind"] == "support" and x["value"] <= current),
                      key=lambda x: (-x["value"], x["timeframe"]))
    resistances = sorted((x for x in candidates if x["kind"] == "resistance" and x["value"] >= current),
                         key=lambda x: (x["value"], x["timeframe"]))
    chosen = {"S1": supports[0] if supports else None,
              "S2": next((x for x in supports[1:] if x["value"] < supports[0]["value"]), None) if supports else None,
              "R1": resistances[0] if resistances else None,
              "R2": next((x for x in resistances[1:] if x["value"] > resistances[0]["value"]), None) if resistances else None}
    source_contract = feature.get("source_contract") or {}
    for key, level in chosen.items():
        if level is None:
            continue
        level.update({
            "level_id": logical_hash({"symbol": feature["symbol"], "kind": key, "value": level["value"],
                                      "bars": level["source_bar_ids"], "version": LEVEL_VERSION}),
            "kind": key, "source_asof": level.get("source_asof", asof.isoformat()),
            "source_vendor": level.get("source_vendor", feature["market_data_source"]),
            "price_basis": source_contract.get("price_basis"),
            "volume_unit": "share", "feature_version": feature.get("feature_version"),
            "level_version": LEVEL_VERSION, "quality_status": "CONFIRMED", "invalidated_at": None,
        })
    prior_levels = (prior or {}).get("levels") or {}
    prior_s1 = _finite_price((prior_levels.get("S1") or {}).get("value"))
    prior_r1 = _finite_price((prior_levels.get("R1") or {}).get("value"))
    broken_support = prior_s1 if prior_s1 is not None and current < prior_s1 else None
    broken_resistance = prior_r1 if prior_r1 is not None and current > prior_r1 else None
    hard_exit = _finite_price(approved_hard_exit)
    return {
        "symbol": feature["symbol"], "trade_date": feature["trade_date"],
        "market_data_asof": asof.isoformat(), "source_hash": feature["source_hash"],
        "level_version": LEVEL_VERSION, "current_price": current,
        "vwap": _finite_price(feature["features"]["1m"].get("vwap")),
        "previous_day_high": previous_day_high, "previous_day_low": previous_day_low,
        "previous_day_close": previous_day_close,
        "levels": chosen, "broken_prior_support": broken_support,
        "broken_prior_resistance": broken_resistance,
        "soft_reduce_trigger": None, "approved_hard_exit": hard_exit,
        "quality_status": "CONFIRMED" if any(chosen.values()) else "INSUFFICIENT_HISTORY",
        "missing_reasons": [] if any(chosen.values()) else ["NO_CONFIRMED_CLOSED_SWING"],
    }
