import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.strategy import strategy_engine as engine
from src.platform.persistence.models import Base, EntryCandidate, StrategySignalRun


@pytest.fixture
def db_factory(monkeypatch, tmp_path):
    sql = create_engine('sqlite:///' + str(tmp_path / 'identity.db'))
    Base.metadata.create_all(sql)
    factory = sessionmaker(bind=sql)
    monkeypatch.setattr(engine, 'SessionLocal', factory)
    monkeypatch.setattr(engine, 'ensure_strategy_catalog', lambda: None)
    monkeypatch.setattr(engine, 'get_strategy_profile_map', lambda: {})
    monkeypatch.setattr(engine, 'get_effective_weight_map', lambda **kw: {})
    monkeypatch.setattr(engine, 'get_factor_weights', lambda *a, **kw: {})
    monkeypatch.setattr(engine, '_upsert_market_regime_snapshots', lambda **kw: {})
    monkeypatch.setattr(engine, '_build_cross_section_features', lambda *a: {})
    monkeypatch.setattr(engine, '_load_news_metrics', lambda **kw: {})
    monkeypatch.setattr(engine, '_strategy_codes_for_candidate', lambda c: ['trend_follow'])
    monkeypatch.setattr(engine, '_compute_factor_breakdown', lambda **kw: {'weighted_score': 80})
    monkeypatch.setattr(engine, '_apply_portfolio_constraints', lambda **kw: {})
    monkeypatch.setattr(engine, '_sync_factor_and_risk_snapshots', lambda **kw: None)
    return factory


def test_rebuilt_candidate_ids_cannot_swap_security_prices(db_factory):
    day = '2026-09-28'
    with db_factory() as db:
        for ident, symbol, price in [(1, '601208', 56), (2, '300308', 896)]:
            db.add(EntryCandidate(id=ident, stock_symbol=symbol, stock_market='CN',
                snapshot_date=day, score=80, action='buy', status='active', entry_low=price))
        db.commit()
    engine.refresh_strategy_signals(snapshot_date=day)
    with db_factory() as db:
        db.query(EntryCandidate).delete(synchronize_session=False)
        db.flush()
        for ident, symbol, price in [(1, '300308', 896), (2, '601208', 56)]:
            db.add(EntryCandidate(id=ident, stock_symbol=symbol, stock_market='CN',
                snapshot_date=day, score=80, action='buy', status='active', entry_low=price))
        db.commit()
    # Before recompute, ambiguous legacy rows are hidden, not rewritten.
    assert engine.list_strategy_signals(snapshot_date=day)['items'] == []
    engine.refresh_strategy_signals(snapshot_date=day)
    with db_factory() as db:
        prices = {r.stock_symbol: r.entry_low for r in db.query(StrategySignalRun)}
    assert prices == {'601208': 56, '300308': 896}
    assert len(engine.list_strategy_signals(snapshot_date=day)['items']) == 2
