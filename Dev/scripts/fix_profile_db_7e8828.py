"""
fix_profile_db_7e8828.py — s71 exhaustive audit DB fix

Fixes applied:
  BUG-1: Remove global SSO keys from all 12 regime profiles
         (sso_stagnant_min_hours, sso_decay_min_hours, sso_slow_velocity_threshold)
         These globals MUST NEVER be modified by profile — per-regime fields govern SSO timing.
  BUG-3: Remove stop_loss_pct and take_profit_pct from all 12 profiles
         Category thresholds and regime-specific fields handle SL/TP, not profile globals.
  BUG-5: Adjust min_success_probability and min_signal_strength for RANGE_SCALP profiles only
         Break-even WR = 33.3% — current 0.52 threshold blocks EV-positive setups.

Run inside container:
    docker compose exec app python Dev/scripts/fix_profile_db_7e8828.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')

try:
    from init_components import get_components
except ImportError:
    print("ERROR: run inside container — docker compose exec app python Dev/scripts/fix_profile_db_7e8828.py")
    sys.exit(1)


# Keys to remove from ALL 12 profiles (BUG-1 + BUG-3)
_KEYS_TO_REMOVE = [
    # BUG-1: Global SSO fields — per-regime fields govern timing (s70 design contract)
    "sso_stagnant_min_hours",
    "sso_decay_min_hours",
    "sso_slow_velocity_threshold",
    # BUG-3: SL/TP global overrides — category_thresholds and range_scalp_sl_pct govern these
    "stop_loss_pct",
    "take_profit_pct",
]

# BUG-5: RANGE_SCALP probability gates — break-even WR=33.3%, current 52% is too strict
# EV-positive setups (WR~40%) get rejected; targets aligned to lateral market statistics
_RANGE_SCALP_PROB_OVERRIDES = {
    "A": {"min_success_probability": 0.44, "min_signal_strength": 0.42},
    "B": {"min_success_probability": 0.46, "min_signal_strength": 0.43},
    "C": {"min_success_probability": 0.48, "min_signal_strength": 0.44},
}

_REGIMES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]
_PROFILES = ["A", "B", "C"]


def main() -> None:
    c = get_components()
    db = c.db_client
    now_iso = datetime.now(timezone.utc).isoformat()
    total_fixed = 0

    print(f"\n=== fix_profile_db_7e8828.py — started {now_iso} ===\n")

    for regime in _REGIMES:
        for profile in _PROFILES:
            key = f"regime_profile_{regime}_{profile}"
            rows = db.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key = %s", (key,)
            )
            if not rows:
                print(f"  {key}: NOT FOUND — skip")
                continue

            raw = rows[0]["value"]
            if isinstance(raw, str):
                raw = json.loads(raw)

            # Locate params dict — profiles use either top-level or nested under 'params'
            if "params" in raw and isinstance(raw["params"], dict):
                params = raw["params"]
            else:
                params = raw

            changed_keys = []

            # BUG-1 + BUG-3: remove forbidden global keys
            for k in _KEYS_TO_REMOVE:
                if k in params:
                    old_val = params.pop(k)
                    changed_keys.append(f"  REMOVED {k}={old_val}")

            # BUG-5: RANGE_SCALP-only probability gate adjustment
            if regime == "RANGE_SCALP" and profile in _RANGE_SCALP_PROB_OVERRIDES:
                overrides = _RANGE_SCALP_PROB_OVERRIDES[profile]
                for field, new_val in overrides.items():
                    old_val = params.get(field, "MISSING")
                    params[field] = new_val
                    changed_keys.append(f"  UPDATED {field}: {old_val} → {new_val}")

            if not changed_keys:
                print(f"  {key}: no changes needed")
                continue

            # Persist fix with audit trail
            if "meta" in raw and isinstance(raw["meta"], dict):
                raw["meta"]["s71_fix_applied_at"] = now_iso
                raw["meta"]["s71_fix_script"] = "fix_profile_db_7e8828.py"
            else:
                raw["_s71_fix_applied_at"] = now_iso

            db.execute_update(
                "UPDATE trading_runtime_state SET value = %s, updated_at = NOW() WHERE key = %s",
                (json.dumps(raw), key),
            )
            total_fixed += 1
            print(f"  {key}: FIXED ({len(changed_keys)} changes)")
            for msg in changed_keys:
                print(msg)

    # Write audit record to DB for traceability
    audit_record = {
        "script": "fix_profile_db_7e8828.py",
        "applied_at": now_iso,
        "bugs_fixed": ["BUG-1", "BUG-3", "BUG-5"],
        "profiles_fixed": total_fixed,
        "keys_removed_all_profiles": _KEYS_TO_REMOVE,
        "range_scalp_prob_overrides": _RANGE_SCALP_PROB_OVERRIDES,
    }
    audit_json = json.dumps(audit_record)
    db.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()",
        ("s71_profile_db_fix_audit", audit_json, audit_json),
    )

    print(f"\n=== DONE: {total_fixed} profiles fixed ===")
    print(f"  Audit record written to trading_runtime_state['s71_profile_db_fix_audit']")
    print(f"\nNext step: docker compose up -d --build  (to activate code changes)")


if __name__ == "__main__":
    main()
