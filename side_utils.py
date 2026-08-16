from __future__ import annotations


def normalize_side(side: str | None) -> str:
    value = str(side or "").strip().upper()
    if value in ("BUY", "LONG"):
        return "LONG"
    if value in ("SELL", "SHORT"):
        return "SHORT"
    return value or "LONG"


def is_long_side(side: str | None) -> bool:
    return normalize_side(side) == "LONG"