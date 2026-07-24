from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence


def _number(value: Any) -> float:
    return float(value)


def round_trip_cost_pct(commission_rate: float, slippage_pct: float) -> float:
    return 2.0 * (float(commission_rate) + float(slippage_pct)) * 100.0


def build_forward_labels(
    bars: Sequence[Dict[str, Any]],
    horizons_bars: Iterable[int] = (2, 4),
    side: str = "LONG",
    round_trip_cost_pct_value: float = 0.0,
) -> List[Dict[str, Any]]:
    normalized_side = str(side).upper()
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    horizons = sorted({int(horizon) for horizon in horizons_bars if int(horizon) > 0})
    if not horizons:
        return []
    max_horizon = max(horizons)
    output: List[Dict[str, Any]] = []
    for index in range(0, len(bars) - max_horizon):
        entry = _number(bars[index]["close"])
        if entry <= 0:
            continue
        row: Dict[str, Any] = {
            "open_time": bars[index].get("open_time"),
            "entry_price": entry,
            "side": normalized_side,
            "round_trip_cost_pct": float(round_trip_cost_pct_value),
        }
        for horizon in horizons:
            window = bars[index + 1:index + horizon + 1]
            exit_price = _number(window[-1]["close"])
            highs = [_number(bar["high"]) for bar in window]
            lows = [_number(bar["low"]) for bar in window]
            if normalized_side == "LONG":
                gross = (exit_price / entry - 1.0) * 100.0
                mfe = (max(highs) / entry - 1.0) * 100.0
                mae = (min(lows) / entry - 1.0) * 100.0
            else:
                gross = (entry / exit_price - 1.0) * 100.0
                mfe = (entry / min(lows) - 1.0) * 100.0
                mae = (entry / max(highs) - 1.0) * 100.0
            suffix = f"{horizon}h"
            row[f"forward_gross_{suffix}"] = round(gross, 8)
            row[f"forward_net_{suffix}"] = round(gross - round_trip_cost_pct_value, 8)
            row[f"forward_mfe_{suffix}"] = round(mfe, 8)
            row[f"forward_mae_{suffix}"] = round(mae, 8)
        output.append(row)
    return output
