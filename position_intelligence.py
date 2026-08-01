"""position_intelligence.py — S/R-aware position monitoring intelligence.

Provides real-time S/R level tracking and behavioral analysis for open positions.
Designed to be called from the hard_risk_monitor loop (every 2s) using cached data.
S/R levels refresh every 30-60s using 1m klines (1 API call per position per refresh).

Architecture:
  1. S/R Level Cache — refreshes levels every _SR_REFRESH_INTERVAL seconds
  2. Behavioral Tracker — tracks how price interacts with S/R over position lifetime
  3. PIM Evaluator — data-driven decision engine (no hardcoded timeouts)

All decisions are based on:
  - Structural evidence (zone position, bounce/breakout/breakdown confirmations)
  - Behavioral evidence (how many times level tested, whether it held)
  - Momentum evidence (direction of price movement, acceleration)
  - ATR-relative distances (adaptive to each asset's volatility)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from support_resistance import compute_sr_features


# ══════════════════════════════════════════════════════════════════════════════
# S/R LEVEL CACHE
# ══════════════════════════════════════════════════════════════════════════════

_SR_CACHE: Dict[str, Dict[str, Any]] = {}
_SR_CACHE_LOCK = threading.Lock()
_SR_REFRESH_INTERVAL = 30.0  # seconds between S/R re-computations per symbol


def refresh_sr_levels(
    symbol: str,
    fetch_klines_fn: Callable[[str, str, int], List[List]],
    current_price: float,
) -> Optional[Dict[str, Any]]:
    """Recompute S/R levels for a symbol if stale. Returns cached result if fresh.

    Args:
        symbol: Trading pair (e.g. 'BTCUSDT').
        fetch_klines_fn: callable(symbol, interval, limit) -> List[List klines].
        current_price: Current mark/last price for this symbol.

    Returns:
        Dict of S/R features or None if computation fails.
    """
    with _SR_CACHE_LOCK:
        cached = _SR_CACHE.get(symbol)
        if cached and (time.time() - cached['updated_at']) < _SR_REFRESH_INTERVAL:
            # Update current_price in cached data for zone recalculation
            levels = dict(cached['levels'])
            return levels

    # Fetch 1m klines (single API call, weight=1)
    try:
        klines = fetch_klines_fn(symbol, '1m', 150)
        if not klines or len(klines) < 60:
            return None
    except Exception:
        return None

    # Compute S/R features
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    atr = sum(h - l for h, l in zip(highs[-20:], lows[-20:])) / 20.0 if len(highs) >= 20 else None

    levels = compute_sr_features(
        closes, highs, lows, volumes,
        current_price=current_price,
        atr=atr,
        lookback=min(120, len(closes)),
        order=3,
    )
    levels['atr_pct'] = (atr / current_price * 100.0) if atr and current_price > 0 else 0.0
    levels['computed_at'] = time.time()

    with _SR_CACHE_LOCK:
        _SR_CACHE[symbol] = {
            'levels': levels,
            'updated_at': time.time(),
            'klines_tail': klines[-30:],
        }
    return levels


def invalidate_sr_cache(symbol: str) -> None:
    """Remove a symbol from the S/R cache (e.g. when position closes)."""
    with _SR_CACHE_LOCK:
        _SR_CACHE.pop(symbol, None)


def get_cached_sr_levels(symbol: str) -> Optional[Dict[str, Any]]:
    """Return cached S/R levels without triggering refresh. For read-only access."""
    with _SR_CACHE_LOCK:
        cached = _SR_CACHE.get(symbol)
        return dict(cached['levels']) if cached else None


# ══════════════════════════════════════════════════════════════════════════════
# BEHAVIORAL TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def update_sr_behavior(
    smart_meta: Dict[str, Any],
    current_price: float,
    sr_levels: Dict[str, Any],
    side: str,
) -> Dict[str, Any]:
    """Update sr_behavior in _smart_meta based on current price vs S/R levels.

    Tracks:
    - support_tests: how many times price approached support
    - support_held: whether last test was a bounce (True) or break (False)
    - resistance_tests: how many times price approached resistance
    - resistance_broken: whether last test was a breakout (True) or rejection (False)
    - max_zone_reached: highest zone position seen
    - min_zone_reached: lowest zone position seen
    - ticks_in_profit_zone: count of evaluations where price was in favorable zone
    - ticks_total: total evaluations
    - last_sr_event: last significant event (bounce/breakout/breakdown/rejection)
    - momentum_direction: running average of price direction

    Args:
        smart_meta: Position's _smart_meta dict (read, not mutated here).
        current_price: Current price of the asset.
        sr_levels: Dict from refresh_sr_levels().
        side: Position side ('LONG'/'SHORT'/'BUY'/'SELL').

    Returns:
        Updated sr_behavior dict. Caller must persist to _smart_meta['sr_behavior'].
    """
    behavior = dict(smart_meta.get('sr_behavior') or {})

    support = sr_levels.get('support_level')
    resistance = sr_levels.get('resistance_level')
    zone = sr_levels.get('sr_zone_position', 0.5)
    sr_bounce = sr_levels.get('sr_bounce', False)
    sr_breakout = sr_levels.get('sr_breakout', False)
    sr_breakdown = sr_levels.get('sr_breakdown', False)
    atr_pct = sr_levels.get('atr_pct', 1.0)
    cluster_pct = max(0.003, atr_pct / 200.0)

    # Initialize counters if first call
    behavior.setdefault('support_tests', 0)
    behavior.setdefault('support_held', None)
    behavior.setdefault('resistance_tests', 0)
    behavior.setdefault('resistance_broken', None)
    behavior.setdefault('max_zone_reached', zone)
    behavior.setdefault('min_zone_reached', zone)
    behavior.setdefault('ticks_in_profit_zone', 0)
    behavior.setdefault('ticks_total', 0)
    behavior.setdefault('last_sr_event', None)
    behavior.setdefault('last_sr_event_at', None)
    behavior.setdefault('momentum_samples', [])

    # Update zone extremes
    if zone is not None:
        behavior['max_zone_reached'] = max(behavior['max_zone_reached'], zone)
        behavior['min_zone_reached'] = min(behavior['min_zone_reached'], zone)
    behavior['ticks_total'] += 1

    # Track if in profit zone (above 0.5 for longs, below 0.5 for shorts)
    is_long = str(side).upper() in ('LONG', 'BUY')
    if (is_long and zone is not None and zone > 0.5) or (not is_long and zone is not None and zone < 0.5):
        behavior['ticks_in_profit_zone'] += 1

    # Detect support test (price within cluster distance of support)
    if support and support > 0 and current_price > 0:
        dist_to_support = abs(current_price - support) / current_price
        near_support = dist_to_support <= cluster_pct * 1.5
        if near_support:
            if not behavior.get('_at_support'):
                behavior['support_tests'] += 1
                behavior['_at_support'] = True
        else:
            if behavior.get('_at_support'):
                # Left support zone — did it hold?
                behavior['support_held'] = current_price > support
                behavior['last_sr_event'] = 'bounce' if current_price > support else 'breakdown'
                behavior['last_sr_event_at'] = datetime.now(timezone.utc).isoformat()
            behavior['_at_support'] = False

    # Detect resistance test (price within cluster distance of resistance)
    if resistance and resistance > 0 and current_price > 0:
        dist_to_resistance = abs(current_price - resistance) / current_price
        near_resistance = dist_to_resistance <= cluster_pct * 1.5
        if near_resistance:
            if not behavior.get('_at_resistance'):
                behavior['resistance_tests'] += 1
                behavior['_at_resistance'] = True
        else:
            if behavior.get('_at_resistance'):
                # Left resistance zone — did it break?
                behavior['resistance_broken'] = current_price > resistance
                behavior['last_sr_event'] = 'breakout' if current_price > resistance else 'rejection'
                behavior['last_sr_event_at'] = datetime.now(timezone.utc).isoformat()
            behavior['_at_resistance'] = False

    # Override by micro-confirmation events from S/R computation
    if sr_breakdown and behavior.get('_at_support'):
        behavior['support_held'] = False
        behavior['last_sr_event'] = 'breakdown'
        behavior['last_sr_event_at'] = datetime.now(timezone.utc).isoformat()
    if sr_breakout and behavior.get('_at_resistance'):
        behavior['resistance_broken'] = True
        behavior['last_sr_event'] = 'breakout'
        behavior['last_sr_event_at'] = datetime.now(timezone.utc).isoformat()
    if sr_bounce and behavior.get('_at_support'):
        behavior['support_held'] = True
        behavior['last_sr_event'] = 'bounce'
        behavior['last_sr_event_at'] = datetime.now(timezone.utc).isoformat()

    # Micro-momentum: keep last 20 price samples to understand direction
    samples = list(behavior.get('momentum_samples') or [])
    samples.append(current_price)
    if len(samples) > 20:
        samples = samples[-20:]
    behavior['momentum_samples'] = samples

    # Compute momentum direction (positive = price rising)
    if len(samples) >= 5:
        recent_avg = sum(samples[-5:]) / 5
        older_avg = sum(samples[:5]) / 5
        if older_avg > 0:
            behavior['momentum_direction'] = round((recent_avg - older_avg) / older_avg, 8)
        else:
            behavior['momentum_direction'] = 0.0
    else:
        behavior.setdefault('momentum_direction', 0.0)

    return behavior


def has_behavior_changed(old_behavior: Optional[Dict], new_behavior: Dict) -> bool:
    """Return True if behavioral state changed meaningfully (worth persisting)."""
    if not old_behavior:
        return True
    return (
        new_behavior.get('support_tests') != old_behavior.get('support_tests')
        or new_behavior.get('resistance_tests') != old_behavior.get('resistance_tests')
        or new_behavior.get('last_sr_event') != old_behavior.get('last_sr_event')
        or new_behavior.get('support_held') != old_behavior.get('support_held')
        or new_behavior.get('resistance_broken') != old_behavior.get('resistance_broken')
    )


# ══════════════════════════════════════════════════════════════════════════════
# PIM EVALUATOR — Decision Engine
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PIMAction:
    """Result of Position Intelligence evaluation."""
    HOLD = "hold"
    TIGHTEN = "tighten_sl"
    FLAG_EXIT = "flag_exit"
    EMERGENCY = "emergency"

    action: str
    reason: str
    confidence: float
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }


def evaluate_position_intelligence(
    entry_price: float,
    current_price: float,
    side: str,
    sr_levels: Dict[str, Any],
    sr_behavior: Dict[str, Any],
    peak_pnl_pct: float = 0.0,
    pnl_velocity: Optional[float] = None,
    breakeven_done: bool = False,
    trailing_active: bool = False,
) -> PIMAction:
    """Core intelligence: evaluate position vs S/R structure and behavioral evidence.

    Decision logic is ENTIRELY data-driven:
    - No hardcoded timeouts
    - Uses ATR-relative distances (dynamic per asset)
    - Uses behavioral evidence (how many times tested, what happened)
    - Uses momentum (direction of price movement)

    Confidence scale:
    - < 0.3: no action taken by caller (logged only)
    - 0.3-0.7: moderate signal (FLAG_EXIT or TIGHTEN)
    - > 0.7: strong signal (EMERGENCY or aggressive action)

    Args:
        entry_price: Position entry price.
        current_price: Current market price.
        side: 'LONG'/'SHORT'/'BUY'/'SELL'.
        sr_levels: S/R feature dict from refresh_sr_levels().
        sr_behavior: Behavioral dict from update_sr_behavior().
        peak_pnl_pct: Highest PnL% reached by this position.
        pnl_velocity: PnL velocity per hour (from compute_pnl_velocity).
        breakeven_done: Whether breakeven has been activated.
        trailing_active: Whether trailing stop is active.

    Returns:
        PIMAction with action, reason, confidence, and detail.
    """
    is_long = str(side).upper() in ('LONG', 'BUY')
    zone = sr_levels.get('sr_zone_position', 0.5)
    support = sr_levels.get('support_level')
    resistance = sr_levels.get('resistance_level')
    sr_bounce = sr_levels.get('sr_bounce', False)
    sr_breakout = sr_levels.get('sr_breakout', False)
    sr_breakdown = sr_levels.get('sr_breakdown', False)
    support_strength = sr_levels.get('support_strength', 0.0) or 0.0
    atr_pct = sr_levels.get('atr_pct', 1.0)

    # Behavioral evidence
    support_tests = int(sr_behavior.get('support_tests', 0) or 0)
    support_held = sr_behavior.get('support_held')
    resistance_tests = int(sr_behavior.get('resistance_tests', 0) or 0)
    resistance_broken = sr_behavior.get('resistance_broken')
    momentum_dir = float(sr_behavior.get('momentum_direction', 0.0) or 0.0)
    ticks_total = max(1, int(sr_behavior.get('ticks_total', 1) or 1))
    ticks_profit = int(sr_behavior.get('ticks_in_profit_zone', 0) or 0)

    # PnL calculation
    if entry_price <= 0 or current_price <= 0:
        return PIMAction(PIMAction.HOLD, "invalid_prices", 0.0)

    pnl_pct = (current_price - entry_price) / entry_price if is_long else (entry_price - current_price) / entry_price
    in_profit = pnl_pct > 0

    # ══════════════════════════════════════════════════════════════════════════
    # EMERGENCY: Confirmed structural break AGAINST our position
    # ══════════════════════════════════════════════════════════════════════════

    if is_long and sr_breakdown:
        confidence = 0.6
        if support_tests >= 2 and support_held is False:
            confidence += 0.2  # Support already failed before
        if momentum_dir < -0.001:
            confidence += 0.1  # Price accelerating down
        if not in_profit:
            confidence += 0.1  # Already losing
        confidence = min(1.0, confidence)
        if confidence >= 0.7:
            return PIMAction(
                PIMAction.EMERGENCY, "support_broken_confirmed", confidence,
                f"breakdown+tests={support_tests}+held={support_held}+mom={momentum_dir:.5f}"
            )

    if not is_long and sr_breakout:
        confidence = 0.6
        if resistance_tests >= 2 and resistance_broken is True:
            confidence += 0.2
        if momentum_dir > 0.001:
            confidence += 0.1
        if not in_profit:
            confidence += 0.1
        confidence = min(1.0, confidence)
        if confidence >= 0.7:
            return PIMAction(
                PIMAction.EMERGENCY, "resistance_broken_confirmed", confidence,
                f"breakout+tests={resistance_tests}+broken={resistance_broken}+mom={momentum_dir:.5f}"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # HOLD: Position at favorable level with evidence of support/resistance
    # ══════════════════════════════════════════════════════════════════════════

    if is_long and not in_profit and zone is not None and zone <= 0.25:
        hold_confidence = 0.3
        if sr_bounce:
            hold_confidence += 0.25
        if support_tests >= 1 and support_held is True:
            hold_confidence += 0.2
        if support_strength >= 0.6:
            hold_confidence += 0.15
        if momentum_dir > 0:
            hold_confidence += 0.1
        # Counter-evidence reduces confidence
        if support_tests >= 2 and support_held is False:
            hold_confidence -= 0.4
        if momentum_dir < -0.002:
            hold_confidence -= 0.2

        hold_confidence = max(0.0, min(1.0, hold_confidence))
        if hold_confidence >= 0.5:
            return PIMAction(
                PIMAction.HOLD, "long_support_hold", hold_confidence,
                f"zone={zone:.3f}+bounce={sr_bounce}+tests={support_tests}+held={support_held}+str={support_strength:.2f}"
            )

    if not is_long and not in_profit and zone is not None and zone >= 0.75:
        hold_confidence = 0.3
        if not sr_breakout:
            hold_confidence += 0.2
        if resistance_tests >= 1 and resistance_broken is False:
            hold_confidence += 0.2
        if momentum_dir < 0:
            hold_confidence += 0.1
        # Counter-evidence
        if resistance_tests >= 2 and resistance_broken is True:
            hold_confidence -= 0.4
        if momentum_dir > 0.002:
            hold_confidence -= 0.2

        hold_confidence = max(0.0, min(1.0, hold_confidence))
        if hold_confidence >= 0.5:
            return PIMAction(
                PIMAction.HOLD, "short_resistance_hold", hold_confidence,
                f"zone={zone:.3f}+tests={resistance_tests}+broken={resistance_broken}"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # TIGHTEN: Approaching opposing level with profit — protect gains
    # ══════════════════════════════════════════════════════════════════════════

    if is_long and in_profit and zone is not None and zone >= 0.75:
        # If breakout IS confirmed, DON'T tighten — let it run
        if sr_breakout:
            return PIMAction(
                PIMAction.HOLD, "long_breakout_momentum", 0.7,
                f"breakout_confirmed+zone={zone:.3f}"
            )
        tighten_confidence = 0.4
        if resistance_tests >= 1 and resistance_broken is False:
            tighten_confidence += 0.25
        if momentum_dir < 0:
            tighten_confidence += 0.1
        if not sr_breakout:
            tighten_confidence += 0.1

        tighten_confidence = max(0.0, min(1.0, tighten_confidence))
        if tighten_confidence >= 0.5:
            return PIMAction(
                PIMAction.TIGHTEN, "approaching_resistance", tighten_confidence,
                f"zone={zone:.3f}+rejected_before={resistance_tests}+mom={momentum_dir:.5f}"
            )

    if not is_long and in_profit and zone is not None and zone <= 0.25:
        if sr_breakdown:
            return PIMAction(
                PIMAction.HOLD, "short_breakdown_momentum", 0.7,
                f"breakdown_confirmed+zone={zone:.3f}"
            )
        tighten_confidence = 0.4
        if support_tests >= 1 and support_held is True:
            tighten_confidence += 0.25
        if momentum_dir > 0:
            tighten_confidence += 0.1
        if not sr_breakdown:
            tighten_confidence += 0.1

        tighten_confidence = max(0.0, min(1.0, tighten_confidence))
        if tighten_confidence >= 0.5:
            return PIMAction(
                PIMAction.TIGHTEN, "approaching_support", tighten_confidence,
                f"zone={zone:.3f}+held_before={support_tests}+mom={momentum_dir:.5f}"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # FLAG_EXIT: Multiple rejections at level = trade thesis exhausted
    # ══════════════════════════════════════════════════════════════════════════

    if is_long and in_profit and resistance_tests >= 2 and resistance_broken is False:
        flag_confidence = 0.4 + min(0.3, resistance_tests * 0.1)
        # Giveback penalty: if we're losing profit from peak
        giveback = peak_pnl_pct - pnl_pct
        if giveback > 0 and atr_pct > 0 and giveback > (atr_pct / 100.0):
            flag_confidence += 0.15
        flag_confidence = max(0.0, min(1.0, flag_confidence))
        if flag_confidence >= 0.5:
            return PIMAction(
                PIMAction.FLAG_EXIT, "resistance_exhaustion", flag_confidence,
                f"rejected={resistance_tests}x+giveback={giveback:.5f}+peak={peak_pnl_pct:.5f}"
            )

    if not is_long and in_profit and support_tests >= 2 and support_held is True:
        flag_confidence = 0.4 + min(0.3, support_tests * 0.1)
        giveback = peak_pnl_pct - pnl_pct
        if giveback > 0 and atr_pct > 0 and giveback > (atr_pct / 100.0):
            flag_confidence += 0.15
        flag_confidence = max(0.0, min(1.0, flag_confidence))
        if flag_confidence >= 0.5:
            return PIMAction(
                PIMAction.FLAG_EXIT, "support_exhaustion", flag_confidence,
                f"bounced={support_tests}x+giveback={giveback:.5f}+peak={peak_pnl_pct:.5f}"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # NEUTRAL: No strong signal
    # ══════════════════════════════════════════════════════════════════════════
    return PIMAction(PIMAction.HOLD, "neutral", 0.0, "no_sr_signal")


# ══════════════════════════════════════════════════════════════════════════════
# TRAILING DISTANCE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def compute_sr_aware_trail_distance(
    side: str,
    base_trail_dist_pct: float,
    sr_levels: Optional[Dict[str, Any]],
    current_price: float,
) -> float:
    """Compute an S/R-aware trailing distance that tightens near opposing levels.

    The trailing distance reduces proportionally as price approaches resistance
    (for longs) or support (for shorts). If far from opposing level, uses base
    distance. Floor at 30% of base to prevent overly tight stops.

    Args:
        side: 'LONG'/'SHORT'.
        base_trail_dist_pct: Base trailing distance as decimal (e.g. 0.015 for 1.5%).
        sr_levels: S/R features dict, or None to use base distance.
        current_price: Current price.

    Returns:
        Adjusted trailing distance as decimal fraction.
    """
    if not sr_levels or current_price <= 0 or base_trail_dist_pct <= 0:
        return base_trail_dist_pct

    is_long = str(side).upper() in ('LONG', 'BUY')

    if is_long:
        resistance = sr_levels.get('resistance_level')
        if resistance and resistance > current_price:
            dist_to_r = (resistance - current_price) / current_price
            # Proximity factor: 1.0 when far, reduces toward 0.3 when very close
            proximity_factor = max(0.3, min(1.0, dist_to_r / base_trail_dist_pct))
            return base_trail_dist_pct * proximity_factor
    else:
        support = sr_levels.get('support_level')
        if support and support < current_price and support > 0:
            dist_to_s = (current_price - support) / current_price
            proximity_factor = max(0.3, min(1.0, dist_to_s / base_trail_dist_pct))
            return base_trail_dist_pct * proximity_factor

    return base_trail_dist_pct
