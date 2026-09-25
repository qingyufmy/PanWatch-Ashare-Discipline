"""Isolated paper-only fills from frozen portfolio signals.

This path never changes SignalEvent approval or writes ExecutionEvent.
Legacy strategy signals are deliberately excluded.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from math import floor, isfinite
from zoneinfo import ZoneInfo

from marketdata.vendors.tencent import fetch_raw
from sqlalchemy.orm import Session

from src.modules.strategy.backtest.cost_model import CostModel
from src.platform.scheduling import trading_calendar
from src.platform.persistence.models import (
    AppSettings, EvidenceSnapshot, ModelRun, PaperPortfolioFill, PaperPortfolioNav,
    PaperTradingAccount, PaperTradingPosition, PaperTradingTrade,
    SignalEvent, SignalPolicyDecision, PositionPlan,
)

SH = ZoneInfo("Asia/Shanghai")
COST = CostModel()
INDEX_CODES = ("sh000001", "sz399001", "sz399006", "sh000300")
VALID_SOURCES = {"daily_portfolio_plan", "intraday_portfolio_plan"}
MAX_PORTFOLIO_EXPOSURE = 0.60


def paper_mode(db: Session) -> bool:
    row = db.query(AppSettings).filter_by(key="portfolio_paper_mode").first()
    return bool(row and row.value == "paper_only")


def _asof(row: dict | None, now: datetime) -> datetime | None:
    try:
        raw = datetime.strptime(str(row["source_asof"]), "%Y%m%d%H%M%S").replace(tzinfo=SH)
        return raw if -30 <= (now - raw).total_seconds() <= 120 and raw.date() == now.date() else None
    except (TypeError, ValueError, KeyError):
        return None


def _lot(symbol: str) -> int:
    return 200 if symbol.startswith("688") else 100


def _sell_qty(action: str, hint: int, owned: int, sellable: int, symbol: str) -> int:
    if action == "EXIT":
        return owned if hint >= owned and sellable >= owned else 0
    if action != "REDUCE" or hint >= owned or hint > sellable:
        return 0
    # A partial sale uses whole lots; an odd-lot remainder is exited together.
    minimum = 200 if symbol.startswith("688") else 100
    return hint if hint >= minimum and hint % 100 == 0 else 0


def _buy_qty(signal: SignalEvent, *, price: float, cash: float,
             current_qty: int, equity: float, risk_on: bool,
             max_weight: float | None, stop: float | None,
             invested_value: float) -> int:
    if not risk_on or signal.action != "ADD" or not signal.target_weight:
        return 0
    target = float(signal.target_weight)
    try:
        ceiling = float(max_weight) if max_weight is not None else None
        floor_stop = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        return 0
    if (not isfinite(target) or target <= 0 or max_weight is None or
            ceiling is None or not isfinite(ceiling) or
            target > min(ceiling, 0.20) or
            floor_stop is None or not isfinite(floor_stop) or
            price <= floor_stop):
        return 0
    lot = _lot(signal.symbol)
    desired = max(0.0, target * equity - current_qty * price)
    quantity = floor(desired / (price * lot)) * lot
    quantity = min(quantity, floor(int(signal.qty_hint or 0) / lot) * lot)
    quantity = min(quantity, floor(max(0, MAX_PORTFOLIO_EXPOSURE * equity - invested_value)
                                   / (price * lot)) * lot)
    while quantity >= lot and -COST.fill("buy", price, quantity).cash_delta > cash:
        quantity -= lot
    return quantity if quantity >= lot else 0


def scan_portfolio_paper(db: Session, account: PaperTradingAccount, *,
                         now: datetime | None = None, quote_fetcher=fetch_raw) -> dict:
    """Mark positions and fill fresh, explicit signals once during CN continuous trading."""
    moment = (now or datetime.now(SH)).astimezone(SH)
    if not account.enabled or not paper_mode(db):
        return {"status": "disabled", "opened": 0, "closed": 0}
    if (trading_calendar._CN_TRADING_DATES is None or
            trading_calendar._CN_RANGE is None or
            not trading_calendar.is_trading_day("CN", moment.date())):
        return {"status": "calendar_unverified_or_closed", "opened": 0, "closed": 0}
    if not (time(9, 30) <= moment.time() < time(11, 30) or
                                     time(13, 0) <= moment.time() < time(14, 57)):
        return {"status": "outside_cn_continuous_session", "opened": 0, "closed": 0}
    day = moment.date().isoformat()
    utc_now = moment.astimezone(timezone.utc).replace(tzinfo=None)
    positions = (db.query(PaperTradingPosition)
                 .filter_by(stock_market="CN", status="open").all())
    signals = (db.query(SignalEvent)
               .filter(SignalEvent.trade_date == day,
                       SignalEvent.market == "CN",
                       SignalEvent.source.in_(VALID_SOURCES),
                       SignalEvent.action.in_(("ADD", "REDUCE", "EXIT")),
                       SignalEvent.status.in_(("APPROVED", "REVIEW_REQUIRED")),
                       SignalEvent.valid_from <= utc_now,
                       SignalEvent.expires_at > utc_now)
               .order_by(SignalEvent.generated_at.asc(), SignalEvent.signal_id.asc()).all())
    symbols = sorted({p.stock_symbol for p in positions} | {s.symbol for s in signals})
    requested = [*INDEX_CODES, *[("sh" if s.startswith(("6", "9")) else "sz") + s for s in symbols]]
    try:
        quote_rows = quote_fetcher(requested)
    except Exception as exc:
        return {"status": "quote_error", "error": type(exc).__name__, "opened": 0, "closed": 0}
    quotes = {str(q.get("symbol")): q for q in quote_rows if isinstance(q, dict)}
    index_rows = [quotes.get(code[2:]) for code in INDEX_CODES]
    fresh_indices = [q for q in index_rows if _asof(q, moment)]
    risk_on = len(fresh_indices) >= 3 and sum(float(q.get("change_pct") or 0) > 0 for q in fresh_indices) >= 3
    pos_by_symbol = {p.stock_symbol: p for p in positions}
    for p in positions:
        quote = quotes.get(p.stock_symbol)
        if _asof(quote, moment):
            try:
                price = float(quote["current_price"])
            except (TypeError, ValueError, KeyError):
                continue
            if isfinite(price) and price > 0:
                p.current_price = price
                p.highest_price = max(float(p.highest_price or 0), price)
                p.unrealized_pnl = round((price - p.entry_price) * p.quantity, 4)
    opened = closed = 0
    filled_ids = []
    for signal in signals:
        if db.get(PaperPortfolioFill, signal.signal_id):
            continue
        pos = pos_by_symbol.get(signal.symbol)
        if pos is None:
            continue  # This cohort only mirrors the held portfolio.
        quote = quotes.get(signal.symbol)
        asof = _asof(quote, moment)
        if not asof or not quote or not quote.get("volume"):
            continue
        try:
            price = float(quote["current_price"])
        except (TypeError, ValueError, KeyError):
            continue
        if not isfinite(price) or price <= 0 or not signal.qty_hint:
            continue
        evidence = db.get(EvidenceSnapshot, signal.evidence_snapshot_id)
        payload = evidence.payload if evidence and isinstance(evidence.payload, dict) else {}
        confidence = payload.get("confidence")
        model = db.get(ModelRun, payload.get("model_run_id")) if payload.get("model_run_id") else None
        if (payload.get("schema_valid") is not True or
                not isinstance(confidence, (float, int)) or confidence < 0.65 or
                not model or model.status != "OK" or model.schema_valid is not True):
            continue
        if signal.action == "ADD":
            auction_id = payload.get("auction_evidence_snapshot_id")
            auction = db.get(EvidenceSnapshot, auction_id) if isinstance(auction_id, int) else None
            if (not auction or auction.source != "portfolio_auction_context" or
                    not isinstance(auction.payload, dict) or auction.payload.get("trade_date") != day or
                    not any(q.get("symbol") == signal.symbol and q.get("quality") == "FRESH"
                            for q in auction.payload.get("holdings", []))):
                continue
        verdicts = db.query(SignalPolicyDecision).filter_by(signal_id=signal.signal_id).all()
        if len(verdicts) < 13 or any(v.decision in {"BLOCK", "EXPIRED"} for v in verdicts):
            continue
        day_buys = sum(f.quantity for f in db.query(PaperPortfolioFill).filter_by(
            trade_date=day, symbol=signal.symbol, action="ADD").all())
        sellable = max(0, pos.quantity - day_buys)
        equity = account.current_capital + sum(
            float(p.current_price or p.entry_price) * p.quantity
            for p in positions if p.status == "open")
        invested_value = equity - account.current_capital
        if signal.action == "ADD":
            plan = (db.query(PositionPlan).filter_by(market="CN", symbol=signal.symbol)
                    .order_by(PositionPlan.version.desc()).first())
            plan_data = plan.plan if plan and isinstance(plan.plan, dict) else {}
            position_rule = plan_data.get("position") or {}
            risk_rule = plan_data.get("risk") or {}
            qty = _buy_qty(signal, price=price, cash=account.current_capital,
                           current_qty=pos.quantity, equity=equity, risk_on=risk_on,
                           max_weight=position_rule.get("max_weight"),
                           stop=risk_rule.get("current_stop"), invested_value=invested_value)
            if qty <= 0:
                continue
            fill = COST.fill("buy", price, qty)
            old_qty = pos.quantity
            pos.entry_price = (pos.entry_price * old_qty - fill.cash_delta) / (old_qty + qty)
            pos.quantity += qty
            pos.current_price = price
            account.current_capital += fill.cash_delta
            opened += 1
        else:
            qty = _sell_qty(signal.action, int(signal.qty_hint), pos.quantity,
                            sellable, signal.symbol)
            if qty <= 0:
                continue
            fill = COST.fill("sell", price, qty)
            pnl = round(fill.cash_delta - pos.entry_price * qty, 4)
            db.add(PaperTradingTrade(
                stock_symbol=pos.stock_symbol, stock_market="CN", stock_name=pos.stock_name,
                quantity=qty, entry_price=pos.entry_price, exit_price=price,
                pnl=pnl, pnl_pct=round(100 * pnl / (pos.entry_price * qty), 2),
                exit_reason="portfolio_paper_" + signal.action.lower(),
                strategy_code="portfolio_paper_only", opened_at=pos.opened_at,
                closed_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
                holding_days=max(0, (moment.date() - pos.opened_at.replace(tzinfo=timezone.utc)
                                     .astimezone(SH).date()).days) if pos.opened_at else 0,
                meta={"portfolio_signal_id": signal.signal_id, "scope": "PAPER_ONLY"},
            ))
            pos.quantity -= qty
            if pos.quantity == 0:
                pos.status = "closed"
                pos.closed_at = moment.astimezone(timezone.utc).replace(tzinfo=None)
                pos.unrealized_pnl = 0.0
            account.current_capital += fill.cash_delta
            account.total_pnl += pnl
            account.total_trades += 1
            account.winning_trades += int(pnl > 0)
            closed += 1
        db.add(PaperPortfolioFill(
            signal_id=signal.signal_id, trade_date=day, symbol=signal.symbol,
            action=signal.action, quantity=qty, price=price,
            fees=round(fill.explicit_fees + fill.slippage_cost, 4),
            cash_delta=fill.cash_delta,
            quote_asof=asof.astimezone(timezone.utc).replace(tzinfo=None),
            filled_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
            policy_scope="PAPER_ONLY",
            details={"signal_status": signal.status, "model_run_id": model.run_id,
                     "source_truth_snapshot_id": signal.truth_snapshot_id,
                     "quote_source": "tencent_quote", "fill_assumption": "last_trade_with_cost_model",
                     "risk_on_indices": risk_on},
        ))
        filled_ids.append(signal.signal_id)
    equity = account.current_capital + sum(
        float(p.current_price or p.entry_price) * p.quantity
        for p in positions if p.status == "open")
    account.peak_capital = max(float(account.peak_capital or 0), equity)
    account.max_drawdown_pct = max(float(account.max_drawdown_pct or 0),
                                   100 * (account.peak_capital - equity) / account.peak_capital)
    db.commit()
    return {"status": "ok", "opened": opened, "closed": closed,
            "filled_signal_ids": filled_ids, "fresh_indices": len(fresh_indices),
            "risk_on": risk_on, "marked_positions": len(positions)}


def capture_paper_nav(db: Session, account: PaperTradingAccount, *,
                      now: datetime | None = None, quote_fetcher=fetch_raw) -> dict:
    """Freeze EOD paper equity only when every held close quote is source dated."""
    moment = (now or datetime.now(SH)).astimezone(SH)
    day = moment.date().isoformat()
    if not paper_mode(db) or moment.time() < time(15, 15):
        return {"status": "not_ready"}
    if (trading_calendar._CN_TRADING_DATES is None or
            trading_calendar._CN_RANGE is None or
            not trading_calendar.is_trading_day("CN", moment.date())):
        return {"status": "calendar_unverified_or_closed"}
    existing = db.get(PaperPortfolioNav, day)
    if existing:
        return {"status": "already_captured", "equity": existing.equity}
    positions = db.query(PaperTradingPosition).filter_by(stock_market="CN", status="open").all()
    requested = [("sh" if p.stock_symbol.startswith(("6", "9")) else "sz") + p.stock_symbol
                 for p in positions]
    try:
        raw = quote_fetcher(requested) if requested else []
    except Exception as exc:
        return {"status": "quote_error", "error": type(exc).__name__}
    quotes = {str(q.get("symbol")): q for q in raw if isinstance(q, dict)}
    marks = []
    for pos in positions:
        quote = quotes.get(pos.stock_symbol)
        try:
            stamp = datetime.strptime(str(quote["source_asof"]), "%Y%m%d%H%M%S").replace(tzinfo=SH)
            price = float(quote["current_price"])
        except (TypeError, ValueError, KeyError):
            return {"status": "close_quote_unverified", "symbol": pos.stock_symbol}
        if stamp.date().isoformat() != day or stamp.time() < time(15, 0) or not isfinite(price) or price <= 0:
            return {"status": "close_quote_unverified", "symbol": pos.stock_symbol}
        marks.append((pos, price, stamp))
    for pos, price, _ in marks:
        pos.current_price = price
        pos.highest_price = max(float(pos.highest_price or 0), price)
        pos.unrealized_pnl = round((price - pos.entry_price) * pos.quantity, 4)
    market_value = sum(price * pos.quantity for pos, price, _ in marks)
    equity = account.current_capital + market_value
    account.peak_capital = max(float(account.peak_capital or 0), equity)
    account.max_drawdown_pct = max(float(account.max_drawdown_pct or 0),
                                   100 * (account.peak_capital - equity) / account.peak_capital)
    db.add(PaperPortfolioNav(
        trade_date=day, cash=account.current_capital,
        market_value=round(market_value, 4), equity=round(equity, 4),
        source_asof_min=min((stamp for _, _, stamp in marks), default=moment)
                           .astimezone(timezone.utc).replace(tzinfo=None),
        captured_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
        position_count=len(marks), source="tencent_close_quote",
    ))
    db.commit()
    return {"status": "captured", "trade_date": day, "equity": round(equity, 2),
            "cash": round(account.current_capital, 2), "position_count": len(marks)}
