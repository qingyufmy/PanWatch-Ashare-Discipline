"""Source-linked advisory risk observations with a Shadow notice decision.

An observation records that a directional proposal exists during verified
market weakness. It does not attest the proposal's technical thesis, approve
shares, or transmit a message. The existing PolicyGate remains authoritative
for execution readiness.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.modules.portfolio.notifications import enqueue_portfolio_notice
from src.platform.persistence.models import PortfolioRiskObservation, SignalEvent


def record_shadow_risk_observations(
    db: Session, *, trade_date: str, phase: str, market_context: dict,
    market_evidence_snapshot_id: int, observed_at: datetime,
) -> list[PortfolioRiskObservation]:
    """Coalesce active old-channel directions per held symbol and batch phase."""
    if market_context.get("risk_tone") != "RISK_OFF":
        return []
    if sum(q.get("quality") == "FRESH" for q in market_context.get("indices", [])) < 3:
        return []
    fresh_holdings = {q.get("symbol") for q in market_context.get("holdings", [])
                      if q.get("quality") == "FRESH"}
    if not fresh_holdings:
        return []
    observed = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    candidates = (db.query(SignalEvent)
                  .filter(SignalEvent.trade_date == trade_date,
                          SignalEvent.market == "CN",
                          SignalEvent.source == "AGENT",
                          SignalEvent.action.in_(("REDUCE", "EXIT")),
                          SignalEvent.status == "REVIEW_REQUIRED",
                          SignalEvent.generated_at <= observed,
                          SignalEvent.valid_from <= observed,
                          SignalEvent.expires_at > observed)
                  .order_by(SignalEvent.generated_at, SignalEvent.signal_id).all())
    grouped: dict[str, list[SignalEvent]] = defaultdict(list)
    for signal in candidates:
        if signal.symbol in fresh_holdings:
            grouped[signal.symbol].append(signal)
    observations = []
    for symbol, signals in sorted(grouped.items()):
        episode_key = f"{trade_date}:{phase}:{symbol}:RISK_OFF_DIRECTIONAL_REVIEW"
        existing = db.query(PortfolioRiskObservation).filter_by(episode_key=episode_key).first()
        if existing:
            observations.append(existing)
            continue
        expires = min(observed + timedelta(minutes=20), max(s.expires_at for s in signals))
        action_types = "/".join(sorted({s.action for s in signals}))
        title = f"风险核查｜{symbol}｜待确认方向"
        body = (
            f"市场指数证据显示 RISK_OFF；{symbol} 有 {action_types} 方向提案，"
            "但个股技术依据和执行资格尚未核实。\n"
            "这是待核查风险观察，不是减仓或清仓指令；未批准数量，也未执行。"
            "请核对最新行情、计划、可卖数量及当前唯一建议。"
        )
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
            symbol=symbol, observation_type="RISK_OFF_DIRECTIONAL_REVIEW",
            source_signal_ids=[s.signal_id for s in signals],
            market_evidence_snapshot_id=market_evidence_snapshot_id,
            data_quality="MARKET_AND_HOLDING_FRESH_PROPOSAL_UNVERIFIED",
            execution_readiness="NEEDS_CONFIRMATION",
            notice_outcome="SUPPRESSED", notice_reason="SHADOW_ONLY",
            notification_id=notice.id, observed_at=observed, expires_at=expires,
        )
        db.add(row)
        observations.append(row)
    db.flush()
    return observations
