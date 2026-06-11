from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi import status as http_status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Optional
import os
from datetime import datetime, timedelta, timezone

from agent import TradingAgent
from binance_client import BinanceClient
from config import get_settings
from news_client import NewsClient
from signal_store import add_signal, get_signals, set_db_client
from strategy import build_order_params, parse_signal
import threading
import run_trades
import analysis_cycle
from backtester import Backtester
from analysis_cache import analysis_cache
from learning_engine import get_learning_engine
from param_optimizer import get_optimizer
from orchestrator import Orchestrator, SystemState
from portfolio_manager import PortfolioManager
from position_manager import PositionManager
from db_client import get_db_client

settings = get_settings()
app = FastAPI(title="Binance Trading Agent")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/frontend", StaticFiles(directory=frontend_path), name="frontend")
    
    # Serve index.html at root
    @app.get("/")
    async def serve_frontend():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(frontend_path, "index.html"))

binance_client = BinanceClient(
    api_key=settings.binance_api_key,
    api_secret=settings.binance_api_secret,
    testnet=settings.binance_use_testnet,
    dry_run=settings.dry_run,
)
news_client = NewsClient()
agent = TradingAgent(binance_client=binance_client, news_client=news_client, settings=settings)

# Initialize Database Client
db_client = None
try:
    db_url = os.getenv('DB_URL')
    if db_url:
        db_client = get_db_client(db_url)
        print(f"Database client initialized with URL: {db_url}")
    else:
        print("DB_URL not set, using in-memory storage")
except Exception as e:
    print(f"Failed to initialize database client: {e}")

# Set db_client for Signal Store
if db_client:
    set_db_client(db_client)

# Initialize Portfolio Manager and Position Manager
portfolio_manager = PortfolioManager(binance_client=binance_client, settings=settings)
position_manager = PositionManager(db_client=db_client)

# Initialize Orchestrator
orchestrator = Orchestrator()
orchestrator.set_portfolio_manager(portfolio_manager)
orchestrator.set_position_manager(position_manager)

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
        interval = int(getattr(settings, 'trade_cycle_minutes', 5))
        orchestrator.start_trade_cycle(run_trades.execute_once, interval_minutes=interval)
    # Always start technical analysis cycle
    orchestrator.start_analysis_cycle(analysis_cycle.execute_once, interval_minutes=15)
    # Set orchestrator to RUNNING state
    orchestrator.start(changed_by="startup", reason="System startup")


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


# Config Console API Endpoints

class ControlRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/control/pause")
async def pause_system(payload: ControlRequest) -> Dict:
    """Pause the system (stop new orders, monitor open positions)."""
    success = orchestrator.pause(changed_by="api", reason=payload.reason or "API request")
    if not success:
        raise HTTPException(status_code=400, detail="Failed to pause system")
    return orchestrator.get_state()


@app.post("/control/stop")
async def stop_system(payload: ControlRequest) -> Dict:
    """Stop the system (stop all, keep open positions)."""
    success = orchestrator.stop(changed_by="api", reason=payload.reason or "API request")
    if not success:
        raise HTTPException(status_code=400, detail="Failed to stop system")
    return orchestrator.get_state()


@app.post("/control/start")
async def start_system(payload: ControlRequest) -> Dict:
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


@app.get("/config/portfolio")
async def get_portfolio_config() -> Dict:
    """Get current Portfolio Manager configuration."""
    return {
        "portfolio_allocation_pct": settings.portfolio_allocation_pct,
        "min_pair_confidence_score": settings.min_pair_confidence_score,
        "max_risk_per_pair_pct": settings.max_risk_per_pair_pct,
        "diversification_factor": settings.diversification_factor,
        "enable_position_closure": settings.enable_position_closure,
    }


@app.post("/config/portfolio")
async def update_portfolio_config(payload: PortfolioConfigRequest) -> Dict:
    """Update Portfolio Manager configuration."""
    # Note: This updates the settings object but won't affect running instances
    # In production, this should trigger a restart or reconfiguration
    config = get_portfolio_config()
    
    if payload.portfolio_allocation_pct is not None:
        config["portfolio_allocation_pct"] = payload.portfolio_allocation_pct
    if payload.min_pair_confidence_score is not None:
        config["min_pair_confidence_score"] = payload.min_pair_confidence_score
    if payload.max_risk_per_pair_pct is not None:
        config["max_risk_per_pair_pct"] = payload.max_risk_per_pair_pct
    if payload.diversification_factor is not None:
        config["diversification_factor"] = payload.diversification_factor
    if payload.enable_position_closure is not None:
        config["enable_position_closure"] = payload.enable_position_closure
    
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
        
        for symbol, position in positions.items():
            # Get current price to calculate unrealized PnL
            try:
                current_price = binance_client.get_symbol_price(symbol)
                if position.side.upper() == "BUY":
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity
                else:
                    unrealized_pnl = (position.entry_price - current_price) * position.quantity
            except Exception:
                current_price = position.entry_price
                unrealized_pnl = 0.0
            
            positions_data.append({
                "symbol": symbol,
                "side": position.side,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "entry_time": to_mexico_time_str(position.entry_time),
                "entry_commission": position.entry_commission,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit
            })
        
        return {
            "positions": positions_data,
            "count": len(positions_data),
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get open positions: {str(e)}")


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


@app.get("/dashboard/summary")
async def get_dashboard_summary() -> Dict:
    """Single endpoint that returns all dashboard data to minimize API calls."""
    result = {}

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

    # Signals
    try:
        result["signals"] = {"signals": get_signals()}
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
        usd_mxn = 17.5
        try:
            usd_mxn = binance_client.get_symbol_price("USDCUSDT") * 17.5 or 17.5
        except Exception:
            pass
        result["balance"] = {
            "total_usdt": total_usdt,
            "total_mxn": total_usdt * usd_mxn,
            "usd_to_mxn_rate": usd_mxn,
            "balances_usdt": balances_usdt,
            "balances_mxn": {k: v * usd_mxn for k, v in balances_usdt.items()},
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
            total = sum(float(p.pnl) for p in positions if p.exit_time and
                        to_mexico_time(datetime.fromisoformat(p.exit_time.replace('Z', '+00:00'))
                                       if isinstance(p.exit_time, str) else p.exit_time.isoformat()) >= since)
            count = sum(1 for p in positions if p.exit_time and
                        to_mexico_time(datetime.fromisoformat(p.exit_time.replace('Z', '+00:00'))
                                       if isinstance(p.exit_time, str) else p.exit_time.isoformat()) >= since)
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

            entry_time_str = ""
            try:
                entry_time_str = to_mexico_time_str(position.entry_time) if position.entry_time else ""
                now_dt = to_mexico_time(datetime.utcnow())
                entry_dt = datetime.fromisoformat(entry_time_str) if entry_time_str else now_dt
                hours_held = (now_dt - entry_dt).total_seconds() / 3600
            except Exception:
                hours_held = 0.0

            open_list.append({
                "symbol": symbol,
                "side": position.side,
                "quantity": float(position.quantity),
                "entry_price": float(position.entry_price),
                "current_price": float(current_price),
                "unrealized_pnl": unrealized_pnl,
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
        for pos in closed_positions:
            closed_list.append({
                "symbol": pos.symbol,
                "side": pos.side,
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

    result["timestamp"] = to_mexico_time(datetime.utcnow()).isoformat()
    return result


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
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    days_back: int = 30,
) -> Dict:
    """Launch a walk-forward backtest in the background. Returns run_id immediately."""
    import uuid
    run_id = str(uuid.uuid4())
    _backtest_tasks[run_id] = {"status": "running", "symbol": symbol,
                               "run_id": run_id, "started_at": datetime.utcnow().isoformat()}

    def _do_backtest():
        try:
            db = get_db_client(settings)
            bt = Backtester(initial_capital=10_000.0, db_client=db)
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
        db = get_db_client(settings)
        bt = Backtester(db_client=db)
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
async def trigger_learning(background_tasks: BackgroundTasks,
                           min_trades: int = 5,
                           lookback_days: int = 30) -> Dict:
    """Manually trigger a LearningEngine cycle."""
    def _do_learn():
        try:
            db = get_db_client(settings)
            from learning_engine import LearningEngine
            engine = LearningEngine(db_client=db)
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
