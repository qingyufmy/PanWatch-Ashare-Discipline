"""Exercise one batch prompt with frozen market context; never create signals."""

import asyncio
import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.portfolio.prompt_registry import run_portfolio_prompt
from src.modules.portfolio.market_context import collect_market_context
from src.modules.portfolio.daily_workflow import _paper_state, _position_limits
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import PortfolioTruthSnapshot


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", choices=("flash", "review"), default="flash")
    args = parser.parse_args()
    with SessionLocal() as db:
        truth = db.query(PortfolioTruthSnapshot).order_by(PortfolioTruthSnapshot.id.desc()).first()
        if truth is None:
            raise RuntimeError("truth_snapshot_missing")
        positions = [{"market": p.market, "symbol": p.symbol,
                      "total_qty": p.total_qty, "sellable_qty": p.sellable_qty}
                     for p in truth.positions]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with SessionLocal() as db:
        market = await collect_market_context(db, trade_date=today, phase="PREMARKET",
                                              symbols=[p["symbol"] for p in positions])
        paper = _paper_state(db)
        limits = _position_limits(db, positions)
    payload = {"trade_date": today, "positions": positions,
               "evidence": {"market_data_status": "STALE_OR_UNVERIFIED",
                            "portfolio_truth_status": "USER_ATTESTED_UNRECONCILED",
                            "market_context": market,
                            "paper_account": paper,
                            "position_limits": limits,
                            "instruction": "After-hours probe only; use HOLD for every symbol because there is no live session evidence."}}
    plan, run = await run_portfolio_prompt(args.prompt, payload)
    print({"run_id": run.run_id, "role": run.requested_role,
           "reported_model": run.reported_model, "count": len(plan.proposals),
           "actions": sorted({p.action for p in plan.proposals})})


if __name__ == "__main__":
    asyncio.run(main())
