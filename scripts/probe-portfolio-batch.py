"""Exercise one review-only portfolio batch call using the local isolated database."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.portfolio.prompt_registry import run_portfolio_prompt
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import PortfolioTruthSnapshot


async def main():
    with SessionLocal() as db:
        truth = db.query(PortfolioTruthSnapshot).order_by(PortfolioTruthSnapshot.id.desc()).first()
        if truth is None:
            raise RuntimeError("truth_snapshot_missing")
        positions = [{"market": p.market, "symbol": p.symbol,
                      "total_qty": p.total_qty, "sellable_qty": p.sellable_qty}
                     for p in truth.positions]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    payload = {"trade_date": today, "positions": positions,
               "evidence": {"market_data_status": "STALE_OR_UNVERIFIED",
                            "portfolio_truth_status": "USER_ATTESTED_UNRECONCILED",
                            "instruction": "Use HOLD for every symbol because evidence is unverified."}}
    plan, run = await run_portfolio_prompt("flash", payload)
    print({"run_id": run.run_id, "role": run.requested_role,
           "reported_model": run.reported_model, "count": len(plan.proposals),
           "actions": sorted({p.action for p in plan.proposals})})


if __name__ == "__main__":
    asyncio.run(main())
