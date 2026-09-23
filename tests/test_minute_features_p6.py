"""P6 session boundaries, source provenance, archive and 20-symbol budget."""

import time
from datetime import datetime

import pandas as pd
import pytest

from src.modules.portfolio.minute_features import (
    aggregate_bars, feature_snapshot, fetch_minute_bars, normalize_bars,
    read_parquet, write_parquet,
)


def _fixture(symbol="sh600001", day="2026-09-22"):
    stamps = list(pd.date_range(f"{day} 09:31", f"{day} 11:30", freq="min"))
    stamps += list(pd.date_range(f"{day} 13:01", f"{day} 14:57", freq="min"))
    stamps += [pd.Timestamp(f"{day} 15:00")]
    return pd.DataFrame([{"symbol": symbol, "timestamp": stamp, "open": 10.0 + i / 1000,
                          "high": 10.2 + i / 1000, "low": 9.8 + i / 1000,
                          "close": 10.1 + i / 1000, "volume": 1000,
                          "amount": 10100, "source": "fixture"} for i, stamp in enumerate(stamps)])


def test_lunch_is_not_gap_and_no_cross_session_aggregation(tmp_path):
    frame = normalize_bars(_fixture(), "2026-09-22")
    snapshot = feature_snapshot(frame)
    assert len(frame) == 238 and snapshot["missing_minutes"] == []
    bars = aggregate_bars(frame, 120)
    assert list(bars.session) == ["AM", "PM"]
    assert list(bars["count"]) == [120, 118]
    assert bars.complete.tolist() == [True, False]
    path = write_parquet(frame, tmp_path)
    assert len(read_parquet(path)) == 238


def test_duplicate_invalid_and_cross_day_are_excluded():
    raw = _fixture()
    raw.loc[2, "high"] = 8
    duplicate = raw.iloc[[0]].copy()
    wrong_day = raw.iloc[[1]].copy()
    wrong_day["timestamp"] = datetime(2026, 9, 23, 9, 32)
    result = normalize_bars(pd.concat([raw, duplicate, wrong_day]), "2026-09-22")
    assert len(result) == 237
    assert len(feature_snapshot(result)["missing_minutes"]) == 1


def test_fallback_records_source_failure():
    def fake(symbol, source, day):
        if source == "sina":
            raise TimeoutError("fixture")
        return normalize_bars(_fixture(symbol, day), day)

    frame, failures = fetch_minute_bars("sh600001", "2026-09-22", fetcher=fake)
    assert len(frame) == 238
    assert failures == [{"source": "sina", "error": "TimeoutError", "detail": "fixture"}]


def test_twenty_symbol_feature_budget():
    start = time.monotonic()
    for i in range(20):
        frame = normalize_bars(_fixture(symbol=f"sh{600000+i}"), "2026-09-22")
        assert feature_snapshot(frame)["bar_count"] == 238
    assert time.monotonic() - start < 30
