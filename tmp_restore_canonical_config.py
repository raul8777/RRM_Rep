import psycopg2, os, json
from datetime import datetime, timezone

url = os.environ.get('DB_URL', 'postgresql://crypto_agent:crypto_agent_password@postgres:5432/crypto_agent')
conn = psycopg2.connect(url)
cur = conn.cursor()

# Keys to restore to canonical defaults (config_helpers.py) under aggressive entry calibration
RESTORE = {
    "min_signal_strength": 0.40,
    "min_success_probability": 0.50,
    "stop_loss_pct": 3.36,
    "take_profit_pct": 4.14,
    "kelly_base_usdt": 150.0,
    "kelly_max_usdt": 2500.0,
    "kelly_max_pct_liquid": 0.90,
    "kelly_max_pct_cap": 0.30,
    "kelly_dynamic_base_pct": 0.22,
    "kelly_conviction_max_pct": 0.22,
    "futures_sl_pct": 2.0,
    "futures_tp_pct": 4.0,
    "futures_kelly_base_usdt": 200.0,
    "futures_kelly_max_usdt": 2500.0,
    "futures_kelly_max_pct_liquid": 0.90,
    "futures_kelly_cap_pct": 0.90,
    "futures_min_prob": 0.45,
    "futures_max_new_per_cycle": 3,
    "max_positions_spot_bull": 8,
    "max_positions_range_scalp": 6,
    "max_positions_preservation": 6,
    "entry_quality_vol_spike_min_spot_bull": 0.45,
    "entry_quality_vol_spike_floor_spot_bull": 0.45,
}

# Load current override
cur.execute("SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'")
row = cur.fetchone()
override = {}
if row:
    override = row[0] if isinstance(row[0], dict) else json.loads(row[0])

old_override = dict(override)
changes = []
for key, val in RESTORE.items():
    old = override.get(key, {}).get("new") if isinstance(override.get(key), dict) else override.get(key)
    if old != val:
        override[key] = {"new": val, "old": old}
        changes.append((key, old, val))

if not changes:
    print("No changes needed")
    conn.close()
    exit(0)

# Update
cur.execute(
    "UPDATE trading_runtime_state SET value = %s::jsonb WHERE key = 'kelly_config_override'",
    (json.dumps(override),)
)

# Log to config_change_log if columns allow
try:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='config_change_log'")
    cols = {r[0] for r in cur.fetchall()}
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_emergency_sl_restore"
    if 'changed_at' in cols and 'snapshot_id' in cols:
        for key, old, val in changes:
            cur.execute(
                """
                INSERT INTO config_change_log (changed_at, snapshot_id, changed_by, details)
                VALUES (NOW(), %s, %s, %s)
                """,
                (snapshot_id, "emergency_diagnostic", json.dumps({"key": key, "old": old, "new": val, "reason": "emergency_sl_restore_canonical"}))
            )
except Exception as e:
    print(f"config_change_log write skipped: {e}")

conn.commit()
conn.close()

print(f"Restored {len(changes)} keys to canonical values:")
for key, old, val in changes:
    print(f"  {key}: {old} -> {val}")
