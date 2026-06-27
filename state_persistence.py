"""
state_persistence.py

Persiste en BD (trading_runtime_state) el estado volátil en memoria de los ciclos
de trading para que sobreviva a reinicios del contenedor app:

- Cooldowns de SL de futuros (_FUTURES_SL_COOLDOWN)
- Cooldown global por mass-SL (_FUTURES_MASS_SL_COOLDOWN_UNTIL)
- Cooldown de VolSpike override (_VOLSPIKE_COOLDOWN)
- Posiciones con override activo (_VOLSPIKE_POSITIONS)
- Contador diario de overrides (_OVERRIDE_DAILY_COUNT)

El módulo expuye save_state() / load_state() para ser invocado desde run_trades.py
al final de cada ciclo y durante el startup del sistema.
"""
import json
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any

_RUNTIME_KEY = "volatile_state"


def _serialize_dt(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, dict):
        return {k: _serialize_dt(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_dt(v) for v in obj]
    return obj


def _deserialize_dt(obj: Any) -> Any:
    if isinstance(obj, str):
        try:
            return datetime.fromisoformat(obj)
        except Exception:
            return obj
    if isinstance(obj, dict):
        return {k: _deserialize_dt(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deserialize_dt(v) for v in obj]
    return obj


def save_state(
    db_client,
    futures_sl_cooldown: Dict[str, datetime],
    futures_mass_sl_cooldown_until: Optional[datetime],
    volspike_cooldown: Dict[str, datetime],
    volspike_positions: set,
    override_daily_count: Dict[str, int],
) -> None:
    """Persist volatile in-memory state to trading_runtime_state."""
    if not db_client:
        return
    try:
        payload = {
            "futures_sl_cooldown": _serialize_dt(futures_sl_cooldown),
            "futures_mass_sl_cooldown_until": _serialize_dt(futures_mass_sl_cooldown_until),
            "volspike_cooldown": _serialize_dt(volspike_cooldown),
            "volspike_positions": _serialize_dt(volspike_positions),
            "override_daily_count": override_daily_count,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (_RUNTIME_KEY, json.dumps(payload), json.dumps(payload))
        )
    except Exception as e:
        print(f"[StatePersistence] save failed: {e}")


def load_state(db_client) -> Dict[str, Any]:
    """Load volatile state from trading_runtime_state. Returns empty dict on error."""
    if not db_client:
        return {}
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s", (_RUNTIME_KEY,)
        )
        if not rows:
            return {}
        value = rows[0].get("value")
        if isinstance(value, str):
            value = json.loads(value)
        return _deserialize_dt(value)
    except Exception as e:
        print(f"[StatePersistence] load failed: {e}")
        return {}
