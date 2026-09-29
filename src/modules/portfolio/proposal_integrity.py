"""Deterministic price facts and account scope for portfolio proposals."""
from decimal import Decimal, InvalidOperation
import re

VERSION = "declared-facts-v1"
SCOPE = "USER_DECLARED"


def price_fact(price, stop):
    try:
        price, stop = Decimal(str(price)), Decimal(str(stop))
        if not price.is_finite() or not stop.is_finite() or price <= 0 or stop <= 0:
            raise ValueError("invalid_price")
    except (InvalidOperation, ValueError, TypeError):
        return {"price": None, "current_stop": None, "stop_relation": "UNKNOWN"}
    return {"price": float(price), "current_stop": float(stop),
            "stop_relation": "AT_OR_BELOW" if price <= stop else "ABOVE"}


def check_proposal(proposal: dict, fact: dict) -> list[str]:
    """Validate structured claims and contradictory narrative. Never infer a trade."""
    errors = []
    actual = price_fact(fact.get("price"), fact.get("current_stop"))["stop_relation"]
    if fact.get("stop_relation") != actual or proposal.get("stop_relation") != actual:
        errors.append("STOP_RELATION_MISMATCH")
    basis = proposal.get("decision_basis")
    if basis not in {"OBSERVATION_PRICE", "MARKET_WEAKNESS", "OTHER", "NO_CHANGE"}:
        errors.append("DECISION_BASIS_MISSING")
    if basis == "OBSERVATION_PRICE" and actual != "AT_OR_BELOW":
        errors.append("OBSERVATION_PRICE_NOT_TRIGGERED")
    if actual == "AT_OR_BELOW" and proposal.get("action") in {"HOLD", "ADD", "OPEN"}:
        errors.append("TRIGGERED_OBSERVATION_CANNOT_HOLD_OR_ADD")
    text = str(proposal.get("rationale") or "")
    # The model must use the supplied fact for price comparisons. These checks
    # also quarantine legacy-style prose even when its structured claim is true.
    if actual == "ABOVE" and re.search(r"(?<!未)(?<!不)(?<!没)(?<!没有)(?<!尚未)(?<!如果)(?<!若)跌破|(?<!未)(?<!不)(?<!没有)(?<!若)低于|(?<!not )below|(?<!not )breached", text, re.I):
        errors.append("NARRATIVE_FALSE_BELOW_CLAIM")
    if actual == "AT_OR_BELOW" and re.search(r"(?<!未)(?<!不)高于|(?<!not )above", text, re.I):
        errors.append("NARRATIVE_FALSE_ABOVE_CLAIM")
    if re.search(r"(?:模拟|paper).{0,25}(?:无|没有|已清|清仓|empty|no position)", text, re.I):
        errors.append("PAPER_STATE_USED_FOR_DECLARED_ADVICE")
    return errors


def evidence_errors(payload: dict, *, action: str, price=None, stop=None) -> list[str]:
    if payload.get("decision_contract") != VERSION or payload.get("account_scope") != SCOPE:
        return ["DECLARED_FACT_CONTRACT_REQUIRED"]
    proposal = payload.get("model_proposal") or {}
    fact = payload.get("decision_fact") or {}
    errors = check_proposal(proposal, fact)
    if proposal.get("action") != action:
        errors.append("PROPOSAL_ACTION_MISMATCH")
    if price is not None or stop is not None:
        if fact != price_fact(price, stop):
            errors.append("FROZEN_FACT_SOURCE_MISMATCH")
    return errors
