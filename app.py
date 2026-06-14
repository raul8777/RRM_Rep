from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends
from fastapi import status as http_status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Optional
import os
import secrets
from datetime import datetime, timedelta, timezone

from config import get_settings
from signal_store import add_signal, get_signals, get_recent_signals, set_db_client
from strategy import parse_signal
import run_trades
import analysis_cycle
from analysis_cache import analysis_cache
from backtester import Backtester
from learning_engine import get_learning_engine
from param_optimizer import get_optimizer
from orchestrator import SystemState
from init_components import get_components
import error_collector as ec

settings = get_settings()
app = FastAPI(title="Binance Trading Agent")

# Add CORS middleware — restrict to localhost only
_CORS_ORIGINS = [
    "http://localhost", "http://localhost:8000",
    "http://127.0.0.1", "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── API Key auth dependency (for mutating / control endpoints) ────────────────
def _require_api_key(request: Request) -> None:
    """
    Validates X-API-Key header against DASHBOARD_API_KEY env var.
    If DASHBOARD_API_KEY is not set, auth is skipped (local dev only).
    """
    api_key = os.environ.get("DASHBOARD_API_KEY", "")
    if not api_key:
        return  # No key configured — local dev, allow through
    provided = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(provided, api_key):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing X-API-Key")

# Mount static files for frontend
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/frontend", StaticFiles(directory=frontend_path), name="frontend")
    
    # Serve index.html at root
    @app.get("/")
    async def serve_frontend():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(frontend_path, "index.html"))

# All service singletons come from init_components — single source of truth
_comp = get_components()
binance_client = _comp.binance_client
news_client = _comp.news_client
db_client = _comp.db_client
position_manager = _comp.position_manager
portfolio_manager = _comp.portfolio_manager
orchestrator = _comp.orchestrator

# Wire db_client into Signal Store
if db_client:
    set_db_client(db_client)

# Mexico Central Timezone (UTC-6)
MEXICO_TZ = timezone(timedelta(hours=-6))

def to_mexico_time(utc_datetime: datetime) -> datetime:
    """Convert UTC datetime to Mexico Central timezone."""
    if utc_datetime.tzinfo is None:
        utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
    return utc_datetime.astimezone(MEXICO_TZ)

def to_mexico_time_str(utc_iso: str) -> str:
    """Convert UTC ISO string to Mexico Central timezone ISO string."""
    try:
        utc_dt = datetime.fromisoformat(utc_iso.replace('Z', '+00:00'))
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        mexico_dt = utc_dt.astimezone(MEXICO_TZ)
        return mexico_dt.isoformat()
    except:
        return utc_iso


@app.on_event("startup")
def startup_event():
    # Set RUNNING state FIRST so cycles can execute on first iteration
    orchestrator.start(changed_by="startup", reason="System startup")
    # Sync exchange balance: import pre-existing/orphan assets as managed positions
    try:
        comp = get_components()
        if comp.position_manager and comp.binance_client:
            synced = comp.position_manager.sync_with_exchange_balance(
                comp.binance_client, settings, min_value_usdt=5.0
            )
            if synced:
                print(f"Balance sync: imported {len(synced)} orphan position(s): {synced}")
            else:
                print("Balance sync: no orphan assets found (>$5 USDT)")
    except Exception as e:
        print(f"Balance sync error at startup: {e}")
    # Start background trade cycle only if agent is in auto mode
    if settings.agent_mode == 'auto':
        interval = int(getattr(settings, 'trade_cycle_minutes', 5))
        orchestrator.start_trade_cycle(run_trades.execute_once, interval_minutes=interval)
        print(f"Trade cycle started (interval={interval}min, mode={settings.agent_mode})")
    # Always start technical analysis cycle (technical indicators, every 15 min)
    orchestrator.start_analysis_cycle(analysis_cycle.execute_once, interval_minutes=15)
    print("Analysis cycle started")
    # Start self-learning cycle (LearningEngine, every 90 min)
    orchestrator.start_learning_cycle(interval_minutes=90)
    print("Learning cycle started")
    # Bootstrap PnL predictor with historical data if insufficient trade history
    try:
        from bootstrap_history import run_bootstrap, needs_bootstrap
        comp2 = get_components()
        if comp2.binance_client and needs_bootstrap(min_trades=50):
            print("Bootstrap: insufficient trade history — loading 90d historical data...")
            import threading
            def _bg_bootstrap():
                try:
                    result = run_bootstrap(comp2.binance_client, settings, lookback_days=90)
                    print(f"Bootstrap complete: {result.get('total_simulated_trades', 0)} trades, "
                          f"win_rate={result.get('global_win_rate', 0):.1%}")
                except Exception as be:
                    print(f"Bootstrap background error: {be}")
            threading.Thread(target=_bg_bootstrap, daemon=True).start()
        else:
            print("Bootstrap: sufficient trade history found — skipping")
    except Exception as e:
        print(f"Bootstrap startup error: {e}")


@app.post("/predictor/bootstrap")
async def trigger_bootstrap(days: int = 90, _auth: None = Depends(_require_api_key)) -> Dict:
    """Manually trigger PnL predictor bootstrap with historical Binance data."""
    if not 1 <= days <= 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    try:
        from bootstrap_history import run_bootstrap
        comp = get_components()
        if not comp.binance_client:
            raise HTTPException(status_code=503, detail="Binance client not initialized")
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: run_bootstrap(comp.binance_client, settings, lookback_days=days)
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/positions/sync")
async def sync_positions(_auth: None = Depends(_require_api_key)) -> Dict:
    """Manually trigger balance sync: import orphan Binance assets >$5 as managed positions."""
    try:
        comp = get_components()
        if not comp.position_manager or not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        synced = comp.position_manager.sync_with_exchange_balance(
            comp.binance_client, settings, min_value_usdt=5.0
        )
        return {
            "status": "ok",
            "imported": synced,
            "count": len(synced),
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/signals/recent")
async def signals_recent(limit: int = 20) -> Dict[str, object]:
    """Get the most recent N signals (BUY and SELL, processed or not)."""
    return {
        "signals": get_recent_signals(limit=limit),
        "count": limit,
    }


@app.post("/webhook")
async def webhook(payload: TradingViewPayload, request: Request, _auth: None = Depends(_require_api_key)) -> Dict:
    secret = request.headers.get("X-Webhook-Secret")
    if secret != settings.tradingview_webhook_secret:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Unauthorized webhook")

    payload_dict = payload.dict()
    try:
        signal = parse_signal(payload_dict)
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))

    signal['source'] = 'external'
    add_signal(signal)
    return {"signal_registered": True, "signal": signal}


@app.post("/command")
async def command(payload: CommandPayload, _auth: None = Depends(_require_api_key)) -> Dict[str, str]:
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


# Config Console API Endpoints

class ControlRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/control/pause")
async def pause_system(payload: ControlRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Pause the system (stop new orders, monitor open positions)."""
    success = orchestrator.pause(changed_by="api", reason=payload.reason or "API request")
    if not success:
        raise HTTPException(status_code=400, detail="Failed to pause system")
    return orchestrator.get_state()


@app.post("/control/stop")
async def stop_system(payload: ControlRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Stop the system (stop all, keep open positions)."""
    success = orchestrator.stop(changed_by="api", reason=payload.reason or "API request")
    if not success:
        raise HTTPException(status_code=400, detail="Failed to stop system")
    return orchestrator.get_state()


@app.post("/control/start")
async def start_system(payload: ControlRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Start the system (recalculate everything)."""
    success = orchestrator.start(changed_by="api", reason=payload.reason or "API request")
    if not success:
        raise HTTPException(status_code=400, detail="Failed to start system")
    return orchestrator.get_state()


@app.get("/control/status")
async def get_control_status() -> Dict:
    """Get current system control status."""
    return orchestrator.get_state()


@app.get("/health")
async def health_check() -> Dict:
    """Get overall health status of the system."""
    return orchestrator.get_health_status()


class PortfolioConfigRequest(BaseModel):
    portfolio_allocation_pct: Optional[float] = None
    min_pair_confidence_score: Optional[float] = None
    max_risk_per_pair_pct: Optional[float] = None
    diversification_factor: Optional[float] = None
    enable_position_closure: Optional[bool] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_enabled: Optional[bool] = None
    trailing_stop_activation_pct: Optional[float] = None
    trailing_stop_distance_pct: Optional[float] = None
    protect_profitable_positions: Optional[bool] = None
    protect_profit_min_pct: Optional[float] = None
    min_signal_strength: Optional[float] = None


@app.get("/config/portfolio")
async def get_portfolio_config() -> Dict:
    """Get current Portfolio Manager configuration."""
    return {
        "portfolio_allocation_pct": settings.portfolio_allocation_pct,
        "min_pair_confidence_score": settings.min_pair_confidence_score,
        "max_risk_per_pair_pct": settings.max_risk_per_pair_pct,
        "diversification_factor": settings.diversification_factor,
        "enable_position_closure": settings.enable_position_closure,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "trailing_stop_enabled": settings.trailing_stop_enabled,
        "trailing_stop_activation_pct": settings.trailing_stop_activation_pct,
        "trailing_stop_distance_pct": settings.trailing_stop_distance_pct,
        "protect_profitable_positions": settings.protect_profitable_positions,
        "protect_profit_min_pct": settings.protect_profit_min_pct,
        "min_signal_strength": settings.min_signal_strength,
    }


@app.post("/config/portfolio")
async def update_portfolio_config(payload: PortfolioConfigRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Update Portfolio Manager configuration."""
    config = {
        "portfolio_allocation_pct": settings.portfolio_allocation_pct,
        "min_pair_confidence_score": settings.min_pair_confidence_score,
        "max_risk_per_pair_pct": settings.max_risk_per_pair_pct,
        "diversification_factor": settings.diversification_factor,
        "enable_position_closure": settings.enable_position_closure,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "trailing_stop_enabled": settings.trailing_stop_enabled,
        "trailing_stop_activation_pct": settings.trailing_stop_activation_pct,
        "trailing_stop_distance_pct": settings.trailing_stop_distance_pct,
        "protect_profitable_positions": settings.protect_profitable_positions,
        "protect_profit_min_pct": settings.protect_profit_min_pct,
        "min_signal_strength": settings.min_signal_strength,
    }
    
    if payload.portfolio_allocation_pct is not None:
        config["portfolio_allocation_pct"] = payload.portfolio_allocation_pct
    if payload.min_pair_confidence_score is not None:
        config["min_pair_confidence_score"] = payload.min_pair_confidence_score
    if payload.max_risk_per_pair_pct is not None:
        config["max_risk_per_pair_pct"] = payload.max_risk_per_pair_pct
    if payload.diversification_factor is not None:
        config["diversification_factor"] = payload.diversification_factor
    if payload.enable_position_closure is not None:
        settings.enable_position_closure = payload.enable_position_closure
        config["enable_position_closure"] = payload.enable_position_closure
    if payload.stop_loss_pct is not None:
        settings.stop_loss_pct = payload.stop_loss_pct
        config["stop_loss_pct"] = payload.stop_loss_pct
    if payload.take_profit_pct is not None:
        settings.take_profit_pct = payload.take_profit_pct
        config["take_profit_pct"] = payload.take_profit_pct
    if payload.trailing_stop_enabled is not None:
        settings.trailing_stop_enabled = payload.trailing_stop_enabled
        config["trailing_stop_enabled"] = payload.trailing_stop_enabled
    if payload.trailing_stop_activation_pct is not None:
        settings.trailing_stop_activation_pct = payload.trailing_stop_activation_pct
        config["trailing_stop_activation_pct"] = payload.trailing_stop_activation_pct
    if payload.trailing_stop_distance_pct is not None:
        settings.trailing_stop_distance_pct = payload.trailing_stop_distance_pct
        config["trailing_stop_distance_pct"] = payload.trailing_stop_distance_pct
    if payload.protect_profitable_positions is not None:
        settings.protect_profitable_positions = payload.protect_profitable_positions
        config["protect_profitable_positions"] = payload.protect_profitable_positions
    if payload.protect_profit_min_pct is not None:
        settings.protect_profit_min_pct = payload.protect_profit_min_pct
        config["protect_profit_min_pct"] = payload.protect_profit_min_pct
    if payload.min_signal_strength is not None:
        settings.min_signal_strength = payload.min_signal_strength
        config["min_signal_strength"] = payload.min_signal_strength
    return config


# Dashboard Enhancement Endpoints

@app.get("/account/balance")
async def get_account_balance() -> Dict:
    """Get current account balance with USDT and MXN conversion."""
    try:
        balances = binance_client.get_account_balance()
        total_usdt = 0.0
        
        # Calculate total in USDT and get prices
        balances_with_prices = {}
        for asset, amount in balances.items():
            if asset == "USDT":
                balances_with_prices[asset] = {"amount": amount, "usdt_value": amount}
                total_usdt += amount
            else:
                # For non-USDT assets, get current price in USDT
                try:
                    symbol = f"{asset}USDT"
                    price = binance_client.get_symbol_price(symbol)
                    usdt_value = amount * price
                    balances_with_prices[asset] = {"amount": amount, "usdt_value": usdt_value}
                    total_usdt += usdt_value
                except Exception:
                    # If price fetch fails, skip this asset
                    pass
        
        # Convert to MXN
        mxn_rate = settings.usd_to_mxn_rate
        total_mxn = total_usdt * mxn_rate
        
        # Convert each balance to MXN and sort by USDT value (highest to lowest)
        # Filter out assets with value less than $5 USD
        sorted_balances = sorted(balances_with_prices.items(), key=lambda x: x[1]["usdt_value"], reverse=True)
        
        balances_usdt = {}
        balances_mxn = {}
        for asset, data in sorted_balances:
            # Filter out assets with value less than $5 USD (except USDT itself)
            if asset != "USDT" and data["usdt_value"] < 5.0:
                continue
            balances_usdt[asset] = data["amount"]
            balances_mxn[asset] = data["usdt_value"] * mxn_rate
        
        return {
            "balances_usdt": balances_usdt,
            "balances_mxn": balances_mxn,
            "total_usdt": total_usdt,
            "total_mxn": total_mxn,
            "usd_to_mxn_rate": mxn_rate,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get account balance: {str(e)}")


@app.get("/account/pnl-history")
async def get_pnl_history() -> Dict:
    """Get PnL history for day, week, and year."""
    try:
        day_pnl = position_manager.calculate_pnl_history("day")
        week_pnl = position_manager.calculate_pnl_history("week")
        year_pnl = position_manager.calculate_pnl_history("year")
        
        return {
            "day": day_pnl,
            "week": week_pnl,
            "year": year_pnl,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get PnL history: {str(e)}")


@app.get("/positions/open")
async def get_open_positions() -> Dict:
    """Get all open positions with current PnL."""
    try:
        positions = position_manager.get_all_positions()
        positions_data = []
        
        for symbol, pos_list in positions.items():
            # Fetch current price once per symbol
            try:
                current_price = float(binance_client.get_symbol_price(symbol))
            except Exception:
                current_price = None
            for entry_num, position in enumerate(pos_list, start=1):
                entry_price = float(position.entry_price)
                quantity    = float(position.quantity)
                if current_price is not None:
                    if position.side.upper() == "BUY":
                        unrealized_pnl = round((current_price - entry_price) * quantity, 4)
                    else:
                        unrealized_pnl = round((entry_price - current_price) * quantity, 4)
                    cp = current_price
                else:
                    cp = entry_price
                    unrealized_pnl = 0.0

                unrealized_pnl_pct = round((cp - entry_price) / entry_price * 100, 4) if entry_price > 0 else 0.0
                # Compute dynamic SL/TP/trailing prices from settings
                sl_price = round(entry_price * (1 - settings.stop_loss_pct / 100), 6)
                tp_price = round(entry_price * (1 + settings.take_profit_pct / 100), 6)
                meta = getattr(position, 'metadata', {}) or {}
                trailing_peak = meta.get('trailing_peak')
                trail_stop_price = round(float(trailing_peak) * (1 - settings.trailing_stop_distance_pct / 100), 6) if trailing_peak else None
                positions_data.append({
                    "symbol": symbol,
                    "position_id": position.position_id,
                    "entry_number": entry_num,
                    "side": position.side,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "current_price": cp,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "entry_time": to_mexico_time_str(position.entry_time),
                    "entry_commission": float(position.entry_commission) if position.entry_commission else 0.0,
                    "stop_loss_price": sl_price,
                    "take_profit_price": tp_price,
                    "trailing_peak": float(trailing_peak) if trailing_peak else None,
                    "trail_stop_price": trail_stop_price,
                })
        
        return {
            "positions": positions_data,
            "count": len(positions_data),
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get open positions: {str(e)}")


@app.post("/positions/close-all")
async def close_all_positions(_auth: None = Depends(_require_api_key)) -> Dict:
    """Sell all open positions on Binance and close them in DB."""
    from run_trades import get_symbol_filters, align_quantity_to_step, is_quantity_valid
    results = []
    all_positions = position_manager.get_all_positions()
    if not all_positions:
        return {"closed": [], "message": "No open positions"}
    for symbol, pos_list in all_positions.items():
        total_qty = sum(float(p.quantity) for p in pos_list)
        try:
            try:
                filters = get_symbol_filters(binance_client, symbol)
                total_qty = align_quantity_to_step(total_qty, filters["step"])
            except Exception:
                pass
            price = float(binance_client.get_symbol_price(symbol))
            order = binance_client.create_market_order(symbol=symbol, side="SELL", quantity=total_qty)
            commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
            position_manager.close_all_positions_for_symbol(symbol, price, "manual_rebalance", commission_rate)
            results.append({"symbol": symbol, "qty": total_qty, "price": price, "status": "sold", "entries_closed": len(pos_list)})
        except Exception as e:
            results.append({"symbol": symbol, "status": "error", "error": str(e)})
    return {"closed": results, "count": len(results)}


@app.get("/positions/closed")
async def get_closed_positions(limit: int = 20) -> Dict:
    """Get last N closed positions."""
    try:
        closed_positions = position_manager.get_closed_positions(limit)
        positions_data = []
        
        for pos in closed_positions:
            positions_data.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "pnl": pos.pnl,
                "pnl_percentage": pos.pnl_percentage,
                "entry_time": to_mexico_time_str(pos.entry_time),
                "exit_time": to_mexico_time_str(pos.exit_time),
                "close_reason": pos.close_reason,
                "entry_commission": pos.entry_commission,
                "exit_commission": pos.exit_commission
            })
        
        return {
            "positions": positions_data,
            "count": len(positions_data),
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get closed positions: {str(e)}")


@app.get("/schedule/next-cycle")
async def get_next_cycle() -> Dict:
    """Get time until next trading cycle."""
    try:
        # Get trade cycle interval from settings
        interval_minutes = settings.trade_cycle_minutes
        
        # Calculate next cycle time based on current time
        now = datetime.utcnow()
        interval_seconds = interval_minutes * 60
        
        # Calculate seconds since last cycle boundary
        seconds_since_boundary = (now.hour * 3600 + now.minute * 60 + now.second) % interval_seconds
        seconds_until_next = interval_seconds - seconds_since_boundary
        
        return {
            "interval_minutes": interval_minutes,
            "seconds_until_next": seconds_until_next,
            "next_cycle_time": to_mexico_time(now + timedelta(seconds=seconds_until_next)).isoformat(),
            "timestamp": to_mexico_time(now).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get next cycle info: {str(e)}")


@app.get("/performance/summary")
async def get_analysis_summary() -> Dict:
    """Get performance metrics summary."""
    try:
        metrics = position_manager.calculate_performance_metrics()
        
        return {
            "metrics": metrics,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get analysis summary: {str(e)}")


# ── Market regime cache (5 min) ──────────────────────────────────────────────
_regime_cache: Dict = {"data": None, "ts": 0.0}


def _get_market_regime() -> Dict:
    """Classify current BTC/crypto market regime using EMA9/21 4h + daily change + vol."""
    import time as _time
    now = _time.monotonic()
    if _regime_cache["data"] and now - _regime_cache["ts"] < 300:
        return _regime_cache["data"]

    try:
        bc = get_components().binance_client
        from autonomous_signals import AutonomousSignalGenerator
        sig_gen = AutonomousSignalGenerator(bc, ["BTCUSDT"])

        # EMA trend 4h
        btc_trend = sig_gen.get_btc_market_trend(interval="4h")

        # Daily change %
        open_p = bc.get_daily_open_price("BTCUSDT")
        cur_p = bc.get_symbol_price("BTCUSDT")
        day_chg = (cur_p - open_p) / open_p * 100 if open_p else 0.0

        # Volume ratio: last 1h vs avg of previous 23h
        klines_1h = bc.client.get_klines(symbol="BTCUSDT", interval="1h", limit=25)
        vols = [float(k[5]) for k in klines_1h]
        avg_vol = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else 1
        vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0

        # BTC 7-day change %
        try:
            klines_7d = bc.client.get_klines(symbol="BTCUSDT", interval="1d", limit=8)
            btc_7d_chg = (float(klines_7d[-1]["close"] if isinstance(klines_7d[-1], dict) else klines_7d[-1][4])
                          - float(klines_7d[-7]["open"] if isinstance(klines_7d[-7], dict) else klines_7d[-7][1])) \
                         / float(klines_7d[-7]["open"] if isinstance(klines_7d[-7], dict) else klines_7d[-7][1]) * 100 \
                         if len(klines_7d) >= 7 else 0.0
        except Exception:
            btc_7d_chg = 0.0

        # Classify regime
        if btc_trend == "UP" and day_chg > 0.8 and vol_ratio >= 0.8:
            regime, color = "ALCISTA", "#4ecca3"
        elif btc_trend == "DOWN" and day_chg < -0.8:
            regime, color = "BAJISTA", "#e74c3c"
        elif -0.8 <= day_chg <= 0.8 and vol_ratio < 0.6:
            regime, color = "LATERAL", "#f39c12"
        elif btc_trend == "UP" and vol_ratio < 0.6:
            regime, color = "DÉBIL ALZA", "#f39c12"
        elif btc_trend == "DOWN":
            regime, color = "DÉBIL BAJA", "#e67e22"
        else:
            regime, color = "LATERAL", "#f39c12"

        # Derive trading mode from regime + 7d change
        # Check if futures are enabled to decide between PRESERVATION and FUTURES_SHORT
        from init_components import get_components as _get_comp
        _futures_on = False
        try:
            _futures_on = getattr(_get_comp().settings, 'futures_enabled', False)
        except Exception:
            pass

        if regime in ("ALCISTA", "DÉBIL ALZA"):
            trading_mode = "SPOT_BULL"
            mode_color = "#4ecca3"
            mode_reason = f"BTC tendencia UP, día {day_chg:+.1f}%"
            mode_desc = "Compras en breakout con Kelly dinámico. Trailing stop activo. Scale-out en TP."
        elif regime == "LATERAL":
            trading_mode = "RANGE_SCALP"
            mode_color = "#f39c12"
            mode_reason = f"Mercado lateral, vol {vol_ratio:.1f}x, día {day_chg:+.1f}%"
            mode_desc = "Compras en soporte BB inferior (RSI<35), venta en BB superior (RSI>65). TP ajustado 1.5%."
        elif btc_7d_chg <= -7:
            if _futures_on:
                trading_mode = "FUTURES_SHORT"
                mode_color = "#9b59b6"
                mode_reason = f"Bear market: BTC 7d={btc_7d_chg:+.1f}% — Futuros ACTIVOS"
                mode_desc = "Shorts 2× en BTC/ETH/SOL/BNB. SL 3% arriba, TP 5% abajo. Máx 2 posiciones simultáneas."
            else:
                trading_mode = "PRESERVATION"
                mode_color = "#e74c3c"
                mode_reason = f"Bear market: BTC 7d={btc_7d_chg:+.1f}%"
                mode_desc = "Capital 80%+ en USDT. Solo NARRATIVE con VolSpike≥2x. Kelly×0.5. Activa FUTURES_ENABLED para shorts."
        elif regime in ("BAJISTA", "DÉBIL BAJA"):
            if _futures_on:
                trading_mode = "FUTURES_SHORT"
                mode_color = "#9b59b6"
                mode_reason = f"BTC bajista: día {day_chg:+.1f}%, 7d={btc_7d_chg:+.1f}% — Futuros ACTIVOS"
                mode_desc = "Shorts 2× en BTC/ETH/SOL/BNB. SL 3% arriba, TP 5% abajo. Máx 2 posiciones simultáneas."
            else:
                trading_mode = "PRESERVATION"
                mode_color = "#e67e22"
                mode_reason = f"BTC bajista, día {day_chg:+.1f}%, 7d={btc_7d_chg:+.1f}%"
                mode_desc = "Modo defensivo. Entradas solo con señal FUERTE (strength>0.85). Kelly reducido 50%."
        else:
            trading_mode = "SPOT_BULL"
            mode_color = "#4ecca3"
            mode_reason = f"Sin régimen claro definido, día {day_chg:+.1f}%"
            mode_desc = "Operando con parámetros estándar. Monitoreo activo de régimen."

        result = {
            "regime": regime,
            "color": color,
            "btc_trend": btc_trend,
            "day_change_pct": round(day_chg, 2),
            "vol_ratio": round(vol_ratio, 2),
            "btc_7d_change_pct": round(btc_7d_chg, 2),
            "trading_mode": trading_mode,
            "mode_color": mode_color,
            "mode_reason": mode_reason,
            "mode_desc": mode_desc,
        }
    except Exception as e:
        result = {
            "regime": "?", "color": "#888", "btc_trend": "?",
            "day_change_pct": 0.0, "vol_ratio": 0.0, "btc_7d_change_pct": 0.0,
            "trading_mode": "SPOT_BULL", "mode_color": "#888",
            "mode_reason": "Error al detectar régimen", "mode_desc": "",
        }

    _regime_cache["data"] = result
    _regime_cache["ts"] = now
    return result


@app.get("/dashboard/config")
async def get_dashboard_config(request: Request) -> Dict:
    """Return frontend runtime config. Only serves API key to local/docker clients."""
    client_host = request.client.host if request.client else ""
    is_local = (
        client_host in ("127.0.0.1", "::1", "localhost")
        or client_host.startswith("172.")   # Docker bridge network
        or client_host.startswith("192.168.")  # LAN
        or client_host.startswith("10.")    # Private network
    )
    api_key = os.environ.get("DASHBOARD_API_KEY", "") if is_local else ""
    return {"api_key": api_key}


@app.get("/dashboard/summary")
async def get_dashboard_summary() -> Dict:
    """Single endpoint that returns all dashboard data to minimize API calls."""
    result = {}

    # Exchange rate (computed once, reused across blocks)
    _usd_mxn = 17.5
    try:
        _usd_mxn = binance_client.get_symbol_price("USDCUSDT") * 17.5 or 17.5
    except Exception:
        pass

    # System status
    try:
        result["system_status"] = orchestrator.get_state()
    except Exception as e:
        result["system_status"] = {"error": str(e)}

    # Health
    try:
        result["health"] = orchestrator.get_health_status()
    except Exception as e:
        result["health"] = {"error": str(e)}

    # Portfolio config
    try:
        result["portfolio_config"] = {
            "portfolio_allocation_pct": settings.portfolio_allocation_pct,
            "min_pair_confidence_score": settings.min_pair_confidence_score,
            "max_risk_per_pair_pct": settings.max_risk_per_pair_pct,
            "diversification_factor": settings.diversification_factor,
            "enable_position_closure": settings.enable_position_closure,
        }
    except Exception as e:
        result["portfolio_config"] = {"error": str(e)}

    # Signals — use get_recent_signals so SELL signals and processed BUYs are visible
    try:
        result["signals"] = {"signals": get_recent_signals(limit=20)}
    except Exception as e:
        result["signals"] = {"signals": [], "error": str(e)}

    # Balance
    try:
        balances = binance_client.get_account_balance()
        total_usdt = 0.0
        balances_usdt = {}
        for asset, amount in balances.items():
            if asset == "USDT":
                balances_usdt[asset] = amount
                total_usdt += amount
            else:
                try:
                    price = binance_client.get_symbol_price(f"{asset}USDT")
                    usdt_val = amount * price
                    balances_usdt[asset] = usdt_val
                    total_usdt += usdt_val
                except Exception:
                    pass
        result["balance"] = {
            "total_usdt": total_usdt,
            "total_mxn": total_usdt * _usd_mxn,
            "usd_to_mxn_rate": _usd_mxn,
            "balances_usdt": balances_usdt,
            "balances_mxn": {k: v * _usd_mxn for k, v in balances_usdt.items()},
        }
    except Exception as e:
        result["balance"] = {"error": str(e)}

    # PnL History
    try:
        now = to_mexico_time(datetime.utcnow())
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=now.weekday())
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

        def _pnl_for_period(positions, since):
            def _to_dt(t):
                if isinstance(t, str):
                    return to_mexico_time(datetime.fromisoformat(t.replace('Z', '+00:00')))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                return to_mexico_time(t)
            total = sum(float(p.pnl) for p in positions if p.exit_time and _to_dt(p.exit_time) >= since)
            count = sum(1 for p in positions if p.exit_time and _to_dt(p.exit_time) >= since)
            return {"total_pnl": total, "total_trades": count, "pnl_percentage": 0.0}

        closed = position_manager.get_closed_positions(limit=500)
        result["pnl_history"] = {
            "day": _pnl_for_period(closed, day_start),
            "week": _pnl_for_period(closed, week_start),
            "year": _pnl_for_period(closed, year_start),
        }
    except Exception as e:
        result["pnl_history"] = {"error": str(e)}

    # Open Positions (with pnl_prediction)
    try:
        positions = position_manager.get_all_positions()
        open_list = []
        for symbol, pos_list in positions.items():
            # Fetch price once per symbol
            try:
                current_price_sym = float(binance_client.get_symbol_price(symbol))
            except Exception:
                current_price_sym = None
            for entry_num, position in enumerate(pos_list, start=1):
                try:
                    entry_p = float(position.entry_price)
                    qty_f   = float(position.quantity)
                    cp = current_price_sym if current_price_sym is not None else entry_p
                    if position.side.upper() == "BUY":
                        unrealized_pnl = round((cp - entry_p) * qty_f, 4)
                    else:
                        unrealized_pnl = round((entry_p - cp) * qty_f, 4)
                except Exception:
                    entry_p = float(position.entry_price)
                    qty_f   = float(position.quantity)
                    cp = entry_p
                    unrealized_pnl = 0.0

                entry_time_str = ""
                try:
                    entry_time_str = to_mexico_time_str(position.entry_time) if position.entry_time else ""
                    now_dt = to_mexico_time(datetime.utcnow())
                    entry_dt = datetime.fromisoformat(entry_time_str) if entry_time_str else now_dt
                    hours_held = (now_dt - entry_dt).total_seconds() / 3600
                except Exception:
                    hours_held = 0.0

                invested_usdt      = round(entry_p * qty_f, 4)
                current_value_usdt = round(cp * qty_f, 4)
                _cat_map = getattr(settings, "token_categories", {})
                _sym_cat = _cat_map.get(symbol, {})
                open_list.append({
                    "symbol": symbol,
                    "position_id": position.position_id,
                    "entry_number": entry_num,
                    "side": position.side,
                    "category": _sym_cat.get("market_behavior", "MIDCAP_ALT"),
                    "origin": _sym_cat.get("origin", ""),
                    "quantity": qty_f,
                    "entry_price": entry_p,
                    "current_price": cp,
                    "invested_usdt": invested_usdt,
                    "invested_mxn": round(invested_usdt * _usd_mxn, 2),
                    "current_value_mxn": round(current_value_usdt * _usd_mxn, 2),
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_mxn": round(unrealized_pnl * _usd_mxn, 2),
                    "entry_time": entry_time_str,
                    "hours_held": round(hours_held, 1),
                    "entry_commission": float(position.entry_commission) if position.entry_commission else 0.0,
                    "stop_loss": float(position.stop_loss) if position.stop_loss else None,
                    "take_profit": float(position.take_profit) if position.take_profit else None,
                    "pnl_prediction": position.pnl_prediction,
                })
        result["open_positions"] = {"positions": open_list, "count": len(open_list)}
    except Exception as e:
        result["open_positions"] = {"positions": [], "count": 0, "error": str(e)}

    # Closed Positions (with pnl_prediction & prediction_accuracy)
    try:
        closed_positions = position_manager.get_closed_positions(limit=20)
        closed_list = []
        _cat_map_cl = getattr(settings, "token_categories", {})
        for pos in closed_positions:
            _cl_cat = _cat_map_cl.get(pos.symbol, {})
            closed_list.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "category": _cl_cat.get("market_behavior", "MIDCAP_ALT"),
                "quantity": float(pos.quantity) if pos.quantity else 0.0,
                "entry_price": float(pos.entry_price) if pos.entry_price else 0.0,
                "exit_price": float(pos.exit_price) if pos.exit_price else 0.0,
                "pnl": float(pos.pnl) if pos.pnl else 0.0,
                "pnl_percentage": float(pos.pnl_percentage) if pos.pnl_percentage else 0.0,
                "entry_time": to_mexico_time_str(pos.entry_time) if pos.entry_time else "",
                "exit_time": to_mexico_time_str(pos.exit_time) if pos.exit_time else "",
                "close_reason": pos.close_reason or "",
                "entry_commission": float(pos.entry_commission) if pos.entry_commission else 0.0,
                "exit_commission": float(pos.exit_commission) if pos.exit_commission else 0.0,
                "pnl_prediction": pos.pnl_prediction,
                "prediction_accuracy": float(pos.prediction_accuracy) if pos.prediction_accuracy else None,
            })
        result["closed_positions"] = {"positions": closed_list, "count": len(closed_list)}
    except Exception as e:
        result["closed_positions"] = {"positions": [], "count": 0, "error": str(e)}

    # Next Cycle
    try:
        interval_minutes = settings.trade_cycle_minutes
        now = datetime.utcnow()
        interval_seconds = interval_minutes * 60
        seconds_since_boundary = (now.hour * 3600 + now.minute * 60 + now.second) % interval_seconds
        seconds_until_next = interval_seconds - seconds_since_boundary
        result["next_cycle"] = {
            "interval_minutes": interval_minutes,
            "seconds_until_next": seconds_until_next,
            "next_cycle_time": to_mexico_time(now + timedelta(seconds=seconds_until_next)).isoformat(),
        }
    except Exception as e:
        result["next_cycle"] = {"error": str(e)}

    # Performance metrics
    try:
        result["performance"] = {"metrics": position_manager.calculate_performance_metrics()}
    except Exception as e:
        result["performance"] = {"metrics": {}, "error": str(e)}

    # Learning engine status + prediction accuracy
    try:
        engine = get_learning_engine()
        engine_status = engine.get_status()
        last_result = engine_status.get("last_result") or {}
        acc = last_result.get("accuracy", {})
        bias = last_result.get("bias", {})
        active_weights = engine.get_weights("__global__")
        result["learning"] = {
            "last_run": engine_status.get("last_run"),
            "trades_analyzed": last_result.get("trades_analyzed", 0),
            "prediction_accuracy": acc.get("prediction_accuracy", 0.0),
            "avg_prediction_error": acc.get("avg_prediction_error", 0.0),
            "signal_quality_score": acc.get("signal_quality_score", 0.0),
            "recommendation": last_result.get("recommendation", ""),
            "active_weights": {
                k: v for k, v in active_weights.items() if k != "bias_correction"
            },
            "bias_correction": active_weights.get("bias_correction", 1.0),
            "bias_by_symbol": bias.get("by_symbol", {}),
        }
    except Exception as e:
        result["learning"] = {"error": str(e)}

    # Market regime
    try:
        result["market_regime"] = _get_market_regime()
    except Exception as e:
        result["market_regime"] = {"regime": "?", "color": "#888", "btc_trend": "?", "day_change_pct": 0.0, "vol_ratio": 0.0}

    # Market context: Fear & Greed + CoinGecko Trending
    try:
        _nc = news_client
        _fng = _nc.get_fear_greed()
        _fng_mod = _nc.get_fear_greed_modifier()
        _trending = _nc.get_trending_symbols()
        _analysis_base = [s.replace("USDT", "") for s in getattr(settings, "analysis_symbols", [])]
        # News sentiment (Fase 3) — count positive/negative articles last 24h
        _news_sentiment = {"positive": 0, "negative": 0, "neutral": 0}
        try:
            _all_syms = getattr(settings, "analysis_symbols", [])
            _recent = _nc.get_recent_news(_all_syms, since_hours=24)
            for _art in _recent:
                _s = _art.get("sentiment", "neutral")
                _news_sentiment[_s] = _news_sentiment.get(_s, 0) + 1
        except Exception:
            pass
        result["market_context"] = {
            "fear_greed_value": _fng.get("value", 50),
            "fear_greed_label": _fng.get("label", "Neutral"),
            "fear_greed_modifier": _fng_mod,
            "trending_symbols": _trending,
            "analysis_symbols_base": _analysis_base,
            "news_sentiment": _news_sentiment,
        }
    except Exception as _fng_e:
        result["market_context"] = {
            "fear_greed_value": 50, "fear_greed_label": "Neutral",
            "fear_greed_modifier": 0.0, "trending_symbols": [], "analysis_symbols_base": [],
            "news_sentiment": {"positive": 0, "negative": 0, "neutral": 0},
        }

    # Effective thresholds (category base + F&G modifier applied)
    try:
        _fng_mod2 = result.get("market_context", {}).get("fear_greed_modifier", 0.0)
        _cat_th = getattr(settings, "category_thresholds", {})
        result["effective_thresholds"] = {
            cat: {
                "base_prob": th["min_prob"],
                "effective_prob": round(th["min_prob"] + _fng_mod2, 3),
                "delta": round(_fng_mod2, 3),
            }
            for cat, th in _cat_th.items()
        }
    except Exception:
        result["effective_thresholds"] = {}

    # Daily ticker bar (8AM Mexico open vs current price)
    try:
        ticker_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
        ticker_data = []
        for sym in ticker_symbols:
            try:
                open_p = binance_client.get_daily_open_price(sym)
                cur_p = binance_client.get_symbol_price(sym)
                change_pct = round((cur_p - open_p) / open_p * 100, 2) if open_p else 0.0
                ticker_data.append({
                    "symbol": sym,
                    "open_price": round(open_p, 6),
                    "current_price": round(cur_p, 6),
                    "change_pct": change_pct,
                })
            except Exception:
                pass
        result["daily_ticker"] = ticker_data
    except Exception as e:
        result["daily_ticker"] = []

    # Monte Carlo projection (static results from last backtest_90d.py run — 2026-06-14)
    result["monte_carlo_projection"] = {
        "generated_at": "2026-06-14",
        "initial_capital": 346.13,
        "months": 6,
        "trades_per_month": 11,
        "win_rate_current": 51.5,
        "win_rate_improved": 53.5,
        "monthly_breakdown": [
            {"month": 1, "capital_pre": 345.71, "pnl_pre": -0.42, "capital_post": 347.73, "pnl_post": 1.60},
            {"month": 2, "capital_pre": 345.29, "pnl_pre": -0.42, "capital_post": 349.29, "pnl_post": 1.56},
            {"month": 3, "capital_pre": 344.91, "pnl_pre": -0.38, "capital_post": 350.90, "pnl_post": 1.61},
            {"month": 4, "capital_pre": 344.49, "pnl_pre": -0.42, "capital_post": 352.56, "pnl_post": 1.65},
            {"month": 5, "capital_pre": 344.07, "pnl_pre": -0.41, "capital_post": 354.14, "pnl_post": 1.58},
            {"month": 6, "capital_pre": 343.73, "pnl_pre": -0.34, "capital_post": 355.74, "pnl_post": 1.60},
        ],
        "total_pnl_pre": -2.40,
        "total_pnl_post": 9.61,
        "p5_pre": 334.76,
        "p95_pre": 352.82,
        "p5_post": 347.53,
        "p95_post": 364.43,
        "btc_hodl_180d_pct": -29.3,
        "btc_hodl_final": 244.80,
    }

    result["timestamp"] = to_mexico_time(datetime.utcnow()).isoformat()
    return result


@app.get("/market/daily-ticker")
async def get_daily_ticker() -> Dict:
    """Daily open (8AM Mexico) vs current price for main trading pairs."""
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    data = []
    for sym in symbols:
        try:
            open_p = binance_client.get_daily_open_price(sym)
            cur_p = binance_client.get_symbol_price(sym)
            change_pct = round((cur_p - open_p) / open_p * 100, 2) if open_p else 0.0
            data.append({
                "symbol": sym,
                "open_price": round(open_p, 6),
                "current_price": round(cur_p, 6),
                "change_pct": change_pct,
            })
        except Exception:
            pass
    return {"ticker": data}


@app.get("/positions/open/detail")
async def get_open_positions_detail() -> Dict:
    """Get open positions with full pnl_prediction detail."""
    try:
        positions = position_manager.get_all_positions()
        positions_data = []
        for symbol, position in positions.items():
            try:
                current_price = binance_client.get_symbol_price(symbol)
                if position.side.upper() == "BUY":
                    unrealized_pnl = float((current_price - position.entry_price) * position.quantity)
                else:
                    unrealized_pnl = float((position.entry_price - current_price) * position.quantity)
            except Exception:
                current_price = float(position.entry_price)
                unrealized_pnl = 0.0
            positions_data.append({
                "symbol": symbol,
                "side": position.side,
                "quantity": float(position.quantity),
                "entry_price": float(position.entry_price),
                "current_price": float(current_price),
                "unrealized_pnl": unrealized_pnl,
                "entry_time": to_mexico_time_str(position.entry_time) if position.entry_time else "",
                "entry_commission": float(position.entry_commission) if position.entry_commission else 0.0,
                "stop_loss": float(position.stop_loss) if position.stop_loss else None,
                "take_profit": float(position.take_profit) if position.take_profit else None,
                "pnl_prediction": position.pnl_prediction,
                "indicators_at_entry": position.indicators_at_entry,
            })
        return {"positions": positions_data, "count": len(positions_data),
                "timestamp": to_mexico_time(datetime.utcnow()).isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get open positions: {str(e)}")


# ── Backtest endpoints ────────────────────────────────────────────────────────

_backtest_tasks: Dict[str, Dict] = {}  # run_id -> status dict


@app.post("/backtest/run")
async def run_backtest(
    background_tasks: BackgroundTasks,
    _auth: None = Depends(_require_api_key),
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    days_back: int = 30,
) -> Dict:
    """Launch a walk-forward backtest in the background. Returns run_id immediately."""
    import re, uuid
    if not re.match(r'^[A-Z0-9]{2,20}$', symbol.upper()):
        raise HTTPException(status_code=400, detail="Invalid symbol format")
    symbol = symbol.upper()
    if not 1 <= days_back <= 365:
        raise HTTPException(status_code=400, detail="days_back must be between 1 and 365")
    if interval not in {"1m","5m","15m","30m","1h","4h","1d"}:
        raise HTTPException(status_code=400, detail="Invalid interval")
    import uuid
    run_id = str(uuid.uuid4())
    _backtest_tasks[run_id] = {"status": "running", "symbol": symbol,
                               "run_id": run_id, "started_at": datetime.utcnow().isoformat()}

    def _do_backtest():
        try:
            bt = Backtester(initial_capital=10_000.0, db_client=db_client)
            klines = bt.fetch_klines_from_binance(symbol, interval, days_back)
            from param_optimizer import DEFAULT_GRID
            default_params = {k: v[len(v) // 2] for k, v in DEFAULT_GRID.items()}
            result = bt.run_backtest_with_params(klines, default_params, symbol=symbol)
            bt.persist_result(result, symbol, interval, days_back)
            _backtest_tasks[run_id].update({
                "status": "completed",
                "win_rate": result.win_rate,
                "sharpe_ratio": result.sharpe_ratio,
                "total_pnl": result.total_pnl,
                "total_trades": result.total_trades,
                "max_drawdown": result.max_drawdown,
                "sortino_ratio": result.sortino_ratio,
                "profit_factor": result.profit_factor,
                "completed_at": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            _backtest_tasks[run_id].update({"status": "error", "error": str(e)})

    background_tasks.add_task(_do_backtest)
    return {"run_id": run_id, "status": "running",
            "message": f"Backtest started for {symbol} ({days_back}d)"}


@app.get("/backtest/results")
async def get_backtest_results(symbol: str = None, limit: int = 10) -> Dict:
    """Return recent backtest results from DB + any in-progress tasks."""
    try:
        bt = Backtester(db_client=db_client)
        db_results = bt.get_results(symbol=symbol, limit=limit)
        pending = [
            v for v in _backtest_tasks.values()
            if v["status"] in ("running", "error")
            and (symbol is None or v["symbol"] == symbol)
        ]
        return {"results": db_results, "pending": pending,
                "timestamp": to_mexico_time(datetime.utcnow()).isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/backtest/status/{run_id}")
async def get_backtest_status(run_id: str) -> Dict:
    """Get status of a specific backtest run."""
    task = _backtest_tasks.get(run_id)
    if task:
        return task
    raise HTTPException(status_code=404, detail=f"run_id {run_id} not found")


# ── Learning engine endpoints ─────────────────────────────────────────────────


@app.get("/learning/status")
async def get_learning_status() -> Dict:
    """Return LearningEngine status, active weights, and last accuracy report."""
    try:
        engine = get_learning_engine()
        status = engine.get_status()
        weights_global = engine.get_weights("__global__")
        return {
            "status": status,
            "active_weights": weights_global,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/learning/run")
async def trigger_learning(background_tasks: BackgroundTasks, _auth: None = Depends(_require_api_key),
                           min_trades: int = 5,
                           lookback_days: int = 30) -> Dict:
    """Manually trigger a LearningEngine cycle."""
    def _do_learn():
        try:
            from learning_engine import LearningEngine
            engine = LearningEngine(db_client=db_client)
            engine.run(min_trades=min_trades, lookback_days=lookback_days)
            # Invalidate PnLPredictor weight cache after re-learning
            from pnl_service import get_predictor
            get_predictor().invalidate_cache()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"LearningEngine manual run failed: {e}")

    background_tasks.add_task(_do_learn)
    return {"status": "started", "message": "Learning cycle triggered in background"}


# ── Strategy params endpoint ──────────────────────────────────────────────────


@app.get("/strategy/params")
async def get_strategy_params(symbol: str = "__global__") -> Dict:
    """Return active optimized params + indicator weights for a symbol."""
    try:
        optimizer = get_optimizer()
        engine = get_learning_engine()
        active = optimizer.get_active_params(symbol)
        history = optimizer.get_optimization_history(symbol, limit=5)
        weights = engine.get_weights(symbol)
        return {
            "symbol": symbol,
            "active_params": active,
            "optimization_history": history,
            "indicator_weights": weights,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Health & Error monitoring endpoints ─────────────────────────────────────

_STALE_CYCLE_MINUTES = 15  # warn if last trade cycle was more than this long ago


@app.get("/health/status")
async def get_health_status() -> Dict:
    """Active health checks: DB, Binance, trade cycle freshness, position sync."""
    checks = {}
    overall = "ok"

    # DB check
    try:
        import time as _time
        t0 = _time.monotonic()
        db_client.execute_query("SELECT 1")
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        checks["db"] = {"status": "ok", "latency_ms": latency_ms}
    except Exception as e:
        checks["db"] = {"status": "error", "detail": str(e)}
        ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"DB health check failed: {e}")
        overall = "error"

    # Binance check
    try:
        import time as _time
        t0 = _time.monotonic()
        binance_client.get_account_balance()
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        checks["binance"] = {"status": "ok", "latency_ms": latency_ms,
                              "testnet": settings.binance_use_testnet}
    except Exception as e:
        checks["binance"] = {"status": "error", "detail": str(e)}
        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Binance health check failed: {e}")
        if overall != "error":
            overall = "warning"

    # Trade cycle freshness
    last_cycle = orchestrator.last_trade_cycle_at
    if last_cycle:
        age_min = (datetime.utcnow() - last_cycle).total_seconds() / 60
        stale = age_min > _STALE_CYCLE_MINUTES
        checks["trade_cycle"] = {
            "status": "warning" if stale else "ok",
            "last_cycle_at": to_mexico_time_str(last_cycle.isoformat()),
            "age_minutes": round(age_min, 1),
            "cycle_running": orchestrator.trade_cycle_thread.is_alive() if orchestrator.trade_cycle_thread else False,
        }
        if stale:
            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                         f"Trade cycle may be stale: last run {age_min:.1f} min ago")
            if overall != "error":
                overall = "warning"
    else:
        checks["trade_cycle"] = {"status": "unknown", "detail": "No cycle completed yet"}

    # Position sync: compare tracked symbols in DB vs their balances in Binance
    try:
        all_pos = position_manager.get_all_positions()
        db_count = sum(len(v) for v in all_pos.values())
        balances = binance_client.get_account_balance()
        # Only check symbols the system is actively tracking (e.g. BNBUSDT -> BNB)
        tracked_symbols = set(all_pos.keys())
        binance_holding_count = sum(
            1 for sym in tracked_symbols
            if float(balances.get(sym.replace("USDT", "").replace("BTC", ""), 0)) > 0
        )
        # mismatch: DB has entries for symbols with no Binance balance
        symbols_missing_in_binance = [
            sym for sym in tracked_symbols
            if float(balances.get(sym.replace("USDT", "").replace("BTC", ""), 0)) == 0
        ]
        mismatch = len(symbols_missing_in_binance)
        sync_status = "ok" if mismatch == 0 else "warning"
        checks["position_sync"] = {
            "status": sync_status,
            "db_open_positions": db_count,
            "tracked_symbols": len(tracked_symbols),
            "binance_confirmed": binance_holding_count,
            "symbols_missing_in_binance": symbols_missing_in_binance,
            "mismatch": mismatch,
        }
        if mismatch > 0:
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_WARNING,
                         f"Position sync mismatch: {mismatch} tracked symbol(s) missing in Binance: {symbols_missing_in_binance}")
            if overall != "error":
                overall = "warning"
    except Exception as e:
        checks["position_sync"] = {"status": "error", "detail": str(e)}

    # Error summary
    err_summary = ec.get_summary(minutes=5)
    checks["errors"] = err_summary
    if err_summary["errors_last_5min"] > 0 and overall == "ok":
        overall = "warning"

    return {
        "overall": overall,
        "checks": checks,
        "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
    }


@app.get("/errors/recent")
async def get_recent_errors(limit: int = 50) -> Dict:
    """Return recent captured errors and warnings from all system layers."""
    records = ec.get_recent(limit)
    summary = ec.get_summary(minutes=5)
    return {
        "errors": records,
        "count": len(records),
        **summary,
        "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
    }


@app.post("/errors/clear")
async def clear_errors() -> Dict:
    """Clear the in-memory error log."""
    ec.clear_errors()
    return {"status": "cleared", "timestamp": to_mexico_time(datetime.utcnow()).isoformat()}


@app.get("/analysis/predictions")
async def get_prediction_analysis() -> Dict:
    """Analyze PnL prediction accuracy from closed positions."""
    try:
        # Get closed positions with predictions
        closed_positions = position_manager.get_closed_positions(limit=100)
        
        # Initialize backtester for analysis
        backtester = Backtester()
        
        # Analyze prediction accuracy
        analysis = backtester.validate_pnl_predictor(closed_positions)
        
        return {
            "analysis": analysis,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get prediction analysis: {str(e)}")
