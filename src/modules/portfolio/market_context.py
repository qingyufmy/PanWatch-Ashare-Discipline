"""Frozen, source-labelled market context for portfolio batch reviews."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketdata.vendors.tencent import fetch_raw
from sqlalchemy.orm import Session

from src.platform.marketdata.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.platform.persistence.models import MarketRegimeSnapshot

SH = ZoneInfo("Asia/Shanghai")
CN_INDICES = {
    "sh000001": "上证指数", "sz399001": "深证成指",
    "sz399006": "创业板指", "sh000300": "沪深300",
}
GLOBAL_TECH = {
    "usIXIC": "纳斯达克综合", "usINX": "标普500",
    "usNVDA": "英伟达", "usAMD": "AMD", "usTSM": "台积电ADR",
}


def context_quality_matrix(context: dict) -> dict:
    """Keep coverage, sample scope and source-time quality separate."""
    indices = context.get("indices") or []
    holdings = context.get("holdings") or []
    fresh_holdings = [q for q in holdings if q.get("quality") == "FRESH"]
    tech = context.get("global_tech") or []
    candidate = context.get("candidate_breadth") or {}
    trade_date = context.get("trade_date")
    candidate_date = candidate.get("snapshot_date")
    return {
        "indices": {"fresh": sum(q.get("quality") == "FRESH" for q in indices),
                    "total": len(indices), "scope": "major_indices"},
        "market_breadth": {"quality": "MISSING", "scope": "whole_market",
                           "reason": "NO_WHOLE_MARKET_FEED"},
        "candidate_pool_breadth": {
            "quality": ("MISSING" if not candidate_date else
                        "CURRENT_DAY" if candidate_date == trade_date else "STALE"),
            "scope": "candidate_sample_not_market_wide",
            "snapshot_date": candidate_date,
            "sample_size": candidate.get("sample_size"),
        },
        "holdings": {"fresh": sum(q.get("quality") == "FRESH" for q in holdings),
                     "total": len(holdings), "scope": "current_holdings"},
        "holding_breadth": {
            "scope": "current_holdings_count_not_market_wide",
            "advancers": sum((q.get("change_pct") or 0) > 0 for q in fresh_holdings),
            "decliners": sum((q.get("change_pct") or 0) < 0 for q in fresh_holdings),
            "unchanged": sum(q.get("change_pct") == 0 for q in fresh_holdings),
            "change_unknown": sum(q.get("change_pct") is None for q in fresh_holdings),
            "missing_or_stale": len(holdings) - len(fresh_holdings),
        },
        "industry_exposure": {"quality": "MISSING",
                              "reason": "NO_VERIFIED_SECTOR_MAPPING"},
        "global_tech": {"source_time_verified": sum(bool(q.get("source_asof")) for q in tech),
                        "total": len(tech),
                        "fresh": sum(q.get("quality") == "FRESH" for q in tech),
                        "scope": "selected_global_technology"},
        "boards": {"quality": "FETCH_TIME_ONLY" if context.get("boards") else "MISSING",
                   "scope": "hot_board_sample"},
    }


def _quote(row: dict | None, label: str, *, collected_at: datetime, phase: str) -> dict:
    if not row:
        return {"name": label, "quality": "MISSING"}
    raw_asof = row.get("source_asof")
    try:
        source_asof = datetime.strptime(str(raw_asof), "%Y%m%d%H%M%S").replace(tzinfo=SH)
    except (TypeError, ValueError):
        source_asof = None
    quality = "SOURCE_TIME_UNKNOWN"
    if source_asof:
        age = (collected_at - source_asof).total_seconds()
        if phase in {"INTRADAY", "AUCTION"}:
            quality = "FRESH" if -30 <= age <= 120 else "STALE"
        elif source_asof.date() == collected_at.date():
            quality = "TODAY_QUOTE"
        elif timedelta(0) <= collected_at - source_asof <= timedelta(days=4):
            quality = "PREVIOUS_CLOSE"
        else:
            quality = "STALE"
    return {
        "name": label, "symbol": row.get("symbol"),
        "price": row.get("current_price"), "change_pct": row.get("change_pct"),
        "prev_close": row.get("prev_close"),
        "source": "tencent_quote", "source_asof": source_asof.isoformat() if source_asof else None,
        "quality": quality,
    }


async def collect_market_context(
    db: Session, *, trade_date: str, phase: str, symbols: list[str],
    now: datetime | None = None, quote_fetcher=fetch_raw,
    board_fetcher=None,
) -> dict:
    """Collect one bounded batch. Missing/timeless evidence stays explicit."""
    observed = (now or datetime.now(SH)).astimezone(SH)
    requested = [*CN_INDICES, *GLOBAL_TECH, *[("sh" if s.startswith(("6", "9")) else "sz") + s for s in symbols]]
    error = None
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(quote_fetcher, requested), timeout=8)
    except Exception as exc:
        rows, error = [], type(exc).__name__
    by_symbol = {str(row.get("symbol")): row for row in rows if isinstance(row, dict)}
    index_keys = ("000001", "399001", "399006", "000300")
    indices = [_quote(by_symbol.get(key), label, collected_at=observed, phase=phase)
               for key, label in zip(index_keys, CN_INDICES.values())]
    tech_keys = (".IXIC", ".INX", "NVDA", "AMD", "TSM")
    tech = [_quote(by_symbol.get(key), label, collected_at=observed, phase=phase)
            for key, label in zip(tech_keys, GLOBAL_TECH.values())]
    holdings = [_quote(by_symbol.get(symbol), symbol, collected_at=observed, phase=phase)
                for symbol in symbols]
    regime = (db.query(MarketRegimeSnapshot)
              .filter_by(market="CN")
              .order_by(MarketRegimeSnapshot.snapshot_date.desc(), MarketRegimeSnapshot.id.desc())
              .first())
    sample = ({"snapshot_date": regime.snapshot_date, "regime": regime.regime,
               "breadth_up_pct": regime.breadth_up_pct, "confidence": regime.confidence,
               "sample_size": regime.sample_size, "scope": "candidate_sample_not_market_wide"}
              if regime else {"quality": "MISSING"})
    boards = []
    if phase == "INTRADAY":
        try:
            fetcher = board_fetcher or EastMoneyDiscoveryCollector().fetch_hot_boards
            found = await asyncio.wait_for(fetcher(market="CN", mode="turnover", limit=8), timeout=6)
            boards = [{"code": b.code, "name": b.name, "change_pct": b.change_pct,
                       "turnover": b.turnover, "quality": "FETCH_TIME_ONLY"} for b in found]
        except Exception:
            pass
    fresh_indices = sum(q["quality"] == "FRESH" for q in indices)
    up = sum((q.get("change_pct") or 0) > 0 for q in indices if q["quality"] == "FRESH")
    risk_tone = ("RISK_ON" if fresh_indices >= 3 and up >= 3 else
                 "RISK_OFF" if fresh_indices >= 3 and up <= 1 else "UNVERIFIED")
    result = {
        "trade_date": trade_date, "phase": phase,
        "collected_at": observed.isoformat(), "quote_error": error,
        "completed_at": datetime.now(SH).isoformat(),
        "indices": indices,
        "market_breadth": {"quality": "MISSING", "scope": "whole_market"},
        "candidate_breadth": sample,
        "global_tech": tech, "boards": boards, "holdings": holdings,
        "risk_tone": risk_tone,
        "limits": ["US source timestamps are unavailable; do not claim fresh global resonance.",
                   "Candidate breadth is not whole-market breadth.",
                   "Board rankings have fetch time only; confirm at individual quote time."],
    }
    result["context_quality"] = context_quality_matrix(result)
    return result
