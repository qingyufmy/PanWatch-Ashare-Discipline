"""Activate the tested 1.0.3 batch prompts and fix the observed output truncation.

Dry-run unless --apply is supplied. The notification candidate remains inactive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules.portfolio.prompt_registry import PROMPT_VERSION, prompt_digest
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ModelProfile, PromptVersion


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        changes = []
        for prompt_id in ("flash", "review"):
            candidate = db.query(PromptVersion).filter_by(
                prompt_id=prompt_id, version=PROMPT_VERSION).one()
            expected = prompt_digest(candidate.prompt_id, candidate.version,
                                     candidate.system_template, candidate.input_schema,
                                     candidate.output_schema, candidate.model_role)
            if candidate.prompt_hash != expected:
                raise ValueError(f"prompt_hash_mismatch:{prompt_id}")
            active = db.query(PromptVersion).filter_by(prompt_id=prompt_id, status="ACTIVE").all()
            if len(active) != 1:
                raise ValueError(f"active_prompt_count_invalid:{prompt_id}")
            if active[0].id != candidate.id:
                if candidate.status != "CANDIDATE":
                    raise ValueError(f"candidate_status_invalid:{prompt_id}")
                changes.append({"prompt_id": prompt_id, "from": active[0].version,
                                "to": candidate.version})
                if args.apply:
                    active[0].status = "SUPERSEDED"
                    candidate.status = "ACTIVE"
        profile = db.get(ModelProfile, "FAST")
        if profile is None or not profile.enabled:
            raise ValueError("fast_profile_unavailable")
        if profile.max_tokens < 10000 or profile.timeout_seconds < 150:
            changes.append({"profile": "FAST", "max_tokens": [profile.max_tokens, max(profile.max_tokens, 10000)],
                            "timeout_seconds": [profile.timeout_seconds, max(profile.timeout_seconds, 150)],
                            "reason": "2026-09-24 ten-position intraday response used 7365 of 8000 output tokens"})
            if args.apply:
                profile.max_tokens = max(profile.max_tokens, 10000)
                profile.timeout_seconds = max(profile.timeout_seconds, 150)
        if args.apply:
            db.commit()
        print(json.dumps({"applied": args.apply, "changes": changes}, ensure_ascii=False))


if __name__ == "__main__":
    main()
