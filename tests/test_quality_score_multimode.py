from run_trades import _quality_score_for_regime


def test_futures_short_prefers_bounce_over_equal_dump_when_liquid():
    score_bounce = _quality_score_for_regime(
        "FUTURES_SHORT",
        price_change_pct_abs=0.04,
        price_change_pct_signed=0.04,
        volatility_limit=0.50,
        volume_usdt=120_000_000,
        min_volume_usdt=10_000_000,
    )
    score_dump = _quality_score_for_regime(
        "FUTURES_SHORT",
        price_change_pct_abs=0.04,
        price_change_pct_signed=-0.04,
        volatility_limit=0.50,
        volume_usdt=120_000_000,
        min_volume_usdt=10_000_000,
    )
    assert score_bounce > score_dump


def test_spot_bull_penalizes_downside():
    score_up = _quality_score_for_regime(
        "SPOT_BULL",
        price_change_pct_abs=0.03,
        price_change_pct_signed=0.03,
        volatility_limit=0.50,
        volume_usdt=80_000_000,
        min_volume_usdt=10_000_000,
    )
    score_down = _quality_score_for_regime(
        "SPOT_BULL",
        price_change_pct_abs=0.03,
        price_change_pct_signed=-0.03,
        volatility_limit=0.50,
        volume_usdt=80_000_000,
        min_volume_usdt=10_000_000,
    )
    assert score_up > score_down
