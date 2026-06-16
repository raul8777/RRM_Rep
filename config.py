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
    kelly_max_usdt: float = Field(default=150.0, env="KELLY_MAX_USDT")
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
    min_cycles_before_close_shorts: int = Field(default=3, env="MIN_CYCLES_BEFORE_CLOSE_SHORTS")
    # Futures: no short when BTC 4h candle just moved up strongly (avoid shorting into momentum)
    futures_no_short_btc_4h_threshold: float = Field(default=2.0, env="FUTURES_NO_SHORT_BTC_4H_THRESHOLD")  # %
    # Scale-out on take-profit
    scale_out_enabled: bool = Field(default=True, env="SCALE_OUT_ENABLED")
    scale_out_first_close_pct: float = Field(default=0.5, env="SCALE_OUT_FIRST_CLOSE_PCT")  # close 50% at TP
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
    enable_position_closure: bool = Field(default=True, env="ENABLE_POSITION_CLOSURE")
    
    # Autonomous signal generation configuration
    analysis_symbols: list = Field(default=[
        'DOGEUSDT', 'RENDERUSDT', 'BNBUSDT', 'SOLUSDT', 'TAOUSDT',
        'FETUSDT', 'AVAXUSDT', 'ADAUSDT', 'XRPUSDT', 'DOTUSDT',
        'SUIUSDT', 'PEPEUSDT', 'WIFUSDT', 'BTCUSDT', 'ETHUSDT', 'LINKUSDT',
        'NEARUSDT', 'WLDUSDT', 'INJUSDT', 'AGIXUSDT',
    ], env="ANALYSIS_SYMBOLS")

    # VolSpike override: bypass PnL predictor for narrative tokens with strong volume
    volspike_override_enabled: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_ENABLED")
    volspike_override_min_spike: float = Field(default=1.5, env="VOLSPIKE_OVERRIDE_MIN_SPIKE")   # x avg volume
    volspike_override_max_rsi: float = Field(default=65.0, env="VOLSPIKE_OVERRIDE_MAX_RSI")      # RSI must be below this
    volspike_override_trend_required: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_TREND_REQUIRED")  # must be 4h UP
    volspike_override_narrative_only: bool = Field(default=True, env="VOLSPIKE_OVERRIDE_NARRATIVE_ONLY")  # only for narrative tokens
    
    # Prediction-based position closure configuration
    min_pnl_improvement_threshold: float = Field(default=0.5, env="MIN_PNL_IMPROVEMENT_THRESHOLD")  # 50% improvement
    min_success_probability_improvement: float = Field(default=0.2, env="MIN_SUCCESS_PROBABILITY_IMPROVEMENT")  # 20% improvement
    enable_prediction_based_closing: bool = Field(default=True, env="ENABLE_PREDICTION_BASED_CLOSING")

    # Stop-Loss / Take-Profit configuration
    stop_loss_pct: float = Field(default=1.5, env="STOP_LOSS_PCT")       # % below entry price
    take_profit_pct: float = Field(default=2.5, env="TAKE_PROFIT_PCT")   # % above entry price
    trailing_stop_enabled: bool = Field(default=True, env="TRAILING_STOP_ENABLED")
    trailing_stop_activation_pct: float = Field(default=2.0, env="TRAILING_STOP_ACTIVATION_PCT")  # activate trailing after +2%
    trailing_stop_distance_pct: float = Field(default=1.0, env="TRAILING_STOP_DISTANCE_PCT")      # trail 1% below peak
    protect_profitable_positions: bool = Field(default=True, env="PROTECT_PROFITABLE_POSITIONS")  # skip rebalance if PnL>1%
    protect_profit_min_pct: float = Field(default=0.3, env="PROTECT_PROFIT_MIN_PCT")              # minimum PnL% to protect (v3: 0.8→0.3 — protect desde micro-ganancia)
    # Rebalancing controls (v2)
    min_hold_minutes: int = Field(default=240, env="MIN_HOLD_MINUTES")         # min time before rebalancing out a position (v4: 120→240 = 4h mínimo)
    max_rebalance_per_cycle: int = Field(default=2, env="MAX_REBALANCE_PER_CYCLE")  # max sells per rebalance cycle
    max_total_exposure_pct: float = Field(default=0.65, env="MAX_TOTAL_EXPOSURE_PCT")  # max portfolio % in open spot positions
    # Smart exit: anti-wick SL confirmation
    sl_confirmation_candles: int = Field(default=1, env="SL_CONFIRMATION_CANDLES")  # 0=immediate, 1=require last 1m candle close below SL
    # Smart exit: rebalance skips if predictor sees upside
    rebalance_skip_if_positive_prediction: bool = Field(default=True, env="REBALANCE_SKIP_IF_POSITIVE_PREDICTION")
    rebalance_min_hold_prob: float = Field(default=0.45, env="REBALANCE_MIN_HOLD_PROB")  # skip rebalance if prob >= this AND epnl > 0 (v3: 0.55→0.45)
    rebalance_skip_if_bullish_signal: bool = Field(default=True, env="REBALANCE_SKIP_IF_BULLISH_SIGNAL")  # require technical negative signal before rebalancing
    rebalance_rsi_overbought: float = Field(default=60.0, env="REBALANCE_RSI_OVERBOUGHT")  # RSI1h threshold — rebalance only if RSI > this OR EMA9 descending
    # Dynamic max positions by regime (replaces single MAX_POSITIONS when regime is known)
    max_positions_spot_bull: int = Field(default=15, env="MAX_POSITIONS_SPOT_BULL")
    max_positions_range_scalp: int = Field(default=8, env="MAX_POSITIONS_RANGE_SCALP")
    max_positions_preservation: int = Field(default=5, env="MAX_POSITIONS_PRESERVATION")
    max_positions_futures_short: int = Field(default=4, env="MAX_POSITIONS_FUTURES_SHORT")
    # Active replacement: swap weakest open position for a better new signal
    replace_only_losing: bool = Field(default=True, env="REPLACE_ONLY_LOSING")  # only replace positions in loss/flat
    replace_ev_min_ratio: float = Field(default=1.3, env="REPLACE_EV_MIN_RATIO")  # new EV must be >= worst_open_EV * this
    replace_strength_min_delta: float = Field(default=0.15, env="REPLACE_STRENGTH_MIN_DELTA")  # new strength must exceed avg open strength by this
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
        "MIDCAP_ALT": 5,   # SOL/AVAX/DOT — max 5
        "NARRATIVE":  4,   # FET/TAO/RENDER — max 4 (concentración temática)
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
    cycle_spot_bull: int = Field(default=15, env="CYCLE_SPOT_BULL")            # minutes per cycle in SPOT_BULL
    cycle_range_scalp: int = Field(default=5, env="CYCLE_RANGE_SCALP")         # minutes per cycle in RANGE_SCALP
    cycle_preservation: int = Field(default=30, env="CYCLE_PRESERVATION")      # minutes per cycle in PRESERVATION
    cycle_futures_short: int = Field(default=8, env="CYCLE_FUTURES_SHORT")     # minutes per cycle in FUTURES_SHORT

    # Signal quality filter
    min_signal_strength: float = Field(default=0.35, env="MIN_SIGNAL_STRENGTH")  # 0.0 = accept all, 0.5 = moderate filter

    # Narrative token classification (used by volspike override + bootstrap)
    narrative_tokens: list = Field(default=[
        'FETUSDT', 'TAOUSDT', 'RENDERUSDT', 'NEARUSDT', 'WLDUSDT',
        'AGIXUSDT', 'OCEANUSDT', 'INJUSDT',
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
    }, env="TOKEN_CATEGORIES")

    # Win-rate thresholds per market_behavior category
    # min_prob: mínima probabilidad de éxito requerida
    # min_pnl:  mínimo PnL esperado (USDT) — negativo permitido para NARRATIVE
    # min_volspike: VolSpike mínimo para confirmar señal (informativo, no enforcement aquí)
    category_thresholds: dict = Field(default={
        "LARGE_CAP":  {"min_prob": 0.55, "min_pnl": 0.10, "min_volspike": 1.2, "stop_loss_pct": 1.8, "take_profit_pct": 3.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 1.2},
        "MIDCAP_ALT": {"min_prob": 0.50, "min_pnl": 0.00, "min_volspike": 1.5, "stop_loss_pct": 1.8, "take_profit_pct": 3.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 1.2},
        "NARRATIVE":  {"min_prob": 0.42, "min_pnl": -5.0, "min_volspike": 1.5, "stop_loss_pct": 2.5, "take_profit_pct": 5.0, "trailing_activation_pct": 3.0, "trailing_distance_pct": 1.5},
        "MEME":       {"min_prob": 0.52, "min_pnl": 0.05, "min_volspike": 2.5, "stop_loss_pct": 2.0, "take_profit_pct": 4.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 1.5},
    }, env="CATEGORY_THRESHOLDS")
    
    # ── Futures trading (USDM, fapi.binance.com) ─────────────────────────────
    futures_enabled: bool = Field(default=False, env="FUTURES_ENABLED")  # must opt-in explicitly
    futures_leverage: int = Field(default=2, env="FUTURES_LEVERAGE")           # hard max 2x
    futures_max_shorts: int = Field(default=2, env="FUTURES_MAX_SHORTS")       # max simultaneous shorts
    futures_usdt_per_trade: float = Field(default=20.0, env="FUTURES_USDT_PER_TRADE")  # margin per short
    futures_sl_pct: float = Field(default=3.0, env="FUTURES_SL_PCT")           # SL % above entry
    futures_tp_pct: float = Field(default=5.0, env="FUTURES_TP_PCT")           # TP % below entry
    futures_min_btc_7d_drop: float = Field(default=-7.0, env="FUTURES_MIN_BTC_7D_DROP")  # only short in bear
    futures_symbols: list = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
    ], env="FUTURES_SYMBOLS")   # symbols eligible for shorting

    # ── Dynamic pair screener ─────────────────────────────────────────────────
    screener_enabled: bool = Field(default=True, env="SCREENER_ENABLED")
    screener_min_vol_usdt: float = Field(default=10_000_000, env="SCREENER_MIN_VOL_USDT")   # $10M spot min
    screener_futures_min_vol: float = Field(default=100_000_000, env="SCREENER_FUTURES_MIN_VOL")  # $100M futures min
    screener_max_pairs: int = Field(default=50, env="SCREENER_MAX_PAIRS")           # max dynamic pairs for spot
    screener_cache_minutes: int = Field(default=15, env="SCREENER_CACHE_MINUTES")   # TTL for screener cache
    screener_merge_with_static: bool = Field(default=True, env="SCREENER_MERGE_WITH_STATIC")

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
