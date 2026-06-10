from fastapi import FastAPI, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel
from typing import Dict, Optional

from agent import TradingAgent
from binance_client import BinanceClient
from config import get_settings
from news_client import NewsClient
from signal_store import add_signal, get_signals
from strategy import build_order_params, parse_signal
import threading
import run_trades
import analysis_cycle
from analysis_cache import analysis_cache

settings = get_settings()
app = FastAPI(title="Binance Trading Agent")

binance_client = BinanceClient(
    api_key=settings.binance_api_key,
    api_secret=settings.binance_api_secret,
    testnet=settings.binance_use_testnet,
    dry_run=settings.dry_run,
)
news_client = NewsClient()
agent = TradingAgent(binance_client=binance_client, news_client=news_client, settings=settings)


def _start_trade_cycle():
    try:
        interval = int(getattr(settings, 'trade_cycle_minutes', 5))
    except Exception:
        interval = 5
    t = threading.Thread(target=run_trades.run_loop, args=(interval,), daemon=True)
    t.start()


def _start_analysis_cycle():
    """Start background technical analysis cycle (every 15 minutes)."""
    t = threading.Thread(target=analysis_cycle.run_loop, daemon=True)
    t.start()


@app.on_event("startup")
def startup_event():
    # Start background trade cycle only if agent is in auto mode
    if settings.agent_mode == 'auto':
        _start_trade_cycle()
    # Always start technical analysis cycle
    _start_analysis_cycle()


class TradingViewPayload(BaseModel):
    symbol: Optional[str] = None
    action: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    comment: Optional[str] = None
    message: Optional[str] = None
    strategy: Optional[str] = None


class CommandPayload(BaseModel):
    mode: str


@app.get("/status")
async def status() -> Dict[str, str]:
    return {
        "status": "ok",
        "agent_mode": settings.agent_mode,
        "binance_testnet": str(settings.binance_use_testnet),
        "signal_count": str(len(get_signals())),
    }


@app.get("/signals")
async def signals() -> Dict[str, object]:
    return {
        "signals": get_signals(),
    }


@app.post("/webhook")
async def webhook(payload: TradingViewPayload, request: Request) -> Dict:
    secret = request.headers.get("X-Webhook-Secret")
    if secret != settings.tradingview_webhook_secret:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized webhook")

    payload_dict = payload.dict()
    try:
        signal = parse_signal(payload_dict)
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))

    add_signal(signal)
    result = agent.process_signal(payload_dict)
    result["signal_registered"] = True
    return result


@app.post("/command")
async def command(payload: CommandPayload) -> Dict[str, str]:
    if payload.mode not in {"suggest", "auto"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid mode")
    settings.agent_mode = payload.mode
    return {"agent_mode": settings.agent_mode}


@app.get("/analysis")
async def get_analysis() -> Dict:
    """Get all technical analysis results for tracked symbols."""
    cache_data = analysis_cache.get_all()
    return {
        "analysis_results": cache_data,
        "count": len(cache_data),
    }


@app.get("/analysis/{symbol}")
async def get_analysis_for_symbol(symbol: str) -> Dict:
    """Get technical analysis for a specific symbol."""
    normalized = symbol.upper().replace("/", "")
    result = analysis_cache.get(normalized)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No analysis found for {symbol}")
    return {
        "symbol": normalized,
        **result,
    }
