"""Source-backed preparation scenarios; these never authorize a trade."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

from src.platform.scheduling.trading_calendar import previous_confirmed_cn_trading_day

VERSION = "premarket-preparation-v1"


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _reference(quote: dict, previous: str | None) -> bool:
    """Re-evaluate frozen legacy quotes without changing their stored evidence."""
    if not previous or not _positive(quote.get("price")):
        return False
    try:
        stamp = datetime.fromisoformat(quote["source_asof"])
        return stamp.tzinfo is not None and stamp.date().isoformat() == previous and stamp.hour >= 15
    except (KeyError, TypeError, ValueError):
        return False


def build_briefing(*, market: dict, positions: list[dict], limits: list[dict],
                   nav: float | None, truth_source: str, created_at: datetime | None = None) -> dict:
    """Compute auditable scenario weights from previous-session facts and saved limits.

    A 25% relative reduction is a review scenario for weak/over-limit positions,
    not an immediate sell. ADD requires a later, independently approved signal.
    """
    prior = previous_confirmed_cn_trading_day(date.fromisoformat(market["trade_date"]))
    previous = prior.isoformat() if prior else None
    indices = [q for q in market.get("indices", []) if _reference(q, previous)
               and isinstance(q.get("change_pct"), (float, int))]
    negative = sum(q["change_pct"] < 0 for q in indices)
    positive = sum(q["change_pct"] > 0 for q in indices)
    tone = ("DEFENSIVE" if len(indices) >= 3 and negative >= 3 else
            "CONSTRUCTIVE" if len(indices) >= 3 and positive >= 3 else
            "MIXED" if len(indices) >= 3 else "DATA_INCOMPLETE")
    labels = {"DEFENSIVE": "上一交易日主要指数普跌，盘前以防守预案为主",
              "CONSTRUCTIVE": "上一交易日主要指数普涨，开盘确认后再考虑增强仓位",
              "MIXED": "上一交易日指数分化，优先比较个股相对强弱",
              "DATA_INCOMPLETE": "上一交易日指数证据不完整，暂不给组合目标区间"}
    average = sum(q["change_pct"] for q in indices) / len(indices) if indices else None
    quotes = {q.get("symbol"): q for q in market.get("holdings", [])}
    rules = {p["symbol"]: p for p in limits}
    denominator = _positive(nav)
    rows = []
    for position in positions:
        symbol = position["symbol"]
        quote, rule = quotes.get(symbol, {}), rules.get(symbol, {})
        valid = _reference(quote, previous)
        close = _positive(quote.get("price")) if valid else None
        weight = close * position["total_qty"] / denominator if close and denominator else None
        limit = _positive(rule.get("max_weight"))
        limit = limit if limit and limit <= 1 else None
        change = quote.get("change_pct") if valid else None
        relative = change - average if isinstance(change, (float, int)) and average is not None else None
        overweight = weight is not None and limit is not None and weight > limit
        weak = relative is not None and relative <= -2
        target = weight
        if target is not None:
            if tone == "DEFENSIVE" and weak:
                target *= .75
            if limit is not None:
                target = min(target, limit)
        stance = ("数据待核查" if not valid or weight is None else
                  "优先复核减仓条件" if overweight or (tone == "DEFENSIVE" and weak) else
                  "强势观察，确认后再加" if relative is not None and relative >= 2 else "观察开盘承接")
        low, high = _positive(quote.get("low_price")), _positive(quote.get("high_price"))
        if not valid or (low and high and not low <= close <= high):
            low = high = None
        low_text = f"上一交易日低点 {low:.2f} 元" if low else "首个已收线 5 分钟低点（开盘后获取）"
        high_text = f"上一交易日高点 {high:.2f} 元" if high else "首个已收线 5 分钟高点（开盘后获取）"
        rows.append({
            "symbol": symbol, "name": position.get("name", symbol), "stance": stance,
            "reference_close": close, "reference_asof": quote.get("source_asof") if valid else None,
            "reference_low": low, "reference_high": high,
            "current_weight": round(weight, 6) if weight is not None else None,
            "conditional_target_weight": round(target, 6) if target is not None and tone != "DATA_INCOMPLETE" else None,
            "suggested_weight_limit": limit, "plan_version": rule.get("plan_version"),
            "relative_change_pp": round(relative, 2) if relative is not None else None,
            "reduce_condition": f"09:30 后已收线 5 分钟跌破{low_text}且未收回，结合最新风险信号复核减仓。",
            "add_condition": f"09:30 后已收线 5 分钟突破{high_text}并回踩守住，同时个股站上当日 VWAP、至少两个主要指数转强；另核对可用现金、单股及组合额度。",
            "invalidation": "竞价不足以确认成交；行情过期、板块背离或突破回落则取消加仓预案，等新的盘中信号。",
        })
    complete = bool(rows) and all(r["current_weight"] is not None for r in rows)
    current = sum(r["current_weight"] for r in rows) if complete else None
    lower = sum(r["conditional_target_weight"] for r in rows) if complete and tone != "DATA_INCOMPLETE" else None
    coverage_ok = current is not None and current <= 1 and lower is not None and lower <= 1
    dated = [q for q in market.get("global_tech", []) if q.get("quality") == "DATED_REFERENCE"]
    tech_facts = "；".join(f"{q['name']} {q['change_pct']:+.2f}%（{q.get('source_session_date')}）"
                          for q in dated if isinstance(q.get("change_pct"), (float, int)))
    global_summary = ((tech_facts + "。仅作海外已报行情背景，时区尚未交叉核实，不能据此认定实时科技共振。")
                      if tech_facts else "全球科技来源时间未核实，暂不判断共振；不以此阻止 A 股上一交易日数据形成条件预案。")
    return {
        "version": VERSION, "scope": "CONDITIONAL_RESEARCH_ONLY", "created_at":
            (created_at or datetime.now(timezone.utc)).isoformat(),
        "trade_date": market["trade_date"], "previous_cn_session": previous,
        "market_summary": labels[tone], "risk_tone": tone,
        "index_facts": [{k: q.get(k) for k in ("name", "change_pct", "source_asof")} for q in indices],
        "breadth_summary": "全市场宽度缺失；候选池及持仓涨跌比例仅反映各自样本，不代表大盘。",
        "global_summary": global_summary,
        "exposure": {"current_weight": current, "suggested_min": lower if coverage_ok else None,
                     "suggested_max": current if coverage_ok else None,
                     "basis": "用户声明总资产与持仓 × 上一交易日收盘价；非券商核对资金",
                     "truth_source": truth_source,
                     "rule": "弱势相对指数落后至少2个百分点的股票，条件成立时试算减少该股25%仓位；超已有建议上限者以其上限试算。区间为情景建议，不是强制仓位或立即卖出指令。"},
        "positions": rows,
        "checkpoints": ["09:25 核对竞价价格与来源时间，识别高低开，暂不把竞价当作成交确认。",
                        "09:35 后可人工核对已收线 5 分钟、当日 VWAP 和板块强弱；条件触发不等于已成交。",
                        "10:00 / 13:30 批量重评组合仓位，使用当时的新行情与模型结果。"],
        "execution_boundary": "预案不生成已批准交易。加仓须另有明确、未过期的盘中信号；实盘由用户确认，PAPER_ONLY 独立门禁执行。",
    }


def briefing_for_plan(db, plan, market: dict) -> dict | None:
    """Legacy display correction is labelled as a new view, never rewritten history."""
    from src.platform.persistence.models import PortfolioTruthSnapshot, PositionPlan
    if (plan.payload or {}).get("preparation"):
        return plan.payload["preparation"]
    truth = db.get(PortfolioTruthSnapshot, plan.truth_snapshot_id)
    if not truth or not market:
        return None
    positions = [{"symbol": p.symbol, "name": p.name, "total_qty": p.total_qty} for p in truth.positions]
    limits = []
    for p in positions:
        rule = (db.query(PositionPlan).filter_by(market="CN", symbol=p["symbol"])
                .filter(PositionPlan.created_at <= plan.created_at)
                .order_by(PositionPlan.version.desc()).first())
        limits.append({"symbol": p["symbol"], "max_weight": ((rule.plan or {}).get("position") or {}).get("max_weight") if rule else None,
                       "plan_version": rule.version if rule else None})
    result = build_briefing(market=market, positions=positions, limits=limits, nav=truth.nav, truth_source=truth.source)
    result["origin"] = "DERIVED_VIEW_OF_FROZEN_PLAN"
    result["source_plan_id"] = plan.id
    return result
