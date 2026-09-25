"""Source scope and clock quality must survive the portfolio batch boundary."""

from src.modules.portfolio.market_context import context_quality_matrix


def test_candidate_up_breadth_cannot_become_whole_market_breadth():
    matrix = context_quality_matrix({
        "trade_date": "2026-09-24",
        "indices": [{"quality": "FRESH"} for _ in range(4)],
        "holdings": ([{"quality": "FRESH", "change_pct": -1.0} for _ in range(8)]
                     + [{"quality": "FRESH", "change_pct": 1.0} for _ in range(2)]),
        "candidate_breadth": {"snapshot_date": "2026-09-24",
                              "breadth_up_pct": 89.011, "sample_size": 91,
                              "scope": "candidate_sample_not_market_wide"},
        "global_tech": [{"quality": "STALE", "source_asof": "2026-09-23T16:00:00-04:00"},
                        {"quality": "SOURCE_TIME_UNKNOWN", "source_asof": None}],
        "boards": [{"quality": "FETCH_TIME_ONLY"}],
    })
    assert matrix["market_breadth"]["quality"] == "MISSING"
    assert matrix["candidate_pool_breadth"]["scope"] == "candidate_sample_not_market_wide"
    assert matrix["candidate_pool_breadth"]["quality"] == "CURRENT_DAY"
    assert matrix["holdings"]["fresh"] == 10
    assert matrix["holding_breadth"]["decliners"] == 8
    assert matrix["holding_breadth"]["advancers"] == 2
    assert matrix["industry_exposure"]["quality"] == "MISSING"
    assert matrix["global_tech"]["fresh"] == 0
    assert matrix["boards"]["quality"] == "FETCH_TIME_ONLY"


def test_prior_day_candidate_sample_remains_stale():
    matrix = context_quality_matrix({
        "trade_date": "2026-09-24", "candidate_breadth": {
            "snapshot_date": "2026-09-23", "sample_size": 26,
        },
    })
    assert matrix["candidate_pool_breadth"]["quality"] == "STALE"
    assert matrix["market_breadth"]["quality"] == "MISSING"
