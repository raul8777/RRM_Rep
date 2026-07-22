import json
import os
from typing import Any, Dict, List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_secrets_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(raw, dict):
        return

    for key, value in raw.items():
        if key and isinstance(value, (str, int, float, bool)):
            os.environ.setdefault(key, str(value))


def _resolve_env_file() -> str:
    env_path = os.environ.get("DOTENV_PATH", ".env")
    if env_path and os.path.exists(env_path):
        return env_path
    return ".env"


def _load_optional_secrets() -> None:
    secrets_path = os.environ.get("SECRETS_FILE_PATH")
    if secrets_path and os.path.exists(secrets_path):
        _load_secrets_file(secrets_path)


_load_optional_secrets()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_use_testnet: bool = Field(default=True)
    # Risk and operation controls
    max_daily_loss_usdt: float = Field(default=100.0)
    max_per_trade_usdt: float = Field(default=1500.0)
    max_positions: int = Field(default=4)
    max_positions_per_symbol: int = Field(default=3)
    dry_run: bool = Field(default=True)
    allow_real_trading: bool = Field(default=False)
    trade_cycle_minutes: int = Field(default=5)
    trade_horizon_minutes: int = Field(default=240)  # realistic hold time for PnL prediction (4h)
    # Kelly dynamic position sizing
    kelly_enabled: bool = Field(default=True)
    kelly_base_usdt: float = Field(default=150.0)  # concentrated: base digna para capital $5,100
    kelly_max_usdt: float = Field(default=1500.0)   # concentrated: cap por posición elevado
    kelly_max_pct_cap: float = Field(default=0.30)       # concentrated: ~$1,530/posición con $5,100
    kelly_max_pct_liquid: float = Field(default=0.85)  # concentrated: hasta 85% del líquido en convicción extrema
    kelly_min_usdt: float = Field(default=0.0)  # min absolute ticket per trade; 0 disables floor
    kelly_dynamic_base_pct: float = Field(default=0.20)  # concentrated: base crece con capital disponible
    # Bear market mode
    bear_market_mode_enabled: bool = Field(default=True)
    bear_market_btc_7d_threshold: float = Field(default=-10.0)  # % change
    bear_market_kelly_multiplier: float = Field(default=0.5)  # reduce Kelly
    bear_market_volspike_min: float = Field(default=2.0)  # stricter spike req
    # Bayesian evidence gate (superseded by entry_profile_gate; kept disabled)
    evidence_gate_enabled: bool = Field(default=False)
    evidence_gate_min_posterior: float = Field(default=0.65)
    evidence_gate_prior: float = Field(default=0.50)
    # Entry profile gate (A/B/C tiered selection based on calibrated historical outcomes)
    entry_profile_gate_enabled: bool = Field(default=True)
    entry_profile_gate_thresholds_path: str = Field(default="Dev/benchmarks/entry_profile_calibration/entry_profile_thresholds.json")
    # Calibrated thresholds from the shadow-log sweep (2026-07-18).
    evidence_gate_min_posterior_per_regime: Dict[str, float] = Field(default={
        "SPOT_BULL": 0.60,
        "FUTURES_SHORT": 0.80,
        "RANGE_SCALP": 0.52,
        "PRESERVATION": 0.80,
    })
    # SPOT_BULL PnL boost â€” relax predictor thresholds when market is in confirmed uptrend
    spot_bull_pnl_boost_enabled: bool = Field(default=True)
    spot_bull_pnl_boost_large_cap: float = Field(default=0.0)    # BTC/ETH: allow up to 0 PnL
    spot_bull_pnl_boost_midcap: float = Field(default=-0.10)        # alts: allow -0.10
    spot_bull_pnl_boost_prob_large_cap: float = Field(default=-0.02)  # relax min_prob for BTC/ETH/BNB in confirmed SPOT_BULL
    spot_bull_pnl_boost_prob_midcap: float = Field(default=-0.04)     # wider relaxation for midcaps in confirmed SPOT_BULL
    spot_bull_btc_4h_up_required: bool = Field(default=True)      # only apply when BTC 4h=UP
    spot_ignore_bnb_when_commission_enabled: bool = Field(default=True)  # keep BNB reserve out of spot parity when used for fees
    # Regime whipsaw protection â€” minimum consecutive PRESERVATION cycles before closing shorts
    min_cycles_before_close_shorts: int = Field(default=5)  # H-FIX: 3â†’5 evita cierre prematuro en oscilaciones de rÃ©gimen
    # Protect profitable shorts on regime change â€” only close if unrealized PnL < this threshold (USD)
    futures_protect_profit_usdt: float = Field(default=-5.0)  # H-FIX: 1.0â†’-5.0 â€” solo cierra en regime change si pÃ©rdida > $5
    # Aggressive exposure in SPOT_BULL + F&G Fear: use higher cap when conditions are optimal
    max_total_exposure_spot_bull_fear: float = Field(default=0.95)
    max_total_exposure_spot_bull_fear_fng_threshold: int = Field(default=40)
    # Futures: no short when BTC 4h candle just moved up strongly (avoid shorting into momentum)
    futures_no_short_btc_4h_threshold: float = Field(default=2.0)  # %
    # Futures: max large-cap SHORTs (BTC/ETH/BNB/SOL) â€” slots above this must be MIDs/NARRATIVE for diversification
    futures_max_largecap_shorts: int = Field(default=5)
    futures_largecap_symbols: List[str] = Field(default=['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'])
    # Unified Governance: if False, SSO partial + PRS close will NOT be escalated to a full close.
    # Instead UG keeps the largest requested partial close pct. This protects from killing a winner
    # when a profit-taking partial aligns with a temporary PRS close signal.
    ug_allow_partial_to_full_escalation: bool = Field(default=False)
    # Peak protection in SPOT_BULL: fraction of peak PnL that must be preserved.
    # Higher = more patient, lets winners run in bull markets (0.70 is more permissive than 0.60).
    peak_protection_spot_bull_pct: float = Field(default=0.65)  # concentrated: proteger más pico
    peak_protection_spot_bull_profile_a_pct: float = Field(default=0.70)  # concentrated: perfil A aún más protector
    # Scale-out on take-profit
    scale_out_enabled: bool = Field(default=True)
    scale_out_first_close_pct: float = Field(default=0.5)  # close 50% at TP
    # Smart TP: analyse momentum at TP before selling â€” extend if bullish, scale-out if neutral, close if bearish
    smart_tp_enabled: bool = Field(default=True)
    smart_tp_max_extensions: int = Field(default=3)       # max extensions per position
    smart_tp_max_pnl_pct: float = Field(default=15.0)        # never extend above this % gain
    smart_tp_extend_score_threshold: int = Field(default=2)  # min score/4 to EXTEND (was 3, now 2 = more aggressive)
    # Smart TP: regime-aware behaviour â€” disable EXTEND in flat/defensive regimes (maximize realized gains)
    smart_tp_extend_disabled_regimes: List[str] = Field(default=["RANGE_SCALP", "PRESERVATION"])
    # Futures Smart TP EXTEND: evaluate SHORT momentum at TP hit before closing
    futures_smart_tp_enabled: bool = Field(default=True)
    futures_smart_tp_max_extensions: int = Field(default=2)  # fewer extensions than spot (leverage risk)
    futures_smart_tp_max_pnl_pct: float = Field(default=12.0)  # cap at 12% PnL (2x leverage)
    futures_smart_tp_extend_score_threshold: int = Field(default=2)  # H-FIX: 3â†’2 (mÃ¡s extensiones TP = mÃ¡s ganancia por trade)
    futures_smart_tp_extension_pct: float = Field(default=2.0)  # extend TP by 2% of current price
    smart_tp_extension_pct: float = Field(default=2.0)  # SPOT smart TP extension %
    # Futures trailing stop: protect unrealized gains during position lifecycle
    futures_trailing_enabled: bool = Field(default=True)
    futures_trailing_activation_pct: float = Field(default=2.5)  # concentrated: activar trailing más arriba
    futures_trailing_distance_pct: float = Field(default=2.0)      # concentrated: evitar cierces en rebotes normales
    # ATR-scaled trailing: trail_dist = max(floor, min(ceiling, ATR_5m * multiplier))
    # This prevents stops from triggering on normal volatility wicks
    futures_trail_atr_enabled: bool = Field(default=True)
    futures_trail_atr_multiplier_low: float = Field(default=1.5)    # 1.5x ATR at 1.5-3% PnL
    futures_trail_atr_multiplier_mid: float = Field(default=1.2)    # 1.2x ATR at 3-5% PnL
    futures_trail_atr_multiplier_high: float = Field(default=1.0)  # 1.0x ATR at 5-8% PnL
    futures_trail_atr_multiplier_deep: float = Field(default=0.8)  # 0.8x ATR at 8%+ PnL
    futures_trail_floor_pct: float = Field(default=0.4)   # absolute min trail dist %
    futures_trail_ceiling_pct: float = Field(default=1.2)  # absolute max trail dist %
    futures_trail_atr_fallback_distance_pct: float = Field(default=1.5)  # fallback distance when ATR trail fails
    futures_breakeven_activation_pct: float = Field(default=1.0)  # FIX-T2C: 2.0â†’1.0 (breakeven fires BEFORE trailing 1.5%)
    # FIX-F1: Cooldown after futures SL hit â€” prevents re-entering same symbol immediately after stop out
    futures_sl_cooldown_hours: float = Field(default=4.0)  # PB6: 1hâ†’4h â€” stop chasing after SL
    # PB1: Stagger entries â€” max new shorts per single cycle (de-correlates entries)
    futures_max_new_per_cycle: int = Field(default=2)  # concentrated: menos entradas por ciclo
    # PB2: Mass SL circuit breaker â€” global pause after N+ positions stop in same check
    futures_mass_sl_threshold: int = Field(default=3)  # trigger if 3+ positions stop together
    futures_mass_sl_cooldown_hours: float = Field(default=4.0)  # 4h global pause
    # PB3: Confirm SHORT entry with red 5m candle (don't short into green momentum)
    futures_confirm_red_candle: bool = Field(default=True)  # require last 5m candle red
    # PB5: BTC 3h rolling momentum filter â€” catches gradual rallies that 1h misses
    futures_no_short_btc_3h_threshold: float = Field(default=1.0)  # block if BTC up >1% in 3h
    # FIX-F2: BTC 1h bounce filter â€” skip new SHORTs if BTC 1h candle is rising above threshold
    futures_no_short_btc_1h_threshold: float = Field(default=0.8)  # 0.8% = meaningful bounce
    # PV1: Scale-out â€” close partial position at first profit milestone
    futures_scale_out_pct: float = Field(default=1.5)  # trigger at +1.5% PnL
    futures_scale_out_fraction: float = Field(default=0.5)  # close 50% of qty
    # PV2: Momentum reversal close â€” close remaining after peak drawback
    futures_momentum_reversal_enabled: bool = Field(default=True)
    futures_momentum_reversal_min_peak: float = Field(default=2.0)  # peak must reach 2%+
    futures_momentum_reversal_drawback: float = Field(default=0.8)  # close if given back 0.8% from peak
    # Early adverse exit: if signal flips against open futures position while already in loss, cut earlier than hard SL
    futures_adverse_exit_enabled: bool = Field(default=True)
    futures_adverse_exit_loss_pct: float = Field(default=1.2)  # concentrated: corte más amplio
    futures_adverse_exit_min_hours: float = Field(default=1.5)
    futures_adverse_exit_min_strength: float = Field(default=0.55)
    # SSO (Smart Scale-Out) â€” adaptive multi-signal exit system
    sso_enabled: bool = Field(default=True)
    sso_stagnant_min_profit_factor: float = Field(default=0.02)
    sso_stagnant_min_profit_floor: float = Field(default=3.0)
    sso_stagnant_min_hours: float = Field(default=2.5)  # concentrated: ligeramente más agresivo en rotación de capital
    sso_stagnant_velocity_window_min: int = Field(default=60)  # jul07: 30→60 — ventana más estable, menos ruido
    # SSO-1b: Profit Decay — close when position is actively giving back gains
    sso_decay_threshold_pct: float = Field(default=0.008)     # jul07: 0.004→0.008 (wolf-hunter: 0.8% caída necesaria)
    sso_decay_min_profit_usd: float = Field(default=2.0)     # min $2 unrealized to bother closing
    sso_decay_min_hours: float = Field(default=2.0)               # concentrated: reacción más rápida a decay
    # SSO-1c: Slow Crawl â€” close when velocity too low to justify slot
    sso_slow_velocity_threshold: float = Field(default=0.001)  # 0.1%/h â€” below this, recycle capital
    sso_slow_min_profit_usd: float = Field(default=1.0)           # min $1 profit to close (don't close losses)
    sso_slow_min_hours: float = Field(default=2.0)                     # min 2h holding before slow check
    sso_slow_tier2_hours: float = Field(default=5.0)               # jul07: 4→5h (wolf-hunter: más paciencia en runners)
    sso_slow_tier3_hours: float = Field(default=7.0)               # jul07: 6→7h
    sso_slow_tier4_hours: float = Field(default=9.0)               # jul07: 8→9h
    sso_momentum_rsi_overbought: float = Field(default=70.0)
    sso_momentum_rsi_oversold: float = Field(default=30.0)
    sso_momentum_min_pnl_pct_futures: float = Field(default=1.0)
    sso_momentum_min_pnl_pct_spot: float = Field(default=1.5)
    sso_momentum_vol_dying_threshold: float = Field(default=0.5)
    sso_multilevel_enabled: bool = Field(default=True)
    sso_multilevel_l1_atr_mult: float = Field(default=1.0)
    sso_multilevel_l2_atr_mult: float = Field(default=2.0)
    sso_multilevel_l3_atr_mult: float = Field(default=3.5)
    sso_multilevel_l1_close_pct: float = Field(default=0.30)
    sso_multilevel_l2_close_pct: float = Field(default=0.30)
    sso_multilevel_l3_close_pct: float = Field(default=0.25)
    sso_target_contribution_pct: float = Field(default=0.15)
    sso_target_daily_usd: float = Field(default=40.0)
    sso_target_min_hours: float = Field(default=1.0)
    sso_reentry_min_strength_diff: float = Field(default=0.3)
    sso_reentry_min_ev_multiplier: float = Field(default=1.5)
    # SSO-6: Bounce Rescue Mode â€” close positions trapped in BTC bounce as soon as they approach breakeven
    futures_bounce_rescue_enabled: bool = Field(default=True)
    futures_bounce_rescue_max_peak_pct: float = Field(default=0.5)   # peak < 0.5% after min_hours = bounce detected
    futures_bounce_rescue_min_hours: float = Field(default=3.0)         # concentrated: liberar margen estancado más rápido
    futures_bounce_rescue_target_pnl: float = Field(default=-0.50)     # close when pnl >= -$0.50 (near breakeven)
    spot_bounce_rescue_enabled: bool = Field(default=True)
    spot_bounce_rescue_max_peak_pct: float = Field(default=0.8)         # SPOT: wider peak threshold (no leverage)
    spot_bounce_rescue_min_hours: float = Field(default=6.0)               # concentrated: liberar capital estancado más rápido
    spot_bounce_rescue_target_pnl: float = Field(default=-0.50)           # concentrated: corte más amplio, evita micro-pérdidas
    # SSO-1d: Flat Stagnation Timeout — recycle capital stuck flat in SPOT RANGE_SCALP
    sso_flat_stagnation_timeout_enabled: bool = Field(default=True)
    sso_flat_stagnation_timeout_hours: float = Field(default=3.0)
    sso_flat_stagnation_timeout_hours_range_scalp: float = Field(default=3.0)
    sso_flat_stagnation_timeout_pnl_pct: float = Field(default=0.003)
    sso_flat_stagnation_timeout_pnl_pct_range_scalp: float = Field(default=0.003)
    # SSO-6: Bounce Rescue Smart Gate â€” block rescue when position is already recovering
    # AND-partial logic: G1_strong alone OR COUNT(G2,G3,G4) >= rescue_gate_min_concurrent
    rescue_smart_gate_enabled: bool = Field(default=True)
    rescue_stay_strength_min: float = Field(default=0.50)      # min signal strength for G2 (raised from 0.40)
    rescue_stay_ev_multiplier: float = Field(default=3.0)     # G3: pred EV > NÃ—|target_pnl| (raised from 2.0, compensates predictor bias)
    rescue_velocity_min_pct: float = Field(default=0.003)       # G4: min pnl velocity/h (0.3%/h) â€” G4 never blocks alone
    rescue_gate_min_concurrent: int = Field(default=2)       # min simultaneous gates from {G2,G3,G4} to block
    rescue_gate_g1_min_pct: float = Field(default=0.005)         # G1 alone only blocks if pnl_pct >= +0.5% (FUTURES)
    rescue_gate_g1_spot_min_pct: float = Field(default=0.003)  # G1 alone SPOT: pnl_pct >= +0.3%
    rescue_gate_vol_min_spike: float = Field(default=1.5)     # G2: min vol_spike to confirm signal (raised from 1.0)
    rescue_gate_max_blocks: int = Field(default=3)               # expiry: force rescue after N blocks without pnl improvement
    rescue_gate_min_pnl_improvement: float = Field(default=0.005)  # expiry: min pnl_pct improvement between blocks to reset counter
    # FIX-7: Throttle Binance algo order verification every N check_positions cycles
    futures_binance_verify_interval_cycles: int = Field(default=3)
    # Profit realization supervisor â€” shared exit authority across SPOT and FUTURES
    profit_realization_enabled: bool = Field(default=True)
    profit_realization_spot_enabled: bool = Field(default=True)
    profit_realization_spot_min_profit_pct: float = Field(default=0.0025)
    profit_realization_spot_partial_minutes: float = Field(default=60.0)  # jul07: 35→60 (wolf-hunter: no estrangular runners)
    profit_realization_spot_full_minutes: float = Field(default=120.0)  # jul07: 70→120 (wolf-hunter: dar tiempo para madurar)
    profit_realization_spot_drawback_pct: float = Field(default=0.0080)  # jul07: 0.004→0.008 (wolf-hunter: micro-rebote no cierra)
    profit_realization_spot_partial_close_pct: float = Field(default=0.60)
    profit_realization_spot_max_extensions: int = Field(default=2)
    profit_realization_futures_enabled: bool = Field(default=True)
    profit_realization_futures_min_profit_pct: float = Field(default=0.0030)
    profit_realization_futures_partial_minutes: float = Field(default=30.0)  # concentrated: menos estrangulamiento
    profit_realization_futures_full_minutes: float = Field(default=60.0)  # concentrated: dejar madurar ganancias
    profit_realization_futures_drawback_pct: float = Field(default=0.0035)
    profit_realization_futures_partial_close_pct: float = Field(default=0.60)
    profit_realization_futures_max_extensions: int = Field(default=1)
    # Unified governance supervisor (profit + loss)
    unified_governance_enabled: bool = Field(default=True)
    unified_governance_profit_enabled: bool = Field(default=True)
    unified_governance_loss_enabled: bool = Field(default=True)
    unified_governance_loss_min_age_profile_a: float = Field(default=90.0)  # jul07: 30→90 (wolf-hunter: SL lógico primero)
    unified_governance_loss_min_age_profile_b: float = Field(default=120.0)  # jul07: 60→120
    unified_governance_loss_min_age_profile_c: float = Field(default=90.0)  # jul07: 45→90
    unified_governance_futures_loss_min_age_profile_a: float = Field(default=30.0)  # jul07: 20→30 (futures sigue más rápido)
    unified_governance_futures_loss_min_age_profile_b: float = Field(default=60.0)  # jul07: 45→60
    unified_governance_futures_loss_min_age_profile_c: float = Field(default=45.0)  # jul07: 30→45
    unified_governance_loss_escalation_age_profile_a: float = Field(default=240.0)  # jul07: 120→240 (wolf-hunter: dejar que el SL actúe)
    unified_governance_loss_escalation_age_profile_b: float = Field(default=180.0)  # jul07: 90→180
    unified_governance_loss_escalation_age_profile_c: float = Field(default=120.0)  # jul07: 60→120
    unified_governance_futures_loss_escalation_age_profile_a: float = Field(default=120.0)  # jul07: 90→120
    unified_governance_futures_loss_escalation_age_profile_b: float = Field(default=90.0)  # jul07: 70→90
    unified_governance_futures_loss_escalation_age_profile_c: float = Field(default=60.0)  # jul07: 45→60
    unified_governance_loss_distance_to_sl_min_pct: float = Field(default=0.50)
    unified_governance_loss_trajectory_acceleration_pct: float = Field(default=0.50)
    unified_governance_loss_signal_reversal_strength: float = Field(default=0.65)
    unified_governance_monitor_only_force_loss_pct: float = Field(default=2.0)
    unified_governance_monitor_only_force_age_minutes: float = Field(default=90.0)
    unified_governance_loss_min_usdt: float = Field(default=7.50)  # concentrated: 0.5% de $1,500 = umbral real
    # WR Screener: block buys on pairs with N consecutive stop_loss closes
    wr_screener_enabled: bool = Field(default=True)
    wr_screener_consecutive_sl_block: int = Field(default=5)  # block after N consecutive SL
    # B3: SPOT mass-SL cooldown
    spot_mass_sl_threshold: int = Field(default=3)
    spot_mass_sl_cooldown_hours: float = Field(default=2.0)
    # A2: Regime confirmation gate for SPOT entries
    spot_require_confirmed_regime: bool = Field(default=True)
    # A3: BTC 1h correlation filter
    btc_correlation_filter_enabled: bool = Field(default=True)
    btc_correlation_drop_threshold_pct: float = Field(default=0.3)
    btc_correlation_max_new_entries: int = Field(default=2)
    # C1: PRS SPOT_BULL minimum partial close time when momentum is high
    profit_realization_spot_bull_min_partial_minutes: float = Field(default=90.0)  # jul07: 45→90 (wolf-hunter: SPOT_BULL necesita tiempo)
    # C2: PRS RANGE_SCALP regime-specific base times (overrides global before scale is applied)
    # Smaller bases account for short TP=1.8% target; profile×fng multipliers still apply on top
    profit_realization_spot_partial_minutes_range_scalp: float = Field(default=22.0)  # s71 BUG-6: 35→22 (bounces peak at 15-25min; Profile A effective=16min)
    profit_realization_spot_full_minutes_range_scalp: float = Field(default=45.0)    # s71 BUG-6: 70→45 (era 70min; full exit within bounce window)
    # A4: Entry quality score gate
    entry_quality_gate_enabled: bool = Field(default=True)
    entry_quality_min_vol_spike: float = Field(default=1.2)  # concentrated: volumen confirma dirección
    entry_quality_min_strength: float = Field(default=0.40)  # jul07: 0.35→0.40
    entry_quality_max_failures: int = Field(default=0)  # concentrated: todos los gates deben pasar
    entry_quality_vol_spike_min_spot_bull: float = Field(default=0.85)
    entry_quality_vol_spike_floor_spot_bull: float = Field(default=0.70)
    entry_quality_rsi_min_spot_bull: float = Field(default=33.0)
    entry_quality_rsi_max_spot_bull: float = Field(default=68.0)
    entry_quality_breakout_min_vol_spot_bull: float = Field(default=1.20)
    entry_quality_breakout_rsi_max_spot_bull: float = Field(default=78.0)
    entry_quality_breakout_min_strength_spot_bull: float = Field(default=0.58)
    # BTC dominance filter
    btc_dominance_filter_enabled: bool = Field(default=True)
    btc_dominance_altcoin_threshold: float = Field(default=56.0)  # if BTC.D > this, skip alts
    tradingview_webhook_secret: str = Field(default="")
    agent_mode: str = Field(default="suggest")
    default_trade_size: float = Field(default=10.0)
    default_symbol: str = Field(default="BTCUSDT")
    news_lookback_hours: int = Field(default=24)
    cryptocompare_api_key: str = Field(default="")
    
    # Portfolio Manager configuration
    portfolio_allocation_pct: float = Field(default=100.0)
    min_pair_confidence_score: float = Field(default=50.0)
    max_risk_per_pair_pct: float = Field(default=10.0)
    diversification_factor: float = Field(default=1.0)
    enable_position_closure: bool = Field(default=False)  # F3: disabled â€” premature DB close without selling in Binance; _close_prediction_based() in run_trades.py handles closures with full gate protection
    
    # Autonomous signal generation configuration
    analysis_symbols: List[str] = Field(default=[
        'DOGEUSDT', 'RENDERUSDT', 'BNBUSDT', 'SOLUSDT', 'TAOUSDT',
        'FETUSDT', 'AVAXUSDT', 'ADAUSDT', 'XRPUSDT', 'DOTUSDT',
        'SUIUSDT', 'PEPEUSDT', 'WIFUSDT', 'BTCUSDT', 'ETHUSDT', 'LINKUSDT',
        'NEARUSDT', 'WLDUSDT', 'INJUSDT', 'AGIXUSDT',
        'ONDOUSDT', 'JUPUSDT', 'OPUSDT',  # M2: +3 NARRATIVE alta vol
        'HBARUSDT', 'AAVEUSDT',  # O5: nuevos pares validados vol24h>$10M
        # E1: ExpansiÃ³n Fase 1 (+7 tokens) - OptimizaciÃ³n activos $3K capital
        'ARBUSDT', 'MATICUSDT', 'ATOMUSDT',  # L2 + Infra
        'GRTUSDT', 'AIOZUSDT',  # AI/Data Narrative
        'SEIUSDT', 'SUSHIUSDT',  # Infra + DeFi
    ])  # E1: Removido env=ANALYSIS_SYMBOLS para forzar lista de 32 tokens

    # VolSpike override: bypass PnL predictor for narrative tokens with strong volume.
    # Disabled under the new entry-profile gate; volume information is already
    # embedded in the calibrated quality score.
    volspike_override_enabled: bool = Field(default=False)
    volspike_override_min_spike: float = Field(default=2.0)   # concentrated: volumen excepcional
    volspike_override_max_rsi: float = Field(default=65.0)      # RSI must be below this
    volspike_override_trend_required: bool = Field(default=True)  # must be 4h UP
    volspike_override_narrative_only: bool = Field(default=True)  # only for narrative tokens
    # V2: Controles adicionales para hacer override mÃ¡s selectivo (jun 16 2026)
    volspike_override_min_strength: float = Field(default=0.75)  # concentrated: alta convicción obligatoria
    volspike_override_max_positions: int = Field(default=1)  # concentrated: compite por 1 de 4-5 slots
    volspike_override_daily_limit: int = Field(default=1)      # concentrated: máx 1 override/día
    
    # Prediction-based position closure configuration
    min_pnl_improvement_threshold: float = Field(default=0.5)  # 50% improvement
    min_success_probability_improvement: float = Field(default=0.2)  # 20% improvement
    enable_prediction_based_closing: bool = Field(default=True)

    # Stop-Loss / Take-Profit configuration
    stop_loss_pct: float = Field(default=3.36)       # jun29: 1.03â†’3.36 (run_0677)
    take_profit_pct: float = Field(default=4.14)   # jun29: 5.83â†’4.14 (run_0677)
    trailing_stop_enabled: bool = Field(default=True)
    trailing_stop_activation_pct: float = Field(default=2.0)  # activate trailing after +2%
    trailing_stop_distance_pct: float = Field(default=1.0)      # trail 1% below peak
    protect_profitable_positions: bool = Field(default=True)  # skip rebalance if PnL>1%
    protect_profit_min_pct: float = Field(default=0.3)              # minimum PnL% to protect (v3: 0.8â†’0.3 â€” protect desde micro-ganancia)
    # Rebalancing controls (v2)
    min_hold_minutes: int = Field(default=120)         # min time before rebalancing out a position (v5: 240â†’120 â€” balance between hold and flexibility)
    min_hold_minutes_range_scalp: int = Field(default=15)  # s70: 30→15 (micro-bounces last 15-30min)
    min_hold_minutes_spot_bull: int = Field(default=120)     # SPOT_BULL: 2h hold before replacement
    max_rebalance_per_cycle: int = Field(default=2)  # max sells per rebalance cycle
    rebalance_excluded_symbols: List[str] = Field(default=['USD1USDT', 'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'FDUSDUSDT'])  # F4: stablecoins never rebalanced
    rebalance_max_abs_pnl_pct: float = Field(default=3.0)  # concentrated: posiciones grandes necesitan más tolerancia
    max_total_exposure_pct: float = Field(default=0.90)  # concentrated: máxima exposición permitida del portafolio
    # M1: ATR-Based dynamic SL â€” replaces fixed SL% with 1.5Ã—ATR for volatile tokens
    atr_sl_enabled: bool = Field(default=True)           # enable ATR-based SL calculation
    atr_sl_multiplier: float = Field(default=1.5)     # SL = max(category_sl, atr_mult Ã— ATR14)
    atr_sl_max_pct: float = Field(default=0.08)          # cap ATR SL at 8% to avoid monster stops
    atr_sl_min_pct: float = Field(default=0.015)         # floor ATR SL at 1.5% (SL-OPT: 1%â†’1.5% â€” prevent micro-stops on volatile tokens)
    # M3: Kelly conviction multiplier â€” scale position size by signal strength
    kelly_conviction_enabled: bool = Field(default=True)
    kelly_conviction_vol2x: float = Field(default=1.8)   # jul07: 1.5→1.8 (wolf-hunter s63: vol 2-3x +80%)
    kelly_conviction_vol3x: float = Field(default=2.5)   # jul07: 2.0→2.5 (wolf-hunter s63: vol >3x +150%)
    kelly_conviction_max_pct: float = Field(default=0.25)  # concentrated: vol >3x permite 25% de portafolio
    # Smart exit: anti-wick SL confirmation
    sl_confirmation_candles: int = Field(default=1)  # 0=immediate, 1=require last 1m candle close below SL
    # Smart exit: rebalance skips if predictor sees upside
    rebalance_skip_if_positive_prediction: bool = Field(default=True)
    rebalance_min_hold_prob: float = Field(default=0.45)  # skip rebalance if prob >= this AND epnl > 0 (v3: 0.55â†’0.45)
    rebalance_skip_if_bullish_signal: bool = Field(default=True)  # require technical negative signal before rebalancing
    rebalance_rsi_overbought: float = Field(default=60.0)  # RSI1h threshold â€” rebalance only if RSI > this OR EMA9 descending
    # Dynamic max positions by regime (replaces single MAX_POSITIONS when regime is known)
    max_positions_spot_bull: int = Field(default=5)    # concentrated: 4-5 posiciones en bull
    max_positions_range_scalp: int = Field(default=4)                   # s71 BUG-8: lateral allows 4 simultaneous setups  # concentrated: pocos setups en rango
    max_positions_preservation: int = Field(default=1)  # concentrated: capital preservado en bear
    max_positions_futures_short: int = Field(default=5)  # concentrated: 3-5 shorts simultáneos
    # Portfolio concentration policy: explicitly couple slot capacity with per-position sizing.
    # Objective: when a mode/profile runs fewer effective slots, increase ticket size in a controlled way.
    portfolio_concentration_enabled: bool = Field(default=True)
    portfolio_concentration_alpha: float = Field(default=0.90)
    portfolio_concentration_min_mult: float = Field(default=0.75)
    portfolio_concentration_max_mult: float = Field(default=1.35)
    portfolio_concentration_pressure_start: float = Field(default=0.50)
    portfolio_concentration_pressure_boost: float = Field(default=0.40)
    portfolio_concentration_baseline_spot_bull: int = Field(default=5)
    portfolio_concentration_baseline_range_scalp: int = Field(default=4)
    portfolio_concentration_baseline_preservation: int = Field(default=1)
    portfolio_concentration_baseline_futures_short: int = Field(default=5)
    # QUBO Portfolio Selection (combinatorial optimizer)
    qubo_enabled: bool = Field(default=True)
    qubo_lambda_corr: float = Field(default=1.0)       # penalty weight for pairwise correlation
    qubo_lambda_size: float = Field(default=5.0)       # penalty weight for cardinality constraint (keep high)
    qubo_lambda_risk: float = Field(default=0.5)         # penalty weight for individual risk
    qubo_solver: str = Field(default="sqa")            # sqa | greedy | brute
    qubo_population_size: int = Field(default=30)        # SQA population
    qubo_iterations: int = Field(default=80)           # SQA iterations
    qubo_seed: int = Field(default=42)                 # random seed
    qubo_min_candidates: int = Field(default=2)          # min candidates to activate QUBO selector
    qubo_include_open_positions: bool = Field(default=True)  # include open positions in correlation matrix
    # Peaks-first pipeline feature flags (s79)
    peaks_first_enabled: bool = Field(default=True)          # Peaks profile drives selection priority
    predictor_gate_mode: str = Field(default="blocking")     # "blocking" | "advisory" | "disabled"
    entry_quality_gate_mode: str = Field(default="disabled")  # superseded by entry_profile_gate; "blocking" is legacy
    entry_decision_enabled: bool = Field(default=True)
    entry_decision_policy_version: str = Field(default="strict_v1")
    entry_decision_regime_mode: Dict[str, str] = Field(default={
        "SPOT_BULL": "enforced",
        "RANGE_SCALP": "enforced",
        "FUTURES_SHORT": "enforced",
        "PRESERVATION": "enforced",
    })
    entry_decision_min_expected_pnl: Dict[str, float] = Field(default={
        "SPOT_BULL": 0.0,
        "RANGE_SCALP": 0.0,
        "FUTURES_SHORT": 0.0,
        "PRESERVATION": 0.0,
    })
    entry_decision_min_probability: Dict[str, float] = Field(default={
        "SPOT_BULL": 0.60,
        "RANGE_SCALP": 0.60,
        "FUTURES_SHORT": 0.60,
        "PRESERVATION": 0.60,
    })
    # Active replacement: swap weakest open position for a better new signal
    replace_only_losing: bool = Field(default=True)  # only replace positions in loss/flat
    replace_ev_min_ratio: float = Field(default=1.1)  # MEJO-1: 1.3â†’1.1 â€” more permissive given small EV values
    replace_strength_min_delta: float = Field(default=0.05)  # MEJO-2: 0.15â†’0.05 â€” BUG-B fix: avg is now real, not hardcoded 0.5
    # Overflow slots: allow extra positions beyond regime max when signal quality is exceptional
    overflow_min_strength: float = Field(default=0.85)  # concentrated: overflow solo convicción extrema
    overflow_min_prob: float = Field(default=0.70)          # concentrated: overflow solo probabilidad extrema
    overflow_max_extra: int = Field(default=1)             # concentrated: máx 1 slot extra
    # â”€â”€ Behavioral Finance: F&G Kelly multiplier (agresivo) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    fng_kelly_boost_enabled: bool = Field(default=True)
    fng_extreme_fear_threshold: int = Field(default=20)   # <= this â†’ boost
    fng_capitulation_threshold: int = Field(default=15)   # <= this â†’ max boost
    fng_extreme_greed_threshold: int = Field(default=75) # >= this â†’ reduce
    fng_extreme_fear_multiplier: float = Field(default=1.35)    # Extreme Fear: +35%
    fng_capitulation_multiplier: float = Field(default=1.50)   # Capitulacion: +50%
    fng_extreme_greed_multiplier: float = Field(default=0.65) # Extreme Greed: -35%
    fng_capitulation_pnl_boost: float = Field(default=-0.25)    # relax min_pnl hasta -$0.25 en capitulacion

    # â”€â”€ Microestructura: liquidez mÃ­nima y slippage implÃ­cito â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    liquidity_filter_enabled: bool = Field(default=True)
    min_liquidity_vol_usdt: float = Field(default=3_000_000.0)  # vol 24h mÃ­nimo
    max_slippage_tp_ratio: float = Field(default=0.25)           # spread/TP max (0.25 = 25% del TP)
    slippage_pct: float = Field(default=0.00052)  # jun29: 0.0005â†’0.00052 (run_0677)

    # â”€â”€ Correlaciones: lÃ­mite de posiciones por categorÃ­a â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    category_max_positions_enabled: bool = Field(default=True)
    category_max_positions: Dict[str, Any] = Field(default={
        "LARGE_CAP":  1,   # concentrated: máx 1 large cap
        "MIDCAP_ALT": 2,   # concentrated: máx 2 midcaps
        "NARRATIVE":  1,   # concentrated: máx 1 narrative
        "MEME":       1,   # concentrated: máx 1 meme
    })

    # â”€â”€ Circuit breaker tÃ©cnico (solo fallas de sistema, no pÃ©rdidas) â”€â”€â”€â”€â”€â”€â”€
    circuit_breaker_enabled: bool = Field(default=True)
    circuit_breaker_consecutive_failures: int = Field(default=5)
    circuit_breaker_pause_minutes: int = Field(default=10)

    # â”€â”€ On-chain: funding rate como seÃ±al de sobrecompra para spot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    funding_rate_spot_filter_enabled: bool = Field(default=True)
    funding_rate_spot_max: float = Field(default=0.0008)  # > 0.08% = longs sobreapal.
    funding_rate_cache_minutes: int = Field(default=10)

    # Dynamic cycle by regime (v2)
    cycle_spot_bull: int = Field(default=10)            # jun17: 20â†’10 min (+47% throughput seÃ±ales)
    cycle_range_scalp: int = Field(default=3)          # s70: 5→3 min (detect bounce in time)
    cycle_preservation: int = Field(default=45)      # jun17: 30â†’45 min (menos ciclos en bajista = menos comisiones)
    cycle_futures_short: int = Field(default=4)     # H-FIX: 6â†’4 min (trailing mÃ¡s reactivo a movimientos rÃ¡pidos)

    # Signal quality filter
    min_signal_strength: float = Field(default=0.60)  # concentrated: solo señales fuertes
    min_success_probability: float = Field(default=0.60)  # concentrated: edge estadístico alto
    # P1.3: Signal cache TTL dinÃ¡mico â€” reduce de 600s fijo a 1 ciclo por rÃ©gimen
    signal_cache_ttl_seconds: int = Field(default=240)  # default 4 min (1 ciclo FUTURES_SHORT)

    # Narrative token classification (used by volspike override + bootstrap)
    narrative_tokens: List[str] = Field(default=[
        'FETUSDT', 'TAOUSDT', 'RENDERUSDT', 'NEARUSDT', 'WLDUSDT',
        'AGIXUSDT', 'OCEANUSDT', 'INJUSDT',
        # E1: Nuevos narratives AI/Data para rotaciÃ³n sectorial
        'GRTUSDT', 'AIOZUSDT',
        # MEJO-J: Sincronizados con token_categories market_behavior=NARRATIVE
        'JTOUSDT',
    ])

    # â”€â”€ Token classification 2D â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Dimension A: market_behavior (cÃ³mo se mueve)
    # Dimension B: origin (por quÃ© existe)
    token_categories: Dict[str, Any] = Field(default={
        "BTCUSDT":    {"market_behavior": "LARGE_CAP",   "origin": "STORE_OF_VALUE"},
        "ETHUSDT":    {"market_behavior": "LARGE_CAP",   "origin": "INFRASTRUCTURE"},
        "BNBUSDT":    {"market_behavior": "LARGE_CAP",   "origin": "INFRASTRUCTURE"},
        "SOLUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "AVAXUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "DOTUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "LINKUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "SUIUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "ADAUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "XRPUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "FETUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "TAOUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "RENDERUSDT": {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "NEARUSDT":   {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "WLDUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "AGIXUSDT":   {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "OCEANUSDT":  {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "DOGEUSDT":   {"market_behavior": "MEME",        "origin": "MEME_POLITICAL"},
        "PEPEUSDT":   {"market_behavior": "MEME",        "origin": "MEME_POLITICAL"},
        "WIFUSDT":    {"market_behavior": "MEME",        "origin": "MEME_POLITICAL"},
        "INJUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "UNIUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "DEFI"},
        "AAVEUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "DEFI"},
        "BCHUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "ZECUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "LTCUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "XLMUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "ZROUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},
        "JTOUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},
        "TRUMPUSDT":  {"market_behavior": "MEME",        "origin": "MEME_POLITICAL"},
        "MEGAUSDT":   {"market_behavior": "MEME",        "origin": "MEME_POLITICAL"},
        "HBARUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},  # O5: Hedera, $15.7M vol/dÃ­a
        # E1: Nuevos tokens Fase 1 (expansiÃ³n activos)
        "ARBUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "L2"},            # Arbitrum L2
        "MATICUSDT":  {"market_behavior": "MIDCAP_ALT",  "origin": "L2"},            # Polygon L2
        "ATOMUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Cosmos infra
        "GRTUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},        # The Graph indexing
        "AIOZUSDT":   {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},        # DePIN + AI streaming
        "SEIUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Sei trading-optimized L1
        "SUSHIUSDT":  {"market_behavior": "MIDCAP_ALT",  "origin": "DEFI"},           # SushiSwap DEX
        "ONDOUSDT":   {"market_behavior": "NARRATIVE",   "origin": "DEFI"},            # Ondo Finance RWA
        "TRXUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # TRON
        # G3 s64: tokens in analysis_symbols that were missing explicit classification
        "JUPUSDT":    {"market_behavior": "NARRATIVE",   "origin": "DEFI"},            # Jupiter DEX aggregator
        "OPUSDT":     {"market_behavior": "MIDCAP_ALT",  "origin": "L2"},             # Optimism L2
        "UUSDT":      {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Upbit token (small cap)
        "FILUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Filecoin storage
    })

    # SÃ­mbolos explÃ­citamente excluidos del trading (oro tokenizado, stablecoins, delisted)
    # Estos son bloqueados antes de cualquier anÃ¡lisis en _buy_top_symbols
    trading_excluded_symbols: List[str] = Field(default=[
        "XAUTUSDT",   # XAU Tether â€” oro tokenizado, SL/TP% no aplica
        "PAXGUSDT",   # PAX Gold â€” oro tokenizado
        "RLUSDUSDT",  # RLUSD â€” stablecoin sin upside
        "USD1USDT",   # USD1 â€” stablecoin sin upside
        "SPCXBUSDT",  # SPCXB â€” volumen artificial
        "UUSDT",      # Upbit token â€” baja volatilidad, predicciones negativas, capital atascado
    ])

    # Win-rate thresholds per market_behavior category
    # min_prob: mÃ­nima probabilidad de Ã©xito requerida
    # min_pnl:  mÃ­nimo PnL esperado (USDT) â€” negativo permitido para NARRATIVE
    # min_volspike: VolSpike mÃ­nimo para confirmar seÃ±al (informativo, no enforcement aquÃ­)
    # MEJO-A: NARRATIVE trailing aumentado (8%/3%) para capturar breakouts +15-30% completos
    category_thresholds: Dict[str, Any] = Field(default={
        "LARGE_CAP":  {"min_prob": 0.527, "min_pnl": 0.10, "min_volspike": 1.2, "stop_loss_pct": 3.5, "take_profit_pct": 3.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 0.8, "tp_extension_pct": 1.5},  # jul07: sl 2.5→3.5, trail_act 2.0→2.5 (wolf-hunter: SL más amplio)
        "MIDCAP_ALT": {"min_prob": 0.527, "min_pnl": 0.00, "min_volspike": 1.5, "stop_loss_pct": 3.5, "take_profit_pct": 3.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 0.8, "tp_extension_pct": 2.0},  # jul07: sl 2.5→3.5, trail_act 2.0→2.5 (wolf-hunter: SL más amplio)
        "NARRATIVE":  {"min_prob": 0.42,  "min_pnl": -1.0, "min_volspike": 1.5, "stop_loss_pct": 3.5, "take_profit_pct": 7.0, "trailing_activation_pct": 5.0, "trailing_distance_pct": 3.0, "tp_extension_pct": 3.0},  # s65: min_prob 0.527->0.42 (asimetrico per diseno); trail_dist 2.0->3.0 (captura breakouts +15-30%)
        "MEME":       {"min_prob": 0.527, "min_pnl": 0.05, "min_volspike": 2.5, "stop_loss_pct": 2.5, "take_profit_pct": 4.5, "trailing_activation_pct": 2.0, "trailing_distance_pct": 1.0, "tp_extension_pct": 2.5},  # jun28: min_prob 0.52â†’0.527
    })
    
    # â”€â”€ PredicciÃ³n con prioridad absoluta (jun 17 2026) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # C1: Re-evaluaciÃ³n predictiva en cada ciclo â€” cierra posiciones cuando EV deteriora
    prediction_exit_enabled: bool = Field(default=True)
    min_hold_for_prediction_exit: int = Field(default=90)  # minutos antes de evaluar cierre
    min_hold_for_prediction_exit_range_scalp: int = Field(default=20)  # s71 BUG-4: bounces 15-30min, no esperar 90min
    min_hold_for_prediction_exit_futures_short: int = Field(default=30)  # s72 B1
    # Wolf Hunter Peak Capture Engine (Profit PredExit in gains)
    profit_predexit_enabled: bool = Field(default=True)
    profit_predexit_capture_margin_A: float = Field(default=0.80)
    profit_predexit_capture_margin_B: float = Field(default=1.00)
    profit_predexit_capture_margin_C: float = Field(default=1.25)
    profit_predexit_size_sensitivity: float = Field(default=1.5)
    profit_predexit_conviction_sensitivity: float = Field(default=0.4)
    profit_predexit_min_profit_pct: float = Field(default=0.005)
    profit_predexit_absolute_min_usd: float = Field(default=1.50)
    profit_predexit_horizon_minutes: int = Field(default=60)
    profit_predexit_giveback_penalty_weight: float = Field(default=0.5)
    profit_predexit_early_protection_mode: str = Field(default="active")
    profit_predexit_early_active_regimes: List[str] = Field(default=["SPOT_BULL"])
    profit_predexit_early_min_peak_pct: float = Field(default=1.5)
    profit_predexit_early_min_giveback_pct: float = Field(default=0.5)
    loss_focus_enabled: bool = Field(default=True)
    loss_focus_threshold_usdt: float = Field(default=9.0)
    loss_focus_reset_usdt: float = Field(default=6.0)
    peak_reconstruction_enabled: bool = Field(default=True)
    peak_reconstruction_lookback_candles: int = Field(default=30)

    # ── Staged multi-cycle entry validation (ready state) ──────────────────────
    staged_entry_enabled: bool = Field(default=True)
    staged_entry_price_source: str = Field(default="avg")  # 'avg' (VWAP 5m) or 'ticker'
    staged_entry_persist_checks: bool = Field(default=True)
    range_reversion_predictor_mode: str = Field(default="shadow")
    preservation_upgrade_to_futures: bool = Field(default=False)
    staged_entry_regime_config: Dict[str, Any] = Field(default={
        "SPOT_BULL": {
            "enabled": True,
            "checks_per_cycle": 3,
            "required_positive_checks": 2,
            "max_cycles": 3,
            "max_age_min": 60,
            "intra_mode": "predictor",  # 'predictor' | 'price_ratio' | 'disabled'
            "price_source": "avg",
            "use_fear_greed": True,
            "cooldown_min": 0,
            "require_inter_positive": False,
            "require_predictor_valid": True,
            "min_expected_pnl": 0.0,
            "policy_version": "v1",
            "max_open_per_cycle": 1,
            "direction_confirmation_mode": "enforced",
            "direction_min_progress_bps": 5.0,
            "direction_max_adverse_bps": 50.0,
            "direction_required_confirmations": 2,
        },
        "RANGE_SCALP": {
            "enabled": True,
            "checks_per_cycle": 2,
            "required_positive_checks": 2,
            "max_cycles": 3,
            "max_age_min": 20,
            "intra_mode": "predictor",
            "price_source": "avg",
            "use_fear_greed": True,
            "cooldown_min": 0,
            "require_inter_positive": False,
            "require_predictor_valid": True,
            "policy_version": "v1",
            "max_open_per_cycle": 1,
            "direction_confirmation_mode": "enforced",
            "direction_min_progress_bps": 3.0,
            "direction_max_adverse_bps": 50.0,
            "direction_required_confirmations": 2,
        },
        "FUTURES_SHORT": {
            "enabled": True,
            "checks_per_cycle": 1,
            "required_positive_checks": 2,
            "max_cycles": 3,
            "max_age_min": 20,
            "intra_mode": "predictor",
            "price_source": "ticker",
            "use_fear_greed": False,
            "cooldown_min": 0,
            "require_inter_positive": True,
            "require_predictor_valid": True,
            "min_expected_pnl": 0.0,
            "policy_version": "v1",
            "max_open_per_cycle": 1,
            "direction_confirmation_mode": "enforced",
            "direction_min_progress_bps": 3.0,
            "direction_max_adverse_bps": 50.0,
            "direction_required_confirmations": 2,
        },
        "PRESERVATION": {
            "enabled": True,
            "checks_per_cycle": 1,
            "required_positive_checks": 2,
            "max_cycles": 3,
            "max_age_min": 100,
            "intra_mode": "predictor",
            "price_source": "avg",
            "use_fear_greed": True,
            "cooldown_min": 0,
            "require_inter_positive": True,
            "require_predictor_valid": True,
            "min_expected_pnl": 0.0,
            "policy_version": "v1",
            "max_open_per_cycle": 1,
            "direction_confirmation_mode": "enforced",
            "direction_min_progress_bps": 3.0,
            "direction_max_adverse_bps": 50.0,
            "direction_required_confirmations": 2,
        },
    })

    # C2: Segunda entrada permitida si predicciÃ³n nueva es fuertemente mejor
    allow_prediction_based_add: bool = Field(default=True)
    prediction_add_min_ev_ratio: float = Field(default=1.5)  # nueva pred >= 1.5x la existente
    prediction_add_min_strength: float = Field(default=0.70)
    prediction_add_kelly_factor: float = Field(default=0.6)  # Kelly reducido para 2Âª entrada
    # C3: Cooldowns en DB â€” previene doble entrada post-restart
    persist_cooldowns_enabled: bool = Field(default=True)
    # C4: Cierre automÃ¡tico de activos prohibidos al arrancar
    auto_close_excluded_on_startup: bool = Field(default=True)

    # â”€â”€ Futures trading (USDM, fapi.binance.com) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    futures_enabled: bool = Field(default=True)  # s80: disabled under entry-profile gate redesign â€” backtest run_0812 uses futures
    futures_leverage: int = Field(default=10)          # K2: 2â†’10 â€” backtest run_0812 validated 10x
    futures_max_shorts: int = Field(default=5)       # concentrated: 3-5 shorts simultáneos
    futures_max_largecap_shorts: int = Field(default=3)  # concentrated: no concentrar todo en BTC/ETH
    futures_usdt_per_trade: float = Field(default=150.0)  # concentrated: floor de margen igualado a SPOT
    futures_sl_pct: float = Field(default=2.00)          # concentrated: mayor tolerancia al ruido
    futures_tp_pct: float = Field(default=4.00)          # concentrated: mejor R:R
    futures_min_btc_7d_drop: float = Field(default=-7.0)  # only short in bear
    futures_symbols: List[str] = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
        'NEARUSDT', 'SUIUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'TAOUSDT',
        'UNIUSDT', 'DOTUSDT', 'BCHUSDT', 'AAVEUSDT', 'FILUSDT',
        'INJUSDT',
    ])   # symbols eligible for shorting (run_0812 baseline)
    futures_largecap_symbols: List[str] = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT',
    ])   # LARGE_CAP futures symbols for category cap
    futures_no_short_btc_4h_threshold: float = Field(default=2.0)
    # Futures overflow: allow extra SHORTs beyond futures_max_shorts when signal is exceptional
    futures_overflow_max_extra: int   = Field(default=1)    # concentrated: máx 1 overflow
    futures_overflow_min_strength: float = Field(default=0.65)  # H-FIX: 0.75â†’0.65 (habilita overflow en condiciones de alta vol moderada)
    futures_overflow_min_vol_spike: float = Field(default=2.0)  # H-FIX: 2.5â†’2.0 (mÃ¡s accesible para overflow en pÃ¡nico moderado)
    futures_overflow_min_drop_24h: float  = Field(default=-3.0)  # min 24h drop %
    # M1: Minimum strength to inject a symbol into the futures signal pool (filters weak HOLDs)
    futures_min_inject_strength: float = Field(default=0.50)  # concentrated: evitar rebotes (lección AAVE -5.88%)
    # Futures Kelly dynamic cap
    futures_kelly_cap_pct: float = Field(default=0.30)  # concentrated: ~30% del margen disponible
    # Futures signal filtering: minimum probability and vol spike (backtest-optimizable)
    futures_min_prob: float = Field(default=0.499)  # jun29: 0.527â†’0.499 (run_0677)
    futures_vol_spike_min: float = Field(default=0.91)  # jun29: 0.83â†’0.91 (run_0677)
    # Futures category thresholds — keys MUST match token_categories market_behavior values
    futures_category_thresholds: Dict[str, Any] = Field(default={
        "LARGE_CAP":  {"min_prob": 0.499, "stop_loss_pct": 1.20, "take_profit_pct": 2.40, "trailing_activation_pct": 1.5, "trailing_distance_pct": 1.0},  # s65: diferenciado — SL más ajustado para large cap
        "MIDCAP_ALT": {"min_prob": 0.499, "stop_loss_pct": 1.42, "take_profit_pct": 2.81, "trailing_activation_pct": 2.0, "trailing_distance_pct": 1.5},  # s65: MIDCAP→MIDCAP_ALT (align token_categories)
        "NARRATIVE":  {"min_prob": 0.499, "stop_loss_pct": 1.80, "take_profit_pct": 3.60, "trailing_activation_pct": 2.5, "trailing_distance_pct": 2.0},  # s65: SL/TP más amplio para mayor volatilidad
        "MEME":       {"min_prob": 0.499, "stop_loss_pct": 1.60, "take_profit_pct": 3.20, "trailing_activation_pct": 2.0, "trailing_distance_pct": 1.5},  # s65: añadido MEME (faltaba)
    })
    # Global vol spike minimum (used by both SPOT and FUTURES signal filtering)
    vol_spike_min: float = Field(default=0.91)  # jun29: 0.83â†’0.91 (run_0677)
    # Hybrid optimizer framework (quantum + swarm + ML)
    hybrid_optimizer_enabled: bool = Field(default=False)
    auto_apply_backtest: bool = Field(default=False)
    backtest_safe_only: bool = Field(default=True)
    # Capital Bridge: bidirectional autonomous spotâ†”futures transfer
    futures_capital_bridge_enabled: bool = Field(default=True)
    futures_capital_bridge_min_transfer: float = Field(default=10.0)  # min USDT to trigger transfer
    # Unified Portfolio Manager: PortfolioManager as capital director for both SPOT and Futures
    use_unified_portfolio_manager: bool = Field(default=False)  # feature flag â€” false = legacy behavior
    futures_kelly_base_usdt: float = Field(default=150.0)  # concentrated: base igualada a SPOT
    futures_kelly_max_usdt: float = Field(default=1500.0)    # concentrated: cap por short elevado
    futures_kelly_max_pct_liquid: float = Field(default=0.85)  # concentrated: consistente con SPOT
    futures_kelly_min_usdt: float = Field(default=0.0)  # min absolute futures margin ticket; 0 disables floor

    # Anti-ghost / reconciliation controls
    spot_dust_cleanup_min_notional_usdt: float = Field(default=5.0)
    spot_reconcile_auto_fix_enabled: bool = Field(default=True)
    spot_reconcile_min_value_usdt: float = Field(default=5.0)

    # â”€â”€ Dynamic pair screener â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    screener_enabled: bool = Field(default=True)
    screener_min_vol_usdt: float = Field(default=10_000_000)   # $10M spot min
    screener_futures_min_vol: float = Field(default=100_000_000)  # $100M futures min
    screener_max_pairs: int = Field(default=50)           # max dynamic pairs for spot
    screener_cache_minutes: int = Field(default=15)   # TTL for screener cache
    screener_merge_with_static: bool = Field(default=True)

    # â”€â”€ Screener v2: Top 100 Quality Assets â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    screener_top_n_enabled: bool = Field(default=True)
    screener_top_n_count: int = Field(default=100)  # Target: 100 quality assets
    screener_quality_min_vol_usdt: float = Field(default=10_000_000)  # $10M minimum volume
    screener_quality_max_volatility: float = Field(default=0.50)     # Max 50% 24h volatility
    screener_quality_min_price_usdt: float = Field(default=0.0001)   # Minimum price filter
    screener_quality_min_age_days: int = Field(default=90)             # Minimum token age

    # â”€â”€ Futures LONG / hedge bidireccional â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    futures_hedge_mode_enabled: bool = Field(default=True)
    futures_long_enabled: bool = Field(default=True)
    futures_max_longs: int = Field(default=2)  # concentrated: hedge más concentrado
    futures_long_sl_pct: float = Field(default=2.5)
    futures_long_tp_pct: float = Field(default=5.0)
    futures_long_in_range_scalp: bool = Field(default=True)
    futures_long_in_spot_bull: bool = Field(default=True)
    futures_long_rsi_threshold: float = Field(default=35.0)

    # â”€â”€ Market Regime adaptive mode â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    market_regime_enabled: bool = Field(default=True)
    current_regime_mode: str = Field(default="SPOT_BULL")
    current_regime_profile_label: str = Field(default="B")  # runtime: active profile (B/A/D); set each cycle
    # RANGE_SCALP mode parameters
    range_scalp_sl_pct: float = Field(default=0.8)    # s70: 1.2→0.8 (R:R 2.25, EV>0 with WR>31%)
    range_scalp_tp_pct: float = Field(default=1.8)    # s70: 1.5→1.8
    range_scalp_kelly_mult: float = Field(default=0.7)
    range_scalp_max_rsi: float = Field(default=60.0)  # s72: 48→60 (lateral RSI normal = 45-65; old 48 was panic-dip only)
    range_scalp_min_rsi: float = Field(default=25.0)  # s70: new — avoid free-fall entries
    range_scalp_require_bb_lower: bool = Field(default=True)
    range_scalp_breakeven_pct: float = Field(default=0.40)  # s70: new — fast breakeven on micro-bounce
    range_scalp_max_bb_position: float = Field(default=0.40)  # s72: max relative position in BB range (0=lower, 1=upper)

    # ── SSO timing per regime (s70: replaces hardcoded *0.8/*1.5 multipliers in smart_scale_out.py) ──
    # Values are absolute finals: base_global × former_multiplier, pre-computed.
    # Globals (sso_stagnant_min_hours etc.) are NEVER modified — these fields are the per-regime source of truth.
    sso_stagnant_min_hours_spot_bull: float = Field(default=3.75)       # global(2.5)*1.5 + harmony*1.05
    sso_stagnant_min_hours_range_scalp: float = Field(default=1.5)      # s70 optimised: global(2.5)*0.6
    sso_stagnant_min_hours_preservation: float = Field(default=2.25)    # global(2.5)*0.90 harmony
    sso_stagnant_min_hours_futures_short: float = Field(default=2.38)   # global(2.5)*0.95 harmony
    sso_slow_min_hours_spot_bull: float = Field(default=2.8)            # global(2.0)*1.4
    sso_slow_min_hours_range_scalp: float = Field(default=1.0)          # s70 optimised: global(2.0)*0.5
    sso_slow_min_hours_preservation: float = Field(default=2.0)         # no multiplier
    sso_slow_min_hours_futures_short: float = Field(default=1.4)        # global(2.0)*0.70
    sso_decay_min_hours_spot_bull: float = Field(default=2.3)           # global(2.0)*1.15
    sso_decay_min_hours_range_scalp: float = Field(default=1.7)         # global(2.0)*0.85
    sso_decay_min_hours_preservation: float = Field(default=2.0)        # no multiplier
    sso_decay_min_hours_futures_short: float = Field(default=2.0)       # no multiplier

    # ── Entry quality gate per regime (s70: replaces global entry_quality_max_failures) ──
    entry_quality_max_failures_spot_bull: int = Field(default=0)
    entry_quality_max_failures_range_scalp: int = Field(default=1)      # vol low in lateral is structural
    entry_quality_max_failures_preservation: int = Field(default=0)
    entry_quality_max_failures_futures_short: int = Field(default=0)
    entry_quality_vol_spike_min_range_scalp: float = Field(default=0.35)  # s72: lateral = low vol; 0.35x floor (vs 1.2x for others)
    sso_target_contribution_pct_range_scalp: float = Field(default=0.05)  # s73 C2: 5% of half-target=$1.00 min — achievable at TP=1.8%

    # ── SmartTP EXTEND per regime (s70: replaces smart_tp_extend_disabled_regimes list) ──
    smart_tp_extend_enabled_spot_bull: bool = Field(default=True)
    smart_tp_extend_enabled_range_scalp: bool = Field(default=True)     # s70: enabled (was False via list)
    smart_tp_extend_enabled_preservation: bool = Field(default=False)
    smart_tp_extend_enabled_futures_short: bool = Field(default=True)
    smart_tp_score_threshold_range_scalp: int = Field(default=2)        # s71 BUG-7: R:R 5.6:1 to extend, 2/4 sufficient

    # ── BB lower touch tolerance per mode (s70: replaces hardcode *1.005 in autonomous_signals.py) ──
    bb_lower_touch_tolerance_pct: float = Field(default=0.5)            # default/SPOT_BULL tolerance %
    bb_lower_touch_tolerance_range_scalp_pct: float = Field(default=1.5)  # wider for lateral market

    # ── PredExit hold floor per regime (s71 BUG-4 + s72 B1) ──
    min_hold_for_prediction_exit_futures_short: int = Field(default=30)  # s72: shorts move fast; 90min global too slow

    # PRESERVATION mode parameters
    preservation_sl_pct: float = Field(default=1.5)
    preservation_tp_pct: float = Field(default=3.0)
    preservation_kelly_mult: float = Field(default=0.5)
    preservation_narrative_only: bool = Field(default=True)
    preservation_max_rsi: float = Field(default=65.0)

    # Binance commission configuration â€” rates are controlled by Binance, not backtest.
    # Defaults are the documented standard rates; live rates are refreshed from API.
    binance_commission_rate: float = Field(default=0.001)  # 0.1% SPOT taker default
    use_bnb_for_commission: bool = Field(default=True)  # 0.05% SPOT with BNB
    bnb_spot_commission_rate: float = Field(default=0.0005)  # 0.05% SPOT with BNB
    futures_commission_rate: float = Field(default=0.0004)  # 0.04% futures taker default
    bnb_futures_commission_rate: float = Field(default=0.0002)  # 0.02% futures with BNB
    # Auto BNB discount: system detects BNB balance and converts USDT if needed
    auto_bnb_for_commission: bool = Field(default=True)
    bnb_buffer_days: int = Field(default=7)
    bnb_min_balance_usd: float = Field(default=5.0)
    bnb_extra_buffer_usd: float = Field(default=10.0)
    bnb_max_reserve_usd: float = Field(default=100.0)  # max USD worth of BNB to hold as commission reserve; excess is sold to USDT
    futures_max_escalamientos: int = Field(default=2)  # max entries per symbol (escalamiento cap)

    # ── Capital Rotator (SSO-6) ──────────────────────────────────────────────
    # SSO-7: Stagnation Trailing + Opportunity Collapse
    # En lugar de cerrar por timeout, sube el SL de la posición estancada hacia
    # el precio actual progresivamente. Si hay un candidato significativamente mejor,
    # colapsa el SL al precio actual para que el mercado saque la posición.
    sso7_enabled: bool = Field(default=True)
    sso7_min_age_hours: float = Field(default=1.0)          # anti-flip: mínimo antes de evaluar
    sso7_max_pnl_usdt: float = Field(default=3.0)           # posición graduada si PnL >= esto
    sso7_vol_spike_breakout_min: float = Field(default=2.0) # vol_spike que bloquea todo apriete
    sso7_min_velocity: float = Field(default=0.0003)        # velocity mínima para considerar "avanzando" (ratio/h)
    sso7_stagnation_cycles_threshold: int = Field(default=3) # ciclos sin avance antes de apretar SL
    sso7_trail_base_dist: float = Field(default=0.010)      # distancia base del stagnation trail (1.0%)
    sso7_trail_squeeze_per_cycle: float = Field(default=0.10) # reducción de distancia por ciclo estancado
    sso7_trail_min_dist: float = Field(default=0.003)       # distancia mínima de trail (0.3% ≈ comisión)
    sso7_opp_enabled: bool = Field(default=True)            # activar Opportunity Collapse
    sso7_opp_min_cycles_stagnant: int = Field(default=3)    # ciclos mínimos estancados para Opp Collapse
    sso7_opp_ev_min_ratio: float = Field(default=1.35)      # EV candidato / EV posición actual
    sso7_opp_prob_min_delta: float = Field(default=0.06)    # prob candidato - prob posición actual
    sso7_opp_min_strength: float = Field(default=0.55)      # strength mínima del candidato
    sso7_opp_min_vol_spike: float = Field(default=1.2)      # vol_spike mínimo del candidato
    
    # Currency conversion configuration
    usd_to_mxn_rate: float = Field(default=17.0)  # Default USD to MXN exchange rate
    
    # Database configuration
    db_url: str = Field(default="")
    db_user: str = Field(default="")
    db_password: str = Field(default="")
    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_name: str = Field(default="crypto_agent")



def _validate_settings(settings: Settings) -> None:
    # Tests/import checks can boot the app without live secrets.
    # Runtime remains strict unless explicitly opted out.
    if os.environ.get("PYTEST_CURRENT_TEST") or str(os.environ.get("ALLOW_MISSING_SECRETS", "")).strip().lower() in {"1", "true", "yes"}:
        return

    required_fields = {
        "BINANCE_API_KEY": settings.binance_api_key,
        "BINANCE_API_SECRET": settings.binance_api_secret,
        "TRADINGVIEW_WEBHOOK_SECRET": settings.tradingview_webhook_secret,
    }
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required secrets: " + ", ".join(missing) + ". "
            "Set them via environment variables, DOTENV_PATH, or SECRETS_FILE_PATH."
        )


def get_settings() -> Settings:
    settings = Settings()
    _validate_settings(settings)
    return settings


# Export constants for backward compatibility
TRADE_CYCLE_MINUTES = 5  # Default value, will be overridden by settings



