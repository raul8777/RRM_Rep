from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import app as core

app = FastAPI(title="Crypto Agent Dashboard Read API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8001",
        "http://127.0.0.1:8001",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/status")
def status():
    return {"ok": True, "environment": core.APP_ENV, "service": "dashboard-read-api"}


@app.get("/positions/open")
async def open_positions():
    spot_data, futures_data = await core.asyncio.gather(
        core.get_open_positions(),
        core.get_futures_positions(),
    )
    spot_positions = [p for p in spot_data.get("positions", []) if p.get("trade_type") != "futures"]
    futures_positions = []
    for position in futures_data.get("positions", []):
        side = str(position.get("side") or position.get("position_side") or "").upper()
        futures_positions.append({
            **position,
            "position_id": position.get("position_id") or f"futures_{position.get('symbol')}_{side}",
            "side": side,
            "quantity": position.get("quantity", position.get("qty")),
            "invested_usdt": position.get("invested_usdt") or (
                float(position.get("entry_price", 0) or 0) * float(position.get("qty", 0) or 0)
            ),
            "stop_loss": position.get("stop_loss", position.get("sl_price")),
            "take_profit": position.get("take_profit", position.get("tp_price")),
            "entry_time": position.get("entry_time") or position.get("opened_at"),
            "trade_type": "futures",
        })
    positions = [*spot_positions, *futures_positions]
    return {
        **spot_data,
        "positions": positions,
        "count": len(positions),
        "spot_count": len(spot_positions),
        "futures_count": len(futures_positions),
    }


@app.get("/futures/positions")
async def futures_positions():
    return await core.get_futures_positions()


@app.get("/positions/closed")
async def closed_positions(limit: int = Query(100, ge=1, le=500)):
    return await core.get_closed_positions(limit)


@app.get("/dashboard/summary")
async def dashboard_summary(days: int = Query(1, ge=1, le=365)):
    payload = await core.asyncio.to_thread(core._load_materialized_dashboard_summary, days)
    materialized_days = days
    if not payload and days != 1:
        payload = await core.asyncio.to_thread(core._load_materialized_dashboard_summary, 1)
        materialized_days = 1
    if not payload:
        raise HTTPException(status_code=503, detail="dashboard_summary_materialization_unavailable")
    return {**payload, "_materialized_days": materialized_days}


@app.get("/")
def dashboard_index():
    return FileResponse(Path(__file__).parent / "frontend" / "index.html")


app.mount("/frontend", StaticFiles(directory=Path(__file__).parent / "frontend"), name="frontend")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_main_api(path: str, request: Request):
    headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.request(
            request.method,
            f"http://127.0.0.1:8001/{path}",
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
    excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    response_headers = {key: value for key, value in response.headers.items() if key.lower() not in excluded}
    return Response(content=response.content, status_code=response.status_code, headers=response_headers)

