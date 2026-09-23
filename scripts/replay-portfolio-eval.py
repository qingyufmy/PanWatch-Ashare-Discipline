"""Offline P8 comparison; no live data or model calls."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.modules.portfolio.evaluation import compare, load_frozen_cases, write_comparison


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--champion-prompt", required=True)
    parser.add_argument("--challenger-prompt", required=True)
    parser.add_argument("--champion-model", required=True)
    parser.add_argument("--challenger-model", required=True)
    parser.add_argument("--champion-policy", required=True)
    parser.add_argument("--challenger-policy", required=True)
    args = parser.parse_args()
    cases, digest = load_frozen_cases(args.cases)
    result = compare(cases, input_hash=digest,
                     champion={"prompt_version": args.champion_prompt,
                               "model_version": args.champion_model,
                               "policy_version": args.champion_policy},
                     challenger={"prompt_version": args.challenger_prompt,
                                 "model_version": args.challenger_model,
                                 "policy_version": args.challenger_policy})
    write_comparison(result, args.output)
    print(f"{len(cases)} frozen cases; champion={result['metrics']['champion']['passed']}; "
          f"challenger={result['metrics']['challenger']['passed']}; shadow only")
