"""Fix DB kelly_config_override with canonical s62/s63 values and persist to trades_audit."""
import json
import sys
sys.path.insert(0, '/app')

from init_components import get_components

c = get_components()
db = c.db_client
s = c.settings

# Read current DB override
rows = db.execute_query("SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'")
raw = rows[0]['value'] if rows else '{}'
if isinstance(raw, str):
    raw = json.loads(raw)

# Target canonical values (s62 + s63 Wolf Hunter)
updates = {
    'min_signal_strength':            {'new': 0.45},
    'min_success_probability':        {'new': 0.52},
    'vol_spike_min':                  {'new': 0.91},
    'kelly_base_usdt':                {'new': 100.0},
    'kelly_max_usdt':                 {'new': 1000.0},
    'kelly_dynamic_base_pct':         {'new': 0.18},
    'kelly_conviction_vol2x':         {'new': 1.8},
    'kelly_conviction_vol3x':         {'new': 2.5},
    'kelly_conviction_max_pct':       {'new': 0.15},
    'kelly_max_pct_liquid':           {'new': 0.704},
    'kelly_max_pct_cap':              {'new': 0.22},
    'stop_loss_pct':                  {'new': 3.36},
    'futures_kelly_max_usdt':         {'new': 1111.0},
    'slippage_pct':                   {'new': 0.00052},
    'wr_screener_consecutive_sl_block': {'new': 5},
}

raw.update(updates)
new_payload = json.dumps(raw)

db.execute_update(
    "INSERT INTO trading_runtime_state (key, value, updated_at) "
    "VALUES ('kelly_config_override', %s, NOW()) "
    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
    (new_payload, new_payload)
)

db.execute_update(
    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
    "VALUES ('__config__', 'system', 'kelly_config_change', %s, false)",
    (json.dumps({'applied': updates, 'source': 'audit_fix_jul7_s62_s63'}),)
)

# Apply to live settings object
for field, change in updates.items():
    new_val = change['new']
    cur = getattr(s, field, None)
    if cur is not None:
        setattr(s, field, type(cur)(new_val))
    else:
        setattr(s, field, new_val)

print("DB updated and live settings patched. Verification:")
for field in updates:
    live = getattr(s, field, 'MISSING')
    db_val = updates[field]['new']
    ok = abs(float(live) - float(db_val)) < 0.001
    print(f"  {'OK' if ok else 'FAIL'} {field}: live={live}  target={db_val}")
