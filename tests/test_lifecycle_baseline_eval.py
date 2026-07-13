"""tests/test_lifecycle_baseline_eval.py — MIMIC baseline evaluation smoke tests."""

from lifecycle.mimic_baseline_eval import (
    spot_bull_a_config,
    sample_spot_bull_analyses,
    sample_price_series,
    run_baseline_window,
)


def test_spot_bull_a_config_matches_sweep_params():
    cfg = spot_bull_a_config()
    assert cfg.regime == "SPOT_BULL"
    assert cfg.profile == "A"
    assert cfg.signal_policy.min_signal_strength == 0.55
    assert cfg.signal_policy.min_success_probability == 0.55
    assert cfg.sizing_policy.max_per_trade_usdt == 1000.0
    assert cfg.sizing_policy.kelly_base_pct == 0.18
    assert cfg.entry_policy.max_total_positions == 5


def test_baseline_window_runs_and_produces_metrics():
    cfg = spot_bull_a_config()
    analyses = sample_spot_bull_analyses()
    prices = sample_price_series()
    atr_map = {"BTCUSDT": 900.0, "SOLUSDT": 1.8, "FETUSDT": 0.04}
    rsi_map = {"BTCUSDT": 50.0, "SOLUSDT": 50.0, "FETUSDT": 50.0}
    adapter, metrics, trace = run_baseline_window(
        cfg, analyses, prices, atr_map, rsi_map, total_liquid=5000.0
    )
    assert trace["steps"] == 6
    assert metrics["trades"] > 0
    assert 0.0 <= metrics["peak_capture_ratio"] <= 1.0
    # FETUSDT rejected in synthetic window; at least BTC and SOL entered
    assert any(o["symbol"] == "BTCUSDT" and o.get("side") == "LONG" for o in adapter.orders)
    assert any(o["symbol"] == "SOLUSDT" and o.get("side") == "LONG" for o in adapter.orders)
