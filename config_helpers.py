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

from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)


# Default values MUST match config.py defaults. Only one place to change them.
_PARAM_DEFAULTS: Dict[str, Any] = {
    # SPOT Kelly
    'kelly_enabled': True,
    'kelly_base_usdt': 73.0,
    'kelly_max_usdt': 747.0,
    'kelly_max_pct_liquid': 0.704,
    'kelly_max_pct_cap': 0.25,
    'kelly_dynamic_base_pct': 0.12,
    'max_per_trade_usdt': 500.0,

    # FUTURES Kelly
    'futures_kelly_base_usdt': 73.0,
    'futures_kelly_max_usdt': 1111.0,
    'futures_kelly_max_pct_liquid': 0.704,
    'futures_kelly_cap_pct': 0.20,

    # FUTURES trading
    'futures_enabled': True,
    'futures_leverage': 10,
    'futures_max_shorts': 12,
    'futures_max_largecap_shorts': 5,
    'futures_max_new_per_cycle': 5,
    'futures_usdt_per_trade': 100.0,
    'futures_sl_pct': 1.42,
    'futures_tp_pct': 2.81,
    'futures_long_sl_pct': 2.5,
    'futures_long_tp_pct': 5.0,
    'futures_symbols': [
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
        'NEARUSDT', 'SUIUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'TAOUSDT',
        'UNIUSDT', 'DOTUSDT', '1000PEPEUSDT', 'BCHUSDT', 'AAVEUSDT', 'FILUSDT',
        'INJUSDT',
    ],
    'futures_largecap_symbols': ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'],

    # SPOT
    'stop_loss_pct': 3.36,
    'take_profit_pct': 4.14,
    'trailing_stop_enabled': True,
    'trailing_stop_activation_pct': 2.5,
    'trailing_stop_distance_pct': 1.2,

    # Signal filters
    'min_signal_strength': 0.389,
    'min_success_probability': 0.499,
    'futures_min_prob': 0.499,
    'futures_vol_spike_min': 0.91,
    'vol_spike_min': 0.91,

    # Exposure / regime
    'max_total_exposure_pct': 0.75,
    'max_positions_spot_bull': 20,
    'max_positions_range_scalp': 10,
    'max_positions_preservation': 5,
    'max_positions_futures_short': 12,
    'max_positions': 12,

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

    # Kelly conviction (SPOT)
    'kelly_conviction_enabled': True,
    'kelly_conviction_vol2x': 1.5,
    'kelly_conviction_vol3x': 2.0,
    'kelly_conviction_max_pct': 0.10,

    # ATR SL (SPOT)
    'atr_sl_enabled': True,
    'atr_sl_multiplier': 1.5,
    'atr_sl_max_pct': 0.08,
    'atr_sl_min_pct': 0.015,

    # Trailing stop (SPOT)
    'trailing_stop_enabled': True,
    'trailing_stop_activation_pct': 2.5,
    'trailing_stop_distance_pct': 1.2,

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
    'spot_bounce_rescue_min_hours': 8.0,
    'spot_bounce_rescue_target_pnl': -0.30,

    # VolSpike override
    'volspike_override_enabled': True,
    'volspike_override_narrative_only': True,
    'volspike_override_min_spike': 1.5,
    'volspike_override_max_rsi': 65.0,
    'volspike_override_trend_required': True,
    'volspike_override_min_strength': 0.65,
    'volspike_override_max_positions': 2,
    'volspike_override_daily_limit': 3,

    # Screener
    'screener_enabled': True,
    'screener_min_vol_usdt': 10_000_000,
    'screener_futures_min_vol': 100_000_000,
    'screener_max_pairs': 50,
    'screener_cache_minutes': 15,
    'screener_top_n_enabled': True,
    'screener_top_n_count': 100,
    'screener_quality_min_vol_usdt': 10_000_000,
    'screener_quality_max_volatility': 0.50,
    'screener_quality_min_price_usdt': 0.0001,
    'liquidity_filter_enabled': True,
    'min_liquidity_vol_usdt': 3_000_000.0,
    'max_slippage_tp_ratio': 0.25,

    # Category / position limits
    'category_max_positions_enabled': True,
    'category_max_positions': {
        "LARGE_CAP": 3, "MIDCAP_ALT": 8, "NARRATIVE": 6, "MEME": 2,
    },
    'token_categories': {},
    'category_thresholds': {},
    'max_positions_per_symbol': 1,
    'overflow_min_strength': 0.80,
    'overflow_min_prob': 0.65,
    'overflow_max_extra': 5,

    # Min hold
    'min_hold_minutes': 240,
    'min_hold_minutes_range_scalp': 30,
    'min_hold_minutes_spot_bull': 120,
    'min_hold_for_prediction_exit': 90,

    # Rebalance
    'max_rebalance_per_cycle': 1,
    'rebalance_rsi_overbought': 60.0,
    'rebalance_skip_if_bullish_signal': True,
    'replace_ev_min_ratio': 1.1,
    'replace_strength_min_delta': 0.05,
    'replace_only_losing': True,

    # BTC dominance / regime filters
    'btc_dominance_filter_enabled': True,
    'btc_dominance_altcoin_threshold': 64.0,
    'market_regime_enabled': True,
    'preservation_narrative_only': True,
    'preservation_max_rsi': 65.0,
    'range_scalp_max_rsi': 40.0,
    'range_scalp_sl_pct': 1.2,
    'range_scalp_tp_pct': 1.5,
    'preservation_sl_pct': 1.5,
    'preservation_tp_pct': 3.0,
    'spot_bull_pnl_boost_enabled': True,
    'spot_bull_btc_4h_up_required': True,
    'spot_bull_pnl_boost_large_cap': 0.0,
    'spot_bull_pnl_boost_midcap': -0.10,
    'max_total_exposure_spot_bull_fear_fng_threshold': 40,
    'max_total_exposure_spot_bull_fear': 0.80,

    # Cycle lengths
    'cycle_spot_bull': 10,
    'cycle_range_scalp': 5,
    'cycle_preservation': 45,
    'cycle_futures_short': 4,
    'trade_cycle_minutes': 5,

    # Prediction-based add / close
    'prediction_exit_enabled': True,
    'allow_prediction_based_add': True,
    'prediction_add_min_strength': 0.70,
    'prediction_add_min_ev_ratio': 1.5,
    'prediction_add_kelly_factor': 0.6,

    # Narrative tokens
    'narrative_tokens': [],
    'bear_market_volspike_min': 2.0,

    # SSO
    'sso_enabled': True,
    'sso_target_daily_usd': 40.0,

    # Funding rate filter
    'funding_rate_spot_filter_enabled': True,
    'funding_rate_spot_max': 0.0008,

    # WR screener
    'wr_screener_enabled': True,
    'wr_screener_consecutive_sl_block': 3,

    # Market Regime
    'current_regime_mode': 'SPOT_BULL',

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
    'futures_min_inject_strength': 0.40,
    'futures_sl_cooldown_hours': 4.0,
    'futures_mass_sl_threshold': 3,
    'futures_mass_sl_cooldown_hours': 4.0,
    'futures_max_escalamientos': 2,
    'futures_protect_profit_usdt': -5.0,
    'min_cycles_before_close_shorts': 5,
    'use_unified_portfolio_manager': True,

    # Futures overflow
    'futures_overflow_min_strength': 0.55,
    'futures_overflow_min_drop_24h': -3.0,
    'futures_overflow_max_extra': 3,
    'futures_overflow_min_vol_spike': 2.0,

    # Futures trailing / smart exits
    'futures_trailing_enabled': True,
    'futures_trailing_activation_pct': 2.0,
    'futures_trailing_distance_pct': 1.5,
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
    'futures_bounce_rescue_min_hours': 4.0,
    'futures_bounce_rescue_target_pnl': -0.50,
    'futures_momentum_reversal_enabled': True,
    'futures_momentum_reversal_min_peak': 2.0,
    'futures_momentum_reversal_drawback': 0.8,
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

    # Trading symbol lists
    'analysis_symbols': [
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
        'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT',
    ],
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

    # Check for known drift against hardcodes (legacy safety)
    if hasattr(settings, 'futures_leverage') and settings.futures_leverage != lev:
        # unreachable but documents intent
        pass

    return {'ok': len([i for i in issues if i['severity'] == 'error']) == 0, 'issues': issues}


def dump_active_config(settings) -> Dict[str, Any]:
    """Return a snapshot of all canonical parameters for persistence."""
    return {name: get_param(settings, name) for name in _PARAM_DEFAULTS}
