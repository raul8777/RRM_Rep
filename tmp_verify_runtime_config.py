import json
from config import get_settings
from config_helpers import dump_active_config, _PARAM_DEFAULTS

settings = get_settings()
actual = dump_active_config(settings)

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

print("Runtime config matches baseline run_0812 defaults. OK")
