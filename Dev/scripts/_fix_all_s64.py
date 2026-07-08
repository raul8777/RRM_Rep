"""
S64 Audit Fix — Grupo 0: DB-only fixes (no rebuild required)
  G0-A: Delete zombie OPGUSDT (qty=0) from trades_open
  G0-B: Update all 12 regime_profiles with correct s62/s63 values (per mode)
  G0-C: Reset kelly_config_override with correct values
All changes are audited in trades_audit.
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')
from init_components import get_components

c = get_components()
db = c.db_client
s = c.settings

_now = datetime.now(timezone.utc).isoformat()

print("=" * 60)
print("S64 Audit Fix — Starting")
print("=" * 60)

# ─────────────────────────────────────────────────────────────
# G0-A: Delete zombie OPGUSDT (qty=0, no physical position)
# ─────────────────────────────────────────────────────────────
print("\n[G0-A] Cleaning zombie OPGUSDT...")
zombie_rows = db.execute_query(
    "SELECT id, quantity, price, metadata FROM trades_open WHERE symbol='OPGUSDT'"
)
deleted_ids = []
for row in zombie_rows:
    qty = float(row['quantity'] or 0)
    if qty == 0:
        db.execute_update("DELETE FROM trades_open WHERE id=%s", (row['id'],))
        deleted_ids.append(row['id'])
        print(f"  Deleted zombie id={row['id']} qty={qty} price={row['price']}")

if deleted_ids:
    db.execute_update(
        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
        "VALUES ('OPGUSDT', 'system', 'zombie_cleanup', %s, false)",
        (json.dumps({'reason': 'zombie_cleanup_qty_zero', 'deleted_ids': deleted_ids, 'source': 's64_audit_fix'}),)
    )
    print(f"  ✅ Deleted {len(deleted_ids)} zombie record(s), audit logged")
else:
    print("  ℹ️  No zombie OPGUSDT records found (qty>0 or already gone)")

# ─────────────────────────────────────────────────────────────
# G0-B: Update all 12 regime_profiles with correct s62/s63 values
# ─────────────────────────────────────────────────────────────
print("\n[G0-B] Updating 12 regime_profiles with s62/s63 correct values...")

# Base values common to ALL modes / profiles (s62/s63 Wolf Hunter)
_BASE_PARAMS = {
    'min_signal_strength':     0.45,
    'min_success_probability': 0.52,
    'kelly_base_usdt':         100.0,
    'kelly_max_usdt':          1000.0,
    'kelly_max_pct_liquid':    0.704,
    'kelly_max_pct_cap':       0.22,
    'kelly_dynamic_base_pct':  0.18,
    'kelly_conviction_vol2x':  1.8,
    'kelly_conviction_vol3x':  2.5,
    'kelly_conviction_max_pct': 0.15,
    'stop_loss_pct':            3.36,
    'slippage_pct':             0.00052,
    'futures_kelly_max_usdt':   1111.0,
    # SSO s62
    'sso_stagnant_min_hours':      3.0,
    'sso_decay_threshold_pct':     0.008,
    'sso_decay_min_hours':         2.5,
    'sso_slow_tier2_hours':        5.0,
    'sso_slow_tier3_hours':        7.0,
    'sso_slow_tier4_hours':        9.0,
    'sso_slow_velocity_threshold': 0.00106,
}

# vol_spike_min is MODE-SPECIFIC
_VOL_SPIKE_BY_MODE = {
    'RANGE_SCALP':    0.55,   # lateral market — bb_lower is primary gate
    'SPOT_BULL':      0.91,   # breakout needs volume confirmation
    'FUTURES_SHORT':  0.91,   # same as SPOT_BULL
    'PRESERVATION':   0.91,   # conservative
}

# Profile-specific multipliers (A=aggressive, B=balanced, C=conservative)
# Only kelly sizing differs by profile; entry thresholds are the same
_PROFILE_KELLY_MULT = {
    'A': {'kelly_dynamic_base_pct': 0.18, 'kelly_max_pct_cap': 0.22},
    'B': {'kelly_dynamic_base_pct': 0.16, 'kelly_max_pct_cap': 0.20},
    'C': {'kelly_dynamic_base_pct': 0.14, 'kelly_max_pct_cap': 0.18},
}

updated_profiles = []
for mode in ['RANGE_SCALP', 'SPOT_BULL', 'FUTURES_SHORT', 'PRESERVATION']:
    for prof in ['A', 'B', 'C']:
        key = f'regime_profile_{mode}_{prof}'
        rows = db.execute_query(
            'SELECT value FROM trading_runtime_state WHERE key=%s', (key,)
        )
        existing = {}
        if rows and rows[0].get('value'):
            raw = rows[0]['value']
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    pass
            if isinstance(raw, dict):
                existing = raw.get('params', raw)

        # Build corrected params: start from existing, overlay base, then mode + profile specifics
        new_params = dict(existing)
        new_params.update(_BASE_PARAMS)
        new_params['vol_spike_min'] = _VOL_SPIKE_BY_MODE[mode]
        new_params.update(_PROFILE_KELLY_MULT[prof])

        # Wrap in {params: ...} structure if that's what was stored
        if rows and rows[0].get('value'):
            raw_check = rows[0]['value']
            if isinstance(raw_check, str):
                try:
                    raw_check = json.loads(raw_check)
                except Exception:
                    pass
            if isinstance(raw_check, dict) and 'params' in raw_check:
                payload = json.dumps({**raw_check, 'params': new_params})
            else:
                payload = json.dumps(new_params)
        else:
            payload = json.dumps(new_params)

        db.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
            (key, payload)
        )
        updated_profiles.append(key)
        print(f"  ✅ {key}: vol={new_params['vol_spike_min']} str={new_params['min_signal_strength']} "
              f"kelly_base={new_params['kelly_base_usdt']} sl={new_params['stop_loss_pct']}")

db.execute_update(
    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
    "VALUES ('__config__', 'system', 'regime_profiles_updated', %s, false)",
    (json.dumps({
        'source': 's64_audit_fix',
        'profiles_updated': updated_profiles,
        'base_params': _BASE_PARAMS,
        'vol_spike_by_mode': _VOL_SPIKE_BY_MODE,
    }),)
)
print(f"\n  ✅ All {len(updated_profiles)} profiles updated and audit logged")

# ─────────────────────────────────────────────────────────────
# G0-C: Reset kelly_config_override with definitive s62/s63 values
# ─────────────────────────────────────────────────────────────
print("\n[G0-C] Resetting kelly_config_override with canonical s62/s63 values...")

rows = db.execute_query(
    "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
)
existing_override = {}
if rows and rows[0].get('value'):
    raw = rows[0]['value']
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            pass
    if isinstance(raw, dict):
        existing_override = raw

# Canonical s62/s63 values — these take precedence
_CANONICAL = {
    'vol_spike_min':              {'new': 0.55},   # RANGE_SCALP active — override per-mode when profile applies
    'min_signal_strength':        {'new': 0.45},
    'min_success_probability':    {'new': 0.52},
    'kelly_base_usdt':            {'new': 100.0},
    'kelly_max_usdt':             {'new': 1000.0},
    'kelly_dynamic_base_pct':     {'new': 0.18},
    'kelly_conviction_vol2x':     {'new': 1.8},
    'kelly_conviction_vol3x':     {'new': 2.5},
    'kelly_conviction_max_pct':   {'new': 0.15},
    'kelly_max_pct_liquid':       {'new': 0.704},
    'kelly_max_pct_cap':          {'new': 0.22},
    'stop_loss_pct':              {'new': 3.36},
    'futures_kelly_max_usdt':     {'new': 1111.0},
    'slippage_pct':               {'new': 0.00052},
    'wr_screener_consecutive_sl_block': {'new': 5},
    # SSO s62 — these were missing from kelly_config_override entirely
    'sso_stagnant_min_hours':     {'new': 3.0},
    'sso_stagnant_velocity_window_min': {'new': 60},
    'sso_decay_threshold_pct':    {'new': 0.008},
    'sso_decay_min_hours':        {'new': 2.5},
    'sso_slow_tier2_hours':       {'new': 5.0},
    'sso_slow_tier3_hours':       {'new': 7.0},
    'sso_slow_tier4_hours':       {'new': 9.0},
    'sso_slow_velocity_threshold':{'new': 0.00106},
    # BTC dominance (already correct but ensure)
    'btc_dominance_altcoin_threshold': {'new': 64.0},
    'btc_dominance_filter_enabled':    {'new': True},
}

# Merge: start from existing, then apply canonical values on top
existing_override.update(_CANONICAL)
new_payload = json.dumps(existing_override)

db.execute_update(
    "INSERT INTO trading_runtime_state (key, value, updated_at) "
    "VALUES ('kelly_config_override', %s, NOW()) "
    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
    (new_payload, )
)

db.execute_update(
    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
    "VALUES ('__config__', 'system', 'kelly_config_change', %s, false)",
    (json.dumps({'applied': _CANONICAL, 'source': 's64_audit_fix_canonical'}),)
)

# Apply to live settings object
applied_live = {}
failed_live = {}
for field, change in _CANONICAL.items():
    new_val = change['new']
    cur = getattr(s, field, None)
    try:
        if cur is not None:
            setattr(s, field, type(cur)(new_val))
        else:
            setattr(s, field, new_val)
        applied_live[field] = new_val
    except Exception as e:
        failed_live[field] = str(e)

print(f"  ✅ kelly_config_override updated ({len(_CANONICAL)} fields)")
print(f"  ✅ Live settings patched: {len(applied_live)} OK, {len(failed_live)} failed")
if failed_live:
    for f, err in failed_live.items():
        print(f"    FAIL {f}: {err}")

# ─────────────────────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("VERIFICATION")
print("=" * 60)

checks = [
    ('vol_spike_min', 0.55),
    ('min_signal_strength', 0.45),
    ('min_success_probability', 0.52),
    ('kelly_base_usdt', 100.0),
    ('kelly_max_usdt', 1000.0),
    ('stop_loss_pct', 3.36),
    ('kelly_max_pct_liquid', 0.704),
    ('slippage_pct', 0.00052),
    ('sso_stagnant_min_hours', 3.0),
    ('sso_decay_min_hours', 2.5),
    ('sso_decay_threshold_pct', 0.008),
    ('sso_slow_tier2_hours', 5.0),
    ('sso_slow_tier3_hours', 7.0),
]
all_ok = True
for field, expected in checks:
    live = getattr(s, field, 'MISSING')
    try:
        ok = abs(float(live) - float(expected)) < 0.001
    except Exception:
        ok = (live == expected)
    status = 'OK  ' if ok else 'FAIL'
    if not ok:
        all_ok = False
    print(f"  {status} {field}: live={live}  expected={expected}")

zombie_remaining = db.execute_query(
    "SELECT COUNT(*) as n FROM trades_open WHERE symbol='OPGUSDT' AND quantity=0"
)
zombie_count = zombie_remaining[0]['n'] if zombie_remaining else '?'
ok_zombie = zombie_count == 0
print(f"\n  {'OK  ' if ok_zombie else 'FAIL'} OPGUSDT zombies remaining: {zombie_count}")
if not ok_zombie:
    all_ok = False

# Verify at least one regime_profile has correct values
sample = db.execute_query(
    "SELECT value FROM trading_runtime_state WHERE key='regime_profile_RANGE_SCALP_A'"
)
if sample and sample[0].get('value'):
    sv = sample[0]['value']
    if isinstance(sv, str):
        try:
            sv = json.loads(sv)
        except Exception:
            pass
    params = sv.get('params', sv) if isinstance(sv, dict) else {}
    vs = params.get('vol_spike_min', 'MISSING')
    ss = params.get('min_signal_strength', 'MISSING')
    ok_prof = abs(float(vs) - 0.55) < 0.001 and abs(float(ss) - 0.45) < 0.001
    print(f"  {'OK  ' if ok_prof else 'FAIL'} regime_profile_RANGE_SCALP_A: vol={vs} str={ss}")
    if not ok_prof:
        all_ok = False

print()
print("=" * 60)
print(f"RESULT: {'✅ ALL CHECKS PASSED' if all_ok else '❌ SOME CHECKS FAILED — see above'}")
print("=" * 60)
print("\nNEXT: Rebuild container to apply G1 (config.py) + G2 (_apply_regime_params MERGE fix)")
