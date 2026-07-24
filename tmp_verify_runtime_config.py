import json
from config import Settings
from config_helpers import _PARAM_DEFAULTS

# Local governance parity should compare the model-declared defaults against the
# canonical config map, not env-influenced runtime values. Live runtime drift is
# covered separately by /config/verify and drift-check endpoints.
actual = {
    name: field.default
    for name, field in Settings.model_fields.items()
}

# Environment/safety overrides, symbol lists, and docker-compose operational overrides
# are intentionally different from canonical defaults at runtime.
_EXCLUDED = {
    # Env/safety overrides
    'dry_run', 'allow_real_trading',
    # Symbol/category lists (environment-specific)
    'analysis_symbols', 'futures_symbols', 'futures_largecap_symbols',
    'trading_excluded_symbols', 'narrative_tokens',
    'token_categories', 'category_thresholds',
    # Docker-compose operational overrides (tuned per deployment, not canonical defaults)
    'max_per_trade_usdt', 'max_positions', 'max_positions_per_symbol',
    'max_total_exposure_pct', 'kelly_dynamic_base_pct',
    'trailing_stop_activation_pct', 'trailing_stop_distance_pct',
    'futures_max_new_per_cycle', 'overflow_max_extra',
    'min_hold_minutes', 'max_rebalance_per_cycle',
    'btc_dominance_altcoin_threshold', 'use_unified_portfolio_manager',
    'futures_overflow_min_strength',
    # Aggressive entry calibration — intentionally tuned per deployment
    'min_signal_strength', 'min_success_probability',
    'futures_min_prob', 'futures_entry_max_rsi_4h',
    'staged_entry_regime_config',
}

mismatches = {}
for k, expected in _PARAM_DEFAULTS.items():
    if k in _EXCLUDED:
        continue
    val = actual.get(k)
    if val != expected:
        mismatches[k] = {"expected": expected, "actual": val}

if mismatches:
    print("MISMATCHES:")
    print(json.dumps(mismatches, indent=2, default=str))
    raise SystemExit(1)

print("Model defaults match canonical baseline. OK")
