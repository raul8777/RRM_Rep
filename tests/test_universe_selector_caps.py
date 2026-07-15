from types import SimpleNamespace

from universe_selector import _resolve_cap


def test_resolve_cap_scales_with_screener_breadth_spot_bull():
    settings = SimpleNamespace(
        current_regime_profile_label="B",
        screener_top_n_count=100,
        screener_max_pairs=50,
    )
    cap = _resolve_cap(settings=settings, regime_mode="SPOT_BULL")
    assert cap >= 100


def test_resolve_cap_preservation_keeps_defensive_floor():
    settings = SimpleNamespace(
        current_regime_profile_label="C",
        screener_top_n_count=100,
        screener_max_pairs=50,
    )
    cap = _resolve_cap(settings=settings, regime_mode="PRESERVATION")
    assert cap >= 50
