from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.api import accounts
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, PortfolioTruthSnapshot


def test_declared_nav_is_separate_from_available_cash(monkeypatch):
    monkeypatch.setattr(accounts, "get_hkd_cny_rate", lambda: 1.0)
    monkeypatch.setattr(accounts, "get_usd_cny_rate", lambda: 1.0)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Account(name="fixture", available_funds=0, enabled=True))
        db.add(PortfolioTruthSnapshot(trade_date="2026-09-22", phase="MANUAL", source="user_attested",
                                      fetched_at=datetime(2026, 9, 22), freshness="FRESH",
                                      truth_status="REVIEW_ONLY", logical_hash="x", anomaly_flags=[],
                                      account_details=[], cash=0, nav=500000))
        db.commit()
        response = accounts.get_portfolio_summary(account_id=None, include_quotes=False, db=db)
        assert response["total"]["declared_nav"] == 500000
        assert response["total"]["declared_nav_status"] == "REVIEW_ONLY"
        assert response["total"]["available_funds"] == 0
        assert response["total"]["total_assets"] == 0
    engine.dispose()
