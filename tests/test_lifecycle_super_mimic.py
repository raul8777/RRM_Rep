"""tests/test_lifecycle_super_mimic.py — lifecycle/Super MIMIC bridge tests."""

from lifecycle import (
    LifecycleConfig,
    SignalPolicy,
    EntryPolicy,
    SizingPolicy,
    ExitPolicy,
    PartialTake,
    MomentumExitRule,
    StagnationRule,
    lifecycle_config_to_dict,
    build_mimic_experiment_design,
    run_mimic_backtest,
    compute_metrics,
)


def _config() -> LifecycleConfig:
    return LifecycleConfig(
        regime="SPOT_BULL",
        profile="A",
        signal_policy=SignalPolicy(
            min_signal_strength=0.0,
            min_success_probability=0.0,
            min_expected_pnl=-9999.0,
            min_liquidity_usdt=0.0,
            categories_enabled=("LARGE_CAP", "MIDCAP_ALT"),
            max_candidates=2,
            diversification_mode="category",
        ),
        entry_policy=EntryPolicy(
            min_composite_score=0.0,
            min_expected_pnl=-9999.0,
            min_success_probability=0.0,
            min_liquidity_usdt=0.0,
            max_total_positions=3,
            max_category_positions={"LARGE_CAP": 1, "MIDCAP_ALT": 1},
        ),
        sizing_policy=SizingPolicy(
            regime_allocation_pct=0.8,
            max_per_trade_usdt=500.0,
            kelly_base_pct=0.22,
            kelly_max_pct=0.25,
            conviction_max_pct=0.22,
            category_caps={"LARGE_CAP": 0.5, "MIDCAP_ALT": 0.5},
        ),
        exit_policy=ExitPolicy(
            regime="SPOT_BULL",
            profile="A",
            hard_sl_atr_mult=2.0,
            hard_tp_atr_mult=3.5,
            trailing_activation_pct=0.02,
            trailing_atr_mult=1.5,
            breakeven_activation_pct=0.015,
            partial_takes=(PartialTake(0.25, 1.5, "tp1"),),
            momentum_exit=MomentumExitRule(70, 30, 80, 20),
            stagnation_rule=StagnationRule(6, 0.05, 0.5),
        ),
    )


def test_lifecycle_config_to_dict():
    cfg = _config()
    d = lifecycle_config_to_dict(cfg)
    assert d["regime"] == "SPOT_BULL"
    assert d["profile"] == "A"
    assert "signal_min_strength" in d
    assert "exit_sl_atr_mult" in d


def test_build_experiment_design():
    cfg = _config()
    design = build_mimic_experiment_design(cfg, window="90d")
    assert design["engine"] == "mimic"
    assert design["mode"] == "SPOT_BULL"
    assert design["windows"] == ["90d"]
    assert "fingerprint" in design


def test_run_mimic_backtest():
    cfg = _config()
    analyses = [
        {"symbol": "BTCUSDT", "signal": "BUY", "strength": 0.8, "expected_pnl": 2.0, "success_probability": 0.6, "liquidity_usdt_24h": 1_000_000_000},
        {"symbol": "SOLUSDT", "signal": "BUY", "strength": 0.7, "expected_pnl": 1.5, "success_probability": 0.55, "liquidity_usdt_24h": 50_000_000},
    ]
    adapter, summary = run_mimic_backtest(
        analyses,
        cfg,
        prices={"BTCUSDT": 50000.0, "SOLUSDT": 100.0},
        atrs={"BTCUSDT": 500.0, "SOLUSDT": 2.0},
        rsis={"BTCUSDT": 50.0, "SOLUSDT": 50.0},
        total_liquid=5000.0,
    )
    assert summary["signals"] == 2
    assert len(summary["entries"]) == 2
    assert len(adapter.positions) == 2
    metrics = compute_metrics(adapter)
    assert metrics["trades"] == 0  # no exits yet
