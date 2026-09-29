"""Public archive may expose hashes and counts, never private message or channel."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    ModelRun, NotifyChannel, PaperPortfolioFill, PaperPortfolioNav,
    PortfolioNotification, PortfolioWorkflowRun,
)


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
        db.add(PortfolioWorkflowRun(run_id="fixture-review", trade_date="2026-09-23",
                                    step="DAILY_REVIEW", slot="DAILY", status="REVIEW",
                                    payload={"model_run_id": "fixture-review-model"},
                                    started_at=datetime(2026, 9, 23, 7, 30)))
        db.add(ModelRun(
            run_id="fixture-model-run", trace_id="fixture-trace", role="FAST",
            profile_role="FAST", requested_model="fixture", input_hash="fixture-input",
            output_hash="fixture-output", output_text="PRIVATE_MODEL_RESPONSE",
            latency_ms=1, status="SUCCEEDED", started_at=datetime(2026, 9, 23, 7, 4),
            finished_at=datetime(2026, 9, 23, 7, 5),
        ))
        db.add(ModelRun(
            run_id="fixture-review-model", trace_id="fixture-review-trace", role="DEEP",
            profile_role="DEEP", requested_model="fixture", input_hash="fixture-review-input",
            output_hash="fixture-review-output", output_text="PRIVATE_REVIEW_RESPONSE",
            latency_ms=1, status="OK", schema_valid=True,
            started_at=datetime(2026, 9, 23, 7, 29),
            finished_at=datetime(2026, 9, 23, 7, 30),
        ))
        db.add(PortfolioNotification(
            id="fixture-notice", semantic_key="fixture", trade_date="2026-09-23",
            symbol="600001", signal_ids=[], channel_id=channel.id,
            template_version="fixture", title="PRIVATE_TITLE", body="PRIVATE_BODY_300_SHARES",
            rendered_content_hash="fixturehash", priority="NORMAL", reason="PLAN",
            queued_at=datetime(2026, 9, 23, 7, 0), delivery_status="SENT_ACCEPTED",
            ack_status="NOT_OBSERVED", retry_count=0,
        ))
        db.add_all([
            PaperPortfolioNav(trade_date="2026-09-22", cash=12345.67, market_value=87654.33,
                              equity=100000, source_asof_min=datetime(2026, 9, 22, 7, 0),
                              captured_at=datetime(2026, 9, 22, 7, 45), position_count=1,
                              source="tencent_close_quote"),
            PaperPortfolioNav(trade_date="2026-09-23", cash=12345.67, market_value=88654.33,
                              equity=101000, source_asof_min=datetime(2026, 9, 23, 7, 0),
                              captured_at=datetime(2026, 9, 23, 7, 45), position_count=1,
                              source="tencent_close_quote"),
            PaperPortfolioFill(signal_id="fixture-paper-signal", trade_date="2026-09-23",
                               symbol="600001", action="REDUCE", quantity=200,
                               price=50.25, fees=12.34, cash_delta=10037.66,
                               quote_asof=datetime(2026, 9, 23, 2, 0),
                               filled_at=datetime(2026, 9, 23, 2, 0),
                               policy_scope="PAPER_ONLY",
                               details={"private": "PRIVATE_PAPER_DETAILS"}),
        ])
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
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["daily_review_complete"] is True
    analysis = json.loads((directory / "intraday_analysis.json").read_text(encoding="utf-8"))
    assert [row["step"] for row in analysis] == ["DAILY_REVIEW"]
    assert analysis[0]["model_run_id"] == "fixture-review-model"
    assert summary[0]["delivery_status"] == "SENT_ACCEPTED"
    assert summary[0]["content_hash"] == "fixturehash"
    paper_summary = json.loads((directory / "paper_summary.json").read_text(encoding="utf-8"))
    paper_fills = json.loads((directory / "paper_fills.json").read_text(encoding="utf-8"))
    assert paper_summary["status"] == "CAPTURED"
    assert paper_summary["daily_return_pct"] == 1.0
    assert paper_summary["fill_count"] == 1
    assert set(paper_fills[0]) == {"signal_id", "symbol", "action", "filled_at_utc", "quote_asof_utc", "policy_scope"}
    entire_archive = "\n".join(p.read_text(encoding="utf-8") for p in directory.glob("*.json"))
    for private in ("PRIVATE_WEBHOOK_SECRET", "PRIVATE_TITLE", "PRIVATE_BODY_300_SHARES",
                    "PRIVATE_MODEL_RESPONSE", "PRIVATE_REVIEW_RESPONSE", "PRIVATE_PAPER_DETAILS",
                    "12345.67", "10037.66"):
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
