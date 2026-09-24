"""Current advice keeps one fresh action, while superseded rows remain in history."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.api.workflow import current_advice, decisions
from src.platform.persistence.database import Base
from src.platform.persistence.models import PortfolioDecision, PortfolioWorkflowRun


def test_advice_expiry_risk_priority_and_symbol_history():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with factory() as db:
        for revision, action, current, expires in (
            (1, "ADD", False, now - timedelta(minutes=1)),
            (2, "HOLD", True, now + timedelta(minutes=10)),
        ):
            db.add(PortfolioDecision(
                trade_date=day, market="CN", symbol="600001", revision=revision,
                signal_id=f"test-{revision}", action=action, approved_qty=None,
                decision_status="REVIEW_REQUIRED", risk_status="UNVERIFIED",
                data_status="UNVERIFIED", execution_status="NOT_EXECUTED",
                semantic_hash=f"hash-{revision}", current=current,
                created_at=now - timedelta(minutes=3 - revision), expires_at=expires,
            ))
        db.commit()
        assert current_advice(db=db)["items"]["600001"]["action"] == "HOLD"
        history = decisions(symbol="600001", current_only=False, db=db)
        assert [row["action"] for row in history] == ["HOLD", "ADD"]
        assert all(row["created_at"].endswith("+00:00") for row in history)

        risk = PortfolioWorkflowRun(
            run_id="risk-test", trade_date=day, step="HARD_RISK", slot="10:00",
            status="SUCCEEDED", started_at=now - timedelta(seconds=20),
            finished_at=now - timedelta(seconds=10),
            payload={"positions": [{"symbol": "600001", "color": "RED", "reason": "HARD_STOP"}]},
        )
        db.add(risk)
        db.commit()
        assert current_advice(db=db)["items"]["600001"]["action"] == "RISK_REVIEW"
        risk.payload = {"positions": [{"symbol": "600001", "color": "GREEN", "reason": "ABOVE_STOP"}]}
        db.commit()
        assert current_advice(db=db)["items"]["600001"]["action"] == "HOLD"

        row = db.query(PortfolioDecision).filter_by(current=True).one()
        row.expires_at = now - timedelta(seconds=1)
        db.commit()
        assert "600001" not in current_advice(db=db)["items"]
    engine.dispose()
