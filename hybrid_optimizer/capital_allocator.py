"""Capital allocation between SPOT and FUTURES based on regime and recent performance."""
from typing import Dict


def allocate_spot_futures(
    regime: str,
    spot_pnl_7d: float,
    futures_pnl_7d: float,
    spot_max_pct: float = 0.60,
    futures_max_pct: float = 0.75,
) -> Dict:
    """
    Decide how much of the total wallet should be allocated to SPOT vs FUTURES.
    Returns percentages (0..1) and the reasoning trace.
    """
    base_spot = 0.35
    base_futures = 0.35

    # Regime adjustments
    if regime == "SPOT_BULL":
        base_spot += 0.25
        base_futures -= 0.10
    elif regime == "RANGE_SCALP":
        base_spot += 0.15
        base_futures -= 0.05
    elif regime == "FUTURES_SHORT":
        base_futures += 0.30
        base_spot -= 0.10
    elif regime == "PRESERVATION":
        base_spot -= 0.10
        base_futures -= 0.10

    # Recent performance adjustments
    if futures_pnl_7d > spot_pnl_7d + 5:
        base_futures += 0.10
        base_spot -= 0.05
    elif spot_pnl_7d > futures_pnl_7d + 5:
        base_spot += 0.10
        base_futures -= 0.05

    # Clamp
    spot = max(0.0, min(spot_max_pct, base_spot))
    futures = max(0.0, min(futures_max_pct, base_futures))

    # Normalize if sum > 1
    total = spot + futures
    if total > 1.0:
        spot = spot / total
        futures = futures / total

    return {
        "spot_pct": round(spot, 4),
        "futures_pct": round(futures, 4),
        "cash_pct": round(max(0.0, 1.0 - spot - futures), 4),
        "reason": f"regime={regime}, spot_pnl_7d={spot_pnl_7d:.2f}, futures_pnl_7d={futures_pnl_7d:.2f}",
    }
