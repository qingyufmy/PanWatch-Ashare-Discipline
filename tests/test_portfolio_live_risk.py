from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio import daily_workflow
from src.platform.persistence.database import Base
from src.platform.persistence.models import PortfolioTruthPosition, PortfolioTruthSnapshot, PositionPlan


def test_tencent_quote_carries_vendor_timestamp(monkeypatch):
    from marketdata.vendors import tencent
    parts = [""] * 50
    parts[1], parts[2], parts[3] = "fixture", "688146", "19.00"
    parts[30] = "20260923093115"
    monkeypatch.setattr(tencent, "_fetch_lines", lambda symbols: ['v_sh688146="' + "~".join(parts) + '"'])
    assert tencent.fetch_raw(["sh688146"])[0]["source_asof"] == "20260923093115"


def test_live_quote_stop_requires_fresh_second_source(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_workflow, "MINUTE_ROOT", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        truth = PortfolioTruthSnapshot(trade_date="2026-09-23", phase="PREMARKET", source="fixture",
                                       fetched_at=datetime(2026, 9, 23, 1), source_asof=datetime(2026, 9, 23, 1),
                                       freshness="FRESH", truth_status="REVIEW_ONLY", logical_hash="x",
                                       anomaly_flags=[], account_details=[])
        db.add(truth)
        db.flush()
        db.add(PortfolioTruthPosition(snapshot_id=truth.id, market="CN", symbol="688146", name="fixture",
                                      total_qty=300, sellable_qty=300, today_locked_qty=0,
                                      avg_cost=30, account_details=[]))
        db.add(PositionPlan(market="CN", symbol="688146", version=1, position_state="WATCH",
                            thesis_state="UNKNOWN", plan={"risk": {"current_stop": 20}}, logical_hash="p"))
        db.commit()
    asof = datetime(2026, 9, 23, 9, 31, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    quote = lambda symbols: [{"symbol": "688146", "current_price": 19.0,
                              "source_asof": "20260923093115"}]

    def minute_at(price):
        return lambda *args, **kwargs: (pd.DataFrame({"timestamp": [pd.Timestamp(
            "2026-09-23 09:31:00", tz="Asia/Shanghai")], "close": [price]}), [])

    confirmed = daily_workflow._risk_scan("2026-09-23", factory, asof=asof,
                                           quote_fetcher=quote, minute_fetcher=minute_at(19.01))
    disputed = daily_workflow._risk_scan("2026-09-23", factory, asof=asof,
                                          quote_fetcher=quote, minute_fetcher=minute_at(22.0))
    assert confirmed["positions"][0]["color"] == "RED"
    assert disputed["positions"][0]["color"] == "YELLOW"
    engine.dispose()
