"""
Smart Scale-Out (SSO) — Adaptive multi-signal exit system.

Replaces fixed percentage scale-out (PV1) with 5 intelligent triggers
that maximize capital velocity toward $40+/day profit target.

Triggers:
  SSO-1: Stagnant Profit (time + velocity = 0)
  SSO-2: Momentum Exhausted (RSI + volume dying)
  SSO-3: Multi-level ATR Scale (ATR-based increments)
  SSO-4: Target Contribution (absolute $ toward daily goal)
  SSO-5: Re-entry Upgrade (better opportunity available)
  SSO-6: Bounce Rescue (close bounce-trapped entries near breakeven)
  SSO-7: Stagnation Trailing + Opportunity Collapse (squeeze SL toward price when stagnant;
          collapse SL to price when a better candidate is waiting)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any


@dataclass
class SSOAction:
    """Result of SSO evaluation."""
    should_close: bool = False
    close_pct: float = 0.0          # 0.0–1.0 fraction to close
    reason: str = ""                 # e.g. "sso_stagnant_profit"
    detail: str = ""                 # human-readable explanation
    trigger: str = ""                # SSO-1, SSO-2, etc.
    priority: int = 99              # lower = higher priority
    diagnosis: str = ""             # always set: why each sub-trigger did/didn't fire


@dataclass
class SSOPositionData:
    """All data needed by SSO to evaluate a position."""
    symbol: str
    side: str                        # "LONG", "SHORT", "BUY"
    pnl_pct: float                   # current PnL as ratio (0.015 = 1.5%)
    pnl_usdt: float                  # unrealized PnL in USDT
    position_size_usdt: float        # notional position size
    entry_price: float
    current_price: float
    opened_at: Optional[datetime] = None
    peak_pnl_pct: float = 0.0       # highest PnL reached
    # Velocity tracking
    last_pnl_snapshot: Optional[Dict] = None  # {"ts": iso, "pnl_pct": float}
    pnl_velocity_1h: Optional[float] = None   # delta pnl_pct over last 1h
    # Multi-level state
    sso_levels_done: List[int] = field(default_factory=list)
    # Technical
    rsi_1h: Optional[float] = None
    vol_spike: Optional[float] = None
    trend_4h: Optional[str] = None
    atr_4h_pct: Optional[float] = None  # ATR as % of price
    # Context
    today_realized_pnl: float = 0.0
    daily_target: float = 40.0
    trade_type: str = "futures"     # "futures" or "spot"
    regime_mode: Optional[str] = None
    profile_label: Optional[str] = None
    # For SSO-5
    current_strength: float = 0.5
    best_available_ev: float = 0.0
    all_slots_full: bool = False
    # For SSO-6: Bounce Rescue
    bounce_entry: bool = False          # True when position is trapped in BTC bounce
    rescue_prediction_ev: Optional[float] = None   # updated EV from predictor at rescue check time
    rescue_signal: Optional[str] = None            # current signal (BUY/SELL/HOLD) at rescue check
    rescue_signal_strength: float = 0.0            # current signal strength at rescue check
    # Gate expiry tracking (injected from smart_meta / metadata)
    _gate_block_count: Optional[int] = None        # how many consecutive cycles this gate has blocked
    _gate_last_pnl_pct: Optional[float] = None     # pnl_pct when gate first blocked (for improvement check)
    # For SSO-7: Stagnation Trailing + Opportunity Collapse
    current_sl_price: Optional[float] = None       # current SL price in absolute terms
    sso7_stagnation_cycles: int = 0                # consecutive cycles with no PnL advance (from metadata)
    sso7_last_advance_pnl: Optional[float] = None  # pnl_usdt at last confirmed advance (from metadata)
    best_candidate_strength: float = 0.0           # strength of best available candidate signal
    best_candidate_prob: float = 0.0               # success_probability of best candidate
    best_candidate_vol_spike: float = 0.0          # vol_spike of best candidate
    best_candidate_pred_ev: float = 0.0            # expected_pnl (EV) of best candidate
    current_pred_ev: float = 0.0                   # current EV of this position from predictor


class SmartScaleOut:
    """Evaluates all 5 SSO triggers and returns the highest-priority action."""

    # Dynamic USD gates: se calculan automáticamente desde el balance del portfolio.
    # futures_total y spot_total se pasan en runtime por cada call site.
    # Fórmulas: decay=0.2%, stagnant=0.3%, slow=0.1%, daily_target=5% del wallet.
    # Fallback a env vars / defaults si no se pasan (SPOT usa spot_total, FUTURES usa futures_total).

    def __init__(self, settings=None, futures_total: float = None, spot_total: float = None):
        self._s = settings

        # ── Dynamic USD gates por tipo de portfolio ───────────────────────────
        # Selecciona el capital de referencia según el tipo de portfolio disponible
        _ref_capital = futures_total or spot_total  # futures tiene prioridad si ambos presentes
        if _ref_capital and _ref_capital > 0:
            _dyn_decay    = round(_ref_capital * 0.002, 2)   # 0.2% del wallet
            _dyn_stagnant = round(_ref_capital * 0.003, 2)   # 0.3% del wallet
            _dyn_slow     = round(_ref_capital * 0.001, 2)   # 0.1% del wallet
            _dyn_daily    = round(_ref_capital * 0.05,  2)   # 5%   del wallet (meta diaria)
            _portfolio_tag = "futures" if futures_total else "spot"
            print(f"[SSO] dynamic gates ({_portfolio_tag} capital={_ref_capital:.0f}): "
                  f"decay=${_dyn_decay} stagnant=${_dyn_stagnant} "
                  f"slow=${_dyn_slow} daily=${_dyn_daily}")
        else:
            _dyn_decay = _dyn_stagnant = _dyn_slow = _dyn_daily = None

        # SSO-1: Stagnant Profit
        self.stagnant_min_profit_factor = self._cfg('sso_stagnant_min_profit_factor', 0.02)
        self.stagnant_min_profit_floor = _dyn_stagnant if _dyn_stagnant else self._cfg('sso_stagnant_min_profit_floor', 3.0)
        self.stagnant_min_hours = self._cfg('sso_stagnant_min_hours', 2.0)
        self.stagnant_velocity_window_min = self._cfg('sso_stagnant_velocity_window_min', 60)
        # SSO-2: Momentum Exhausted
        self.momentum_rsi_overbought = self._cfg('sso_momentum_rsi_overbought', 70)
        self.momentum_rsi_oversold = self._cfg('sso_momentum_rsi_oversold', 30)
        self.momentum_min_pnl_futures = self._cfg('sso_momentum_min_pnl_pct_futures', 1.0) / 100.0
        self.momentum_min_pnl_spot = self._cfg('sso_momentum_min_pnl_pct_spot', 1.5) / 100.0
        self.momentum_vol_dying = self._cfg('sso_momentum_vol_dying_threshold', 0.5)
        # SSO-3: Multi-level
        self.multilevel_enabled = self._cfg('sso_multilevel_enabled', True)
        self.ml_l1_mult = self._cfg('sso_multilevel_l1_atr_mult', 1.0)
        self.ml_l2_mult = self._cfg('sso_multilevel_l2_atr_mult', 2.0)
        self.ml_l3_mult = self._cfg('sso_multilevel_l3_atr_mult', 3.5)
        self.ml_l1_close = self._cfg('sso_multilevel_l1_close_pct', 0.30)
        self.ml_l2_close = self._cfg('sso_multilevel_l2_close_pct', 0.30)
        self.ml_l3_close = self._cfg('sso_multilevel_l3_close_pct', 0.25)
        # SSO-4: Target Contribution
        self.target_contribution_pct = self._cfg('sso_target_contribution_pct', 0.15)
        self.target_daily_usd = _dyn_daily if _dyn_daily else self._cfg('sso_target_daily_usd', 40.0)
        self.target_min_hours = self._cfg('sso_target_min_hours', 1.0)
        # SSO-5: Re-entry Upgrade
        self.reentry_min_strength_diff = self._cfg('sso_reentry_min_strength_diff', 0.3)
        self.reentry_min_ev_multiplier = self._cfg('sso_reentry_min_ev_multiplier', 1.5)
        # SSO-6: Bounce Rescue Mode
        self.bounce_rescue_enabled = self._cfg('futures_bounce_rescue_enabled', True)
        self.bounce_rescue_max_peak_pct = self._cfg('futures_bounce_rescue_max_peak_pct', 0.5) / 100.0
        self.bounce_rescue_min_hours = self._cfg('futures_bounce_rescue_min_hours', 4.0)
        self.bounce_rescue_target_pnl = self._cfg('futures_bounce_rescue_target_pnl', -0.50)
        # Store dynamic overrides for SSO-1b and SSO-1c (read inside _check_sso1_stagnant)
        self._dyn_decay_min_profit = _dyn_decay
        self._dyn_slow_min_profit = _dyn_slow

    def _cfg(self, key: str, default):
        """Read config from settings via get_param so runtime governance applies."""
        if self._s:
            from config_helpers import get_param
            return get_param(self._s, key, default)
        return default

    def _profile_scale(self, pos: SSOPositionData, aggressive: float = 1.0, balanced: float = 1.0, defensive: float = 1.0) -> float:
        profile = str(getattr(pos, 'profile_label', '') or '').strip().upper()
        if profile == 'A':
            return aggressive
        if profile == 'C':
            return defensive
        return balanced

    def _regime_scale(self, pos: SSOPositionData, spot_bull: float = 1.0, range_scalp: float = 1.0, preservation: float = 1.0, futures_short: float = 1.0) -> float:
        regime = str(getattr(pos, 'regime_mode', '') or '').strip().upper()
        return {
            'SPOT_BULL': spot_bull,
            'RANGE_SCALP': range_scalp,
            'PRESERVATION': preservation,
            'FUTURES_SHORT': futures_short,
        }.get(regime, 1.0)

    def evaluate(self, pos: SSOPositionData) -> SSOAction:
        """
        Evaluate all SSO triggers in priority order.
        Returns the first (highest priority) action that fires, or no-action.
        Always populates diagnosis with per-trigger status.
        """
        _diag_parts = []
        checks = [
            ("SSO-LOSS", self._check_sso_loss),
            ("SSO-6", self._check_sso6_bounce_rescue),
            ("SSO-1", self._check_sso1_stagnant),
            ("SSO-2", self._check_sso2_momentum_exhausted),
            ("SSO-3", self._check_sso3_multilevel),
            ("SSO-4", self._check_sso4_target_contribution),
            ("SSO-5", self._check_sso5_reentry_upgrade),
            ("SSO-7", self._check_sso7_stagnation_exit),
        ]
        for label, check_fn in checks:
            action = check_fn(pos)
            if action and action.should_close:
                action.diagnosis = " | ".join(_diag_parts + [f"{label}:FIRE({action.reason})"])
                return action
            # Collect why it didn't fire
            _skip_reason = action.diagnosis if (action and action.diagnosis) else f"{label}:skip"
            _diag_parts.append(_skip_reason)
        no_action = SSOAction()
        no_action.diagnosis = " | ".join(_diag_parts)
        return no_action

    # ── SSO-LOSS: Fast loss cut when position has reversed against us ───────────
    def _check_sso_loss(self, pos: SSOPositionData) -> SSOAction:
        """Close 100% if the position is losing and current signal is against it."""
        if pos.pnl_pct > -0.005:
            return SSOAction(diagnosis=f"SSO-LOSS:no(pnl={pos.pnl_pct*100:.2f}%>-0.5%)")
        if not pos.rescue_signal or pos.rescue_signal_strength is None:
            return SSOAction(diagnosis="SSO-LOSS:no(signal_missing)")
        _sig = str(pos.rescue_signal).strip().upper()
        _str = float(pos.rescue_signal_strength)
        _is_against = (
            (pos.side.upper() in ("LONG", "BUY") and _sig == "SELL")
            or (pos.side.upper() in ("SHORT", "SELL") and _sig == "BUY")
        )
        if not _is_against or _str < 0.55:
            return SSOAction(diagnosis=f"SSO-LOSS:no(signal={_sig} str={_str:.2f})")
        return SSOAction(
            should_close=True,
            close_pct=1.0,
            reason="sso_loss_cut",
            detail=(
                f"SSO-LOSS: pnl={pos.pnl_pct*100:.2f}% signal={_sig} str={_str:.2f} "
                f"vel={(pos.pnl_velocity_1h or 0)*100:.3f}%/h"
            ),
            trigger="SSO-LOSS",
            priority=10,
        )

    # ── SSO-6: Bounce Rescue Mode — close trapped bounce entries near breakeven ──
    def _check_sso6_bounce_rescue(self, pos: SSOPositionData) -> SSOAction:
        """Close 100% when a bounce-trapped position approaches breakeven.
        Activates when peak_pnl is very low after min_hours OR bounce_entry flag set.
        Target: close as soon as pnl_usdt >= rescue_target_pnl (default -$0.50).
        Smart Gate: blocks rescue when position is already recovering (signal/EV/velocity).
        Applies to both FUTURES and SPOT trade types."""
        _enabled = self.bounce_rescue_enabled
        if pos.trade_type == 'spot':
            _enabled = self._cfg('spot_bounce_rescue_enabled', True)
        if not _enabled:
            return SSOAction(diagnosis="SSO-6:disabled")
        if not pos.opened_at:
            return SSOAction(diagnosis="SSO-6:skip(no opened_at)")

        age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600

        # Use per-type thresholds (canonical defaults)
        _max_peak = self.bounce_rescue_max_peak_pct
        _min_hours = self.bounce_rescue_min_hours
        _target_pnl = self.bounce_rescue_target_pnl
        if pos.trade_type == 'spot':
            _max_peak = self._cfg('spot_bounce_rescue_max_peak_pct', 0.8) / 100.0
            _min_hours = self._cfg('spot_bounce_rescue_min_hours', 8.0)
            _target_pnl = self._cfg('spot_bounce_rescue_target_pnl', -0.50)
        # s78-fix: tighter rescue thresholds only for RANGE_SCALP
        _is_range_scalp = str(pos.regime_mode or '').strip().upper() == 'RANGE_SCALP'
        if _is_range_scalp:
            _max_peak = min(_max_peak, 0.002)
            _min_hours = min(_min_hours, 2.0)
            _target_pnl = max(_target_pnl, -0.10)
        _min_hours *= self._profile_scale(pos, aggressive=1.12, balanced=1.0, defensive=0.90)
        _target_pnl *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.90)

        # Determine if this position qualifies as a bounce entry
        _inline_bounce = False
        _is_bounce = pos.bounce_entry
        if not _is_bounce:
            # Inline detection: peak too low after enough time OR position already losing
            _loss_trigger = _is_range_scalp and pos.pnl_pct < -0.001
            if age_hours >= _min_hours and (pos.peak_pnl_pct < _max_peak or _loss_trigger):
                _is_bounce = True
                _inline_bounce = True

        if not _is_bounce:
            return SSOAction(diagnosis=f"SSO-6:no(peak={pos.peak_pnl_pct*100:.2f}%>={_max_peak*100:.2f}% or age={age_hours:.1f}h<{_min_hours:.1f}h)")

        # Bounce confirmed — check if we're near enough to breakeven to close
        if pos.pnl_usdt < _target_pnl:
            return SSOAction(
                diagnosis=(f"SSO-6:wait(bounce=True,pnl=${pos.pnl_usdt:.2f}<${_target_pnl:.2f},"
                           f"age={age_hours:.1f}h)")
            )

        # s78-fix: inline-detected bounces that are already in profit should not be rescued;
        # let normal SSO/stagnation logic handle them. Bounce-flagged entries still run the smart gate.
        if _inline_bounce and pos.pnl_usdt > 0.05:
            return SSOAction(
                diagnosis=(f"SSO-6:skip(inline_bounce,pnl=${pos.pnl_usdt:.2f}>0.05 profit, not near breakeven)")
            )

        # ── SMART GATE: block rescue if position is already recovering ──────
        # AND-partial logic:
        #   G1_strong alone  OR  COUNT(G2,G3,G4) >= rescue_gate_min_concurrent
        # Expiry: force rescue after N blocks without sufficient pnl improvement.
        _gate_enabled = self._cfg('rescue_smart_gate_enabled', True)
        _gate_blocked = False
        _gate_reasons = []
        _gate_diag = []  # full diagnosis including non-blocking gates

        if _gate_enabled:
            _is_spot = pos.trade_type == 'spot'
            _min_concurrent = int(self._cfg('rescue_gate_min_concurrent', 2))
            _g1_min = self._cfg('rescue_gate_g1_spot_min_pct', 0.003) if _is_spot else self._cfg('rescue_gate_g1_min_pct', 0.005)
            _str_thresh = self._cfg('rescue_stay_strength_min', 0.50)
            _vol_min = self._cfg('rescue_gate_vol_min_spike', 1.5)
            _ev_mult = self._cfg('rescue_stay_ev_multiplier', 3.0)
            _vel_thresh = self._cfg('rescue_velocity_min_pct', 0.003)
            _max_blocks = int(self._cfg('rescue_gate_max_blocks', 3))
            _min_improvement = self._cfg('rescue_gate_min_pnl_improvement', 0.005)

            # ── Gate 1 (G1): position strongly in profit ──────────────────────
            # Blocks ALONE only if pnl_pct >= threshold (avoids single-tick false positives)
            _g1_active = pos.pnl_pct >= _g1_min
            if _g1_active:
                _gate_reasons.append(f"pnl_positive({pos.pnl_pct*100:.2f}%>={_g1_min*100:.1f}%)")
                _gate_blocked = True
                _gate_diag.append(f"G1=BLOCK({pos.pnl_pct*100:.2f}%)")
            else:
                _gate_diag.append(f"G1=pass({pos.pnl_pct*100:.3f}%<{_g1_min*100:.1f}%)")

            if not _gate_blocked:
                # ── Gate 2 (G2): favorable signal + vol confirmation ──────────
                _sig = pos.rescue_signal or ""
                _sig_str = pos.rescue_signal_strength or 0.0
                _is_favorable = (
                    (pos.side in ("SHORT", "SELL") and _sig == "SELL") or
                    (pos.side in ("LONG", "BUY") and _sig == "BUY")
                )
                _vol_ok = (pos.vol_spike is not None and pos.vol_spike >= _vol_min)
                _g2_active = _is_favorable and _sig_str >= _str_thresh and _vol_ok
                if _g2_active:
                    _gate_diag.append(f"G2=YES({_sig} str={_sig_str:.2f} vol={pos.vol_spike:.1f})")
                else:
                    _gate_diag.append(f"G2=no(fav={_is_favorable} str={_sig_str:.2f}<{_str_thresh} vol={pos.vol_spike})")

                # ── Gate 3 (G3): prediction EV substantially positive ─────────
                _ev = pos.rescue_prediction_ev
                _ev_thresh = abs(_target_pnl) * _ev_mult
                _g3_active = _ev is not None and _ev > _ev_thresh
                if _g3_active:
                    _gate_diag.append(f"G3=YES(ev=${_ev:.2f}>{_ev_thresh:.2f})")
                else:
                    _ev_str = f"${_ev:.2f}" if _ev is not None else "None"
                    _gate_diag.append(f"G3=no(ev={_ev_str}<={_ev_thresh:.2f})")

                # ── Gate 4 (G4): sustained pnl velocity — NEVER blocks alone ──
                _vel = pos.pnl_velocity_1h or 0.0
                _g4_active = _vel > _vel_thresh and pos.pnl_pct > pos.peak_pnl_pct * 0.5
                if _g4_active:
                    _gate_diag.append(f"G4=YES(vel={_vel*100:.3f}%/h)")
                else:
                    _gate_diag.append(f"G4=no(vel={_vel*100:.3f}%/h)")

                # ── AND-partial: count concurrent gates {G2,G3,G4} ───────────
                _concurrent = sum([_g2_active, _g3_active, _g4_active])
                if _concurrent >= _min_concurrent:
                    _active = [n for n, a in [("G2", _g2_active), ("G3", _g3_active), ("G4", _g4_active)] if a]
                    _gate_reasons.append(f"signal_favorable({'+'.join(_active)} concurrent={_concurrent}/{_min_concurrent})")
                    _gate_blocked = True

            # ── Expiry check: force rescue after max_blocks without improvement ─
            if _gate_blocked and _max_blocks > 0:
                _block_count = getattr(pos, '_gate_block_count', None)
                _last_pnl = getattr(pos, '_gate_last_pnl_pct', None)
                if _block_count is None:
                    # first block — expiry not triggered yet
                    pass
                elif _block_count >= _max_blocks:
                    # check if pnl has improved enough since first block
                    _improvement = pos.pnl_pct - (_last_pnl or pos.pnl_pct)
                    if _improvement < _min_improvement:
                        # EXPIRY: override the block and force rescue
                        _gate_blocked = False
                        _gate_reasons = [f"gate_expired(blocks={_block_count}>={_max_blocks},improvement={_improvement*100:.3f}%<{_min_improvement*100:.1f}%)"]
                        _gate_diag.append(f"EXPIRY=FORCE_RESCUE(blocks={_block_count})")

        if _gate_blocked and _gate_reasons:
            return SSOAction(
                diagnosis=(f"SSO-6:GATE_BLOCKED({', '.join(_gate_reasons)}) — "
                           f"pnl=${pos.pnl_usdt:.2f} age={age_hours:.1f}h | {' '.join(_gate_diag)}")
            )

        # No gate blocked — execute rescue
        return SSOAction(
            should_close=True, close_pct=1.0,
            reason="sso_bounce_rescue",
            detail=(f"Bounce rescue: pnl=${pos.pnl_usdt:.2f}>=${_target_pnl:.2f} "
                    f"gate=clear (peak={pos.peak_pnl_pct*100:.2f}%, age={age_hours:.1f}h)"),
            trigger="SSO-6", priority=0,
        )

    # ── SSO-7: Stagnation Trailing + Opportunity Collapse ────────────────────────
    def _check_sso7_stagnation_exit(self, pos: SSOPositionData) -> SSOAction:
        """Squeeze the SL upward on stagnant positions; collapse it when a better
        candidate is waiting.  Never issues a direct close — instead signals the
        caller via detail='sso7_stagnation_trail:...' or 'sso7_opportunity_collapse:...'
        so run_trades.py can call update_position_sl().

        Guards (any fails → skip):
          - sso7_enabled=False
          - pnl_usdt < 0  (SL handles losses)
          - pnl_usdt >= sso7_max_pnl_usdt  (position graduated — trailing handles it)
          - age < sso7_min_age_hours  (anti-flip)
          - vol_spike >= sso7_vol_spike_breakout_min  (active breakout — leave alone)

        Stagnation detection:
          - pnl_velocity_1h <= sso7_min_velocity → stagnation_cycles++
          - else → reset cycles (position is advancing)

        Opportunity Collapse (fires first if candidate passes all filters):
          - stagnation_cycles >= sso7_opp_min_cycles_stagnant
          - best_candidate_strength >= sso7_opp_min_strength
          - best_candidate_vol_spike >= sso7_opp_min_vol_spike
          - best_candidate_prob >= current_pred_ev_proxy + sso7_opp_prob_min_delta
          - best_candidate_pred_ev >= effective_current_ev * sso7_opp_ev_min_ratio
          → Sets SL to current_price × (1 - commission_margin)

        Stagnation Trailing (fires when stagnation_cycles >= threshold):
          - Computes trail_dist = base_dist × max(min_dist, 1 - squeeze_per_cycle×cycles)
          - new_sl = max(current_sl, current_price × (1 - trail_dist))
          - SL only moves up, never down, never below entry if breakeven done
        """
        if not self._cfg('sso7_enabled', True):
            return SSOAction(diagnosis="SSO-7:disabled")

        # ── Guard: severe losses → let hard SL / UG fast-cut handle it ─────────────
        if pos.pnl_pct < -0.005:
            return SSOAction(diagnosis=f"SSO-7:skip(pnl_pct={pos.pnl_pct*100:.2f}%<-0.5% — UG/SL handles)")

        # ── Guard: graduated positions (significant profit → trailing manages) ───
        _max_pnl = self._cfg('sso7_max_pnl_usdt', 3.0)
        if pos.pnl_usdt >= _max_pnl:
            return SSOAction(diagnosis=f"SSO-7:skip(pnl=${pos.pnl_usdt:.2f}>=${_max_pnl:.1f} — graduated)")

        # ── Guard: anti-flip (minimum age) ──────────────────────────────────────
        if not pos.opened_at:
            return SSOAction(diagnosis="SSO-7:skip(no opened_at)")
        age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
        _min_age = self._cfg('sso7_min_age_hours', 1.0)
        # s78-fix: faster stagnation trailing for RANGE_SCALP
        _min_age = self._cfg(f'sso7_min_age_hours_{str(pos.regime_mode or "").lower()}', _min_age)
        if str(pos.regime_mode or '').strip().upper() == 'RANGE_SCALP':
            _min_age = min(_min_age, 0.5)
        if age_hours < _min_age:
            return SSOAction(diagnosis=f"SSO-7:skip(age={age_hours:.2f}h<{_min_age:.1f}h — anti-flip)")

        # ── Guard: active breakout → leave alone ────────────────────────────────
        _cur_vol = pos.vol_spike or 0.0
        _breakout_min = self._cfg('sso7_vol_spike_breakout_min', 2.0)
        if _cur_vol >= _breakout_min:
            return SSOAction(diagnosis=f"SSO-7:skip(vol_spike={_cur_vol:.2f}x >= {_breakout_min:.1f}x — breakout activo)")

        # ── Stagnation detection ─────────────────────────────────────────────────
        _min_vel = self._cfg('sso7_min_velocity', 0.0003)  # ratio/h
        _vel = pos.pnl_velocity_1h or 0.0
        _is_advancing = _vel > _min_vel

        # Retrieve stagnation state from position metadata (passed via sso7_stagnation_cycles)
        _cycles = int(pos.sso7_stagnation_cycles or 0)
        _last_adv_pnl = pos.sso7_last_advance_pnl  # None if never recorded

        if _is_advancing:
            # Position is growing — reset and signal caller to persist reset
            return SSOAction(
                should_close=False,
                diagnosis=f"SSO-7:advancing(vel={_vel*100:.4f}%/h>min — reset cycles)",
                detail="sso7_advance_reset",
                reason="sso7_advance_reset",
            )

        # Position is stagnant this cycle
        _new_cycles = _cycles + 1
        _threshold = self._cfg('sso7_stagnation_cycles_threshold', 3)

        if _new_cycles < _threshold:
            return SSOAction(
                should_close=False,
                diagnosis=f"SSO-7:stagnant(cycles={_new_cycles}/{_threshold} — building)",
                detail=f"sso7_stagnation_count:{_new_cycles}",
                reason="sso7_stagnation_count",
            )

        # ── Stagnation confirmed (cycles >= threshold) ───────────────────────────

        _cur_price = pos.current_price
        _cur_sl = pos.current_sl_price or 0.0
        _entry = pos.entry_price or 0.0
        _commission_margin = 0.002  # 0.2% — 2× round-trip Binance spot commission

        # ── Opportunity Collapse check ───────────────────────────────────────────
        if self._cfg('sso7_opp_enabled', True):
            _opp_min_cycles = self._cfg('sso7_opp_min_cycles_stagnant', 3)
            if _new_cycles >= _opp_min_cycles:
                _cand_str = pos.best_candidate_strength or 0.0
                _cand_vol = pos.best_candidate_vol_spike or 0.0
                _cand_prob = pos.best_candidate_prob or 0.0
                _cand_ev = pos.best_candidate_pred_ev or 0.0
                _self_ev = pos.current_pred_ev or 0.0

                _min_str = self._cfg('sso7_opp_min_strength', 0.55)
                _min_vol = self._cfg('sso7_opp_min_vol_spike', 1.2)
                _prob_delta = self._cfg('sso7_opp_prob_min_delta', 0.06)
                _ev_ratio = self._cfg('sso7_opp_ev_min_ratio', 1.35)

                # Estimate current position's success probability from velocity proxy
                # Use 0.50 baseline if no pred EV available
                _self_prob_proxy = max(0.40, min(0.70, 0.50 + _vel * 10))

                _opp_ok = (
                    _cand_str >= _min_str
                    and _cand_vol >= _min_vol
                    and _cand_prob >= (_self_prob_proxy + _prob_delta)
                    and (_self_ev <= 0 or _cand_ev >= _self_ev * _ev_ratio)
                )

                if _opp_ok:
                    # Collapse SL to just below current price (≈ comisión de salida)
                    _collapse_sl = round(_cur_price * (1.0 - _commission_margin), 8)
                    # Never set SL below what it already is (only move up)
                    _collapse_sl = max(_collapse_sl, _cur_sl)
                    return SSOAction(
                        should_close=False,
                        reason="sso7_opportunity_collapse",
                        detail=(
                            f"sso7_opportunity_collapse:{_collapse_sl:.6f} "
                            f"cand_str={_cand_str:.2f} cand_vol={_cand_vol:.1f}x "
                            f"cand_prob={_cand_prob:.2f}(+{_cand_prob-_self_prob_proxy:.2f}) "
                            f"cand_ev={_cand_ev:.2f} self_ev={_self_ev:.2f} "
                            f"cycles={_new_cycles} pnl=${pos.pnl_usdt:.2f}"
                        ),
                        diagnosis=(
                            f"SSO-7:opp_collapse(SL→{_collapse_sl:.4f} "
                            f"cand_str={_cand_str:.2f}>={_min_str} "
                            f"vol={_cand_vol:.1f}x>={_min_vol})"
                        ),
                        trigger="SSO-7", priority=1,
                    )
                else:
                    # Log why collapse didn't fire
                    _opp_diag = (
                        f"SSO-7:opp_skip("
                        f"str={_cand_str:.2f}<{_min_str} " if _cand_str < _min_str else ""
                        f"vol={_cand_vol:.1f}x<{_min_vol} " if _cand_vol < _min_vol else ""
                        f"prob_delta={_cand_prob-_self_prob_proxy:+.2f}<{_prob_delta} " if _cand_prob < _self_prob_proxy + _prob_delta else ""
                        f"ev_ratio={(_cand_ev/_self_ev if _self_ev > 0 else 0):.2f}<{_ev_ratio}" if _self_ev > 0 and _cand_ev < _self_ev * _ev_ratio else ""
                        f")"
                    )
                    # Fall through to stagnation trailing

        # ── Stagnation Trailing ──────────────────────────────────────────────────
        _base_dist = self._cfg('sso7_trail_base_dist', 0.010)       # 1.0%
        _squeeze = self._cfg('sso7_trail_squeeze_per_cycle', 0.10)   # 10% tighter per cycle
        _min_dist = self._cfg('sso7_trail_min_dist', 0.003)          # 0.3% floor

        # Trail distance shrinks each stagnant cycle: 1.0% → 0.9% → 0.8% → ...
        _squeeze_factor = max(_min_dist / _base_dist, 1.0 - _squeeze * (_new_cycles - _threshold))
        _trail_dist = max(_min_dist, _base_dist * _squeeze_factor)

        _new_sl = _cur_price * (1.0 - _trail_dist)

        # SL only moves up, never below entry if breakeven already done
        _floor = _entry if _cur_sl >= _entry else 0.0
        _new_sl = max(_new_sl, _cur_sl, _floor)
        _new_sl = round(_new_sl, 8)

        if _new_sl <= _cur_sl:
            return SSOAction(
                should_close=False,
                diagnosis=f"SSO-7:trail_nochange(sl={_cur_sl:.4f} already>={_new_sl:.4f} cycles={_new_cycles})",
                detail=f"sso7_stagnation_count:{_new_cycles}",
                reason="sso7_stagnation_count",
            )

        return SSOAction(
            should_close=False,
            reason="sso7_stagnation_trail",
            detail=(
                f"sso7_stagnation_trail:{_new_sl:.6f} "
                f"dist={_trail_dist*100:.2f}% cycles={_new_cycles} "
                f"pnl=${pos.pnl_usdt:.2f} vel={_vel*100:.4f}%/h"
            ),
            diagnosis=(
                f"SSO-7:trail(SL {_cur_sl:.4f}→{_new_sl:.4f} "
                f"dist={_trail_dist*100:.2f}% cycles={_new_cycles})"
            ),
            trigger="SSO-7", priority=1,
        )

    # ── SSO-1: Capital Velocity — decay / slow / stagnant ───────────────────────
    def _check_sso1_stagnant(self, pos: SSOPositionData) -> Optional[SSOAction]:
        """Close 100% if position is decaying, crawling too slowly, or fully stagnant.
        Three sub-checks with independent gates (SSO-1b, SSO-1c, SSO-1).
        Always returns an SSOAction with diagnosis even on no-fire."""
        if not pos.opened_at:
            return SSOAction(diagnosis="SSO-1:skip(no opened_at)")
        age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
        _lifetime_vel = (pos.pnl_pct / age_hours) if age_hours > 0 else 0.0
        _snap_vel = pos.pnl_velocity_1h
        _regime = str(pos.regime_mode or '').strip().upper()
        _profile = str(pos.profile_label or '').strip().upper()

        # ── SSO-1b: Profit Decay — giving back gains ──────────────────────────
        _decay_threshold = self._cfg('sso_decay_threshold_pct', 0.004)
        _decay_min_profit = self._dyn_decay_min_profit if self._dyn_decay_min_profit else self._cfg('sso_decay_min_profit_usd', 2.0)
        # s70: read per-regime field (absolute value), fall back to global. Globals NEVER modified.
        _decay_regime_field = f'sso_decay_min_hours_{_regime.lower()}' if _regime else None
        _decay_min_hours = (
            float(self._cfg(_decay_regime_field, None))
            if (_decay_regime_field and self._cfg(_decay_regime_field, None) is not None)
            else self._cfg('sso_decay_min_hours', 1.5)
        )
        _decay_min_hours *= self._profile_scale(pos, aggressive=1.12, balanced=1.0, defensive=0.90)
        _decay_min_profit *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.92)
        decay_pct = pos.peak_pnl_pct - pos.pnl_pct
        _1b_reasons = []
        if decay_pct < _decay_threshold:
            _1b_reasons.append(f"decay={decay_pct*100:.2f}%<{_decay_threshold*100:.1f}%")
        if pos.pnl_usdt < _decay_min_profit:
            _1b_reasons.append(f"${pos.pnl_usdt:.1f}<${_decay_min_profit:.0f}")
        if age_hours < _decay_min_hours:
            _1b_reasons.append(f"age={age_hours:.1f}h<{_decay_min_hours:.1f}h")
        if not _1b_reasons:  # all conditions met → FIRE
            return SSOAction(
                should_close=True, close_pct=1.0,
                reason="sso_profit_decay",
                detail=f"Decay: peak={pos.peak_pnl_pct*100:.2f}% → {pos.pnl_pct*100:.2f}% "
                       f"(gave back {decay_pct*100:.2f}%, ${pos.pnl_usdt:.2f} remaining, "
                       f"age={age_hours:.1f}h)",
                trigger="SSO-1", priority=1,
            )
        _1b_diag = f"1b:no({','.join(_1b_reasons)})"

        # ── SSO-1c: Slow Crawl — lifetime + snapshot velocity ─────────────────
        _slow_vel_base = self._cfg('sso_slow_velocity_threshold', 0.001)  # 0.1%/h
        _slow_min_profit = self._dyn_slow_min_profit if self._dyn_slow_min_profit else self._cfg('sso_slow_min_profit_usd', 1.0)
        # s70: read per-regime slow_min_hours field, fall back to global
        _slow_regime_field = f'sso_slow_min_hours_{_regime.lower()}' if _regime else None
        _slow_min_hours = (
            float(self._cfg(_slow_regime_field, None))
            if (_slow_regime_field and self._cfg(_slow_regime_field, None) is not None)
            else self._cfg('sso_slow_min_hours', 2.0)
        )
        _slow_t2 = self._cfg('sso_slow_tier2_hours', 4.0)
        _slow_t3 = self._cfg('sso_slow_tier3_hours', 6.0)
        _slow_t4 = self._cfg('sso_slow_tier4_hours', 8.0)
        # W4: FUTURES_SHORT / futures trade_type needs tighter patience on slow crawl
        _is_futures = str(pos.trade_type or '').strip().lower() == 'futures'
        # s70: regime adjustments to vel_base and profit floor — regime time is now in per-regime field above
        if _is_futures or _regime == 'FUTURES_SHORT':
            _slow_vel_base *= 1.40    # require higher velocity to stay (40% stricter)
            _slow_min_profit *= 0.85  # lower profit floor (leveraged = smaller absolute gains)
        elif _regime == 'SPOT_BULL':
            _slow_vel_base *= 0.6
        elif _regime == 'RANGE_SCALP':
            _slow_vel_base *= 1.2
            _slow_min_profit *= 0.8
        elif _regime == 'PRESERVATION':
            _slow_vel_base *= 0.8
        _slow_vel_base *= self._profile_scale(pos, aggressive=0.90, balanced=1.0, defensive=1.10)
        _slow_min_hours *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.92)

        _1c_reasons = []
        if pos.pnl_usdt < _slow_min_profit:
            _1c_reasons.append(f"${pos.pnl_usdt:.1f}<${_slow_min_profit:.0f}")
        if age_hours < _slow_min_hours:
            _1c_reasons.append(f"age={age_hours:.1f}h<{_slow_min_hours:.1f}h")

        if not _1c_reasons:
            # Determine tier threshold
            if age_hours >= _slow_t4:
                _effective_vel = _slow_vel_base * 5.0    # 0.50%/h
                _tier = "T4"
            elif age_hours >= _slow_t3:
                _effective_vel = _slow_vel_base * 3.5    # 0.35%/h
                _tier = "T3"
            elif age_hours >= _slow_t2:
                _effective_vel = _slow_vel_base * 1.5    # 0.15%/h
                _tier = "T2"
            else:
                _effective_vel = _slow_vel_base           # 0.10%/h
                _tier = "T1"

            # Use worst of snapshot and lifetime velocity
            _worst_vel = min(_snap_vel if _snap_vel is not None else _lifetime_vel, _lifetime_vel)
            if _worst_vel <= _effective_vel:
                return SSOAction(
                    should_close=True, close_pct=1.0,
                    reason="sso_slow_crawl",
                    detail=f"Slow {_tier}: life={_lifetime_vel*100:.3f}%/h snap={(_snap_vel or 0)*100:.3f}%/h "
                           f"worst={_worst_vel*100:.3f}%/h<={_effective_vel*100:.2f}%/h "
                           f"(${pos.pnl_usdt:.2f}, age={age_hours:.1f}h)",
                    trigger="SSO-1", priority=1,
                )
            _1c_reasons.append(f"worst={_worst_vel*100:.2f}%/h>{_effective_vel*100:.2f}%/h({_tier})")
        _1c_diag = f"1c:no({','.join(_1c_reasons)})"

        # ── SSO-1: Original Stagnant — velocity zero or negative ──────────────
        min_profit = max(self.stagnant_min_profit_floor,
                         pos.position_size_usdt * self.stagnant_min_profit_factor)
        # s70: read per-regime stagnant_min_hours field, fall back to self.stagnant_min_hours (global — NEVER modified)
        _stagnant_regime_field = f'sso_stagnant_min_hours_{_regime.lower()}' if _regime else None
        _stagnant_min_hours = (
            float(self._cfg(_stagnant_regime_field, None))
            if (_stagnant_regime_field and self._cfg(_stagnant_regime_field, None) is not None)
            else self.stagnant_min_hours
        )
        min_profit *= self._profile_scale(pos, aggressive=1.08, balanced=1.0, defensive=0.92)
        # s73 C1: _stagnant_min_hours already has profile multiplier from harmony (_apply_mode_profile_harmony).
        # Do NOT apply _profile_scale again — that was double-multiplying (e.g. SPOT_BULL Profile A: 4.05h×1.08=4.37h).
        _1_reasons = []
        if pos.pnl_usdt < min_profit:
            _1_reasons.append(f"${pos.pnl_usdt:.1f}<${min_profit:.0f}")
        if age_hours < _stagnant_min_hours:
            _1_reasons.append(f"age={age_hours:.1f}h<{_stagnant_min_hours:.1f}h")
        if _snap_vel is None:
            _1_reasons.append("vel=None")
        elif _snap_vel > 0.001:
            _1_reasons.append(f"vel={_snap_vel*100:.3f}%/h>0.1%")

        if not _1_reasons:
            return SSOAction(
                should_close=True, close_pct=1.0,
                reason="sso_stagnant_profit",
                detail=f"${pos.pnl_usdt:.2f} stagnant {age_hours:.1f}h (vel={(_snap_vel or 0)*100:.3f}%/h)",
                trigger="SSO-1", priority=1,
            )
        _1_diag = f"1:no({','.join(_1_reasons)})"

        # ── SSO-1d: Flat Stagnation Timeout ──────────────────────────────────
        # Closes SPOT positions in RANGE_SCALP that have remained flat for too long,
        # independent of the wallet-percentage profit floors that block small positions.
        _1d_diag = "1d:no"
        _flat_enabled = self._cfg('sso_flat_stagnation_timeout_enabled', True)
        if _flat_enabled and pos.trade_type == 'spot' and _regime == 'RANGE_SCALP':
            _flat_hours_field = f'sso_flat_stagnation_timeout_hours_{_regime.lower()}' if _regime else None
            _flat_hours = (
                float(self._cfg(_flat_hours_field, None))
                if (_flat_hours_field and self._cfg(_flat_hours_field, None) is not None)
                else self._cfg('sso_flat_stagnation_timeout_hours', 3.0)
            )
            _flat_pnl_field = f'sso_flat_stagnation_timeout_pnl_pct_{_regime.lower()}' if _regime else None
            _flat_pnl_pct = (
                float(self._cfg(_flat_pnl_field, None))
                if (_flat_pnl_field and self._cfg(_flat_pnl_field, None) is not None)
                else self._cfg('sso_flat_stagnation_timeout_pnl_pct', 0.003)
            )
            _flat_pnl_pct = abs(_flat_pnl_pct)
            if age_hours >= _flat_hours and abs(pos.pnl_pct) < _flat_pnl_pct:
                return SSOAction(
                    should_close=True, close_pct=1.0,
                    reason="sso_flat_stagnation_timeout",
                    detail=(f"Flat stagnation timeout: |pnl|={abs(pos.pnl_pct)*100:.2f}% "
                            f"<{_flat_pnl_pct*100:.2f}%, age={age_hours:.1f}h >= {_flat_hours:.1f}h"),
                    trigger="SSO-1", priority=1,
                )
            _1d_diag = f"1d:no(age={age_hours:.1f}h<{_flat_hours:.1f}h or |pnl|>={_flat_pnl_pct*100:.2f}%)"

        return SSOAction(
            diagnosis=f"SSO-1:[{_1b_diag} {_1c_diag} {_1_diag} {_1d_diag}] "
                      f"pk={pos.peak_pnl_pct*100:.2f}% cur={pos.pnl_pct*100:.2f}% "
                      f"life={_lifetime_vel*100:.3f}%/h snap={(_snap_vel or 0)*100:.3f}%/h "
                      f"${pos.pnl_usdt:.2f} {age_hours:.1f}h"
        )

    # ── SSO-2: Momentum Exhausted ─────────────────────────────────────────────
    def _check_sso2_momentum_exhausted(self, pos: SSOPositionData) -> Optional[SSOAction]:
        """Close 75% if RSI exhausted + volume dying."""
        # Min PnL gate
        min_pnl = self.momentum_min_pnl_futures if pos.trade_type == "futures" else self.momentum_min_pnl_spot
        _regime = str(pos.regime_mode or '').strip().upper()
        if _regime == 'FUTURES_SHORT' and pos.trade_type == 'futures':
            min_pnl *= 0.9
        elif _regime == 'RANGE_SCALP' and pos.trade_type != 'futures':
            min_pnl *= 0.8
        min_pnl *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.90)
        if pos.pnl_pct < min_pnl:
            return None

        # W5: Return diagnostic instead of silent None when indicators missing
        if pos.rsi_1h is None or pos.vol_spike is None:
            _missing = []
            if pos.rsi_1h is None: _missing.append("rsi_1h=None")
            if pos.vol_spike is None: _missing.append("vol_spike=None")
            return SSOAction(diagnosis=f"SSO-2:skip({','.join(_missing)})")

        # RSI exhaustion check (direction-aware)
        # W5: SHORT uses separate configurable oversold threshold (default same as momentum_rsi_oversold)
        _is_long = pos.side.upper() in ("LONG", "BUY")
        _rsi_ob_short = self._cfg('sso_momentum_rsi_oversold_short', self.momentum_rsi_oversold)
        rsi_exhausted = False
        rsi_diag = f"RSI={pos.rsi_1h:.0f}"
        if _is_long and pos.rsi_1h >= self.momentum_rsi_overbought:
            rsi_exhausted = True
            rsi_diag += f">={self.momentum_rsi_overbought:.0f}(ob)"
        elif not _is_long and pos.rsi_1h <= _rsi_ob_short:
            rsi_exhausted = True
            rsi_diag += f"<={_rsi_ob_short:.0f}(os)"

        if not rsi_exhausted:
            return SSOAction(diagnosis=f"SSO-2:no({rsi_diag} not exhausted, vol={pos.vol_spike:.2f}x)")

        # Volume dying
        if pos.vol_spike > self.momentum_vol_dying:
            return SSOAction(diagnosis=f"SSO-2:no({rsi_diag} exhausted but vol={pos.vol_spike:.2f}x>{self.momentum_vol_dying:.2f}x still active)")

        return SSOAction(
            should_close=True,
            close_pct=0.75,
            reason="sso_momentum_exhausted",
            detail=f"{rsi_diag} vol={pos.vol_spike:.2f}x (exhausted at +{pos.pnl_pct*100:.1f}%)",
            trigger="SSO-2",
            priority=2,
        )

    # ── SSO-3: Multi-level ATR Scale ──────────────────────────────────────────
    def _check_sso3_multilevel(self, pos: SSOPositionData) -> Optional[SSOAction]:
        """Close incrementally at ATR-based levels."""
        if not self.multilevel_enabled:
            return None
        if pos.atr_4h_pct is None or pos.atr_4h_pct <= 0:
            return None

        atr = pos.atr_4h_pct  # e.g., 0.008 = 0.8%
        levels_done = set(pos.sso_levels_done or [])
        _atr_gate_mult = self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.90)
        _close_mult = self._profile_scale(pos, aggressive=0.85, balanced=1.0, defensive=1.10)

        # Check levels in order (highest first for best close %)
        if 3 not in levels_done and pos.pnl_pct >= atr * self.ml_l3_mult * _atr_gate_mult:
            return SSOAction(
                should_close=True,
                close_pct=min(1.0, self.ml_l3_close * _close_mult),
                reason="sso_multilevel_l3",
                detail=f"Level 3: pnl={pos.pnl_pct*100:.2f}% >= {atr*self.ml_l3_mult*_atr_gate_mult*100:.2f}% (3.5×ATR)",
                trigger="SSO-3",
                priority=3,
            )
        if 2 not in levels_done and pos.pnl_pct >= atr * self.ml_l2_mult * _atr_gate_mult:
            return SSOAction(
                should_close=True,
                close_pct=min(1.0, self.ml_l2_close * _close_mult),
                reason="sso_multilevel_l2",
                detail=f"Level 2: pnl={pos.pnl_pct*100:.2f}% >= {atr*self.ml_l2_mult*_atr_gate_mult*100:.2f}% (2×ATR)",
                trigger="SSO-3",
                priority=3,
            )
        if 1 not in levels_done and pos.pnl_pct >= atr * self.ml_l1_mult * _atr_gate_mult:
            return SSOAction(
                should_close=True,
                close_pct=min(1.0, self.ml_l1_close * _close_mult),
                reason="sso_multilevel_l1",
                detail=f"Level 1: pnl={pos.pnl_pct*100:.2f}% >= {atr*self.ml_l1_mult*_atr_gate_mult*100:.2f}% (1×ATR)",
                trigger="SSO-3",
                priority=3,
            )
        return None

    # ── SSO-4: Target Contribution ────────────────────────────────────────────
    def _check_sso4_target_contribution(self, pos: SSOPositionData) -> Optional[SSOAction]:
        """Close 100% if profit contributes meaningfully toward daily target."""
        target = pos.daily_target or self.target_daily_usd
        _target_contribution_pct = self.target_contribution_pct
        _target_min_hours = self.target_min_hours
        _regime = str(pos.regime_mode or '').strip().upper()
        _profile = str(pos.profile_label or '').strip().upper()
        if _regime == 'RANGE_SCALP':
            target = min(target, self.target_daily_usd * 0.5)
            # s73 C2: per-regime contribution floor — RANGE_SCALP TP=1.8% can't hit 15% of $40 daily target
            # s110: raised 0.05->0.10 — at 0.05, min_contribution was only ~$1-8 (e.g. $20
            # target x 0.05 = $1.00), letting SSO-4 fully close winners at a fraction of the
            # $18-27 USD loss guardrail (avg win $6.84 vs avg loss $17.82 over 48h, RR~0.38).
            # 0.10 still stays under the global default (0.15) but roughly doubles the floor.
            _rs_contrib = self._cfg('sso_target_contribution_pct_range_scalp', 0.10)
            _target_contribution_pct = _rs_contrib
            _target_min_hours *= 0.8
        elif _regime == 'PRESERVATION':
            target = min(target, self.target_daily_usd * 0.4)
            _target_contribution_pct *= 0.9
        elif _regime == 'SPOT_BULL':
            _target_contribution_pct *= 1.1
            _target_min_hours *= 1.15
        _target_contribution_pct *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.90)
        _target_min_hours *= self._profile_scale(pos, aggressive=1.10, balanced=1.0, defensive=0.90)
        min_contribution = target * _target_contribution_pct

        if pos.pnl_usdt < min_contribution:
            return None

        # Time gate
        if pos.opened_at:
            age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
            if age_hours < _target_min_hours:
                return None

        # W6: Capital rotation logic — differentiate "close to target" from "far exceeded"
        if pos.today_realized_pnl >= target * 0.85:
            # Excellent day: only keep position if it still has active momentum
            if pos.today_realized_pnl >= target * 1.5:
                # Day far exceeded target → rotate capital: close if position is stagnant
                _age_h = 0.0
                if pos.opened_at:
                    _age_h = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
                _snap_vel = pos.pnl_velocity_1h
                _life_vel = (pos.pnl_pct / _age_h) if _age_h > 0 else 0.0
                _worst_vel = min(_snap_vel if _snap_vel is not None else _life_vel, _life_vel)
                _rotation_vel_threshold = self._cfg('sso_target_rotation_vel_threshold', 0.001)
                if _worst_vel <= _rotation_vel_threshold:
                    return SSOAction(
                        should_close=True,
                        close_pct=1.0,
                        reason="sso_capital_rotation",
                        detail=(
                            f"Day exceeded {pos.today_realized_pnl:.2f}/${target:.0f} (>{target*1.5:.0f}) "
                            f"— stagnant pos vel={_worst_vel*100:.3f}%/h <= {_rotation_vel_threshold*100:.2f}%/h"
                        ),
                        trigger="SSO-4",
                        priority=4,
                    )
            # 85-150% of target: more patience, let momentum continue
            return None

        # Urgency: if late in the day and far from target, be aggressive
        now_utc = datetime.now(timezone.utc)
        hour_utc = now_utc.hour
        # Mexico time ≈ UTC-6, so 18:00 MX = 00:00 UTC
        # Be more aggressive after 22:00 UTC (16:00 MX) if behind target
        _behind_target = pos.today_realized_pnl < target * 0.5
        _late_day = hour_utc >= 22 or hour_utc < 4  # 16:00-22:00 MX
        if not _late_day and not _behind_target:
            # Standard hours + on track → only trigger if profit is really significant
            if pos.pnl_usdt < min_contribution * 1.5:
                return None

        return SSOAction(
            should_close=True,
            close_pct=1.0,
            reason="sso_target_contribution",
            detail=f"${pos.pnl_usdt:.2f} toward target (today=${pos.today_realized_pnl:.2f}/${target:.0f})",
            trigger="SSO-4",
            priority=4,
        )

    # ── SSO-5: Re-entry Upgrade ───────────────────────────────────────────────
    def _check_sso5_reentry_upgrade(self, pos: SSOPositionData) -> Optional[SSOAction]:
        """Close if better opportunity available and slots are full."""
        if not pos.all_slots_full:
            return None
        if pos.pnl_usdt <= 0:
            return None  # only close if in profit
        if pos.current_strength >= 0.5:
            return None  # current position is still decent

        # Check if there's a significantly better signal
        strength_diff = pos.best_available_ev - pos.current_strength
        strength_threshold = self.reentry_min_strength_diff * self._profile_scale(pos, aggressive=1.12, balanced=1.0, defensive=0.88)
        if strength_diff < strength_threshold:
            return None

        return SSOAction(
            should_close=True,
            close_pct=1.0,
            reason="sso_reentry_upgrade",
            detail=f"Upgrade: current={pos.current_strength:.2f} → available={pos.best_available_ev:.2f} (diff={strength_diff:.2f})",
            trigger="SSO-5",
            priority=5,
        )


# ── Velocity tracking helper ──────────────────────────────────────────────────
def compute_pnl_velocity(current_pnl_pct: float, last_snapshot: Optional[Dict],
                         window_minutes: int = 60) -> tuple:
    """
    Compute PnL velocity (change per hour) and return updated snapshot.

    Returns:
        (velocity_per_hour: float or None, new_snapshot: dict)
    """
    now = datetime.now(timezone.utc)
    new_snapshot = {"ts": now.isoformat(), "pnl_pct": current_pnl_pct}

    if not last_snapshot:
        return None, new_snapshot

    try:
        last_ts = datetime.fromisoformat(last_snapshot["ts"].replace('Z', '+00:00'))
        last_pnl = float(last_snapshot["pnl_pct"])
    except (KeyError, ValueError, TypeError):
        return None, new_snapshot

    elapsed_min = (now - last_ts).total_seconds() / 60
    if elapsed_min < 3:  # too soon to measure
        return None, last_snapshot  # keep old snapshot

    # If window elapsed, compute velocity and reset snapshot
    if elapsed_min >= window_minutes:
        velocity_per_hour = (current_pnl_pct - last_pnl) / (elapsed_min / 60)
        return velocity_per_hour, new_snapshot

    # Within window: compute but don't reset (interpolate)
    velocity_per_hour = (current_pnl_pct - last_pnl) / (elapsed_min / 60)
    return velocity_per_hour, last_snapshot


def extract_sso_level_from_reason(reason: str) -> Optional[int]:
    """Extract the level number from an SSO multilevel reason."""
    if reason == "sso_multilevel_l1":
        return 1
    elif reason == "sso_multilevel_l2":
        return 2
    elif reason == "sso_multilevel_l3":
        return 3
    return None
