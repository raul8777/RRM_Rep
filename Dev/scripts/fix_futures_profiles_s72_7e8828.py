"""
Dev/scripts/fix_futures_profiles_s72_7e8828.py
s72: Create FUTURES_SHORT regime profiles A/B/C in trading_runtime_state DB.
Also updates RANGE_SCALP profiles with new s72 fields (range_scalp_max_rsi=60, etc.)

Run inside docker:
  docker compose exec app python Dev/scripts/fix_futures_profiles_s72_7e8828.py
"""
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, '/app')

try:
    from init_components import get_components
except ImportError:
    print("ERROR: run inside container — docker compose exec app python Dev/scripts/fix_futures_profiles_s72_7e8828.py")
    sys.exit(1)

_c = get_components()
db = _c.db_client
now = datetime.now(timezone.utc).isoformat()

# ── FUTURES_SHORT profiles ────────────────────────────────────────────────────
FUTURES_SHORT_PROFILES = {
    'A': {
        'min_signal_strength': 0.43,
        'min_success_probability': 0.499,
        'vol_spike_min': 0.91,
        'futures_vol_spike_min': 0.91,
        'futures_min_prob': 0.499,
        'sso_stagnant_min_hours_futures_short': 2.17,   # 2.38 × 0.91 (Profile A aggressive)
        'sso_slow_min_hours_futures_short': 1.27,       # 1.40 × 0.91
        'sso_decay_min_hours_futures_short': 1.82,      # 2.00 × 0.91
        'min_hold_for_prediction_exit_futures_short': 25,  # more aggressive PredExit
    },
    'B': {
        'min_signal_strength': 0.45,
        'min_success_probability': 0.499,
        'vol_spike_min': 0.91,
        'futures_vol_spike_min': 0.91,
        'futures_min_prob': 0.499,
        'sso_stagnant_min_hours_futures_short': 2.38,
        'sso_slow_min_hours_futures_short': 1.40,
        'sso_decay_min_hours_futures_short': 2.00,
        'min_hold_for_prediction_exit_futures_short': 30,
    },
    'C': {
        'min_signal_strength': 0.47,
        'min_success_probability': 0.52,
        'vol_spike_min': 0.91,
        'futures_vol_spike_min': 0.95,
        'futures_min_prob': 0.52,
        'sso_stagnant_min_hours_futures_short': 2.57,   # 2.38 × 1.08 (Profile C defensive)
        'sso_slow_min_hours_futures_short': 1.51,       # 1.40 × 1.08
        'sso_decay_min_hours_futures_short': 2.16,      # 2.00 × 1.08
        'min_hold_for_prediction_exit_futures_short': 35,
    },
}

# ── RANGE_SCALP profile updates — add new s72 fields ─────────────────────────
RANGE_SCALP_UPDATES = {
    'A': {
        'range_scalp_max_rsi': 60.0,
        'range_scalp_max_bb_position': 0.40,
        'entry_quality_vol_spike_min_range_scalp': 0.30,   # Profile A: more aggressive
    },
    'B': {
        'range_scalp_max_rsi': 60.0,
        'range_scalp_max_bb_position': 0.40,
        'entry_quality_vol_spike_min_range_scalp': 0.35,   # Profile B: neutral
    },
    'C': {
        'range_scalp_max_rsi': 58.0,                       # Profile C: slightly more conservative
        'range_scalp_max_bb_position': 0.35,               # Profile C: tighter zone
        'entry_quality_vol_spike_min_range_scalp': 0.40,   # Profile C: higher vol requirement
    },
}

errors = []

# ── Create FUTURES_SHORT profiles ────────────────────────────────────────────
for label, params in FUTURES_SHORT_PROFILES.items():
    key = f'regime_profile_FUTURES_SHORT_{label}'
    payload = json.dumps({'params': params, '_updated': now, '_session': 's72'})
    try:
        existing = db.execute_query(
            "SELECT key FROM trading_runtime_state WHERE key=%s", (key,)
        )
        if existing:
            db.execute_update(
                "UPDATE trading_runtime_state SET value=%s WHERE key=%s",
                (payload, key)
            )
            print(f"  UPDATED {key}")
        else:
            db.execute_update(
                "INSERT INTO trading_runtime_state (key, value) VALUES (%s, %s)",
                (key, payload)
            )
            print(f"  CREATED {key}")
    except Exception as e:
        errors.append(f"{key}: {e}")
        print(f"  ERROR {key}: {e}")

# ── Set active profile for FUTURES_SHORT (Profile B = balanced default) ───────
try:
    fs_active_key = 'regime_profile_active_FUTURES_SHORT'
    fs_active_val = json.dumps({'profile': 'B', '_updated': now, '_session': 's72'})
    existing = db.execute_query(
        "SELECT key FROM trading_runtime_state WHERE key=%s", (fs_active_key,)
    )
    if existing:
        db.execute_update(
            "UPDATE trading_runtime_state SET value=%s WHERE key=%s",
            (fs_active_val, fs_active_key)
        )
    else:
        db.execute_update(
            "INSERT INTO trading_runtime_state (key, value) VALUES (%s, %s)",
            (fs_active_key, fs_active_val)
        )
    print(f"  ACTIVE profile FUTURES_SHORT = B")
except Exception as e:
    errors.append(f"active_FUTURES_SHORT: {e}")
    print(f"  ERROR active_FUTURES_SHORT: {e}")

# ── Update RANGE_SCALP profiles with new s72 fields ──────────────────────────
for label, updates in RANGE_SCALP_UPDATES.items():
    key = f'regime_profile_RANGE_SCALP_{label}'
    try:
        rows = db.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s", (key,)
        )
        if not rows:
            print(f"  SKIP {key} (not found in DB)")
            continue
        raw = rows[0]['value']
        if isinstance(raw, str):
            raw = json.loads(raw)
        params = raw.get('params', raw) if isinstance(raw, dict) else {}
        params.update(updates)
        params['_updated'] = now
        params['_session'] = 's72'
        payload = json.dumps({'params': params})
        db.execute_update(
            "UPDATE trading_runtime_state SET value=%s WHERE key=%s",
            (payload, key)
        )
        print(f"  UPDATED RANGE_SCALP_{label}: range_scalp_max_rsi={updates['range_scalp_max_rsi']}")
    except Exception as e:
        errors.append(f"{key}: {e}")
        print(f"  ERROR {key}: {e}")

# ── Audit record ──────────────────────────────────────────────────────────────
audit_key = 's72_profile_fix_audit'
audit_val = json.dumps({
    'session': 's72',
    'ts': now,
    'created': [f'regime_profile_FUTURES_SHORT_{l}' for l in FUTURES_SHORT_PROFILES],
    'updated_range_scalp': [f'regime_profile_RANGE_SCALP_{l}' for l in RANGE_SCALP_UPDATES],
    'errors': errors,
})
try:
    db.execute_update(
        "INSERT INTO trading_runtime_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (audit_key, audit_val)
    )
    print(f"\n  Audit record written: {audit_key}")
except Exception as e:
    print(f"  Audit record ERROR: {e}")

print(f"\nDone. Errors: {len(errors)}")
if errors:
    for err in errors:
        print(f"  - {err}")
