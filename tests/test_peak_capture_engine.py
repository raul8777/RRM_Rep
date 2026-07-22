"""Unit tests for the Wolf Hunter Peak Capture Engine."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from peak_capture_engine import (
    PeakCaptureContext,
    PeakCaptureDecision,
    PeakCaptureEngine,
    select_peak_capture_advisory,
)
from unified_governance_supervisor import UnifiedGovernanceContext, evaluate as evaluate_unified_governance


def _make_settings(overrides=None):
    defaults = {
        'profit_predexit_enabled': True,
        'profit_predexit_capture_margin_A': 0.80,
        'profit_predexit_capture_margin_B': 1.00,
        'profit_predexit_capture_margin_C': 1.25,
        'profit_predexit_size_sensitivity': 1.5,
        'profit_predexit_conviction_sensitivity': 0.4,
        'profit_predexit_min_profit_pct': 0.005,
        'profit_predexit_absolute_min_usd': 1.50,
        'profit_predexit_horizon_minutes': 60,
        'profit_predexit_giveback_penalty_weight': 0.5,
        'profit_predexit_early_protection_mode': 'shadow',
        'profit_predexit_early_active_regimes': ['SPOT_BULL'],
        'profit_predexit_early_min_peak_pct': 1.5,
        'profit_predexit_early_min_giveback_pct': 0.5,
        'peak_reconstruction_enabled': True,
        'peak_reconstruction_lookback_candles': 30,
        'max_positions': 4,
    }
    if overrides:
        defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_pos(entry_price=100.0):
    return SimpleNamespace(entry_price=entry_price)


def _make_client_with_klines(highs):
    client = MagicMock()
    client.client.get_klines.return_value = [
        [None, None, str(h), None, None, None, None, None, None, None, None, None]
        for h in highs
    ]
    return client


class TestPeakCaptureEngine:

    def setup_method(self):
        PeakCaptureEngine._peak_cache.clear()

    def test_reconstruct_peak_from_metadata(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        pos = _make_pos(entry_price=100.0)
        metadata = {'trailing_peak': 105.0}
        client = _make_client_with_klines([])

        peak = engine.reconstruct_peak_pnl_pct(client, 'BTCUSDT', pos, 104.0, metadata)

        assert peak == pytest.approx(0.05)
        assert metadata['peak_pnl_pct_real'] == pytest.approx(0.05)
        assert metadata['last_peak_price'] == 105.0

    def test_reconstruct_peak_from_klines(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        pos = _make_pos(entry_price=100.0)
        metadata = {}
        client = _make_client_with_klines([101.0, 103.0, 102.0])

        peak = engine.reconstruct_peak_pnl_pct(client, 'BTCUSDT', pos, 102.0, metadata)

        assert peak == pytest.approx(0.03)
        client.client.get_klines.assert_called_once_with(
            symbol='BTCUSDT', interval='1m', limit=30
        )

    def test_reconstruct_peak_disabled(self):
        settings = _make_settings({'peak_reconstruction_enabled': False})
        engine = PeakCaptureEngine(settings)
        pos = _make_pos(entry_price=100.0)
        metadata = {'trailing_peak': 110.0}
        client = _make_client_with_klines([120.0])

        peak = engine.reconstruct_peak_pnl_pct(client, 'BTCUSDT', pos, 115.0, metadata)

        assert peak == 0.0
        assert 'peak_pnl_pct_real' not in metadata

    def test_evaluate_hold_when_future_ev_higher(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=100.0,
            portfolio_total_usdt=1000.0,
            current_price=105.0,
            entry_price=100.0,
            current_pnl_usdt=5.0,
            current_pnl_pct=0.05,
            peak_pnl_pct=0.05,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.7,
            signal_direction='BUY',
            pnl_velocity_1h=0.001,
            indicators={},
            fear_greed_value=50,
            metadata={},
            prediction_1h={
                'expected_pnl': 15.0,
                'success_probability': 0.95,
                'price_range': {'max': 110.0, 'min': 100.0},
            },
        )

        decision = engine.evaluate(ctx)

        assert decision.should_close is False
        assert decision.reason == 'profit_predexit_hold'
        assert decision.prob_higher_peak > 0.0
        assert decision.capture_margin > 0.0

    def test_evaluate_capture_when_current_pnl_above_adjusted_ev(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=1000.0,
            portfolio_total_usdt=1000.0,
            current_price=108.0,
            entry_price=100.0,
            current_pnl_usdt=8.0,
            current_pnl_pct=0.08,
            peak_pnl_pct=0.10,
            regime_mode='RANGE_SCALP',
            profile_label='B',
            signal_strength=0.4,
            signal_direction='BUY',
            pnl_velocity_1h=-0.001,
            indicators={},
            fear_greed_value=50,
            metadata={},
            prediction_1h={
                'expected_pnl': 1.0,
                'success_probability': 0.3,
                'price_range': {'max': 109.0, 'min': 100.0},
            },
        )

        decision = engine.evaluate(ctx)

        assert decision.should_close is True
        assert decision.reason == 'profit_predexit_capture'
        assert decision.close_pct == 1.0  # prob_higher_peak < 0.35

    def test_evaluate_momentum_exhausted_closes(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=100.0,
            portfolio_total_usdt=1000.0,
            current_price=105.0,
            entry_price=100.0,
            current_pnl_usdt=5.0,
            current_pnl_pct=0.05,
            peak_pnl_pct=0.05,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.7,
            signal_direction='BUY',
            pnl_velocity_1h=0.001,
            indicators={},
            fear_greed_value=50,
            metadata={},
            momentum_exhausted=True,
        )

        decision = engine.evaluate(ctx)

        assert decision.should_close is True
        assert decision.reason == 'profit_predexit_momentum_exhausted'
        assert decision.close_pct == 1.0
        assert decision.priority == 93

    def test_evaluate_below_min_profit_does_nothing(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=10.0,
            portfolio_total_usdt=1000.0,
            current_price=100.1,
            entry_price=100.0,
            current_pnl_usdt=0.1,
            current_pnl_pct=0.001,
            peak_pnl_pct=0.001,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.5,
            signal_direction='BUY',
            pnl_velocity_1h=0.0,
            indicators={},
            fear_greed_value=50,
            metadata={},
        )

        decision = engine.evaluate(ctx)

        assert decision.should_close is False
        assert decision.reason == 'profit_predexit_below_min'

    def test_early_peak_protection_shadows_confirmed_reversal(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='TOWNSUSDT', trade_type='spot', side='BUY',
            position_size_usdt=1000.0, portfolio_total_usdt=2000.0,
            current_price=104.0, entry_price=100.0,
            current_pnl_usdt=40.0, current_pnl_pct=0.04, peak_pnl_pct=0.057,
            regime_mode='SPOT_BULL', profile_label='A', signal_strength=0.5,
            signal_direction='BUY', pnl_velocity_1h=-0.03, indicators={},
            fear_greed_value=25, metadata={},
        )

        decision = engine.evaluate_early_peak_protection(ctx)

        assert decision.should_close is False
        assert decision.reason == 'profit_predexit_early_peak_protection'
        assert decision.metrics['behavior_confirmed'] is True

    def test_early_peak_protection_uses_partial_close_only_when_active(self):
        settings = _make_settings({'profit_predexit_early_protection_mode': 'active'})
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='TOWNSUSDT', trade_type='spot', side='BUY',
            position_size_usdt=1000.0, portfolio_total_usdt=2000.0,
            current_price=104.0, entry_price=100.0,
            current_pnl_usdt=40.0, current_pnl_pct=0.04, peak_pnl_pct=0.057,
            regime_mode='SPOT_BULL', profile_label='A', signal_strength=0.5,
            signal_direction='BUY', pnl_velocity_1h=-0.03, indicators={},
            fear_greed_value=25, metadata={},
        )

        decision = engine.evaluate_early_peak_protection(ctx)

        assert decision.should_close is True
        assert decision.close_pct == 0.5
        assert decision.reason == 'profit_predexit_early_peak_protection'

    def test_early_peak_protection_keeps_non_allowed_regime_in_shadow(self):
        settings = _make_settings({'profit_predexit_early_protection_mode': 'active'})
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT', trade_type='spot', side='BUY',
            position_size_usdt=1000.0, portfolio_total_usdt=2000.0,
            current_price=104.0, entry_price=100.0,
            current_pnl_usdt=40.0, current_pnl_pct=0.04, peak_pnl_pct=0.057,
            regime_mode='RANGE_SCALP', profile_label='A', signal_strength=0.5,
            signal_direction='BUY', pnl_velocity_1h=-0.03, indicators={},
            fear_greed_value=25, metadata={},
        )

        decision = engine.evaluate_early_peak_protection(ctx)

        assert decision.should_close is False
        assert decision.reason == 'profit_predexit_early_peak_protection'
        assert decision.metrics['mode'] == 'shadow'
        assert decision.metrics['regime_allowed'] is False

    def test_unified_governance_does_not_escalate_wolf_early_partial_with_prs(self):
        settings = _make_settings()
        ctx = UnifiedGovernanceContext(
            symbol='TOWNSUSDT', trade_type='spot', side='LONG',
            pnl_pct=0.04, pnl_usdt=40.0, entry_price=100.0, current_price=104.0,
            regime_mode='SPOT_BULL', profile_label='A', peak_pnl_pct=0.057,
            peak_capture_should_close=True, peak_capture_close_pct=0.5,
            peak_capture_reason='profit_predexit_early_peak_protection',
            peak_capture_detail='confirmed early reversal',
            prs_advisory_should_close=True, prs_advisory_close_pct=1.0,
            prs_advisory_reason='prs_loss_momentum_exit',
        )

        decision = evaluate_unified_governance(ctx, settings)

        assert decision.should_close is True
        assert decision.close_pct == 0.5
        assert decision.reason == 'profit_predexit_early_peak_protection'

    def test_early_peak_selection_is_not_overwritten_by_normal_hold(self):
        early = PeakCaptureDecision(
            should_close=True,
            close_pct=0.5,
            reason='profit_predexit_early_peak_protection',
            priority=84,
        )
        normal = PeakCaptureDecision(
            should_close=False,
            reason='profit_predexit_hold',
            priority=99,
        )

        selected, source = select_peak_capture_advisory(early, normal)

        assert selected is early
        assert source == 'early'

    def test_higher_priority_normal_peak_close_replaces_early_protection(self):
        early = PeakCaptureDecision(
            should_close=True,
            close_pct=0.5,
            reason='profit_predexit_early_peak_protection',
            priority=84,
        )
        normal = PeakCaptureDecision(
            should_close=True,
            close_pct=1.0,
            reason='profit_predexit_momentum_exhausted',
            priority=93,
        )

        selected, source = select_peak_capture_advisory(early, normal)

        assert selected is normal
        assert source == 'normal'

    def test_dynamic_close_pct(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        assert engine._dynamic_close_pct(0.0) == 1.0
        assert engine._dynamic_close_pct(0.34) == 1.0
        assert engine._dynamic_close_pct(0.35) == 0.5
        assert engine._dynamic_close_pct(0.59) == 0.5
        assert engine._dynamic_close_pct(0.60) == 0.0
        assert engine._dynamic_close_pct(0.9) == 0.0

    def test_capture_margin_decreases_with_position_size(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        base_ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=250.0,
            portfolio_total_usdt=1000.0,
            current_price=105.0,
            entry_price=100.0,
            current_pnl_usdt=5.0,
            current_pnl_pct=0.05,
            peak_pnl_pct=0.05,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.5,
            signal_direction='BUY',
            pnl_velocity_1h=0.0,
            indicators={},
            fear_greed_value=50,
            metadata={},
        )
        small_margin = engine._compute_capture_margin(base_ctx)

        large_ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=900.0,
            portfolio_total_usdt=1000.0,
            current_price=105.0,
            entry_price=100.0,
            current_pnl_usdt=5.0,
            current_pnl_pct=0.05,
            peak_pnl_pct=0.05,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.5,
            signal_direction='BUY',
            pnl_velocity_1h=0.0,
            indicators={},
            fear_greed_value=50,
            metadata={},
        )
        large_margin = engine._compute_capture_margin(large_ctx)

        # Larger position should be more eager to capture (lower margin)
        assert large_margin < small_margin

    def test_giveback_penalty_reduces_wait_ev(self):
        settings = _make_settings()
        engine = PeakCaptureEngine(settings)
        ctx = PeakCaptureContext(
            symbol='BTCUSDT',
            trade_type='spot',
            side='BUY',
            position_size_usdt=100.0,
            portfolio_total_usdt=1000.0,
            current_price=104.0,
            entry_price=100.0,
            current_pnl_usdt=4.0,
            current_pnl_pct=0.04,
            peak_pnl_pct=0.10,
            regime_mode='SPOT_BULL',
            profile_label='B',
            signal_strength=0.5,
            signal_direction='BUY',
            pnl_velocity_1h=-0.001,
            indicators={},
            fear_greed_value=50,
            metadata={},
            prediction_1h={
                'expected_pnl': 5.0,
                'success_probability': 0.5,
                'price_range': {'max': 110.0, 'min': 95.0},
            },
        )

        decision = engine.evaluate(ctx)

        assert decision.metrics['giveback_penalty'] > 0.0
        assert decision.metrics['risk_adjusted_wait'] < decision.metrics['ev_wait']
