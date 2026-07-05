"""
Component Initialisation Module

Centralises the creation and wiring of all singleton service objects so that
app.py, run_trades.py and analysis_cycle.py all use the same instances
instead of creating their own.

Usage
-----
    from init_components import get_components
    components = get_components()
    binance  = components.binance_client
    position = components.position_manager
    ...
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Lazy singleton ────────────────────────────────────────────────────────────
_components = None


@dataclass
class Components:
    binance_client: object = None
    news_client: object = None
    db_client: object = None
    position_manager: object = None
    portfolio_manager: object = None
    orchestrator: object = None
    settings: object = None


def _build() -> Components:
    """Construct every component exactly once."""
    from config import get_settings
    from config_helpers import validate_config
    settings = get_settings()

    validation = validate_config(settings)
    if not validation.get("ok", False):
        raise RuntimeError(f"Config validation failed: {validation['issues']}")

    c = Components(settings=settings)

    # ── Binance client ────────────────────────────────────────────────────────
    try:
        from binance_client import BinanceClient
        c.binance_client = BinanceClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_use_testnet,
            dry_run=settings.dry_run,
        )
        logger.info("BinanceClient initialised")
    except Exception as e:
        logger.error(f"BinanceClient init failed: {e}")

    # ── News client ───────────────────────────────────────────────────────────
    try:
        from news_client import NewsClient
        c.news_client = NewsClient()
        logger.info("NewsClient initialised")
    except Exception as e:
        logger.error(f"NewsClient init failed: {e}")

    # ── Database client ───────────────────────────────────────────────────────
    try:
        db_url = getattr(settings, "db_url", None)
        if not db_url:
            db_url = (
                f"postgresql://{settings.db_user}:{settings.db_password}"
                f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
            )
        if db_url:
            from db_client import get_db_client
            c.db_client = get_db_client(db_url)
            logger.info("DatabaseClient initialised")
            try:
                from config_helpers import load_runtime_overrides_from_db
                _restore = load_runtime_overrides_from_db(settings, c.db_client)
                if _restore.get("applied_scalars") or _restore.get("applied_thresholds"):
                    logger.info(
                        "Runtime overrides restored: scalars=%s thresholds=%s",
                        _restore.get("applied_scalars"),
                        _restore.get("applied_thresholds"),
                    )
            except Exception as _re:
                logger.warning("Runtime override restore skipped: %s", _re)
    except Exception as e:
        logger.error(f"DatabaseClient init failed: {e}")

    # ── Position Manager ──────────────────────────────────────────────────────
    try:
        from position_manager import PositionManager
        c.position_manager = PositionManager(db_client=c.db_client)
        logger.info("PositionManager initialised")
    except Exception as e:
        logger.error(f"PositionManager init failed: {e}")

    # ── Portfolio Manager ─────────────────────────────────────────────────────
    try:
        from portfolio_manager import PortfolioManager
        c.portfolio_manager = PortfolioManager(
            c.binance_client,
            settings,
            position_manager=c.position_manager,
        )
        logger.info("PortfolioManager initialised")
    except Exception as e:
        logger.error(f"PortfolioManager init failed: {e}")

    # ── Orchestrator ──────────────────────────────────────────────────────────
    try:
        from orchestrator import Orchestrator
        c.orchestrator = Orchestrator(db_client=c.db_client)
        c.orchestrator.set_position_manager(c.position_manager)
        c.orchestrator.set_portfolio_manager(c.portfolio_manager)
        logger.info("Orchestrator initialised")
    except Exception as e:
        logger.error(f"Orchestrator init failed: {e}")

    return c


def get_components() -> Components:
    """Return the process-wide singleton Components instance (creates on first call)."""
    global _components
    if _components is None:
        _components = _build()
    return _components


def reset_components():
    """Force re-initialisation on next get_components() call (useful for tests)."""
    global _components
    _components = None
