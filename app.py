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
    if not 1 <= days <= 1000:
        raise HTTPException(status_code=400, detail="days must be between 1 and 1000")
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


@app.post("/predictor/reset-corrupted")
async def reset_corrupted_trades(_auth: None = Depends(_require_api_key)) -> Dict:
    """
    Mark rebalancing-origin trades as corrupted so LearningEngine ignores them.
    These trades were produced by the old _sell_holdings_not_in_top / decide_position_closures
    fallback logic that has since been removed (commit 9b10e36, jun-16-2026).
    Returns counts of trades marked and weights invalidated.
    """
    try:
        comp = get_components()
        if not comp.db_client:
            raise HTTPException(status_code=503, detail="DB not initialized")
        # Add is_corrupted column if it doesn't exist (idempotent)
        try:
            comp.db_client.execute_update(
                "ALTER TABLE trades_closed ADD COLUMN IF NOT EXISTS is_corrupted BOOLEAN DEFAULT FALSE",
                ()
            )
        except Exception:
            pass
        # Mark all rebalancing / manual-rebalance trades as corrupted
        _CORRUPTED_REASONS = (
            'portfolio_rebalancing', 'manual_rebalance',
            'prediction_reallocation', 'prediction_realloc',
        )
        placeholders = ', '.join(['%s'] * len(_CORRUPTED_REASONS))
        sql_mark = f"""
            UPDATE trades_closed
            SET is_corrupted = TRUE
            WHERE close_reason IN ({placeholders})
              AND (is_corrupted IS NULL OR is_corrupted = FALSE)
        """
        comp.db_client.execute_update(sql_mark, _CORRUPTED_REASONS)
        # Count how many are now marked
        rows = comp.db_client.execute_query(
            "SELECT COUNT(*) AS n FROM trades_closed WHERE is_corrupted = TRUE", ()
        )
        marked = rows[0]['n'] if rows else 0
        # Invalidate predictor weight cache
        try:
            from pnl_service import get_predictor
            get_predictor().invalidate_cache()
        except Exception:
            pass
        # Invalidate learning engine cache
        try:
            from learning_engine import get_learning_engine
            get_learning_engine()._cache.clear()
        except Exception:
            pass
        return {
            "status": "ok",
            "corrupted_trades_marked": marked,
            "message": (
                f"{marked} trades marked as corrupted (rebalancing-origin). "
                "Run POST /predictor/bootstrap?days=1000 to re-train with clean data."
            )
        }
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


# ── Monte Carlo projection (C5) ──────────────────────────────────────────────
_mc_cache: Dict = {"data": None, "ts": 0.0}
_MC_CACHE_TTL = 3600  # 1 hour default; reset via /monte-carlo/recalculate


def _run_monte_carlo(closed_trades: list, initial_capital: float,
                     months: int = 3, trades_per_month: int = 6,
                     n_sims: int = 1_000,
                     scenario_label: str = "",
                     override_win_rate: float = None) -> Dict:
    """
    Run Monte Carlo projection using REAL closed-trade PnL distribution.
    Returns monthly breakdown + P5/P50/P95 percentiles.
    override_win_rate: if set, forces this WR (for optimistic scenario C).
    """
    import random, time as _t
    rng = random.Random(_t.time())

    wins  = [t["pnl"] for t in closed_trades if t.get("pnl", 0) > 0]
    losses= [t["pnl"] for t in closed_trades if t.get("pnl", 0) <= 0]
    if not wins:
        wins = [0.54]
    if not losses:
        losses = [-1.18]

    real_wr   = len(wins) / max(len(closed_trades), 1)
    win_rate  = override_win_rate if override_win_rate is not None else real_wr
    avg_win   = sum(wins)  / len(wins)
    avg_loss  = sum(losses) / len(losses)

    # Simulate total PnL across all months
    finals = []
    for _ in range(n_sims):
        cap = initial_capital
        for _m in range(months):
            for _t2 in range(trades_per_month):
                if rng.random() < win_rate:
                    cap += rng.choice(wins)
                else:
                    cap += rng.choice(losses)
        finals.append(cap)

    finals.sort()
    p5  = finals[int(n_sims * 0.05)]
    p50 = finals[int(n_sims * 0.50)]
    p95 = finals[int(n_sims * 0.95)]
    mean_final = sum(finals) / n_sims

    # Monthly breakdown (expected value walk)
    ev_per_trade = win_rate * avg_win + (1 - win_rate) * avg_loss
    monthly_breakdown = []
    cap = initial_capital
    for m in range(1, months + 1):
        pnl = ev_per_trade * trades_per_month
        cap += pnl
        monthly_breakdown.append({"month": m, "capital": round(cap, 2), "pnl": round(pnl, 4)})

    return {
        "label": scenario_label,
        "initial_capital": initial_capital,
        "months": months,
        "trades_per_month": trades_per_month,
        "n_trades_used": len(closed_trades),
        "win_rate": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "monthly_breakdown": monthly_breakdown,
        "total_pnl": round(mean_final - initial_capital, 4),
        "mean_final": round(mean_final, 2),
        "p5":  round(p5,  2),
        "p50": round(p50, 2),
        "p95": round(p95, 2),
        "return_pct": round((mean_final / initial_capital - 1) * 100, 1) if initial_capital > 0 else 0.0,
    }


def _get_monte_carlo_projection(force: bool = False) -> Dict:
    import time as _time
    now = _time.monotonic()
    if not force and _mc_cache["data"] and now - _mc_cache["ts"] < _MC_CACHE_TTL:
        return _mc_cache["data"]
    try:
        closed_objs = position_manager.get_closed_positions(limit=500) if position_manager else []

        # All closed trades with close_reason
        all_closed = [{"pnl": float(p.pnl), "reason": getattr(p, 'close_reason', '') or ''}
                      for p in closed_objs if p.pnl is not None]

        # Separate rebalancing from normal trades (SL/TP/predictor)
        _REBAL_REASONS_MC = ('rebalancing', 'manual_rebalance', 'manual', 'balance_sync')
        rebal_trades  = [t for t in all_closed if any(r in t["reason"] for r in _REBAL_REASONS_MC)]
        normal_trades = [t for t in all_closed
                         if not any(r in t["reason"] for r in _REBAL_REASONS_MC)
                         and abs(t["pnl"]) < 50.0]  # exclude wild outliers

        # Capital total: USDT libre + valor invertido en spot
        try:
            _bal = binance_client.get_account_balance()
            _usdt_free = float(_bal.get("USDT", 0))
            # Estimate invested value from open positions
            _open_pos = position_manager.get_all_positions() if position_manager else {}
            _invested = 0.0
            for _sym, _pos in _open_pos.items():
                try:
                    _px = binance_client.get_symbol_price(_sym)
                    _invested += float(getattr(_pos, 'quantity', 0)) * _px
                except Exception:
                    pass
            total_capital = round(_usdt_free + _invested, 2)
            if total_capital < 100:
                total_capital = _usdt_free or 346.13
        except Exception:
            total_capital = 3080.0

        # Trades/month for normal trades (post-fix baseline: ~6/day normal + ~6 rebalancing)
        # Use actual history days to estimate rate
        _days_history = max(1, len(all_closed) // max(1, 10))  # rough: 10 trades/day avg
        _normal_tpm = max(3, len(normal_trades) / max(1, _days_history / 30))
        _rebal_tpm_postfix = max(1, min(6, _normal_tpm * 0.3))  # post-fix: ~30% of normal trades

        # BTC HODL comparison (30d)
        try:
            bc = get_components().binance_client
            kd = bc.client.get_klines(symbol="BTCUSDT", interval="1d", limit=32)
            btc_30d = (float(kd[-1][4]) - float(kd[-30][1])) / float(kd[-30][1]) * 100 if len(kd) >= 30 else 0.0
            btc_hodl_90d = round(btc_30d * 3, 1)
            btc_hodl_final = round(total_capital * (1 + btc_30d / 100 * 3), 2)
        except Exception:
            btc_hodl_90d = 0.0
            btc_hodl_final = total_capital

        # ── Escenario A: Bear — mercado bajista (todos los trades históricos) ──
        sc_a = _run_monte_carlo(
            closed_trades=all_closed,
            initial_capital=total_capital,
            months=3,
            trades_per_month=max(5, len(all_closed) // max(1, _days_history // 30)),
            n_sims=1000,
            scenario_label="Bear (mercado bajista)",
        )

        # ── Escenario B: Base — sistema actual (solo trades SL/TP/predictor) ─
        pool_b = normal_trades if normal_trades else all_closed
        sc_b = _run_monte_carlo(
            closed_trades=pool_b,
            initial_capital=total_capital,
            months=3,
            trades_per_month=max(3, int(_normal_tpm)),
            n_sims=1000,
            scenario_label="Base (sistema actual)",
        )

        # ── Escenario C: Bull — mercado alcista (WR 55%, +M1-M5 activos) ────
        _bull_tpm = max(3, int(_normal_tpm * 1.4))  # más señales en mercado bull
        sc_c = _run_monte_carlo(
            closed_trades=normal_trades or all_closed,
            initial_capital=total_capital,
            months=3,
            trades_per_month=_bull_tpm,
            n_sims=1000,
            scenario_label="Bull (F&G>50 + M1-M5)",
            override_win_rate=0.55,
        )

        # ── Meta 1,000 MXN/día con capital actual ────────────────────────────
        _usd_mxn_mc = 19.5
        _target_daily_usd = 1000.0 / _usd_mxn_mc  # ~51.28 USDT/día
        _wr_base  = sc_b["win_rate"] / 100.0
        _avg_w_b  = sc_b["avg_win"]
        _avg_l_b  = abs(sc_b["avg_loss"])
        _ev_trade = _wr_base * _avg_w_b - (1 - _wr_base) * _avg_l_b
        _tpm_base = sc_b["trades_per_month"]
        _trades_day_current = round(_tpm_base / 30, 2)
        _pnl_day_current_usd = round(_ev_trade * _trades_day_current, 4)
        _pnl_day_current_mxn = round(_pnl_day_current_usd * _usd_mxn_mc, 2)
        # trades/day needed to hit 1,000 MXN/day (EV per trade fixed)
        if _ev_trade > 0:
            _trades_needed_day = round(_target_daily_usd / _ev_trade, 1)
            _pct_toward_goal   = round(min(_trades_day_current / _trades_needed_day * 100, 100), 1)
            _bull_trades_day   = round(_bull_tpm / 30, 2)
            _bull_pnl_day_mxn  = round(_ev_trade * _bull_trades_day * 1.4 * _usd_mxn_mc, 2)  # bull WR boost
        else:
            _trades_needed_day = 999
            _pct_toward_goal   = 0.0
            _bull_trades_day   = 0.0
            _bull_pnl_day_mxn  = 0.0

        _meta_1000 = {
            "target_mxn_day": 1000,
            "target_usd_day": round(_target_daily_usd, 2),
            "ev_per_trade_usd": round(_ev_trade, 4),
            "trades_day_current": _trades_day_current,
            "pnl_day_current_usd": _pnl_day_current_usd,
            "pnl_day_current_mxn": _pnl_day_current_mxn,
            "trades_needed_day": _trades_needed_day,
            "pct_toward_goal": _pct_toward_goal,
            "bull_trades_day": _bull_trades_day,
            "bull_pnl_day_mxn": _bull_pnl_day_mxn,
        }

        data = {
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "initial_capital": total_capital,
            "n_trades_used": len(all_closed),
            "n_normal_trades": len(normal_trades),
            "n_rebal_trades": len(rebal_trades),
            "months": 3,
            "scenarios": {"A": sc_a, "B": sc_b, "C": sc_c},
            "meta_1000_mxn_day": _meta_1000,
            # Legacy fields (backward compat) — Base scenario as primary
            "trades_per_month": sc_b["trades_per_month"],
            "win_rate": sc_b["win_rate"],
            "avg_win": sc_b["avg_win"],
            "avg_loss": sc_b["avg_loss"],
            "monthly_breakdown": sc_b["monthly_breakdown"],
            "total_pnl": sc_b["total_pnl"],
            "mean_final": sc_b["mean_final"],
            "p5":  sc_b["p5"],
            "p50": sc_b["p50"],
            "p95": sc_b["p95"],
            "btc_hodl_pct":   btc_hodl_90d,
            "btc_hodl_final": btc_hodl_final,
            "savings_per_day": 0,  # deprecated — kept for compat
        }
    except Exception as _e:
        data = {
            "generated_at": "—", "initial_capital": 3080.0, "months": 3,
            "trades_per_month": 6, "n_trades_used": 0, "win_rate": 28.0,
            "avg_win": 0.54, "avg_loss": -1.18,
            "monthly_breakdown": [], "total_pnl": 0.0, "mean_final": 3080.0,
            "p5": 3000.0, "p50": 3080.0, "p95": 3160.0,
            "btc_hodl_pct": 0.0, "btc_hodl_final": 3080.0,
            "scenarios": {}, "savings_per_day": 0,
            "meta_1000_mxn_day": {},
            "error": str(_e),
        }
    _mc_cache["data"] = data
    _mc_cache["ts"] = now
    return data


# ── Market regime cache (5 min) ──────────────────────────────────────────────
_regime_cache: Dict = {"data": None, "ts": 0.0}


def _get_market_regime() -> Dict:
    """
    Return regime data sourced from the real trading engine (market_regime.get_regime()).
    Dashboard is now always consistent with run_trades.py decisions.
    Cache TTL = 240s (matches market_regime._CACHE_TTL_SECONDS).
    """
    import time as _time
    now = _time.monotonic()
    if _regime_cache["data"] and now - _regime_cache["ts"] < 240:
        return _regime_cache["data"]

    try:
        comp = get_components()
        from market_regime import get_regime as _get_regime
        r = _get_regime(comp.binance_client, settings=comp.settings)

        # Mode descriptions (single source of truth)
        _MODE_DESC = {
            "SPOT_BULL":     "Compras en breakout con Kelly dinámico. Trailing stop activo. Scale-out en TP.",
            "RANGE_SCALP":   "Compras en soporte BB inferior (RSI<40), venta en BB superior. TP ajustado 1.5%.",
            "PRESERVATION":  "Capital 80%+ en USDT. Solo NARRATIVE con VolSpike≥2x. Kelly×0.5.",
            "FUTURES_SHORT": "Shorts 2× en pares Tier A screener (BTCUSDT, ETHUSDT, SOLUSDT…). LONG en soporte. SL SHORT 3% / LONG 2.5% | TP 5%. Hedge mode activo.",
        }
        _MODE_COLOR = {
            "SPOT_BULL": "#4ecca3", "RANGE_SCALP": "#f39c12",
            "PRESERVATION": "#e74c3c", "FUTURES_SHORT": "#9b59b6",
        }

        result = {
            "regime":           r.regime,
            "color":            r.color,
            "btc_trend":        r.ema9_vs_ema21,
            "day_change_pct":   r.btc_day_pct,
            "vol_ratio":        r.vol_ratio,
            "bb_bandwidth":     r.bb_bandwidth,
            "btc_7d_change_pct": r.btc_7d_pct,
            "btc_30d_change_pct": r.btc_30d_pct,
            "trading_mode":     r.mode,
            "mode_color":       _MODE_COLOR.get(r.mode, "#888"),
            "mode_reason":      r.reason + (" [pendiente confirmación]" if not r.confirmed else ""),
            "mode_desc":        _MODE_DESC.get(r.mode, ""),
            "mode_confirmed":   r.confirmed,
            "regime_sl_pct":    r.sl_pct,
            "regime_tp_pct":    r.tp_pct,
            "regime_kelly":     r.kelly_multiplier,
        }
    except Exception as e:
        result = {
            "regime": "?", "color": "#888", "btc_trend": "?",
            "day_change_pct": 0.0, "vol_ratio": 0.0, "bb_bandwidth": 0.0,
            "btc_7d_change_pct": 0.0, "btc_30d_change_pct": 0.0,
            "trading_mode": "SPOT_BULL", "mode_color": "#888",
            "mode_reason": f"Error detectando régimen: {e}", "mode_desc": "",
            "mode_confirmed": False, "regime_sl_pct": 1.5, "regime_tp_pct": 2.5, "regime_kelly": 1.0,
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
        # Futures wallet balance
        _spot_usdt = float(balances.get("USDT", 0))
        _futures_usdt = 0.0
        try:
            from run_trades import _get_futures_client
            _fc_bal = _get_futures_client(settings)
            _futures_usdt = _fc_bal.get_available_usdt()
        except Exception:
            pass
        _grand_total = total_usdt + _futures_usdt
        result["balance"] = {
            "total_usdt": _grand_total,
            "total_mxn": _grand_total * _usd_mxn,
            "usd_to_mxn_rate": _usd_mxn,
            "balances_usdt": balances_usdt,
            "balances_mxn": {k: v * _usd_mxn for k, v in balances_usdt.items()},
            "spot_usdt": round(total_usdt, 4),
            "futures_usdt": round(_futures_usdt, 4),
            "spot_pct": round(total_usdt / _grand_total * 100, 1) if _grand_total > 0 else 100.0,
            "futures_pct": round(_futures_usdt / _grand_total * 100, 1) if _grand_total > 0 else 0.0,
        }
    except Exception as e:
        result["balance"] = {"error": str(e)}

    # Profit Projection (Monte Carlo forward, based on real trade history)
    try:
        import random as _rnd, math as _math
        _rnd.seed(42)
        _closed_pp = position_manager.get_closed_positions(limit=500)
        # Only use clean SL/TP/predictor trades (exclude rebalancing, corrupted, outliers)
        _all_pp     = [p for p in _closed_pp if p.pnl is not None]
        _REBAL_REASONS = ('rebalancing', 'manual_rebalance', 'manual', 'balance_sync')
        _normal_pp  = [p for p in _all_pp
                       if not any(r in (getattr(p, 'close_reason', '') or '') for r in _REBAL_REASONS)
                       and not getattr(p, 'is_corrupted', False)
                       and abs(float(p.pnl)) < 50.0]  # exclude wild outliers
        # Use clean normal trades if ≥5, else fall back to all non-corrupted
        _source_pp  = _normal_pp if len(_normal_pp) >= 5 else [
            p for p in _all_pp if not getattr(p, 'is_corrupted', False)
        ]
        _pnls_pp   = [float(p.pnl) for p in _source_pp]
        _wins_pp   = [p for p in _pnls_pp if p > 0]
        _losses_pp = [p for p in _pnls_pp if p <= 0]
        _n_pp = len(_pnls_pp)
        if _n_pp >= 5:
            _wr_pp      = len(_wins_pp) / _n_pp
            _avg_win    = sum(_wins_pp) / len(_wins_pp) if _wins_pp else 0.001
            _avg_loss   = sum(_losses_pp) / len(_losses_pp) if _losses_pp else -0.001
            # Estimate trades per month from normal trades only (5-day history)
            _days_obs   = max(1, len(_all_pp) // max(1, 10))  # ~10 trades/day rough estimate
            _tpm_pp = max(1.0, len(_source_pp) / max(1, _days_obs / 30))
            # Simple Monte Carlo: 1000 sims x 30 days
            def _mc_period(days, n_sims=1000):
                results = []
                for _ in range(n_sims):
                    trades = max(0, int(_rnd.gauss(_tpm_pp * days / 30, _tpm_pp * days / 30 * 0.3)))
                    pnl = sum(
                        _rnd.gauss(_avg_win, abs(_avg_win) * 0.4) if _rnd.random() < _wr_pp
                        else _rnd.gauss(_avg_loss, abs(_avg_loss) * 0.4)
                        for _ in range(trades)
                    )
                    results.append(pnl)
                results.sort()
                return {
                    "p5":  round(results[int(n_sims * 0.05)], 4),
                    "p50": round(results[int(n_sims * 0.50)], 4),
                    "p95": round(results[int(n_sims * 0.95)], 4),
                    "mean": round(sum(results) / n_sims, 4),
                }
            _proj_day   = _mc_period(1)
            _proj_week  = _mc_period(7)
            _proj_month = _mc_period(30)
            # Capital actual
            _cap_current  = result.get("balance", {}).get("total_usdt", 282.0)
            # Capital for $30k MXN/month
            _target_usd_month = 30000 / _usd_mxn
            _cap_for_target = (_target_usd_month / _proj_month["mean"] * _cap_current
                               if _proj_month["mean"] > 0.001 else 999999)
            # Meta 1,000 MXN/día con capital actual
            _ev_pp = _wr_pp * _avg_win + (1 - _wr_pp) * _avg_loss
            _target_day_usd = 1000.0 / _usd_mxn
            _trades_per_day_pp = _tpm_pp / 30.0
            _pnl_day_mxn_pp = round(_ev_pp * _trades_per_day_pp * _usd_mxn, 2)
            if _ev_pp > 0:
                _trades_needed_pp = round(_target_day_usd / _ev_pp, 1)
                _pct_goal_pp = round(min(_trades_per_day_pp / _trades_needed_pp * 100, 100), 1)
            else:
                _trades_needed_pp = 999
                _pct_goal_pp = 0.0
            result["profit_projection"] = {
                "day":   {k: round(v * 1, 4) for k, v in _proj_day.items()},
                "week":  {k: round(v * 1, 4) for k, v in _proj_week.items()},
                "month": {k: round(v * 1, 4) for k, v in _proj_month.items()},
                "day_mxn":   {k: round(v * _usd_mxn, 2) for k, v in _proj_day.items()},
                "week_mxn":  {k: round(v * _usd_mxn, 2) for k, v in _proj_week.items()},
                "month_mxn": {k: round(v * _usd_mxn, 2) for k, v in _proj_month.items()},
                "win_rate": round(_wr_pp * 100, 1),
                "trades_per_month": round(_tpm_pp, 1),
                "avg_win_usd": round(_avg_win, 4),
                "avg_loss_usd": round(_avg_loss, 4),
                "capital_usd": round(_cap_current, 2),
                "capital_mxn": round(_cap_current * _usd_mxn, 2),
                "usd_mxn_rate": _usd_mxn,
                # Capital actual scenario (replaces hypothetical $100k)
                "scenario_capital_actual": {
                    "capital_usd": round(_cap_current, 2),
                    "capital_mxn": round(_cap_current * _usd_mxn, 2),
                    "monthly_mean_usd":  round(_proj_month["mean"], 2),
                    "monthly_mean_mxn":  round(_proj_month["mean"] * _usd_mxn, 2),
                    "monthly_p5_mxn":    round(_proj_month["p5"] * _usd_mxn, 2),
                    "monthly_p95_mxn":   round(_proj_month["p95"] * _usd_mxn, 2),
                    "annual_mean_mxn":   round(_proj_month["mean"] * 12 * _usd_mxn, 2),
                },
                # Target: $30k MXN/month
                "target_30k_mxn": {
                    "monthly_target_usd": round(_target_usd_month, 2),
                    "capital_required_usd": round(min(_cap_for_target, 9_999_999), 2),
                    "capital_required_mxn": round(min(_cap_for_target * _usd_mxn, 999_999_999), 2),
                    "current_progress_pct": round(_cap_current / min(_cap_for_target, 9_999_999) * 100, 2),
                },
                # Meta 1,000 MXN/día con capital actual
                "meta_1000_mxn_day": {
                    "pnl_day_mxn": _pnl_day_mxn_pp,
                    "trades_day_current": round(_trades_per_day_pp, 2),
                    "trades_needed_day": _trades_needed_pp,
                    "pct_toward_goal": _pct_goal_pp,
                    "ev_per_trade_usd": round(_ev_pp, 4),
                },
            }
        else:
            result["profit_projection"] = {"error": "insufficient_trades", "n_trades": _n_pp}
    except Exception as _ppe:
        result["profit_projection"] = {"error": str(_ppe)}

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
        try:
            from run_trades import _overflow_positions as _ov_positions
        except Exception:
            _ov_positions = set()
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
                    "is_overflow": symbol in _ov_positions,
                    "trade_type": "spot",
                })
        result["open_positions"] = {"positions": open_list, "count": len(open_list)}
    except Exception as e:
        result["open_positions"] = {"positions": [], "count": 0, "error": str(e)}

    # Closed Positions (with pnl_prediction & prediction_accuracy) - Last 3 days only
    try:
        three_days_ago = datetime.utcnow() - timedelta(days=3)
        closed_positions = position_manager.get_closed_positions_since(since=three_days_ago, limit=50)
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
                "trade_type": "spot",
            })
        result["closed_positions"] = {"positions": closed_list, "count": len(closed_list), "period_days": 3}
    except Exception as e:
        result["closed_positions"] = {"positions": [], "count": 0, "error": str(e), "period_days": 3}

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

    result["monte_carlo_projection"] = _get_monte_carlo_projection()

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


@app.get("/metrics/override-performance")
async def get_override_performance() -> Dict:
    """Return VolSpike Override performance metrics for dashboard auditing."""
    try:
        # Importar las variables de tracking desde run_trades
        from run_trades import _VOLSPIKE_POSITIONS, _OVERRIDE_DAILY_COUNT, _OVERRIDE_PERFORMANCE, _VOLSPIKE_COOLDOWN
        
        # Calcular métricas agregadas
        total_overrides = len(_OVERRIDE_PERFORMANCE)
        if total_overrides == 0:
            return {
                "summary": {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "avg_accuracy": 0.0,
                    "total_pnl": 0.0,
                    "active_positions": len(_VOLSPIKE_POSITIONS),
                    "todays_count": _OVERRIDE_DAILY_COUNT.get(datetime.utcnow().strftime('%Y-%m-%d'), 0),
                },
                "by_symbol": {},
                "recent_trades": [],
                "recommendations": ["No hay datos de override todavía"],
            }
        
        # Calcular métricas por símbolo
        by_symbol = {}
        for trade in _OVERRIDE_PERFORMANCE:
            sym = trade['symbol']
            if sym not in by_symbol:
                by_symbol[sym] = {'trades': 0, 'wins': 0, 'total_pnl': 0.0, 'total_accuracy': 0.0}
            by_symbol[sym]['trades'] += 1
            if trade['actual_pnl'] > 0:
                by_symbol[sym]['wins'] += 1
            by_symbol[sym]['total_pnl'] += trade['actual_pnl']
            by_symbol[sym]['total_accuracy'] += trade.get('accuracy_pct', 0)
        
        # Calcular win rate y accuracy promedio por símbolo
        for sym in by_symbol:
            stats = by_symbol[sym]
            stats['win_rate'] = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
            stats['avg_accuracy'] = stats['total_accuracy'] / stats['trades'] if stats['trades'] > 0 else 0
        
        # Calcular métricas globales
        total_pnl = sum(t['actual_pnl'] for t in _OVERRIDE_PERFORMANCE)
        wins = sum(1 for t in _OVERRIDE_PERFORMANCE if t['actual_pnl'] > 0)
        win_rate = (wins / total_overrides * 100) if total_overrides > 0 else 0
        avg_accuracy = sum(t.get('accuracy_pct', 0) for t in _OVERRIDE_PERFORMANCE) / total_overrides if total_overrides > 0 else 0
        
        # Generar recomendaciones
        recommendations = []
        for sym, stats in by_symbol.items():
            if stats['win_rate'] < 30 and stats['trades'] >= 3:
                recommendations.append(f"⚠️ {sym}: Win rate bajo ({stats['win_rate']:.0f}%) - Considerar excluir de override")
            elif stats['win_rate'] > 60 and stats['trades'] >= 3:
                recommendations.append(f"✅ {sym}: Buen performance ({stats['win_rate']:.0f}% WR) - Mantener")
        
        if win_rate < 40 and total_overrides >= 5:
            recommendations.append("🚨 Win rate global override <40% - Considerar desactivar temporalmente")
        
        # Preparar recent trades (últimos 10)
        recent = sorted(_OVERRIDE_PERFORMANCE, key=lambda x: x.get('exit_time', ''), reverse=True)[:10]
        
        return {
            "summary": {
                "total_trades": total_overrides,
                "win_rate": round(win_rate, 1),
                "avg_accuracy": round(avg_accuracy, 1),
                "total_pnl": round(total_pnl, 2),
                "active_positions": len(_VOLSPIKE_POSITIONS),
                "todays_count": _OVERRIDE_DAILY_COUNT.get(datetime.utcnow().strftime('%Y-%m-%d'), 0),
                "daily_limit": getattr(settings, 'volspike_override_daily_limit', 3),
                "positions_limit": getattr(settings, 'volspike_override_max_positions', 2),
            },
            "by_symbol": {
                sym: {
                    "trades": stats['trades'],
                    "win_rate": round(stats['win_rate'], 1),
                    "avg_accuracy": round(stats['avg_accuracy'], 1),
                    "total_pnl": round(stats['total_pnl'], 2),
                }
                for sym, stats in by_symbol.items()
            },
            "recent_trades": [
                {
                    "symbol": t['symbol'],
                    "expected": round(t['expected_pnl'], 2),
                    "actual": round(t['actual_pnl'], 2),
                    "accuracy": round(t.get('accuracy_pct', 0), 1),
                    "reason": t.get('close_reason', 'unknown'),
                    "duration": round(t.get('duration_hours', 0), 1),
                }
                for t in recent
            ],
            "recommendations": recommendations if recommendations else ["✅ Override funcionando dentro de parámetros"],
        }
    except Exception as e:
        return {"error": str(e), "summary": {}, "by_symbol": {}, "recent_trades": [], "recommendations": ["Error cargando métricas"]}


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


# ── Futures positions endpoint (C1) ──────────────────────────────────────────

@app.post("/monte-carlo/recalculate")
async def recalculate_monte_carlo(_auth: None = Depends(_require_api_key)) -> Dict:
    """Force-recalculate the Monte Carlo projection using current closed trades and USDT balance."""
    data = _get_monte_carlo_projection(force=True)
    return {"ok": True, "monte_carlo_projection": data}


@app.get("/futures/positions")
async def get_futures_positions() -> Dict:
    """Return open futures positions queried live from Binance (source of truth).
    Also syncs the FuturesClient in-memory tracking to avoid stale state.
    """
    try:
        from run_trades import _get_futures_client
        from config import get_settings as _get_s
        _s = _get_s()
        if not getattr(_s, 'futures_enabled', False):
            return {"enabled": False, "positions": [], "count": 0}
        _fc = _get_futures_client(_s)

        # Query Binance directly — single source of truth
        live_positions = _fc.get_positions()  # non-zero positionAmt only

        # Sync in-memory tracking: mark closed anything not in live_positions
        live_keys = set()
        for lp in live_positions:
            sym = lp.get("symbol", "")
            ps  = lp.get("positionSide", "BOTH")
            amt = float(lp.get("positionAmt", 0))
            if ps == "BOTH":
                side = "SHORT" if amt < 0 else "LONG"
            else:
                side = ps
            live_keys.add(_fc._pos_key(sym, side))

        for _key, _pos in list(_fc._open_positions.items()):
            if not _pos.closed and _key not in live_keys:
                _pos.closed = True  # mark stale entry as closed

        positions = []
        for lp in live_positions:
            sym   = lp.get("symbol", "")
            ps    = lp.get("positionSide", "BOTH")
            amt   = float(lp.get("positionAmt", 0))
            entry = float(lp.get("entryPrice", 0))
            if entry <= 0:
                continue
            side = ("SHORT" if amt < 0 else "LONG") if ps == "BOTH" else ps
            qty  = abs(amt)

            try:
                cur_price = _fc.get_price(sym)
            except Exception:
                cur_price = entry

            # Retrieve SL/TP from tracking if available, otherwise estimate
            _tkey = _fc._pos_key(sym, side)
            _tpos = _fc._open_positions.get(_tkey)
            if _tpos and not _tpos.closed:
                sl_price = _tpos.sl_price
                tp_price = _tpos.tp_price
                opened_at = _tpos.opened_at.isoformat() if _tpos.opened_at else None
            else:
                tick = _fc._get_tick_size(sym)
                sl_price = _fc._align_price(entry * (0.97 if side == "LONG" else 1.03), tick)
                tp_price = _fc._align_price(entry * (1.06 if side == "LONG" else 0.94), tick)
                opened_at = None

            if side == "LONG":
                upnl      = (cur_price - entry) * qty
                sl_dist   = ((cur_price - sl_price) / cur_price * 100) if cur_price > 0 else 0
                tp_dist   = ((tp_price - cur_price) / cur_price * 100) if cur_price > 0 else 0
            else:
                upnl      = (entry - cur_price) * qty
                sl_dist   = ((sl_price - cur_price) / cur_price * 100) if cur_price > 0 else 0
                tp_dist   = ((cur_price - tp_price) / cur_price * 100) if cur_price > 0 else 0

            positions.append({
                "symbol":        sym,
                "side":          side,
                "qty":           qty,
                "entry_price":   entry,
                "current_price": cur_price,
                "sl_price":      sl_price,
                "tp_price":      tp_price,
                "sl_dist_pct":   round(sl_dist, 2),
                "tp_dist_pct":   round(tp_dist, 2),
                "unrealized_pnl": round(upnl, 4),
                "opened_at":     opened_at,
            })

        return {"enabled": True, "positions": positions, "count": len(positions),
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"enabled": False, "positions": [], "count": 0, "error": str(e)}


@app.get("/futures/closed")
async def get_futures_closed_positions(limit: int = 20, days: int = 3) -> Dict:
    """Return closed futures positions queried live from Binance userTrades (last N days).
    Survives container restarts — does not depend on in-memory tracking.
    """
    try:
        from run_trades import _get_futures_client
        from config import get_settings as _get_s
        _s = _get_s()
        if not getattr(_s, 'futures_enabled', False):
            return {"enabled": False, "positions": [], "count": 0}

        _fc = _get_futures_client(_s)

        # Build symbol list from config to query even after restart (empty tracking)
        _extra = list(getattr(_s, 'futures_symbols', []))
        try:
            from pair_screener import get_screened_pairs
            _extra = list(dict.fromkeys(_extra + get_screened_pairs(settings=_s).get("futures", [])))
        except Exception:
            pass

        raw = _fc.get_recent_trades(days=days, limit=limit, extra_symbols=_extra)

        closed = []
        for t in raw:
            closed_at_raw = t.get("closed_at")
            closed.append({
                "symbol":      t["symbol"],
                "side":        t["side"],
                "qty":         t["qty"],
                "entry_price": t.get("entry_price"),
                "exit_price":  t.get("exit_price"),
                "pnl":         t.get("pnl"),
                "opened_at":   None,
                "closed_at":   to_mexico_time_str(closed_at_raw) if closed_at_raw else None,
                "trade_type":  "futures",
            })

        return {
            "enabled": True,
            "positions": closed[:limit],
            "count": len(closed[:limit]),
            "period_days": days,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        return {"enabled": False, "positions": [], "count": 0, "error": str(e)}


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

_last_stale_reported_cycle = None  # dedup: track which last_cycle timestamp was already reported


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
        _stale_threshold = orchestrator.current_cycle_minutes * 1.5
        stale = age_min > _stale_threshold
        checks["trade_cycle"] = {
            "status": "warning" if stale else "ok",
            "last_cycle_at": to_mexico_time_str(last_cycle.isoformat()),
            "age_minutes": round(age_min, 1),
            "stale_threshold_minutes": round(_stale_threshold, 1),
            "cycle_running": orchestrator.trade_cycle_thread.is_alive() if orchestrator.trade_cycle_thread else False,
        }
        if stale:
            global _last_stale_reported_cycle
            if _last_stale_reported_cycle != last_cycle:
                ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                             f"Trade cycle may be stale: last run {age_min:.1f} min ago "
                             f"(threshold={_stale_threshold:.0f} min, regime={orchestrator.current_cycle_minutes}min)")
                _last_stale_reported_cycle = last_cycle
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

        def _sym_to_asset(sym: str) -> str:
            for quote in ("USDT", "BUSD", "BTC", "ETH"):
                if sym.endswith(quote) and len(sym) > len(quote):
                    return sym[:-len(quote)]
            return sym

        binance_holding_count = sum(
            1 for sym in tracked_symbols
            if float(balances.get(_sym_to_asset(sym), 0)) > 0
        )
        # mismatch: DB has entries for symbols with no Binance balance
        symbols_missing_in_binance = [
            sym for sym in tracked_symbols
            if float(balances.get(_sym_to_asset(sym), 0)) == 0
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

    # C6: Futures API (fapi.binance.com) health check
    try:
        _futures_enabled = getattr(settings, 'futures_enabled', False)
        if _futures_enabled:
            import urllib.request as _ur, time as _time
            t0 = _time.monotonic()
            with _ur.urlopen('https://fapi.binance.com/fapi/v1/ping', timeout=5) as _r:
                _r.read()
            _fapi_lat = round((_time.monotonic() - t0) * 1000, 1)
            try:
                from run_trades import _get_futures_client
                _fc = _get_futures_client(settings)
                _open_shorts = _fc.count_open_shorts()
                _open_longs  = _fc.count_open_longs()
            except Exception:
                _open_shorts = 0
                _open_longs  = 0
            checks["futures_api"] = {
                "status": "ok", "latency_ms": _fapi_lat,
                "open_shorts": _open_shorts, "open_longs": _open_longs, "leverage_max": 2,
            }
        else:
            checks["futures_api"] = {"status": "disabled"}
    except Exception as _fe:
        checks["futures_api"] = {"status": "error", "detail": str(_fe)}
        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Futures API health check failed: {_fe}")
        if overall != "error":
            overall = "warning"

    # Social score (CryptoCompare) health check
    try:
        from news_client import _cryptocompare_enabled, _SOCIAL_BASELINES, NewsClient as _NewsClient
        _cc_enabled = _cryptocompare_enabled()
        if _cc_enabled:
            _nc_tmp = _NewsClient()
            _trending_count = sum(
                1 for sym in _SOCIAL_BASELINES if _nc_tmp.is_social_trending(sym)
            )
            checks["social_score"] = {
                "status": "ok", "enabled": True,
                "trending_count": _trending_count,
                "symbols_monitored": len(_SOCIAL_BASELINES),
            }
        else:
            checks["social_score"] = {"status": "disabled", "enabled": False}
    except Exception as _se:
        checks["social_score"] = {"status": "error", "enabled": False, "detail": str(_se)}

    # Error summary
    err_summary = ec.get_summary(minutes=5)
    checks["errors"] = err_summary
    if err_summary["errors_last_5min"] > 0 and overall == "ok":
        overall = "warning"

    # Fase D: portfolio exposure + ciclo dinámico actual
    try:
        comp = get_components()
        _bal = comp.portfolio_manager.get_total_balance()
        _total = float(_bal.get("total_usdt", 0))
        _liquid = float(_bal.get("usdt_available", 0))
        _invested = _total - _liquid
        _exp_pct = _invested / _total if _total > 0 else 0.0
        _max_exp = getattr(settings, 'max_total_exposure_pct', 0.65)
        _exp_status = "ok" if _exp_pct < _max_exp * 0.85 else ("warning" if _exp_pct < _max_exp else "cap_reached")
        checks["portfolio_exposure"] = {
            "status": _exp_status,
            "invested_usdt": round(_invested, 2),
            "liquid_usdt": round(_liquid, 2),
            "total_usdt": round(_total, 2),
            "exposure_pct": round(_exp_pct * 100, 1),
            "max_exposure_pct": round(_max_exp * 100, 1),
        }
        if _exp_status == "cap_reached" and overall == "ok":
            overall = "warning"
    except Exception as _pe:
        checks["portfolio_exposure"] = {"status": "error", "detail": str(_pe)}

    # Ciclo dinámico actual + posiciones dinámicas
    _regime_now = getattr(orchestrator, 'current_regime_mode', 'SPOT_BULL') or 'SPOT_BULL'
    try:
        from run_trades import _get_max_positions_for_regime, _overflow_positions as _ov_set
        _max_pos_now = _get_max_positions_for_regime(settings, _regime_now)
        _overflow_used = len(_ov_set)
    except Exception:
        _max_pos_now = getattr(settings, 'max_positions', 12)
        _overflow_used = 0
    checks["trade_cycle"] = {
        "current_cycle_minutes": orchestrator.current_cycle_minutes,
        "last_trade_cycle_at": orchestrator.last_trade_cycle_at.isoformat() if orchestrator.last_trade_cycle_at else None,
        "regime": _regime_now,
        "max_positions_current": _max_pos_now,
        "overflow_slots_used": _overflow_used,
        "overflow_max_extra": getattr(settings, 'overflow_max_extra', 3),
    }

    # Circuit breaker status
    try:
        from run_trades import _circuit_breaker_until, _consecutive_failures
        _cb_active = _circuit_breaker_until is not None
        checks["circuit_breaker"] = {
            "status": "paused" if _cb_active else "ok",
            "consecutive_failures": _consecutive_failures,
            "paused_until": _circuit_breaker_until.isoformat() if _cb_active else None,
            "threshold": getattr(settings, 'circuit_breaker_consecutive_failures', 5),
        }
        if _cb_active and overall == "ok":
            overall = "warning"
    except Exception:
        checks["circuit_breaker"] = {"status": "ok", "consecutive_failures": 0}

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
