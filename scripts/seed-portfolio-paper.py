"""Archive the old singleton paper account and seed a marked CN portfolio baseline.

Run after schema migration. Dry-run unless --apply is supplied. The archive and
the private evidence report are always kept under ignored data/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from marketdata.vendors.tencent import fetch_raw
from sqlalchemy.orm import Session

from src.platform.persistence.database import DB_PATH, SessionLocal
from src.platform.persistence.models import (
    AppSettings, PaperPortfolioFill, PaperPortfolioNav, PaperTradingAccount,
    PaperTradingPosition, PaperTradingTrade, PortfolioTruthSnapshot,
    Position, Stock,
)

SH = ZoneInfo("Asia/Shanghai")


def plan(db: Session, *, nav: float, asof: str, quote_fetcher=fetch_raw) -> dict:
    truth = db.query(PortfolioTruthSnapshot).order_by(PortfolioTruthSnapshot.id.desc()).first()
    if (not truth or truth.trade_date != asof or truth.source != "user_attested" or
            truth.truth_status != "REVIEW_ONLY"):
        raise ValueError("latest_user_attested_truth_required")
    holdings = (db.query(Position, Stock).join(Stock, Position.stock_id == Stock.id)
                .order_by(Stock.symbol).all())
    if not holdings or len(holdings) != len(truth.positions):
        raise ValueError("truth_position_count_mismatch")
    declared = {(row.market, row.symbol): (row.total_qty, row.sellable_qty)
                for row in truth.positions}
    if any(declared.get((stock.market, stock.symbol)) != (position.quantity, position.quantity)
           for position, stock in holdings):
        raise ValueError("truth_position_quantity_mismatch")
    raw = quote_fetcher([("sh" if stock.symbol.startswith(("6", "9")) else "sz") + stock.symbol
                         for _, stock in holdings])
    by_symbol = {str(r.get("symbol")): r for r in raw if isinstance(r, dict)}
    rows = []
    for position, stock in holdings:
        quote = by_symbol.get(stock.symbol)
        if not quote:
            raise ValueError(f"baseline_quote_missing:{stock.symbol}")
        stamp = datetime.strptime(str(quote.get("source_asof")), "%Y%m%d%H%M%S").replace(tzinfo=SH)
        price = float(quote.get("current_price") or 0)
        if stamp.date().isoformat() != asof or stamp.hour < 15 or price <= 0 or position.quantity <= 0:
            raise ValueError(f"baseline_quote_unverified:{stock.symbol}")
        rows.append({"symbol": stock.symbol, "name": stock.name, "quantity": position.quantity,
                     "mark_price": price, "source_cost_price": position.cost_price,
                     "quote_asof": stamp.isoformat(), "market": stock.market})
    market_value = round(sum(r["mark_price"] * r["quantity"] for r in rows), 2)
    synthetic_cash = round(nav - market_value, 2)
    if synthetic_cash < 0:
        raise ValueError("declared_nav_below_marked_holdings")
    return {"trade_date": asof, "truth_snapshot_id": truth.id,
            "truth_source": truth.source, "nav_source": "user_declared",
            "declared_nav": nav, "market_value": market_value,
            "synthetic_cash": synthetic_cash, "quote_source": "tencent_close_quote",
            "cash_source": "synthetic_nav_minus_marked_holdings_not_broker_cash",
            "positions": rows}


def apply_baseline(db: Session, baseline: dict) -> None:
    marker = db.query(AppSettings).filter_by(key="portfolio_paper_baseline").first()
    if marker:
        raise ValueError("portfolio_paper_baseline_already_exists")
    db.query(PaperTradingPosition).delete()
    db.query(PaperTradingTrade).delete()
    db.query(PaperPortfolioFill).delete()
    db.query(PaperPortfolioNav).delete()
    account = db.query(PaperTradingAccount).first()
    if account is None:
        account = PaperTradingAccount()
        db.add(account)
    account.initial_capital = baseline["declared_nav"]
    account.current_capital = baseline["synthetic_cash"]
    account.total_pnl = 0.0
    account.total_trades = 0
    account.winning_trades = 0
    account.max_drawdown_pct = 0.0
    account.peak_capital = baseline["declared_nav"]
    account.enabled = True
    account.market_allocations = {"CN": 1.0, "HK": 0.0, "US": 0.0}
    account.excluded_markets = ["HK", "US"]
    opened = (datetime.fromisoformat(baseline["trade_date"] + "T15:00:00")
              .replace(tzinfo=SH).astimezone(timezone.utc).replace(tzinfo=None))
    for row in baseline["positions"]:
        db.add(PaperTradingPosition(
            stock_symbol=row["symbol"], stock_market="CN", stock_name=row["name"],
            quantity=row["quantity"], entry_price=row["mark_price"],
            source_cost_price=row["source_cost_price"], current_price=row["mark_price"],
            highest_price=row["mark_price"], unrealized_pnl=0.0,
            status="open", signal_snapshot_date=baseline["trade_date"],
            signal_action="baseline", strategy_code="portfolio_baseline", opened_at=opened,
        ))
    db.add(PaperPortfolioNav(
        trade_date=baseline["trade_date"], cash=baseline["synthetic_cash"],
        market_value=baseline["market_value"], equity=baseline["declared_nav"],
        source_asof_min=min(datetime.fromisoformat(row["quote_asof"])
                            for row in baseline["positions"]).astimezone(timezone.utc).replace(tzinfo=None),
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        position_count=len(baseline["positions"]), source="tencent_close_quote",
    ))
    meta = {k: v for k, v in baseline.items() if k != "positions"}
    db.add(AppSettings(key="portfolio_paper_baseline", value=json.dumps(meta, ensure_ascii=False),
                       description="Private paper-only baseline; synthetic cash"))
    db.add(AppSettings(key="portfolio_paper_mode", value="paper_only",
                       description="Only frozen portfolio signals may fill the paper account"))
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nav", type=float, required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        baseline = plan(db, nav=args.nav, asof=args.asof)
        marker = db.query(AppSettings).filter_by(key="portfolio_paper_baseline").first()
        if marker:
            raise SystemExit("baseline_already_exists_no_reseed")
        if not args.apply:
            print(json.dumps({"dry_run": True, "position_count": len(baseline["positions"]),
                              "market_value": baseline["market_value"],
                              "synthetic_cash": baseline["synthetic_cash"]}))
            return
        folder = ROOT / "data" / "paper-trading-archive"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SH).strftime("%Y%m%d-%H%M%S")
        archive = folder / f"pre-portfolio-mirror-{stamp}.db"
        report = folder / f"portfolio-baseline-{stamp}.json"
        with sqlite3.connect(DB_PATH) as source, sqlite3.connect(archive) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("archive_integrity_failed")
        baseline["archived_legacy_db"] = str(archive)
        baseline["archived_legacy_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        report.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        apply_baseline(db, baseline)
        print(json.dumps({"applied": True, "position_count": len(baseline["positions"]),
                          "market_value": baseline["market_value"],
                          "synthetic_cash": baseline["synthetic_cash"],
                          "truth_snapshot_id": baseline["truth_snapshot_id"],
                          "archive": str(archive), "report": str(report)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
