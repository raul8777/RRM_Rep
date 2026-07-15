"""Verify s76 aggressive Kelly values are active in runtime."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from db_client import get_db_client
from config import Settings
s = Settings()
db = get_db_client(s.db_url)

# Check harmony snapshot
rows = db.execute_query("SELECT value FROM trading_runtime_state WHERE key='mode_profile_harmony_snapshot'")
if rows and rows[0].get('value'):
    raw = rows[0]['value']
    if isinstance(raw, str):
        raw = json.loads(raw)
    print("=== mode_profile_harmony_snapshot ===")
    for k in ['kelly_max_usdt', 'kelly_dynamic_base_pct', 'kelly_base_usdt',
              'kelly_max_pct_liquid', 'kelly_max_pct_cap', 'kelly_conviction_max_pct',
              'max_positions_range_scalp']:
        print(f"  {k}: {raw.get(k, 'NOT FOUND')}")
else:
    print("No harmony snapshot yet (waiting for first cycle)")

# Check profiles
for label in ['A', 'B', 'C']:
    key = f"regime_profile_RANGE_SCALP_{label}"
    rows = db.execute_query("SELECT value FROM trading_runtime_state WHERE key=%s", (key,))
    if rows and rows[0].get('value'):
        raw = rows[0]['value']
        if isinstance(raw, str):
            raw = json.loads(raw)
        params = raw.get('params', {})
        print(f"\n=== Profile {label} ===")
        for k in ['kelly_max_usdt', 'kelly_dynamic_base_pct', 'kelly_base_usdt',
                   'kelly_max_pct_liquid', 'kelly_max_pct_cap', 'kelly_conviction_max_pct',
                   'max_positions_range_scalp']:
            print(f"  {k}: {params.get(k, 'NOT SET')}")

# Check current regime
rows = db.execute_query("SELECT value FROM trading_runtime_state WHERE key='effective_runtime_selection_snapshot'")
if rows and rows[0].get('value'):
    raw = rows[0]['value']
    if isinstance(raw, str):
        raw = json.loads(raw)
    print(f"\n=== Active regime ===")
    print(f"  mode: {raw.get('regime_mode', '?')}")
    print(f"  profile: {raw.get('profile_label', '?')}")
