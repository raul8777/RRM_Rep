from init_components import get_components
import json

comp = get_components()
db = comp.db_client if hasattr(comp, 'db_client') else None
if db:
    keys = [
        'kelly_config_override',
        'regime_profile_active_RANGE_SCALP',
        'regime_profile_active_SPOT_BULL',
        'effective_runtime_selection_snapshot',
        'mode_profile_harmony_snapshot'
    ]
    placeholders = ','.join(['%s'] * len(keys))
    rows = db.execute_query(
        f"SELECT key, value FROM trading_runtime_state WHERE key IN ({placeholders})",
        tuple(keys)
    )
    for r in rows:
        print('---', r['key'], '---')
        print(json.dumps(r['value'], default=str, indent=2)[:2000])
else:
    print('no db')
