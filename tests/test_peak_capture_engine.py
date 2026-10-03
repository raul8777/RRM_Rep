"""Tests for peak_capture_engine."""
from types import SimpleNamespace

import pytest

from peak_capture_engine import PeakCaptureContext, PeakCaptureEngine


def _settings():
    return SimpleNamespace()


def test_compute_prob_higher_peak_matches_evaluate_output():
    """compute_prob_higher_peak must return the same prob_higher_peak as evaluate()."""
    engine = PeakCaptureEngine(_settings())
    prediction = {
        "success_probability": 0.65,
        "expected_pnl": 5.0,
        "price_range": {"max": 120.0, "min": 95.0},
    }
    ctx = PeakCaptureContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="LONG",
        position_size_usdt=100.0,
        portfolio_total_usdt=1000.0,
        current_price=110.0,
        entry_price=100.0,
        current_pnl_usdt=10.0,
        current_pnl_pct=0.10,
        peak_pnl_pct=0.15,
        regime_mode="SPOT_BULL",
        profile_label=None,
        signal_strength=0.6,
        signal_direction="BUY",
        pnl_velocity_1h=None,
        indicators={},
        fear_greed_value=50.0,
        metadata={},
        leverage=1.0,
        prediction_1h=prediction,
        prediction_4h=None,
        momentum_exhausted=False,
        trend_reversal=False,
        current_sl_price=95.0,
        current_tp_price=120.0,
    )
    standalone = engine.compute_prob_higher_peak(ctx)
    decision = engine.evaluate(ctx)
    assert standalone == pytest.approx(decision.prob_higher_peak, abs=1e-9)
    assert 0.0 <= standalone <= 1.0


def test_compute_prob_higher_peak_returns_zero_when_no_prediction():
    engine = PeakCaptureEngine(_settings())
    ctx = PeakCaptureContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="LONG",
        position_size_usdt=100.0,
        portfolio_total_usdt=1000.0,
        current_price=110.0,
        entry_price=100.0,
        current_pnl_usdt=10.0,
        current_pnl_pct=0.10,
        peak_pnl_pct=0.15,
        regime_mode="SPOT_BULL",
        profile_label=None,
        signal_strength=0.6,
        signal_direction="BUY",
        pnl_velocity_1h=None,
        indicators={},
        fear_greed_value=50.0,
        metadata={},
        leverage=1.0,
        prediction_1h=None,
        prediction_4h=None,
        momentum_exhausted=False,
        trend_reversal=False,
    )
    assert engine.compute_prob_higher_peak(ctx) == 0.0


def test_compute_prob_higher_peak_with_momentum_exhausted():
    engine = PeakCaptureEngine(_settings())
    prediction = {
        "success_probability": 0.80,
        "expected_pnl": 5.0,
        "price_range": {"max": 120.0, "min": 95.0},
    }
    ctx = PeakCaptureContext(
        symbol="BTCUSDT",
        trade_type="spot",
        side="LONG",
        position_size_usdt=100.0,
        portfolio_total_usdt=1000.0,
        current_price=110.0,
        entry_price=100.0,
        current_pnl_usdt=10.0,
        current_pnl_pct=0.10,
        peak_pnl_pct=0.15,
        regime_mode="SPOT_BULL",
        profile_label=None,
        signal_strength=0.6,
        signal_direction="BUY",
        pnl_velocity_1h=None,
        indicators={},
        fear_greed_value=50.0,
        metadata={},
        leverage=1.0,
        prediction_1h=prediction,
        prediction_4h=None,
        momentum_exhausted=True,
        trend_reversal=False,
    )
    prob = engine.compute_prob_higher_peak(ctx)
    assert 0.0 <= prob <= 1.0
