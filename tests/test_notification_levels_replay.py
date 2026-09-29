"""Frozen synthetic cases supplied with the 2026-09-23 remediation request."""

import math

import pandas as pd
import pytest

from src.modules.portfolio.minute_features import aggregate_bars, feature_snapshot, normalize_bars
from src.modules.portfolio.technical_levels import derive_level_snapshot


def _rows(times, *, source="fixture", amount=1010.0, volume=100.0):
    return pd.DataFrame([{
        "symbol": "sh600001", "timestamp": f"2026-09-23 {minute}",
        "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1,
        "volume": volume, "amount": amount, "source": source,
    } for minute in times])


def test_gap_does_not_shift_fixed_five_minute_windows():
    frame = normalize_bars(_rows(["09:31", "09:32", "09:34", "09:35", "09:36"]), "2026-09-23")
    bars = aggregate_bars(frame, 5)
    assert bars["count"].tolist() == [4, 1]
    assert bars["complete"].tolist() == [False, False]
    assert bars.timestamp.iloc[0].strftime("%H:%M") == "09:35"
    assert bars.timestamp.iloc[1].strftime("%H:%M") == "09:40"
    assert any("09:33" in item for item in bars.missing_minutes.iloc[0])


def test_ma20_requires_twenty_closed_bars_not_two_120m_bars():
    minutes = pd.date_range("2026-09-23 09:31", "2026-09-23 11:30", freq="min")
    minutes = list(minutes) + list(pd.date_range("2026-09-23 13:01", "2026-09-23 15:00", freq="min"))
    frame = normalize_bars(_rows([t.strftime("%H:%M") for t in minutes]), "2026-09-23")
    result = feature_snapshot(frame)
    assert result["features"]["120m"]["ma20"] is None
    assert result["quality"]["120m"]["ma20"] == "INSUFFICIENT_HISTORY"


def test_eastmoney_lots_normalize_once_for_vwap():
    raw = _rows(["09:31"], source="eastmoney", amount=13_471_486.0, volume=12_967)
    raw.loc[0, ["open", "high", "low", "close"]] = [10.38, 10.40, 10.38, 10.40]
    frame = normalize_bars(raw, "2026-09-23")
    assert frame.volume.iloc[0] == 1_296_700
    assert frame.raw_volume.iloc[0] == 12_967
    assert math.isclose(feature_snapshot(frame)["features"]["1m"]["vwap"], 10.389054, abs_tol=1e-6)
    sina = raw.copy()
    sina["source"] = "sina"
    sina["volume"] = 1_296_700
    assert normalize_bars(sina, "2026-09-23").volume.iloc[0] == 1_296_700


def test_current_bar_cannot_erase_prior_support():
    times = [t.strftime("%H:%M") for t in pd.date_range("2026-09-23 09:31", periods=22, freq="min")]
    raw = _rows(times)
    raw.loc[:, ["open", "high", "low", "close"]] = [20.5, 20.6, 20.3, 20.4]
    raw.loc[:, "amount"] = 2040.0
    raw.loc[21, ["open", "high", "low", "close", "amount"]] = [20.30, 20.30, 20.10, 20.18, 2018.0]
    frame = normalize_bars(raw, "2026-09-23")
    assert feature_snapshot(frame)["features"]["1m"]["support_20"] == 20.3
    level = derive_level_snapshot(frame, feature_snapshot(frame),
                                  prior={"levels": {"S1": {"value": 20.3}}})
    assert level["broken_prior_support"] == 20.3
    assert level["levels"]["S1"] is None


def test_archived_prior_sessions_warm_up_120m_ma20_without_future_prices():
    days = pd.bdate_range("2026-08-20", periods=21)
    history = []
    for day in days[:20]:
        stamps = pd.date_range(f"{day.date()} 09:31", periods=120, freq="min")
        raw = _rows([stamp.strftime("%H:%M") for stamp in stamps])
        raw["timestamp"] = stamps
        history.append(normalize_bars(raw, day.date().isoformat()))
    current_day = days[20].date().isoformat()
    stamps = pd.date_range(f"{current_day} 09:31", periods=120, freq="min")
    today = _rows([stamp.strftime("%H:%M") for stamp in stamps])
    today["timestamp"] = stamps
    frame = normalize_bars(today, current_day)
    prior = pd.concat(history, ignore_index=True)
    result = feature_snapshot(frame, history=prior)
    assert result["quality"]["120m"]["ma20"] == "OK"
    assert result["features"]["120m"]["ma20"] == 10.1
    assert result["features"]["120m"]["vwap"] == 10.1
    with pytest.raises(ValueError, match="future_or_mixed_history"):
        feature_snapshot(frame, history=pd.concat([prior, frame], ignore_index=True))
