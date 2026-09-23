import copy
from pathlib import Path

import pytest

from src.modules.portfolio.evaluation import compare, load_frozen_cases


CASES = Path(__file__).resolve().parents[1] / "docs/portfolio_discipline/eval_cases_v1.json"
VERSIONS = {"prompt_version": "v1", "model_version": "frozen", "policy_version": "p3"}


def test_ten_templates_replay_without_activation():
    cases, digest = load_frozen_cases(CASES)
    result = compare(cases, champion=VERSIONS, challenger=VERSIONS, input_hash=digest)
    assert len(cases) == 10
    assert result["metrics"]["champion"]["passed"] == 10
    assert result["challenger_mode"] == "SHADOW_ONLY"


def test_future_evidence_rejected():
    cases, _ = load_frozen_cases(CASES)
    bad = copy.deepcopy(cases[0])
    bad["frozen"]["evidence"]["asof"] = "2026-09-23T09:31:00+08:00"
    import json
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as temp:
        path = Path(temp) / "cases.json"
        path.write_text(json.dumps([bad]), encoding="utf-8")
        with pytest.raises(ValueError, match="future_data"):
            load_frozen_cases(path)
