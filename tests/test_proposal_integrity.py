"""Numeric facts and account scope must hold even when model prose is confident."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.platform.persistence.database import Base
from src.modules.portfolio.proposal_integrity import VERSION, SCOPE, price_fact, check_proposal, evidence_errors
from src.modules.portfolio.prompt_registry import seed_fact_prompt, active_prompt, FACT_PROMPT_ID

@pytest.mark.parametrize("price,stop", [(41.78,38.50),(50.80,45.99),(43.75,39.56)])
def test_false_below_claim_blocked_even_with_correct_relation(price, stop):
    p={"action":"EXIT", "decision_basis":"OBSERVATION_PRICE", "stop_relation":"ABOVE", "rationale":"现价低于观察价，退出"}
    errors=check_proposal(p,price_fact(price,stop))
    assert "OBSERVATION_PRICE_NOT_TRIGGERED" in errors
    assert "NARRATIVE_FALSE_BELOW_CLAIM" in errors

@pytest.mark.parametrize("price,relation", [(9.99,"AT_OR_BELOW"),(10,"AT_OR_BELOW"),(10.01,"ABOVE"),(None,"UNKNOWN"),(float("nan"),"UNKNOWN")])
def test_decimal_boundary_and_invalid_inputs(price,relation):
    assert price_fact(price,10)["stop_relation"] == relation

@pytest.mark.parametrize("reason", ["没有跌破观察价，市场转弱", "未跌破观察价，市场转弱", "price not below observation", "尚未跌破观察价"])
def test_negative_claim_does_not_reverse_arithmetic(reason):
    assert check_proposal({"action":"REDUCE", "decision_basis":"MARKET_WEAKNESS", "stop_relation":"ABOVE", "rationale":reason},price_fact(11,10)) == []

@pytest.mark.parametrize("action", ["HOLD","ADD","OPEN"])
def test_touching_observation_cannot_be_hold_or_buy(action):
    assert "TRIGGERED_OBSERVATION_CANNOT_HOLD_OR_ADD" in check_proposal({"action":action,"decision_basis":"NO_CHANGE","stop_relation":"AT_OR_BELOW"},price_fact(10,10))

def test_scope_and_frozen_source_cannot_be_forged():
    p={"decision_contract":VERSION,"account_scope":SCOPE,"decision_fact":price_fact(11,10),
       "model_proposal":{"action":"HOLD","stop_relation":"ABOVE","decision_basis":"NO_CHANGE","rationale":"模拟盘已清仓，继续持有"}}
    assert "PAPER_STATE_USED_FOR_DECLARED_ADVICE" in evidence_errors(p,action="HOLD")
    assert "FROZEN_FACT_SOURCE_MISMATCH" in evidence_errors(p,action="HOLD",price=9,stop=10)
    assert evidence_errors({},action="EXIT") == ["DECLARED_FACT_CONTRACT_REQUIRED"]

def test_new_prompt_is_immutable_candidate_and_keeps_daily_review_separate():
    engine=create_engine("sqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as db:
        row=seed_fact_prompt(db)
        digest=row.prompt_hash
        assert row.status == "CANDIDATE" and row.prompt_id != "review"
        assert seed_fact_prompt(db).id == row.id
        schema=row.output_schema["$defs"]["ActionProposal"]
        assert {"decision_basis","stop_relation"} <= set(schema["required"])
        row.status="ACTIVE";db.commit()
        assert active_prompt(db,FACT_PROMPT_ID).prompt_hash == digest
    engine.dispose()
