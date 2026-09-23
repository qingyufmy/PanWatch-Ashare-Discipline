"""P4: role routing stays bounded and records failed/fallback attempts."""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.model_router import run_role, seed_default_profiles
from src.platform.persistence.database import Base
from src.platform.persistence.models import AIModel, AIService, ModelProfile, ModelRun


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        service = AIService(name="fixture", base_url="http://fixture", api_key="secret")
        db.add(service)
        db.flush()
        db.add(AIModel(name="primary", service_id=service.id, model="primary", is_default=True))
        db.add(AIModel(name="backup", service_id=service.id, model="backup"))
        db.commit()
        assert seed_default_profiles(db) == 2
        assert seed_default_profiles(db) == 0
        backup = db.query(AIModel).filter_by(model="backup").one()
        db.add(ModelProfile(role="FAST_BACKUP", ai_model_id=backup.id, thinking="default",
                            max_tokens=300, timeout_seconds=2, retries=0, enabled=True))
        db.commit()
    yield factory
    engine.dispose()


def test_primary_failure_falls_back_and_journals_both_attempts(sessions):
    class Client:
        def __init__(self, base_url, api_key, model):
            self.model = model
            self.last_reported_model = model + "-reported"

        async def chat_multi(self, messages, **kwargs):
            if self.model == "primary":
                raise RuntimeError("provider unavailable")
            return '{"ok":true}'

    with sessions() as db:
        db.get(ModelProfile, "FAST").retries = 0
        db.commit()
    result = asyncio.run(run_role("FAST", "sys", "user", db_factory=sessions, client_factory=Client,
                                  schema_validator=lambda raw: raw == '{"ok":true}'))
    assert result.degraded and result.profile_role == "FAST_BACKUP"
    assert result.reported_model == "backup-reported"
    with sessions() as db:
        runs = db.query(ModelRun).order_by(ModelRun.started_at).all()
        assert [r.status for r in runs] == ["FAILED", "MODEL_DEGRADED"]
        assert all(r.trace_id == runs[0].trace_id for r in runs)
        assert runs[1].schema_valid is True


def test_invalid_schema_never_becomes_actionable_or_silent_fallback(sessions):
    class Client:
        def __init__(self, base_url, api_key, model):
            self.model = model

        async def chat_multi(self, messages, **kwargs):
            return "not json"

    with pytest.raises(ValueError, match="model_schema_invalid"):
        asyncio.run(run_role("FAST", "sys", "user", db_factory=sessions, client_factory=Client,
                             schema_validator=lambda _: False))
    with sessions() as db:
        runs = db.query(ModelRun).all()
        assert len(runs) == 1
        assert runs[0].status == "FAILED" and runs[0].schema_valid is False


def test_timeout_records_failure(sessions):
    class Client:
        def __init__(self, base_url, api_key, model):
            self.model = model

        async def chat_multi(self, messages, **kwargs):
            await asyncio.sleep(0.03)
            return "late"

    with sessions() as db:
        profile = db.get(ModelProfile, "DEEP")
        profile.timeout_seconds = 0.001
        profile.retries = 0
        db.commit()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_role("DEEP", "sys", "user", db_factory=sessions, client_factory=Client))
    with sessions() as db:
        assert db.query(ModelRun).one().status == "FAILED"
