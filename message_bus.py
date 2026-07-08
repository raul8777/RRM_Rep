"""
message_bus.py — Minimal stub for test compatibility.
"""

EVENT_SIGNAL = "signal"
EVENT_ORDER  = "order"


class _MessageBus:
    def __init__(self):
        self._handlers = {}

    def subscribe(self, event: str, handler):
        self._handlers.setdefault(event, []).append(handler)

    def publish(self, event: str, payload=None):
        for h in self._handlers.get(event, []):
            h(payload)


message_bus = _MessageBus()
