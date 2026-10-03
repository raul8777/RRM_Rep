"""range_scalp_intelligent_hold.py — thesis-based hold/close evaluator for RANGE_SCALP SSO-4.

STAGING s130c Fase 3: replaces the fixed-$/fixed-% profit floor previously used to
decide whether a winning RANGE_SCALP position should keep running instead of being
closed by SSO-4 (`sso_target_range_scalp_guard_min_pnl_pct` / `..._min_abs_gain_usdt`).

Per user directive (2026-08-16): no hardcoded USD or percentage floor. The decision
must instead compare the position's *current structural/momentum/probability state*
against its *own state at entry*, using signals already computed by the pipeline:

  - range_position (0..1, entry vs current): a scalp thesis "plays out" as price
    travels from the edge of its range (entry, near support/resistance) toward the
    opposite side. This is already normalized per-symbol by construction — no
    fitted threshold is needed, only the relative *displacement* and the
    domain-inherent midpoint (0.5) of an already-bounded metric.
  - momentum persistence (velocity / vol_spike / trend): reused as-is from the
    existing SSO-4 guard — already a market-state signal, not a $ or % floor.
  - success_probability trend (entry vs current, from the same predictor used to
    approve the trade): a rising/stable probability supports letting the position
    run; a declining one supports closing now regardless of the $ amount banked.

All comparisons are *relative to the position's own entry snapshot* — nothing here
is a fixed number tuned across all symbols/position sizes. Any dimension whose data
is missing at entry or now is excluded from the decision (never defaulted), so a
partial data gap degrades gracefully to relying on the remaining dimensions instead
of silently pretending a stronger/weaker thesis than the evidence supports.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_thesis(
    *,
    entry_range_position: Optional[float],
    current_range_position: Optional[float],
    entry_success_probability: Optional[float],
    current_success_probability: Optional[float],
    momentum_alive: bool,
    side: str = "LONG",
) -> Dict[str, Any]:
    """Return whether the range-scalp thesis is still intact (worth holding).

    `range_position` is expected in [0, 1] (0 = at the lower edge of the recent
    range / support, 1 = at the upper edge / resistance) — the same convention
    used by `graph_range_position` / `sr_zone_position` elsewhere in the codebase.
    For a SHORT (breakdown) setup the polarity is inverted before comparison.
    """
    is_short = str(side or "LONG").strip().upper() == "SHORT"

    erp = _f(entry_range_position)
    crp = _f(current_range_position)
    esp = _f(entry_success_probability)
    csp = _f(current_success_probability)

    displacement: Optional[float] = None
    thesis_progressing: Optional[bool] = None
    if erp is not None and crp is not None:
        raw_displacement = crp - erp
        # SHORT theses play out moving toward 0 (support); invert so that a
        # positive `displacement` always means "moving as expected".
        displacement = -raw_displacement if is_short else raw_displacement
        # Structural reference: the range midpoint (0.5) is inherent to the
        # already-normalized metric, not a fitted parameter. Thesis is
        # "exhausted" once price has crossed past the midpoint further than
        # where it started, in the expected direction.
        current_effective = (1.0 - crp) if is_short else crp
        entry_effective = (1.0 - erp) if is_short else erp
        thesis_progressing = displacement > 0.0
        thesis_exhausted = current_effective >= 0.5 and current_effective > entry_effective
    else:
        thesis_exhausted = None

    probability_trend: Optional[str] = None
    probability_declining: Optional[bool] = None
    if esp is not None and csp is not None:
        probability_declining = csp < esp
        probability_trend = "declining" if probability_declining else ("improving" if csp > esp else "stable")

    # Decision: hold (thesis_intact=True) unless we have positive evidence the
    # setup is done — never force a close purely from missing data.
    reasons = []
    thesis_intact = True

    if not momentum_alive:
        thesis_intact = False
        reasons.append("momentum_faded")

    if thesis_exhausted is True:
        if probability_declining is True:
            thesis_intact = False
            reasons.append("range_progress_exhausted_and_probability_declining")
        elif probability_declining is None:
            # No probability comparison available — range exhaustion alone is
            # weaker evidence, but still meaningful for a mean-reversion thesis.
            thesis_intact = False
            reasons.append("range_progress_exhausted")
    elif probability_declining is True and momentum_alive is False:
        reasons.append("probability_declining")

    if thesis_intact and not reasons:
        reasons.append("thesis_developing" if thesis_progressing else "insufficient_evidence_to_close_early")

    return {
        "thesis_intact": thesis_intact,
        "reasons": reasons,
        "evidence": {
            "entry_range_position": erp,
            "current_range_position": crp,
            "displacement": displacement,
            "thesis_progressing": thesis_progressing,
            "thesis_exhausted": thesis_exhausted,
            "entry_success_probability": esp,
            "current_success_probability": csp,
            "probability_trend": probability_trend,
            "momentum_alive": momentum_alive,
            "side": "SHORT" if is_short else "LONG",
        },
    }
