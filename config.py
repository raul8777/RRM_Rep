import json
import os
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings


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
    binance_api_key: str = Field(default="", env="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", env="BINANCE_API_SECRET")
    binance_use_testnet: bool = Field(default=True, env="BINANCE_USE_TESTNET")
    # Risk and operation controls
    max_daily_loss_usdt: float = Field(default=100.0, env="MAX_DAILY_LOSS_USDT")
    max_per_trade_usdt: float = Field(default=10.0, env="MAX_PER_TRADE_USDT")
    max_positions: int = Field(default=5, env="MAX_POSITIONS")
    max_positions_per_symbol: int = Field(default=3, env="MAX_POSITIONS_PER_SYMBOL")
    dry_run: bool = Field(default=True, env="DRY_RUN")
    allow_real_trading: bool = Field(default=False, env="ALLOW_REAL_TRADING")
    trade_cycle_minutes: int = Field(default=5, env="TRADE_CYCLE_MINUTES")
    trade_horizon_minutes: int = Field(default=240, env="TRADE_HORIZON_MINUTES")  # realistic hold time for PnL prediction (4h)
    # Kelly dynamic position sizing
    kelly_enabled: bool = Field(default=True, env="KELLY_ENABLED")
    kelly_base_usdt: float = Field(default=60.0, env="KELLY_BASE_USDT")
    kelly_max_usdt: float = Field(default=350.0, env="KELLY_MAX_USDT")           # floor: never less than this
    kelly_max_pct_cap: float = Field(default=0.25, env="KELLY_MAX_PCT_CAP")       # Smart Kelly: max = max(floor, liquid * this)
    kelly_max_pct_liquid: float = Field(default=0.40, env="KELLY_MAX_PCT_LIQUID")
    kelly_dynamic_base_pct: float = Field(default=0.15, env="KELLY_DYNAMIC_BASE_PCT")  # base = capital * this (if > fixed base)
    # Bear market mode
    bear_market_mode_enabled: bool = Field(default=True, env="BEAR_MARKET_MODE_ENABLED")
    bear_market_btc_7d_threshold: float = Field(default=-10.0, env="BEAR_MARKET_BTC_7D_THRESHOLD")  # % change
    bear_market_kelly_multiplier: float = Field(default=0.5, env="BEAR_MARKET_KELLY_MULTIPLIER")  # reduce Kelly
    bear_market_volspike_min: float = Field(default=2.0, env="BEAR_MARKET_VOLSPIKE_MIN")  # stricter spike req
    # SPOT_BULL PnL boost — relax predictor thresholds when market is in confirmed uptrend
    spot_bull_pnl_boost_enabled: bool = Field(default=True, env="SPOT_BULL_PNL_BOOST_ENABLED")
    spot_bull_pnl_boost_large_cap: float = Field(default=0.0, env="SPOT_BULL_PNL_BOOST_LARGE_CAP")    # BTC/ETH: allow up to 0 PnL
    spot_bull_pnl_boost_midcap: float = Field(default=-0.10, env="SPOT_BULL_PNL_BOOST_MIDCAP")        # alts: allow -0.10
    spot_bull_btc_4h_up_required: bool = Field(default=True, env="SPOT_BULL_BTC_4H_UP_REQUIRED")      # only apply when BTC 4h=UP
    # Regime whipsaw protection — minimum consecutive PRESERVATION cycles before closing shorts
    min_cycles_before_close_shorts: int = Field(default=5, env="MIN_CYCLES_BEFORE_CLOSE_SHORTS")  # H-FIX: 3→5 evita cierre prematuro en oscilaciones de régimen
    # Protect profitable shorts on regime change — only close if unrealized PnL < this threshold (USD)
    futures_protect_profit_usdt: float = Field(default=-5.0, env="FUTURES_PROTECT_PROFIT_USDT")  # H-FIX: 1.0→-5.0 — solo cierra en regime change si pérdida > $5
    # Aggressive exposure in SPOT_BULL + F&G Fear: use higher cap when conditions are optimal
    max_total_exposure_spot_bull_fear: float = Field(default=0.80, env="MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR")
    max_total_exposure_spot_bull_fear_fng_threshold: int = Field(default=40, env="MAX_TOTAL_EXPOSURE_SPOT_BULL_FEAR_FNG_THRESHOLD")
    # Futures: no short when BTC 4h candle just moved up strongly (avoid shorting into momentum)
    futures_no_short_btc_4h_threshold: float = Field(default=2.0, env="FUTURES_NO_SHORT_BTC_4H_THRESHOLD")  # %
    # Futures: max large-cap SHORTs (BTC/ETH/BNB/SOL) — slots above this must be MIDs/NARRATIVE for diversification
    futures_max_largecap_shorts: int = Field(default=5, env="FUTURES_MAX_LARGECAP_SHORTS")
    futures_largecap_symbols: list = Field(default=['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'], env="FUTURES_LARGECAP_SYMBOLS")
    # Scale-out on take-profit
    scale_out_enabled: bool = Field(default=True, env="SCALE_OUT_ENABLED")
    scale_out_first_close_pct: float = Field(default=0.5, env="SCALE_OUT_FIRST_CLOSE_PCT")  # close 50% at TP
    # Smart TP: analyse momentum at TP before selling — extend if bullish, scale-out if neutral, close if bearish
    smart_tp_enabled: bool = Field(default=True, env="SMART_TP_ENABLED")
    smart_tp_max_extensions: int = Field(default=3, env="SMART_TP_MAX_EXTENSIONS")       # max extensions per position
    smart_tp_max_pnl_pct: float = Field(default=15.0, env="SMART_TP_MAX_PNL_PCT")        # never extend above this % gain
    smart_tp_extend_score_threshold: int = Field(default=2, env="SMART_TP_EXTEND_SCORE_THRESHOLD")  # min score/4 to EXTEND (was 3, now 2 = more aggressive)
    # Smart TP: regime-aware behaviour — disable EXTEND in flat/defensive regimes (maximize realized gains)
    smart_tp_extend_disabled_regimes: list = Field(default=["RANGE_SCALP", "PRESERVATION"], env="SMART_TP_EXTEND_DISABLED_REGIMES")
    # Futures Smart TP EXTEND: evaluate SHORT momentum at TP hit before closing
    futures_smart_tp_enabled: bool = Field(default=True, env="FUTURES_SMART_TP_ENABLED")
    futures_smart_tp_max_extensions: int = Field(default=2, env="FUTURES_SMART_TP_MAX_EXTENSIONS")  # fewer extensions than spot (leverage risk)
    futures_smart_tp_max_pnl_pct: float = Field(default=12.0, env="FUTURES_SMART_TP_MAX_PNL_PCT")  # cap at 12% PnL (2x leverage)
    futures_smart_tp_extend_score_threshold: int = Field(default=2, env="FUTURES_SMART_TP_EXTEND_SCORE_THRESHOLD")  # H-FIX: 3→2 (más extensiones TP = más ganancia por trade)
    futures_smart_tp_extension_pct: float = Field(default=2.0, env="FUTURES_SMART_TP_EXTENSION_PCT")  # extend TP by 2% of current price
    # Futures trailing stop: protect unrealized gains during position lifecycle
    futures_trailing_enabled: bool = Field(default=True, env="FUTURES_TRAILING_ENABLED")
    futures_trailing_activation_pct: float = Field(default=2.0, env="FUTURES_TRAILING_ACTIVATION_PCT")  # SL-OPT: 1.5→2.0 (earlier trailing compensates wider SL)
    futures_trailing_distance_pct: float = Field(default=1.5, env="FUTURES_TRAILING_DISTANCE_PCT")      # FIX-F4: trail SL/TP 1.5% from best price (was 1.0 — avoids premature exits on volatility)
    # ATR-scaled trailing: trail_dist = max(floor, min(ceiling, ATR_5m * multiplier))
    # This prevents stops from triggering on normal volatility wicks
    futures_trail_atr_enabled: bool = Field(default=True, env="FUTURES_TRAIL_ATR_ENABLED")
    futures_trail_atr_multiplier_low: float = Field(default=1.5, env="FUTURES_TRAIL_ATR_MULT_LOW")    # 1.5x ATR at 1.5-3% PnL
    futures_trail_atr_multiplier_mid: float = Field(default=1.2, env="FUTURES_TRAIL_ATR_MULT_MID")    # 1.2x ATR at 3-5% PnL
    futures_trail_atr_multiplier_high: float = Field(default=1.0, env="FUTURES_TRAIL_ATR_MULT_HIGH")  # 1.0x ATR at 5-8% PnL
    futures_trail_atr_multiplier_deep: float = Field(default=0.8, env="FUTURES_TRAIL_ATR_MULT_DEEP")  # 0.8x ATR at 8%+ PnL
    futures_trail_floor_pct: float = Field(default=0.4, env="FUTURES_TRAIL_FLOOR_PCT")   # absolute min trail dist %
    futures_trail_ceiling_pct: float = Field(default=1.2, env="FUTURES_TRAIL_CEILING_PCT")  # absolute max trail dist %
    futures_breakeven_activation_pct: float = Field(default=1.0, env="FUTURES_BREAKEVEN_ACTIVATION_PCT")  # FIX-T2C: 2.0→1.0 (breakeven fires BEFORE trailing 1.5%)
    # FIX-F1: Cooldown after futures SL hit — prevents re-entering same symbol immediately after stop out
    futures_sl_cooldown_hours: float = Field(default=4.0, env="FUTURES_SL_COOLDOWN_HOURS")  # PB6: 1h→4h — stop chasing after SL
    # PB1: Stagger entries — max new shorts per single cycle (de-correlates entries)
    futures_max_new_per_cycle: int = Field(default=3, env="FUTURES_MAX_NEW_PER_CYCLE")  # max 3 new shorts opened per 4min cycle
    # PB2: Mass SL circuit breaker — global pause after N+ positions stop in same check
    futures_mass_sl_threshold: int = Field(default=3, env="FUTURES_MASS_SL_THRESHOLD")  # trigger if 3+ positions stop together
    futures_mass_sl_cooldown_hours: float = Field(default=4.0, env="FUTURES_MASS_SL_COOLDOWN_HOURS")  # 4h global pause
    # PB3: Confirm SHORT entry with red 5m candle (don't short into green momentum)
    futures_confirm_red_candle: bool = Field(default=True, env="FUTURES_CONFIRM_RED_CANDLE")  # require last 5m candle red
    # PB5: BTC 3h rolling momentum filter — catches gradual rallies that 1h misses
    futures_no_short_btc_3h_threshold: float = Field(default=1.0, env="FUTURES_NO_SHORT_BTC_3H_THRESHOLD")  # block if BTC up >1% in 3h
    # FIX-F2: BTC 1h bounce filter — skip new SHORTs if BTC 1h candle is rising above threshold
    futures_no_short_btc_1h_threshold: float = Field(default=0.8, env="FUTURES_NO_SHORT_BTC_1H_THRESHOLD")  # 0.8% = meaningful bounce
    # PV1: Scale-out — close partial position at first profit milestone
    futures_scale_out_pct: float = Field(default=1.5, env="FUTURES_SCALE_OUT_PCT")  # trigger at +1.5% PnL
    futures_scale_out_fraction: float = Field(default=0.5, env="FUTURES_SCALE_OUT_FRACTION")  # close 50% of qty
    # PV2: Momentum reversal close — close remaining after peak drawback
    futures_momentum_reversal_enabled: bool = Field(default=True, env="FUTURES_MOMENTUM_REVERSAL_ENABLED")
    futures_momentum_reversal_min_peak: float = Field(default=2.0, env="FUTURES_MOMENTUM_REVERSAL_MIN_PEAK")  # peak must reach 2%+
    futures_momentum_reversal_drawback: float = Field(default=0.8, env="FUTURES_MOMENTUM_REVERSAL_DRAWBACK")  # close if given back 0.8% from peak
    # SSO (Smart Scale-Out) — adaptive multi-signal exit system
    sso_enabled: bool = Field(default=True, env="SSO_ENABLED")
    sso_stagnant_min_profit_factor: float = Field(default=0.02, env="SSO_STAGNANT_MIN_PROFIT_FACTOR")
    sso_stagnant_min_profit_floor: float = Field(default=3.0, env="SSO_STAGNANT_MIN_PROFIT_FLOOR")
    sso_stagnant_min_hours: float = Field(default=2.0, env="SSO_STAGNANT_MIN_HOURS")
    sso_stagnant_velocity_window_min: int = Field(default=30, env="SSO_STAGNANT_VELOCITY_WINDOW_MIN")  # SL-OPT: 60→30 — detect decay faster
    # SSO-1b: Profit Decay — close when position is actively giving back gains
    sso_decay_threshold_pct: float = Field(default=0.004, env="SSO_DECAY_THRESHOLD_PCT")     # 0.4% drawback from peak triggers close
    sso_decay_min_profit_usd: float = Field(default=2.0, env="SSO_DECAY_MIN_PROFIT_USD")     # min $2 unrealized to bother closing
    sso_decay_min_hours: float = Field(default=1.5, env="SSO_DECAY_MIN_HOURS")               # min 1.5h holding before decay check
    # SSO-1c: Slow Crawl — close when velocity too low to justify slot
    sso_slow_velocity_threshold: float = Field(default=0.001, env="SSO_SLOW_VELOCITY_THRESHOLD")  # 0.1%/h — below this, recycle capital
    sso_slow_min_profit_usd: float = Field(default=1.0, env="SSO_SLOW_MIN_PROFIT_USD")           # min $1 profit to close (don't close losses)
    sso_slow_min_hours: float = Field(default=2.0, env="SSO_SLOW_MIN_HOURS")                     # min 2h holding before slow check
    sso_slow_tier2_hours: float = Field(default=4.0, env="SSO_SLOW_TIER2_HOURS")               # 4h+: 1.5x threshold (0.15%/h)
    sso_slow_tier3_hours: float = Field(default=6.0, env="SSO_SLOW_TIER3_HOURS")               # 6h+: 3.5x threshold (0.35%/h)
    sso_slow_tier4_hours: float = Field(default=8.0, env="SSO_SLOW_TIER4_HOURS")               # 8h+: 5.0x threshold (0.50%/h)
    sso_momentum_rsi_overbought: float = Field(default=70.0, env="SSO_MOMENTUM_RSI_OVERBOUGHT")
    sso_momentum_rsi_oversold: float = Field(default=30.0, env="SSO_MOMENTUM_RSI_OVERSOLD")
    sso_momentum_min_pnl_pct_futures: float = Field(default=1.0, env="SSO_MOMENTUM_MIN_PNL_PCT_FUTURES")
    sso_momentum_min_pnl_pct_spot: float = Field(default=1.5, env="SSO_MOMENTUM_MIN_PNL_PCT_SPOT")
    sso_momentum_vol_dying_threshold: float = Field(default=0.5, env="SSO_MOMENTUM_VOL_DYING_THRESHOLD")
    sso_multilevel_enabled: bool = Field(default=True, env="SSO_MULTILEVEL_ENABLED")
    sso_multilevel_l1_atr_mult: float = Field(default=1.0, env="SSO_MULTILEVEL_L1_ATR_MULT")
    sso_multilevel_l2_atr_mult: float = Field(default=2.0, env="SSO_MULTILEVEL_L2_ATR_MULT")
    sso_multilevel_l3_atr_mult: float = Field(default=3.5, env="SSO_MULTILEVEL_L3_ATR_MULT")
    sso_multilevel_l1_close_pct: float = Field(default=0.30, env="SSO_MULTILEVEL_L1_CLOSE_PCT")
    sso_multilevel_l2_close_pct: float = Field(default=0.30, env="SSO_MULTILEVEL_L2_CLOSE_PCT")
    sso_multilevel_l3_close_pct: float = Field(default=0.25, env="SSO_MULTILEVEL_L3_CLOSE_PCT")
    sso_target_contribution_pct: float = Field(default=0.15, env="SSO_TARGET_CONTRIBUTION_PCT")
    sso_target_daily_usd: float = Field(default=40.0, env="SSO_TARGET_DAILY_USD")
    sso_target_min_hours: float = Field(default=1.0, env="SSO_TARGET_MIN_HOURS")
    sso_reentry_min_strength_diff: float = Field(default=0.3, env="SSO_REENTRY_MIN_STRENGTH_DIFF")
    sso_reentry_min_ev_multiplier: float = Field(default=1.5, env="SSO_REENTRY_MIN_EV_MULTIPLIER")
    # WR Screener: block buys on pairs with N consecutive stop_loss closes
    wr_screener_enabled: bool = Field(default=True, env="WR_SCREENER_ENABLED")
    wr_screener_consecutive_sl_block: int = Field(default=3, env="WR_SCREENER_CONSECUTIVE_SL_BLOCK")  # block after N consecutive SL
    # BTC dominance filter
    btc_dominance_filter_enabled: bool = Field(default=True, env="BTC_DOMINANCE_FILTER_ENABLED")
    btc_dominance_altcoin_threshold: float = Field(default=56.0, env="BTC_DOMINANCE_ALTCOIN_THRESHOLD")  # if BTC.D > this, skip alts
    tradingview_webhook_secret: str = Field(default="", env="TRADINGVIEW_WEBHOOK_SECRET")
    agent_mode: str = Field(default="suggest", env="AGENT_MODE")
    default_trade_size: float = Field(default=10.0, env="DEFAULT_TRADE_SIZE")
    default_symbol: str = Field(default="BTCUSDT", env="DEFAULT_SYMBOL")
    news_lookback_hours: int = Field(default=24, env="NEWS_LOOKBACK_HOURS")
    cryptocompare_api_key: str = Field(default="", env="CRYPTOCOMPARE_API_KEY")
    
    # Portfolio Manager configuration
    portfolio_allocation_pct: float = Field(default=100.0, env="PORTFOLIO_ALLOCATION_PCT")
    min_pair_confidence_score: float = Field(default=50.0, env="MIN_PAIR_CONFIDENCE_SCORE")
    max_risk_per_pair_pct: float = Field(default=10.0, env="MAX_RISK_PER_PAIR_PCT")
    diversification_factor: float = Field(default=1.0, env="DIVERSIFICATION_FACTOR")
    enable_position_closure: bool = Field(default=False, env="ENABLE_POSITION_CLOSURE")  # F3: disabled — premature DB close without selling in Binance; _close_prediction_based() in run_trades.py handles closures with full gate protection
    
    # Autonomous signal generation configuration
    analysis_symbols: list = Field(default=[
        'DOGEUSDT', 'RENDERUSDT', 'BNBUSDT', 'SOLUSDT', 'TAOUSDT',
        'FETUSDT', 'AVAXUSDT', 'ADAUSDT', 'XRPUSDT', 'DOTUSDT',
        'SUIUSDT', 'PEPEUSDT', 'WIFUSDT', 'BTCUSDT', 'ETHUSDT', 'LINKUSDT',
        'NEARUSDT', 'WLDUSDT', 'INJUSDT', 'AGIXUSDT',
        'ONDOUSDT', 'JUPUSDT', 'OPUSDT',  # M2: +3 NARRATIVE alta vol
        'HBARUSDT', 'AAVEUSDT',  # O5: nuevos pares validados vol24h>$10M
        # E1: Expansión Fase 1 (+7 tokens) - Optimización activos $3K capital
        'ARBUSDT', 'MATICUSDT', 'ATOMUSDT',  # L2 + Infra
        'GRTUSDT', 'AIOZUSDT',  # AI/Data Narrative
        'SEIUSDT', 'SUSHIUSDT',  # Infra + DeFi
    ])  # E1: Removido env=ANALYSIS_SYMBOLS para forzar lista de 32 tokens

    # VolSpike override: bypass PnL predictor for narrative tokens with strong volume
    volspike_override_enabled: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_ENABLED")
    volspike_override_min_spike: float = Field(default=1.5, env="VOLSPIKE_OVERRIDE_MIN_SPIKE")   # x avg volume
    volspike_override_max_rsi: float = Field(default=65.0, env="VOLSPIKE_OVERRIDE_MAX_RSI")      # RSI must be below this
    volspike_override_trend_required: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_TREND_REQUIRED")  # must be 4h UP
    volspike_override_narrative_only: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_NARRATIVE_ONLY")  # only for narrative tokens
    # V2: Controles adicionales para hacer override más selectivo (jun 16 2026)
    volspike_override_min_strength: float = Field(default=0.65, env="VOLSPIKE_OVERRIDE_MIN_STRENGTH")  # Requerir alta convicción técnica
    volspike_override_max_positions: int = Field(default=2, env="VOLSPIKE_OVERRIDE_MAX_POSITIONS")  # Máx 2 posiciones override simultáneas
    volspike_override_daily_limit: int = Field(default=3, env="VOLSPIKE_OVERRIDE_DAILY_LIMIT")      # Máx 3 overrides por día
    
    # Prediction-based position closure configuration
    min_pnl_improvement_threshold: float = Field(default=0.5, env="MIN_PNL_IMPROVEMENT_THRESHOLD")  # 50% improvement
    min_success_probability_improvement: float = Field(default=0.2, env="MIN_SUCCESS_PROBABILITY_IMPROVEMENT")  # 20% improvement
    enable_prediction_based_closing: bool = Field(default=True, env="ENABLE_PREDICTION_BASED_CLOSING")

    # Stop-Loss / Take-Profit configuration
    stop_loss_pct: float = Field(default=2.5, env="STOP_LOSS_PCT")       # % below entry price (SL-OPT: 1.8→2.5 — SSO era, reduce wick stops)
    take_profit_pct: float = Field(default=3.5, env="TAKE_PROFIT_PCT")   # % above entry price
    trailing_stop_enabled: bool = Field(default=True, env="TRAILING_STOP_ENABLED")
    trailing_stop_activation_pct: float = Field(default=2.0, env="TRAILING_STOP_ACTIVATION_PCT")  # activate trailing after +2%
    trailing_stop_distance_pct: float = Field(default=1.0, env="TRAILING_STOP_DISTANCE_PCT")      # trail 1% below peak
    protect_profitable_positions: bool = Field(default=True, env="PROTECT_PROFITABLE_POSITIONS")  # skip rebalance if PnL>1%
    protect_profit_min_pct: float = Field(default=0.3, env="PROTECT_PROFIT_MIN_PCT")              # minimum PnL% to protect (v3: 0.8→0.3 — protect desde micro-ganancia)
    # Rebalancing controls (v2)
    min_hold_minutes: int = Field(default=120, env="MIN_HOLD_MINUTES")         # min time before rebalancing out a position (v5: 240→120 — balance between hold and flexibility)
    min_hold_minutes_range_scalp: int = Field(default=30, env="MIN_HOLD_MINUTES_RANGE_SCALP")  # RANGE_SCALP: shorter hold (5min cycles × 6 = 30min)
    min_hold_minutes_spot_bull: int = Field(default=120, env="MIN_HOLD_MINUTES_SPOT_BULL")     # SPOT_BULL: 2h hold before replacement
    max_rebalance_per_cycle: int = Field(default=2, env="MAX_REBALANCE_PER_CYCLE")  # max sells per rebalance cycle
    rebalance_excluded_symbols: list = Field(default=['USD1USDT', 'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'FDUSDUSDT'],  # F4: stablecoins never rebalanced
                                             env="REBALANCE_EXCLUDED_SYMBOLS")
    max_total_exposure_pct: float = Field(default=0.65, env="MAX_TOTAL_EXPOSURE_PCT")  # max portfolio % in open spot positions
    # M1: ATR-Based dynamic SL — replaces fixed SL% with 1.5×ATR for volatile tokens
    atr_sl_enabled: bool = Field(default=True, env="ATR_SL_ENABLED")           # enable ATR-based SL calculation
    atr_sl_multiplier: float = Field(default=1.5, env="ATR_SL_MULTIPLIER")     # SL = max(category_sl, atr_mult × ATR14)
    atr_sl_max_pct: float = Field(default=0.08, env="ATR_SL_MAX_PCT")          # cap ATR SL at 8% to avoid monster stops
    atr_sl_min_pct: float = Field(default=0.015, env="ATR_SL_MIN_PCT")         # floor ATR SL at 1.5% (SL-OPT: 1%→1.5% — prevent micro-stops on volatile tokens)
    # M3: Kelly conviction multiplier — scale position size by signal strength
    kelly_conviction_enabled: bool = Field(default=True, env="KELLY_CONVICTION_ENABLED")
    kelly_conviction_vol2x: float = Field(default=1.5, env="KELLY_CONVICTION_VOL2X")   # vol 2-3x → 1.5x sizing
    kelly_conviction_vol3x: float = Field(default=2.0, env="KELLY_CONVICTION_VOL3X")   # vol >3x → 2.0x sizing
    kelly_conviction_max_pct: float = Field(default=0.10, env="KELLY_CONVICTION_MAX_PCT")  # hard cap: never >10% portfolio in 1 trade
    # Smart exit: anti-wick SL confirmation
    sl_confirmation_candles: int = Field(default=1, env="SL_CONFIRMATION_CANDLES")  # 0=immediate, 1=require last 1m candle close below SL
    # Smart exit: rebalance skips if predictor sees upside
    rebalance_skip_if_positive_prediction: bool = Field(default=True, env="REBALANCE_SKIP_IF_POSITIVE_PREDICTION")
    rebalance_min_hold_prob: float = Field(default=0.45, env="REBALANCE_MIN_HOLD_PROB")  # skip rebalance if prob >= this AND epnl > 0 (v3: 0.55→0.45)
    rebalance_skip_if_bullish_signal: bool = Field(default=True, env="REBALANCE_SKIP_IF_BULLISH_SIGNAL")  # require technical negative signal before rebalancing
    rebalance_rsi_overbought: float = Field(default=60.0, env="REBALANCE_RSI_OVERBOUGHT")  # RSI1h threshold — rebalance only if RSI > this OR EMA9 descending
    # Dynamic max positions by regime (replaces single MAX_POSITIONS when regime is known)
    max_positions_spot_bull: int = Field(default=20, env="MAX_POSITIONS_SPOT_BULL")    # jun17: 15→20 (32 activos disponibles)
    max_positions_range_scalp: int = Field(default=10, env="MAX_POSITIONS_RANGE_SCALP")  # jun17: 8→10
    max_positions_preservation: int = Field(default=5, env="MAX_POSITIONS_PRESERVATION")
    max_positions_futures_short: int = Field(default=4, env="MAX_POSITIONS_FUTURES_SHORT")
    # Active replacement: swap weakest open position for a better new signal
    replace_only_losing: bool = Field(default=True, env="REPLACE_ONLY_LOSING")  # only replace positions in loss/flat
    replace_ev_min_ratio: float = Field(default=1.1, env="REPLACE_EV_MIN_RATIO")  # MEJO-1: 1.3→1.1 — more permissive given small EV values
    replace_strength_min_delta: float = Field(default=0.05, env="REPLACE_STRENGTH_MIN_DELTA")  # MEJO-2: 0.15→0.05 — BUG-B fix: avg is now real, not hardcoded 0.5
    # Overflow slots: allow extra positions beyond regime max when signal quality is exceptional
    overflow_min_strength: float = Field(default=0.80, env="OVERFLOW_MIN_STRENGTH")  # Kelly FUERTE tier threshold
    overflow_min_prob: float = Field(default=0.65, env="OVERFLOW_MIN_PROB")          # Kelly FUERTE tier threshold
    overflow_max_extra: int = Field(default=3, env="OVERFLOW_MAX_EXTRA")             # max simultaneous overflow slots
    # ── Behavioral Finance: F&G Kelly multiplier (agresivo) ─────────────────
    fng_kelly_boost_enabled: bool = Field(default=True, env="FNG_KELLY_BOOST_ENABLED")
    fng_extreme_fear_threshold: int = Field(default=20, env="FNG_EXTREME_FEAR_THRESHOLD")   # <= this → boost
    fng_capitulation_threshold: int = Field(default=15, env="FNG_CAPITULATION_THRESHOLD")   # <= this → max boost
    fng_extreme_greed_threshold: int = Field(default=75, env="FNG_EXTREME_GREED_THRESHOLD") # >= this → reduce
    fng_extreme_fear_multiplier: float = Field(default=1.35, env="FNG_EXTREME_FEAR_MULTIPLIER")    # Extreme Fear: +35%
    fng_capitulation_multiplier: float = Field(default=1.50, env="FNG_CAPITULATION_MULTIPLIER")   # Capitulacion: +50%
    fng_extreme_greed_multiplier: float = Field(default=0.65, env="FNG_EXTREME_GREED_MULTIPLIER") # Extreme Greed: -35%
    fng_capitulation_pnl_boost: float = Field(default=-0.25, env="FNG_CAPITULATION_PNL_BOOST")    # relax min_pnl hasta -$0.25 en capitulacion

    # ── Microestructura: liquidez mínima y slippage implícito ───────────────
    liquidity_filter_enabled: bool = Field(default=True, env="LIQUIDITY_FILTER_ENABLED")
    min_liquidity_vol_usdt: float = Field(default=3_000_000.0, env="MIN_LIQUIDITY_VOL_USDT")  # vol 24h mínimo
    max_slippage_tp_ratio: float = Field(default=0.25, env="MAX_SLIPPAGE_TP_RATIO")           # spread/TP max (0.25 = 25% del TP)

    # ── Correlaciones: límite de posiciones por categoría ──────────────────
    category_max_positions_enabled: bool = Field(default=True, env="CATEGORY_MAX_POSITIONS_ENABLED")
    category_max_positions: dict = Field(default={
        "LARGE_CAP":  3,   # BTC/ETH/BNB — max 3
        "MIDCAP_ALT": 8,   # E2: 7→8 slots (12 midcap disponibles en screener)
        "NARRATIVE":  6,   # E2: 5→6 slots (10 AI/data narratives disponibles)
        "MEME":       2,   # DOGE/PEPE/WIF — max 2 (alta correlación)
    }, env="CATEGORY_MAX_POSITIONS")

    # ── Circuit breaker técnico (solo fallas de sistema, no pérdidas) ───────
    circuit_breaker_enabled: bool = Field(default=True, env="CIRCUIT_BREAKER_ENABLED")
    circuit_breaker_consecutive_failures: int = Field(default=5, env="CIRCUIT_BREAKER_CONSECUTIVE_FAILURES")
    circuit_breaker_pause_minutes: int = Field(default=10, env="CIRCUIT_BREAKER_PAUSE_MINUTES")

    # ── On-chain: funding rate como señal de sobrecompra para spot ──────────
    funding_rate_spot_filter_enabled: bool = Field(default=True, env="FUNDING_RATE_SPOT_FILTER_ENABLED")
    funding_rate_spot_max: float = Field(default=0.0008, env="FUNDING_RATE_SPOT_MAX")  # > 0.08% = longs sobreapal.
    funding_rate_cache_minutes: int = Field(default=10, env="FUNDING_RATE_CACHE_MINUTES")

    # Dynamic cycle by regime (v2)
    cycle_spot_bull: int = Field(default=10, env="CYCLE_SPOT_BULL")            # jun17: 20→10 min (+47% throughput señales)
    cycle_range_scalp: int = Field(default=5, env="CYCLE_RANGE_SCALP")         # minutes per cycle in RANGE_SCALP
    cycle_preservation: int = Field(default=45, env="CYCLE_PRESERVATION")      # jun17: 30→45 min (menos ciclos en bajista = menos comisiones)
    cycle_futures_short: int = Field(default=4, env="CYCLE_FUTURES_SHORT")     # H-FIX: 6→4 min (trailing más reactivo a movimientos rápidos)

    # Signal quality filter
    min_signal_strength: float = Field(default=0.55, env="MIN_SIGNAL_STRENGTH")  # v4 OPT: 0.35→0.55 (+$570 USDT/mes validado en sim)

    # Narrative token classification (used by volspike override + bootstrap)
    narrative_tokens: list = Field(default=[
        'FETUSDT', 'TAOUSDT', 'RENDERUSDT', 'NEARUSDT', 'WLDUSDT',
        'AGIXUSDT', 'OCEANUSDT', 'INJUSDT',
        # E1: Nuevos narratives AI/Data para rotación sectorial
        'GRTUSDT', 'AIOZUSDT',
        # MEJO-J: Sincronizados con token_categories market_behavior=NARRATIVE
        'JTOUSDT',
    ], env="NARRATIVE_TOKENS")

    # ── Token classification 2D ───────────────────────────────────────────────
    # Dimension A: market_behavior (cómo se mueve)
    # Dimension B: origin (por qué existe)
    token_categories: dict = Field(default={
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
        "HBARUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"},  # O5: Hedera, $15.7M vol/día
        # E1: Nuevos tokens Fase 1 (expansión activos)
        "ARBUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "L2"},            # Arbitrum L2
        "MATICUSDT":  {"market_behavior": "MIDCAP_ALT",  "origin": "L2"},            # Polygon L2
        "ATOMUSDT":   {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Cosmos infra
        "GRTUSDT":    {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},        # The Graph indexing
        "AIOZUSDT":   {"market_behavior": "NARRATIVE",   "origin": "AI_DATA"},        # DePIN + AI streaming
        "SEIUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # Sei trading-optimized L1
        "SUSHIUSDT":  {"market_behavior": "MIDCAP_ALT",  "origin": "DEFI"},           # SushiSwap DEX
        "ONDOUSDT":   {"market_behavior": "NARRATIVE",   "origin": "DEFI"},            # Ondo Finance RWA
        "TRXUSDT":    {"market_behavior": "MIDCAP_ALT",  "origin": "INFRASTRUCTURE"}, # TRON
    }, env="TOKEN_CATEGORIES")

    # Símbolos explícitamente excluidos del trading (oro tokenizado, stablecoins, delisted)
    # Estos son bloqueados antes de cualquier análisis en _buy_top_symbols
    trading_excluded_symbols: list = Field(default=[
        "XAUTUSDT",   # XAU Tether — oro tokenizado, SL/TP% no aplica
        "PAXGUSDT",   # PAX Gold — oro tokenizado
        "RLUSDUSDT",  # RLUSD — stablecoin sin upside
        "USD1USDT",   # USD1 — stablecoin sin upside
        "SPCXBUSDT",  # SPCXB — volumen artificial
    ], env="TRADING_EXCLUDED_SYMBOLS")

    # Win-rate thresholds per market_behavior category
    # min_prob: mínima probabilidad de éxito requerida
    # min_pnl:  mínimo PnL esperado (USDT) — negativo permitido para NARRATIVE
    # min_volspike: VolSpike mínimo para confirmar señal (informativo, no enforcement aquí)
    # MEJO-A: NARRATIVE trailing aumentado (8%/3%) para capturar breakouts +15-30% completos
    category_thresholds: dict = Field(default={
        "LARGE_CAP":  {"min_prob": 0.55, "min_pnl": 0.10, "min_volspike": 1.2, "stop_loss_pct": 2.5, "take_profit_pct": 3.5, "trailing_activation_pct": 2.0, "trailing_distance_pct": 0.8, "tp_extension_pct": 1.5},  # SL-OPT: 1.8→2.5
        "MIDCAP_ALT": {"min_prob": 0.50, "min_pnl": 0.00, "min_volspike": 1.5, "stop_loss_pct": 2.5, "take_profit_pct": 3.5, "trailing_activation_pct": 2.0, "trailing_distance_pct": 0.8, "tp_extension_pct": 2.0},  # SL-OPT: 1.8→2.5
        "NARRATIVE":  {"min_prob": 0.45, "min_pnl": -1.0, "min_volspike": 1.5, "stop_loss_pct": 3.5, "take_profit_pct": 7.0, "trailing_activation_pct": 5.0, "trailing_distance_pct": 2.0, "tp_extension_pct": 3.0},  # V4: trail tighter (8/3→5/2) — SL stays 3.5%
        "MEME":       {"min_prob": 0.52, "min_pnl": 0.05, "min_volspike": 2.5, "stop_loss_pct": 2.5, "take_profit_pct": 4.5, "trailing_activation_pct": 2.0, "trailing_distance_pct": 1.0, "tp_extension_pct": 2.5},  # SL-OPT: 2.0→2.5
    }, env="CATEGORY_THRESHOLDS")
    
    # ── Predicción con prioridad absoluta (jun 17 2026) ──────────────────────
    # C1: Re-evaluación predictiva en cada ciclo — cierra posiciones cuando EV deteriora
    prediction_exit_enabled: bool = Field(default=True, env="PREDICTION_EXIT_ENABLED")
    min_hold_for_prediction_exit: int = Field(default=90, env="MIN_HOLD_FOR_PREDICTION_EXIT")  # minutos antes de evaluar cierre
    # C2: Segunda entrada permitida si predicción nueva es fuertemente mejor
    allow_prediction_based_add: bool = Field(default=True, env="ALLOW_PREDICTION_ADD")
    prediction_add_min_ev_ratio: float = Field(default=1.5, env="PREDICTION_ADD_MIN_EV_RATIO")  # nueva pred >= 1.5x la existente
    prediction_add_min_strength: float = Field(default=0.70, env="PREDICTION_ADD_MIN_STRENGTH")
    prediction_add_kelly_factor: float = Field(default=0.6, env="PREDICTION_ADD_KELLY_FACTOR")  # Kelly reducido para 2ª entrada
    # C3: Cooldowns en DB — previene doble entrada post-restart
    persist_cooldowns_enabled: bool = Field(default=True, env="PERSIST_COOLDOWNS_ENABLED")
    # C4: Cierre automático de activos prohibidos al arrancar
    auto_close_excluded_on_startup: bool = Field(default=True, env="AUTO_CLOSE_EXCLUDED_ON_STARTUP")

    # ── Futures trading (USDM, fapi.binance.com) ─────────────────────────────
    futures_enabled: bool = Field(default=False, env="FUTURES_ENABLED")  # must opt-in explicitly
    futures_leverage: int = Field(default=2, env="FUTURES_LEVERAGE")           # hard max 2x
    futures_max_shorts: int = Field(default=5, env="FUTURES_MAX_SHORTS")       # max simultaneous shorts
    futures_max_largecap_shorts: int = Field(default=5, env="FUTURES_MAX_LARGECAP_SHORTS")  # max LARGE_CAP shorts simultaneously (diversification guard — raise MAX_SHORTS but keep this at 5 to force MID-CAP slots)
    futures_usdt_per_trade: float = Field(default=50.0, env="FUTURES_USDT_PER_TRADE")  # PB4: 20→50 minimum margin floor per short
    futures_sl_pct: float = Field(default=7.0, env="FUTURES_SL_PCT")           # SL % above entry (SL-OPT: 2.5→7.0 — align with TP, SSO/breakeven protect early)
    futures_tp_pct: float = Field(default=7.0, env="FUTURES_TP_PCT")           # TP % below entry (A4+v4: 7.0%, EV +1.40%/trade)
    futures_min_btc_7d_drop: float = Field(default=-7.0, env="FUTURES_MIN_BTC_7D_DROP")  # only short in bear
    futures_symbols: list = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
    ], env="FUTURES_SYMBOLS")   # symbols eligible for shorting
    futures_largecap_symbols: list = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT',
    ], env="FUTURES_LARGECAP_SYMBOLS")   # LARGE_CAP futures symbols for category cap
    futures_no_short_btc_4h_threshold: float = Field(default=2.0, env="FUTURES_NO_SHORT_BTC_4H_THRESHOLD")
    # Futures overflow: allow extra SHORTs beyond futures_max_shorts when signal is exceptional
    futures_overflow_max_extra: int   = Field(default=3,    env="FUTURES_OVERFLOW_MAX_EXTRA")    # max extra simultaneous overflow SHORTs (3+3=6 total) — review after 1 week
    futures_overflow_min_strength: float = Field(default=0.65, env="FUTURES_OVERFLOW_MIN_STRENGTH")  # H-FIX: 0.75→0.65 (habilita overflow en condiciones de alta vol moderada)
    futures_overflow_min_vol_spike: float = Field(default=2.0,  env="FUTURES_OVERFLOW_MIN_VOL_SPIKE")  # H-FIX: 2.5→2.0 (más accesible para overflow en pánico moderado)
    futures_overflow_min_drop_24h: float  = Field(default=-3.0, env="FUTURES_OVERFLOW_MIN_DROP_24H")  # min 24h drop %
    # M1: Minimum strength to inject a symbol into the futures signal pool (filters weak HOLDs)
    futures_min_inject_strength: float = Field(default=0.40, env="FUTURES_MIN_INJECT_STRENGTH")
    # Futures Kelly dynamic cap
    futures_kelly_cap_pct: float = Field(default=0.20, env="FUTURES_KELLY_CAP_PCT")  # PB4: 15→20% of available futures margin (bigger positions)
    # Capital Bridge: bidirectional autonomous spot↔futures transfer
    futures_capital_bridge_enabled: bool = Field(default=True, env="FUTURES_CAPITAL_BRIDGE_ENABLED")
    futures_capital_bridge_min_transfer: float = Field(default=10.0, env="FUTURES_CAPITAL_BRIDGE_MIN_TRANSFER")  # min USDT to trigger transfer
    # Unified Portfolio Manager: PortfolioManager as capital director for both SPOT and Futures
    use_unified_portfolio_manager: bool = Field(default=False, env="USE_UNIFIED_PORTFOLIO_MANAGER")  # feature flag — false = legacy behavior
    futures_kelly_base_usdt: float = Field(default=100.0, env="FUTURES_KELLY_BASE_USDT")  # Kelly floor for Futures (vs kelly_base_usdt for SPOT)
    futures_kelly_max_usdt: float = Field(default=500.0, env="FUTURES_KELLY_MAX_USDT")    # Kelly ceiling for Futures (vs kelly_max_usdt for SPOT)
    futures_kelly_max_pct_liquid: float = Field(default=0.55, env="FUTURES_KELLY_MAX_PCT_LIQUID")  # liquid cap for Futures Kelly (vs kelly_max_pct_liquid=0.28 for SPOT)

    # ── Dynamic pair screener ─────────────────────────────────────────────────
    screener_enabled: bool = Field(default=True, env="SCREENER_ENABLED")
    screener_min_vol_usdt: float = Field(default=10_000_000, env="SCREENER_MIN_VOL_USDT")   # $10M spot min
    screener_futures_min_vol: float = Field(default=100_000_000, env="SCREENER_FUTURES_MIN_VOL")  # $100M futures min
    screener_max_pairs: int = Field(default=50, env="SCREENER_MAX_PAIRS")           # max dynamic pairs for spot
    screener_cache_minutes: int = Field(default=15, env="SCREENER_CACHE_MINUTES")   # TTL for screener cache
    screener_merge_with_static: bool = Field(default=True, env="SCREENER_MERGE_WITH_STATIC")

    # ── Screener v2: Top 100 Quality Assets ───────────────────────────────────
    screener_top_n_enabled: bool = Field(default=True, env="SCREENER_TOP_N_ENABLED")
    screener_top_n_count: int = Field(default=100, env="SCREENER_TOP_N_COUNT")  # Target: 100 quality assets
    screener_quality_min_vol_usdt: float = Field(default=10_000_000)  # $10M minimum volume
    screener_quality_max_volatility: float = Field(default=0.50)     # Max 50% 24h volatility
    screener_quality_min_price_usdt: float = Field(default=0.0001)   # Minimum price filter
    screener_quality_min_age_days: int = Field(default=90)             # Minimum token age

    # ── Futures LONG / hedge bidireccional ────────────────────────────────────
    futures_hedge_mode_enabled: bool = Field(default=True, env="FUTURES_HEDGE_MODE_ENABLED")
    futures_long_enabled: bool = Field(default=True, env="FUTURES_LONG_ENABLED")
    futures_max_longs: int = Field(default=3, env="FUTURES_MAX_LONGS")
    futures_long_sl_pct: float = Field(default=2.5, env="FUTURES_LONG_SL_PCT")
    futures_long_tp_pct: float = Field(default=5.0, env="FUTURES_LONG_TP_PCT")
    futures_long_in_range_scalp: bool = Field(default=True, env="FUTURES_LONG_IN_RANGE_SCALP")
    futures_long_in_spot_bull: bool = Field(default=True, env="FUTURES_LONG_IN_SPOT_BULL")
    futures_long_rsi_threshold: float = Field(default=35.0, env="FUTURES_LONG_RSI_THRESHOLD")

    # ── Market Regime adaptive mode ──────────────────────────────────────────
    market_regime_enabled: bool = Field(default=True, env="MARKET_REGIME_ENABLED")
    # RANGE_SCALP mode parameters
    range_scalp_sl_pct: float = Field(default=1.2, env="RANGE_SCALP_SL_PCT")
    range_scalp_tp_pct: float = Field(default=1.5, env="RANGE_SCALP_TP_PCT")
    range_scalp_kelly_mult: float = Field(default=0.7, env="RANGE_SCALP_KELLY_MULT")
    range_scalp_max_rsi: float = Field(default=40.0, env="RANGE_SCALP_MAX_RSI")   # only enter RSI < 40
    range_scalp_require_bb_lower: bool = Field(default=True, env="RANGE_SCALP_REQUIRE_BB_LOWER")
    # PRESERVATION mode parameters
    preservation_sl_pct: float = Field(default=1.5, env="PRESERVATION_SL_PCT")
    preservation_tp_pct: float = Field(default=3.0, env="PRESERVATION_TP_PCT")
    preservation_kelly_mult: float = Field(default=0.5, env="PRESERVATION_KELLY_MULT")
    preservation_narrative_only: bool = Field(default=True, env="PRESERVATION_NARRATIVE_ONLY")
    preservation_max_rsi: float = Field(default=65.0, env="PRESERVATION_MAX_RSI")

    # Binance commission configuration
    binance_commission_rate: float = Field(default=0.001, env="BINANCE_COMMISSION_RATE")  # 0.1% default
    use_bnb_for_commission: bool = Field(default=False, env="USE_BNB_FOR_COMMISSION")  # 0.05% if True
    
    # Currency conversion configuration
    usd_to_mxn_rate: float = Field(default=17.0, env="USD_TO_MXN_RATE")  # Default USD to MXN exchange rate
    
    # Database configuration
    db_url: str = Field(default="", env="DB_URL")
    db_user: str = Field(default="", env="DB_USER")
    db_password: str = Field(default="", env="DB_PASSWORD")
    db_host: str = Field(default="localhost", env="DB_HOST")
    db_port: int = Field(default=5432, env="DB_PORT")
    db_name: str = Field(default="crypto_agent", env="DB_NAME")

    class Config:
        env_file = _resolve_env_file()
        env_file_encoding = "utf-8"
        extra = "ignore"


def _validate_settings(settings: Settings) -> None:
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
