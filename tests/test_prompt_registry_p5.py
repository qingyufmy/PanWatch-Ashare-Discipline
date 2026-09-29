"""P5 prompt immutability and all-holdings strict output contract."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.portfolio.prompt_registry import (
    ActionProposal, active_prompt, parse_portfolio_plan, seed_prompts,
    seed_notification_candidate_prompts,
)
from src.platform.persistence.database import Base


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_seed_is_idempotent_and_hash_tamper_fails_closed():
    db = _db()
    assert seed_prompts(db) == 3
    assert seed_prompts(db) == 0
    assert seed_notification_candidate_prompts(db) == 3
    assert seed_notification_candidate_prompts(db) == 0
    row = active_prompt(db, "flash")
    assert row.version == "1.0.3"
    row.system_template += " unauthorized"
    db.commit()
    with pytest.raises(ValueError, match="prompt_hash_mismatch"):
        active_prompt(db, "flash")
    db.close()


def test_strict_schema_rejects_extra_duplicate_missing_and_quantity_error():
    item = {"market": "CN", "symbol": "sh600001", "action": "HOLD",
            "confidence": 0.7, "rationale": "Insufficient evidence",
            "evidence_refs": ["quote:1"]}
    payload = {"trade_date": "2026-09-23", "proposals": [item],
               "portfolio_rationale": "Review all holdings"}
    parsed = parse_portfolio_plan(json.dumps(payload), {"600001"}, "2026-09-23")
    assert parsed.proposals[0].action == "HOLD" and parsed.proposals[0].symbol == "600001"
    with pytest.raises(ValueError, match="plan_symbols_mismatch"):
        parse_portfolio_plan(json.dumps(payload), {"600001", "000001"}, "2026-09-23")
    with pytest.raises(ValueError, match="duplicate_symbol"):
        parse_portfolio_plan(json.dumps({**payload, "proposals": [item, item]}), {"600001"}, "2026-09-23")
    with pytest.raises(ValueError):
        ActionProposal.model_validate({**item, "order_placed": True})
    with pytest.raises(ValueError, match="qty_hint_required_for_change"):
        ActionProposal.model_validate({**item, "action": "EXIT"})
