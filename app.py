from fastapi import FastAPI, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel
from typing import Dict, Optional

from agent import TradingAgent
from binance_client import BinanceClient
from config import get_settings
from news_client import NewsClient

settings = get_settings()
app = FastAPI(title="Binance Trading Agent")

binance_client = BinanceClient(
    api_key=settings.binance_api_key,
    api_secret=settings.binance_api_secret,
    testnet=settings.binance_use_testnet,
)
news_client = NewsClient()
agent = TradingAgent(binance_client=binance_client, news_client=news_client, settings=settings)


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
    }


@app.post("/webhook")
async def webhook(payload: TradingViewPayload, request: Request) -> Dict:
    secret = request.headers.get("X-Webhook-Secret")
    if secret != settings.tradingview_webhook_secret:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized webhook")
    return agent.process_signal(payload.dict())


@app.post("/command")
async def command(payload: CommandPayload) -> Dict[str, str]:
    if payload.mode not in {"suggest", "auto"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid mode")
    settings.agent_mode = payload.mode
    return {"agent_mode": settings.agent_mode}
