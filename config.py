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
    dry_run: bool = Field(default=True, env="DRY_RUN")
    allow_real_trading: bool = Field(default=False, env="ALLOW_REAL_TRADING")
    trade_cycle_minutes: int = Field(default=5, env="TRADE_CYCLE_MINUTES")
    tradingview_webhook_secret: str = Field(default="", env="TRADINGVIEW_WEBHOOK_SECRET")
    agent_mode: str = Field(default="suggest", env="AGENT_MODE")
    default_trade_size: float = Field(default=10.0, env="DEFAULT_TRADE_SIZE")
    default_symbol: str = Field(default="BTCUSDT", env="DEFAULT_SYMBOL")
    news_lookback_hours: int = Field(default=24, env="NEWS_LOOKBACK_HOURS")

    class Config:
        env_file = _resolve_env_file()
        env_file_encoding = "utf-8"


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
