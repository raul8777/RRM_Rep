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
    
    # Portfolio Manager configuration
    portfolio_allocation_pct: float = Field(default=100.0, env="PORTFOLIO_ALLOCATION_PCT")
    min_pair_confidence_score: float = Field(default=50.0, env="MIN_PAIR_CONFIDENCE_SCORE")
    max_risk_per_pair_pct: float = Field(default=10.0, env="MAX_RISK_PER_PAIR_PCT")
    diversification_factor: float = Field(default=1.0, env="DIVERSIFICATION_FACTOR")
    enable_position_closure: bool = Field(default=True, env="ENABLE_POSITION_CLOSURE")
    
    # Autonomous signal generation configuration
    analysis_symbols: list = Field(default=[
        'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
        'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT',
        'SUIUSDT', 'PEPEUSDT', 'FETUSDT', 'TAOUSDT', 'RENDERUSDT', 'WIFUSDT'
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
    protect_profit_min_pct: float = Field(default=2.5, env="PROTECT_PROFIT_MIN_PCT")              # minimum PnL% to protect

    # Signal quality filter
    min_signal_strength: float = Field(default=0.35, env="MIN_SIGNAL_STRENGTH")  # 0.0 = accept all, 0.5 = moderate filter

    # Narrative token classification (used by volspike override + bootstrap)
    narrative_tokens: list = Field(default=[
        'FETUSDT', 'TAOUSDT', 'RENDERUSDT', 'NEARUSDT', 'WLDUSDT',
        'AGIXUSDT', 'OCEANUSDT',
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
    }, env="TOKEN_CATEGORIES")

    # Win-rate thresholds per market_behavior category
    # min_prob: mínima probabilidad de éxito requerida
    # min_pnl:  mínimo PnL esperado (USDT) — negativo permitido para NARRATIVE
    # min_volspike: VolSpike mínimo para confirmar señal (informativo, no enforcement aquí)
    category_thresholds: dict = Field(default={
        "LARGE_CAP":  {"min_prob": 0.55, "min_pnl": 0.10, "min_volspike": 1.2, "stop_loss_pct": 1.5, "take_profit_pct": 2.5, "trailing_activation_pct": 1.5, "trailing_distance_pct": 1.0},
        "MIDCAP_ALT": {"min_prob": 0.50, "min_pnl": 0.00, "min_volspike": 1.5, "stop_loss_pct": 1.5, "take_profit_pct": 2.5, "trailing_activation_pct": 2.0, "trailing_distance_pct": 1.2},
        "NARRATIVE":  {"min_prob": 0.42, "min_pnl": -5.0, "min_volspike": 1.5, "stop_loss_pct": 2.5, "take_profit_pct": 4.0, "trailing_activation_pct": 3.0, "trailing_distance_pct": 1.5},
        "MEME":       {"min_prob": 0.52, "min_pnl": 0.05, "min_volspike": 2.5, "stop_loss_pct": 2.0, "take_profit_pct": 3.5, "trailing_activation_pct": 2.5, "trailing_distance_pct": 1.5},
    }, env="CATEGORY_THRESHOLDS")
    
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
