"""Logical FAST/DEEP model routing with bounded calls and attempt journals."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import logical_hash
from src.platform.ai.ai_client import AIClient
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import AIModel, AIService, ModelProfile, ModelRun


ROLES = frozenset({"FAST", "FAST_BACKUP", "DEEP", "DEEP_BACKUP"})


@dataclass(frozen=True)
class ModelResult:
    content: str
    run_id: str
    requested_role: str
    profile_role: str
    requested_model: str
    reported_model: str | None
    degraded: bool


def seed_default_profiles(db: Session) -> int:
    """Bind logical roles to the existing default model without changing keys."""
    model = db.query(AIModel).filter(AIModel.is_default.is_(True)).first() or db.query(AIModel).first()
    if model is None:
        return 0
    defaults = {
        "FAST": (2400, 60, 1, "FAST_BACKUP"),
        "DEEP": (6000, 120, 0, "DEEP_BACKUP"),
    }
    created = 0
    for role, (max_tokens, timeout, retries, fallback) in defaults.items():
        if db.get(ModelProfile, role) is None:
            db.add(ModelProfile(
                role=role, ai_model_id=model.id, thinking="default",
                max_tokens=max_tokens, timeout_seconds=timeout,
                retries=retries, fallback_role=fallback, enabled=True,
            ))
            created += 1
    db.commit()
    return created


def _profile_config(db_factory: Callable[[], Session], role: str) -> dict | None:
    with db_factory() as db:
        profile = db.get(ModelProfile, role)
        if profile is None or not profile.enabled:
            return None
        model = db.get(AIModel, profile.ai_model_id)
        service = db.get(AIService, model.service_id) if model else None
        if model is None or service is None or not service.api_key:
            return None
        return {
            "role": role, "model": model.model,
            "base_url": service.base_url, "api_key": service.api_key,
            "max_tokens": profile.max_tokens,
            "reasoning_effort": profile.reasoning_effort,
            "thinking": profile.thinking,
            "timeout_seconds": profile.timeout_seconds,
            "retries": profile.retries,
            "fallback_role": profile.fallback_role,
        }


def _save_run(db_factory: Callable[[], Session], values: dict) -> None:
    with db_factory() as db:
        db.add(ModelRun(**values))
        db.commit()


def _usage_numbers(client) -> tuple[int | None, int | None, float | None]:
    usage = getattr(client, "last_usage", None)
    if usage is None:
        return None, None, None
    return (
        getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None),
        getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None),
        getattr(usage, "cost_usd", None) or getattr(usage, "estimated_cost_usd", None),
    )


async def run_role(
    role: str, system_prompt: str, user_content: str, *,
    trace_id: str | None = None, prompt_id: str | None = None,
    prompt_version: str | None = None, prompt_hash: str | None = None,
    schema_validator: Callable[[str], bool] | None = None,
    db_factory: Callable[[], Session] = SessionLocal,
    client_factory: Callable[..., AIClient] = AIClient,
) -> ModelResult:
    """Return a response only after its attempt is durably recorded."""
    if role not in ROLES or not system_prompt or not user_content:
        raise ValueError("invalid_model_role_or_input")
    trace = trace_id or uuid.uuid4().hex
    input_hash = logical_hash({"system": system_prompt, "user": user_content})
    current_role = role
    visited: set[str] = set()
    last_error: Exception | None = None
    while current_role and current_role not in visited:
        visited.add(current_role)
        config = _profile_config(db_factory, current_role)
        if config is None:
            if last_error is None:
                last_error = RuntimeError(f"model_profile_unavailable:{current_role}")
            break
        for attempt in range(config["retries"] + 1):
            run_id = uuid.uuid4().hex
            started = datetime.now(timezone.utc)
            clock = time.monotonic()
            client = client_factory(config["base_url"], config["api_key"], model=config["model"])
            reported = None
            input_tokens = output_tokens = cost_usd = None
            schema_valid = None
            error = None
            content = ""
            try:
                request_options = {"temperature": 0.2, "max_tokens": config["max_tokens"]}
                if config["reasoning_effort"]:
                    request_options["reasoning_effort"] = config["reasoning_effort"]
                if config["thinking"] in {"enabled", "disabled"}:
                    request_options["thinking"] = config["thinking"]
                content = await asyncio.wait_for(
                    client.chat_multi(
                        [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_content}],
                        **request_options,
                    ),
                    timeout=config["timeout_seconds"],
                )
                reported = getattr(client, "last_reported_model", None)
                input_tokens, output_tokens, cost_usd = _usage_numbers(client)
                schema_valid = schema_validator(content) if schema_validator else None
                if schema_valid is False:
                    raise ValueError("model_schema_invalid")
            except Exception as exc:
                last_error = exc
                error = (str(exc) or type(exc).__name__)[:255]
            finished = datetime.now(timezone.utc)
            degraded = current_role != role
            status = "FAILED" if error else "MODEL_DEGRADED" if degraded else "OK"
            _save_run(db_factory, {
                "run_id": run_id, "trace_id": trace,
                "role": role, "profile_role": current_role,
                "requested_model": config["model"],
                "reported_model": reported,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "prompt_hash": prompt_hash,
                "input_hash": input_hash,
                "output_hash": logical_hash(content) if content else None,
                "output_text": content or None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "latency_ms": int((time.monotonic() - clock) * 1000),
                "fallback_used": degraded,
                "schema_valid": schema_valid,
                "status": status, "error": error,
                "started_at": started.replace(tzinfo=None),
                "finished_at": finished.replace(tzinfo=None),
            })
            if error is None:
                return ModelResult(
                    content=content, run_id=run_id,
                    requested_role=role, profile_role=current_role,
                    requested_model=config["model"],
                    reported_model=reported, degraded=degraded,
                )
            if isinstance(last_error, ValueError) and str(last_error) == "model_schema_invalid":
                raise last_error  # Schema errors require review, never silently change model.
        current_role = config["fallback_role"]
    raise last_error or RuntimeError("model_route_unavailable")


async def probe_role(role: str, **kwargs) -> dict:
    """Bounded live capability check; every attempt is recorded in model_runs."""
    def valid_json(raw: str) -> bool:
        try:
            return json.loads(raw) == {"ok": True}
        except (ValueError, TypeError):
            return False

    try:
        result = await run_role(
            role,
            'Return only JSON exactly {"ok":true}.',
            'Return the requested JSON now.',
            prompt_id="capability_probe", prompt_version="1",
            schema_validator=valid_json, **kwargs,
        )
        return {"role": role, "status": "OK" if not result.degraded else "MODEL_DEGRADED",
                "run_id": result.run_id, "requested_model": result.requested_model,
                "reported_model": result.reported_model}
    except Exception as exc:
        return {"role": role, "status": "FAILED", "error": str(exc)[:255]}
