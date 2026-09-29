"""Frozen, source-labelled market context for portfolio batch reviews."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketdata.vendors.tencent import fetch_raw
from sqlalchemy.orm import Session

from src.platform.marketdata.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.platform.persistence.models import MarketRegimeSnapshot
from src.platform.scheduling.trading_calendar import previous_confirmed_cn_trading_day

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
    planning = context.get("phase") == "PREMARKET"
    usable = {"FRESH", "PREVIOUS_CLOSE"} if planning else {"FRESH"}
    fresh_holdings = [q for q in holdings if q.get("quality") in usable]
    tech = context.get("global_tech") or []
    candidate = context.get("candidate_breadth") or {}
    trade_date = context.get("trade_date")
    candidate_date = candidate.get("snapshot_date")
    return {
        "indices": {"fresh": sum(q.get("quality") == "FRESH" for q in indices),
                    "usable_for_planning": sum(q.get("quality") in usable for q in indices),
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
                     "usable_for_planning": len(fresh_holdings),
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
                        "dated_reference": sum(q.get("quality") == "DATED_REFERENCE" for q in tech),
                        "total": len(tech),
                        "fresh": sum(q.get("quality") == "FRESH" for q in tech),
                        "scope": "selected_global_technology"},
        "boards": {"quality": "FETCH_TIME_ONLY" if context.get("boards") else "MISSING",
                   "scope": "hot_board_sample"},
    }


def _quote(row: dict | None, label: str, *, collected_at: datetime, phase: str,
           market: str = "CN") -> dict:
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
        if age < -30:
            quality = "FUTURE_TIMESTAMP"
        elif phase in {"INTRADAY", "AUCTION"}:
            quality = "FRESH" if -30 <= age <= 120 else "STALE"
        elif source_asof.date() == collected_at.date():
            quality = "TODAY_QUOTE"
        elif (source_asof.date() == previous_confirmed_cn_trading_day(collected_at.date())
              and source_asof.hour >= 15):
            quality = "PREVIOUS_CLOSE"
        else:
            quality = "STALE"
    raw_time = row.get("source_time_raw")
    source_session_date = None
    if market == "US" and raw_time:
        try:
            # Preserve the vendor's native session date. Its timezone is not
            # documented in this adapter, so never manufacture a UTC timestamp.
            native = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
            source_session_date = native.date().isoformat()
            age_days = (collected_at.date() - native.date()).days
            if 1 <= age_days <= 4:
                quality = "DATED_REFERENCE"
        except (TypeError, ValueError):
            pass
    return {
        "name": label, "symbol": row.get("symbol"),
        "price": row.get("current_price"), "change_pct": row.get("change_pct"),
        "prev_close": row.get("prev_close"),
        "high_price": row.get("high_price"), "low_price": row.get("low_price"),
        "source_time_raw": raw_time, "source_session_date": source_session_date,
        "timezone_status": "UNVERIFIED" if market == "US" else "Asia/Shanghai",
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
    tech = [_quote(by_symbol.get(key), label, collected_at=observed, phase=phase, market="US")
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
    usable = {"PREVIOUS_CLOSE"} if phase == "PREMARKET" else {"FRESH"}
    fresh_indices = sum(q["quality"] in usable for q in indices)
    up = sum((q.get("change_pct") or 0) > 0 for q in indices if q["quality"] in usable)
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
        "previous_cn_session": (previous_confirmed_cn_trading_day(observed.date()).isoformat()
                                if previous_confirmed_cn_trading_day(observed.date()) else None),
        "risk_tone_basis": "PREVIOUS_CN_CLOSE" if phase == "PREMARKET" else "CURRENT_QUOTES",
        "limits": ["US native session dates are reference-only; timezone is unverified, not live resonance.",
                   "Candidate breadth is not whole-market breadth.",
                   "Board rankings have fetch time only; confirm at individual quote time."],
    }
    result["context_quality"] = context_quality_matrix(result)
    return result
