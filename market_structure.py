from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from support_resistance import compute_sr_features


SCHEMA_VERSION = "lifecycle_structure_v1"


@dataclass(frozen=True)
class StructureDecision:
    state: str
    action: str
    reason: str
    confirmation_path: str
    snapshot: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "action": self.action,
            "reason": self.reason,
            "confirmation_path": self.confirmation_path,
            "snapshot": self.snapshot,
        }


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _closed_klines(klines: List[List], now_ms: int) -> List[List]:
    return [row for row in klines if len(row) > 6 and int(row[6]) < now_ms]


def _features(klines: List[List], current_price: float, lookback: int, order: int) -> Optional[Dict[str, Any]]:
    if len(klines) < lookback:
        return None
    rows = klines[-lookback:]
    closes = [float(row[4]) for row in rows]
    highs = [float(row[2]) for row in rows]
    lows = [float(row[3]) for row in rows]
    volumes = [float(row[5]) for row in rows]
    atr = sum(high - low for high, low in zip(highs[-20:], lows[-20:])) / 20.0 if len(rows) >= 20 else None
    result = compute_sr_features(
        closes,
        highs,
        lows,
        volumes,
        current_price=current_price,
        atr=atr,
        lookback=lookback,
        order=order,
    )
    result["source_close_ms"] = int(rows[-1][6])
    result["candle_count"] = len(rows)
    return result


def build_snapshot(
    fetch_klines: Callable[[str, str, int], List[List]],
    symbol: str,
    current_price: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    price = _number(current_price)
    now = now or datetime.now(timezone.utc)
    if price is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "unavailable",
            "reason": "invalid_current_price",
            "generated_at": now.isoformat(),
            "intervals": {},
        }

    now_ms = int(now.timestamp() * 1000)
    specs = {
        "1m": (150, 120, 3),
        "5m": (120, 96, 3),
        "1h": (120, 96, 3),
        "4h": (80, 60, 3),
    }
    intervals: Dict[str, Any] = {}
    for interval, (limit, lookback, order) in specs.items():
        try:
            raw = fetch_klines(symbol, interval, limit) or []
            closed = _closed_klines(raw, now_ms)
            feature = _features(closed, price, lookback, order)
            intervals[interval] = feature or {
                "state": "unavailable",
                "reason": "insufficient_closed_candles",
                "candle_count": len(closed),
            }
        except Exception as exc:
            intervals[interval] = {"state": "unavailable", "reason": str(exc)}

    structure = intervals.get("1h") or {}
    confirmation = intervals.get("5m") or {}
    timing = intervals.get("1m") or {}
    required = (structure, confirmation, timing)
    if any(item.get("state") == "unavailable" for item in required):
        state, reason = "unavailable", "missing_required_structure_data"
    else:
        state, reason = "neutral", "no_zone_event"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "state": state,
        "reason": reason,
        "current_price": price,
        "intervals": intervals,
        "context_4h": intervals.get("4h"),
        "structure_1h": structure,
        "confirmation_5m": confirmation,
        "timing_1m": timing,
    }


def evaluate(snapshot: Dict[str, Any], side: str) -> StructureDecision:
    if snapshot.get("state") == "unavailable":
        return StructureDecision("unavailable", "allow_exit", snapshot.get("reason", "unavailable"), "none", snapshot)

    normalized_side = str(side or "LONG").upper()
    structure = snapshot.get("structure_1h") or {}
    confirmation = snapshot.get("confirmation_5m") or {}
    timing = snapshot.get("timing_1m") or {}
    # s106b: explicit None-check, not `or` — 0.0 (price exactly at support) is a
    # legitimate value that `x or 0.5` would silently discard as "missing".
    _zone_raw = structure.get("sr_zone_position")
    zone = float(_zone_raw) if _zone_raw is not None else 0.5
    micro_confirmation = bool(
        confirmation.get("sr_bounce")
        or confirmation.get("sr_breakout")
        or confirmation.get("sr_breakdown")
        or timing.get("sr_bounce")
        or timing.get("sr_breakout")
        or timing.get("sr_breakdown")
    )
    if not micro_confirmation:
        return StructureDecision("neutral", "allow_exit", "no_micro_confirmation", "none", snapshot)

    if normalized_side in ("LONG", "BUY"):
        if zone <= 0.25 and (confirmation.get("sr_bounce") or timing.get("sr_bounce")):
            return StructureDecision("structural_confirmed", "hold_loss", "long_support_reclaim", "1h_support+micro_bounce", snapshot)
        if zone >= 0.85 and not (confirmation.get("sr_breakout") or timing.get("sr_breakout")):
            return StructureDecision("structural_confirmed", "partial_profit", "long_resistance_rejection", "1h_resistance+micro_rejection", snapshot)
    else:
        if zone >= 0.75 and not (confirmation.get("sr_breakout") or timing.get("sr_breakout")):
            return StructureDecision("structural_confirmed", "hold_loss", "short_resistance_rejection", "1h_resistance+micro_rejection", snapshot)
        if zone <= 0.15 and not (confirmation.get("sr_breakdown") or timing.get("sr_breakdown")):
            return StructureDecision("structural_confirmed", "partial_profit", "short_support_hold", "1h_support+micro_hold", snapshot)

    return StructureDecision("invalidated", "allow_exit", "structure_invalidated", "confirmed_break", snapshot)
