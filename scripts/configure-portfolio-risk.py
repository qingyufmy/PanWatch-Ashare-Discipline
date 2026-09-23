"""Apply a frozen, human review-only risk proposal to append-only position plans."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.modules.portfolio.discipline import capture_truth, create_plan_version, current_plan
from src.platform.persistence.models import AppSettings, PortfolioTruthSnapshot, Position, Stock


def proposal(record: dict, quantity: int, nav: float) -> dict:
    close, atr, low = (float(record[key]) for key in ("close", "atr14", "low10"))
    if not all(math.isfinite(x) and x > 0 for x in (close, atr, low, nav)):
        raise ValueError("invalid_market_or_nav")
    if record["trade_date"] != "2026-09-22" or record["sina_close"] != record["close"]:
        raise ValueError("unverified_daily_close")
    if record["sina_last_at"] != "2026-09-22T15:00:00+08:00":
        raise ValueError("incomplete_minute_close")
    atr_pct = atr / close
    cap = .05 if atr_pct >= .08 else .06 if atr_pct >= .06 else .07 if atr_pct >= .04 else .08
    stop = math.floor(max(close - 2 * atr, low - .5 * atr) * 100) / 100
    current_weight = close * quantity / nav
    return {"close": close, "atr14": round(atr, 4), "low10": low,
            "current_weight": round(current_weight, 6), "max_weight": cap,
            "current_stop": stop, "overweight": current_weight > cap}


def apply(db_path: Path, evidence_path: Path, *, dry_run: bool = False) -> list[dict]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    nav = float(evidence["declared_nav_cny"])
    records = evidence["stocks"]
    if len(records) != 11 or len(set(records)) != 11 or nav != 500000:
        raise ValueError("portfolio_evidence_mismatch")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    output = []
    with Session(engine) as db:
        held = {stock.symbol: (stock.market, pos.quantity) for pos, stock in db.query(Position, Stock).join(Stock).all()}
        if set(held) != set(records):
            raise ValueError("held_symbols_changed")
        for symbol, record in sorted(records.items()):
            market, qty = held[symbol]
            plan = current_plan(db, market, symbol)
            if plan is None:
                raise ValueError(f"plan_missing:{symbol}")
            p = proposal(record, qty, nav)
            output.append({"symbol": symbol, "quantity": qty, **p})
        if dry_run:
            return output
        nav_setting = db.query(AppSettings).filter_by(key="portfolio_declared_nav_cny").first()
        if nav_setting is None:
            nav_setting = AppSettings(key="portfolio_declared_nav_cny")
            db.add(nav_setting)
        nav_setting.value = str(int(nav))
        nav_setting.description = "用户于2026-09-22声明的总资产，非券商核对值"
        db.commit()
        truth = (db.query(PortfolioTruthSnapshot).filter_by(nav=nav, phase="MANUAL")
                 .order_by(PortfolioTruthSnapshot.id.desc()).first())
        if truth is None:
            truth = capture_truth(db, phase="MANUAL", sellable_equals_quantity=True)
        for row in output:
            symbol = row["symbol"]
            market, _ = held[symbol]
            prior = current_plan(db, market, symbol)
            if ((prior.plan or {}).get("risk", {}).get("current_stop") == row["current_stop"]
                    and (prior.plan or {}).get("position", {}).get("max_weight") == row["max_weight"]):
                row["plan_version"] = prior.version
                continue
            document = copy.deepcopy(prior.plan)
            document["position"].update({
                "current_weight": row["current_weight"], "max_weight": row["max_weight"],
                "overweight": row["overweight"],
                "sellable_qty": row["quantity"], "today_locked_qty": 0,
            })
            document["risk"].update({
                "initial_stop": row["current_stop"], "current_stop": row["current_stop"],
                "stop_basis": "2026-09-22 close; max(close-2*ATR14,10d_low-0.5*ATR14)",
                "market_data_asof": "2026-09-22T15:00:00+08:00",
                "market_data_sources": ["Tencent qfq daily", "Sina 1m close"],
            })
            document["review_status"] = "REVIEW_ONLY"
            document["review_reasons"] = ["USER_ATTESTED_UNRECONCILED", "STOP_PROVISIONAL_RECHECK_NEXT_OPEN"]
            if row["overweight"]:
                document["review_reasons"].append("CURRENT_WEIGHT_ABOVE_PROPOSED_CAP")
            document["position"]["max_weight_basis"] = "500000 CNY user-attested NAV; ATR14 tier cap"
            new_state = "OVERWEIGHT" if row["overweight"] and prior.position_state in {
                "CORE", "ADD_ALLOWED", "OVERWEIGHT"} else prior.position_state
            created, event = create_plan_version(
                db, market=market, symbol=symbol, position_state=new_state,
                thesis_state=prior.thesis_state, plan=document,
                reason="2026-09-22 user-declared NAV and verified-close risk proposal",
                truth_snapshot_id=truth.id, expected_version=prior.version,
            )
            if created is None:
                raise RuntimeError(f"plan_rejected:{symbol}:{event.reason}")
            row["plan_version"] = created.version
        return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(apply(args.db, args.evidence, dry_run=not args.apply), ensure_ascii=False, indent=2))
