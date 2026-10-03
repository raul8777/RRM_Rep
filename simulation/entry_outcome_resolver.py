"""Deterministic fixed-contract outcome resolution for MIMIC and offline replay."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence


@dataclass(frozen=True)
class BarExit:
    reason: str
    exit_price: Optional[float]
    ambiguous_intrabar: bool = False
    gap_through: bool = False


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def resolve_bar_exit(
    side: str,
    bar: Dict[str, Any],
    stop_price: float,
    target_price: float,
    *,
    ambiguous_policy: str = "stop_first",
) -> Optional[BarExit]:
    """Resolve one OHLC bar; same-bar stop/target is conservatively stop-first."""
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if ambiguous_policy not in {"stop_first", "mark_ambiguous"}:
        raise ValueError("ambiguous_policy must be stop_first or mark_ambiguous")
    stop = _positive(stop_price)
    target = _positive(target_price)
    if stop is None or target is None:
        raise ValueError("stop and target prices must be positive")

    open_price = _positive(bar.get("open")) or _positive(bar.get("close"))
    high = _positive(bar.get("high")) or open_price
    low = _positive(bar.get("low")) or open_price
    if open_price is None or high is None or low is None:
        raise ValueError("bar must contain positive open/high/low prices")

    if normalized_side == "LONG":
        if open_price < stop:
            return BarExit("SL", open_price, gap_through=True)
        if open_price >= target:
            return BarExit("TP", target, gap_through=open_price > target)
        stop_hit = low <= stop
        target_hit = high >= target
    else:
        if open_price > stop:
            return BarExit("SL", open_price, gap_through=True)
        if open_price <= target:
            return BarExit("TP", target, gap_through=open_price < target)
        stop_hit = high >= stop
        target_hit = low <= target

    if stop_hit and target_hit:
        if ambiguous_policy == "mark_ambiguous":
            return BarExit("AMBIGUOUS", None, ambiguous_intrabar=True)
        return BarExit("SL", stop, ambiguous_intrabar=True)
    if stop_hit:
        return BarExit("SL", stop)
    if target_hit:
        return BarExit("TP", target)
    return None


def resolve_contract_path(
    bars: Sequence[Dict[str, Any]],
    *,
    side: str,
    entry_time: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    horizon_minutes: int,
    quantity: float = 1.0,
    entry_fee_bps: float = 0.0,
    exit_fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    funding_cost_usdt: float = 0.0,
    bar_seconds: int = 60,
    ambiguous_policy: str = "stop_first",
) -> Dict[str, Any]:
    """Replay an already-fixed contract; missing bars return censored, not a label."""
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if horizon_minutes <= 0 or bar_seconds <= 0 or (horizon_minutes * 60) % bar_seconds:
        raise ValueError("horizon must be positive and divisible by bar_seconds")
    entry = _positive(entry_price)
    stop = _positive(stop_price)
    target = _positive(target_price)
    qty = float(quantity)
    if entry is None or stop is None or target is None or not math.isfinite(qty) or qty <= 0:
        raise ValueError("entry, stop, target, and quantity must be positive and finite")

    bars_by_time = {}
    for bar in bars:
        raw_time = bar.get("open_time", bar.get("timestamp"))
        if raw_time is None:
            continue
        timestamp = int(round(float(raw_time)))
        if timestamp in bars_by_time:
            return {
                "outcome": "DATA_GAP",
                "label_valid": False,
                "reason": "DUPLICATE_BAR_TIMESTAMP",
                "duplicate_bar_time": timestamp,
            }
        bars_by_time[timestamp] = bar
    horizon_seconds = horizon_minutes * 60
    steps = horizon_seconds // bar_seconds
    final_bar: Optional[Dict[str, Any]] = None
    resolved: Optional[BarExit] = None
    exit_time: Optional[int] = None
    for step in range(steps):
        timestamp = int(round(float(entry_time))) + step * bar_seconds
        bar = bars_by_time.get(timestamp)
        if bar is None:
            return {
                "outcome": "DATA_GAP",
                "label_valid": False,
                "reason": "EXPECTED_BAR_MISSING",
                "missing_bar_time": timestamp,
            }
        final_bar = bar
        resolved = resolve_bar_exit(
            normalized_side, bar, stop, target, ambiguous_policy=ambiguous_policy,
        )
        if resolved is not None:
            exit_time = timestamp
            break

    if resolved is not None and resolved.reason == "AMBIGUOUS":
        return {
            "outcome": "AMBIGUOUS",
            "label_valid": False,
            "reason": "SAME_BAR_STOP_AND_TARGET",
            "exit_time": exit_time,
            "ambiguous_intrabar": True,
        }
    if resolved is not None:
        reason = resolved.reason
        exit_price = float(resolved.exit_price)
        ambiguous_intrabar = resolved.ambiguous_intrabar
        gap_through = resolved.gap_through
    else:
        if final_bar is None:
            return {"outcome": "DATA_GAP", "label_valid": False, "reason": "NO_HORIZON_BARS"}
        reason = "EXPIRY"
        exit_price = _positive(final_bar.get("close")) or 0.0
        exit_time = int(round(float(entry_time))) + horizon_seconds
        ambiguous_intrabar = False
        gap_through = False
        if exit_price <= 0:
            return {"outcome": "DATA_GAP", "label_valid": False, "reason": "INVALID_EXPIRY_PRICE"}

    direction = 1.0 if normalized_side == "LONG" else -1.0
    gross_pnl = direction * (exit_price - entry) * qty
    entry_fee = entry * qty * max(0.0, entry_fee_bps) / 10000.0
    exit_fee = exit_price * qty * max(0.0, exit_fee_bps) / 10000.0
    slippage = (entry + exit_price) * qty * max(0.0, slippage_bps) / 10000.0
    net_pnl = gross_pnl - entry_fee - exit_fee - slippage - float(funding_cost_usdt)
    return {
        "outcome": reason,
        "label_valid": True,
        "reason": reason,
        "entry_time": int(round(float(entry_time))),
        "exit_time": exit_time,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": qty,
        "gross_pnl_usdt": gross_pnl,
        "entry_fee_usdt": entry_fee,
        "exit_fee_usdt": exit_fee,
        "slippage_usdt": slippage,
        "funding_cost_usdt": float(funding_cost_usdt),
        "net_pnl_usdt": net_pnl,
        "ambiguous_intrabar": ambiguous_intrabar,
        "gap_through": gap_through,
    }