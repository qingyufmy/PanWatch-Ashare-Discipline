"""Source-linked advisory risk observations with a Shadow notice decision.

An observation records that a directional proposal exists during verified
market weakness. It does not attest the proposal's technical thesis, approve
shares, or transmit a message. The existing PolicyGate remains authoritative
for execution readiness.
"""

from __future__ import annotations

import uuid
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.modules.portfolio.discipline import logical_hash
from src.modules.portfolio.notifications import enqueue_portfolio_notice
from src.platform.persistence.models import (
    PortfolioFeatureSnapshot, PortfolioLevelSnapshot, PortfolioRiskObservation, SignalEvent,
)


def _positive(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _confirmed_support_break(db: Session, *, trade_date: str, symbol: str,
                             quote: dict, observed: datetime) -> tuple[PortfolioLevelSnapshot, dict] | None:
    """Require a closed prior support, current minute level and fresh agreeing quote."""
    source = "sh" if symbol.startswith(("6", "9")) else "bj" if symbol.startswith(("4", "8")) else "sz"
    row = (db.query(PortfolioLevelSnapshot)
           .filter_by(trade_date=trade_date, symbol=source + symbol)
           .filter(PortfolioLevelSnapshot.market_asof <= observed)
           .order_by(PortfolioLevelSnapshot.market_asof.desc(), PortfolioLevelSnapshot.id.desc()).first())
    if (row is None or row.quality_status not in {"CONFIRMED", "INSUFFICIENT_HISTORY"}
            or row.prior_level_snapshot_id is None):
        return None
    if not timedelta(0) <= observed - row.market_asof <= timedelta(seconds=120):
        return None
    feature = db.get(PortfolioFeatureSnapshot, row.feature_snapshot_id)
    prior = db.get(PortfolioLevelSnapshot, row.prior_level_snapshot_id)
    prior_feature = db.get(PortfolioFeatureSnapshot, prior.feature_snapshot_id) if prior else None
    if (feature is None or feature.quality_status != "OK" or prior is None
            or prior.quality_status != "CONFIRMED"
            or prior_feature is None or prior_feature.quality_status != "OK"
            or not isinstance(row.payload, dict) or not isinstance(feature.payload, dict)
            or not isinstance(prior.payload, dict)
            or prior.symbol != row.symbol or prior.market_asof >= row.market_asof
            or prior_feature.symbol != prior.symbol
            or prior_feature.trade_date != prior.trade_date
            or (prior.payload or {}).get("source_hash") != prior_feature.source_hash
            or prior.source_hash != logical_hash(prior.payload)
            or (feature.payload or {}).get("missing_minutes")
            or row.source_hash != logical_hash(row.payload)
            or (row.payload or {}).get("source_hash") != feature.source_hash):
        return None
    previous = ((prior.payload or {}).get("levels") or {}).get("S1") or {}
    broken = _positive((row.payload or {}).get("broken_prior_support"))
    prior_support = _positive(previous.get("value"))
    minute_price = _positive((row.payload or {}).get("current_price"))
    quote_price = _positive(quote.get("price"))
    if (not broken or not prior_support or not minute_price or not quote_price
            or previous.get("quality_status") != "CONFIRMED"
            or previous.get("bar_closed") is not True
            or not previous.get("source_asof") or not previous.get("method")
            or not previous.get("timeframe")
            or abs(broken - prior_support) > 0.000001
            or minute_price >= broken or quote_price >= broken
            or abs(minute_price - quote_price) > max(0.02, quote_price * 0.003)):
        return None
    try:
        quote_asof = datetime.fromisoformat(quote["source_asof"])
    except (KeyError, TypeError, ValueError):
        return None
    if quote_asof.tzinfo is None:
        return None
    try:
        level_asof = datetime.fromisoformat(row.payload["market_data_asof"])
        support_asof = datetime.fromisoformat(previous["source_asof"])
    except (KeyError, TypeError, ValueError):
        return None
    if (level_asof.tzinfo is None or support_asof.tzinfo is None
            or abs((level_asof.astimezone(timezone.utc).replace(tzinfo=None)
                    - row.market_asof).total_seconds()) > 1
            or support_asof.astimezone(timezone.utc).replace(tzinfo=None) > prior.market_asof):
        return None
    age = observed - quote_asof.astimezone(timezone.utc).replace(tzinfo=None)
    if not timedelta(seconds=-30) <= age <= timedelta(seconds=120):
        return None
    return row, previous


def record_shadow_risk_observations(
    db: Session, *, trade_date: str, phase: str, market_context: dict,
    market_evidence_snapshot_id: int, observed_at: datetime,
) -> list[PortfolioRiskObservation]:
    """Coalesce active old-channel directions per held symbol and batch phase."""
    risk_off = (market_context.get("risk_tone") == "RISK_OFF" and
                sum(q.get("quality") == "FRESH" for q in market_context.get("indices", [])) >= 3)
    fresh_holdings = {q.get("symbol"): q for q in market_context.get("holdings", [])
                      if q.get("quality") == "FRESH" and q.get("symbol")}
    if not fresh_holdings:
        return []
    observed = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    candidates = ((db.query(SignalEvent)
                   .filter(SignalEvent.trade_date == trade_date,
                           SignalEvent.market == "CN",
                           SignalEvent.source == "AGENT",
                           SignalEvent.action.in_(("REDUCE", "EXIT")),
                           SignalEvent.status == "REVIEW_REQUIRED",
                           SignalEvent.generated_at <= observed,
                           SignalEvent.valid_from <= observed,
                           SignalEvent.expires_at > observed)
                   .order_by(SignalEvent.generated_at, SignalEvent.signal_id).all())
                  if risk_off else [])
    grouped: dict[str, list[SignalEvent]] = defaultdict(list)
    for signal in candidates:
        if signal.symbol in fresh_holdings:
            grouped[signal.symbol].append(signal)
    observations = []
    for symbol, quote in sorted(fresh_holdings.items()):
        signals = grouped.get(symbol, [])
        confirmed = _confirmed_support_break(db, trade_date=trade_date, symbol=symbol,
                                             quote=quote, observed=observed)
        if not confirmed and not signals:
            continue
        observation_type = "CONFIRMED_SUPPORT_BREAK" if confirmed else "RISK_OFF_DIRECTIONAL_REVIEW"
        episode_key = f"{trade_date}:{phase}:{symbol}:{observation_type}"
        existing = db.query(PortfolioRiskObservation).filter_by(episode_key=episode_key).first()
        if existing:
            observations.append(existing)
            continue
        expires = (min(observed + timedelta(minutes=20), max(s.expires_at for s in signals))
                   if signals else observed + timedelta(minutes=20))
        if confirmed:
            level_row, prior_support = confirmed
            body = (
                f"{symbol} 当前分钟价与新鲜持仓报价均低于上一版已收线支撑"
                f"{float(prior_support['value']):.2f}元（{prior_support['timeframe']}，"
                f"{prior_support['method']}，来源时刻 {prior_support['source_asof']}；"
                f"当前 LevelSnapshot #{level_row.id}，行情时刻 {quote['source_asof']}）。\n"
                "这是已证实的价位风险观察，动作方向及数量仍待核对；未批准交易，也未执行。"
            )
            data_quality = "CONFIRMED_LEVEL_AND_FRESH_QUOTE"
            severity = "HIGH"
        else:
            level_row = None
            action_types = "/".join(sorted({s.action for s in signals}))
            body = (
                f"市场指数证据显示 RISK_OFF；{symbol} 有 {action_types} 方向提案，"
                "但个股技术依据和执行资格尚未核实。\n"
                "这是待核查风险观察，不是减仓或清仓指令；未批准数量，也未执行。"
                "请核对最新行情、计划、可卖数量及当前唯一建议。"
            )
            data_quality = "MARKET_AND_HOLDING_FRESH_PROPOSAL_UNVERIFIED"
            severity = "REVIEW"
        title = f"风险核查｜{symbol}｜{'支撑失守' if confirmed else '待确认方向'}"
        notice, _ = enqueue_portfolio_notice(
            db, key=f"shadow-risk:{episode_key}", title=title, content=body,
            trade_date=trade_date, symbol=symbol,
            signal_ids=[s.signal_id for s in signals], priority="HIGH",
            reason="RISK_OBSERVATION", expires_at=expires,
        )
        notice.delivery_status = "SUPPRESSED"
        notice.suppression_reason = "SHADOW_ONLY"
        row = PortfolioRiskObservation(
            id=uuid.uuid4().hex, episode_key=episode_key, trade_date=trade_date,
            symbol=symbol, observation_type=observation_type, severity=severity,
            source_signal_ids=[s.signal_id for s in signals],
            market_evidence_snapshot_id=market_evidence_snapshot_id,
            level_snapshot_id=level_row.id if level_row else None,
            data_quality=data_quality,
            execution_readiness="NEEDS_CONFIRMATION",
            notice_outcome="SUPPRESSED", notice_reason="SHADOW_ONLY",
            notification_id=notice.id, observed_at=observed, expires_at=expires,
        )
        db.add(row)
        observations.append(row)
    db.flush()
    return observations
