"""P4 model role configuration and bounded capability probe."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.portfolio.model_router import ROLES, probe_role, seed_default_profiles
from src.platform.persistence.database import get_db
from src.platform.persistence.models import AIModel, ModelProfile, ModelRun, PromptVersion

router = APIRouter()


class ProfileUpdate(BaseModel):
    ai_model_id: int
    thinking: str = "default"
    reasoning_effort: str | None = None
    max_tokens: int = Field(ge=64, le=32768)
    timeout_seconds: int = Field(ge=1, le=300)
    retries: int = Field(ge=0, le=2)
    fallback_role: str | None = None
    enabled: bool = True


def _profile(row: ModelProfile) -> dict:
    return {"role": row.role, "ai_model_id": row.ai_model_id,
            "thinking": row.thinking, "reasoning_effort": row.reasoning_effort,
            "max_tokens": row.max_tokens, "timeout_seconds": row.timeout_seconds,
            "retries": row.retries, "fallback_role": row.fallback_role,
            "enabled": row.enabled, "updated_at": row.updated_at}


@router.get("/profiles")
def profiles(db: Session = Depends(get_db)):
    return [_profile(row) for row in db.query(ModelProfile).order_by(ModelProfile.role).all()]


@router.post("/profiles/seed")
def seed_profiles(db: Session = Depends(get_db)):
    return {"created": seed_default_profiles(db)}


@router.put("/profiles/{role}")
def update_profile(role: str, body: ProfileUpdate, db: Session = Depends(get_db)):
    if role not in ROLES or body.fallback_role == role or (body.fallback_role and body.fallback_role not in ROLES):
        raise HTTPException(400, "invalid_model_role")
    if body.thinking not in {"default", "enabled", "disabled"}:
        raise HTTPException(400, "invalid_thinking")
    if body.reasoning_effort not in {None, "low", "medium", "high"}:
        raise HTTPException(400, "invalid_reasoning_effort")
    if db.get(AIModel, body.ai_model_id) is None:
        raise HTTPException(404, "model_not_found")
    row = db.get(ModelProfile, role)
    if row is None:
        row = ModelProfile(role=role, ai_model_id=body.ai_model_id)
        db.add(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return _profile(row)


@router.post("/profiles/{role}/probe")
async def probe_profile(role: str):
    if role not in ROLES:
        raise HTTPException(400, "invalid_model_role")
    return await probe_role(role)


@router.get("/runs")
def list_runs(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.query(ModelRun).order_by(ModelRun.started_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [{"run_id": r.run_id, "trace_id": r.trace_id, "role": r.role,
             "profile_role": r.profile_role, "requested_model": r.requested_model,
             "reported_model": r.reported_model, "prompt_id": r.prompt_id,
             "prompt_version": r.prompt_version, "prompt_hash": r.prompt_hash,
             "input_hash": r.input_hash, "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens, "cost_usd": r.cost_usd,
             "latency_ms": r.latency_ms, "fallback_used": r.fallback_used,
             "schema_valid": r.schema_valid, "status": r.status, "error": r.error,
             "started_at": r.started_at} for r in rows]


@router.get("/prompts")
def list_prompt_versions(db: Session = Depends(get_db)):
    rows = db.query(PromptVersion).order_by(PromptVersion.prompt_id, PromptVersion.id.desc()).all()
    return [{"prompt_id": r.prompt_id, "version": r.version, "model_role": r.model_role,
             "input_schema": r.input_schema, "output_schema": r.output_schema,
             "change_reason": r.change_reason, "parent_version": r.parent_version,
             "prompt_hash": r.prompt_hash, "status": r.status,
             "created_at": r.created_at} for r in rows]
