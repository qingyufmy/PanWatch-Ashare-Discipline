"""Risk directions remain visible in Shadow without becoming trade approval."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.api.journal import list_risk_observations
from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.notifications import dispatch_portfolio_notice
from src.modules.portfolio.risk_observation import (
    _confirmed_support_break, record_shadow_risk_observations,
)
from src.modules.portfolio.signal_journal import record_signal
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    EvidenceSnapshot, PortfolioFeatureSnapshot, PortfolioLevelSnapshot,
    PortfolioNotification, PortfolioRiskObservation,
)


def test_risk_off_direction_is_coalesced_and_cannot_dispatch_shadow_notice():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    at = datetime.now(timezone.utc).replace(microsecond=0)
    day = at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    market = {
        "risk_tone": "RISK_OFF",
        "indices": [{"quality": "FRESH"} for _ in range(3)],
        "holdings": [{"symbol": "600001", "quality": "FRESH"}],
    }
    with factory() as db:
        macro = EvidenceSnapshot(captured_at=at.replace(tzinfo=None), source="portfolio_market_context",
                                 logical_hash="macro", payload=market)
        db.add(macro)
        db.flush()
        signal, _ = record_signal(
            db, market="CN", symbol="600001", action="REDUCE", source="AGENT",
            evidence={"reason": "unverified model direction", "private": "PRIVATE_MODEL_TEXT"},
            ttl_seconds=3600, generated_at=at - timedelta(minutes=2), commit=False,
        )
        signal.status = "REVIEW_REQUIRED"
        db.flush()
        rows = record_shadow_risk_observations(
            db, trade_date=day, phase="MORNING_ADJUST", market_context=market,
            market_evidence_snapshot_id=macro.id, observed_at=at,
        )
        again = record_shadow_risk_observations(
            db, trade_date=day, phase="MORNING_ADJUST", market_context=market,
            market_evidence_snapshot_id=macro.id, observed_at=at,
        )
        assert [r.id for r in rows] == [r.id for r in again]
        assert db.query(PortfolioRiskObservation).count() == 1
        assert db.query(PortfolioNotification).count() == 1
        observation = rows[0]
        notice = db.get(PortfolioNotification, observation.notification_id)
        assert observation.execution_readiness == "NEEDS_CONFIRMATION"
        assert observation.notice_outcome == "SUPPRESSED"
        assert observation.source_signal_ids == [signal.signal_id]
        assert notice.delivery_status == "SUPPRESSED"
        assert notice.suppression_reason == "SHADOW_ONLY"
        assert "PRIVATE_MODEL_TEXT" not in notice.body
        assert "100股" not in notice.body
        assert "不是减仓或清仓指令" in notice.body
        visible = list_risk_observations(trade_date=day, db=db)
        assert len(visible) == 1
        assert visible[0]["notice_reason"] == "SHADOW_ONLY"
        assert visible[0]["execution_readiness"] == "NEEDS_CONFIRMATION"
        notice_id = notice.id
        db.commit()
    result = asyncio.run(dispatch_portfolio_notice(notice_id, db_factory=factory))
    assert result == {"status": "SKIPPED", "reason": "SUPPRESSED"}
    engine.dispose()


def test_confirmed_closed_support_break_creates_advisory_without_sellable_quantity():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    at = datetime.now(timezone.utc).replace(microsecond=0)
    day = at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    quote = {"symbol": "600001", "quality": "FRESH", "price": 10.01,
             "source_asof": at.isoformat()}
    market = {"risk_tone": "RISK_ON", "indices": [{"quality": "FRESH"} for _ in range(4)],
              "holdings": [quote]}
    with factory() as db:
        macro = EvidenceSnapshot(captured_at=at.replace(tzinfo=None), source="portfolio_market_context",
                                 logical_hash=logical_hash(market), payload=market)
        db.add(macro)
        db.flush()
        prior_feature = PortfolioFeatureSnapshot(
            symbol="sh600001", trade_date=day, version="fixture", source_vendor="fixture",
            market_asof=(at - timedelta(minutes=10)).replace(tzinfo=None),
            fetched_at=(at - timedelta(minutes=10)).replace(tzinfo=None),
            source_hash="prior-feature-source", quality_status="OK",
            payload={"source_hash": "prior-feature-source", "missing_minutes": []},
        )
        db.add(prior_feature)
        db.flush()
        previous_payload = {"source_hash": prior_feature.source_hash,
                            "levels": {"S1": {"value": 10.5, "quality_status": "CONFIRMED",
                                              "bar_closed": True, "timeframe": "15m",
                                              "method": "confirmed_pivot_2x2",
                                              "source_asof": (at - timedelta(minutes=15)).isoformat()}}}
        previous = PortfolioLevelSnapshot(
            symbol="sh600001", trade_date=day, version="fixture",
            feature_snapshot_id=prior_feature.id,
            market_asof=(at - timedelta(minutes=10)).replace(tzinfo=None),
            source_hash=logical_hash(previous_payload), quality_status="CONFIRMED",
            payload=previous_payload,
        )
        db.add(previous)
        db.flush()
        feature_payload = {"source_hash": "feature-source", "missing_minutes": []}
        feature = PortfolioFeatureSnapshot(
            symbol="sh600001", trade_date=day, version="fixture", source_vendor="fixture",
            market_asof=(at - timedelta(seconds=30)).replace(tzinfo=None),
            fetched_at=at.replace(tzinfo=None), source_hash="feature-source",
            quality_status="OK", payload=feature_payload,
        )
        db.add(feature)
        db.flush()
        current_payload = {"symbol": "sh600001", "trade_date": day,
                           "source_hash": feature.source_hash, "current_price": 10.0,
                           "market_data_asof": (at - timedelta(seconds=30)).isoformat(),
                           "broken_prior_support": 10.5}
        current = PortfolioLevelSnapshot(
            symbol="sh600001", trade_date=day, version="fixture",
            feature_snapshot_id=feature.id, prior_level_snapshot_id=previous.id,
            market_asof=(at - timedelta(seconds=30)).replace(tzinfo=None),
            source_hash=logical_hash(current_payload), quality_status="INSUFFICIENT_HISTORY",
            payload=current_payload,
        )
        db.add(current)
        db.flush()
        assert _confirmed_support_break(
            db, trade_date=day, symbol="600001",
            quote={**quote, "price": 10.8}, observed=at.replace(tzinfo=None),
        ) is None
        rows = record_shadow_risk_observations(
            db, trade_date=day, phase="MORNING_ADJUST", market_context=market,
            market_evidence_snapshot_id=macro.id, observed_at=at,
        )
        assert len(rows) == 1
        observation = rows[0]
        assert observation.observation_type == "CONFIRMED_SUPPORT_BREAK"
        assert observation.severity == "HIGH"
        assert observation.level_snapshot_id == current.id
        assert observation.source_signal_ids == []
        assert observation.execution_readiness == "NEEDS_CONFIRMATION"
        notice = db.get(PortfolioNotification, observation.notification_id)
        assert "10.50元" in notice.body and "15m" in notice.body
        assert "未批准交易" in notice.body and "股" not in notice.body
        assert notice.delivery_status == "SUPPRESSED"
        db.commit()
    engine.dispose()
