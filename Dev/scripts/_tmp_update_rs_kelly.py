"""
s76: Update RANGE_SCALP profiles A/B/C with uniform aggressive Kelly.
Reads current profile, merges only Kelly fields, writes back.
Only affects RANGE_SCALP — no other regime touched.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db_client import get_db_client

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    from config import Settings
    s = Settings()
    db_url = s.db_url
db = get_db_client(db_url)

# Uniform aggressive Kelly for all RANGE_SCALP profiles
KELLY_UNIFORM = {
    "kelly_max_usdt": 2500.0,
    "kelly_dynamic_base_pct": 0.26,
    "kelly_base_usdt": 200.0,
    "kelly_max_pct_liquid": 0.90,
    "kelly_max_pct_cap": 0.35,
    "kelly_conviction_max_pct": 0.30,
    "max_positions_range_scalp": 5,
}

PROFILES = ["A", "B", "C"]

print("=" * 60)
print("s76: Aggressive Kelly for RANGE_SCALP profiles (uniform)")
print("=" * 60)

for label in PROFILES:
    key = f"regime_profile_RANGE_SCALP_{label}"
    rows = db.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (key,)
    )

    if not rows or rows[0].get("value") is None:
        print(f"\n[{label}] No existing profile found — creating new one")
        existing_params = {}
        existing_meta = {}
    else:
        raw = rows[0]["value"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        existing_params = raw.get("params", {})
        existing_meta = raw.get("meta", {})
        print(f"\n[{label}] Existing profile found with {len(existing_params)} params")

    # Show what's changing
    for k, v in KELLY_UNIFORM.items():
        old = existing_params.get(k, "NOT SET")
        if old != v:
            print(f"  {k}: {old} → {v}")
        else:
            print(f"  {k}: {v} (unchanged)")

    # Merge: keep all existing params, override only Kelly fields
    merged_params = {**existing_params, **KELLY_UNIFORM}

    payload = {
        "regime": "RANGE_SCALP",
        "profile": label,
        "params": merged_params,
        "meta": {
            **existing_meta,
            "updated_by": "s76_aggressive_kelly",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "change_reason": "s76: uniform aggressive Kelly for RANGE_SCALP (sweep-validated, CDR 50%→85% target)",
        },
    }

    payload_json = json.dumps(payload)
    db.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (key, payload_json, payload_json),
    )

    # Audit trail
    db.execute_update(
        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
        "VALUES ('__config__', 'system', 'regime_profile_saved', %s, false)",
        (json.dumps({
            "regime": "RANGE_SCALP",
            "profile": label,
            "accepted": KELLY_UNIFORM,
            "source": "s76_aggressive_kelly",
        }),),
    )
    print(f"  ✓ Profile {label} saved ({len(merged_params)} total params)")

# Verify
print("\n" + "=" * 60)
print("VERIFICATION")
print("=" * 60)
for label in PROFILES:
    key = f"regime_profile_RANGE_SCALP_{label}"
    rows = db.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (key,)
    )
    if rows:
        raw = rows[0]["value"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        params = raw.get("params", {})
        ok = all(params.get(k) == v for k, v in KELLY_UNIFORM.items())
        print(f"  [{label}] kelly_max_usdt={params.get('kelly_max_usdt')} "
              f"kelly_dynamic_base_pct={params.get('kelly_dynamic_base_pct')} "
              f"max_positions_range_scalp={params.get('max_positions_range_scalp')} "
              f"→ {'OK' if ok else 'MISMATCH!'}")

print("\nDone.")
