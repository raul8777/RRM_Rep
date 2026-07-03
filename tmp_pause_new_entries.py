from init_components import get_components
import json

comp = get_components()
db = comp.db_client
key = 'pause_new_entries'
db.execute_update(
    'INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()',
    (key, json.dumps({'paused': True, 'reason': 'full config audit', 'started_at': __import__('datetime').datetime.utcnow().isoformat()}))
)
db.execute_update(
    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
    ('__config__', 'system', 'pause_new_entries', json.dumps({'paused': True, 'reason': 'full config audit'}), False)
)
print('New entries paused. Key:', key)
