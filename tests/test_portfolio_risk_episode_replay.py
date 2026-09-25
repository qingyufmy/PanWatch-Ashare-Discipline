"""Post-event bars are outcome samples, not inputs available at the signal."""

import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def test_replay_uses_first_strictly_later_closed_bar_and_keeps_missing_horizons_null():
    script = Path(__file__).resolve().parents[1] / "scripts" / "replay-portfolio-risk-episodes-20260924.py"
    spec = importlib.util.spec_from_file_location("portfolio_risk_episode_replay", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sh = ZoneInfo("Asia/Shanghai")
    frame = pd.DataFrame({
        "timestamp": [datetime(2026, 9, 24, 9, minute, tzinfo=sh) for minute in (31, 32, 33)],
        "close": [11.0, 10.0, 9.0], "low": [10.9, 9.8, 8.7], "high": [11.1, 10.2, 9.2],
    })
    event = datetime(2026, 9, 24, 9, 31, 20, tzinfo=sh)
    outcome = module.episode_path(frame, event)
    assert outcome["first_future_bar"] == "2026-09-24T09:32:00+08:00"
    assert outcome["first_future_close"] == 10.0
    assert outcome["future_bar_count"] == 2
    assert outcome["bar_5_change_pct"] is None
    assert outcome["official_1500_close"] is None
    assert outcome["downside_excursion_pct"] == 13.0
