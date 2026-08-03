import os, re, json, sys, ast
from collections import defaultdict
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
sys.path.insert(0, ROOT)
from config_helpers import _PARAM_DEFAULTS

# Canonical defaults from config_helpers.py (single source of truth)
CANON = dict(_PARAM_DEFAULTS)
CRITICAL_PARAMS = set(CANON.keys())

# Module globals for evaluating non-literal defaults (e.g. constants / .get calls)
import config_helpers as _cfg_helpers_mod
_DEFAULTS_NAMESPACE = {k: getattr(_cfg_helpers_mod, k) for k in dir(_cfg_helpers_mod) if not k.startswith('__')}

# Params excluded from CRITICAL classification because they are legitimately
# overridden by docker-compose env vars, are dynamic runtime state, or are
# safety defaults that intentionally differ from canonical (e.g. dry_run=False
# in production code is intentionally conservative).
_AUDIT_EXCLUDED = {
    # Dynamic runtime state — not a code bug
    'current_regime_mode',
    # Runtime label — '' and None are intentional sentinels (populated at startup from DB)
    'current_regime_profile_label',
    # Regime cycle — controlled by kelly_config_override in DB, not code default
    'cycle_range_scalp',
    # Safety/env flags — production code using False is intentionally conservative
    'dry_run', 'allow_real_trading',
    # Docker-compose operational overrides — not defaults in the code-bug sense
    'max_per_trade_usdt', 'max_positions', 'max_positions_per_symbol',
    'max_total_exposure_pct', 'kelly_dynamic_base_pct',
    'trailing_stop_activation_pct', 'trailing_stop_distance_pct',
    'futures_max_new_per_cycle', 'overflow_max_extra',
    'min_hold_minutes', 'max_rebalance_per_cycle',
    'btc_dominance_altcoin_threshold', 'use_unified_portfolio_manager',
    'futures_overflow_min_strength',
    # Symbol/category lists — deployment-specific, not canonical defaults
    'analysis_symbols', 'futures_symbols', 'futures_largecap_symbols',
    'trading_excluded_symbols', 'narrative_tokens',
    'token_categories', 'category_thresholds',
    # PRS params with deliberate operational tuning
    'profit_realization_spot_min_profit_pct', 'profit_realization_spot_partial_minutes',
    'profit_realization_spot_full_minutes', 'profit_realization_spot_drawback_pct',
    'profit_realization_spot_partial_close_pct', 'profit_realization_spot_max_extensions',
    'profit_realization_futures_min_profit_pct', 'profit_realization_futures_partial_minutes',
    'profit_realization_futures_full_minutes', 'profit_realization_futures_drawback_pct',
    'profit_realization_futures_partial_close_pct', 'profit_realization_futures_max_extensions',
    # Futures ATR trail — new params with None sentinels intentional (feature-flag pattern)
    'trailing_stop_activation_pct', 'trailing_stop_distance_pct',
    'futures_breakeven_activation_pct', 'futures_trailing_activation_pct',
    'futures_trailing_distance_pct', 'futures_trail_floor_pct', 'futures_trail_ceiling_pct',
    'futures_trail_atr_multiplier_low', 'futures_trail_atr_multiplier_mid',
    'futures_trail_atr_multiplier_high', 'futures_trail_atr_multiplier_deep',
    'sso_target_daily_usd',
    # Regime-specific position limits — deployment-tuned
    'max_positions_spot_bull', 'max_positions_range_scalp',
    'max_positions_preservation', 'max_positions_futures_short',
    'min_hold_minutes_spot_bull', 'min_hold_minutes_range_scalp',
    'futures_long_sl_pct', 'futures_long_tp_pct', 'max_rebalance_per_cycle',
    'min_cycles_before_close_shorts',
    # Overflow / portfolio params tuned per deployment
    'futures_overflow_min_strength',
    # s72: new per-regime fields — governed by DB profiles, not code defaults
    'range_scalp_max_rsi', 'range_scalp_max_bb_position',
    'entry_quality_vol_spike_min_range_scalp',
    'min_hold_for_prediction_exit_futures_short',
    'sso_target_contribution_pct_range_scalp',  # s73 C2: DB-governed per profile
    # Aggressive entry calibration — intentionally tuned per deployment
    'min_signal_strength', 'min_success_probability',
    'futures_min_prob', 'futures_entry_max_rsi_4h',
    'staged_entry_regime_config',
    # Legitimate fallback sentinels / not canonical defaults
    'futures_category_thresholds', 'rebalance_excluded_symbols', 'signal_cache_ttl_seconds',
    # Env-specific DB credentials — fallback values differ from canonical empty/localhost
    'db_user', 'db_host',
}

import ast

def parse_default(val):
    if val is None:
        return None
    val = val.strip().rstrip(',')
    try:
        if val.lower() == 'true':
            return True
        if val.lower() == 'false':
            return False
        if val.startswith('"') and val.endswith('"'):
            return val[1:-1]
        if val.startswith("'") and val.endswith("'"):
            return val[1:-1]
        if val.startswith('[') or val.startswith('{'):
            return ast.literal_eval(val)
        if '.' in val:
            return float(val)
        return int(val)
    except Exception:
        pass
    # Try evaluating module-level constants / simple expressions (e.g. _PARAM_DEFAULTS.get(...))
    try:
        # only allow safe attribute/subscript/getattr calls
        if re.match(r'^[A-Za-z_][A-Za-z0-9_\._]*(?:\(.*\))?(?:\[.*\])?$', val):
            return eval(val, {"__builtins__": {}}, _DEFAULTS_NAMESPACE)
    except Exception:
        pass
    return val

def find_getattr_defaults(root):
    results = []
    for path, dirs, files in os.walk(root):
        for f in files:
            if not f.endswith('.py'):
                continue
            if f.startswith('tmp_'):
                continue
            fp = os.path.join(path, f)
            try:
                with open(fp) as fh:
                    src = fh.read()
            except Exception:
                continue
            for m in re.finditer(r'getattr\(([^,]+),\s*[\'"]([^\'"]+)[\'"](?:,\s*([^)]+))?\)', src):
                obj, attr, default = m.group(1), m.group(2), m.group(3)
                line = src.count('\n', 0, m.start()) + 1
                parsed = parse_default(default.strip() if default else None)
                severity = 'info'
                if attr in CRITICAL_PARAMS and attr not in _AUDIT_EXCLUDED:
                    expected = CANON.get(attr)
                    if parsed is not None and parsed != expected:
                        severity = 'critical'
                results.append((os.path.relpath(fp, root).replace(os.sep, '/'), attr, default.strip() if default else '<no default>', line, severity))
    return results

def find_hardcodes(root):
    results = []
    magic = re.compile(r'\b(\d+(?:\.\d+)?)\b')
    for path, dirs, files in os.walk(root):
        for f in files:
            if not f.endswith('.py'):
                continue
            fp = os.path.join(path, f)
            try:
                with open(fp) as fh:
                    src = fh.read()
            except Exception:
                continue
            for m in re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$', src, re.M):
                name, val = m.group(1), m.group(2).strip()
                if name in ('FAPI_BASE', 'MAX_SHORTS', 'LEVERAGE_MAX', 'FUNDING_LIMIT', '_PRICE_CACHE_TTL'):
                    continue
                results.append((os.path.relpath(fp, root).replace(os.sep, '/'), name, val, src.count('\n', 0, m.start())+1))
    return results

getattr_res = find_getattr_defaults(ROOT)
by_file = defaultdict(list)
for r in getattr_res:
    by_file[r[0]].append(r)

with open(os.path.join(ROOT, 'audit_getattr.txt'), 'w') as out:
    out.write('=== getattr(settings, ...) audit ===\n')
    out.write(f'Total: {len(getattr_res)} occurrences\n')
    critical = [r for r in getattr_res if r[4] == 'critical']
    out.write(f'Critical mismatches: {len(critical)}\n\n')
    for r in critical:
        out.write(f'CRITICAL {r[0]}:{r[3]}  {r[1]:40} default={r[2]:20} expected={CANON.get(r[1])}\n')
    out.write('\n')
    for f in sorted(by_file):
        out.write(f'\n=== {f} ===\n')
        for r in by_file[f]:
            out.write(f'  {r[4]:8} {r[1]:40} default={r[2]:20} line={r[3]}\n')

print('audit_getattr.txt written')
