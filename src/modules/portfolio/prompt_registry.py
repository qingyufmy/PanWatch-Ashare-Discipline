"""P5 immutable prompt versions and strict structured portfolio output."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.model_router import ModelResult, run_role
from src.platform.persistence.models import PromptVersion


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    market: Literal["CN"]
    symbol: str = Field(pattern=r"^\d{6}$")
    action: Literal["OPEN", "ADD", "HOLD", "REDUCE", "EXIT"]
    qty_hint: int | None = Field(default=None, ge=1)
    target_weight: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=3, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_cn_symbol(cls, value):
        if isinstance(value, str) and len(value) == 8 and value[:2] in {"sh", "sz", "bj"} and value[2:].isdigit():
            return value[2:]
        return value

    @model_validator(mode="after")
    def quantity_for_change(self):
        if self.action in {"OPEN", "ADD", "REDUCE", "EXIT"} and self.qty_hint is None:
            raise ValueError("qty_hint_required_for_change")
        if self.action == "HOLD" and self.qty_hint is not None:
            raise ValueError("hold_must_not_have_quantity")
        return self


class PortfolioActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    proposals: list[ActionProposal] = Field(min_length=1, max_length=50)
    portfolio_rationale: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def unique_symbols(self):
        keys = [(row.market, row.symbol) for row in self.proposals]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_symbol")
        return self


PROMPTS = {
    "flash": ("FAST", "Return one JSON PortfolioActionPlan for the supplied holdings. "
              "Cover each holding exactly once. First assess the source-labelled CN indices, "
              "candidate-sample breadth, and global technology observations. Describe their "
              "agreement or disagreement and a bounded whole-portfolio exposure in "
              "portfolio_rationale. Give each holding a target_weight as a portfolio weight "
              "when supported by evidence and explain ADD more carefully than HOLD. "
              "Treat missing timestamps, stale quotes, sample breadth and unverified global "
              "technology as uncertainty, never as confirmation. Auction and intraday data "
              "must confirm directional proposals before a paper fill. Use HOLD when evidence "
              "is insufficient. Do not invent prices, fills, broker cash or sellable quantity. "
              "Never claim an order was placed. No prose outside JSON."),
    "deep": ("DEEP", "Review the portfolio evidence and return one JSON PortfolioActionPlan. "
             "Cover every holding exactly once; cite evidence_refs for each proposal. "
             "Treat stale or missing inputs as HOLD. Never claim execution. No prose outside JSON."),
    "review": ("FAST", "Review the frozen whole-portfolio evidence and current index, sector "
               "and holding quotes. Return one JSON PortfolioActionPlan with every holding "
               "exactly once. Set target_weight for supported position changes and provide "
               "explicit qty_hint. ADD requires fresh stock and index confirmation, adequate "
               "cash and a risk limit; otherwise HOLD. An intraday round trip needs separate "
               "sell and later buy signals and cannot sell shares bought today. Treat missing "
               "or timeless evidence as uncertain. Never claim execution. No prose outside JSON."),
}
PROMPT_VERSION = "1.0.3"
NOTIFICATION_CANDIDATE_VERSION = "1.1.0-notification-candidate"


def seed_notification_candidate_prompts(db: Session) -> int:
    """Store the stricter contract for replay; never activate it at startup."""
    created = 0
    output_schema = {
        "type": "object", "required": ["trade_date", "proposals"],
        "properties": {
            "trade_date": {"type": "string"},
            "proposals": {"type": "array", "items": {"type": "object", "required": [
                "market", "symbol", "action", "decision_status", "fact_refs", "invalidation_refs",
            ], "properties": {
                "market": {"const": "CN"}, "symbol": {"type": "string"},
                "action": {"enum": ["ADD", "REDUCE", "HOLD", "EXIT", None]},
                "decision_status": {"enum": ["PROPOSED", "DATA_UNKNOWN", "MODEL_FAILED"]},
                "fact_refs": {"type": "array", "items": {"type": "string"}},
                "invalidation_refs": {"type": "array", "items": {"type": "string"}},
                "escalate": {"type": "boolean"},
            }}},
        },
    }
    for prompt_id in ("flash", "deep", "review"):
        if db.query(PromptVersion).filter_by(prompt_id=prompt_id,
                                              version=NOTIFICATION_CANDIDATE_VERSION).first():
            continue
        role = "DEEP" if prompt_id == "deep" else "FAST"
        template = (
            "Use only frozen evidence references supplied in the context. "
            "Return one JSON proposal per held CN position. Missing data must use action=null "
            "and decision_status=DATA_UNKNOWN, never HOLD. Do not invent prices, shares, "
            "executions or broker confirmation. Cite at most three fact_refs. "
            "A proposed action is not an approved or executed trade."
        )
        db.add(PromptVersion(
            prompt_id=prompt_id, version=NOTIFICATION_CANDIDATE_VERSION,
            system_template=template,
            input_schema={"type": "object", "required": ["trade_date", "positions", "feature_snapshot_refs"]},
            output_schema=output_schema, model_role=role,
            change_reason="Candidate fact-reference and unknown-state contract; replay before activation",
            parent_version=PROMPT_VERSION,
            prompt_hash=prompt_digest(prompt_id, NOTIFICATION_CANDIDATE_VERSION, template,
                                      {"type": "object", "required": ["trade_date", "positions", "feature_snapshot_refs"]},
                                      output_schema, role),
            status="CANDIDATE",
        ))
        created += 1
    db.commit()
    return created


def prompt_digest(prompt_id: str, version: str, system_template: str,
                  input_schema: dict, output_schema: dict, role: str) -> str:
    return logical_hash({"prompt_id": prompt_id, "version": version,
                         "system_template": system_template, "input_schema": input_schema,
                         "output_schema": output_schema, "model_role": role})


def seed_prompts(db: Session) -> int:
    created = 0
    input_schema = {"type": "object", "required": ["trade_date", "positions", "evidence"]}
    output_schema = PortfolioActionPlan.model_json_schema()
    for prompt_id, (role, template) in PROMPTS.items():
        if db.query(PromptVersion).filter_by(prompt_id=prompt_id, version=PROMPT_VERSION).first():
            continue
        prior = db.query(PromptVersion).filter_by(prompt_id=prompt_id, status="ACTIVE").order_by(PromptVersion.id.desc()).first()
        db.add(PromptVersion(
            prompt_id=prompt_id, version=PROMPT_VERSION, system_template=template,
            input_schema=input_schema, output_schema=output_schema,
            model_role=role, change_reason="Compact all-holdings JSON contract",
            parent_version=prior.version if prior else None,
            prompt_hash=prompt_digest(prompt_id, PROMPT_VERSION, template, input_schema, output_schema, role),
            status="ACTIVE" if prior is None else "CANDIDATE",
        ))
        created += 1
    db.commit()
    return created


def active_prompt(db: Session, prompt_id: str) -> PromptVersion:
    row = db.query(PromptVersion).filter_by(prompt_id=prompt_id, status="ACTIVE").order_by(PromptVersion.id.desc()).first()
    if row is None:
        raise ValueError("active_prompt_missing")
    if row.prompt_hash != prompt_digest(row.prompt_id, row.version, row.system_template,
                                        row.input_schema, row.output_schema, row.model_role):
        raise ValueError("prompt_hash_mismatch")
    return row


def parse_portfolio_plan(raw: str, expected_symbols: set[str], trade_date: str) -> PortfolioActionPlan:
    plan = PortfolioActionPlan.model_validate_json(raw)
    if plan.trade_date != trade_date:
        raise ValueError("plan_trade_date_mismatch")
    if {row.symbol for row in plan.proposals} != expected_symbols:
        raise ValueError("plan_symbols_mismatch")
    return plan


async def run_portfolio_prompt(prompt_id: str, payload: dict, *, db_factory=None,
                               client_factory=None) -> tuple[PortfolioActionPlan, ModelResult]:
    """One batch request for all holdings; invalid output remains review-only."""
    from src.platform.persistence.database import SessionLocal
    from src.platform.ai.ai_client import AIClient

    factory = db_factory or SessionLocal
    with factory() as db:
        prompt = active_prompt(db, prompt_id)
        fields = (prompt.model_role, prompt.system_template, prompt.prompt_id,
                  prompt.version, prompt.prompt_hash, prompt.output_schema)
    role, system, pid, version, digest, output_schema = fields
    if not isinstance(payload, dict) or not {"trade_date", "positions", "evidence"} <= payload.keys():
        raise ValueError("prompt_input_schema_invalid")
    if not isinstance(payload["positions"], list) or not 1 <= len(payload["positions"]) <= 20:
        raise ValueError("prompt_position_count_invalid")
    symbols = {row["symbol"] for row in payload["positions"]}
    if len(symbols) != len(payload["positions"]):
        raise ValueError("prompt_duplicate_symbol")
    trade_date = payload["trade_date"]
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    system = f"{system}\nOutput JSON Schema: {json.dumps(output_schema, ensure_ascii=False, separators=(',', ':'))}"

    def validate(raw: str) -> bool:
        try:
            parse_portfolio_plan(raw, symbols, trade_date)
            return True
        except Exception:
            return False

    result = await run_role(role, system, content, prompt_id=pid,
                            prompt_version=version, prompt_hash=digest,
                            schema_validator=validate, db_factory=factory,
                            client_factory=client_factory or AIClient)
    return parse_portfolio_plan(result.content, symbols, trade_date), result
