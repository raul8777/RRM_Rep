from init_components import get_components
from config_helpers import dump_active_config
import json

comp = get_components()
s = comp.settings
db = comp.db_client
snap = dump_active_config(s)
key = 'config_snapshot_pre_full_audit_20260628'
db.execute_update(
    'INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()',
    (key, json.dumps(snap))
)
db.execute_update(
    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
    ('__config__', 'system', 'config_snapshot_pre_audit', json.dumps({'snapshot_id': key, 'source': 'manual_audit'}), False)
)
print('Snapshot saved:', key)
