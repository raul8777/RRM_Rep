import json
from config import get_settings
from config_helpers import dump_active_config, _PARAM_DEFAULTS

settings = get_settings()
actual = dump_active_config(settings)

# Environment/safety overrides and symbol lists are intentionally different in runtime.
_EXCLUDED = {
    'dry_run', 'allow_real_trading', 'analysis_symbols', 'futures_symbols',
    'futures_largecap_symbols', 'trading_excluded_symbols', 'narrative_tokens',
    'token_categories', 'category_thresholds',
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
