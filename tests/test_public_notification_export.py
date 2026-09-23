"""Public archive may expose hashes and counts, never private message or channel."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.persistence.database import Base
from src.platform.persistence.models import ModelRun, NotifyChannel, PortfolioNotification, PortfolioWorkflowRun


def test_public_notification_summary_is_allowlisted(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        channel = NotifyChannel(name="private", type="lark", config={"webhook": "PRIVATE_WEBHOOK_SECRET"},
                                enabled=True, is_default=True)
        db.add(channel)
        db.flush()
        db.add(PortfolioWorkflowRun(run_id="fixture-run", trade_date="2026-09-23",
                                    step="EOD_TRUTH", slot="DAILY", status="REVIEW",
                                    payload={"model_run_id": "fixture-model-run"},
                                    started_at=datetime(2026, 9, 23, 7, 5)))
        db.add(ModelRun(
            run_id="fixture-model-run", trace_id="fixture-trace", role="FAST",
            profile_role="FAST", requested_model="fixture", input_hash="fixture-input",
            output_hash="fixture-output", output_text="PRIVATE_MODEL_RESPONSE",
            latency_ms=1, status="SUCCEEDED", started_at=datetime(2026, 9, 23, 7, 4),
            finished_at=datetime(2026, 9, 23, 7, 5),
        ))
        db.add(PortfolioNotification(
            id="fixture-notice", semantic_key="fixture", trade_date="2026-09-23",
            symbol="600001", signal_ids=[], channel_id=channel.id,
            template_version="fixture", title="PRIVATE_TITLE", body="PRIVATE_BODY_300_SHARES",
            rendered_content_hash="fixturehash", priority="NORMAL", reason="PLAN",
            queued_at=datetime(2026, 9, 23, 7, 0), delivery_status="SENT_ACCEPTED",
            ack_status="NOT_OBSERVED", retry_count=0,
        ))
        db.commit()
    script = Path(__file__).resolve().parents[1] / "scripts" / "export-public-daily.py"
    spec = importlib.util.spec_from_file_location("public_daily_fixture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SessionLocal = factory
    module._market_coverage = lambda _day: []
    module.export_day("2026-09-23", tmp_path)
    directory = tmp_path / "2026-09-23"
    summary = json.loads((directory / "notification_summary.json").read_text(encoding="utf-8"))
    assert summary[0]["delivery_status"] == "SENT_ACCEPTED"
    assert summary[0]["content_hash"] == "fixturehash"
    entire_archive = "\n".join(p.read_text(encoding="utf-8") for p in directory.glob("*.json"))
    for private in ("PRIVATE_WEBHOOK_SECRET", "PRIVATE_TITLE", "PRIVATE_BODY_300_SHARES",
                    "PRIVATE_MODEL_RESPONSE"):
        assert private not in entire_archive

    private_script = Path(__file__).resolve().parents[1] / "scripts" / "export-notification-review.py"
    private_spec = importlib.util.spec_from_file_location("private_review_fixture", private_script)
    private_module = importlib.util.module_from_spec(private_spec)
    private_spec.loader.exec_module(private_module)
    private_module.SessionLocal = factory
    private_module.export_review("2026-09-23", tmp_path / "private")
    private_packet = json.loads((tmp_path / "private" / "2026-09-23" / "review.json").read_text(encoding="utf-8"))
    assert private_packet["model_runs"][0]["output_text"] == "PRIVATE_MODEL_RESPONSE"
    assert private_packet["notifications"][0]["body"] == "PRIVATE_BODY_300_SHARES"
    engine.dispose()
