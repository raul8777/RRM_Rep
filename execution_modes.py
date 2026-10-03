from __future__ import annotations

from typing import Optional


SPOT_LONG = "SPOT_LONG"
SPOT_MARGIN_SHORT = "SPOT_MARGIN_SHORT"
FUTURES_LONG = "FUTURES_LONG"
FUTURES_SHORT = "FUTURES_SHORT"
ABSTAIN = "ABSTAIN"

VALID_EXECUTION_MODES = {
    SPOT_LONG,
    SPOT_MARGIN_SHORT,
    FUTURES_LONG,
    FUTURES_SHORT,
    ABSTAIN,
}


def trade_type_for_mode(selected_mode: str) -> Optional[str]:
    mode = str(selected_mode or "").strip().upper()
    if mode in {FUTURES_LONG, FUTURES_SHORT}:
        return "futures"
    if mode == SPOT_LONG:
        return "spot"
    if mode == SPOT_MARGIN_SHORT:
        return "spot_margin"
    return None


def side_for_mode(selected_mode: str) -> Optional[str]:
    mode = str(selected_mode or "").strip().upper()
    if mode in {FUTURES_SHORT, SPOT_MARGIN_SHORT}:
        return "SHORT"
    if mode in {FUTURES_LONG, SPOT_LONG}:
        return "LONG"
    return None


def capital_bucket_for_mode(selected_mode: str) -> Optional[str]:
    trade_type = trade_type_for_mode(selected_mode)
    if trade_type == "futures":
        return "futures"
    if trade_type in {"spot", "spot_margin"}:
        return "spot"
    return None


def range_scalp_short_mode(settings) -> str:
    instrument = str(getattr(settings, "range_scalp_short_instrument", "spot_margin") or "spot_margin").strip().lower()
    if instrument == "spot_margin" and bool(getattr(settings, "spot_margin_short_enabled", True)):
        return SPOT_MARGIN_SHORT
    return FUTURES_SHORT