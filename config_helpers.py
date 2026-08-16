"""
config_helpers.py — Centralized, validated configuration accessors.

This module provides a single source of truth for reading trading parameters.
It eliminates three recurring bugs:
  1. Module-level hardcodes that override settings (e.g., LEVERAGE_MAX=2).
  2. getattr(..., 'param', hardcode) defaults that drift from config.py.
  3. SPOT vs FUTURES parameter confusion.

Usage:
    from config_helpers import get_param, get_futures_param, get_spot_param, validate_config
    leverage = get_futures_param(settings, 'leverage')
    sl_pct = get_spot_param(settings, 'sl_pct')
"""

from copy import deepcopy
from typing import Any, Dict, Optional
import json
import logging
import os

logger = logging.getLogger(__name__)


# Default values MUST match config.py defaults. Only one place to change them.
_PARAM_DEFAULTS: Dict[str, Any] = {
    # SPOT Kelly
    'kelly_enabled': True,
    'kelly_base_usdt': 150.0,
    'kelly_max_usdt': 2500.0,
    'kelly_max_pct_liquid': 0.90,  # aggressive calibration: up to 90% of liquid
    'kelly_max_pct_cap': 0.30,
    'kelly_min_usdt': 0.0,
    'kelly_dynamic_base_pct': 0.22,
    'max_per_trade_usdt': 1500.0,

    # FUTURES Kelly
    'futures_kelly_base_usdt': 200.0,
    'futures_kelly_max_usdt': 2500.0,
    'futures_kelly_max_pct_liquid': 0.90,  # aggressive calibration: consistent with SPOT
    'futures_kelly_cap_pct': 0.90,
    'futures_kelly_min_usdt': 0.0,

    # FUTURES trading
    'futures_enabled': True,  # EntryDecision blocks unpromoted futures entries
    'futures_leverage': 10,
    'futures_max_shorts': 4,
    'futures_max_largecap_shorts': 3,
    'futures_max_new_per_cycle': 2,
    'futures_usdt_per_trade': 150.0,
    'futures_sl_pct': 2.00,
    'futures_tp_pct': 4.00,
    'futures_long_sl_pct': 2.5,
    'futures_long_tp_pct': 5.0,
    'futures_symbols': [
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
        'NEARUSDT', 'SUIUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'TAOUSDT',
        'UNIUSDT', 'DOTUSDT', 'BCHUSDT', 'AAVEUSDT', 'FILUSDT',
        'INJUSDT',
    ],
    'futures_largecap_symbols': ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'],

    # SPOT
    'stop_loss_pct': 3.36,
    'take_profit_pct': 4.14,
    'trailing_stop_enabled': True,
    'trailing_stop_activation_pct': 2.0,
    'trailing_stop_distance_pct': 1.0,

    # Anti-ghost / reconciliation
    'spot_dust_cleanup_min_notional_usdt': 5.0,
    'spot_reconcile_auto_fix_enabled': True,
    'spot_reconcile_min_value_usdt': 5.0,

    # Kelly position sizing (aggressive canonical baseline)
    'kelly_base_usdt': 150.0,          # concentrated: base digna para $5,100
    'kelly_max_usdt': 2500.0,          # aggressive calibration: cap por posición elevado
    'kelly_dynamic_base_pct': 0.22,    # aggressive calibration: base crece con capital disponible
    'kelly_conviction_vol2x': 1.8,     # jul07: 1.5→1.8 (wolf-hunter s63)
    'kelly_conviction_vol3x': 2.5,     # jul07: 2.0→2.5 (wolf-hunter s63)
    'kelly_conviction_max_pct': 0.22,  # aggressive canonical cap

    # Signal filters
    'min_signal_strength': 0.60,       # concentrated: solo señales fuertes
    'min_success_probability': 0.60,   # concentrated: edge estadístico alto
    'futures_min_prob': 0.499,
    'futures_vol_spike_min': 0.91,
    'vol_spike_min': 0.91,

    # Entry quality score gate
    'entry_quality_gate_enabled': True,
    'entry_quality_min_vol_spike': 1.2,  # concentrated: volumen confirma dirección
    'entry_quality_min_strength': 0.40,
    'entry_quality_max_failures': 0,  # concentrated: todos los gates deben pasar
    'entry_quality_vol_spike_min_spot_bull': 0.85,
    'entry_quality_vol_spike_floor_spot_bull': 0.70,
    'entry_quality_rsi_min_spot_bull': 33.0,
    'entry_quality_rsi_max_spot_bull': 68.0,
    'entry_quality_vol_spike_min_range_scalp': 0.35,  # s72: lateral = low vol structural; lower floor
    'sso_target_contribution_pct_range_scalp': 0.10,  # s110: raised 0.05->0.10, min_contribution was too small vs $18-27 SL guardrail (RR~0.38)
    'sso_target_range_scalp_guard_mode': 'active',
    'sso_target_range_scalp_guard_notional_min_usdt': 700.0,
    'sso_target_range_scalp_guard_min_pnl_pct': 1.40,
    'sso_target_range_scalp_guard_min_abs_gain_usdt': 9.0,
    'sso_target_range_scalp_guard_min_velocity_pct_h': 0.08,
    'sso_target_range_scalp_guard_min_vol_spike': 1.40,

    # Bayesian evidence gate (superseded by entry_profile_gate; kept disabled)
    'evidence_gate_enabled': False,
    'evidence_gate_min_posterior': 0.65,
    'evidence_gate_prior': 0.50,

    # Entry profile gate (A/B/C tiered selection based on calibrated historical outcomes)
    'entry_profile_gate_enabled': True,
    'entry_profile_gate_thresholds_path': 'Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json',
    'range_scalp_quality_model_mode': 'shadow',
    'range_scalp_quality_model_path': 'Dev/benchmarks/entry_profile_calibration/quality_score_model_range_scalp.pkl',
    'peaks_first_enabled': True,
    'predictor_gate_mode': 'blocking',  # 'blocking' | 'advisory' | 'disabled'
    'entry_quality_gate_mode': 'disabled',  # superseded by entry_profile_gate; 'blocking' kept for legacy overrides
    'entry_decision_enabled': True,
    'entry_decision_policy_version': 'strict_v1',
    'entry_decision_regime_mode': {
        'SPOT_BULL': 'enforced',
        'RANGE_SCALP': 'enforced',
        'FUTURES_SHORT': 'enforced',
        'PRESERVATION': 'enforced',
    },
    'entry_decision_min_expected_pnl': {
        'SPOT_BULL': 0.0,
        'RANGE_SCALP': 0.0,
        'FUTURES_SHORT': 0.0,
        'PRESERVATION': 0.0,
    },
    'entry_decision_min_probability': {
        'SPOT_BULL': 0.50,
        'RANGE_SCALP': 0.50,
        'FUTURES_SHORT': 0.50,
        'PRESERVATION': 0.50,
    },
    # Calibrated thresholds from the shadow-log sweep (2026-07-18).
    'evidence_gate_min_posterior_per_regime': {
        'SPOT_BULL': 0.60,
        'FUTURES_SHORT': 0.80,
        'RANGE_SCALP': 0.52,
        'PRESERVATION': 0.80,
    },

    # Exposure / regime
    'max_total_exposure_pct': 0.95,  # SR strategy: deploy up to 95% of available capital
    'max_positions_spot_bull': 12,
    'max_positions_range_scalp': 8,
    'range_scalp_max_bb_position': 0.40,              # s72: 40% of BB range = lower zone
    'range_scalp_max_rsi': 60.0,                      # s72: 48→60 (lateral RSI normal 45-65)
    'max_positions_preservation': 4,
    'max_positions_futures_short': 8,
    'max_positions': 8,
    # Portfolio concentration policy (slot capacity -> per-position ticket coupling)
    'portfolio_concentration_enabled': True,
    'portfolio_concentration_alpha': 0.90,
    'portfolio_concentration_min_mult': 0.75,
    'portfolio_concentration_max_mult': 1.35,
    'portfolio_concentration_pressure_start': 0.50,
    'portfolio_concentration_pressure_boost': 0.40,
    'portfolio_concentration_baseline_spot_bull': 5,
    'portfolio_concentration_baseline_range_scalp': 4,
    'portfolio_concentration_baseline_preservation': 1,
    'portfolio_concentration_baseline_futures_short': 5,

    # QUBO portfolio selection
    'qubo_enabled': True,
    'qubo_lambda_corr': 1.0,
    'qubo_lambda_size': 5.0,
    'qubo_lambda_risk': 0.5,
    'qubo_solver': 'sqa',
    'qubo_population_size': 30,
    'qubo_iterations': 80,
    'qubo_seed': 42,
    'qubo_min_candidates': 2,
    'qubo_include_open_positions': True,

    # Commissions / BNB — rates are controlled by Binance, not backtest.
    # Defaults are the documented standard rates; live rates are refreshed from API.
    'binance_commission_rate': 0.001,      # 0.1% spot taker
    'futures_commission_rate': 0.0004,     # 0.04% futures taker
    'use_bnb_for_commission': True,
    'bnb_spot_commission_rate': 0.0005,    # 0.05% spot with BNB discount
    'bnb_futures_commission_rate': 0.0002, # 0.02% futures with BNB discount
    'auto_bnb_for_commission': True,
    'slippage_pct': 0.00052,

    # F&G boost
    'fng_kelly_boost_enabled': True,
    'fng_capitulation_threshold': 15,
    'fng_extreme_fear_threshold': 20,
    'fng_extreme_greed_threshold': 75,
    'fng_capitulation_multiplier': 1.50,
    'fng_extreme_fear_multiplier': 1.35,
    'fng_extreme_greed_multiplier': 0.65,

    # ATR SL (SPOT)
    'atr_sl_enabled': True,
    'atr_sl_multiplier': 1.5,
    'atr_sl_max_pct': 0.08,
    'atr_sl_min_pct': 0.015,

    # Trailing stop (SPOT)
    'trailing_stop_enabled': True,
    'trailing_stop_activation_pct': 2.0,
    'trailing_stop_distance_pct': 1.0,

    # Smart TP / scale out (SPOT)
    'smart_tp_enabled': True,
    'smart_tp_max_extensions': 3,
    'smart_tp_max_pnl_pct': 15.0,
    'smart_tp_extend_score_threshold': 2,
    'smart_tp_extension_pct': 2.0,
    'smart_tp_extend_disabled_regimes': ['RANGE_SCALP', 'PRESERVATION'],
    'scale_out_enabled': True,
    'scale_out_first_close_pct': 0.5,
    'sl_confirmation_candles': 1,

    # Spot bounce rescue
    'spot_bounce_rescue_enabled': True,
    'spot_bounce_rescue_max_peak_pct': 0.8,
    'spot_bounce_rescue_min_hours': 6.0,
    'spot_bounce_rescue_target_pnl': -0.50,

    # SSO-1d: Flat Stagnation Timeout
    'sso_flat_stagnation_timeout_enabled': True,
    'sso_flat_stagnation_timeout_hours': 3.0,
    'sso_flat_stagnation_timeout_hours_range_scalp': 3.0,
    'sso_flat_stagnation_timeout_pnl_pct': 0.003,
    'sso_flat_stagnation_timeout_pnl_pct_range_scalp': 0.003,

    # VolSpike override — disabled under the new entry-profile gate; volume
    # spikes are already part of the quality score.  Re-enable only with a
    # dedicated calibration showing positive post-cost outcomes.
    'volspike_override_enabled': False,
    'volspike_override_narrative_only': True,
    'volspike_override_min_spike': 2.0,
    'volspike_override_max_rsi': 65.0,
    'volspike_override_trend_required': True,
    'volspike_override_min_strength': 0.75,
    'volspike_override_max_positions': 1,
    'volspike_override_daily_limit': 1,

    # Screener
    'screener_enabled': True,
    'hard_risk_monitor_enabled': True,
    'hard_risk_monitor_interval_seconds': 2.0,
    'hard_risk_breakeven_enabled': True,
    # High-frequency telemetry watcher for open positions
    'position_telemetry_watcher_enabled': True,
    'position_telemetry_watcher_interval_seconds': 1.0,
    'position_telemetry_db_throttle_seconds': 5.0,
    'position_reaction_policy_mode': 'hard_rules',
    'position_profit_guard_enabled': True,
    'position_profit_guard_activation_pct': 0.35,
    'position_profit_guard_min_peak_pct': 0.50,
    'position_profit_guard_min_giveback_pct': 0.25,
    'position_profit_guard_giveback_factor': 0.55,
    'position_profit_guard_soft_lock_factor': 0.25,
    'position_profit_guard_hard_lock_factor': 0.45,
    'position_profit_guard_floor_lock_pct': 0.05,
    'position_profit_guard_sl_buffer_pct': 0.20,
    'position_profit_guard_min_step_pct': 0.03,
    # s120: Intelligent drawdown monitor
    'drawdown_intelligent_monitor_enabled': True,
    'drawdown_monitor_threshold_usdt': -10.0,
    'drawdown_cut_velocity_threshold': -0.0003,
    'drawdown_cut_min_data_points': 20,
    'drawdown_cut_min_consecutive': 3,
    # s120: Profit erosion detection (anti-strangulation: only tightens SL, never closes)
    'erosion_min_peak_usdt': 5.0,
    'erosion_lock_fraction': 0.30,
    'erosion_lock_fraction_gentle': 0.15,
    'erosion_velocity_threshold': -0.00015,
    'binance_event_stream_enabled': True,
    'binance_event_stream_futures_enabled': True,
    'binance_event_stream_spot_enabled': False,
    'range_scalp_short_first_enabled': False,
    'range_scalp_short_priority_bonus': 0.0,
    'range_scalp_short_preferred': False,  # Neutral = do not bias selection by side
    'range_scalp_long_entries_enabled': True,
    'range_scalp_long_min_probability': 0.50,
    'range_scalp_long_min_expected_pnl': 0.0,
    'range_scalp_long_support_zone_max': 0.55,
    'range_scalp_long_superiority_delta': 0.0,
    'range_scalp_direction_mode': 'ev_symmetric',  # short_priority | ev_symmetric
    'range_scalp_direction_min_probability': 0.55,
    'range_scalp_short_resistance_threshold': 0.65,
    'range_scalp_short_min_probability': 0.50,
    # TEMP (jul 24 2026, RANGE_SCALP universe expansion to ~100 assets):
    # lower liquidity floor to broaden the universe while keeping slippage in check.
    # Execution liquidity filter stays at $3M. Monitor commission vs PnL; rollback if worsens.
    'screener_min_vol_usdt': 1_000_000,
    'screener_futures_min_vol': 10_000_000,
    'screener_max_pairs': 100,
    'screener_max_futures_pairs': 100,
    'screener_cache_minutes': 15,
    'screener_top_n_enabled': True,
    'screener_top_n_count': 100,
    'screener_quality_min_vol_usdt': 1_000_000,
    'screener_quality_max_volatility': 0.50,
    'screener_quality_min_price_usdt': 0.0001,
    'liquidity_filter_enabled': True,
    'min_liquidity_vol_usdt': 3_000_000.0,
    'min_liquidity_vol_usdt_range_scalp': 1_000_000.0,
    'max_slippage_tp_ratio': 0.25,

    # Category / position limits
    'category_max_positions_enabled': False,
    'category_max_positions': {
        "LARGE_CAP": 1, "MIDCAP_ALT": 2, "NARRATIVE": 1, "MEME": 1,
    },
    'token_categories': {},
    'category_thresholds': {},
    'max_positions_per_symbol': 3,
    'overflow_min_strength': 0.85,
    'overflow_min_prob': 0.70,
    'overflow_max_extra': 1,

    # Min hold
    'min_hold_minutes': 120,
    'min_hold_minutes_range_scalp': 15,   # s70: 30→15
    'min_hold_minutes_spot_bull': 120,
    'min_hold_for_prediction_exit': 90,
    'min_hold_for_prediction_exit_range_scalp': 20,    # s71 BUG-4: bounces 15-30min, no esperar 90min
    'min_hold_for_prediction_exit_futures_short': 30,  # s72 B1: shorts move fast; 90min global too slow

    # Rebalance
    'max_rebalance_per_cycle': 2,
    'rebalance_rsi_overbought': 60.0,
    'rebalance_skip_if_bullish_signal': True,
    'replace_ev_min_ratio': 1.1,
    'replace_strength_min_delta': 0.05,
    'replace_only_losing': True,

    # BTC dominance / regime filters
    'btc_dominance_filter_enabled': True,
    'btc_dominance_altcoin_threshold': 56.0,
    'market_regime_enabled': True,
    'preservation_narrative_only': True,
    'preservation_max_rsi': 70.0,
    'preservation_min_vol_spike': 0.8,
    'preservation_kelly_mult': 0.6,
    'range_scalp_sl_pct': 0.8,          # s70: 1.2→0.8 (R:R 2.25)
    'range_scalp_tp_pct': 1.8,          # s70: 1.5→1.8
    'range_scalp_min_rsi': 25.0,        # s70: new
    'range_scalp_breakeven_pct': 0.40,  # s70: new
    'spot_bull_breakeven_pct': 0.25,
    'preservation_sl_pct': 1.5,
    'preservation_tp_pct': 3.0,
    'spot_bull_pnl_boost_enabled': True,
    'spot_bull_btc_4h_up_required': True,
    'spot_bull_pnl_boost_large_cap': 0.0,
    'spot_bull_pnl_boost_midcap': -0.10,
    'spot_bull_pnl_boost_prob_large_cap': -0.02,
    'spot_bull_pnl_boost_prob_midcap': -0.04,
    'spot_ignore_bnb_when_commission_enabled': True,
    'max_total_exposure_spot_bull_fear_fng_threshold': 40,
    'max_total_exposure_spot_bull_fear': 0.95,

    # Cycle lengths
    'cycle_spot_bull': 10,
    'cycle_range_scalp': 3,            # s70: 5→3 min
    'cycle_preservation': 45,
    'cycle_futures_short': 4,
    'trade_cycle_minutes': 5,

    # Prediction-based add / close
    'prediction_exit_enabled': True,
    'allow_prediction_based_add': True,
    'prediction_add_min_strength': 0.70,
    'prediction_add_min_ev_ratio': 1.5,
    'prediction_add_kelly_factor': 0.6,

    # Wolf Hunter Peak Capture Engine (Profit PredExit in gains)
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
    'profit_predexit_early_protection_mode': 'active',
    'profit_predexit_early_active_regimes': ['SPOT_BULL'],
    'profit_predexit_early_min_peak_pct': 1.5,
    'profit_predexit_early_min_giveback_pct': 0.5,
    'loss_focus_enabled': True,
    'loss_focus_threshold_usdt': 9.0,
    'loss_focus_reset_usdt': 6.0,
    'peak_reconstruction_enabled': True,
    'peak_reconstruction_lookback_candles': 30,

    # Staged multi-cycle entry validation (ready state)
    'staged_entry_enabled': True,
    'staged_entry_fast_path_enabled': True,
    'staged_entry_price_source': 'avg',
    'staged_entry_persist_checks': True,
    'staged_hold_policy_enabled': True,
    'staged_hold_soft_ttl_min': 60.0,
    'staged_hold_hard_ttl_min': 120.0,
    'staged_hold_require_block_reason': True,
    'staged_hold_allowed_block_reason_tokens': [
        'CAPITAL_ROUTING',
        'INSUFFICIENT_USDT',
        'MAX_POSITIONS',
        'PER_SYMBOL_LIMIT',
        'EXPOSURE_CAP',
        'SLOT',
    ],
    'directional_policy_mode': 'shadow',
    'funding_fee_guard_mode': 'active',
    'funding_fee_guard_canary_fraction': 0.35,
    'funding_fee_guard_fail_closed': True,
    'funding_guard_warning_fee_usdt': 0.5,
    'funding_guard_block_fee_usdt': 1.25,
    'funding_guard_warning_rate_pct': 0.15,
    'funding_guard_block_rate_pct': 0.35,
    'direction_volume_gate_rollout': 'active_canary',
    'direction_volume_gate_canary_fraction': 0.35,
    'range_reversion_predictor_mode': 'shadow',
    'sr_attempt_sequence_mode': 'shadow',
    'realtime_open_rerank_mode': 'shadow',
    'anti_monopoly_decay_mode': 'shadow',
    'sr_pool_attempt_memory_mode': 'shadow',
    'realtime_open_score_half_life_min': 45.0,
    'realtime_open_score_sr_max_dist_pct': 5.0,
    'preservation_upgrade_to_futures': False,
    'range_scalp_bidirectional_enabled': True,
    'range_scalp_short_resistance_threshold': 0.65,
    'range_scalp_short_max_per_cycle': 2,
    'range_scalp_short_execution_enabled': True,
    'opportunity_shadow_mode': 'active_canary',
    'opportunity_shadow_canary_fraction': 0.20,
    'opportunity_shadow_max_promotions_per_side': 1,
    'opportunity_shadow_rank_slack': 2,
    'range_scalp_short_min_expected_pnl': 0.0,
    'range_scalp_short_min_probability': 0.40,
    'range_scalp_short_usdt_per_trade': 1000.0,
    'range_scalp_short_sl_pct': 2.0,
    'range_scalp_short_tp_pct': 4.0,
    'range_scalp_short_atr_sl_anchor_enabled': True,
    'range_scalp_short_atr_sl_multiplier': 1.5,
    'spot_atr_sl_anchor_enabled': True,
    'spot_atr_sl_multiplier': 1.5,
    'staged_immediate_entry_enabled': False,
    'staged_immediate_entry_fraction': 0.3,
    'range_scalp_sr_thesis_gate_enabled': True,
    'range_scalp_sr_thesis_max_dist_pct': 5.0,
    'sr_pool_interval': '5m',
    'sr_pool_kline_limit': 120,
    'staged_short_risk_guard_enabled': True,
    'staged_short_risk_guard_canary_fraction': 0.35,
    'staged_short_risk_guard_max_resistance_dist_pct': 3.0,
    'staged_short_risk_guard_min_risk_flags': 2,
    'staged_short_risk_guard_volume_anomaly_multiplier': 3.0,
    'futures_sl_cooldown_window_hours': 24.0,
    'futures_sl_cooldown_extra_hours_per_loss': 1.0,
    'futures_sl_cooldown_max_hours': 8.0,
    # s106 NOTE: config.py::Settings.staged_entry_regime_config is the REAL,
    # LIVE-EFFECTIVE source for these values in production — this dict is only
    # the fallback used if _sync_canonical_defaults_from_settings() (below)
    # fails at import time; that function overwrites _PARAM_DEFAULTS (and thus
    # _DEFAULT_STAGED_ENTRY_REGIME_CONFIG) with config.py's values on every
    # startup. Editing ONLY this dict has NO effect on a running system.
    # Keep both copies identical; drift here silently does nothing.
    'staged_entry_regime_config': {
        'SPOT_BULL': {
            'enabled': True,
            'checks_per_cycle': 3,
            'required_positive_checks': 1,
            'max_cycles': 100,  # s105: decoupled from the 8h max_age_min ceiling
            'max_age_min': 480,  # s105: a breakout candidate may legitimately wait up to 8h
            'intra_mode': 'predictor',
            'price_source': 'avg',
            'use_fear_greed': True,
            'cooldown_min': 0,
            'require_inter_positive': False,
            'require_predictor_valid': True,
            'min_expected_pnl': 0.0,
            'policy_version': 'v1',
            'max_open_per_cycle': 2,
            'direction_confirmation_mode': 'disabled',
            'direction_min_progress_bps': 5.0,
            'direction_max_adverse_bps': 50.0,
            'direction_required_confirmations': 2,
            'direction_max_adverse_observations': 3,
            'direction_sr_invalidation_pct': 1.5,
            'sr_lookback_bars': 96,
            'sr_pivot_order': 3,
            'sr_near_band_atr_mult': 0.25,
            'sr_near_band_price_pct': 0.0025,
            'sr_pool_zone_support_max': 0.20,
            'sr_pool_zone_resistance_min': 0.80,
            'sr_pool_min_strength': 0.0,
            'sr_side_guard_mode': 'active_canary',
            'sr_side_guard_canary_fraction': 0.35,
            'sr_side_guard_max_dist_pct': 2.5,
            'sr_side_guard_breakout_min_pressure': 0.65,
            'sr_side_guard_breakout_buffer_pct': 0.15,
            'sr_side_guard_breakout_max_away_pct': 1.20,
        },
        'RANGE_SCALP': {
            'enabled': True,
            'checks_per_cycle': 2,
            'required_positive_checks': 1,
            'max_cycles': 100,  # s105: decoupled from the 8h max_age_min ceiling
            'max_age_min': 480,  # s105: a breakout candidate may legitimately wait up to 8h
            'intra_mode': 'watcher',
            'watcher_interval_seconds': 10,
            'price_source': 'avg',
            'use_fear_greed': True,
            'cooldown_min': 0,
            'require_inter_positive': False,
            'require_predictor_valid': True,
            'policy_version': 'v1',
            'max_open_per_cycle': 2,
            'direction_confirmation_mode': 'enforced',
            'direction_min_progress_bps': 1.0,
            'direction_max_adverse_bps': 150.0,
            'direction_required_confirmations': 1,
            'direction_max_adverse_observations': 4,
            'direction_sr_invalidation_pct': 1.5,
            'sr_lookback_bars': 96,
            'sr_pivot_order': 3,
            'sr_near_band_atr_mult': 0.25,
            'sr_near_band_price_pct': 0.0025,
            'sr_pool_zone_support_max': 0.20,
            'sr_pool_zone_resistance_min': 0.80,
            'sr_pool_min_strength': 0.0,
            'direction_geometry_anchor_enabled': True,
            'direction_min_progress_stop_fraction': 0.05,
            'direction_max_adverse_stop_fraction': 0.6,
            'direction_volume_gate_enabled': True,
            'direction_volume_gate_canary_fraction': 0.35,
            'direction_volume_anomaly_multiplier': 3.0,
            'sr_side_guard_mode': 'enforced',
            'sr_side_guard_canary_fraction': 1.0,
            'sr_side_guard_max_dist_pct': 1.2,
            'sr_side_guard_breakout_min_pressure': 0.60,
            'sr_side_guard_breakout_buffer_pct': 0.10,
            'sr_side_guard_breakout_max_away_pct': 0.90,
        },
        'FUTURES_SHORT': {
            'enabled': True,
            'checks_per_cycle': 1,
            'required_positive_checks': 2,
            'max_cycles': 100,  # s105: decoupled from the 8h max_age_min ceiling
            'max_age_min': 480,  # s105: a breakout candidate may legitimately wait up to 8h
            'intra_mode': 'watcher',
            'watcher_interval_seconds': 10,
            'price_source': 'ticker',
            'use_fear_greed': False,
            'cooldown_min': 0,
            'require_inter_positive': True,
            'require_predictor_valid': True,
            'min_expected_pnl': 0.0,
            'policy_version': 'v1',
            'max_open_per_cycle': 2,
            'direction_confirmation_mode': 'enforced',
            'direction_min_progress_bps': 3.0,
            'direction_max_adverse_bps': 50.0,
            'direction_required_confirmations': 2,
            'direction_max_adverse_observations': 2,
            'direction_sr_invalidation_pct': 1.5,
            'sr_lookback_bars': 96,
            'sr_pivot_order': 3,
            'sr_near_band_atr_mult': 0.25,
            'sr_near_band_price_pct': 0.0025,
            'sr_pool_zone_support_max': 0.20,
            'sr_pool_zone_resistance_min': 0.80,
            'sr_pool_min_strength': 0.0,
            'sr_side_guard_mode': 'enforced',
            'sr_side_guard_canary_fraction': 1.0,
            'sr_side_guard_max_dist_pct': 1.5,
            'sr_side_guard_breakout_min_pressure': 0.60,
            'sr_side_guard_breakout_buffer_pct': 0.10,
            'sr_side_guard_breakout_max_away_pct': 1.00,
        },
        'PRESERVATION': {
            'enabled': True,
            'checks_per_cycle': 1,
            'required_positive_checks': 2,
            'max_cycles': 100,  # s105: decoupled from the 8h max_age_min ceiling
            'max_age_min': 480,  # s105: a breakout candidate may legitimately wait up to 8h
            'intra_mode': 'watcher',
            'watcher_interval_seconds': 10,
            'price_source': 'avg',
            'use_fear_greed': True,
            'cooldown_min': 0,
            'require_inter_positive': True,
            'require_predictor_valid': True,
            'min_expected_pnl': 0.0,
            'policy_version': 'v1',
            'max_open_per_cycle': 2,
            'direction_confirmation_mode': 'enforced',
            'direction_min_progress_bps': 3.0,
            'direction_max_adverse_bps': 50.0,
            'direction_required_confirmations': 2,
            'direction_max_adverse_observations': 3,
            'direction_sr_invalidation_pct': 1.5,
            'sr_lookback_bars': 96,
            'sr_pivot_order': 3,
            'sr_near_band_atr_mult': 0.25,
            'sr_near_band_price_pct': 0.0025,
            'sr_pool_zone_support_max': 0.20,
            'sr_pool_zone_resistance_min': 0.80,
            'sr_pool_min_strength': 0.0,
            'sr_side_guard_mode': 'active_canary',
            'sr_side_guard_canary_fraction': 0.35,
            'sr_side_guard_max_dist_pct': 2.0,
            'sr_side_guard_breakout_min_pressure': 0.65,
            'sr_side_guard_breakout_buffer_pct': 0.15,
            'sr_side_guard_breakout_max_away_pct': 1.10,
        },
    },

    # Narrative tokens
    'narrative_tokens': [],
    'bear_market_volspike_min': 2.0,

    # SSO per-regime timing (s70: replaces hardcoded multipliers in smart_scale_out.py)
    # Globals below are NEVER modified at runtime — these per-regime fields are the source of truth.
    'sso_stagnant_min_hours_spot_bull': 3.75,
    'sso_stagnant_min_hours_range_scalp': 1.5,
    'sso_stagnant_min_hours_preservation': 2.25,
    'sso_stagnant_min_hours_futures_short': 2.38,
    'sso_slow_min_hours_spot_bull': 2.8,
    'sso_slow_min_hours_range_scalp': 1.0,
    'sso_slow_min_hours_preservation': 2.0,
    'sso_slow_min_hours_futures_short': 1.4,
    'sso_decay_min_hours_spot_bull': 2.3,
    'sso_decay_min_hours_range_scalp': 1.7,
    'sso_decay_min_hours_preservation': 2.0,
    'sso_decay_min_hours_futures_short': 2.0,

    # Entry quality gate per regime (s70: replaces global entry_quality_max_failures)
    'entry_quality_max_failures_spot_bull': 0,
    'entry_quality_max_failures_range_scalp': 1,
    'entry_quality_max_failures_preservation': 0,
    'entry_quality_max_failures_futures_short': 0,

    # SmartTP EXTEND per regime (s70: replaces smart_tp_extend_disabled_regimes list)
    'smart_tp_extend_enabled_spot_bull': True,
    'smart_tp_extend_enabled_range_scalp': True,
    'smart_tp_extend_enabled_preservation': False,
    'smart_tp_extend_enabled_futures_short': True,
    'smart_tp_score_threshold_range_scalp': 2,  # s71 BUG-7: R:R 5.6:1 to extend, 2/4 sufficient

    # BB lower touch tolerance per mode (s70: replaces hardcode *1.005 in autonomous_signals.py)
    'bb_lower_touch_tolerance_pct': 0.5,
    'bb_lower_touch_tolerance_range_scalp_pct': 1.5,

    # SSO
    'sso_enabled': True,
    'sso_target_contribution_enabled': True,   # re-enabled with a more conservative daily goal
    'sso_target_contribution_pct': 0.10,       # recommendation: 150 * 0.10 = 15 USD minimum contribution
    'sso_target_daily_usd': 150.0,
    'sso_stagnant_min_hours': 2.5,          # concentrated: ligeramente más agresivo en rotación
    'sso_stagnant_velocity_window_min': 60, # jul07: 30→60 (wolf-hunter: more stable window)
    'sso_decay_threshold_pct': 0.008,       # jul07: 0.004→0.008 (wolf-hunter)
    'sso_decay_min_hours': 2.0,             # concentrated: reacción más rápida a decay
    'sso_min_winner_profit_usdt': 0.0,
    'sso_slow_tier2_hours': 5.0,            # jul07: 4→5h (wolf-hunter)
    'sso_slow_tier3_hours': 7.0,            # jul07: 6→7h (wolf-hunter)
    'sso_slow_tier4_hours': 9.0,            # jul07: 8→9h (wolf-hunter)

    # SSO-7: Stagnation Trailing + Opportunity Collapse (wolf-hunter s63 redesign)
    'sso7_enabled': True,
    'sso7_min_age_hours': 1.0,
    'sso7_max_pnl_usdt': 3.0,
    'sso7_vol_spike_breakout_min': 2.0,
    'sso7_min_velocity': 0.0003,
    'sso7_stagnation_cycles_threshold': 3,
    'sso7_trail_base_dist': 0.010,
    'sso7_trail_squeeze_per_cycle': 0.10,
    'sso7_trail_min_dist': 0.003,
    'sso7_opp_enabled': True,
    'sso7_opp_min_cycles_stagnant': 3,
    'sso7_opp_ev_min_ratio': 1.35,
    'sso7_opp_prob_min_delta': 0.06,
    'sso7_opp_min_strength': 0.55,
    'sso7_opp_min_vol_spike': 1.2,

    # Funding rate filter
    'funding_rate_spot_filter_enabled': True,
    'funding_rate_spot_max': 0.0008,

    # WR screener
    'wr_screener_enabled': True,
    'wr_screener_consecutive_sl_block': 5,
    'wr_screener_short_window_minutes': 120.0,
    'wr_screener_short_window_min_losses': 2,

    # Market Regime
    'current_regime_mode': 'SPOT_BULL',
    'current_regime_profile_label': 'B',

    # Futures mode
    'futures_hedge_mode_enabled': True,
    'futures_max_longs': 3,
    'futures_long_enabled': True,
    'futures_long_in_range_scalp': True,
    'futures_long_in_spot_bull': True,
    'futures_long_rsi_threshold': 35.0,
    'futures_no_short_btc_4h_threshold': 2.0,
    'futures_no_short_btc_1h_threshold': 0.8,
    'futures_no_short_btc_3h_threshold': 1.0,
    'futures_confirm_red_candle': True,
    'futures_min_btc_7d_drop': -7.0,
    'futures_min_inject_strength': 0.50,
    'futures_sl_cooldown_hours': 4.0,
    'futures_mass_sl_threshold': 3,
    'futures_mass_sl_cooldown_hours': 4.0,
    'futures_mass_sl_window_minutes': 75.0,
    'futures_staged_btc_momentum_gate_enabled': True,
    'portfolio_drawdown_circuit_breaker_enabled': True,
    'portfolio_drawdown_window_minutes': 120.0,
    'portfolio_drawdown_max_loss_usd': 150.0,
    'portfolio_drawdown_recheck_momentum_minutes': 30.0,
    'portfolio_drawdown_recheck_momentum_pct': 0.5,
    'futures_max_escalamientos': 2,
    'futures_protect_profit_usdt': -5.0,
    'min_cycles_before_close_shorts': 5,
    'use_unified_portfolio_manager': False,

    # Futures overflow
    'futures_overflow_min_strength': 0.65,
    'futures_overflow_min_drop_24h': -3.0,
    'futures_overflow_max_extra': 1,
    'futures_overflow_min_vol_spike': 2.0,

    # Futures trailing / smart exits
    'futures_trailing_enabled': True,
    'futures_trailing_activation_pct': 2.5,
    'futures_trailing_distance_pct': 2.0,
    'futures_breakeven_activation_pct': 1.0,
    'futures_smart_tp_enabled': True,
    'futures_smart_tp_max_extensions': 2,
    'futures_smart_tp_max_pnl_pct': 12.0,
    'futures_smart_tp_extend_score_threshold': 2,
    'futures_smart_tp_extension_pct': 2.0,
    'futures_trail_atr_enabled': True,
    'futures_trail_floor_pct': 0.4,
    'futures_trail_ceiling_pct': 1.2,
    'futures_trail_atr_fallback_distance_pct': 1.5,
    'futures_trail_atr_multiplier_deep': 0.8,
    'futures_trail_atr_multiplier_high': 1.0,
    'futures_trail_atr_multiplier_mid': 1.2,
    'futures_trail_atr_multiplier_low': 1.5,
    'futures_bounce_rescue_enabled': True,
    'futures_bounce_rescue_max_peak_pct': 0.5,
    'futures_bounce_rescue_min_hours': 3.0,
    'futures_bounce_rescue_target_pnl': -0.50,
    'futures_momentum_reversal_enabled': True,
    'futures_momentum_reversal_min_peak': 2.0,
    'futures_momentum_reversal_drawback': 0.8,
    'futures_adverse_exit_enabled': True,
    'futures_adverse_exit_loss_pct': 0.8,
    'futures_adverse_exit_min_hours': 0.75,
    'futures_adverse_exit_min_strength': 0.45,
    # SR structure invalidation early exit (shadow first)
    'sr_invalidation_exit_enabled': True,
    'sr_invalidation_shadow_mode': True,
    'sr_invalidation_loss_pct': 0.5,
    'sr_invalidation_confirmation_bars': 3,
    'sr_invalidation_min_strength': 0.50,
    'sr_invalidation_only_with_loss': True,
    'sr_invalidation_lookback_minutes': 10.0,
    'profit_realization_enabled': True,
    'profit_realization_spot_enabled': True,
    'profit_realization_spot_min_profit_pct': 0.0025,
    'profit_realization_spot_partial_minutes': 60.0,   # jul07: 35→60 (wolf-hunter)
    'profit_realization_spot_full_minutes': 120.0,     # jul07: 70→120 (wolf-hunter)
    'profit_realization_spot_drawback_pct': 0.0080,    # jul07: 0.004→0.008 (wolf-hunter)
    'profit_realization_spot_partial_close_pct': 0.60,
    'profit_realization_spot_max_extensions': 2,
    'profit_realization_spot_partial_min_profit_multiplier': 2.5,
    'profit_realization_spot_bull_min_partial_minutes': 90.0,  # jul07: 45→90 (wolf-hunter)
    'profit_realization_spot_partial_minutes_range_scalp': 22.0,  # s71 BUG-6: 35→22 (bounces peak at 15-25min)
    'profit_realization_spot_full_minutes_range_scalp': 45.0,    # s71 BUG-6: 70→45 (full exit within bounce window)
    'profit_realization_futures_enabled': True,
    'profit_realization_futures_min_profit_pct': 0.0030,
    'profit_realization_futures_partial_minutes': 30.0,
    'profit_realization_futures_full_minutes': 60.0,
    'profit_realization_futures_drawback_pct': 0.0035,
    'profit_realization_futures_partial_close_pct': 0.60,
    'profit_realization_futures_max_extensions': 1,
    'profit_realization_futures_partial_min_profit_multiplier': 2.5,
    'profit_realization_sr_accel_enabled': True,
    'profit_realization_sr_accel_partial_multiplier': 0.80,
    'profit_realization_sr_accel_full_multiplier': 0.85,
    'profit_realization_sr_accel_drawback_multiplier': 0.85,
    'profit_realization_sr_resistance_zone_min': 0.68,
    'unified_governance_enabled': True,
    'unified_governance_profit_enabled': True,
    'ug_breakout_follow_enabled': True,
    'ug_breakout_follow_mode': 'active',
    'ug_breakout_follow_min_hold_minutes': 45.0,
    'ug_breakout_follow_max_hold_minutes': 240.0,
    'ug_breakout_follow_min_notional_usdt': 700.0,
    'ug_breakout_follow_min_pnl_usdt': 2.0,
    'ug_breakout_follow_max_pnl_pct': 0.80,
    'ug_breakout_follow_min_signal_strength': 0.58,
    'ug_breakout_follow_min_velocity_pct_h': 0.08,
    'ug_breakout_follow_cut_signal_strength': 0.62,
    'ug_breakout_follow_cut_velocity_pct_h': 0.12,
    'unified_governance_min_profit_hold_minutes': 15.0,
    'lifecycle_structure_mode': 'active',
    'lifecycle_structure_partial_close_pct': 0.35,
    'position_intelligence_enabled': True,
    'position_intelligence_sr_refresh_seconds': 30.0,
    'position_intelligence_min_confidence': 0.3,
    'position_intelligence_emergency_confidence': 0.7,
    'unified_governance_loss_enabled': True,
    'unified_governance_loss_min_age_profile_a': 90.0,    # jul07: 30→90 (wolf-hunter: SL lógico primero)
    'unified_governance_loss_min_age_profile_b': 120.0,   # jul07: 60→120
    'unified_governance_loss_min_age_profile_c': 90.0,    # jul07: 45→90
    'unified_governance_futures_loss_min_age_profile_a': 30.0,   # jul07: 20→30
    'unified_governance_futures_loss_min_age_profile_b': 60.0,   # jul07: 45→60
    'unified_governance_futures_loss_min_age_profile_c': 45.0,   # jul07: 30→45
    'unified_governance_loss_escalation_age_profile_a': 240.0,   # jul07: 120→240
    'unified_governance_loss_escalation_age_profile_b': 180.0,   # jul07: 90→180
    'unified_governance_loss_escalation_age_profile_c': 120.0,   # jul07: 60→120
    'unified_governance_futures_loss_escalation_age_profile_a': 120.0,  # jul07: 90→120
    'unified_governance_futures_loss_escalation_age_profile_b': 90.0,   # jul07: 70→90
    'unified_governance_futures_loss_escalation_age_profile_c': 60.0,   # jul07: 45→60
    'unified_governance_loss_distance_to_sl_min_pct': 0.50,
    'unified_governance_loss_trajectory_acceleration_pct': 0.50,
    'unified_governance_loss_signal_reversal_strength': 0.65,
    'unified_governance_monitor_only_force_loss_pct': 2.0,
    'unified_governance_monitor_only_force_age_minutes': 90.0,
    'unified_governance_loss_min_usdt': 7.50,  # concentrated: 0.5% de $1,500 = umbral real
    # USD loss guard — dynamic SL cap per trade without reducing Kelly sizing
    'max_loss_usd_per_trade': 18.0,
    'emergency_gap_loss_multiplier': 1.5,
    'ug_fast_loss_cut_enabled': False,
    'ug_peak_drawdown_cut_enabled': False,
    'ug_soft_supervisor_loss_escalation_enabled': False,
    'ug_loss_policy_mode': 'active_canary',
    'ug_loss_policy_canary_fraction': 0.25,
    'futures_binance_verify_interval_cycles': 3,

    # Futures capital bridge
    'futures_capital_bridge_enabled': True,
    'futures_capital_bridge_min_transfer': 10.0,

    # Portfolio manager
    'default_trade_size': 10.0,
    'min_pnl_improvement_threshold': 0.5,
    'min_success_probability_improvement': 0.2,
    'enable_prediction_based_closing': True,

    # PnL service
    'trade_horizon_minutes': 240,

    # BNB commission
    'bnb_min_balance_usd': 5.0,
    'bnb_extra_buffer_usd': 10.0,
    'bnb_buffer_days': 7,
    'bnb_max_reserve_usd': 100.0,

    # Trading symbol lists
    'analysis_symbols': [],
    'trading_excluded_symbols': [],
    'auto_close_excluded_on_startup': True,

    # Circuit breakers
    'circuit_breaker_enabled': True,
    'circuit_breaker_consecutive_failures': 5,
    'circuit_breaker_pause_minutes': 10,

    # Safety / misc
    'dry_run': True,
    'allow_real_trading': False,
    'funding_rate_cache_minutes': 10,
    'backtest_safe_only': True,

    # Rebalancing cap (s66: H6/H7 — was hardcoded 0.5 in getattr fallback)
    'rebalance_max_abs_pnl_pct': 3.0,

    # Peak protection SPOT_BULL (s66: H7 — ensure get_param() has correct fallback)
    'peak_protection_spot_bull_pct': 0.65,
    'peak_protection_spot_bull_profile_a_pct': 0.70,

    # SR-pool runtime switches
    'sr_pool_enabled': False,
    'sr_pool_trade_enabled': False,
    'sr_pool_full_scan_minutes': 5,
    'sr_pool_lookback_minutes': 150,
    'sr_pool_max_open_per_cycle': 2,
    'sr_pool_intra_cycle_enabled': False,
}


# Mapping from trade_type to parameter name prefix/suffix for pairs that differ by mode.
_TRADE_TYPE_PARAM_MAP: Dict[str, Dict[str, str]] = {
    'spot': {
        'sl_pct': 'stop_loss_pct',
        'tp_pct': 'take_profit_pct',
        'kelly_base': 'kelly_base_usdt',
        'kelly_max': 'kelly_max_usdt',
        'kelly_max_pct_liquid': 'kelly_max_pct_liquid',
    },
    'futures': {
        'sl_pct': 'futures_sl_pct',
        'tp_pct': 'futures_tp_pct',
        'kelly_base': 'futures_kelly_base_usdt',
        'kelly_max': 'futures_kelly_max_usdt',
        'kelly_max_pct_liquid': 'futures_kelly_max_pct_liquid',
    },
}


# Canonical guardrail: recalibration may tune many knobs, but Kelly must not
# fall below the aggressive baseline.
_AGGRESSIVE_KELLY_FLOORS: Dict[str, float] = {
    'kelly_base_usdt': 150.0,
    'kelly_max_usdt': 2500.0,
    'kelly_max_pct_liquid': 0.90,
    'kelly_dynamic_base_pct': 0.22,
    'kelly_conviction_max_pct': 0.22,
    'futures_kelly_base_usdt': 200.0,
    'futures_kelly_max_usdt': 2500.0,
    'futures_kelly_max_pct_liquid': 0.90,
    'futures_kelly_cap_pct': 0.90,
}


def enforce_aggressive_kelly_floor(name: str, value: Any) -> Any:
    """Clamp Kelly scalars so runtime overrides cannot downgrade aggressiveness."""
    floor = _AGGRESSIVE_KELLY_FLOORS.get(name)
    if floor is None:
        return value
    try:
        numeric = float(value)
    except Exception:
        return value
    return max(numeric, floor)


def _sync_canonical_defaults_from_settings() -> None:
    """Keep hardcoded helper defaults aligned with config.Settings defaults."""
    try:
        from config import Settings as _Settings  # Local import to avoid startup cycles.

        fields = getattr(_Settings, "model_fields", {}) or {}
        for name, field in fields.items():
            if field is None:
                continue
            default = getattr(field, "default", None)
            if default is not None:
                _PARAM_DEFAULTS[name] = deepcopy(default)
    except Exception as exc:
        # Don't fail silently: if this sync fails, _PARAM_DEFAULTS keeps the
        # hardcoded fallback values above, which can silently drift from the
        # real config.Settings defaults (e.g. staged_entry_regime_config) and
        # make the system run stricter/looser thresholds than intended.
        logger.warning(f"_sync_canonical_defaults_from_settings failed, using hardcoded fallback defaults: {exc}")
        return


_sync_canonical_defaults_from_settings()


def get_param(settings, name: str, default: Optional[Any] = None, *,
              warn_if_default: bool = False) -> Any:
    """Read a parameter from settings, falling back to the canonical default."""
    if default is None and name in _PARAM_DEFAULTS:
        default = _PARAM_DEFAULTS[name]
    value = getattr(settings, name, default)
    if warn_if_default and value == default:
        logger.warning(f"Config param {name} using default value {default}")
    return value


def get_spot_param(settings, name: str) -> Any:
    """Read a SPOT-specific parameter by its trade-type-neutral name."""
    mapped = _TRADE_TYPE_PARAM_MAP['spot'].get(name)
    if mapped is None:
        raise KeyError(f"No SPOT mapping for param '{name}'")
    return get_param(settings, mapped)


def get_futures_param(settings, name: str) -> Any:
    """Read a FUTURES-specific parameter by its trade-type-neutral name."""
    mapped = _TRADE_TYPE_PARAM_MAP['futures'].get(name)
    if mapped is None:
        raise KeyError(f"No FUTURES mapping for param '{name}'")
    return get_param(settings, mapped)


def get_trade_param(settings, name: str, trade_type: str) -> Any:
    """Read a parameter aware of trade_type ('spot' or 'futures')."""
    tt = trade_type.lower()
    if tt == 'spot':
        return get_spot_param(settings, name)
    if tt in ('futures', 'future'):
        return get_futures_param(settings, name)
    raise ValueError(f"trade_type must be 'spot' or 'futures', got {trade_type}")


_DEFAULT_STAGED_ENTRY_REGIME_CONFIG = _PARAM_DEFAULTS.get('staged_entry_regime_config', {})


def get_staged_entry_regime_config(settings, regime: str) -> Dict[str, Any]:
    """Return merged staged-entry config for a regime with canonical defaults."""
    global_config = None
    if settings is not None:
        global_config = getattr(settings, 'staged_entry_regime_config', _DEFAULT_STAGED_ENTRY_REGIME_CONFIG)
    if not global_config:
        global_config = _DEFAULT_STAGED_ENTRY_REGIME_CONFIG
    regime_config = (global_config or {}).get(regime, {})
    default_config = _DEFAULT_STAGED_ENTRY_REGIME_CONFIG.get(regime, {}) if isinstance(_DEFAULT_STAGED_ENTRY_REGIME_CONFIG, dict) else {}
    merged = dict(default_config or {})
    merged.update(regime_config)
    enabled_global = False
    if settings is not None:
        enabled_global = bool(getattr(settings, 'staged_entry_enabled', True))
    merged['enabled'] = bool(merged.get('enabled', False)) and enabled_global
    # Allow RANGE_SCALP to use a single confirmation for fast SR bounces.
    if regime != "RANGE_SCALP":
        merged['direction_required_confirmations'] = max(2, int(merged.get('direction_required_confirmations', 2) or 2))
    elif bool(getattr(settings, 'range_scalp_short_first_enabled', False)):
        merged['direction_confirmation_mode'] = 'enforced'
        merged['direction_required_confirmations'] = min(max(1, int(merged.get('direction_required_confirmations', 1) or 1)), 1)
        merged['direction_min_progress_bps'] = min(float(merged.get('direction_min_progress_bps', 1.0) or 1.0), 1.0)
    if regime == "RANGE_SCALP" and settings is not None:
        rollout_mode = str(getattr(settings, "direction_volume_gate_rollout", "active_canary") or "active_canary").strip().lower()
        if rollout_mode == "active_canary":
            merged['direction_volume_gate_enabled'] = True
            merged['direction_volume_gate_canary_fraction'] = max(
                0.0,
                min(1.0, float(getattr(settings, "direction_volume_gate_canary_fraction", 0.35) or 0.0)),
            )
    return merged


def validate_config(settings) -> Dict[str, Any]:
    """Return a list of warnings/errors about dangerous config mismatches."""
    issues = []

    # Hard safety limits
    lev = get_param(settings, 'futures_leverage')
    if lev > 20:
        issues.append({'severity': 'error', 'param': 'futures_leverage',
                       'message': f'futures_leverage={lev} exceeds safe maximum of 20'})
    if lev < 1:
        issues.append({'severity': 'error', 'param': 'futures_leverage',
                       'message': f'futures_leverage={lev} must be >= 1'})

    # Consistency checks
    f_cap = get_param(settings, 'futures_kelly_max_pct_liquid')
    s_cap = get_param(settings, 'kelly_max_pct_liquid')
    if f_cap < s_cap * 0.5:
        issues.append({'severity': 'warning', 'param': 'futures_kelly_max_pct_liquid',
                       'message': f'futures_kelly_max_pct_liquid ({f_cap}) much lower than spot ({s_cap})'})

    # Canonical default parity: config.py must not drift from _PARAM_DEFAULTS.
    try:
        from config import Settings
        for name, default in _PARAM_DEFAULTS.items():
            model_field = Settings.model_fields.get(name)
            if model_field is None:
                issues.append({'severity': 'error', 'param': name,
                               'message': f'{name} missing from config.Settings'})
                continue
            model_default = model_field.default
            if model_default != default:
                issues.append({'severity': 'error', 'param': name,
                               'message': f'config_helpers default {default!r} != config.Settings default {model_default!r}'})
    except Exception as e:
        issues.append({'severity': 'error', 'param': '_PARAM_DEFAULTS',
                       'message': f'Unable to validate config default parity: {e}'})

    # Check for known drift against hardcodes (legacy safety)
    if hasattr(settings, 'futures_leverage') and settings.futures_leverage != lev:
        # unreachable but documents intent
        pass

    return {'ok': len([i for i in issues if i['severity'] == 'error']) == 0, 'issues': issues}


def dump_active_config(settings) -> Dict[str, Any]:
    """Return a snapshot of all canonical parameters for persistence."""
    return {name: get_param(settings, name) for name in _PARAM_DEFAULTS}


def load_runtime_overrides_from_db(settings, db_client) -> Dict[str, Any]:
    """Reload persisted hot-config overrides from DB into the live settings object.

    This function is intentionally non-fatal: any parse/apply failure is logged and skipped.
    """
    result = {
        "ok": True,
        "applied_scalars": 0,
        "applied_thresholds": [],
        "errors": [],
    }
    if settings is None or db_client is None:
        result["ok"] = False
        result["errors"].append("missing settings or db_client")
        return result

    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
        )
        if rows and rows[0].get("value") is not None:
            raw = rows[0].get("value")
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                for field, change in raw.items():
                    if field == "category_thresholds":
                        continue
                    if not hasattr(settings, field):
                        continue
                    # If an explicit env var exists for this field, it is authoritative.
                    # Pydantic Settings maps FIELD_NAME from the environment to field_name.
                    if os.environ.get(field.upper()) is not None:
                        continue
                    new_value = None
                    if isinstance(change, dict) and "new" in change:
                        new_value = change.get("new")
                    elif not isinstance(change, dict):
                        new_value = change
                    if new_value is None:
                        continue
                    try:
                        _clamped = enforce_aggressive_kelly_floor(field, new_value)
                        cur = getattr(settings, field, None)
                        if cur is None:
                            setattr(settings, field, _clamped)
                        else:
                            setattr(settings, field, type(cur)(_clamped))
                        result["applied_scalars"] += 1
                    except Exception as e:
                        result["errors"].append(f"scalar {field}: {e}")
    except Exception as e:
        result["errors"].append(f"load kelly_config_override: {e}")

    for attr, state_key in (
        ("category_thresholds", "category_thresholds_override"),
        ("futures_category_thresholds", "futures_category_thresholds_override"),
    ):
        try:
            rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key=%s",
                (state_key,),
            )
            if not rows or rows[0].get("value") is None:
                continue
            raw = rows[0].get("value")
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                restored = deepcopy(getattr(settings, attr, {}) or {})
                raw_map = deepcopy(raw)
                if attr == "futures_category_thresholds" and "MIDCAP" in raw_map:
                    legacy_midcap = raw_map.pop("MIDCAP")
                    raw_map.setdefault("MIDCAP_ALT", legacy_midcap)
                for key, value in raw_map.items():
                    if isinstance(value, dict) and isinstance(restored.get(key), dict):
                        merged = dict(restored.get(key, {}))
                        merged.update(value)
                        restored[key] = merged
                    else:
                        restored[key] = deepcopy(value)
                setattr(settings, attr, restored)
                result["applied_thresholds"].append(attr)
        except Exception as e:
            result["errors"].append(f"{state_key}: {e}")

    if result["errors"]:
        logger.warning("Runtime override restore completed with warnings: %s", result["errors"])
    return result
