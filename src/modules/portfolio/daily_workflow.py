"""P7 trading-day workflow with durable, idempotent slot runs.

The workflow generates proposals and review evidence only. It never places orders.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from src.modules.portfolio.discipline import capture_truth, logical_hash
from src.modules.portfolio.issue_ledger import record_issue
from src.modules.portfolio.minute_features import feature_snapshot, fetch_minute_bars, write_parquet
from src.modules.portfolio.notifications import send_portfolio_notice
from src.modules.portfolio.model_router import probe_role
from src.modules.portfolio.prompt_registry import run_portfolio_prompt
from src.modules.portfolio.signal_journal import record_signal
from src.modules.portfolio.policy_gate import evaluate_signal
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    DailyPortfolioPlan, ExecutionEvent, ModelProfile, ModelRun, NewsCache,
    NextDayAction, PortfolioTruthSnapshot, PortfolioWorkflowRun,
    PositionPlan, SignalEvent,
)
from src.platform.scheduling import trading_calendar
from src.platform.scheduling.trading_calendar import next_confirmed_cn_trading_day

SH = ZoneInfo("Asia/Shanghai")
MINUTE_ROOT = Path(__file__).resolve().parents[3] / "data" / "minute_bars"
_QUOTE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portfolio-quote")
_MINUTE_VERIFY_POOL = ThreadPoolExecutor(max_workers=20, thread_name_prefix="portfolio-stop-verify")
FIXED_STEPS = (
    ("HEALTH_PROBE", "06:50"), ("DATA_WARMUP", "07:20"),
    ("OVERNIGHT_INTAKE", "07:30"), ("RISK_SCAN", "08:00"),
    ("DEEP_REVIEW", "08:20"), ("PREMARKET_PLAN", "08:50"),
    ("AUCTION_MODE", "09:15"), ("OPEN_CONFIRM", "09:25"),
    ("MIDDAY_REVIEW", "11:35"), ("NOON_REFRESH", "12:50"),
    ("CLOSING_RISK", "14:30"), ("EXECUTION_AUDIT", "14:50"),
    ("EOD_TRUTH", "15:05"), ("DAILY_REVIEW", "15:30"),
    ("EXCEPTION_REVIEW", "18:10"), ("EVENING_EVENTS", "20:30"),
    ("EVENING_DEEP", "20:45"), ("NEXT_DAY_DRAFT", "21:10"),
    ("P10_REVIEW", "21:20"),
)
MONITOR_STARTS = ((time(9, 30), time(11, 30)), (time(13, 0), time(14, 30)))


def _now_sh(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(SH)


def _latest_truth(db: Session, trade_date: str | None = None) -> PortfolioTruthSnapshot | None:
    query = db.query(PortfolioTruthSnapshot)
    if trade_date:
        query = query.filter_by(trade_date=trade_date)
    return query.order_by(PortfolioTruthSnapshot.id.desc()).first()


def _ensure_today_truth(db: Session, trade_date: str) -> PortfolioTruthSnapshot | None:
    truth = _latest_truth(db, trade_date)
    if truth is None and trade_date == datetime.now(SH).date().isoformat():
        truth = capture_truth(db, phase="PREMARKET", sellable_equals_quantity=True)
    return truth


def _positions(truth: PortfolioTruthSnapshot | None) -> list[dict]:
    return [{"market": p.market, "symbol": p.symbol, "name": p.name,
             "total_qty": p.total_qty, "sellable_qty": p.sellable_qty,
             "avg_cost": p.avg_cost} for p in truth.positions] if truth else []


def _status(reason: str, **data) -> dict:
    return {"status": "REVIEW", "reason": reason, **data}


async def _health_probe(day: str, db_factory) -> dict:
    with db_factory() as db:
        roles = [p.role for p in db.query(ModelProfile).filter_by(enabled=True).all()]
    fast = await probe_role("FAST", db_factory=db_factory)
    calendar_confirmed = trading_calendar._CN_TRADING_DATES is not None
    return {"status": "SUCCEEDED" if fast["status"] == "OK" and calendar_confirmed else "REVIEW",
            "model_probe": fast, "enabled_roles": roles,
            "calendar_confirmed": calendar_confirmed}


async def _data_warmup(day: str, db_factory) -> dict:
    with db_factory() as db:
        truth = capture_truth(db, phase="PREMARKET", sellable_equals_quantity=True)
        positions = _positions(truth)
        return {"status": "REVIEW", "reason": "USER_ATTESTED_UNRECONCILED",
                "truth_snapshot_id": truth.id, "position_count": len(positions),
                "truth_status": truth.truth_status}


async def _news_intake(day: str, db_factory, window: str) -> dict:
    with db_factory() as db:
        truth = _ensure_today_truth(db, day)
        symbols = {p.symbol for p in truth.positions} if truth else set()
        cutoff = datetime.fromisoformat(day) - timedelta(days=1)
        rows = db.query(NewsCache).filter(NewsCache.publish_time >= cutoff).order_by(NewsCache.publish_time.desc()).limit(300).all()
        matched = [r for r in rows if symbols.intersection(r.symbols or [])]
        return {"status": "SUCCEEDED" if matched else "REVIEW", "window": window,
                "news_ids": [r.id for r in matched], "matched_count": len(matched),
                "reason": None if matched else "NO_VERIFIED_NEWS_EVIDENCE"}


def _risk_scan(day: str, db_factory, *, asof: datetime | None = None,
               quote_fetcher=None, minute_fetcher=fetch_minute_bars) -> dict:
    with db_factory() as db:
        truth = _ensure_today_truth(db, day)
        if truth is None:
            return _status("TRUTH_SNAPSHOT_MISSING", coverage=0)
        market_now = asof.astimezone(SH) if asof else None
        session_open = bool(market_now and (quote_fetcher is not None or market_now.date() == datetime.now(SH).date()) and
                            (time(9, 30) <= market_now.time() <= time(11, 30) or
                             time(13, 0) <= market_now.time() <= time(15, 0)))
        quotes = {}
        quote_error = None
        if session_open:
            try:
                from marketdata.vendors.tencent import fetch_raw
                symbols = [_vendor_symbol(p.symbol) for p in truth.positions]
                future = _QUOTE_POOL.submit(quote_fetcher or fetch_raw, symbols)
                quotes = {r["symbol"]: r for r in future.result(timeout=8)}
            except Exception as exc:
                quote_error = type(exc).__name__
        rows = []
        pending_confirmations = {}
        for p in truth.positions:
            plan = (db.query(PositionPlan).filter_by(market=p.market, symbol=p.symbol)
                    .order_by(PositionPlan.version.desc()).first())
            risk = (plan.plan or {}).get("risk", {}) if plan else {}
            stop = risk.get("current_stop") if isinstance(risk, dict) else None
            bars_path = MINUTE_ROOT / f"trade_date={day}" / f"symbol={_vendor_symbol(p.symbol)}" / "bars.parquet"
            price = market_asof = None
            source = None
            quote = quotes.get(p.symbol)
            if quote and quote.get("source_asof"):
                try:
                    stamp = datetime.strptime(quote["source_asof"], "%Y%m%d%H%M%S").replace(tzinfo=SH)
                    price, market_asof, source = float(quote["current_price"]), stamp.isoformat(), "tencent_quote"
                except (ValueError, TypeError):
                    pass
            if bars_path.exists():
                from src.modules.portfolio.minute_features import read_parquet
                bars = read_parquet(bars_path)
                if not bars.empty and market_asof is None:
                    price = float(bars.close.iloc[-1])
                    market_asof = str(bars.timestamp.iloc[-1])
                    source = "minute_archive"
            fresh = False
            if market_asof and asof:
                stamp = datetime.fromisoformat(market_asof).astimezone(timezone.utc)
                fresh = -30 <= (asof.astimezone(timezone.utc) - stamp).total_seconds() <= 120
            if fresh and stop is not None and price is not None:
                color = "RED" if price <= float(stop) else "GREEN"
            else:
                color = "YELLOW"
            reason = {"RED": "HARD_STOP", "GREEN": "ABOVE_STOP",
                      "YELLOW": "STOP_OR_FRESH_PRICE_UNVERIFIED"}[color]
            rows.append({"symbol": p.symbol, "color": color, "reason": reason,
                         "price": price, "market_data_asof": market_asof, "market_data_source": source,
                         "current_stop": stop, "sellable_qty": p.sellable_qty})
            if color == "RED" and source == "tencent_quote":
                pending_confirmations[len(rows) - 1] = _MINUTE_VERIFY_POOL.submit(
                    minute_fetcher, _vendor_symbol(p.symbol), day,
                    sources=("sina",), timeout_seconds=8)
        if pending_confirmations:
            done, _ = wait(pending_confirmations.values(), timeout=8)
            for index, future in pending_confirmations.items():
                confirmed = False
                if future in done:
                    try:
                        frame, _ = future.result()
                        check_stamp = frame.timestamp.iloc[-1]
                        check_fresh = -30 <= (asof.astimezone(timezone.utc) - check_stamp.astimezone(timezone.utc)).total_seconds() <= 120
                        price = rows[index]["price"]
                        confirmed = check_fresh and abs(float(frame.close.iloc[-1]) - price) <= max(.02, price * .003)
                    except Exception:
                        pass
                if not confirmed:
                    future.cancel()
                    rows[index]["color"] = "YELLOW"
                    rows[index]["reason"] = "STOP_PRICE_CROSS_SOURCE_UNVERIFIED"
        return {"status": "REVIEW" if any(r["color"] == "YELLOW" for r in rows) else "SUCCEEDED",
                "coverage": len(rows), "red": sum(r["color"] == "RED" for r in rows),
                "yellow": sum(r["color"] == "YELLOW" for r in rows), "positions": rows,
                "quote_error": quote_error}


def _vendor_symbol(symbol: str) -> str:
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("invalid_cn_symbol")
    return ("sh" if symbol.startswith(("6", "9")) else "bj" if symbol.startswith(("4", "8")) else "sz") + symbol


async def _feature_refresh(day: str, db_factory, now: datetime) -> dict:
    with db_factory() as db:
        truth = _ensure_today_truth(db, day)
        positions = _positions(truth)
    if not positions:
        return _status("TRUTH_SNAPSHOT_MISSING", coverage=0)

    async def one(position):
        symbol = _vendor_symbol(position["symbol"])
        try:
            frame, failures = await asyncio.to_thread(fetch_minute_bars, symbol, day)
            path = write_parquet(frame, MINUTE_ROOT)
            feature = feature_snapshot(frame)
            market_asof = datetime.fromisoformat(feature["market_data_asof"])
            age = (now.astimezone(SH) - market_asof).total_seconds()
            status = "OK" if -30 <= age <= 120 and not feature["missing_minutes"] else "STALE"
            if status == "STALE":
                with db_factory() as db:
                    record_issue(db, category="DATA_STALE", code="MINUTE_FRESHNESS", source=feature["market_data_source"],
                                 title="Minute data stale or incomplete",
                                 context={"symbol": position["symbol"], "asof": feature["market_data_asof"],
                                          "age_seconds": int(age), "missing": len(feature["missing_minutes"])})
            return {"symbol": position["symbol"], "status": status, "path": str(path),
                    "asof": feature["market_data_asof"], "bar_count": feature["bar_count"],
                    "missing_count": len(feature["missing_minutes"]),
                    "source_failures": failures}
        except Exception as exc:
            with db_factory() as db:
                record_issue(db, category="DATA_MISSING", code=type(exc).__name__, source="minute_bars",
                             title="Minute bars unavailable", context={"symbol": position["symbol"], "detail": str(exc)[:150]})
            return {"symbol": position["symbol"], "status": "FAILED", "error": type(exc).__name__}

    results = await asyncio.gather(*(one(p) for p in positions))
    return {"status": "SUCCEEDED" if all(r["status"] == "OK" for r in results) else "REVIEW",
            "coverage": len(results), "ok": sum(r["status"] == "OK" for r in results),
            "results": results}


async def _premarket_plan(day: str, db_factory, now: datetime | None = None) -> dict:
    with db_factory() as db:
        truth = _ensure_today_truth(db, day)
        positions = _positions(truth)
        if not positions:
            return _status("TRUTH_SNAPSHOT_MISSING", coverage=0)
        evidence = {"portfolio_truth_status": truth.truth_status,
                    "market_data_status": "PREMARKET_UNVERIFIED",
                    "truth_snapshot_id": truth.id}
    payload = {"trade_date": day, "positions": positions, "evidence": evidence}
    try:
        proposal, model = await run_portfolio_prompt("flash", payload, db_factory=db_factory)
    except Exception as exc:
        with db_factory() as db:
            record_issue(db, category="MODEL_SCHEMA_ERROR" if isinstance(exc, ValueError) else "MODEL_TIMEOUT",
                         code=type(exc).__name__, source="premarket_plan",
                         title="Portfolio batch proposal failed", context={"detail": str(exc)[:150]})
        return _status("BATCH_MODEL_FAILED", error=type(exc).__name__)
    frozen = proposal.model_dump(mode="json")
    with db_factory() as db:
        model_run = db.get(ModelRun, model.run_id)
        prompt_version = model_run.prompt_version if model_run else None
        existing = db.query(DailyPortfolioPlan).filter_by(trade_date=day).order_by(DailyPortfolioPlan.version.desc()).first()
        version = (existing.version + 1) if existing else 1
        plan = DailyPortfolioPlan(trade_date=day, version=version, status="REVIEW_ONLY",
                                  truth_snapshot_id=truth.id, model_run_id=model.run_id,
                                  prompt_id="flash", prompt_version=prompt_version,
                                  input_hash=logical_hash(payload), output_hash=logical_hash(frozen),
                                  payload=frozen)
        db.add(plan)
        db.commit()
        signal_ids = []
        for action in proposal.proposals:
            evidence_payload = {"schema_valid": True, "model_conflict": False,
                                "market_data_source": None, "market_data_asof": None,
                                "evidence_refs": action.evidence_refs,
                                "rationale": action.rationale,
                                "meta": {"quote": {"current_price": None}}}
            signal, created = record_signal(
                db, market=action.market, symbol=action.symbol, action=action.action,
                source="daily_portfolio_plan", source_agent="portfolio_flash",
                evidence=evidence_payload, ttl_seconds=3600,
                generated_at=now,
                qty_hint=action.qty_hint, target_weight=action.target_weight,
                prompt_id="flash", prompt_version=prompt_version, model_role=model.requested_role,
                requested_model=model.requested_model, reported_model=model.reported_model,
            )
            if created:
                evaluate_signal(db, signal.signal_id, now=now)
            signal_ids.append(signal.signal_id)
        result = {"status": "REVIEW", "reason": "USER_ATTESTED_AND_PREMARKET_DATA_UNVERIFIED",
                  "daily_plan_id": plan.id, "daily_plan_version": version,
                  "model_run_id": model.run_id, "coverage": len(proposal.proposals),
                  "signal_ids": signal_ids}
    lines = [f"{p.symbol} {p.action}（待复核）" for p in proposal.proposals]
    result["notification"] = await send_portfolio_notice(
        key=f"plan:{day}", title=f"A股持仓盘前计划 {day}",
        content=f"覆盖 {len(lines)} 只持仓；账户与盘前行情仍待核对，仅供人工参考。\n" + "\n".join(lines),
        db_factory=db_factory, timeout_seconds=12)
    return result


async def _model_review(day: str, db_factory, prompt_id: str, phase: str, evidence: dict) -> dict:
    with db_factory() as db:
        truth = _ensure_today_truth(db, day)
        positions = _positions(truth)
    if not positions:
        return _status("TRUTH_SNAPSHOT_MISSING", coverage=0)
    payload = {"trade_date": day, "positions": positions,
               "evidence": {"phase": phase, "truth_status": truth.truth_status, **evidence}}
    try:
        proposal, model = await run_portfolio_prompt(prompt_id, payload, db_factory=db_factory)
        actions = {kind: sum(p.action == kind for p in proposal.proposals)
                   for kind in ("OPEN", "ADD", "HOLD", "REDUCE", "EXIT")}
        return {"status": "REVIEW", "reason": "MODEL_REVIEW_REQUIRES_HUMAN_CONFIRMATION",
                "model_run_id": model.run_id, "coverage": len(proposal.proposals),
                "action_counts": actions, "portfolio_rationale": proposal.portfolio_rationale}
    except Exception as exc:
        with db_factory() as db:
            record_issue(db, category="MODEL_SCHEMA_ERROR" if isinstance(exc, ValueError) else "MODEL_TIMEOUT",
                         code=type(exc).__name__, source=phase,
                         title=f"Portfolio {phase} model review failed",
                         context={"detail": str(exc)[:150]})
        return _status("MODEL_REVIEW_FAILED", error=type(exc).__name__)


async def _simple_step(step: str, day: str, db_factory, now: datetime) -> dict:
    if step == "P10_REVIEW":
        import json
        from src.modules.portfolio.review_gate import review_snapshot

        next_day = next_confirmed_cn_trading_day(date.fromisoformat(day))
        cadences = ["DAILY"]
        if now.weekday() == 4:
            cadences.append("WEEKLY")
            if now.isocalendar().week % 2 == 0:
                cadences.append("BIWEEKLY")
        if next_day and next_day.month != now.month:
            cadences.append("MONTHLY")
        reports = {}
        root = Path(__file__).resolve().parents[3] / "data" / "reviews" / day
        root.mkdir(parents=True, exist_ok=True)
        for cadence in cadences:
            days = {"DAILY": 1, "WEEKLY": 7, "BIWEEKLY": 14, "MONTHLY": 31}[cadence]
            since = (now.astimezone(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
            with db_factory() as db:
                report = review_snapshot(db, since=since, cadence=cadence)
            target = root / f"{cadence.lower()}.json"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            reports[cadence] = {"path": str(target), "gate": report["gate"]["status"]}
        if "WEEKLY" in cadences or "MONTHLY" in cadences:
            from src.modules.portfolio.upstream_watch import collect
            target = root / "upstream_watch.json"
            previous_path = root.parent / "upstream_last.json"
            previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else None
            with db_factory() as db:
                upstream = collect(db, previous=previous)
            target.write_text(json.dumps(upstream, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if any(r.get("sha") for r in upstream["repositories"]):
                previous_path.write_text(json.dumps(upstream, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            reports["UPSTREAM"] = {"path": str(target), "changed": upstream["changed_repositories"]}
        return {"status": "REVIEW", "reports": reports,
                "reason": "PROD_REQUIRES_NATURAL_DAY_EVIDENCE_AND_HUMAN_APPROVAL"}
    if step in {"OVERNIGHT_INTAKE", "NOON_REFRESH", "EVENING_EVENTS"}:
        return await _news_intake(day, db_factory, step)
    if step in {"RISK_SCAN", "AUCTION_MODE", "OPEN_CONFIRM", "CLOSING_RISK", "HARD_RISK"}:
        result = _risk_scan(day, db_factory, asof=now)
        red = [p for p in result.get("positions", []) if p.get("color") == "RED"]
        if red and step in {"HARD_RISK", "RISK_SCAN", "OPEN_CONFIRM", "CLOSING_RISK"}:
            symbols = ",".join(sorted(p["symbol"] for p in red))
            lines = [f"{p['symbol']} 现价 {p['price']:.2f}，观察价 {float(p['current_stop']):.2f}"
                     for p in red]
            result["notification"] = await send_portfolio_notice(
                key=f"hard-risk:{day}:{symbols}", title=f"A股持仓风险提醒 {day}",
                content="以下持仓触及停止观察价，请人工核对实时行情及可卖数量；系统未下单。\n" + "\n".join(lines),
                db_factory=db_factory, timeout_seconds=5)
        return result
    if step == "FEATURE_REFRESH":
        return await _feature_refresh(day, db_factory, now)
    if step == "PREMARKET_PLAN":
        return await _premarket_plan(day, db_factory, now)
    if step == "HEALTH_PROBE":
        return await _health_probe(day, db_factory)
    if step == "DATA_WARMUP":
        return await _data_warmup(day, db_factory)
    if step == "EOD_TRUTH":
        with db_factory() as db:
            truth = capture_truth(db, phase="EOD", sellable_equals_quantity=True)
            return _status("USER_ATTESTED_UNRECONCILED", eod_snapshot_id=truth.id,
                           coverage=len(truth.positions))
    if step == "EXECUTION_AUDIT":
        with db_factory() as db:
            pending = (db.query(SignalEvent).filter_by(trade_date=day)
                       .filter(SignalEvent.action.in_(("REDUCE", "EXIT")))
                       .filter(SignalEvent.status.notin_(("EXECUTED", "CANCELLED"))).all())
            return {"status": "REVIEW" if pending else "SUCCEEDED",
                    "pending_signal_ids": [p.signal_id for p in pending],
                    "next_day_open": db.query(NextDayAction).filter_by(status="OPEN").count()}
    if step in {"MIDDAY_REVIEW", "DAILY_REVIEW", "EXCEPTION_REVIEW", "NEXT_DAY_DRAFT", "WEEKLY_REVIEW"}:
        with db_factory() as db:
            signals = db.query(SignalEvent).filter_by(trade_date=day).all()
            executions = db.query(ExecutionEvent).filter(ExecutionEvent.signal_id.in_([s.signal_id for s in signals])).all() if signals else []
            if step == "NEXT_DAY_DRAFT":
                due = next_confirmed_cn_trading_day(date.fromisoformat(day))
                for signal in signals:
                    if signal.action not in {"REDUCE", "EXIT"} or signal.status in {"EXECUTED", "CANCELLED"} or not signal.qty_hint:
                        continue
                    exists = db.query(NextDayAction).filter_by(signal_id=signal.signal_id,
                                                                reason="UNRESOLVED_SELL_REVIEW").first()
                    if not exists:
                        db.add(NextDayAction(signal_id=signal.signal_id, market=signal.market,
                                             symbol=signal.symbol, due_trade_date=due.isoformat() if due else "UNRESOLVED",
                                             pending_qty=signal.qty_hint, reason="UNRESOLVED_SELL_REVIEW",
                                             status="REVIEW_REQUIRED" if due else "CALENDAR_REQUIRED"))
                db.commit()
            next_day = db.query(NextDayAction).filter(NextDayAction.status.in_(("OPEN", "REVIEW_REQUIRED", "CALENDAR_REQUIRED"))).all()
            evidence = {"signal_count": len(signals), "execution_count": len(executions),
                        "next_day_action_ids": [a.id for a in next_day],
                        "unresolved_signals": [s.signal_id for s in signals if s.status not in {"EXECUTED", "CANCELLED"}]}
        if step == "NEXT_DAY_DRAFT":
            return {"status": "REVIEW", "reason": "NEXT_DAY_ACTIONS_REQUIRE_HUMAN_CONFIRMATION", **evidence}
        if step == "EXCEPTION_REVIEW" and not any(s.status == "POLICY_REJECTED" for s in signals):
            return {"status": "SUCCEEDED", "reason": "NO_POLICY_EXCEPTION", **evidence}
        prompt_id = "deep" if step in {"EXCEPTION_REVIEW", "WEEKLY_REVIEW"} else "review"
        return await _model_review(day, db_factory, prompt_id, step, evidence)
    if step in {"DEEP_REVIEW", "EVENING_DEEP"}:
        risk = _risk_scan(day, db_factory, asof=now)
        with db_factory() as db:
            major_news = db.query(NewsCache).filter(NewsCache.importance >= 2,
                                                   NewsCache.publish_time >= datetime.fromisoformat(day)).count()
        if risk.get("red", 0) or (step == "EVENING_DEEP" and major_news):
            return await _model_review(day, db_factory, "deep", step,
                                       {"red": risk.get("red", 0), "major_news": major_news})
        return {"status": "REVIEW" if risk.get("yellow", 0) else "SUCCEEDED",
                "reason": "VERIFIED_MAJOR_EVENT_ABSENT" if not major_news else "RISK_EVIDENCE_UNVERIFIED",
                "red": risk.get("red", 0), "yellow": risk.get("yellow", 0), "major_news": major_news}
    raise ValueError(f"unknown_workflow_step:{step}")


async def run_step(step: str, *, now: datetime | None = None,
                   db_factory: Callable[[], Session] = SessionLocal,
                   handler: Callable[[str, str, Callable, datetime], Awaitable[dict]] | None = None,
                   calendar_check: Callable[[date], bool] | None = None) -> dict:
    """Claim one slot first; repeat calls return its frozen result."""
    moment = _now_sh(now)
    day = moment.date().isoformat()
    slot = moment.strftime("%H:%M") if step in {"HARD_RISK", "FEATURE_REFRESH"} else "DAILY"
    calendar_ok = calendar_check(moment.date()) if calendar_check else (
        trading_calendar._CN_TRADING_DATES is not None and trading_calendar.is_trading_day("CN", moment.date())
    )
    with db_factory() as db:
        existing = db.query(PortfolioWorkflowRun).filter_by(trade_date=day, step=step, slot=slot).first()
        if existing:
            return {"run_id": existing.run_id, "status": existing.status, **(existing.payload or {})}
        run = PortfolioWorkflowRun(
            run_id=uuid.uuid4().hex, trade_date=day, step=step, slot=slot,
            status="RUNNING", payload={},
            started_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.query(PortfolioWorkflowRun).filter_by(trade_date=day, step=step, slot=slot).one()
            return {"run_id": winner.run_id, "status": winner.status, **(winner.payload or {})}
        run_id = run.run_id
    if not calendar_ok:
        unknown = calendar_check is None and trading_calendar._CN_TRADING_DATES is None
        result = {"status": "REVIEW" if unknown else "SKIPPED",
                  "reason": "CALENDAR_UNVERIFIED" if unknown else "NON_TRADING_DAY"}
        if unknown:
            with db_factory() as db:
                record_issue(db, category="DATA_MISSING", code="CALENDAR_UNVERIFIED",
                             source="trading_calendar", title="Portfolio workflow calendar unavailable",
                             context={"trade_date": day, "step": step})
    else:
        try:
            result = await (handler(step, day, db_factory, moment) if handler else
                            _simple_step(step, day, db_factory, moment))
        except Exception as exc:
            result = {"status": "FAILED", "error": type(exc).__name__, "detail": str(exc)[:200]}
            with db_factory() as db:
                record_issue(db, category="JOB_STUCK" if isinstance(exc, asyncio.TimeoutError) else "JOB_FAILED",
                             code=type(exc).__name__, source=step,
                             title=f"Portfolio workflow {step} failed", context={"trade_date": day, "slot": slot})
    with db_factory() as db:
        row = db.get(PortfolioWorkflowRun, run_id)
        row.status = result.get("status", "SUCCEEDED")
        row.payload = result
        row.output_hash = logical_hash(result)
        row.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    return {"run_id": run_id, **result}


def _monitor_slots(day: date) -> list[tuple[str, datetime]]:
    slots = []
    for start, end in MONITOR_STARTS:
        current = datetime.combine(day, start, SH)
        last = datetime.combine(day, end, SH)
        while current <= last:
            slots.append(("HARD_RISK", current))
            if current.minute % 5 == 0:
                slots.append(("FEATURE_REFRESH", current))
            current += timedelta(minutes=1)
    return slots


def _all_due_slots(day: date) -> list[tuple[str, datetime]]:
    rows = [(step, datetime.combine(day, time.fromisoformat(hhmm), SH)) for step, hhmm in FIXED_STEPS]
    rows += _monitor_slots(day)
    if day.weekday() == 4:
        rows.append(("WEEKLY_REVIEW", datetime.combine(day, time(20, 30), SH)))
    return sorted(rows, key=lambda row: row[1])


def recover_missed(*, now: datetime | None = None, db_factory=SessionLocal,
                   calendar_check: Callable[[date], bool] | None = None) -> dict:
    """Record due slots missed while service was down; never replay stale decisions."""
    moment = _now_sh(now)
    with db_factory() as db:
        activation = db.query(PortfolioWorkflowRun).filter_by(trade_date="GLOBAL", step="ACTIVATED", slot="ONCE").first()
        if activation is None:
            activation = PortfolioWorkflowRun(
                run_id=uuid.uuid4().hex, trade_date="GLOBAL", step="ACTIVATED", slot="ONCE",
                status="SUCCEEDED", payload={"activated_at": moment.isoformat()},
                started_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
                finished_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
            )
            db.add(activation)
            db.commit()
        start = datetime.fromisoformat(activation.payload["activated_at"]).astimezone(SH)
        if calendar_check is None and trading_calendar._CN_TRADING_DATES is None:
            record_issue(db, category="DATA_MISSING", code="CALENDAR_UNVERIFIED", source="trading_calendar",
                         title="Portfolio workflow trading calendar unavailable",
                         context={"trade_date": moment.date().isoformat()})
            return {"missed": 0, "reason": "CALENDAR_UNVERIFIED"}
        if not (calendar_check(moment.date()) if calendar_check else trading_calendar.is_trading_day("CN", moment.date())):
            return {"missed": 0, "reason": "NON_TRADING_DAY"}
        stale_running = (db.query(PortfolioWorkflowRun)
                         .filter_by(trade_date=moment.date().isoformat(), status="RUNNING")
                         .filter(PortfolioWorkflowRun.started_at <
                                 (moment - timedelta(minutes=5)).astimezone(timezone.utc).replace(tzinfo=None)).all())
        for running in stale_running:
            running.status = "FAILED"
            running.payload = {"reason": "JOB_STUCK_AFTER_RESTART"}
            running.finished_at = moment.astimezone(timezone.utc).replace(tzinfo=None)
        if stale_running:
            db.commit()
            record_issue(db, category="JOB_STUCK", code="RESTART_RECOVERY", source="portfolio_workflow",
                         title="Portfolio workflow jobs stuck after restart",
                         context={"trade_date": moment.date().isoformat(),
                                  "run_ids": [r.run_id for r in stale_running]})
        count = 0
        for step, due in _all_due_slots(moment.date()):
            if due < start or due >= moment - timedelta(minutes=2):
                continue
            slot = due.strftime("%H:%M") if step in {"HARD_RISK", "FEATURE_REFRESH"} else "DAILY"
            if db.query(PortfolioWorkflowRun).filter_by(trade_date=due.date().isoformat(), step=step, slot=slot).first():
                continue
            row = PortfolioWorkflowRun(
                run_id=uuid.uuid4().hex, trade_date=due.date().isoformat(), step=step,
                slot=slot, status="MISSED", payload={"reason": "SERVICE_DOWN", "due_at": due.isoformat()},
                started_at=due.astimezone(timezone.utc).replace(tzinfo=None),
                finished_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
            )
            db.add(row)
            count += 1
        db.commit()
        if count:
            record_issue(db, category="SCHEDULER_MISSED", code="SERVICE_DOWN", source="portfolio_workflow",
                         title="Portfolio workflow missed scheduled slots",
                         context={"trade_date": moment.date().isoformat(), "count": count})
        return {"missed": count, "activation_at": start.isoformat()}


def register_jobs(agent_scheduler) -> int:
    """Register every fixed job and independent 60s/5m monitor triggers."""
    scheduler = agent_scheduler.scheduler
    count = 0
    for step, hhmm in FIXED_STEPS:
        hh, mm = map(int, hhmm.split(":"))
        scheduler.add_job(run_step, "cron", args=[step], hour=hh, minute=mm,
                          timezone=SH, id=f"portfolio_{step}", replace_existing=True,
                          max_instances=1, coalesce=True, misfire_grace_time=90)
        count += 1
    ranges = ((9, "30-59", "30,35,40,45,50,55"),
              (10, "*", "*/5"), (11, "0-30", "0,5,10,15,20,25,30"),
              (13, "*", "*/5"), (14, "0-30", "0,5,10,15,20,25,30"))
    for hour, minute, feature_minute in ranges:
        for step, spec in (("HARD_RISK", minute), ("FEATURE_REFRESH", feature_minute)):
            scheduler.add_job(run_step, "cron", args=[step], hour=hour, minute=spec,
                              timezone=SH, id=f"portfolio_{step}_{hour}", replace_existing=True,
                              max_instances=1, coalesce=True, misfire_grace_time=45)
            count += 1
    scheduler.add_job(run_step, "cron", args=["WEEKLY_REVIEW"], day_of_week="fri",
                      hour=20, minute=30, timezone=SH, id="portfolio_WEEKLY_REVIEW",
                      replace_existing=True, max_instances=1, coalesce=True)
    return count + 1
