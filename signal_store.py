from datetime import datetime
from threading import Lock
from typing import Any, Dict, List

_lock = Lock()
_signals: List[Dict[str, Any]] = []


def add_signal(signal: Dict[str, Any]) -> None:
    record = signal.copy()
    record["received_at"] = datetime.utcnow().isoformat() + "Z"
    with _lock:
        _signals.append(record)


def get_signals() -> List[Dict[str, Any]]:
    with _lock:
        return list(_signals)


def clear_signals() -> None:
    with _lock:
        _signals.clear()
