"""P1 portfolio truth and append-only position plans.

This module records evidence and human plans. It never proposes or executes orders.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.platform.persistence.models import (
    Account, AppSettings, PortfolioTruthPosition, PortfolioTruthSnapshot, Position,
    PositionPlan, PositionStateEvent, Stock,
)


POSITION_STATES = frozenset({
    "WATCH", "STARTER", "CORE", "ADD_ALLOWED", "OVERWEIGHT",
    "REDUCE_REQUIRED", "EXIT_PENDING", "CLOSED",
})
THESIS_STATES = frozenset({"VALID", "WEAKENING", "INVALID", "UNKNOWN"})

ALLOWED_TRANSITIONS = {
    "WATCH": frozenset({"WATCH", "STARTER"}),
    "STARTER": frozenset({"STARTER", "CORE", "EXIT_PENDING"}),
    "CORE": frozenset({"CORE", "ADD_ALLOWED", "OVERWEIGHT", "REDUCE_REQUIRED", "EXIT_PENDING"}),
    "ADD_ALLOWED": frozenset({"ADD_ALLOWED", "CORE", "OVERWEIGHT", "REDUCE_REQUIRED", "EXIT_PENDING"}),
    "OVERWEIGHT": frozenset({"OVERWEIGHT", "CORE", "REDUCE_REQUIRED", "EXIT_PENDING"}),
    "REDUCE_REQUIRED": frozenset({"REDUCE_REQUIRED", "CORE", "EXIT_PENDING"}),
    "EXIT_PENDING": frozenset({"EXIT_PENDING", "CLOSED"}),
    "CLOSED": frozenset({"CLOSED"}),
}


def logical_hash(value: Any) -> str:
    """Stable across capture time and input order after canonical aggregation."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def aggregate_positions(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Aggregate same symbols across accounts without inventing T+1 fields."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    anomalies: set[str] = set()
    seen_account_symbols: set[tuple[int, str, str]] = set()
    for row in rows:
        market, symbol = str(row["market"]), str(row["symbol"])
        key = (int(row["account_id"]), market, symbol)
        if key in seen_account_symbols:
            anomalies.add("DUPLICATE_ACCOUNT_POSITION")
        seen_account_symbols.add(key)
        qty = int(row["quantity"])
        cost = _decimal(row["cost_price"])
        if qty < 0 or cost < 0:
            anomalies.add("NEGATIVE_POSITION_VALUE")
        sellable, locked = row.get("sellable_qty"), row.get("today_locked_qty")
        if sellable is None or locked is None:
            anomalies.add("SELLABLE_UNKNOWN")
        else:
            if int(sellable) < 0 or int(locked) < 0 or int(sellable) + int(locked) != qty:
                anomalies.add("T_PLUS_ONE_QUANTITY_MISMATCH")
        mv = row.get("market_value")
        if mv is not None and _decimal(mv) < 0:
            anomalies.add("NEGATIVE_MARKET_VALUE")
        grouped[(market, symbol)].append(row)

    positions = []
    for (market, symbol), group in sorted(grouped.items()):
        details = []
        total_qty = sum(int(row["quantity"]) for row in group)
        total_cost = sum(
            _decimal(row["cost_price"]) * int(row["quantity"]) for row in group
        )
        avg_cost = total_cost / total_qty if total_qty > 0 else Decimal("0")
        sellable = (
            sum(int(row["sellable_qty"]) for row in group)
            if all(row.get("sellable_qty") is not None for row in group) else None
        )
        locked = (
            sum(int(row["today_locked_qty"]) for row in group)
            if all(row.get("today_locked_qty") is not None for row in group) else None
        )
        market_value = (
            sum(_decimal(row["market_value"]) for row in group)
            if all(row.get("market_value") is not None for row in group) else None
        )
        for row in sorted(group, key=lambda x: (int(x["account_id"]), int(x.get("position_id") or 0))):
            details.append({
                "account_id": int(row["account_id"]),
                "account_name": str(row["account_name"]),
                "position_id": row.get("position_id"),
                "stock_id": row.get("stock_id"),
                "quantity": int(row["quantity"]),
                "cost_price": str(_decimal(row["cost_price"])),
                "sellable_qty": row.get("sellable_qty"),
                "today_locked_qty": row.get("today_locked_qty"),
                "market_value": str(_decimal(row["market_value"])) if row.get("market_value") is not None else None,
            })
        positions.append({
            "market": market,
            "symbol": symbol,
            "name": str(group[0]["name"]),
            "total_qty": total_qty,
            "sellable_qty": sellable,
            "today_locked_qty": locked,
            "avg_cost": str(avg_cost.quantize(Decimal("0.000001"))),
            "market_value": str(market_value) if market_value is not None else None,
            "account_details": details,
        })
    if len(positions) > 20:
        anomalies.add("POSITION_LIMIT_EXCEEDED")
    return positions, sorted(anomalies)


def capture_truth(
    db: Session,
    *,
    phase: str,
    source: str = "manual_ui",
    source_asof: datetime | None = None,
    ttl_seconds: int = 300,
    fetched_at: datetime | None = None,
    sellable_equals_quantity: bool = False,
) -> PortfolioTruthSnapshot:
    """Capture local positions; explicit user attestation is never broker proof."""
    if phase not in {"PREMARKET", "EOD", "MANUAL"}:
        raise ValueError("invalid_truth_phase")
    now = fetched_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if sellable_equals_quantity:
        source = "user_attested"
        source_asof = now
    account_rows = db.query(Account).filter(Account.enabled.is_(True)).order_by(Account.id).all()
    account_details = [{
        "account_id": a.id, "name": a.name, "available_funds": a.available_funds,
        "source": source,
    } for a in account_rows]
    rows = []
    for pos, stock, account in (
        db.query(Position, Stock, Account)
        .join(Stock, Position.stock_id == Stock.id)
        .join(Account, Position.account_id == Account.id)
        .filter(Account.enabled.is_(True))
        .all()
    ):
        rows.append({
            "position_id": pos.id, "stock_id": stock.id,
            "account_id": account.id, "account_name": account.name,
            "market": stock.market, "symbol": stock.symbol, "name": stock.name,
            "quantity": pos.quantity, "cost_price": pos.cost_price,
            "sellable_qty": pos.quantity if sellable_equals_quantity else None,
            "today_locked_qty": 0 if sellable_equals_quantity else None,
            "market_value": None,
        })
    aggregated, anomalies = aggregate_positions(rows)
    if sellable_equals_quantity:
        anomalies.append("USER_ATTESTED_UNRECONCILED")
    if not account_rows:
        anomalies.append("ACCOUNT_MISSING")
    if not aggregated:
        anomalies.append("POSITIONS_EMPTY")
    if source_asof is None:
        freshness = "UNKNOWN"
        anomalies.append("SOURCE_TIME_UNKNOWN")
    else:
        asof = source_asof.replace(tzinfo=timezone.utc) if source_asof.tzinfo is None else source_asof
        age = (now - asof).total_seconds()
        if age < -30:
            freshness = "UNKNOWN"
            anomalies.append("SOURCE_TIME_FUTURE")
        elif age > ttl_seconds:
            freshness = "STALE"
            anomalies.append("SOURCE_STALE")
        else:
            freshness = "FRESH"
    if any(a.available_funds is None or a.available_funds < 0 for a in account_rows):
        anomalies.append("ACCOUNT_FUNDS_INVALID")
    nav_setting = db.query(AppSettings).filter_by(key="portfolio_declared_nav_cny").first()
    declared_nav = None
    if nav_setting:
        try:
            declared_nav = float(nav_setting.value)
            if declared_nav <= 0:
                raise ValueError("nonpositive_nav")
        except (TypeError, ValueError):
            declared_nav = None
            anomalies.append("DECLARED_NAV_INVALID")
    anomalies = sorted(set(anomalies))
    status = "TRUSTED" if freshness == "FRESH" and not anomalies else "REVIEW_ONLY"
    payload = {"accounts": account_details, "positions": aggregated,
               "declared_nav_cny": declared_nav}
    snapshot = PortfolioTruthSnapshot(
        trade_date=now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        phase=phase, source=source, fetched_at=now.replace(tzinfo=None),
        source_asof=(source_asof.replace(tzinfo=timezone.utc) if source_asof.tzinfo is None else source_asof.astimezone(timezone.utc)).replace(tzinfo=None) if source_asof else None,
        freshness=freshness, truth_status=status,
        logical_hash=logical_hash(payload), anomaly_flags=anomalies,
        account_details=account_details,
        cash=sum(float(a.available_funds or 0) for a in account_rows), nav=declared_nav,
    )
    db.add(snapshot)
    db.flush()
    for item in aggregated:
        db.add(PortfolioTruthPosition(
            snapshot_id=snapshot.id, market=item["market"], symbol=item["symbol"],
            name=item["name"], total_qty=item["total_qty"],
            sellable_qty=item["sellable_qty"], today_locked_qty=item["today_locked_qty"],
            avg_cost=float(item["avg_cost"]),
            market_value=float(item["market_value"]) if item["market_value"] is not None else None,
            account_details=item["account_details"],
        ))
    db.commit()
    db.refresh(snapshot)
    return snapshot


def current_freshness(snapshot: PortfolioTruthSnapshot, *, now: datetime | None = None, ttl_seconds: int = 300) -> str:
    """Assess current age without rewriting the capture-time evidence."""
    if snapshot.source_asof is None:
        return "UNKNOWN"
    asof = snapshot.source_asof.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - asof).total_seconds()
    if age < -30:
        return "UNKNOWN"
    return "STALE" if age > ttl_seconds else "FRESH"


def current_plan(db: Session, market: str, symbol: str) -> PositionPlan | None:
    return (db.query(PositionPlan)
            .filter_by(market=market, symbol=symbol)
            .order_by(PositionPlan.version.desc()).first())


def create_plan_version(
    db: Session, *, market: str, symbol: str, position_state: str,
    thesis_state: str, plan: dict, reason: str,
    truth_snapshot_id: int | None = None, expected_version: int | None = None,
) -> tuple[PositionPlan | None, PositionStateEvent]:
    """Create a new immutable plan or record REVIEW_REQUIRED on illegal transition."""
    if position_state not in POSITION_STATES or thesis_state not in THESIS_STATES:
        raise ValueError("invalid_position_or_thesis_state")
    if not isinstance(plan, dict):
        raise ValueError("invalid_plan")
    previous = current_plan(db, market, symbol)
    actual_version = previous.version if previous else 0
    bootstrap_correction = bool(
        previous
        and previous.version == 1
        and previous.position_state == "CORE"
        and (previous.plan or {}).get("review_status") == "REVIEW_ONLY"
        and not (previous.plan or {}).get("entry_thesis")
        and position_state == "WATCH"
        and reason == "BOOTSTRAP_CORRECTION"
    )
    if expected_version is not None and actual_version != expected_version:
        decision, why = "REVIEW_REQUIRED", "VERSION_CONFLICT"
    elif previous and position_state not in ALLOWED_TRANSITIONS[previous.position_state] and not bootstrap_correction:
        decision, why = "REVIEW_REQUIRED", "ILLEGAL_STATE_TRANSITION"
    elif not previous and position_state not in {"WATCH", "STARTER", "CORE"}:
        decision, why = "REVIEW_REQUIRED", "INVALID_INITIAL_STATE"
    elif truth_snapshot_id and not db.get(PortfolioTruthSnapshot, truth_snapshot_id):
        decision, why = "REVIEW_REQUIRED", "TRUTH_SNAPSHOT_MISSING"
    else:
        decision, why = "APPROVED", reason.strip()[:255]
    event = PositionStateEvent(
        market=market, symbol=symbol,
        from_state=previous.position_state if previous else None,
        to_state=position_state,
        from_thesis=previous.thesis_state if previous else None,
        to_thesis=thesis_state,
        decision=decision, reason=why, plan_version=actual_version + 1 if decision == "APPROVED" else None,
    )
    db.add(event)
    if decision != "APPROVED":
        db.commit()
        return None, event
    document = {
        "market": market, "symbol": symbol, "version": actual_version + 1,
        "position_state": position_state, "thesis_state": thesis_state,
        "plan": plan, "truth_snapshot_id": truth_snapshot_id,
    }
    created = PositionPlan(
        market=market, symbol=symbol, version=actual_version + 1,
        position_state=position_state, thesis_state=thesis_state, plan=plan,
        logical_hash=logical_hash(document), truth_snapshot_id=truth_snapshot_id,
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created, event


def initialize_plans_from_truth(db: Session, snapshot_id: int) -> list[PositionPlan]:
    """Seed review-only plans for held symbols; never overwrite prior versions."""
    snapshot = db.get(PortfolioTruthSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError("truth_snapshot_missing")
    if len(snapshot.positions) > 20:
        raise ValueError("position_limit_exceeded")
    created = []
    for item in snapshot.positions:
        if item.total_qty <= 0 or current_plan(db, item.market, item.symbol):
            continue
        plan = {
            "strategy_style": "unverified",
            "entry_thesis": [],
            "position": {
                "current_weight": None, "target_weight": None, "max_weight": None,
                "total_qty": item.total_qty, "sellable_qty": item.sellable_qty,
                "today_locked_qty": item.today_locked_qty,
                "avg_cost": item.avg_cost, "account_details": item.account_details,
            },
            "risk": {"initial_stop": None, "current_stop": None, "risk_budget_pct": None},
            "add_policy": {"eligible": False, "conditions": []},
            "reduce_policy": {"conditions": []},
            "hard_exit": {"conditions": []},
            "review_status": "REVIEW_ONLY",
            "review_reasons": snapshot.anomaly_flags or ["PLAN_NOT_REVIEWED"],
        }
        row, _ = create_plan_version(
            db, market=item.market, symbol=item.symbol,
            position_state="WATCH", thesis_state="UNKNOWN", plan=plan,
            reason="MANUAL_TRUTH_BOOTSTRAP", truth_snapshot_id=snapshot.id,
            expected_version=0,
        )
        if row:
            created.append(row)
    return created
