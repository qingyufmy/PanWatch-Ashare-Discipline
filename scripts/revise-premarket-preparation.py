"""Append a current pre-auction briefing correction; never backfill trade signals."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.portfolio.daily_workflow import _positions, _position_limits, _market_capture_time
from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.market_context import collect_market_context, context_quality_matrix
from src.modules.portfolio.premarket_briefing import build_briefing, VERSION
from src.modules.portfolio.notification_renderer import render_premarket_plan
from src.modules.portfolio.notifications import enqueue_portfolio_notice, dispatch_portfolio_notice
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import DailyPortfolioPlan, EvidenceSnapshot, PortfolioTruthSnapshot
from src.platform.scheduling import trading_calendar


async def revise(apply: bool, notify: bool):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    await trading_calendar.refresh()
    if not trading_calendar.cn_business_day() or now.time() >= time(9, 15):
        raise RuntimeError("Current confirmed pre-auction session required; no historical replay")
    with SessionLocal() as db:
        previous = (db.query(DailyPortfolioPlan).filter_by(trade_date=day)
                    .order_by(DailyPortfolioPlan.version.desc()).first())
        if previous is None:
            raise RuntimeError("Original plan missing")
        if previous.prompt_id == "premarket_preparation_correction":
            return {"status": "ALREADY_REVISED", "plan_id": previous.id, "version": previous.version}
        truth = db.get(PortfolioTruthSnapshot, previous.truth_snapshot_id)
        positions = _positions(truth)
        market = await collect_market_context(db, trade_date=day, phase="PREMARKET",
                                             symbols=[p["symbol"] for p in positions], now=now)
        # Providers reset today's indicative quotes around 09:00. Keep the
        # original 08:50 frozen CN close, with its explicit evidence lineage.
        original = db.get(EvidenceSnapshot, previous.macro_evidence_snapshot_id)
        if original:
            market["latest_indicative_quotes"] = {key: market[key] for key in ("indices", "holdings")}
            for key in ("indices", "holdings"):
                market[key] = original.payload.get(key, [])
            market["cn_reference_evidence_snapshot_id"] = original.id
            market["context_quality"] = context_quality_matrix(market)
        limits = _position_limits(db, positions)
        preparation = build_briefing(market=market, positions=positions, limits=limits,
                                    nav=truth.nav, truth_source=truth.source)
        preparation["origin"] = "CURRENT_PREMARKET_CORRECTION"
        preparation["source_plan_id"] = previous.id
        preparation["model_status"] = "RULE_BASED_NO_MODEL_CALL"
        market["risk_tone"] = {"DEFENSIVE": "RISK_OFF", "CONSTRUCTIVE": "RISK_ON"}.get(preparation["risk_tone"], "UNVERIFIED")
        if preparation["exposure"]["suggested_min"] is None:
            raise RuntimeError("Incomplete previous-session price or declared NAV evidence")
        summary = {"status": "PREVIEW", "coverage": len(positions), "risk_tone": preparation["risk_tone"],
                   "previous_cn_session": preparation["previous_cn_session"],
                   "source_plan_id": previous.id, "created_signals": 0, "model_calls": 0}
        if not apply:
            return summary
        if datetime.now(ZoneInfo("Asia/Shanghai")).time() >= time(9, 15):
            raise RuntimeError("Pre-auction window ended while collecting evidence")
        macro = EvidenceSnapshot(captured_at=_market_capture_time(market, now),
            source="portfolio_market_context", logical_hash=logical_hash(market), payload=market,
            truth_snapshot_id=truth.id)
        db.add(macro)
        db.flush()
        frozen = {"trade_date": day, "proposals": [], "preparation": preparation,
                  "portfolio_rationale": "本次为规则条件预案修订；原模型输出及信号保留，未新增模型调用或交易信号。",
                  "supersedes_plan_id": previous.id}
        plan = DailyPortfolioPlan(trade_date=day, version=previous.version + 1, status="REVIEW_ONLY",
            truth_snapshot_id=truth.id, macro_evidence_snapshot_id=macro.id, model_run_id=None,
            prompt_id="premarket_preparation_correction", prompt_version=VERSION,
            input_hash=logical_hash({"market": market, "positions": positions, "limits": limits,
                                    "truth_id": truth.id, "nav": truth.nav}),
            output_hash=logical_hash(frozen), payload=frozen,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None))
        db.add(plan)
        db.flush()
        notice_id = None
        if notify:
            title, body = render_premarket_plan([], [], positions, preparation)
            title = "修订｜" + title
            body = f"{now.strftime('%H:%M')} 新生成的条件预案，补充今日早盘计划。\n" + body
            notice, _ = enqueue_portfolio_notice(db, key=f"premarket-correction:{day}:{VERSION}",
                title=title, content=body, trade_date=day, reason="PREMARKET_CORRECTION",
                expires_at=now.replace(hour=9, minute=15, second=0).astimezone(timezone.utc).replace(tzinfo=None))
            notice_id = notice.id
        db.commit()
        summary.update(status="REVISED", plan_id=plan.id, version=plan.version, notification_id=notice_id)
    if notice_id:
        summary["notification"] = await dispatch_portfolio_notice(notice_id, db_factory=SessionLocal)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    if args.notify and not args.apply:
        parser.error("--notify requires --apply")
    print(json.dumps(asyncio.run(revise(args.apply, args.notify)), ensure_ascii=False))
