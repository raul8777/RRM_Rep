from types import SimpleNamespace

from universe_selector import _resolve_cap, select_analysis_universe


def test_resolve_cap_scales_with_screener_breadth_spot_bull():
    settings = SimpleNamespace(
        screener_top_n_count=100,
        screener_max_pairs=50,
    )
    cap = _resolve_cap(settings=settings, regime_mode="SPOT_BULL")
    assert cap >= 100


def test_resolve_cap_preservation_keeps_defensive_floor():
    settings = SimpleNamespace(
        screener_top_n_count=100,
        screener_max_pairs=50,
    )
    cap = _resolve_cap(settings=settings, regime_mode="PRESERVATION")
    assert cap >= 50


def test_select_analysis_universe_with_no_profile_label():
    """Regression: s95 removed the global active profile, so profile_label can be None."""
    settings = SimpleNamespace(
        current_regime_mode="SPOT_BULL",
        analysis_symbols=["BTCUSDT", "ETHUSDT"],
        futures_symbols=[],
        narrative_tokens=[],
        screener_enabled=False,
        screener_top_n_count=100,
        screener_max_pairs=50,
    )
    result = select_analysis_universe(settings=settings, profile_label=None)
    assert result["ok"]
    assert "profile_label" in result
    assert result["profile_label"] is None
