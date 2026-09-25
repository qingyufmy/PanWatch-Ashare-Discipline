"""Paper-only account invariants: frozen signal lineage, idempotency and T+1."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.paper_trading.portfolio_paper import capture_paper_nav, scan_portfolio_paper
from src.modules.paper_trading.api.paper_trading import _build_equity_curve
from src.modules.portfolio.market_context import _quote
from src.platform.scheduling import trading_calendar
from src.platform.persistence.database import Base
from src.platform.persistence.models import (
    AppSettings, EvidenceSnapshot, ModelRun, PaperPortfolioFill, PaperPortfolioNav,
    PaperTradingAccount, PaperTradingPosition, PaperTradingTrade,
    PositionPlan, SignalEvent, SignalPolicyDecision,
)

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 10, 5, tzinfo=SH)
UTC = NOW.astimezone(timezone.utc).replace(tzinfo=None)


def _db(monkeypatch):
    monkeypatch.setattr(trading_calendar, "_CN_TRADING_DATES", frozenset({NOW.date()}))
    monkeypatch.setattr(trading_calendar, "_CN_RANGE", (NOW.date(), NOW.date()))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([
        AppSettings(key="portfolio_paper_mode", value="paper_only"),
        PaperTradingAccount(id=1, initial_capital=100000, current_capital=97000,
                            peak_capital=100000, enabled=True),
        PaperTradingPosition(stock_symbol="600206", stock_market="CN", stock_name="fixture",
                             quantity=300, entry_price=10, current_price=10,
                             strategy_code="portfolio_baseline", status="open"),
        PositionPlan(market="CN", symbol="600206", version=1,
                     position_state="WATCH", thesis_state="UNKNOWN",
                     plan={"position": {"max_weight": 0.10},
                           "risk": {"current_stop": 8}}, logical_hash="plan"),
        ModelRun(run_id="model", trace_id="trace", role="FAST", profile_role="FAST",
                 requested_model="fixture", prompt_id="flash", input_hash="input",
                 latency_ms=1, status="OK", schema_valid=True,
                 started_at=UTC, finished_at=UTC),
        EvidenceSnapshot(captured_at=UTC, source="portfolio_auction_context",
                         logical_hash="auction", payload={"trade_date": "2026-09-24",
                         "holdings": [{"symbol": "600206", "quality": "FRESH"}]}),
    ])
    db.commit()
    return engine, db


def _signal(db: Session, action: str, qty: int, order: int, target: float | None = None,
            blocked: bool = False):
    auction = db.query(EvidenceSnapshot).filter_by(source="portfolio_auction_context").one()
    evidence = EvidenceSnapshot(captured_at=UTC, source="fixture", logical_hash=f"e{order}",
                                payload={"schema_valid": True, "confidence": 0.9,
                                         "model_run_id": "model",
                                         "auction_evidence_snapshot_id": auction.id})
    db.add(evidence)
    db.flush()
    signal = SignalEvent(
        signal_id=f"signal-{order}", trace_id="trace", trade_date="2026-09-24",
        market="CN", symbol="600206", source="intraday_portfolio_plan",
        source_agent="portfolio_intraday", evidence_snapshot_id=evidence.id,
        plan_version=1, action=action, raw_action=action,
        qty_hint=qty, target_weight=target, generated_at=UTC + timedelta(seconds=order),
        valid_from=UTC - timedelta(minutes=5), expires_at=UTC + timedelta(minutes=15),
        dedupe_key=f"dedupe-{order}", status="REVIEW_REQUIRED",
    )
    db.add(signal)
    for i in range(13):
        db.add(SignalPolicyDecision(signal_id=signal.signal_id, rule_id=f"rule-{i}",
                                    input_hash="input", decision="BLOCK" if blocked and i == 0 else "REVIEW"))
    db.commit()


def _quotes(symbols):
    out = []
    for code in symbols:
        bare = code[2:]
        out.append({"symbol": bare, "current_price": 10 if bare == "600206" else 1000,
                    "change_pct": 1, "volume": 100000,
                    "source_asof": "20260924100430"})
    return out


def test_paper_round_trip_never_sells_same_day_buy_and_is_idempotent(monkeypatch):
    engine, db = _db(monkeypatch)
    try:
        _signal(db, "REDUCE", 100, 1)
        _signal(db, "ADD", 100, 2, target=0.05)
        _signal(db, "EXIT", 300, 3)
        account = db.get(PaperTradingAccount, 1)
        result = scan_portfolio_paper(db, account, now=NOW, quote_fetcher=_quotes)
        assert (result["opened"], result["closed"]) == (1, 1)
        assert db.query(PaperPortfolioFill).count() == 2
        assert db.query(PaperTradingTrade).count() == 1
        position = db.query(PaperTradingPosition).one()
        assert position.quantity == 300 and position.status == "open"
        cash = account.current_capital
        again = scan_portfolio_paper(db, account, now=NOW, quote_fetcher=_quotes)
        assert again["opened"] == again["closed"] == 0
        assert account.current_capital == cash
    finally:
        db.close()
        engine.dispose()


def test_stale_quote_or_blocked_policy_cannot_fill(monkeypatch):
    engine, db = _db(monkeypatch)
    try:
        _signal(db, "REDUCE", 100, 1)
        stale = lambda symbols: [{**q, "source_asof": "20260924095000"} for q in _quotes(symbols)]
        result = scan_portfolio_paper(db, db.get(PaperTradingAccount, 1), now=NOW,
                                      quote_fetcher=stale)
        assert result["closed"] == 0
        _signal(db, "REDUCE", 100, 2, blocked=True)
        result = scan_portfolio_paper(db, db.get(PaperTradingAccount, 1), now=NOW,
                                      quote_fetcher=_quotes)
        assert result["closed"] == 1
        assert db.get(PaperPortfolioFill, "signal-2") is None
    finally:
        db.close()
        engine.dispose()


def test_global_quote_without_source_time_is_not_fresh():
    row = _quote({"symbol": "NVDA", "current_price": 200, "change_pct": 1,
                  "source_asof": None}, "NVIDIA", collected_at=NOW, phase="INTRADAY")
    assert row["quality"] == "SOURCE_TIME_UNKNOWN"


def test_eod_nav_requires_all_dated_quotes_and_is_idempotent(monkeypatch):
    engine, db = _db(monkeypatch)
    try:
        account = db.get(PaperTradingAccount, 1)
        close = datetime(2026, 9, 24, 15, 50, tzinfo=SH)
        stale = lambda symbols: [{"symbol": "600206", "current_price": 11,
                                  "source_asof": "20260924143000"}]
        assert capture_paper_nav(db, account, now=close, quote_fetcher=stale)["status"] == "close_quote_unverified"
        assert db.query(PaperPortfolioNav).count() == 0
        fresh = lambda symbols: [{"symbol": "600206", "current_price": 11,
                                  "source_asof": "20260924150500"}]
        result = capture_paper_nav(db, account, now=close, quote_fetcher=fresh)
        assert result["status"] == "captured" and result["equity"] == 100300
        assert capture_paper_nav(db, account, now=close, quote_fetcher=stale)["status"] == "already_captured"
        curve, peak, _ = _build_equity_curve(db, account, None)
        assert curve[0] == {"date": "2026-09-24", "equity": 100300}
        assert peak == 100300
    finally:
        db.close()
        engine.dispose()
