from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends
from fastapi import status as http_status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from collections import Counter
import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from config import get_settings
from config_helpers import get_param as _cfg_param, dump_active_config, _PARAM_DEFAULTS
import config_helpers
from signal_store import add_signal, get_signals, get_recent_signals, set_db_client
from strategy import parse_signal
import run_trades
import analysis_cycle
from analysis_cache import analysis_cache
from universe_selector import select_analysis_universe
from backtester import Backtester
from learning_engine import get_learning_engine
from param_optimizer import get_optimizer
from orchestrator import SystemState
from init_components import get_components
from audit_tools import check_signal_audit_consistency
import error_collector as ec
from super_mimic.api import router as super_mimic_router
from binance_truth_guard import (
    get_truth_status,
    get_truth_status_spot,
    get_truth_status_futures,
    evaluate_spot_parity,
    evaluate_futures_parity,
    auto_reconcile_spot,
    auto_reconcile_futures,
)
from trade_utils import get_min_notional_delta, get_symbol_filters

settings = get_settings()
app = FastAPI(title="Binance Trading Agent")
app.include_router(super_mimic_router)

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

_HEALTH_RESULTS_CACHE: Dict[str, Any] = {
    "snapshot": None,
    "last_run_at": None,
    "interval_seconds": 300,
    "last_trigger": "startup",
}
_HEALTH_RESULTS_EVENT = asyncio.Event()
_HEALTH_RESULTS_LEVEL = {"ok": 0, "inconclusive": 1, "warning": 2, "critical": 3, "error": 3}

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


def _get_request_price(price_cache: Dict[str, float], symbol: str) -> float:
    """Return a symbol price using a request-local cache to avoid repeated fetches."""
    cached = price_cache.get(symbol)
    if cached is not None:
        return cached
    price = float(binance_client.get_symbol_price(symbol))
    price_cache[symbol] = price
    return price


def _enrich_entries(entries: list, current_price: float, side: str) -> list:
    """Enrich escalamiento entries with per-entry net PnL after Binance fees."""
    from binance_commission import total_commission
    result = []
    is_short = str(side).upper() in ('SHORT', 'SELL')
    for e in entries:
        ep = float(e.get('entry_price', 0) or 0)
        qty = float(e.get('qty', 0) or 0)
        if ep > 0 and qty > 0:
            if is_short:
                gross = (ep - current_price) * qty
            else:
                gross = (current_price - ep) * qty
            _comm = total_commission(qty, ep, current_price, 'spot', None)
            pnl = round(gross - _comm, 4)
            pnl_pct = round((pnl / (ep * qty)) * 100, 2) if ep * qty > 0 else 0.0
        else:
            pnl = 0.0
            pnl_pct = 0.0
        result.append({**e, "pnl_usdt": pnl, "pnl_pct": pnl_pct})
    return result


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
    # P0.1: Warmup PnL predictor — preload learned weights before first trade cycle
    try:
        from pnl_predictor import PnLPredictor
        _predictor = PnLPredictor()
        _warmup_syms = list(_cfg_param(settings, 'futures_symbols'))
        _top_spot = list(_cfg_param(settings, 'analysis_symbols'))[:10]
        for _s in _top_spot:
            if _s not in _warmup_syms:
                _warmup_syms.append(_s)
        _loaded = 0
        for _sym in _warmup_syms:
            try:
                _predictor.load_learned_weights(_sym)
                _loaded += 1
            except Exception:
                pass
        print(f"Predictor warmup: loaded weights for {_loaded}/{len(_warmup_syms)} symbols")
    except Exception as _pw_err:
        print(f"Predictor warmup error (non-fatal): {_pw_err}")
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
    # ── Restore hot-config overrides persisted in trading_runtime_state ────────
    # Use the canonical helper so startup restore matches runtime governance logic.
    try:
        _comp_s = get_components()
        _live_s = _comp_s.settings
        _restore_result = config_helpers.load_runtime_overrides_from_db(_live_s, db_client)
        if _restore_result.get("applied_scalars"):
            print(f"Config restore: {_restore_result['applied_scalars']} kelly overrides re-applied from DB")
        for _ct_attr in _restore_result.get("applied_thresholds", []):
            _ct_val = getattr(_live_s, _ct_attr, {}) or {}
            print(f"Config restore: {_ct_attr} re-applied ({list(_ct_val.keys())})")
    except Exception as _re:
        print(f"Config restore error (non-fatal): {_re}")
    # Refresh live commission rates from Binance API (Binance-controlled values)
    try:
        from binance_commission import refresh_config_from_binance
        from run_trades import _get_futures_client
        _comp_c = get_components()
        _refresh = refresh_config_from_binance(
            _comp_c.settings,
            spot_client=_comp_c.binance_client,
            futures_client=_get_futures_client(_comp_c.settings),
            db_client=_comp_c.db_client,
        )
        print(f"Commission refresh: spot={_refresh.get('spot_rate')} futures={_refresh.get('futures_rate')} use_bnb={_refresh.get('use_bnb')}")
    except Exception as e:
        print(f"Commission refresh error (non-fatal): {e}")
    # Startup sanity check: BNB auto-management must be enabled for commission discount
    try:
        _comp_s = get_components()
        _s_s = _comp_s.settings
        if not getattr(_s_s, 'auto_bnb_for_commission', True):
            _msg = "AUTO_BNB_FOR_COMMISSION is disabled — BNB discount will not be auto-maintained"
            print(f"[BNB] WARNING: {_msg}")
            if _comp_s.db_client:
                _comp_s.db_client.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                    ('__config__', 'system', 'bnb_auto_disabled_warning',
                     json.dumps({'message': _msg}), False)
                )
    except Exception as e:
        print(f"[BNB] startup sanity check error (non-fatal): {e}")
    # Start background periodic config snapshot task for traceability
    try:
        asyncio.create_task(_config_snapshot_loop())
    except Exception as e:
        print(f"[ConfigGovernance] failed to start snapshot loop: {e}")
    # Start background periodic config drift monitoring
    try:
        asyncio.create_task(_config_drift_loop())
    except Exception as e:
        print(f"[ConfigGovernance] failed to start drift loop: {e}")
    # Start SMART result-based health recheck loop
    try:
        asyncio.create_task(_health_results_recheck_loop())
    except Exception as e:
        print(f"[SMART-HEALTH] failed to start recheck loop: {e}")


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
        # Add corruption flags if they don't exist (idempotent)
        try:
            comp.db_client.execute_update(
                "ALTER TABLE trades_closed ADD COLUMN IF NOT EXISTS is_corrupted BOOLEAN DEFAULT FALSE",
                ()
            )
        except Exception:
            pass
        try:
            comp.db_client.execute_update(
                "ALTER TABLE trades_closed ADD COLUMN IF NOT EXISTS corrupt BOOLEAN DEFAULT FALSE",
                ()
            )
        except Exception:
            pass
        # Mark operational/non-strategic exits as corrupted (legacy + runtime side effects).
        # This keeps learning/parity clean from exits that do not represent strategy alpha.
        _CORRUPTED_REASONS = (
            'portfolio_rebalancing', 'manual_rebalance',
            'prediction_reallocation', 'prediction_realloc',
            'active_replacement', 'regime_futures_short_spot_cleanup',
            'excluded_symbol_cleanup', 'manual_liquidation_idle_capital',
            'SL_BROKEN_EXCL',
        )
        placeholders = ', '.join(['%s'] * len(_CORRUPTED_REASONS))
        rows_before = comp.db_client.execute_query(
            "SELECT COUNT(*) AS n FROM trades_closed WHERE is_corrupted = TRUE OR corrupt = TRUE", ()
        )
        before = rows_before[0]['n'] if rows_before else 0
        sql_mark = f"""
            UPDATE trades_closed
            SET is_corrupted = TRUE,
                corrupt = TRUE
            WHERE close_reason IN ({placeholders})
              AND (
                is_corrupted IS NULL OR is_corrupted = FALSE
                OR corrupt IS NULL OR corrupt = FALSE
              )
        """
        comp.db_client.execute_update(sql_mark, _CORRUPTED_REASONS)
        # Count how many are now marked
        rows = comp.db_client.execute_query(
            "SELECT COUNT(*) AS n FROM trades_closed WHERE is_corrupted = TRUE OR corrupt = TRUE", ()
        )
        marked_total = rows[0]['n'] if rows else 0
        newly_marked = max(0, marked_total - before)
        try:
            comp.db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                (
                    '__config__',
                    'system',
                    'corrupted_trades_marked',
                    json.dumps({
                        'reasons': list(_CORRUPTED_REASONS),
                        'newly_marked': newly_marked,
                        'total_corrupted': marked_total,
                    }, default=str),
                    False,
                ),
            )
        except Exception:
            pass
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
            "newly_marked": newly_marked,
            "corrupted_trades_marked": marked_total,
            "reasons": list(_CORRUPTED_REASONS),
            "message": (
                f"{newly_marked} newly marked; {marked_total} total corrupted trades. "
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


@app.get("/reconciliation/spot")
async def get_spot_reconciliation(_auth: None = Depends(_require_api_key)) -> Dict:
    """Current SPOT parity status and persisted reconciliation report."""
    try:
        comp = get_components()
        if not comp.position_manager or not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        parity = evaluate_spot_parity(comp.position_manager, comp.binance_client, settings=settings)
        persisted = get_truth_status_spot(comp.db_client)
        return {
            "ok": parity.get("ok", True),
            "severity": parity.get("severity", "unknown"),
            "parity": parity,
            "persisted": persisted,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reconciliation/spot/run")
async def run_spot_reconciliation(dry_run: bool = True, _auth: None = Depends(_require_api_key)) -> Dict:
    """Run SPOT auto-reconciliation; dry_run=True returns only recommendations."""
    try:
        comp = get_components()
        if not comp.position_manager or not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        report = auto_reconcile_spot(
            comp.position_manager, comp.binance_client, comp.db_client, settings=settings, dry_run=dry_run
        )
        return {
            "status": "ok",
            "dry_run": dry_run,
            "report": report,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reconciliation/futures")
async def get_futures_reconciliation(_auth: None = Depends(_require_api_key)) -> Dict:
    """Current FUTURES parity status and persisted reconciliation report."""
    try:
        from run_trades import _get_futures_client
        comp = get_components()
        if not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        fc = _get_futures_client(settings, db_client=comp.db_client)
        fc._binance_client_ref = comp.binance_client
        parity = evaluate_futures_parity(fc)
        persisted = get_truth_status(comp.db_client)
        return {
            "ok": parity.get("ok", True),
            "severity": parity.get("severity", "unknown"),
            "parity": parity,
            "persisted": persisted,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reconciliation/futures/run")
async def run_futures_reconciliation(dry_run: bool = True, _auth: None = Depends(_require_api_key)) -> Dict:
    """Run FUTURES auto-reconciliation; dry_run=True returns only recommendations."""
    try:
        from run_trades import _get_futures_client
        comp = get_components()
        if not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        fc = _get_futures_client(settings, db_client=comp.db_client)
        fc._binance_client_ref = comp.binance_client
        report = auto_reconcile_futures(fc, comp.db_client, dry_run=dry_run)
        return {
            "status": "ok",
            "dry_run": dry_run,
            "report": report,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reconcile/cleanup-dust")
async def cleanup_dust(dry_run: bool = True, _auth: None = Depends(_require_api_key)) -> Dict:
    """
    Sell all SPOT balances in Binance that are below $5 notional.
    For each symbol: if balance*price < min_notional, buy the delta needed to reach
    min_notional, then sell the whole balance. dry_run=True returns plan only.
    """
    try:
        comp = get_components()
        if not comp.binance_client or not comp.position_manager:
            raise HTTPException(status_code=503, detail="Components not initialized")
        client = comp.binance_client
        balances = client.get_account_balance()
        plan = []
        executed = []
        excluded = {"USDT", "BNB", "BTTC", "WIN", "JST", "IQ"}
        for asset, amount in balances.items():
            if asset in excluded or amount <= 0:
                continue
            symbol = f"{asset}USDT"
            try:
                price = client.get_symbol_price(symbol)
                notional = amount * price
                filters = get_symbol_filters(client, symbol)
                min_notional = filters.get('min_notional', 5.0)
                if notional >= min_notional:
                    continue
                if notional <= 0:
                    continue
                delta = get_min_notional_delta(client, symbol, price, target_notional_usdt=max(min_notional, 5.0))
                plan.append({
                    "symbol": symbol,
                    "balance": amount,
                    "notional": round(notional, 4),
                    "buy_qty": delta.get('buy_qty'),
                    "sell_qty": delta.get('sell_qty'),
                    "min_notional": min_notional,
                })
                if not dry_run:
                    if delta.get('needed') and delta.get('buy_qty', 0) > 0:
                        client.create_market_order(symbol=symbol, side="BUY", quantity=delta['buy_qty'])
                    sell_qty = delta.get('sell_qty', amount)
                    if sell_qty > 0:
                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=sell_qty)
                        executed.append({"symbol": symbol, "sell_qty": sell_qty, "order": order.get('orderId')})
            except Exception as e:
                plan.append({"symbol": symbol, "error": str(e)})
        return {
            "status": "ok",
            "dry_run": dry_run,
            "plan": plan,
            "executed": executed,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics/ghosts")
async def get_ghost_metrics(_auth: None = Depends(_require_api_key)) -> Dict:
    """Current DB/Binance parity metrics for ghost/orphan/dust detection."""
    try:
        comp = get_components()
        spot_parity = evaluate_spot_parity(comp.position_manager, comp.binance_client, settings=settings, min_notional_usdt=5.0)
        futures_parity = {"ok": True, "severity": "ok", "mismatch_count": 0}
        try:
            from run_trades import _get_futures_client
            fc = _get_futures_client(settings, db_client=comp.db_client)
            fc._binance_client_ref = comp.binance_client
            futures_parity = evaluate_futures_parity(fc)
        except Exception as fe:
            futures_parity = {"ok": False, "error": str(fe)}
        spot_status = get_truth_status_spot(comp.db_client)
        futures_status = get_truth_status(comp.db_client)
        return {
            "ok": spot_parity.get("ok", True) and futures_parity.get("ok", True),
            "spot": {
                "ok": spot_parity.get("ok", True),
                "severity": spot_parity.get("severity", "unknown"),
                "db_count": spot_parity.get("db_count", 0),
                "binance_count": spot_parity.get("binance_count", 0),
                "mismatch_count": spot_parity.get("mismatch_count", 0),
                "dust_skipped_count": spot_parity.get("dust_skipped_count", 0),
                "last_status": spot_status,
            },
            "futures": {
                "ok": futures_parity.get("ok", True),
                "severity": futures_parity.get("severity", "unknown"),
                "mismatch_count": futures_parity.get("mismatch_count", 0),
                "last_status": futures_status,
            },
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/system/position-reconciliation")
async def get_position_reconciliation_health(_auth: None = Depends(_require_api_key)) -> Dict:
    """Health check: return current SPOT/FUTURES reconciliation discrepancies.

    Useful for dashboard and alerting. Does not mutate state.
    """
    try:
        comp = get_components()
        if not comp.position_manager or not comp.binance_client:
            raise HTTPException(status_code=503, detail="Components not initialized")
        spot_parity = evaluate_spot_parity(comp.position_manager, comp.binance_client, settings=settings)
        spot_status = get_truth_status_spot(comp.db_client)
        futures_parity = {}
        futures_status = {}
        try:
            fc = _get_futures_client(settings, db_client=comp.db_client)
            fc._binance_client_ref = comp.binance_client
            futures_parity = evaluate_futures_parity(fc)
            futures_status = get_truth_status_futures(comp.db_client)
        except Exception as _fe:
            futures_parity = {"ok": False, "error": str(_fe)}
        _healthy = bool(spot_parity.get("ok", True)) and bool(futures_parity.get("ok", True))
        return {
            "healthy": _healthy,
            "spot": {
                "ok": spot_parity.get("ok", True),
                "severity": spot_parity.get("severity", "unknown"),
                "discrepancies": spot_parity.get("discrepancies", []),
                "last_status": spot_status,
            },
            "futures": {
                "ok": futures_parity.get("ok", True),
                "severity": futures_parity.get("severity", "unknown"),
                "discrepancies": futures_parity.get("discrepancies", []),
                "last_status": futures_status,
            },
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


@app.get("/audit/consistency")
async def audit_consistency(limit: int = 100, _auth: None = Depends(_require_api_key)) -> Dict:
    """Check that recent signal decisions are mirrored in trades_audit."""
    comp = get_components()
    return check_signal_audit_consistency(comp.db_client, limit=limit)


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
    persist_result = add_signal(signal, strict_persistence=True)
    if not persist_result.get("ok") or persist_result.get("persisted") != "db":
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Signal persistence failed",
        )
    return {
        "signal_registered": True,
        "signal": persist_result.get("record", signal),
        "persisted": persist_result.get("persisted"),
    }


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


class PauseEntriesRequest(BaseModel):
    reason: Optional[str] = None
    ttl_minutes: Optional[float] = 180.0


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


@app.post("/control/resume-all")
async def resume_all(payload: ControlRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Resume full operation: RUNNING state, entries unpaused, operator alerts cleared."""
    if not db_client:
        raise HTTPException(status_code=503, detail="DB not available")

    _before_state = orchestrator.get_state()
    _before_pause = _get_pause_new_entries_status()
    _before_alerts = _list_active_operator_alerts()
    _resume_reason = (payload.reason or "manual_resume_all").strip() or "manual_resume_all"

    _orchestrator_started = False
    if not orchestrator.is_running():
        _orchestrator_started = bool(orchestrator.start(changed_by="api", reason=_resume_reason))

    db_client.execute_update(
        "DELETE FROM trading_runtime_state WHERE key=%s",
        ('pause_new_entries',),
    )

    _cleared_alert_types = [str(a.get("alert_type") or "") for a in _before_alerts if a.get("alert_type")]
    if _cleared_alert_types:
        db_client.execute_update(
            "DELETE FROM trading_runtime_state WHERE key LIKE %s",
            ('operator_alert_%',),
        )

    _after_state = orchestrator.get_state()
    _after_pause = _get_pause_new_entries_status()
    _after_alerts = _list_active_operator_alerts()
    _unified = _build_unified_system_status(_after_state, _after_pause, _after_alerts)

    try:
        _audit_payload = {
            "reason": _resume_reason,
            "before": {
                "system_state": _before_state,
                "pause_new_entries": _before_pause,
                "operator_alerts_count": len(_before_alerts),
            },
            "after": {
                "system_state": _after_state,
                "pause_new_entries": _after_pause,
                "operator_alerts_count": len(_after_alerts),
            },
            "actions": {
                "orchestrator_started": _orchestrator_started,
                "pause_cleared": bool(_before_pause.get("active")),
                "alerts_cleared": _cleared_alert_types,
            },
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', 'resume_all', json.dumps(_audit_payload), False),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "before": {
            "system_state": _before_state,
            "pause_new_entries": _before_pause,
            "operator_alerts_count": len(_before_alerts),
        },
        "after": {
            "system_state": _after_state,
            "pause_new_entries": _after_pause,
            "operator_alerts_count": len(_after_alerts),
        },
        "actions": {
            "orchestrator_started": _orchestrator_started,
            "pause_cleared": bool(_before_pause.get("active")),
            "alerts_cleared": _cleared_alert_types,
        },
        "unified_status": _unified,
    }


@app.post("/control/pause-new-entries")
async def pause_new_entries(payload: PauseEntriesRequest, _auth: None = Depends(_require_api_key)) -> Dict:
    """Pause only new entries via runtime-state key with optional TTL auto-expiry."""
    if not db_client:
        raise HTTPException(status_code=503, detail="DB not available")
    _now = datetime.now(timezone.utc)
    _ttl = None
    _expires_at = None
    if payload.ttl_minutes is not None:
        try:
            _ttl = max(1.0, float(payload.ttl_minutes))
            _expires_at = _now + timedelta(minutes=_ttl)
        except Exception:
            raise HTTPException(status_code=400, detail="ttl_minutes must be numeric")
    _value = {
        "paused": True,
        "reason": (payload.reason or "manual_pause_new_entries").strip() or "manual_pause_new_entries",
        "started_at": _now.isoformat(),
        "ttl_minutes": _ttl,
        "expires_at": _expires_at.isoformat() if _expires_at else None,
        "changed_by": "api",
    }
    _payload = json.dumps(_value)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        ('pause_new_entries', _payload, _payload),
    )
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', 'pause_new_entries', _payload, False),
        )
    except Exception:
        pass
    return {
        "ok": True,
        "pause_new_entries": _get_pause_new_entries_status(),
    }


@app.delete("/control/pause-new-entries")
async def resume_new_entries(_auth: None = Depends(_require_api_key)) -> Dict:
    """Resume new entries by removing pause_new_entries key."""
    if not db_client:
        raise HTTPException(status_code=503, detail="DB not available")
    _before = _get_pause_new_entries_status()
    db_client.execute_update(
        "DELETE FROM trading_runtime_state WHERE key=%s",
        ('pause_new_entries',),
    )
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            (
                '__config__',
                'system',
                'resume_new_entries',
                json.dumps({
                    "resumed_at": datetime.now(timezone.utc).isoformat(),
                    "previous_pause": _before,
                    "changed_by": "api",
                }),
                False,
            ),
        )
    except Exception:
        pass
    return {
        "ok": True,
        "pause_new_entries": _get_pause_new_entries_status(),
    }


@app.get("/control/status")
async def get_control_status() -> Dict:
    """Get current system control status."""
    _state = orchestrator.get_state()
    _pause = _get_pause_new_entries_status()
    _alerts = _list_active_operator_alerts()
    _unified = _build_unified_system_status(_state, _pause, _alerts)
    return {
        **_state,
        "pause_new_entries": _pause,
        "operator_alerts_active": {
            "count": len(_alerts),
            "alerts": _alerts,
        },
        "unified_status": _unified,
    }


@app.get("/system/operator-alerts")
async def get_operator_alerts() -> Dict:
    """Return all active (unacknowledged) operator alerts persisted by run_trades.py.
    These are non-blocking notifications that require the operator to take action
    (e.g. press STOP manually) — they never auto-pause the system.
    """
    if not db_client:
        return {"ok": False, "alerts": [], "error": "DB not available"}
    try:
        import json as _json
        rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state "
            "WHERE key LIKE %s ORDER BY updated_at DESC",
            ('operator_alert_%',),
        )
        alerts = []
        for row in (rows or []):
            try:
                payload = row.get("value") or {}
                if isinstance(payload, str):
                    payload = _json.loads(payload)
                if not isinstance(payload, dict):
                    continue
                if not payload.get("acknowledged", False):
                    alerts.append({
                        "key": row.get("key"),
                        "alert_type": payload.get("alert_type"),
                        "message": payload.get("message"),
                        "ts": payload.get("ts"),
                        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
                    })
            except Exception:
                continue
        return {"ok": True, "alerts": alerts, "count": len(alerts)}
    except Exception as e:
        return {"ok": False, "alerts": [], "error": str(e)}


@app.post("/system/operator-alerts/{alert_type}/ack")
async def ack_operator_alert(alert_type: str, _auth: None = Depends(_require_api_key)) -> Dict:
    """Acknowledge an operator alert (removes it from the active list)."""
    if not db_client:
        raise HTTPException(status_code=503, detail="DB not available")
    try:
        import json as _json
        _key = f"operator_alert_{alert_type}"
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s", (_key,)
        )
        if not rows:
            return {"ok": True, "acknowledged": False, "reason": "alert not found"}
        payload = rows[0].get("value") or {}
        if isinstance(payload, str):
            payload = _json.loads(payload)
        if isinstance(payload, dict):
            payload["acknowledged"] = True
        _updated = _json.dumps(payload)
        db_client.execute_update(
            "UPDATE trading_runtime_state SET value=%s, updated_at=NOW() WHERE key=%s",
            (_updated, _key),
        )
        return {"ok": True, "acknowledged": True, "alert_type": alert_type}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check() -> Dict:
    """Get overall health status of the system."""
    health = orchestrator.get_health_status()
    try:
        comp = get_components()
        health["audit_consistency"] = check_signal_audit_consistency(comp.db_client, limit=100)
    except Exception as e:
        health["audit_consistency"] = {
            "available": False,
            "error": str(e),
        }
    return health


@app.get("/performance/commissions")
async def get_commission_summary(hours: int = 24) -> Dict:
    """Commission and net PnL breakdown for the last N hours (default 24h), split by SPOT and FUTURES."""
    try:
        comp = get_components()
        if not comp.db_client:
            raise HTTPException(status_code=503, detail="DB not available")
        from datetime import datetime, timezone, timedelta
        import json as _json
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = comp.db_client.execute_query(
            """SELECT symbol, quantity, entry_price, exit_price, pnl,
                      entry_commission, exit_commission, metadata
               FROM trades_closed WHERE exit_time > %s""",
            (cutoff,)
        )
        spot_gross = spot_comm = spot_trades = 0.0
        fut_gross  = fut_comm  = fut_trades  = 0.0
        _spot_rate = config_helpers.get_param(settings, 'binance_commission_rate')
        _fut_rate  = config_helpers.get_param(settings, 'futures_commission_rate')
        for r in rows:
            meta = r.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception as _meta_err:
                    logger.warning("[PnL breakdown] metadata parse failed for %s: %s", r.get('symbol'), _meta_err)
                    meta = {'_metadata_corrupt_raw': True}
            ttype = meta.get('trade_type', 'spot')
            pnl  = float(r.get('pnl') or 0)
            qty  = float(r.get('quantity') or 0)
            ep   = float(r.get('entry_price') or 0)
            xp   = float(r.get('exit_price') or ep)
            ec   = r.get('entry_commission')
            xc   = r.get('exit_commission')
            # Fase 2.6: prefer real average prices from fill metadata when available
            _real_ep = float(meta.get('entry_avg_price', 0) or ep)
            _real_xp = float(meta.get('exit_avg_price', 0) or xp)
            if ttype == 'futures':
                fut_trades += 1
                fut_gross  += pnl
                if ec is not None and xc is not None:
                    fut_comm += float(ec) + float(xc)
                else:
                    fut_comm += (_real_ep * qty + _real_xp * qty) * _fut_rate
            else:
                spot_trades += 1
                spot_gross  += pnl
                if ec is not None and xc is not None:
                    spot_comm += float(ec) + float(xc)
                else:
                    spot_comm += (_real_ep * qty + _real_xp * qty) * _spot_rate
        total_gross = spot_gross + fut_gross
        total_comm  = spot_comm + fut_comm
        net_pnl     = total_gross - total_comm
        return {
            "window_hours": hours,
            "futures": {
                "trades": int(fut_trades),
                "gross_pnl": round(fut_gross, 4),
                "estimated_commissions": round(fut_comm, 4),
                "net_pnl": round(fut_gross - fut_comm, 4),
                "commission_rate_used": _fut_rate,
            },
            "spot": {
                "trades": int(spot_trades),
                "gross_pnl": round(spot_gross, 4),
                "estimated_commissions": round(spot_comm, 4),
                "net_pnl": round(spot_gross - spot_comm, 4),
                "commission_rate_used": _spot_rate,
            },
            "total": {
                "trades": int(fut_trades + spot_trades),
                "gross_pnl": round(total_gross, 4),
                "estimated_commissions": round(total_comm, 4),
                "net_pnl": round(net_pnl, 4),
                "commission_pct_of_gross": round(total_comm / total_gross * 100, 2) if total_gross != 0 else 0.0,
            },
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        import asyncio as _asyncio
        balances = await _asyncio.to_thread(binance_client.get_account_balance)
        total_usdt = 0.0
        price_cache: Dict[str, float] = {}
        
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
                    price = _get_request_price(price_cache, symbol)
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
        _runtime_ctx = _get_dashboard_context_reference()
        price_cache: Dict[str, float] = {}
        
        for symbol, pos_list in positions.items():
            # Fetch current price once per symbol
            try:
                current_price = float(_get_request_price(price_cache, symbol))
            except Exception:
                current_price = None
            for entry_num, position in enumerate(pos_list, start=1):
                entry_price = float(position.entry_price)
                quantity    = float(position.quantity)
                if current_price is not None:
                    cp = current_price
                    if position.side.upper() == "BUY":
                        _gross_unrealized = (cp - entry_price) * quantity
                    else:
                        _gross_unrealized = (entry_price - cp) * quantity
                    # C0: return net PnL after Binance fees
                    from binance_commission import total_commission as _open_total_comm
                    _comm_unrealized = _open_total_comm(quantity, entry_price, cp, 'spot', settings)
                    unrealized_pnl = round(_gross_unrealized - _comm_unrealized, 4)
                else:
                    cp = entry_price
                    unrealized_pnl = 0.0

                unrealized_pnl_pct = round((unrealized_pnl / (entry_price * quantity)) * 100, 4) if entry_price > 0 and quantity > 0 else 0.0
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
                    **_normalize_transaction_context(getattr(position, 'metadata', {}) or {}, _runtime_ctx),
                })
        
        _context_counts = {
            "legacy": sum(1 for _p in positions_data if _p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for _p in positions_data if _p.get('config_context_state') == 'partial'),
            "current": sum(1 for _p in positions_data if _p.get('config_context_state') == 'current'),
            "unknown": sum(1 for _p in positions_data if _p.get('config_context_state') == 'unknown'),
        }
        return {
            "positions": positions_data,
            "count": len(positions_data),
            "legacy_count": _context_counts["legacy"],
            "partial_context_count": _context_counts["partial"],
            "current_context_count": _context_counts["current"],
            "unknown_context_count": _context_counts["unknown"],
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
    price_cache: Dict[str, float] = {}
    for symbol, pos_list in all_positions.items():
        total_qty = sum(float(p.quantity) for p in pos_list)
        try:
            try:
                filters = get_symbol_filters(binance_client, symbol)
                total_qty = align_quantity_to_step(total_qty, filters["step"])
            except Exception:
                pass
            price = float(_get_request_price(price_cache, symbol))
            order = binance_client.create_market_order(symbol=symbol, side="SELL", quantity=total_qty)
            commission_rate = config_helpers.get_param(settings, 'binance_commission_rate')
            position_manager.close_all_positions_for_symbol(symbol, price, "manual_rebalance", commission_rate)
            results.append({"symbol": symbol, "qty": total_qty, "price": price, "status": "sold", "entries_closed": len(pos_list)})
        except Exception as e:
            results.append({"symbol": symbol, "status": "error", "error": str(e)})
    return {"closed": results, "count": len(results)}


@app.get("/positions/{symbol}/close/preview")
async def preview_position_close(symbol: str, trade_type: str = "auto", _auth: None = Depends(_require_api_key)) -> Dict:
    """Preview what would happen when manually closing a position. Does NOT mutate state."""
    import manual_position_closer as mpc
    comp = get_components()
    sym = symbol.upper()
    tt = trade_type.lower()
    if tt == "auto":
        db_rows = db_client.execute_query(
            "SELECT metadata FROM trades_open WHERE symbol=%s LIMIT 5", (sym,)
        ) if db_client else []
        tt = "spot"
        for row in db_rows:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if meta.get("trade_type") == "futures":
                tt = "futures"
                break
    if tt == "futures":
        return mpc.preview_futures(sym, comp)
    return mpc.preview_spot(sym, comp)


@app.post("/positions/{symbol}/close")
async def close_position_manual(
    symbol: str,
    trade_type: str = "auto",
    side: str = "SHORT",
    ghost: bool = False,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """
    Manually close a single open position.
    - SPOT: reads real Binance balance, auto top-up if below minNotional, sells and closes DB.
    - FUTURES: cancels SL/TP algos, closes position, verifies amt=0, closes DB.
    - ghost=true: close DB record only (no real Binance balance to sell).
    Marked as manual_close + exclude_from_analysis in metadata.
    """
    import manual_position_closer as mpc
    comp = get_components()
    sym = symbol.upper()
    tt = trade_type.lower()
    if tt == "auto":
        db_rows = db_client.execute_query(
            "SELECT metadata FROM trades_open WHERE symbol=%s LIMIT 5", (sym,)
        ) if db_client else []
        tt = "spot"
        for row in db_rows:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if meta.get("trade_type") == "futures":
                tt = "futures"
                break
    try:
        if ghost:
            return mpc.close_ghost_db(sym, comp, source="dashboard")
        if tt == "futures":
            return mpc.execute_futures(sym, side.upper(), comp, source="dashboard")
        return mpc.execute_spot(sym, comp, source="dashboard")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/positions/closed")
async def get_closed_positions(limit: int = 20) -> Dict:
    """Get last N closed positions."""
    try:
        closed_positions = position_manager.get_closed_positions(limit)
        positions_data = []
        _runtime_ctx = _get_dashboard_context_reference()
        
        for pos in closed_positions:
            positions_data.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "invested_usdt": round(float(pos.entry_price or 0) * float(pos.quantity or 0), 4),
                "pnl": pos.pnl,
                "pnl_percentage": pos.pnl_percentage,
                "entry_time": to_mexico_time_str(pos.entry_time),
                "exit_time": to_mexico_time_str(pos.exit_time),
                "close_reason": pos.close_reason,
                "entry_commission": pos.entry_commission,
                "exit_commission": pos.exit_commission,
                "bounce_entry": (getattr(pos, 'metadata', None) or {}).get('bounce_entry', False),
                **_normalize_transaction_context(getattr(pos, 'metadata', {}) or {}, _runtime_ctx),
            })
        
        _context_counts = {
            "legacy": sum(1 for _p in positions_data if _p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for _p in positions_data if _p.get('config_context_state') == 'partial'),
            "current": sum(1 for _p in positions_data if _p.get('config_context_state') == 'current'),
            "unknown": sum(1 for _p in positions_data if _p.get('config_context_state') == 'unknown'),
        }
        return {
            "positions": positions_data,
            "count": len(positions_data),
            "legacy_count": _context_counts["legacy"],
            "partial_context_count": _context_counts["partial"],
            "current_context_count": _context_counts["current"],
            "unknown_context_count": _context_counts["unknown"],
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
                    _px = _get_request_price(price_cache, _sym)
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
_REGIME_CACHE_TTL_SECONDS = 60
_MODE_DESC = {
    "SPOT_BULL": "Compras en breakout con Kelly dinamico. Trailing stop activo. Scale-out en TP.",
    "RANGE_SCALP": "Compras en soporte BB inferior (RSI<55), venta en BB superior. TP ajustado 1.5%.",
    "PRESERVATION": "Capital 80%+ en USDT. Solo NARRATIVE con VolSpike>=2x. Kelly x0.5.",
    "FUTURES_SHORT": "Shorts 2x en pares Tier A (BTCUSDT, ETHUSDT, SOLUSDT...). LONG en soporte. SL SHORT 3% / LONG 2.5% | TP 7%. Hedge mode activo.",
}
_MODE_COLOR = {
    "SPOT_BULL": "#4ecca3",
    "RANGE_SCALP": "#f39c12",
    "PRESERVATION": "#e74c3c",
    "FUTURES_SHORT": "#9b59b6",
}
_PROFILE_ALIAS = {
    "A": "Agresivo",
    "B": "Balance",
    "C": "Defensa",
}


def _decode_runtime_state_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _get_pause_new_entries_status() -> Dict[str, Any]:
    """Return normalized pause_new_entries status including TTL semantics."""
    _status = {
        "active": False,
        "reason": None,
        "started_at": None,
        "expires_at": None,
        "remaining_minutes": None,
        "source": "runtime_state",
    }
    if not db_client:
        _status["source"] = "no_db"
        return _status
    try:
        _rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
            ('pause_new_entries',),
        )
        if not _rows:
            return _status

        _raw = _rows[0].get('value')
        _val = _decode_runtime_state_value(_raw)
        if not isinstance(_val, dict):
            _val = {"value": _val}

        def _parse_ts(raw):
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            if isinstance(raw, str):
                try:
                    _dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    return _dt if _dt.tzinfo else _dt.replace(tzinfo=timezone.utc)
                except Exception:
                    return None
            return None

        _paused = bool(_val.get('paused', False))
        _reason = str(_val.get('reason') or '').strip() or None
        _started_at = _parse_ts(_val.get('started_at')) or _rows[0].get('updated_at')
        _expires_at = _parse_ts(_val.get('expires_at'))
        if _expires_at is None and _started_at is not None:
            _ttl_raw = _val.get('ttl_minutes', _val.get('max_minutes'))
            if _ttl_raw is not None:
                try:
                    _ttl = max(1.0, float(_ttl_raw))
                    _expires_at = _started_at + timedelta(minutes=_ttl)
                except Exception:
                    _expires_at = None

        _now = datetime.now(timezone.utc)
        _remaining = None
        _active = _paused
        if _expires_at is not None:
            _remaining = round((_expires_at - _now).total_seconds() / 60.0, 2)
            if _remaining <= 0:
                _active = False
                _remaining = 0.0

        _status.update({
            "active": _active,
            "reason": _reason,
            "started_at": _started_at.isoformat() if _started_at else None,
            "expires_at": _expires_at.isoformat() if _expires_at else None,
            "remaining_minutes": _remaining,
            "raw": _val,
        })
        return _status
    except Exception as _e:
        _status["error"] = str(_e)
        return _status


def _list_active_operator_alerts() -> List[Dict[str, Any]]:
    """Return active operator alerts stored in runtime_state."""
    if not db_client:
        return []
    _alerts: List[Dict[str, Any]] = []
    try:
        _rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state "
            "WHERE key LIKE %s ORDER BY updated_at DESC",
            ('operator_alert_%',),
        )
        for _row in (_rows or []):
            try:
                _payload = _decode_runtime_state_value(_row.get('value') or {})
                if not isinstance(_payload, dict):
                    continue
                if bool(_payload.get('acknowledged', False)):
                    continue
                _alerts.append({
                    "key": _row.get('key'),
                    "alert_type": _payload.get('alert_type'),
                    "message": _payload.get('message'),
                    "ts": _payload.get('ts'),
                    "updated_at": _row.get('updated_at').isoformat() if _row.get('updated_at') else None,
                })
            except Exception:
                continue
    except Exception:
        return []
    return _alerts


def _build_unified_system_status(
    system_state: Optional[Dict[str, Any]] = None,
    pause_state: Optional[Dict[str, Any]] = None,
    alerts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a single operational status used by dashboard and control panel."""
    _base = str((system_state or {}).get("state") or "UNKNOWN").strip().upper() or "UNKNOWN"
    _pause = pause_state or {}
    _entries_paused = bool(_pause.get("active"))
    _alerts = alerts or []

    if _base == "STOPPED":
        _effective = "STOPPED"
        _label = "Stopped"
        _dot = "stopped"
        _severity = "critical"
    elif _base == "PAUSED":
        _effective = "PAUSED"
        _label = "Paused"
        _dot = "paused"
        _severity = "warning"
    elif _base == "RUNNING" and _entries_paused:
        _effective = "RUNNING_ENTRIES_PAUSED"
        _label = "Running (Entradas en pausa)"
        _dot = "paused"
        _severity = "warning"
    elif _base == "RUNNING":
        _effective = "RUNNING_FULL"
        _label = "Running"
        _dot = "running"
        _severity = "ok"
    else:
        _effective = "UNKNOWN"
        _label = "Unknown"
        _dot = "stopped"
        _severity = "critical"

    return {
        "base_state": _base,
        "entries_paused": _entries_paused,
        "effective_state": _effective,
        "ui_label": _label,
        "dot_state": _dot,
        "severity": _severity,
        "can_open_new_entries": _base == "RUNNING" and not _entries_paused,
        "has_operator_alerts": len(_alerts) > 0,
        "operator_alerts_count": len(_alerts),
        "pause_reason": _pause.get("reason"),
        "pause_remaining_minutes": _pause.get("remaining_minutes"),
    }


def _get_effective_runtime_selection(summary_mode: Optional[str] = None) -> Dict[str, Any]:
    """Return effective mode/profile used by the live engine whenever possible."""
    _fallback_mode = str(summary_mode or "SPOT_BULL").strip().upper() or "SPOT_BULL"
    _mode = str(getattr(orchestrator, 'current_regime_mode', '') or '').strip().upper()
    _mode_source = "orchestrator" if _mode else "summary"
    _profile = str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper()
    _profile_source = "settings" if _profile else "runtime_state"
    _profile_updated_at = None

    try:
        _rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state "
            "WHERE key IN ('smart_total_active_mode', %s)",
            (f"regime_profile_active_{_mode or _fallback_mode}",),
        )
        _state = {str(r.get('key') or ''): r for r in (_rows or [])}

        if not _mode and _state.get('smart_total_active_mode'):
            _mv = _decode_runtime_state_value(_state['smart_total_active_mode'].get('value'))
            if isinstance(_mv, dict):
                _mode = str(_mv.get('mode') or _mv.get('selected_mode') or _mv.get('value') or '').strip().upper()
            elif isinstance(_mv, str):
                _mode = _mv.strip().upper()
            _mode_source = "runtime_state" if _mode else _mode_source

        if not _mode:
            _mode = _fallback_mode

        _profile_key = f"regime_profile_active_{_mode}"
        _pr_row = _state.get(_profile_key)
        if _pr_row is None:
            _pr_rows = db_client.execute_query(
                "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s",
                (_profile_key,),
            )
            if _pr_rows:
                _pr_row = _pr_rows[0]
        if _pr_row:
            _pv = _decode_runtime_state_value(_pr_row.get('value'))
            if isinstance(_pv, dict):
                _profile = str(_pv.get('profile') or _pv.get('label') or '').strip().upper()
            elif isinstance(_pv, str):
                _profile = _pv.strip().upper()
            _profile_source = "runtime_state" if _profile else _profile_source
            _ts = _pr_row.get('updated_at')
            _profile_updated_at = _ts.isoformat() if _ts else None
    except Exception:
        if not _mode:
            _mode = _fallback_mode

    if not _profile:
        _profile = "A"
        _profile_source = "default"

    # NOTE: do not apply harmony to the app-level settings singleton here.
    # The dashboard only needs the effective mode/profile labels; applying the
    # harmony multipliers in this read-only path would mutate settings and
    # produce false drift in _get_mode_profile_harmony_drift. The drift check
    # recomputes expected values from a fresh base settings object when needed.

    return {
        "mode": _mode,
        "mode_source": _mode_source,
        "profile_label": _profile,
        "profile_alias": _PROFILE_ALIAS.get(_profile, _profile),
        "profile_source": _profile_source,
        "profile_updated_at": _profile_updated_at,
    }


def _build_operator_focus(
    health: Dict[str, Any],
    health_results: Dict[str, Any],
    effective_runtime: Dict[str, Any],
    pause_state: Optional[Dict[str, Any]] = None,
    binance_truth: Optional[Dict[str, Any]] = None,
    binance_truth_spot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create an actionable operator queue from health/runtime signals."""
    items: List[Dict[str, Any]] = []
    checks = (health or {}).get("checks") or {}

    def _push(code: str, severity: str, title: str, detail: str, action: str) -> None:
        items.append({
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
            "action": action,
        })

    _overall = str((health or {}).get("overall") or "unknown").lower()
    _hr_overall = str((health_results or {}).get("overall") or "unknown").lower()
    _mode = str((effective_runtime or {}).get("mode") or "SPOT_BULL").upper()
    _profile = str((effective_runtime or {}).get("profile_label") or "A").upper()
    _harmony_drift = (health_results or {}).get("harmony_drift") or {}
    _drift_count = int(_harmony_drift.get("drift_count") or 0)
    _parity_validation = (health_results or {}).get("parity_validation") or {}
    _parity_status = str(_parity_validation.get("status") or "unknown").lower()
    _pause = pause_state or {}
    if bool(_pause.get("active")):
        _rem = _pause.get("remaining_minutes")
        _rem_txt = f"{_rem:.1f} min" if isinstance(_rem, (int, float)) else "indefinido"
        _push(
            "pause_new_entries",
            "critical",
            "Entradas nuevas pausadas",
            f"pause_new_entries activo. Razón={_pause.get('reason') or 'N/A'}; restante={_rem_txt}.",
            "Reanudar entradas solo cuando finalice auditoría y health/binance_truth estén en estado seguro.",
        )

    _cycle_fail = int((checks.get("cycle_failures") or {}).get("consecutive") or 0)
    if _cycle_fail >= 3:
        _push(
            "cycle_failures",
            "critical",
            "Fallos de ciclo consecutivos",
            f"Se detectaron {_cycle_fail} fallos consecutivos en execute_once.",
            "Revisar errores recientes y pausar nuevas entradas hasta recuperar estabilidad.",
        )

    _fapi = checks.get("futures_api") or {}
    if str(_fapi.get("status") or "").lower() == "error":
        _push(
            "futures_api",
            "critical",
            "Futures API degradada",
            str(_fapi.get("detail") or "No hay latencia válida para fapi.binance.com."),
            "Bloquear nuevas operaciones FUTURES_SHORT y mantener solo gestión defensiva.",
        )

    _exposure = checks.get("portfolio_exposure") or {}
    _exp_pct = float(_exposure.get("exposure_pct") or 0.0)
    _exp_max = float(_exposure.get("max_exposure_pct") or 75.0)
    if _exp_pct >= _exp_max:
        _push(
            "exposure_cap",
            "critical",
            "Cap de exposición alcanzado",
            f"Exposición actual {_exp_pct:.1f}% >= cap {_exp_max:.1f}%.",
            "Priorizar reducción de riesgo antes de abrir nuevas posiciones.",
        )
    elif _exp_pct >= (_exp_max * 0.85):
        _push(
            "exposure_high",
            "warning",
            "Exposición alta",
            f"Exposición actual {_exp_pct:.1f}% cerca del cap {_exp_max:.1f}%.",
            "Aplicar criterio estricto de calidad en nuevas entradas.",
        )

    if _hr_overall in ("critical", "error"):
        _push(
            "smart_health",
            "critical",
            "SMART Recheck crítico",
            "El health por resultados detecta deterioro de performance reciente.",
            "Reducir agresividad: priorizar perfil C y evitar expansión de riesgo.",
        )
    elif _hr_overall == "warning":
        _push(
            "smart_health_warn",
            "warning",
            "SMART Recheck en warning",
            "Se detectan señales de deriva o caída de calidad operativa.",
            "Monitorear métricas de SPOT/FUTURES y confirmar en próximo ciclo.",
        )

    if _drift_count > 0:
        _push(
            "harmony_drift",
            "warning",
            "Drift de armonía detectado",
            f"Se detectaron {_drift_count} diferencias entre el snapshot persistido y el runtime efectivo.",
            "Revisar la matriz aplicada antes de continuar con el rollout.",
        )

    _bt_sev = str((binance_truth or {}).get("severity") or "unknown").lower()
    if _bt_sev == "critical":
        _bt_mm = (binance_truth or {}).get("mismatches") or []
        _bt_syms = ", ".join({m.get("symbol", "") for m in _bt_mm[:3]})
        _push(
            "binance_truth_critical",
            "critical",
            "Paridad FUTURES crítica con Binance",
            f"mismatch_count={(binance_truth or {}).get('mismatch_count', 0)}. Símbolos: {_bt_syms or 'N/A'}.",
            "Revisar posiciones futures en Binance y reconciliar antes de nuevas entradas.",
        )

    _bts_sev = str((binance_truth_spot or {}).get("severity") or "unknown").lower()
    if _bts_sev == "high":
        _bts_mm = (binance_truth_spot or {}).get("mismatches") or []
        _bts_syms = ", ".join({m.get("symbol", "") for m in _bts_mm[:3]})
        _push(
            "binance_truth_spot_high",
            "critical",
            "Paridad SPOT crítica con Binance",
            f"mismatch_count={(binance_truth_spot or {}).get('mismatch_count', 0)}. Símbolos: {_bts_syms or 'N/A'}.",
            "Revisar posiciones SPOT en DB y Binance — posibles fantasmas o inconsistencias de balance.",
        )

    if _parity_status == "critical":
        _push(
            "parity_critical",
            "critical",
            "Paridad backtest-live crítica",
            "; ".join((_parity_validation.get("reasons") or [])[:2]) or "Gap crítico entre baseline y live.",
            "No promover rollout; revisar baseline, ventana comparada y divergencia live.",
        )
    elif _parity_status in ("warning", "inconclusive"):
        _push(
            "parity_watch",
            "warning",
            "Paridad backtest-live pendiente",
            "; ".join((_parity_validation.get("reasons") or [])[:2]) or "Paridad aún no concluyente.",
            "Acumular muestra adicional y revalidar antes de promover cambios.",
        )

    _sel_rows = db_client.execute_query(
        "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
        ("effective_runtime_selection_snapshot",),
    )
    _selection_snapshot = None
    if _sel_rows:
        _selection_snapshot = _decode_runtime_state_value(_sel_rows[0].get("value"))
        if isinstance(_selection_snapshot, dict):
            _selection_snapshot["updated_at"] = _sel_rows[0].get("updated_at").isoformat() if _sel_rows[0].get("updated_at") else None

    _transition_rows = db_client.execute_query(
        "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
        ("regime_transition_state",),
    )
    _transition_state = None
    if _transition_rows:
        _transition_state = _decode_runtime_state_value(_transition_rows[0].get("value"))
        if isinstance(_transition_state, dict):
            _transition_state["updated_at"] = _transition_rows[0].get("updated_at").isoformat() if _transition_rows[0].get("updated_at") else None
            if bool(_transition_state.get("active")):
                _pre = _transition_state.get("pre_switch") or {}
                _push(
                    "transition_active",
                    "info",
                    "Transición de régimen activa",
                    f"{_pre.get('from_mode') or '?'} -> {_pre.get('to_mode') or '?'} (conf={_pre.get('confidence') or 0}).",
                    "Verificar cierres defensivos y evitar entradas no confirmadas.",
                )

    if not items:
        _push(
            "all_clear",
            "ok",
            "Sistema estable",
            f"Modo {_mode} / Perfil {_profile} sin bloqueos críticos activos.",
            "Mantener ejecución normal con monitoreo periódico.",
        )

    _severity_rank = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    items.sort(key=lambda x: _severity_rank.get(str(x.get("severity") or "ok"), 99))

    return {
        "overall": _overall,
        "smart_overall": _hr_overall,
        "mode": _mode,
        "profile": _profile,
        "items": items[:5],
        "selection_snapshot": _selection_snapshot or {},
        "transition_state": _transition_state or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _get_market_regime() -> Dict:
    """
    Return regime data sourced from the real trading engine (market_regime.get_regime()).
    Dashboard is now always consistent with run_trades.py decisions.
    Cache TTL = 60s for fresher dashboard consistency with live execution.
    """
    import time as _time
    now = _time.monotonic()
    if _regime_cache["data"] and now - _regime_cache["ts"] < _REGIME_CACHE_TTL_SECONDS:
        return _regime_cache["data"]

    try:
        comp = get_components()
        from market_regime import get_regime as _get_regime
        r = _get_regime(comp.binance_client, settings=comp.settings)

        # Fetch BTC current price for dashboard context
        _btc_price = 0.0
        _btc_rsi = 0.0
        try:
            _btc_price = float(_get_request_price(price_cache, "BTCUSDT"))
        except Exception:
            pass
        result = {
            "regime":           r.regime,
            "color":            r.color,
            "btc_trend":        r.ema9_vs_ema21,
            "day_change_pct":   r.btc_day_pct,
            "vol_ratio":        r.vol_ratio,
            "bb_bandwidth":     r.bb_bandwidth,
            "btc_7d_change_pct": r.btc_7d_pct,
            "btc_30d_change_pct": r.btc_30d_pct,
            "btc_price":        _btc_price,
            "btc_24h_change_pct": r.btc_day_pct,
            "trading_mode":     r.mode,
            "mode_color":       _MODE_COLOR.get(r.mode, "#888"),
            "mode_reason":      r.reason + (" [pendiente confirmacion]" if not r.confirmed else ""),
            "mode_desc":        _MODE_DESC.get(r.mode, ""),
            "mode_confirmed":   r.confirmed,
            "mode_confidence":  getattr(r, 'confidence', 0.5),  # P3.1
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


def _get_active_profile_summary(mode: str) -> Dict[str, Any]:
    _mode = str(mode or "SPOT_BULL").strip().upper()
    _profile = "A"
    _updated_at = None
    try:
        _rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s",
            (f"regime_profile_active_{_mode}",),
        )
        if _rows:
            _val = _rows[0].get("value")
            if isinstance(_val, str):
                try:
                    _val = json.loads(_val)
                except Exception:
                    _val = {"raw": _val}
            if isinstance(_val, dict):
                _profile = str(_val.get("profile") or _profile).strip().upper() or "A"
            _ts = _rows[0].get("updated_at")
            _updated_at = _ts.isoformat() if _ts else None
    except Exception:
        pass

    return {
        "mode": _mode,
        "label": _profile,
        "alias": _PROFILE_ALIAS.get(_profile, _profile or "N/A"),
        "legend": _PROFILE_ALIAS,
        "legend_text": "A=Agresivo | B=Balance | C=Defensa",
        "updated_at": _updated_at,
    }


def _get_dashboard_context_reference() -> Dict[str, Any]:
    """Return the live runtime context used to classify transaction lineage."""
    _summary_mode = str(getattr(orchestrator, 'current_regime_mode', '') or 'SPOT_BULL').strip().upper() or 'SPOT_BULL'
    _effective = {}
    try:
        _effective = _get_effective_runtime_selection(_summary_mode) or {}
    except Exception:
        _effective = {}

    _mode = str(_effective.get('mode') or _summary_mode).strip().upper() or 'SPOT_BULL'
    _profile = str(_effective.get('profile_label') or _effective.get('profile') or 'A').strip().upper() or 'A'
    _snapshot_id = None
    try:
        if db_client:
            _rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key='config_snapshot_latest' LIMIT 1"
            )
            if _rows:
                _raw = _decode_runtime_state_value(_rows[0].get('value'))
                if isinstance(_raw, dict):
                    _snapshot_id = str(_raw.get('snapshot_id') or _raw.get('id') or '').strip() or None
                elif _raw is not None:
                    _snapshot_id = str(_raw).strip() or None
    except Exception:
        _snapshot_id = None

    return {
        "mode": _mode,
        "profile": _profile,
        "snapshot_id": _snapshot_id,
        "profile_alias": _PROFILE_ALIAS.get(_profile, _profile or "N/A"),
    }


def _normalize_transaction_context(meta: Any, current_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify a trade as current/legacy/partial/unknown for dashboard visibility."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    current_ctx = current_ctx or _get_dashboard_context_reference()

    def _pick(keys, upper: bool = True):
        for key in keys:
            value = meta.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
                return value.upper() if upper else value
            value = str(value).strip()
            if not value:
                continue
            return value.upper() if upper else value
        return None

    entry_mode = _pick(('regime_at_entry', 'regime_mode', 'current_regime_mode', 'entry_regime_mode'))
    entry_profile = _pick(('profile_at_entry', 'profile_label', 'current_profile_label', 'entry_profile_label'))
    entry_snapshot_id = _pick(('config_snapshot_id', 'snapshot_id', 'applied_snapshot_id', 'runtime_snapshot_id', 'effective_runtime_snapshot_id'), upper=False)
    current_mode = str(current_ctx.get('mode') or '').strip().upper() or None
    current_profile = str(current_ctx.get('profile') or '').strip().upper() or None
    current_snapshot_id = str(current_ctx.get('snapshot_id') or '').strip() or None

    has_entry_signal = any((entry_mode, entry_profile, entry_snapshot_id))
    has_current_signal = any((current_mode, current_profile, current_snapshot_id))
    mismatch_reasons = []

    if entry_snapshot_id and current_snapshot_id and entry_snapshot_id != current_snapshot_id:
        mismatch_reasons.append('snapshot')
    if entry_mode and current_mode and entry_mode != current_mode:
        mismatch_reasons.append('mode')
    if entry_profile and current_profile and entry_profile != current_profile:
        mismatch_reasons.append('profile')

    if mismatch_reasons:
        state = 'legacy'
        label = 'LEGACY'
    elif not has_entry_signal and not has_current_signal:
        state = 'unknown'
        label = 'SIN CONTEXTO'
    elif entry_mode and entry_profile:
        state = 'current'
        label = 'ACTUAL'
    elif entry_snapshot_id and (entry_mode or entry_profile):
        state = 'current' if not mismatch_reasons else 'legacy'
        label = 'ACTUAL' if state == 'current' else 'LEGACY'
    elif has_entry_signal or has_current_signal:
        state = 'partial'
        label = 'CONTEXTO PARCIAL'
    else:
        state = 'unknown'
        label = 'SIN CONTEXTO'

    if mismatch_reasons:
        reason = ','.join(mismatch_reasons)
    elif state == 'current':
        reason = 'match'
    elif state == 'partial':
        reason = 'missing_entry_context'
    else:
        reason = 'no_context'

    return {
        'config_context_state': state,
        'config_context_label': label,
        'config_context_reason': reason,
        'entry_regime_mode': entry_mode,
        'entry_profile_label': entry_profile,
        'entry_snapshot_id': entry_snapshot_id,
        'current_regime_mode': current_mode,
        'current_regime_profile_label': current_profile,
        'current_snapshot_id': current_snapshot_id,
        'current_profile_alias': current_ctx.get('profile_alias'),
        'has_context_match': state == 'current',
        'is_legacy_context': state == 'legacy',
        'is_partial_context': state == 'partial',
    }


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
    comp = get_components()
    _snapshot_started_at = datetime.now(timezone.utc)
    result = {}
    price_cache: Dict[str, float] = {}
    _runtime_ctx = _get_dashboard_context_reference()
    _runtime_ctx = _get_dashboard_context_reference()

    # Exchange rate (computed once, reused across blocks)
    _usd_mxn = 17.5  # Fixed display rate MXN/USD (USDCUSDT ≈1.0 on Binance, not a real FX source)
    try:
        _usdmxn_candidate = _get_request_price(price_cache, "USDTMXN") or 0
        if _usdmxn_candidate > 10:  # sanity check: real MXN rate is always > 10
            _usd_mxn = _usdmxn_candidate
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

    # Pause status for new entries (operator visibility + TTL)
    try:
        result["pause_new_entries"] = _get_pause_new_entries_status()
    except Exception as e:
        result["pause_new_entries"] = {"active": False, "error": str(e)}

    # Unified operational status (single source for UI chips and control panel)
    try:
        result["unified_status"] = _build_unified_system_status(
            result.get("system_status", {}),
            result.get("pause_new_entries", {}),
            _list_active_operator_alerts(),
        )
    except Exception as e:
        result["unified_status"] = {"effective_state": "UNKNOWN", "ui_label": "Unknown", "error": str(e)}

    # Binance truth parity status (runtime vs exchange)
    try:
        result["binance_truth"] = get_truth_status(comp.db_client)
    except Exception as e:
        result["binance_truth"] = {"ok": False, "severity": "error", "error": str(e)}

    # Binance SPOT truth parity status
    try:
        result["binance_truth_spot"] = get_truth_status_spot(comp.db_client)
    except Exception as e:
        result["binance_truth_spot"] = {"ok": False, "severity": "error", "error": str(e)}

    # Basket reset recommendation (runtime decision persisted by trade cycle)
    try:
        _br_rows = comp.db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key=%s LIMIT 1",
            ('basket_reset_decision',),
        ) if comp.db_client else []
        if _br_rows:
            _br_raw = _br_rows[0].get('value')
            _br_val = _decode_runtime_state_value(_br_raw)
            if isinstance(_br_val, dict):
                _br_ts = _br_rows[0].get('updated_at')
                _br_val['updated_at'] = _br_ts.isoformat() if _br_ts and hasattr(_br_ts, 'isoformat') else None
                result["basket_reset_decision"] = _br_val
            else:
                result["basket_reset_decision"] = {"raw": _br_val}
        else:
            result["basket_reset_decision"] = {"trigger": False, "reason": "no_decision"}
    except Exception as e:
        result["basket_reset_decision"] = {"trigger": False, "error": str(e)}

    # Result-based SMART health (SPOT/FUTURES split + adaptive cadence)
    try:
        _cached_hr = _HEALTH_RESULTS_CACHE.get("snapshot")
        if not isinstance(_cached_hr, dict):
            _cached_hr = _load_persisted_health_results_snapshot()
        result["health_results"] = _cached_hr or {"ok": False, "overall": "unknown", "source": "empty"}
    except Exception as e:
        result["health_results"] = {"ok": False, "overall": "unknown", "error": str(e)}

    # Audit consistency — mirrors signal decisions against trades_audit
    try:
        result["audit_consistency"] = check_signal_audit_consistency(comp.db_client, limit=100)
    except Exception as e:
        result["audit_consistency"] = {"available": False, "error": str(e)}

    # Performance changepoint — compare current behavior to the Smart Backtest baseline marker
    try:
        result["performance_changepoint"] = _get_performance_changepoint_metrics()
    except Exception as e:
        result["performance_changepoint"] = {"available": False, "error": str(e)}

    # Mode/profile harmony — expose the effective Kelly/KPI/SSO overlay currently active
    try:
        result["mode_profile_harmony"] = _get_mode_profile_harmony_snapshot()
    except Exception as e:
        result["mode_profile_harmony"] = {"ok": False, "error": str(e)}

    # Rollout readiness — consolidated operational verdict for current tuning state
    try:
        result["rollout_readiness"] = _get_rollout_readiness_snapshot()
    except Exception as e:
        result["rollout_readiness"] = {"ok": False, "status": "error", "error": str(e)}

    # Parity validation — compare active rollout baseline vs live results
    try:
        result["parity_validation"] = _get_parity_validation_snapshot()
    except Exception as e:
        result["parity_validation"] = {"ok": False, "status": "error", "error": str(e)}

    # Promotion criteria — decide whether the current tuning has enough evidence to promote
    try:
        result["promotion_criteria"] = _get_promotion_criteria_snapshot()
    except Exception as e:
        result["promotion_criteria"] = {"ok": False, "status": "error", "error": str(e)}

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
        import asyncio as _asyncio
        balances = await _asyncio.to_thread(binance_client.get_account_balance)
        total_usdt = 0.0
        # Reuse dashboard-local price cache when the same quote appears multiple times.
        balances_usdt = {}
        for asset, amount in balances.items():
            if asset == "USDT":
                balances_usdt[asset] = amount
                total_usdt += amount
            else:
                try:
                    price = _get_request_price(price_cache, f"{asset}USDT")
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
            _futures_usdt = await _asyncio.to_thread(_fc_bal.get_total_wallet_usdt)
        except Exception:
            pass
        _grand_total = total_usdt + _futures_usdt
        result["balance"] = {
            "total_usdt": _grand_total,
            "total_mxn": _grand_total * _usd_mxn,
            "usd_to_mxn_rate": _usd_mxn,
            "usdt_available": round(_spot_usdt, 4),
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

        # Build daily_breakdown for the last 30 days (for chart)
        def _daily_breakdown(positions, since_days=30):
            from collections import defaultdict
            cutoff = to_mexico_time(datetime.utcnow()) - timedelta(days=since_days)
            daily = defaultdict(float)
            for p in positions:
                if not p.exit_time:
                    continue
                try:
                    if isinstance(p.exit_time, str):
                        dt = to_mexico_time(datetime.fromisoformat(p.exit_time.replace('Z', '+00:00')))
                    else:
                        t = p.exit_time
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        dt = to_mexico_time(t)
                    if dt >= cutoff:
                        daily[dt.strftime('%Y-%m-%d')] += float(p.pnl)
                except Exception:
                    pass
            # Fill all days in range (even zero-trade days)
            result_days = []
            for i in range(since_days, 0, -1):
                d = (to_mexico_time(datetime.utcnow()) - timedelta(days=i)).strftime('%Y-%m-%d')
                result_days.append({"date": d, "pnl": round(daily.get(d, 0.0), 4)})
            return result_days

        # Also include futures closed PnL in daily breakdown
        _fut_closed_pnl_by_day = {}
        try:
            from run_trades import _get_futures_client as _gfc
            _fc2 = _gfc(settings)
            _fut_closed = _fc2.get_closed_positions_db(days=30) if hasattr(_fc2, 'get_closed_positions_db') else []
            from collections import defaultdict as _dd2
            _fday = _dd2(float)
            for fp in _fut_closed:
                _fd = fp.get('closed_at') or fp.get('opened_at', '')
                if _fd:
                    try:
                        _fdt = datetime.fromisoformat(str(_fd).replace('Z', '+00:00'))
                        _fday[_fdt.strftime('%Y-%m-%d')] += float(fp.get('pnl', 0))
                    except Exception:
                        pass
            _fut_closed_pnl_by_day = dict(_fday)
        except Exception:
            pass

        _breakdown = _daily_breakdown(closed, since_days=30)
        # Merge futures PnL into breakdown
        for day_item in _breakdown:
            day_item['pnl'] = round(day_item['pnl'] + _fut_closed_pnl_by_day.get(day_item['date'], 0.0), 4)

        result["pnl_history"] = {
            "day": _pnl_for_period(closed, day_start),
            "week": _pnl_for_period(closed, week_start),
            "year": _pnl_for_period(closed, year_start),
            "daily_breakdown": _breakdown,
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
                current_price_sym = float(_get_request_price(price_cache, symbol))
            except Exception:
                current_price_sym = None

            _cat_map = getattr(settings, "token_categories", {})
            _sym_cat = _cat_map.get(symbol, {})

            # ESC-AGG-B: Build ONE parent row per symbol (WAP + total qty + total PnL)
            # Use oldest position for metadata (SL/TP/prediction from first entry)
            _sp_first = pos_list[0]
            _sp_cp = current_price_sym if current_price_sym is not None else float(_sp_first.entry_price)
            _sp_side = _sp_first.side.upper()
            _sp_is_short = _sp_side in ("SELL", "SHORT")

            # Aggregate across all entries
            _sp_entries_raw = []
            _sp_total_cost = 0.0
            _sp_total_qty  = 0.0
            _sp_total_pnl  = 0.0
            _sp_max_age_h  = 0.0
            _sp_first_entry_time = ""
            _sp_age_parse_errors = 0
            _sp_pred = None
            _sp_meta_last = {}
            now_dt = to_mexico_time(datetime.utcnow())
            now_utc = datetime.now(timezone.utc)
            for _pos_i, position in enumerate(pos_list):
                try:
                    _ep = float(position.entry_price)
                    _qty = float(position.quantity)
                except Exception:
                    continue
                _cost = _ep * _qty
                _sp_total_cost += _cost
                _sp_total_qty  += _qty
                if _sp_is_short:
                    _sp_total_pnl += (_ep - _sp_cp) * _qty
                else:
                    _sp_total_pnl += (_sp_cp - _ep) * _qty
                # Age
                try:
                    _et_str = to_mexico_time_str(position.entry_time) if position.entry_time else ""
                    _et_dt = position.entry_time
                    if isinstance(_et_dt, str):
                        _et_dt = datetime.fromisoformat(_et_dt.replace('Z', '+00:00'))
                    if _et_dt and _et_dt.tzinfo is None:
                        _et_dt = _et_dt.replace(tzinfo=timezone.utc)
                    _age_h = (now_utc - _et_dt).total_seconds() / 3600 if _et_dt else 0.0
                except Exception:
                    _et_str = ""
                    _age_h  = 0.0
                    _sp_age_parse_errors += 1
                if _age_h > _sp_max_age_h:
                    _sp_max_age_h = _age_h
                    _sp_first_entry_time = _et_str
                # Prediction: use first available
                if _sp_pred is None and position.pnl_prediction:
                    _sp_pred = position.pnl_prediction
                # Metadata from last (most recent) entry for SL/TP lifecycle
                _sp_meta_last = getattr(position, 'metadata', {}) or {}
                # Build sub-entry for entries[] list
                _sp_meta_i = getattr(position, 'metadata', {}) or {}
                _sp_entries_raw.append({
                    "entry_price": _ep,
                    "qty": _qty,
                    "age_h": round(_age_h, 2),
                    "age_parse_error": _age_h <= 0 and bool(_et_str == ""),
                    "escalonamiento_index": _sp_meta_i.get('escalonamiento_index', _pos_i + 1),
                    "es_escalonamiento": _sp_meta_i.get('es_escalonamiento', _pos_i > 0),
                    "trade_type": "spot",
                    "opened_at": _et_str,
                    "pnl_usdt": 0.0,
                    "pnl_pct": 0.0,
                })

            if _sp_total_qty <= 0:
                continue
            _sp_wap = _sp_total_cost / _sp_total_qty
            # C0: dashboard shows net PnL after Binance fees (entry+exit on total notional)
            from binance_commission import total_commission as _sp_total_commission
            _sp_comm_total = _sp_total_commission(_sp_total_qty, _sp_wap, _sp_cp, 'spot', settings)
            _sp_total_pnl = round(_sp_total_pnl - _sp_comm_total, 4)
            _sp_total_qty = round(_sp_total_qty, 8)
            _sp_wap       = round(_sp_wap, 8)
            _sp_total_cost = round(_sp_total_cost, 4)
            _sp_esc_n = len(pos_list)
            # Realized PnL from partial closes for this symbol (e.g. SSO scale-outs)
            _sp_realized_pnl = 0.0
            try:
                if db_client:
                    _sp_real_rows = db_client.execute_query(
                        "SELECT COALESCE(SUM(pnl), 0) AS realized FROM trades_closed "
                        "WHERE symbol=%s AND metadata->>'partial_close' = 'true'",
                        (symbol,),
                    )
                    if _sp_real_rows:
                        _sp_realized_pnl = float(_sp_real_rows[0].get('realized', 0) or 0)
            except Exception:
                _sp_realized_pnl = 0.0

            # Backfill prediction if still None
            if _sp_pred is None:
                try:
                    import pnl_service as _pnl_svc
                    _sp_pred, _ = _pnl_svc.predict_for_symbol(
                        symbol=symbol, client=binance_client, settings=settings,
                        position_size_usdt=_sp_total_cost or 50.0, trade_type='spot'
                    )
                    if _sp_pred and _sp_first.db_id:
                        import json as _json
                        db_client.execute_update(
                            "UPDATE trades_open SET pnl_prediction = %s::jsonb WHERE id = %s",
                            (_json.dumps(_sp_pred), _sp_first.db_id)
                        )
                except Exception:
                    _sp_pred = None

            # SL/TP from first (oldest) entry — that's the one the system manages
            _sl_f = float(_sp_first.stop_loss) if _sp_first.stop_loss else None
            _tp_f = float(_sp_first.take_profit) if _sp_first.take_profit else None
            _sl_dist = round((_sl_f - _sp_wap) / _sp_wap * 100, 2) if _sl_f and _sp_wap > 0 else None
            _tp_dist = round((_tp_f - _sp_wap) / _sp_wap * 100, 2) if _tp_f and _sp_wap > 0 else None
            _trail_peak = float(_sp_meta_last.get('trailing_peak', 0)) if _sp_meta_last.get('trailing_peak') else None

            open_list.append({
                "symbol": symbol,
                "position_id": _sp_first.position_id,
                "entry_number": 1,
                "side": _sp_first.side,
                "category": _sym_cat.get("market_behavior", "MIDCAP_ALT"),
                "origin": _sym_cat.get("origin", ""),
                "quantity": _sp_total_qty,
                "entry_price": _sp_wap,
                "current_price": _sp_cp,
                "invested_usdt": _sp_total_cost,
                "invested_mxn": round(_sp_total_cost * _usd_mxn, 2),
                "current_value_mxn": round(_sp_cp * _sp_total_qty * _usd_mxn, 2),
                "unrealized_pnl": _sp_total_pnl,
                "unrealized_pnl_mxn": round(_sp_total_pnl * _usd_mxn, 2),
                "realized_pnl": round(_sp_realized_pnl, 4),
                "realized_pnl_mxn": round(_sp_realized_pnl * _usd_mxn, 2),
                "total_pnl": round(_sp_total_pnl + _sp_realized_pnl, 4),
                "total_pnl_mxn": round((_sp_total_pnl + _sp_realized_pnl) * _usd_mxn, 2),
                "entry_time": _sp_first_entry_time,
                "first_entry_time": _sp_first_entry_time,
                "hours_held": round(_sp_max_age_h, 1),
                "age_parse_errors": _sp_age_parse_errors,
                "entry_commission": float(_sp_first.entry_commission) if _sp_first.entry_commission else 0.0,
                "stop_loss": _sl_f,
                "take_profit": _tp_f,
                "pnl_prediction": _sp_pred,
                "is_overflow": symbol in _ov_positions,
                "trade_type": "spot",
                "sl_dist_pct": _sl_dist,
                "tp_dist_pct": _tp_dist,
                "trailing_peak": _trail_peak,
                "trailing_active": _trail_peak is not None and _trail_peak > 0,
                "breakeven_done": False,
                "tp_extensions": int(_sp_meta_last.get('tp_extensions_count', 0)),
                "tp_decisions": _sp_meta_last.get('tp_decisions', []),
                "scale_out_done": bool(_sp_meta_last.get('scale_out_done', False)),
                "escalamientos": _sp_esc_n,
                "entries": _enrich_entries(_sp_entries_raw, _sp_cp, _sp_first.side),
                "bounce_entry": bool(_sp_meta_last.get('bounce_entry', False)),
                "bounce_gate_blocked_count": int(_sp_meta_last.get('bounce_gate_blocked_count', 0)),
                "bounce_gate_blocked_reason": _sp_meta_last.get('bounce_gate_blocked_reason'),
                "bounce_gate_last_signal": _sp_meta_last.get('bounce_gate_last_signal'),
                "bounce_gate_last_signal_strength": _sp_meta_last.get('bounce_gate_last_signal_strength'),
                "bounce_gate_last_ev": _sp_meta_last.get('bounce_gate_last_ev'),
                "bounce_gate_last_velocity": _sp_meta_last.get('bounce_gate_last_velocity'),
                "bounce_gate_first_qualified_at": _sp_meta_last.get('bounce_gate_first_qualified_at'),
                "btc_chg_since_entry_pct": None,
                "metadata": _sp_meta_last,
                **_normalize_transaction_context(_sp_meta_last, _runtime_ctx),
            })
        # Add futures positions to the dashboard
        try:
            from run_trades import _get_futures_client
            fc = _get_futures_client(settings)
            futures_positions = fc.get_open_positions_summary()
            # Pre-load escalamiento entries from DB for badge enrichment
            _dash_esc_map: dict = {}
            try:
                import json as _jesc
                _esc_rows = db_client.execute_query(
                    "SELECT symbol, quantity, price, entry_time, metadata "
                    "FROM trades_open "
                    "WHERE metadata->>'trade_type' = 'futures' "
                    "ORDER BY symbol, entry_time ASC", ()
                ) if db_client else []
                from datetime import timezone as _tz_esc
                _now_esc2 = datetime.utcnow().replace(tzinfo=_tz_esc.utc)
                for _er in _esc_rows:
                    _em = _er.get('metadata') or {}
                    if isinstance(_em, str):
                        try: _em = _jesc.loads(_em)
                        except: _em = {}
                    _et2 = _er.get('entry_time')
                    _age2 = 0.0
                    _et2a = None
                    if _et2:
                        _et2a = _et2 if (hasattr(_et2,'tzinfo') and _et2.tzinfo) else _et2.replace(tzinfo=_tz_esc.utc) if hasattr(_et2,'replace') else None
                        if _et2a: _age2 = round((_now_esc2 - _et2a).total_seconds() / 3600, 2)
                    _esc_opened_str = to_mexico_time_str(_et2a) if _et2a else ""
                    _dash_esc_map.setdefault(_er['symbol'], []).append({
                        "entry_price": float(_er['price']),
                        "qty": float(_er['quantity']),
                        "age_h": _age2,
                        "escalonamiento_index": _em.get('escalonamiento_index', 1),
                        "es_escalonamiento": _em.get('es_escalonamiento', False),
                        "trade_type": 'futures',
                        "opened_at": _esc_opened_str,
                        "pnl_usdt": 0.0,
                        "pnl_pct": 0.0,
                    })
            except Exception as _eme:
                pass
            
            for fp in futures_positions:
                # FIX-D1: calculate real hours_held and inject pnl_prediction from summary
                _fp_opened = fp.get("opened_at", "")
                _fp_hours = 0.0
                _fp_age_parse_error = False
                try:
                    if _fp_opened:
                        from datetime import datetime as _dt2
                        _fp_dt = _dt2.fromisoformat(str(_fp_opened).replace(' ', 'T', 1).replace('Z', '+00:00'))
                        _fp_dt_utc = _fp_dt if _fp_dt.tzinfo else _fp_dt.replace(tzinfo=timezone.utc)
                        _fp_hours = round((datetime.now(timezone.utc) - _fp_dt_utc).total_seconds() / 3600, 1)
                except Exception:
                    _fp_hours = 0.0
                    _fp_age_parse_error = True

                _fp_symbol_entries = _dash_esc_map.get(fp["symbol"], [])
                if _fp_hours <= 0 and _fp_symbol_entries:
                    _fp_hours = round(max((float(e.get('age_h', 0.0) or 0.0) for e in _fp_symbol_entries), default=0.0), 1)
                    _fp_age_parse_error = _fp_age_parse_error and (_fp_hours <= 0)
                # FIX-D1: use entry_time in Mexico timezone for display
                _fp_entry_time_str = to_mexico_time_str(_fp_opened) if _fp_opened else ""
                if not _fp_entry_time_str and _fp_symbol_entries:
                    _fp_oldest = max(_fp_symbol_entries, key=lambda _x: float(_x.get('age_h', 0.0) or 0.0))
                    _fp_entry_time_str = _fp_oldest.get('opened_at', '')

                # FIX-PR2: backfill pnl_prediction for futures huérfanos (no prediction in DB)
                _fp_pred = fp.get("pnl_prediction")
                if _fp_pred is None:
                    try:
                        import pnl_service as _pnl_svc
                        _fp_invested = round(fp["entry_price"] * fp["qty"], 2)
                        _fp_pred, _ = _pnl_svc.predict_for_symbol(
                            symbol=fp["symbol"], client=binance_client, settings=settings,
                            position_size_usdt=_fp_invested or 100.0,
                            trade_type='futures',
                            trade_side=('SHORT' if str(fp.get('side', '')).upper() == 'SHORT' else 'LONG'),
                        )
                        if _fp_pred and db_client:
                            import json as _json
                            _fp_side_db = 'SELL' if fp["side"] == 'SHORT' else 'BUY'
                            db_client.execute_update(
                                """UPDATE trades_open SET pnl_prediction = %s::jsonb
                                   WHERE symbol = %s AND side = %s
                                   AND metadata->>'trade_type' = 'futures'
                                   AND pnl_prediction IS NULL""",
                                (_json.dumps(_fp_pred), fp["symbol"], _fp_side_db)
                            )
                    except Exception as _fpe:
                        print(f"[FIX-PR2] backfill failed for {fp['symbol']}: {_fpe}")
                        _fp_pred = None

                # VIS-2b: SL/TP lifecycle fields for futures
                _fp_sl = fp["sl_price"]
                _fp_tp = fp["tp_price"]

                # ESC-AGG-A: Aggregate entry_price (WAP), qty, PnL from DB entries when >1 escalamiento
                _fp_cur = fp["current_price"]
                _fp_db_entries = [e for e in _dash_esc_map.get(fp["symbol"], [])
                                  if e.get('trade_type', 'spot') == 'futures']
                _is_short = fp["side"] == "SHORT"
                if len(_fp_db_entries) > 1:
                    _fp_total_qty = sum(float(e['qty']) for e in _fp_db_entries)
                    _fp_total_cost = sum(float(e['entry_price']) * float(e['qty']) for e in _fp_db_entries)
                    _fp_wap = _fp_total_cost / _fp_total_qty if _fp_total_qty > 0 else fp["entry_price"]
                    _fp_total_upnl = sum(
                        (float(e['entry_price']) - _fp_cur) * float(e['qty']) if _is_short
                        else (_fp_cur - float(e['entry_price'])) * float(e['qty'])
                        for e in _fp_db_entries
                    )
                    _fp_entry  = round(_fp_wap, 8)
                    _fp_qty    = round(_fp_total_qty, 8)
                    _fp_upnl   = round(_fp_total_upnl, 4)
                    _fp_invested = round(_fp_total_cost, 4)
                else:
                    _fp_entry  = fp["entry_price"]
                    _fp_qty    = fp["qty"]
                    _fp_upnl   = fp["unrealized_pnl"]
                    _fp_invested = round(_fp_entry * _fp_qty, 4)

                _fp_sl_dist = round((_fp_sl - _fp_entry) / _fp_entry * 100, 2) if _fp_entry > 0 else None
                _fp_tp_dist = round((_fp_tp - _fp_entry) / _fp_entry * 100, 2) if _fp_entry > 0 else None

                open_list.append({
                    "symbol": fp["symbol"],
                    "position_id": f"futures_{fp['symbol']}_{fp['side']}",
                    "entry_number": 1,
                    "side": fp["side"],
                    "category": "FUTURES",
                    "origin": "",
                    "quantity": _fp_qty,
                    "entry_price": _fp_entry,
                    "current_price": _fp_cur,
                    "invested_usdt": _fp_invested,
                    "margin_usdt": round(_fp_invested / 2, 4),
                    "invested_mxn": round(_fp_invested * _usd_mxn, 2),
                    "current_value_usdt": round(_fp_cur * _fp_qty, 4),
                    "current_value_mxn": round(_fp_cur * _fp_qty * _usd_mxn, 2),
                    "unrealized_pnl": _fp_upnl,
                    "unrealized_pnl_mxn": round(_fp_upnl * _usd_mxn, 2),
                    "entry_time": _fp_entry_time_str,
                    "first_entry_time": _fp_entry_time_str,
                    "hours_held": _fp_hours,
                    "age_parse_error": _fp_age_parse_error,
                    "entry_commission": 0.0,
                    "stop_loss": _fp_sl,
                    "take_profit": _fp_tp,
                    "pnl_prediction": _fp_pred,
                    "is_overflow": False,
                    "trade_type": "futures",
                    "sl_dist_pct": _fp_sl_dist,
                    "tp_dist_pct": _fp_tp_dist,
                    "trailing_peak": fp.get("trail_best_price"),
                    "trailing_active": fp.get("trailing_active", False),
                    "breakeven_done": fp.get("breakeven_done", False),
                    "tp_extensions": fp.get("tp_extensions", 0),
                    "tp_decisions": [],
                    "scale_out_done": fp.get("scale_out_done", False),
                    "scale_out_pnl": fp.get("scale_out_pnl", 0.0),
                    "peak_pnl_pct": fp.get("peak_pnl_pct", 0.0),
                    "dynamic_trail_dist_pct": fp.get("dynamic_trail_dist_pct"),
                    "sso_levels_done": fp.get("sso_levels_done", []),
                    "sso_last_trigger": fp.get("sso_last_trigger", ""),
                    "bounce_entry": fp.get("bounce_entry", False),
                    "bounce_gate_blocked_count": fp.get("bounce_gate_blocked_count", 0),
                    "bounce_gate_blocked_reason": fp.get("bounce_gate_blocked_reason"),
                    "bounce_gate_last_signal": fp.get("bounce_gate_last_signal"),
                    "bounce_gate_last_signal_strength": fp.get("bounce_gate_last_signal_strength"),
                    "bounce_gate_last_ev": fp.get("bounce_gate_last_ev"),
                    "bounce_gate_last_velocity": fp.get("bounce_gate_last_velocity"),
                    "bounce_gate_first_qualified_at": fp.get("bounce_gate_first_qualified_at"),
                    "btc_chg_since_entry_pct": fp.get("btc_chg_since_entry_pct"),
                    "escalamientos": max(len(_dash_esc_map.get(fp["symbol"], [])), max((e.get('escalonamiento_index', 1) for e in _dash_esc_map.get(fp["symbol"], [])), default=1)),
                    "entries": _enrich_entries(_dash_esc_map.get(fp["symbol"], []), fp["current_price"], fp["side"]),
                    **_normalize_transaction_context(fp.get('_smart_meta') or fp.get('metadata') or {}, _runtime_ctx),
                })
        except Exception as e:
            # If futures client fails, continue with spot positions only
            print(f"Warning: Could not fetch futures positions for dashboard: {e}")


        _spot_count = sum(1 for p in open_list if p.get('trade_type') != 'futures')
        _fut_count  = sum(1 for p in open_list if p.get('trade_type') == 'futures')
        _open_unrealized_total = round(sum(float(p.get('unrealized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_realized_total = round(sum(float(p.get('realized_pnl', 0.0) or 0.0) for p in open_list), 4)
        _open_total_pnl = round(_open_unrealized_total + _open_realized_total, 4)
        _open_context_counts = {
            "legacy": sum(1 for p in open_list if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in open_list if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in open_list if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in open_list if p.get('config_context_state') == 'unknown'),
        }
        result["open_positions"] = {
            "positions": open_list,
            "count": len(open_list),
            "spot_count": _spot_count,
            "futures_count": _fut_count,
            "total_unrealized_pnl": _open_unrealized_total,
            "total_realized_pnl": _open_realized_total,
            "total_pnl": _open_total_pnl,
            "legacy_count": _open_context_counts["legacy"],
            "partial_context_count": _open_context_counts["partial"],
            "current_context_count": _open_context_counts["current"],
            "unknown_context_count": _open_context_counts["unknown"],
        }
    except Exception as e:
        result["open_positions"] = {"positions": [], "count": 0, "error": str(e)}

    # Closed Positions (with pnl_prediction & prediction_accuracy) - Last 7 days
    try:
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        closed_positions = position_manager.get_closed_positions_since(since=seven_days_ago, limit=100)
        _cat_map_cl = getattr(settings, "token_categories", {})
        # Build flat list first
        _cl_flat = []
        for pos in closed_positions:
            _cl_cat = _cat_map_cl.get(pos.symbol, {})
            _cl_meta = pos.metadata if isinstance(pos.metadata, dict) else {}
            _cl_flat.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "category": _cl_cat.get("market_behavior", "MIDCAP_ALT"),
                "quantity": float(pos.quantity) if pos.quantity else 0.0,
                "entry_price": float(pos.entry_price) if pos.entry_price else 0.0,
                "exit_price": float(pos.exit_price) if pos.exit_price else 0.0,
                "invested_usdt": round(float(pos.entry_price or 0) * float(pos.quantity or 0), 4),
                "pnl": float(pos.pnl) if pos.pnl else 0.0,
                "pnl_percentage": float(pos.pnl_percentage) if pos.pnl_percentage else 0.0,
                "entry_time": to_mexico_time_str(pos.entry_time) if pos.entry_time else "",
                "exit_time": to_mexico_time_str(pos.exit_time) if pos.exit_time else "",
                "close_reason": pos.close_reason or "",
                "entry_commission": float(pos.entry_commission) if pos.entry_commission else 0.0,
                "exit_commission": float(pos.exit_commission) if pos.exit_commission else 0.0,
                "pnl_prediction": pos.pnl_prediction,
                "prediction_accuracy": float(pos.prediction_accuracy) if pos.prediction_accuracy else None,
                "trade_type": _cl_meta.get('trade_type', 'spot'),
                "trailing_peak": _cl_meta.get('trailing_peak'),
                "tp_decisions": _cl_meta.get('tp_decisions', []),
                "scale_out_done": bool(_cl_meta.get('scale_out_done', False)),
                "scale_out_pnl": round(float(_cl_meta.get('scale_out_pnl', 0) or 0), 4),
                "peak_pnl_pct": round(float(_cl_meta.get('peak_pnl_pct', 0) or 0) * 100, 2),
                "tp_extensions": int(_cl_meta.get('tp_extensions_count', 0)),
                "metadata": _cl_meta,
                "bounce_entry": bool(_cl_meta.get('bounce_entry', False)),
                **_normalize_transaction_context(_cl_meta, _runtime_ctx),
                "_close_group_id": _cl_meta.get('close_group_id'),
            })
        # ESC-AGG-C: Group rows with same close_group_id into one parent row
        from collections import OrderedDict as _OD
        _cl_groups: dict = _OD()
        _cl_no_group = []
        for _row in _cl_flat:
            _cgid = _row.get('_close_group_id')
            if _cgid:
                _cl_groups.setdefault(_cgid, []).append(_row)
            else:
                _cl_no_group.append(_row)
        closed_list = list(_cl_no_group)  # ungrouped first
        for _cgid, _grp in _cl_groups.items():
            if len(_grp) == 1:
                closed_list.append(_grp[0])
                continue
            # Aggregate: WAP entry, sum qty, sum pnl, latest exit_time
            _g_total_qty = sum(r['quantity'] for r in _grp)
            _g_total_cost = sum(r['entry_price'] * r['quantity'] for r in _grp)
            _g_wap = _g_total_cost / _g_total_qty if _g_total_qty > 0 else _grp[0]['entry_price']
            _g_total_pnl = sum(r['pnl'] for r in _grp)
            _g_exit_price = sum(r['exit_price'] * r['quantity'] for r in _grp) / _g_total_qty if _g_total_qty > 0 else _grp[0]['exit_price']
            _g_pnl_pct = (_g_total_pnl / _g_total_cost * 100) if _g_total_cost > 0 else 0.0
            _g_exit_time = max((r['exit_time'] for r in _grp if r['exit_time']), default=_grp[0]['exit_time'])
            _g_entry_time = min((r['entry_time'] for r in _grp if r['entry_time']), default=_grp[0]['entry_time'])
            _g_meta = dict(_grp[-1]['metadata'])  # use most recent metadata
            _g_meta['total_escalonamientos'] = len(_grp)
            _g_meta['close_group_id'] = _cgid
            closed_list.append({
                **_grp[0],
                "quantity": round(_g_total_qty, 8),
                "entry_price": round(_g_wap, 8),
                "exit_price": round(_g_exit_price, 8),
                "invested_usdt": round(_g_total_cost, 4),
                "pnl": round(_g_total_pnl, 4),
                "pnl_percentage": round(_g_pnl_pct, 4),
                "entry_time": _g_entry_time,
                "exit_time": _g_exit_time,
                "metadata": _g_meta,
                **_normalize_transaction_context(_g_meta, _runtime_ctx),
            })
        # Strip internal tracking field before exposing
        for _cl in closed_list:
            _cl.pop('_close_group_id', None)
        _cl_spot = sum(1 for p in closed_list if p.get('trade_type') != 'futures')
        _cl_fut  = sum(1 for p in closed_list if p.get('trade_type') == 'futures')
        _closed_context_counts = {
            "legacy": sum(1 for p in closed_list if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in closed_list if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in closed_list if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in closed_list if p.get('config_context_state') == 'unknown'),
        }
        result["closed_positions"] = {
            "positions": closed_list,
            "count": len(closed_list),
            "spot_count": _cl_spot,
            "futures_count": _cl_fut,
            "period_days": 7,
            "legacy_count": _closed_context_counts["legacy"],
            "partial_context_count": _closed_context_counts["partial"],
            "current_context_count": _closed_context_counts["current"],
            "unknown_context_count": _closed_context_counts["unknown"],
        }
    except Exception as e:
        result["closed_positions"] = {"positions": [], "count": 0, "error": str(e), "period_days": 7}

    # PnL by close reason (today + 7d)
    try:
        _now_mx = to_mexico_time(datetime.utcnow())
        _today_start = _now_mx.replace(hour=0, minute=0, second=0, microsecond=0)
        _week_start = _today_start - timedelta(days=7)
        _closed_all = result.get("closed_positions", {}).get("positions", [])
        from collections import defaultdict as _dd_cr
        _cr_today = _dd_cr(lambda: {"count": 0, "pnl": 0.0})
        _cr_week  = _dd_cr(lambda: {"count": 0, "pnl": 0.0})
        _today_spot_pnl = 0.0; _today_fut_pnl = 0.0; _today_spot_c = 0; _today_fut_c = 0
        _week_spot_pnl = 0.0; _week_fut_pnl = 0.0; _week_spot_c = 0; _week_fut_c = 0
        for cp in _closed_all:
            _cp_exit = cp.get("exit_time", "")
            _cp_pnl = float(cp.get("pnl", 0))
            _cp_reason = cp.get("close_reason", "unknown") or "unknown"
            _cp_type = cp.get("trade_type", "spot")
            # Also include scale_out_pnl for positions that had a scale-out (adds to the total realized)
            _cp_so_pnl = float(cp.get("scale_out_pnl", 0))
            _cp_total_pnl = _cp_pnl + _cp_so_pnl
            try:
                _cp_dt = datetime.fromisoformat(str(_cp_exit).replace('Z', '+00:00'))
                if _cp_dt.tzinfo is None:
                    from datetime import timezone as _tz
                    _cp_dt = _cp_dt.replace(tzinfo=_tz.utc)
                _cp_mx = to_mexico_time(_cp_dt)
            except Exception:
                continue
            if _cp_mx >= _week_start:
                _cr_week[_cp_reason]["count"] += 1
                _cr_week[_cp_reason]["pnl"] += _cp_total_pnl
                if _cp_type == "futures":
                    _week_fut_pnl += _cp_total_pnl; _week_fut_c += 1
                else:
                    _week_spot_pnl += _cp_total_pnl; _week_spot_c += 1
            if _cp_mx >= _today_start:
                _cr_today[_cp_reason]["count"] += 1
                _cr_today[_cp_reason]["pnl"] += _cp_total_pnl
                if _cp_type == "futures":
                    _today_fut_pnl += _cp_total_pnl; _today_fut_c += 1
                else:
                    _today_spot_pnl += _cp_total_pnl; _today_spot_c += 1
        def _cr_to_list(d):
            return sorted([{"reason": k, "count": v["count"], "pnl": round(v["pnl"], 4)}
                           for k, v in d.items()], key=lambda x: x["pnl"], reverse=True)
        result["pnl_by_close_reason"] = {
            "today": {
                "by_reason": _cr_to_list(_cr_today),
                "total_pnl": round(sum(v["pnl"] for v in _cr_today.values()), 4),
                "total_trades": sum(v["count"] for v in _cr_today.values()),
                "spot_pnl": round(_today_spot_pnl, 4), "spot_count": _today_spot_c,
                "futures_pnl": round(_today_fut_pnl, 4), "futures_count": _today_fut_c,
            },
            "week": {
                "by_reason": _cr_to_list(_cr_week),
                "total_pnl": round(sum(v["pnl"] for v in _cr_week.values()), 4),
                "total_trades": sum(v["count"] for v in _cr_week.values()),
                "spot_pnl": round(_week_spot_pnl, 4), "spot_count": _week_spot_c,
                "futures_pnl": round(_week_fut_pnl, 4), "futures_count": _week_fut_c,
            },
            "daily_target": 40.0,
        }
    except Exception as e:
        result["pnl_by_close_reason"] = {"error": str(e)}

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

    # Effective runtime mode/profile (source of truth from live engine when available)
    try:
        _detected_mode = str(result.get("market_regime", {}).get("trading_mode") or "SPOT_BULL").strip().upper() or "SPOT_BULL"
        _effective = _get_effective_runtime_selection(_detected_mode)
        _effective_mode = str(_effective.get("mode") or _detected_mode).strip().upper() or _detected_mode
        _mr = result.get("market_regime", {})
        if _effective_mode != _detected_mode:
            _mr["detected_mode"] = _detected_mode
            _prev_reason = str(_mr.get("mode_reason") or "").strip()
            _mr["mode_reason"] = (
                f"{_prev_reason} | effective_mode={_effective_mode} source={_effective.get('mode_source')}"
                if _prev_reason else
                f"effective_mode={_effective_mode} source={_effective.get('mode_source')}"
            )
        _mr["trading_mode"] = _effective_mode
        _mr["mode_color"] = _MODE_COLOR.get(_effective_mode, _mr.get("mode_color", "#888"))
        _mr["mode_desc"] = _MODE_DESC.get(_effective_mode, _mr.get("mode_desc", ""))
        _mr["mode_effective_source"] = _effective.get("mode_source")
        result["market_regime"] = _mr
        result["effective_runtime"] = _effective
    except Exception as e:
        result["effective_runtime"] = {"mode": "SPOT_BULL", "profile_label": "A", "error": str(e)}

    # Active regime profile shown in dashboard status bar (A/B/C + compact semantic alias)
    try:
        _effective = result.get("effective_runtime", {})
        _mode = str(_effective.get("mode") or result.get("market_regime", {}).get("trading_mode") or "SPOT_BULL").strip().upper() or "SPOT_BULL"
        _active = _get_active_profile_summary(_mode)
        _eff_profile = str(_effective.get("profile_label") or "").strip().upper()
        if _eff_profile:
            _active["label"] = _eff_profile
            _active["alias"] = _PROFILE_ALIAS.get(_eff_profile, _eff_profile)
            _active["source"] = _effective.get("profile_source")
            _active["updated_at"] = _active.get("updated_at") or _effective.get("profile_updated_at")
        result["active_profile"] = _active
    except Exception as e:
        result["active_profile"] = {"mode": "SPOT_BULL", "label": "A", "alias": "Agresivo", "legend": _PROFILE_ALIAS, "legend_text": "A=Agresivo | B=Balance | C=Defensa", "error": str(e)}

    # Operator Focus: concise actionable queue for live operation
    try:
        result["operator_focus"] = _build_operator_focus(
            result.get("health", {}),
            result.get("health_results", {}),
            result.get("effective_runtime", {}),
            result.get("pause_new_entries", {}),
            binance_truth=result.get("binance_truth"),
            binance_truth_spot=result.get("binance_truth_spot"),
        )
    except Exception as e:
        result["operator_focus"] = {"items": [], "error": str(e)}

    try:
        result["profit_realization"] = await get_profit_realization_metrics()
    except Exception as e:
        result["profit_realization"] = {"ok": False, "error": str(e)}

    # Market context: Fear & Greed + CoinGecko Trending
    try:
        _nc = news_client
        _fng = _nc.get_fear_greed()
        _fng_mod = _nc.get_fear_greed_modifier()
        _trending = _nc.get_trending_symbols()
        _analysis_base = [s.replace("USDT", "") for s in _cfg_param(settings, "analysis_symbols")]
        # News sentiment (Fase 3) — count positive/negative articles last 24h
        _news_sentiment = {"positive": 0, "negative": 0, "neutral": 0}
        try:
            _all_syms = _cfg_param(settings, "analysis_symbols")
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
        import asyncio as _asyncio
        ticker_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
        async def _fetch_ticker(sym):
            try:
                open_p = await _asyncio.to_thread(binance_client.get_daily_open_price, sym)
                cur_p = _get_request_price(price_cache, sym)
                change_pct = round((cur_p - open_p) / open_p * 100, 2) if open_p else 0.0
                return {"symbol": sym, "open_price": round(open_p, 6),
                        "current_price": round(cur_p, 6), "change_pct": change_pct}
            except Exception:
                return None
        _ticker_results = await _asyncio.gather(*[_fetch_ticker(s) for s in ticker_symbols])
        result["daily_ticker"] = [r for r in _ticker_results if r is not None]
    except Exception as e:
        result["daily_ticker"] = []

    result["monte_carlo_projection"] = _get_monte_carlo_projection()

    # Pair Screener — build from analysis_cache (per-symbol technical data)
    try:
        _all_cached = analysis_cache.get_all()
        _regime_mode = result.get("market_regime", {}).get("mode", "SPOT_BULL")
        _screener_data = []
        for _sym, _cdata in _all_cached.items():
            _ind = _cdata.get("indicators", {}) if isinstance(_cdata, dict) else {}
            _score = float(_cdata.get("technical_score", 50.0)) if isinstance(_cdata, dict) else 50.0
            _strength = _score / 100.0  # technical_score is strength * 100
            _rsi = float(_ind.get("rsi", 50.0)) if _ind else 50.0
            _vol = float(_ind.get("vol_spike", 0.5)) if _ind else 0.5
            _trend = _ind.get("trend_4h", "FLAT") if _ind else "FLAT"
            _bb_lower = float(_ind.get("bb_lower", 0.0)) if _ind else 0.0
            _bb_upper = float(_ind.get("bb_upper", 0.0)) if _ind else 0.0
            _price = float(_ind.get("price", 0.0)) if _ind else 0.0
            _bb_lower_touch = _bb_lower > 0 and _price > 0 and _price <= _bb_lower * 1.005
            _bb_upper_touch = _bb_upper > 0 and _price > 0 and _price >= _bb_upper * 0.995
            _chg = float(_ind.get("change_24h", 0.0)) if _ind else 0.0
            # Raw bias: technical signal only, fixed scale bug (strength is 0-1, not 0-100)
            _raw_bias = "LONG" if _strength >= 0.6 else ("SHORT" if _strength <= 0.35 else "NEUTRAL")
            # Regime-adjusted bias: context of current market regime
            _regime_bias = _raw_bias
            if _regime_mode == "FUTURES_SHORT":
                # In bear regime, short the bounces inside downtrend, not oversold dumps
                if _trend == "DOWN" and _rsi >= 45 and _rsi <= 65 and _chg > -3.0:
                    _regime_bias = "SHORT"
                elif _trend == "DOWN" and _rsi > 65 and _chg > 0:
                    _regime_bias = "SHORT"
                elif _bb_lower_touch and _rsi < 40:
                    _regime_bias = "LONG"  # clear bounce at support
                else:
                    _regime_bias = "NEUTRAL"
            elif _regime_mode == "SPOT_BULL":
                if _raw_bias == "LONG" or (_trend == "UP" and _rsi < 65 and _chg > 0):
                    _regime_bias = "LONG"
                elif _trend == "DOWN" and _rsi > 65:
                    _regime_bias = "SHORT"
                else:
                    _regime_bias = "NEUTRAL"
            elif _regime_mode == "RANGE_SCALP":
                if _bb_lower_touch and _rsi < 40:
                    _regime_bias = "LONG"
                elif _bb_upper_touch and _rsi > 65:
                    _regime_bias = "SHORT"
                else:
                    _regime_bias = "NEUTRAL"
            elif _regime_mode == "PRESERVATION":
                _regime_bias = "NEUTRAL"
            _screener_data.append({
                "symbol": _sym,
                "composite_score": round(_score, 3),
                "strength": round(_strength, 3),
                "rsi_4h": round(_rsi, 1),
                "vol_spike": round(_vol, 2),
                "change_24h": round(_chg, 2),
                "trend_4h": _trend,
                "bias": _raw_bias,
                "regime_adjusted_bias": _regime_bias,
            })
        # Sort by composite score descending
        _screener_data.sort(key=lambda x: x["composite_score"], reverse=True)
        if not _screener_data:
            # Last resort fallback: daily_ticker with basic metrics
            for t in result.get("daily_ticker", []):
                _chg = t.get("change_pct", 0)
                _trend = "DOWN" if _chg < -1 else ("UP" if _chg > 1 else "FLAT")
                _raw_bias = "SHORT" if _chg < -1 else ("LONG" if _chg > 1 else "NEUTRAL")
                _regime_bias = _raw_bias
                if _regime_mode == "FUTURES_SHORT":
                    _regime_bias = "SHORT" if _chg > 0 and _trend == "DOWN" else ("NEUTRAL" if _chg < -1 else _raw_bias)
                _screener_data.append({
                    "symbol": t["symbol"],
                    "composite_score": 50.0,
                    "strength": 0.5,
                    "rsi_4h": 50.0,
                    "vol_spike": 0.5,
                    "change_24h": round(_chg, 2),
                    "trend_4h": _trend,
                    "bias": _raw_bias,
                    "regime_adjusted_bias": _regime_bias,
                })
        result["pair_screener"] = {
            "regime_mode": _regime_mode,
            "scanner_note": (
                "FUTURES_SHORT: bias ajustado = shortear rebotes en tendencia bajista, "
                "no caídas sobreextendidas."
                if _regime_mode == "FUTURES_SHORT"
                else "SPOT_BULL: bias ajustado = priorizar pares alcistas."
                if _regime_mode == "SPOT_BULL"
                else "Régimen lateral: bias ajustado = comprar en soporte, vender en resistencia."
            ),
            "candidates": _screener_data,
        }
    except Exception as _se:
        result["pair_screener"] = {"regime_mode": "SPOT_BULL", "candidates": []}

    # Expectativa vs Realidad — build from closed positions with predictions
    try:
        _evr_closed = position_manager.get_closed_positions(limit=200)
        _evr_details = []
        for _p in _evr_closed:
            _pred = _p.pnl_prediction
            if _pred and isinstance(_pred, dict):
                _ep = float(_pred.get("expected_pnl") or 0)
                _ap = float(_p.pnl or 0)
                _evr_details.append({
                    "symbol": _p.symbol,
                    "side": _p.side,
                    "prediction_pnl": round(_ep, 4),
                    "actual_pnl": round(_ap, 4),
                    "prediction_accuracy": float(_p.prediction_accuracy) if _p.prediction_accuracy is not None else None,
                    "entry_time": to_mexico_time_str(_p.entry_time) if _p.entry_time else "",
                    "opened_at": to_mexico_time_str(_p.entry_time) if _p.entry_time else "",
                    "exit_time": to_mexico_time_str(_p.exit_time) if _p.exit_time else "",
                    "closed_at": to_mexico_time_str(_p.exit_time) if _p.exit_time else "",
                })
        result["expectativa_vs_realidad"] = {
            "details": _evr_details[-50:],
            "count": len(_evr_details),
        }
    except Exception as _evre:
        result["expectativa_vs_realidad"] = {"details": [], "count": 0}

    _snapshot_finished_at = datetime.now(timezone.utc)
    _snapshot_generation_ms = int((_snapshot_finished_at - _snapshot_started_at).total_seconds() * 1000)
    _truth_sev = str((result.get("binance_truth") or {}).get("severity") or "unknown").lower()
    _truth_spot_sev = str((result.get("binance_truth_spot") or {}).get("severity") or "unknown").lower()
    result["snapshot_meta"] = {
        "snapshot_timestamp": _snapshot_started_at.isoformat(),
        "snapshot_finished_at": _snapshot_finished_at.isoformat(),
        "snapshot_generation_ms": _snapshot_generation_ms,
        "data_freshness_seconds": round((datetime.now(timezone.utc) - _snapshot_started_at).total_seconds(), 3),
        "binance_truth_severity": _truth_sev,
        "binance_truth_spot_severity": _truth_spot_sev,
        "tactical_decision_blocked": _truth_sev in ("critical",) or _truth_spot_sev in ("high",),
    }
    result["timestamp"] = to_mexico_time(datetime.utcnow()).isoformat()
    return result


@app.get("/market/daily-ticker")
async def get_daily_ticker() -> Dict:
    """Daily open (8AM Mexico) vs current price for main trading pairs."""
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    data = []
    price_cache: Dict[str, float] = {}
    for sym in symbols:
        try:
            open_p = binance_client.get_daily_open_price(sym)
            cur_p = _get_request_price(price_cache, sym)
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
                "daily_limit": getattr(settings, 'volspike_override_daily_limit', 1),
                "positions_limit": getattr(settings, 'volspike_override_max_positions', 1),
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


def _trade_scope_expr() -> str:
    return (
        "CASE "
        "WHEN COALESCE(metadata->>'trade_type','') <> '' THEN lower(metadata->>'trade_type') "
        "WHEN side IN ('BUY','SELL') THEN 'spot' "
        "WHEN side IN ('LONG','SHORT') THEN 'futures' "
        "ELSE 'system' END"
    )


def _summarize_closed_trades_range(start_ts: datetime, end_ts: Optional[datetime] = None) -> Dict[str, Any]:
    _end_ts = end_ts or datetime.now(timezone.utc)
    rows = db_client.execute_query(
        f"""
        SELECT
            {_trade_scope_expr()} AS trade_scope,
            COUNT(*)::bigint AS n_trades,
            COALESCE(SUM(pnl), 0)::numeric AS pnl_sum,
            COALESCE(AVG(pnl), 0)::numeric AS avg_pnl,
            COALESCE(100.0 * AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0)::numeric AS win_rate_pct,
            COALESCE(SUM(COALESCE(entry_commission,0) + COALESCE(exit_commission,0)), 0)::numeric AS fees_sum,
            COALESCE(AVG(pnl_percentage), 0)::numeric AS pnl_pct_avg
        FROM trades_closed
        WHERE exit_time >= %s AND exit_time < %s
        GROUP BY trade_scope
        ORDER BY trade_scope
        """,
        (start_ts, _end_ts),
    )
    by_scope: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        scope = str(r.get("trade_scope") or "system").lower()
        by_scope[scope] = {
            "n_trades": int(r.get("n_trades") or 0),
            "pnl_sum": round(float(r.get("pnl_sum") or 0.0), 4),
            "avg_pnl": round(float(r.get("avg_pnl") or 0.0), 4),
            "win_rate_pct": round(float(r.get("win_rate_pct") or 0.0), 2),
            "fees_sum": round(float(r.get("fees_sum") or 0.0), 4),
            "pnl_pct_avg": round(float(r.get("pnl_pct_avg") or 0.0), 4),
        }
    return {"start_ts": start_ts.isoformat(), "end_ts": _end_ts.isoformat(), "by_scope": by_scope}


def _trade_diagnostics_range(start_ts: datetime, end_ts: Optional[datetime] = None) -> Dict[str, Any]:
    _end_ts = end_ts or datetime.now(timezone.utc)
    rows = db_client.execute_query(
        f"""
        SELECT
            {_trade_scope_expr()} AS trade_scope,
            close_reason,
            pnl,
            pnl_percentage,
            COALESCE(entry_commission, 0) AS entry_commission,
            COALESCE(exit_commission, 0) AS exit_commission,
            EXTRACT(EPOCH FROM (exit_time - entry_time))::numeric AS hold_seconds
        FROM trades_closed
        WHERE exit_time >= %s AND exit_time < %s
        ORDER BY exit_time ASC
        """,
        (start_ts, _end_ts),
    )

    def _num(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    def _build_bucket() -> Dict[str, Any]:
        return {
            "n_trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl_sum": 0.0,
            "fees_sum": 0.0,
            "hold_seconds_sum": 0.0,
            "pnl_pct_sum": 0.0,
            "win_pnl_sum": 0.0,
            "loss_pnl_sum": 0.0,
            "by_reason": {},
        }

    buckets: Dict[str, Dict[str, Any]] = {"spot": _build_bucket(), "futures": _build_bucket()}

    for row in rows:
        scope = str(row.get("trade_scope") or "system").lower()
        if scope not in buckets:
            continue
        bucket = buckets[scope]
        pnl = _num(row.get("pnl"))
        pnl_pct = _num(row.get("pnl_percentage"))
        fees = _num(row.get("entry_commission")) + _num(row.get("exit_commission"))
        hold_seconds = _num(row.get("hold_seconds"))
        reason = _normalize_close_reason(row.get("close_reason"))

        bucket["n_trades"] += 1
        bucket["pnl_sum"] += pnl
        bucket["fees_sum"] += fees
        bucket["hold_seconds_sum"] += hold_seconds
        bucket["pnl_pct_sum"] += pnl_pct
        if pnl > 0:
            bucket["wins"] += 1
            bucket["win_pnl_sum"] += pnl
        elif pnl < 0:
            bucket["losses"] += 1
            bucket["loss_pnl_sum"] += pnl

        reason_bucket = bucket["by_reason"].setdefault(
            reason,
            {
                "close_reason": reason,
                "n_trades": 0,
                "pnl_sum": 0.0,
                "fees_sum": 0.0,
                "hold_seconds_sum": 0.0,
                "wins": 0,
                "losses": 0,
                "win_pnl_sum": 0.0,
                "loss_pnl_sum": 0.0,
            },
        )
        reason_bucket["n_trades"] += 1
        reason_bucket["pnl_sum"] += pnl
        reason_bucket["fees_sum"] += fees
        reason_bucket["hold_seconds_sum"] += hold_seconds
        if pnl > 0:
            reason_bucket["wins"] += 1
            reason_bucket["win_pnl_sum"] += pnl
        elif pnl < 0:
            reason_bucket["losses"] += 1
            reason_bucket["loss_pnl_sum"] += pnl

    def _finalize(raw: Dict[str, Any]) -> Dict[str, Any]:
        n_trades = int(raw.get("n_trades") or 0)
        wins = int(raw.get("wins") or 0)
        losses = int(raw.get("losses") or 0)
        pnl_sum = _num(raw.get("pnl_sum"))
        fees_sum = _num(raw.get("fees_sum"))
        hold_seconds_sum = _num(raw.get("hold_seconds_sum"))
        pnl_pct_sum = _num(raw.get("pnl_pct_sum"))
        reason_rows = []
        for item in raw.get("by_reason", {}).values():
            item_n = int(item.get("n_trades") or 0)
            item_wins = int(item.get("wins") or 0)
            item_losses = int(item.get("losses") or 0)
            item_pnl = _num(item.get("pnl_sum"))
            item_fees = _num(item.get("fees_sum"))
            item_hold = _num(item.get("hold_seconds_sum"))
            reason_rows.append({
                "close_reason": item.get("close_reason"),
                "n_trades": item_n,
                "pnl_sum": round(item_pnl, 4),
                "net_pnl": round(item_pnl - item_fees, 4),
                "fees_sum": round(item_fees, 4),
                "win_rate_pct": round((100.0 * item_wins / item_n), 2) if item_n else 0.0,
                "avg_pnl": round(item_pnl / item_n, 4) if item_n else 0.0,
                "avg_win": round(_num(item.get("win_pnl_sum")) / item_wins, 4) if item_wins else 0.0,
                "avg_loss": round(_num(item.get("loss_pnl_sum")) / item_losses, 4) if item_losses else 0.0,
                "avg_hold_hours": round((item_hold / item_n) / 3600.0, 2) if item_n else 0.0,
            })
        reason_rows.sort(key=lambda item: (-item["n_trades"], item["close_reason"]))
        return {
            "n_trades": n_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((100.0 * wins / n_trades), 2) if n_trades else 0.0,
            "pnl_sum": round(pnl_sum, 4),
            "net_pnl": round(pnl_sum - fees_sum, 4),
            "fees_sum": round(fees_sum, 4),
            "fees_pct_of_gross_pnl": round((100.0 * fees_sum / abs(pnl_sum)), 2) if pnl_sum else 0.0,
            "avg_pnl": round(pnl_sum / n_trades, 4) if n_trades else 0.0,
            "avg_win": round(_num(raw.get("win_pnl_sum")) / wins, 4) if wins else 0.0,
            "avg_loss": round(_num(raw.get("loss_pnl_sum")) / losses, 4) if losses else 0.0,
            "avg_hold_hours": round((hold_seconds_sum / n_trades) / 3600.0, 2) if n_trades else 0.0,
            "avg_pnl_pct": round(pnl_pct_sum / n_trades, 4) if n_trades else 0.0,
            "stop_loss_share_pct": round((100.0 * sum(1 for item in reason_rows if item["close_reason"] == "stop_loss" for _ in range(item["n_trades"])) / n_trades), 2) if n_trades else 0.0,
            "by_reason": reason_rows,
        }

    by_scope = {scope: _finalize(bucket) for scope, bucket in buckets.items()}
    aggregate = _build_bucket()
    for scope in ("spot", "futures"):
        bucket = buckets[scope]
        aggregate["n_trades"] += bucket["n_trades"]
        aggregate["wins"] += bucket["wins"]
        aggregate["losses"] += bucket["losses"]
        aggregate["pnl_sum"] += bucket["pnl_sum"]
        aggregate["fees_sum"] += bucket["fees_sum"]
        aggregate["hold_seconds_sum"] += bucket["hold_seconds_sum"]
        aggregate["pnl_pct_sum"] += bucket["pnl_pct_sum"]
        aggregate["win_pnl_sum"] += bucket["win_pnl_sum"]
        aggregate["loss_pnl_sum"] += bucket["loss_pnl_sum"]
        for reason, item in bucket["by_reason"].items():
            agg_reason = aggregate["by_reason"].setdefault(
                reason,
                {
                    "close_reason": reason,
                    "n_trades": 0,
                    "pnl_sum": 0.0,
                    "fees_sum": 0.0,
                    "hold_seconds_sum": 0.0,
                    "wins": 0,
                    "losses": 0,
                    "win_pnl_sum": 0.0,
                    "loss_pnl_sum": 0.0,
                },
            )
            for key in ("n_trades", "pnl_sum", "fees_sum", "hold_seconds_sum", "wins", "losses", "win_pnl_sum", "loss_pnl_sum"):
                agg_reason[key] += item.get(key, 0)

    return {
        "start_ts": start_ts.isoformat(),
        "end_ts": _end_ts.isoformat(),
        "summary": _finalize(aggregate),
        "by_scope": by_scope,
    }


def _trade_throughput_exposure_range(
    start_ts: datetime,
    end_ts: Optional[datetime] = None,
    cycle_minutes: int = 5,
) -> Dict[str, Any]:
    _end_ts = end_ts or datetime.now(timezone.utc)
    if cycle_minutes <= 0:
        raise ValueError("cycle_minutes must be > 0")

    rows = db_client.execute_query(
        f"""
        SELECT
            {_trade_scope_expr()} AS trade_scope,
            symbol,
            entry_time,
            exit_time,
            quantity,
            entry_price,
            pnl,
            close_reason
        FROM trades_closed
        WHERE exit_time >= %s AND exit_time < %s
        ORDER BY exit_time ASC
        """,
        (start_ts, _end_ts),
    )

    def _num(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    def _to_utc_dt(value: Any, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return fallback
        return fallback

    def _floor_cycle(ts: datetime, minutes: int) -> datetime:
        cycle_seconds = minutes * 60
        epoch = int(ts.timestamp())
        floored = epoch - (epoch % cycle_seconds)
        return datetime.fromtimestamp(floored, tz=timezone.utc)

    def _bucket() -> Dict[str, Any]:
        return {
            "n_trades": 0,
            "cycle_counts": {},
            "notional_values": [],
            "hold_seconds_values": [],
            "capital_time_usdt_hours": 0.0,
            "intervals": [],
            "reason_counts": {},
            "symbol_capital_time": {},
            "pnl_sum": 0.0,
        }

    buckets: Dict[str, Dict[str, Any]] = {"spot": _bucket(), "futures": _bucket()}
    window_seconds = max((_end_ts - start_ts).total_seconds(), 1.0)

    for row in rows:
        scope = str(row.get("trade_scope") or "system").lower()
        if scope not in buckets:
            continue
        bucket = buckets[scope]

        exit_time = _to_utc_dt(row.get("exit_time"), _end_ts)
        entry_time = _to_utc_dt(row.get("entry_time"), exit_time)
        if entry_time > exit_time:
            entry_time = exit_time

        quantity = abs(_num(row.get("quantity")))
        entry_price = abs(_num(row.get("entry_price")))
        notional = quantity * entry_price
        hold_seconds = max((exit_time - entry_time).total_seconds(), 0.0)
        hold_hours = hold_seconds / 3600.0

        cycle_key = _floor_cycle(exit_time, cycle_minutes).isoformat()
        bucket["cycle_counts"][cycle_key] = int(bucket["cycle_counts"].get(cycle_key, 0)) + 1
        bucket["n_trades"] += 1
        bucket["notional_values"].append(notional)
        bucket["hold_seconds_values"].append(hold_seconds)
        bucket["capital_time_usdt_hours"] += notional * hold_hours
        bucket["intervals"].append((entry_time, exit_time))
        bucket["pnl_sum"] += _num(row.get("pnl"))

        reason = _normalize_close_reason(row.get("close_reason"))
        bucket["reason_counts"][reason] = int(bucket["reason_counts"].get(reason, 0)) + 1

        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            bucket["symbol_capital_time"][symbol] = _num(bucket["symbol_capital_time"].get(symbol)) + (notional * hold_hours)

    def _avg_concurrency(intervals: List[Any]) -> Dict[str, float]:
        if not intervals:
            return {"avg": 0.0, "max": 0.0}
        events = []
        for start_i, end_i in intervals:
            events.append((start_i, 1))
            events.append((end_i, -1))
        events.sort(key=lambda item: (item[0], item[1]))

        concurrent = 0.0
        max_concurrent = 0.0
        area = 0.0
        prev = start_ts
        for ts, delta in events:
            seg_start = prev if prev > start_ts else start_ts
            seg_end = ts if ts < _end_ts else _end_ts
            seg_seconds = max((seg_end - seg_start).total_seconds(), 0.0)
            if seg_seconds > 0:
                area += concurrent * seg_seconds
            concurrent += float(delta)
            if concurrent > max_concurrent:
                max_concurrent = concurrent
            prev = ts
        if prev < _end_ts:
            area += concurrent * max((_end_ts - prev).total_seconds(), 0.0)
        avg = area / window_seconds
        return {"avg": avg, "max": max_concurrent}

    def _pctile(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        v = sorted(values)
        idx = int(round((len(v) - 1) * p))
        idx = max(0, min(idx, len(v) - 1))
        return v[idx]

    def _finalize(scope: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        n_trades = int(raw.get("n_trades") or 0)
        cycle_counts = raw.get("cycle_counts") or {}
        cycle_values = list(cycle_counts.values())
        active_cycles = len(cycle_values)
        notional_values = [abs(_num(v)) for v in (raw.get("notional_values") or [])]
        hold_values = [max(_num(v), 0.0) for v in (raw.get("hold_seconds_values") or [])]
        total_notional = sum(notional_values)

        top_k = max(1, (n_trades * 20 + 99) // 100) if n_trades else 0
        top_notional_share = 0.0
        if n_trades and total_notional > 0:
            top_notional_share = (sum(sorted(notional_values, reverse=True)[:top_k]) / total_notional) * 100.0

        symbol_ct = raw.get("symbol_capital_time") or {}
        symbol_ct_sum = sum(_num(v) for v in symbol_ct.values())
        top_symbol = None
        top_symbol_share = 0.0
        if symbol_ct:
            top_symbol = max(symbol_ct.items(), key=lambda item: _num(item[1]))[0]
            if symbol_ct_sum > 0:
                top_symbol_share = (_num(symbol_ct[top_symbol]) / symbol_ct_sum) * 100.0

        concurrency = _avg_concurrency(raw.get("intervals") or [])
        max_slots = int(_cfg_param(settings, "max_positions_spot_bull")) if scope == "spot" else int(_cfg_param(settings, "max_positions_futures_short"))
        slot_util_pct = (concurrency["avg"] / max(max_slots, 1)) * 100.0

        cycle_sorted = sorted(cycle_counts.items(), key=lambda item: (-int(item[1]), item[0]))
        top_cycles = [{"cycle_start": k, "n_trades": int(v)} for k, v in cycle_sorted[:5]]

        return {
            "n_trades": n_trades,
            "window_hours": round(window_seconds / 3600.0, 2),
            "active_cycles": active_cycles,
            "avg_trades_per_active_cycle": round((sum(cycle_values) / active_cycles), 3) if active_cycles else 0.0,
            "max_trades_in_cycle": int(max(cycle_values)) if cycle_values else 0,
            "trades_per_hour": round((n_trades / (window_seconds / 3600.0)), 3) if window_seconds > 0 else 0.0,
            "rotation_per_24h": round((n_trades / (window_seconds / 3600.0)) * 24.0, 3) if window_seconds > 0 else 0.0,
            "pnl_sum": round(_num(raw.get("pnl_sum")), 4),
            "total_notional_usdt": round(total_notional, 4),
            "avg_notional_usdt": round((total_notional / n_trades), 4) if n_trades else 0.0,
            "p75_notional_usdt": round(_pctile(notional_values, 0.75), 4),
            "avg_hold_hours": round((sum(hold_values) / n_trades) / 3600.0, 3) if n_trades else 0.0,
            "capital_time_usdt_hours": round(_num(raw.get("capital_time_usdt_hours")), 4),
            "avg_concurrent_positions": round(concurrency["avg"], 4),
            "max_concurrent_positions": round(concurrency["max"], 4),
            "max_slots_reference": max_slots,
            "slot_utilization_pct": round(slot_util_pct, 3),
            "top20_notional_share_pct": round(top_notional_share, 3),
            "fragmentation_index_pct": round(100.0 - top_notional_share, 3) if n_trades else 0.0,
            "top_symbol_by_capital_time": top_symbol,
            "top_symbol_capital_time_share_pct": round(top_symbol_share, 3),
            "close_reason_counts": raw.get("reason_counts") or {},
            "top_cycles": top_cycles,
        }

    by_scope = {scope: _finalize(scope, raw) for scope, raw in buckets.items()}
    summary = {
        "n_trades": int(by_scope["spot"]["n_trades"] + by_scope["futures"]["n_trades"]),
        "total_notional_usdt": round(by_scope["spot"]["total_notional_usdt"] + by_scope["futures"]["total_notional_usdt"], 4),
        "capital_time_usdt_hours": round(by_scope["spot"]["capital_time_usdt_hours"] + by_scope["futures"]["capital_time_usdt_hours"], 4),
        "avg_slot_utilization_pct": round((by_scope["spot"]["slot_utilization_pct"] + by_scope["futures"]["slot_utilization_pct"]) / 2.0, 3),
    }
    return {
        "start_ts": start_ts.isoformat(),
        "end_ts": _end_ts.isoformat(),
        "cycle_minutes": cycle_minutes,
        "summary": summary,
        "by_scope": by_scope,
    }


def _quantify_spot_bull_root_causes(hours: int = 168, cycle_minutes: int = 5) -> Dict[str, Any]:
    if hours <= 0:
        raise ValueError("hours must be > 0")
    if cycle_minutes <= 0:
        raise ValueError("cycle_minutes must be > 0")

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(hours=hours)
    diagnostics = _trade_diagnostics_range(start_ts, end_ts)
    throughput = _trade_throughput_exposure_range(start_ts, end_ts, cycle_minutes=cycle_minutes)

    early_exit_reasons = {
        "portfolio_rebalancing",
        "prediction_exit_deteriorated",
        "sso_slow_crawl",
        "manual",
        "manual_rebalance",
        "active_replacement",
    }

    scopes = {}
    for scope in ("spot", "futures"):
        d = (diagnostics.get("by_scope") or {}).get(scope) or {}
        t = (throughput.get("by_scope") or {}).get(scope) or {}
        n_trades = int(d.get("n_trades") or 0)
        by_reason = d.get("by_reason") or []
        reason_count_map = {str(item.get("close_reason")): int(item.get("n_trades") or 0) for item in by_reason}
        early_exit_count = sum(reason_count_map.get(reason, 0) for reason in early_exit_reasons)
        early_exit_share = (100.0 * early_exit_count / n_trades) if n_trades else 0.0

        stop_loss_share = float(d.get("stop_loss_share_pct") or 0.0)
        win_rate = float(d.get("win_rate_pct") or 0.0)
        avg_win = abs(float(d.get("avg_win") or 0.0))
        avg_loss = abs(float(d.get("avg_loss") or 0.0))
        rr_ratio = (avg_win / avg_loss) if avg_loss > 0 else (1.0 if avg_win > 0 else 0.0)

        entry_score = max(0.0, (stop_loss_share * 0.65) + (max(0.0, 50.0 - win_rate) * 0.35))
        exit_score = max(0.0, (early_exit_share * 0.60) + (max(0.0, 1.20 - rr_ratio) * 40.0))
        sizing_score = max(
            0.0,
            (float(t.get("fragmentation_index_pct") or 0.0) * 0.70)
            + (max(0.0, 30.0 - float(t.get("slot_utilization_pct") or 0.0)) * 0.30),
        )
        total = entry_score + exit_score + sizing_score
        if total <= 0:
            contrib = {"entry_quality_pct": 0.0, "exit_policy_pct": 0.0, "sizing_fragmentation_pct": 0.0}
        else:
            contrib = {
                "entry_quality_pct": round((entry_score / total) * 100.0, 2),
                "exit_policy_pct": round((exit_score / total) * 100.0, 2),
                "sizing_fragmentation_pct": round((sizing_score / total) * 100.0, 2),
            }

        scopes[scope] = {
            "n_trades": n_trades,
            "scores": {
                "entry_quality": round(entry_score, 4),
                "exit_policy": round(exit_score, 4),
                "sizing_fragmentation": round(sizing_score, 4),
            },
            "contributions_pct": contrib,
            "evidence": {
                "stop_loss_share_pct": round(stop_loss_share, 2),
                "win_rate_pct": round(win_rate, 2),
                "early_exit_share_pct": round(early_exit_share, 2),
                "reward_to_risk_ratio": round(rr_ratio, 4),
                "fragmentation_index_pct": round(float(t.get("fragmentation_index_pct") or 0.0), 3),
                "slot_utilization_pct": round(float(t.get("slot_utilization_pct") or 0.0), 3),
            },
        }

    total_trades = sum(int((scopes.get(scope) or {}).get("n_trades") or 0) for scope in ("spot", "futures"))
    weighted = {"entry_quality": 0.0, "exit_policy": 0.0, "sizing_fragmentation": 0.0}
    for scope in ("spot", "futures"):
        item = scopes.get(scope) or {}
        w = (float(item.get("n_trades") or 0.0) / total_trades) if total_trades else 0.0
        contrib = item.get("contributions_pct") or {}
        weighted["entry_quality"] += w * float(contrib.get("entry_quality_pct") or 0.0)
        weighted["exit_policy"] += w * float(contrib.get("exit_policy_pct") or 0.0)
        weighted["sizing_fragmentation"] += w * float(contrib.get("sizing_fragmentation_pct") or 0.0)

    return {
        "ok": True,
        "window": {
            "hours": hours,
            "cycle_minutes": cycle_minutes,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
        },
        "scopes": scopes,
        "overall_weighted_contributions_pct": {
            "entry_quality_pct": round(weighted["entry_quality"], 2),
            "exit_policy_pct": round(weighted["exit_policy"], 2),
            "sizing_fragmentation_pct": round(weighted["sizing_fragmentation"], 2),
        },
        "diagnostics": diagnostics,
        "throughput_exposure": throughput,
    }


_SPOT_BULL_CANDIDATE_V1: Dict[str, Any] = {
    "candidate_label": "spot_bull_quality_v1",
    "objective": "single-mode, high-conviction sizing with anti-strangling entry gates and explicit futures mirror",
    "spot_changes": {
        "min_signal_strength": 0.41,
        "min_success_probability": 0.54,
        "max_positions_spot_bull": 24,
        "replace_ev_min_ratio": 1.05,
        "overflow_min_strength": 0.80,
        "overflow_min_prob": 0.62,
        "kelly_conviction_max_pct": 0.13,
    },
    "futures_mirror_changes": {
        "futures_min_prob": 0.52,
        "futures_vol_spike_min": 1.00,
        "futures_max_new_per_cycle": 4,
        "futures_overflow_min_strength": 0.58,
        "futures_overflow_min_vol_spike": 2.0,
        "max_positions_futures_short": 12,
    },
    "notes": [
        "Mirror is explicit: each anti-strangling SPOT move has a bounded equivalent in FUTURES.",
        "Candidate does not auto-apply runtime config; it must pass Smart Backtest validation gate.",
        "Use with /smart-backtest/validation/start or /tuning/candidates/spot-bull-v1/validate-start.",
    ],
}


def _build_spot_bull_candidate_snapshot() -> Dict[str, Any]:
    current = dump_active_config(settings)
    spot_changes = _SPOT_BULL_CANDIDATE_V1.get("spot_changes") or {}
    futures_changes = _SPOT_BULL_CANDIDATE_V1.get("futures_mirror_changes") or {}

    deltas: Dict[str, Dict[str, Any]] = {}
    candidate_params: Dict[str, Any] = {}
    for param, new_value in {**spot_changes, **futures_changes}.items():
        old_value = current.get(param)
        deltas[param] = {"current": old_value, "candidate": new_value}
        candidate_params[param] = new_value

    return {
        **_SPOT_BULL_CANDIDATE_V1,
        "candidate_params": candidate_params,
        "deltas": deltas,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _get_performance_changepoint_metrics() -> Dict[str, Any]:
    try:
        marker_rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state "
            "WHERE key LIKE %s "
            "ORDER BY updated_at DESC, key DESC LIMIT 1",
            ('performance_changepoint_%',)
        )
        if not marker_rows:
            return {"ok": False, "error": "performance changepoint marker not found"}

        marker_row = marker_rows[0]
        marker_value = marker_row.get("value") or {}
        if isinstance(marker_value, str):
            try:
                marker_value = json.loads(marker_value)
            except Exception:
                marker_value = {}
        marker_ts = marker_row.get("updated_at") or datetime.now(timezone.utc)
        if isinstance(marker_ts, str):
            marker_ts = datetime.fromisoformat(marker_ts.replace("Z", "+00:00"))
        if marker_ts.tzinfo is None:
            marker_ts = marker_ts.replace(tzinfo=timezone.utc)

        now_ts = datetime.now(timezone.utc)
        elapsed = max((now_ts - marker_ts).total_seconds(), 1.0)
        previous_start = marker_ts - timedelta(seconds=elapsed)

        since_marker = _summarize_closed_trades_range(marker_ts, now_ts)
        prev_window = _summarize_closed_trades_range(previous_start, marker_ts)

        def _aggregate_window(window: Dict[str, Any]) -> Dict[str, Any]:
            scopes = window.get("by_scope", {}) or {}
            total = {
                "n_trades": 0,
                "pnl_sum": 0.0,
                "fees_sum": 0.0,
                "weighted_win_numerator": 0.0,
            }
            for scope in ("spot", "futures"):
                stats = scopes.get(scope, {})
                n = int(stats.get("n_trades") or 0)
                total["n_trades"] += n
                total["pnl_sum"] += float(stats.get("pnl_sum") or 0.0)
                total["fees_sum"] += float(stats.get("fees_sum") or 0.0)
                total["weighted_win_numerator"] += n * float(stats.get("win_rate_pct") or 0.0)
            total["win_rate_pct"] = round(total["weighted_win_numerator"] / total["n_trades"], 2) if total["n_trades"] else 0.0
            total["pnl_sum"] = round(total["pnl_sum"], 4)
            total["fees_sum"] = round(total["fees_sum"], 4)
            total["net_pnl"] = round(total["pnl_sum"] - total["fees_sum"], 4)
            return total

        current_total = _aggregate_window(since_marker)
        previous_total = _aggregate_window(prev_window)

        return {
            "ok": True,
            "marker": {
                "key": marker_row.get("key"),
                "changepoint_id": marker_value.get("changepoint_id"),
                "base_run_id": marker_value.get("base_run_id"),
                "root_session_id": marker_value.get("root_session_id"),
                "updated_at": marker_ts.isoformat(),
            },
            "current_window": {**since_marker, "summary": current_total},
            "previous_window": {**prev_window, "summary": previous_total},
            "delta": {
                "n_trades": current_total["n_trades"] - previous_total["n_trades"],
                "pnl_sum": round(current_total["pnl_sum"] - previous_total["pnl_sum"], 4),
                "fees_sum": round(current_total["fees_sum"] - previous_total["fees_sum"], 4),
                "net_pnl": round(current_total["net_pnl"] - previous_total["net_pnl"], 4),
                "win_rate_pct": round(current_total["win_rate_pct"] - previous_total["win_rate_pct"], 2),
            },
            "timestamp": now_ts.isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_mode_profile_harmony_snapshot() -> Dict[str, Any]:
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='mode_profile_harmony_snapshot'"
        )
        if not rows:
            return {"ok": False, "error": "mode profile harmony snapshot not found"}
        row = rows[0]
        value = row.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {}
        return {
            "ok": True,
            "snapshot": value if isinstance(value, dict) else {},
            "updated_at": (row.get("updated_at") or datetime.now(timezone.utc)).isoformat() if row.get("updated_at") else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_mode_profile_harmony_drift() -> Dict[str, Any]:
    try:
        harmony = _get_mode_profile_harmony_snapshot()
        if not harmony.get("ok"):
            return {"ok": False, "error": harmony.get("error") or "harmony snapshot unavailable"}

        snapshot = harmony.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        effective = _get_effective_runtime_selection(str(snapshot.get("selected_mode") or "SPOT_BULL"))
        _mode = str(effective.get("mode") or "SPOT_BULL").strip().upper() or "SPOT_BULL"
        _profile = str(effective.get("profile_label") or "A").strip().upper() or "A"

        # Recompute the expected harmonized values from a fresh base settings object.
        # This avoids comparing the persisted snapshot against the app-level settings
        # singleton, which may already be harmonized or even double-harmonized by
        # the _get_effective_runtime_selection call.
        _expected_settings = get_settings()
        _harmony_apply = getattr(run_trades, '_apply_mode_profile_harmony', None)
        if callable(_harmony_apply):
            try:
                _harmony_apply(_expected_settings, _mode, _profile)
            except Exception:
                pass

        mismatches = []

        def _compare_float(name: str, expected: Any, actual: Any, tolerance: float = 1e-6) -> None:
            if expected is None or actual is None:
                return
            try:
                if abs(float(expected) - float(actual)) > tolerance:
                    mismatches.append({"field": name, "expected": float(expected), "actual": float(actual)})
            except Exception:
                if expected != actual:
                    mismatches.append({"field": name, "expected": expected, "actual": actual})

        if str(snapshot.get("selected_mode") or "").strip().upper() != str(effective.get("mode") or "").strip().upper():
            mismatches.append({"field": "selected_mode", "expected": snapshot.get("selected_mode"), "actual": effective.get("mode")})
        if str(snapshot.get("selected_profile") or "").strip().upper() != str(effective.get("profile_label") or "").strip().upper():
            mismatches.append({"field": "selected_profile", "expected": snapshot.get("selected_profile"), "actual": effective.get("profile_label")})

        _compare_float("trailing_stop_activation_pct", snapshot.get("harmony_result", {}).get("applied", {}).get("trailing_stop_activation_pct", {}).get("new"), getattr(_expected_settings, 'trailing_stop_activation_pct', None))
        _compare_float("trailing_stop_distance_pct", snapshot.get("harmony_result", {}).get("applied", {}).get("trailing_stop_distance_pct", {}).get("new"), getattr(_expected_settings, 'trailing_stop_distance_pct', None))
        _compare_float("futures_breakeven_activation_pct", snapshot.get("futures_breakeven_activation_pct"), getattr(_expected_settings, 'futures_breakeven_activation_pct', None))
        _compare_float("futures_trailing_activation_pct", snapshot.get("futures_trailing_activation_pct"), getattr(_expected_settings, 'futures_trailing_activation_pct', None))
        _compare_float("futures_trailing_distance_pct", snapshot.get("futures_trailing_distance_pct"), getattr(_expected_settings, 'futures_trailing_distance_pct', None))
        _compare_float("futures_trail_floor_pct", snapshot.get("futures_trail_floor_pct"), getattr(_expected_settings, 'futures_trail_floor_pct', None))
        _compare_float("futures_trail_ceiling_pct", snapshot.get("futures_trail_ceiling_pct"), getattr(_expected_settings, 'futures_trail_ceiling_pct', None))
        _compare_float("futures_trail_atr_multiplier_low", snapshot.get("futures_trail_atr_multiplier_low"), getattr(_expected_settings, 'futures_trail_atr_multiplier_low', None))
        _compare_float("futures_trail_atr_multiplier_mid", snapshot.get("futures_trail_atr_multiplier_mid"), getattr(_expected_settings, 'futures_trail_atr_multiplier_mid', None))
        _compare_float("futures_trail_atr_multiplier_high", snapshot.get("futures_trail_atr_multiplier_high"), getattr(_expected_settings, 'futures_trail_atr_multiplier_high', None))
        _compare_float("futures_trail_atr_multiplier_deep", snapshot.get("futures_trail_atr_multiplier_deep"), getattr(_expected_settings, 'futures_trail_atr_multiplier_deep', None))
        _compare_float("sso_target_daily_usd", snapshot.get("sso_target_daily_usd"), getattr(_expected_settings, 'sso_target_daily_usd', None))
        _compare_float("sso_stagnant_min_hours", snapshot.get("sso_stagnant_min_hours"), getattr(_expected_settings, 'sso_stagnant_min_hours', 2.5))
        _compare_float("sso_slow_velocity_threshold", snapshot.get("sso_slow_velocity_threshold"), getattr(_expected_settings, 'sso_slow_velocity_threshold', None))
        _compare_float("sso_reentry_min_strength_diff", snapshot.get("sso_reentry_min_strength_diff"), getattr(_expected_settings, 'sso_reentry_min_strength_diff', None))
        _compare_float("max_positions_spot_bull", snapshot.get("max_positions_spot_bull"), getattr(_expected_settings, 'max_positions_spot_bull', None))
        _compare_float("max_positions_range_scalp", snapshot.get("max_positions_range_scalp"), getattr(_expected_settings, 'max_positions_range_scalp', None))
        _compare_float("max_positions_preservation", snapshot.get("max_positions_preservation"), getattr(_expected_settings, 'max_positions_preservation', None))
        _compare_float("max_positions_futures_short", snapshot.get("max_positions_futures_short"), getattr(_expected_settings, 'max_positions_futures_short', None))
        _compare_float("futures_max_new_per_cycle", snapshot.get("futures_max_new_per_cycle"), getattr(_expected_settings, 'futures_max_new_per_cycle', None))
        _compare_float("min_hold_minutes", snapshot.get("min_hold_minutes"), getattr(_expected_settings, 'min_hold_minutes', None))
        _compare_float("min_hold_minutes_spot_bull", snapshot.get("min_hold_minutes_spot_bull"), getattr(_expected_settings, 'min_hold_minutes_spot_bull', None))
        _compare_float("min_hold_minutes_range_scalp", snapshot.get("min_hold_minutes_range_scalp"), getattr(_expected_settings, 'min_hold_minutes_range_scalp', None))
        _compare_float("futures_long_sl_pct", snapshot.get("futures_long_sl_pct"), getattr(_expected_settings, 'futures_long_sl_pct', None))
        _compare_float("futures_long_tp_pct", snapshot.get("futures_long_tp_pct"), getattr(_expected_settings, 'futures_long_tp_pct', None))
        _compare_float("max_rebalance_per_cycle", snapshot.get("max_rebalance_per_cycle"), getattr(_expected_settings, 'max_rebalance_per_cycle', None))

        return {
            "ok": True,
            "drift_count": len(mismatches),
            "mismatches": mismatches,
            "snapshot_updated_at": harmony.get("updated_at"),
            "effective_runtime": effective,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "drift_count": 0, "mismatches": []}


def _get_rollout_readiness_snapshot() -> Dict[str, Any]:
    try:
        health_snapshot = _compute_health_results_snapshot(trigger="rollout_readiness")
        drift_blockers = _get_top_drift_blockers(limit=10)
        active_drift = _get_active_critical_drift_blockers()
        parity_validation = _get_parity_validation_snapshot()

        overall = str((health_snapshot or {}).get("overall") or "unknown").lower()
        harmony_drift = (health_snapshot or {}).get("harmony_drift") or {}
        drift_count = int(harmony_drift.get("drift_count") or 0)
        active_drift_count = int(active_drift.get("count") or 0)
        changepoint = (health_snapshot or {}).get("changepoint") or {}
        delta = changepoint.get("delta") or {}
        health_harmony = (health_snapshot or {}).get("harmony") or {}
        spot = (health_snapshot or {}).get("spot") or {}
        futures = (health_snapshot or {}).get("futures") or {}
        spot_reasons = list(spot.get("reasons") or [])
        futures_reasons = list(futures.get("reasons") or [])

        reasons: List[str] = []
        status = "ready"

        if drift_count > 0:
            status = "blocked"
            reasons.append(f"harmony drift={drift_count}")

        if active_drift_count > 0:
            status = "blocked"
            reasons.append(f"active critical drift={active_drift_count}")

        if overall in ("critical", "error"):
            status = "blocked"
            reasons.append(f"health overall={overall}")
        elif overall in ("warning", "inconclusive") and status != "blocked":
            status = "caution"
            reasons.append(f"health overall={overall}")

        net_delta = float(delta.get("net_pnl") or 0.0)
        trade_delta = int(delta.get("n_trades") or 0)
        if net_delta < -20:
            status = "blocked"
            reasons.append(f"changepoint net_pnl delta {net_delta:.4f}")
        elif net_delta < 0 and status == "ready":
            status = "caution"
            reasons.append(f"changepoint net_pnl delta {net_delta:.4f}")

        if trade_delta <= 0 and status == "ready":
            status = "caution"
            reasons.append("no positive post-change trade delta yet")

        parity_status = str(parity_validation.get('status') or 'unknown').lower()
        if parity_status == 'critical':
            status = 'blocked'
            reasons.append('parity validation critical')
        elif parity_status == 'warning' and status == 'ready':
            status = 'caution'
            reasons.append('parity validation warning')
        elif parity_status == 'inconclusive' and status == 'ready':
            status = 'caution'
            reasons.append('parity validation inconclusive')

        spot_status = str(spot.get("status") or "unknown").lower()
        futures_status = str(futures.get("status") or "unknown").lower()
        if spot_status in ("critical", "error") or futures_status in ("critical", "error"):
            status = "blocked"
            reasons.append(f"market health spot={spot_status} futures={futures_status}")
            if spot_reasons:
                reasons.append(f"spot: {spot_reasons[0]}")
            if futures_reasons:
                reasons.append(f"futures: {futures_reasons[0]}")

        if not reasons:
            reasons.append("health, drift, and changepoint checks are aligned")

        return {
            "ok": True,
            "status": status,
            "reasons": reasons,
            "overall_health": overall,
            "harmony_drift_count": drift_count,
            "active_critical_drift_count": active_drift_count,
            "changepoint_delta": delta,
            "spot_status": spot_status,
            "futures_status": futures_status,
            "spot_reasons": spot_reasons,
            "futures_reasons": futures_reasons,
            "active_critical_drift_blockers": (active_drift.get("blockers") or []),
            "critical_drift_blockers": (drift_blockers.get("blockers") or []),
            "critical_drift_blocker_count": int(drift_blockers.get("count") or 0),
            "parity_validation": parity_validation,
            "harmony_snapshot_updated_at": health_harmony.get("updated_at"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e), "reasons": [str(e)]}


def _get_parity_validation_snapshot() -> Dict[str, Any]:
    try:
        baseline = _resolve_rollout_baseline()
        if not baseline.get('ok'):
            return {
                'ok': False,
                'status': str(baseline.get('status') or 'unavailable'),
                'error': str(baseline.get('reason') or 'no baseline run_id resolved'),
                'baseline': baseline,
            }

        run_id = int(baseline.get('run_id'))
        effective_runtime = _get_effective_runtime_selection()
        active_mode = str(effective_runtime.get('mode') or 'SPOT_BULL').strip().upper() or 'SPOT_BULL'
        active_trade_type = _active_trade_type_for_mode(active_mode)

        compare = _build_backtest_compare_vs_live(run_id, active_trade_type=active_trade_type, active_mode=active_mode)
        active_scope = compare.get('active_scope') or {}
        parity = active_scope.get('parity') or compare.get('parity') or {}
        comparability = active_scope.get('comparability') or {}
        gap = parity.get('pnl_total_gap_pct')
        trade_gap = abs(int(parity.get('n_trades_gap') or 0))
        win_gap = float(parity.get('win_rate_gap_pct') or 0.0)

        status = 'ok'
        reasons: List[str] = []
        comparability_status = str(comparability.get('status') or 'not_comparable').lower()
        sample_status = str(comparability.get('sample_status') or 'none').lower()

        coverage_ratio = comparability.get('coverage_ratio')
        is_under_sampled = coverage_ratio is not None and float(coverage_ratio) < 0.05

        if comparability_status in ('not_comparable', 'limited'):
            if comparability_status == 'limited' and is_under_sampled:
                status = 'info'
            else:
                status = 'warning'
            reasons.extend([str(r) for r in (comparability.get('reasons') or []) if r])
            if gap is not None and not (comparability_status == 'limited' and is_under_sampled):
                reasons.append(f'active pnl gap observed {gap:.2f}% but comparison is not fully homologable')
        else:
            if gap is None:
                status = 'warning'
                reasons.append(f'active {active_trade_type} live pnl is zero or insufficient for parity gap')
            else:
                abs_gap = abs(float(gap))
                if abs_gap >= 100.0:
                    status = 'critical'
                    reasons.append(f'active {active_trade_type} pnl parity gap {gap:.2f}%')
                elif abs_gap >= 25.0:
                    status = 'warning'
                    reasons.append(f'active {active_trade_type} pnl parity gap {gap:.2f}%')

            if abs(win_gap) >= 20.0:
                status = 'critical' if status != 'critical' else status
                reasons.append(f'active {active_trade_type} win-rate gap {win_gap:.2f}pp')
            elif abs(win_gap) >= 10.0 and status == 'ok':
                status = 'warning'
                reasons.append(f'active {active_trade_type} win-rate gap {win_gap:.2f}pp')

        if trade_gap > 20:
            if status == 'ok':
                status = 'warning'
            reasons.append(f'active {active_trade_type} trade count gap {trade_gap}')

        if not reasons:
            reasons.append(f'parity within current tolerance for active {active_trade_type}')

        deduped_reasons: List[str] = []
        for reason in reasons:
            if reason not in deduped_reasons:
                deduped_reasons.append(reason)

        return {
            'ok': True,
            'status': status,
            'run_id': run_id,
            'active_market': active_trade_type,
            'active_mode': active_mode,
            'comparability_status': comparability_status,
            'sample_status': sample_status,
            'reasons': deduped_reasons,
            'parity': parity,
            'comparability': comparability,
            'baseline': baseline,
            'compare': compare,
        }
    except HTTPException as e:
        return {"ok": False, "status": "error", "error": str(e.detail)}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)}


def _get_top_drift_blockers(limit: int = 10) -> Dict[str, Any]:
    try:
        _limit = max(1, min(int(limit), 50))
        rows = db_client.execute_query(
            "SELECT id, detected_at, severity, param, expected, actual, source "
            "FROM config_drift_alerts WHERE acknowledged=FALSE AND severity='critical' "
            "ORDER BY detected_at DESC LIMIT %s",
            (_limit,),
        )
        blockers = []
        for row in rows or []:
            blockers.append({
                "id": row.get("id"),
                "detected_at": row.get("detected_at").isoformat() if row.get("detected_at") else None,
                "severity": row.get("severity"),
                "param": row.get("param"),
                "expected": row.get("expected"),
                "actual": row.get("actual"),
                "source": row.get("source"),
            })
        return {"ok": True, "count": len(blockers), "blockers": blockers}
    except Exception as e:
        return {"ok": False, "count": 0, "blockers": [], "error": str(e)}


def _get_promotion_criteria_snapshot() -> Dict[str, Any]:
    try:
        rollout = _get_rollout_readiness_snapshot()
        changepoint = _get_performance_changepoint_metrics()
        parity = _get_parity_validation_snapshot()

        current_window = (changepoint.get('current_window') or {}) if changepoint.get('ok') else {}
        current_summary = (current_window.get('summary') or {}) if isinstance(current_window, dict) else {}
        current_trades = int(current_summary.get('n_trades') or 0)
        current_net = float(current_summary.get('net_pnl') or 0.0)

        sample_target = 8
        sample_status = 'sufficient' if current_trades >= sample_target else ('limited' if current_trades >= 3 else 'insufficient')

        status = 'hold'
        reasons: List[str] = []

        rollout_status = str(rollout.get('status') or 'error').lower()
        parity_status = str(parity.get('status') or 'error').lower()
        active_drift = int(rollout.get('active_critical_drift_count') or 0)

        if active_drift > 0:
            reasons.append(f'active critical drift={active_drift}')
        if rollout_status in ('blocked', 'error'):
            reasons.append(f'rollout status={rollout_status}')
        if parity_status in ('critical', 'error'):
            reasons.append(f'parity status={parity_status}')
        if sample_status != 'sufficient':
            reasons.append(f'sample {sample_status} ({current_trades}/{sample_target} trades)')
        if current_net < 0:
            reasons.append(f'current net pnl {current_net:.4f}')

        if not reasons:
            status = 'promote'
            reasons.append('sample, parity, drift, and rollout checks passed')
        elif rollout_status in ('caution',) and parity_status in ('warning', 'inconclusive', 'ok') and sample_status != 'insufficient' and active_drift == 0:
            status = 'observe'
        else:
            status = 'hold'

        return {
            'ok': True,
            'status': status,
            'sample': {
                'target_trades': sample_target,
                'current_trades': current_trades,
                'status': sample_status,
                'current_net_pnl': round(current_net, 4),
            },
            'rollout_status': rollout_status,
            'parity_status': parity_status,
            'active_critical_drift_count': active_drift,
            'reasons': reasons,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {'ok': False, 'status': 'error', 'reasons': [str(e)], 'error': str(e)}


@app.get("/metrics/performance-changepoint")
async def get_performance_changepoint(_auth=Depends(_require_api_key)) -> Dict:
    """Return before/after performance around the Smart Backtest changepoint, split by SPOT and FUTURES."""
    return _get_performance_changepoint_metrics()


@app.get("/metrics/trade-diagnostics")
async def get_trade_diagnostics(hours: int = 168, _auth=Depends(_require_api_key)) -> Dict:
    """Return close-reason, pnl, fees and hold-time diagnostics split by SPOT and FUTURES."""
    if hours <= 0:
        raise HTTPException(status_code=400, detail="hours must be > 0")
    start_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return _trade_diagnostics_range(start_ts, datetime.now(timezone.utc))


@app.get("/metrics/trade-throughput-exposure")
async def get_trade_throughput_exposure(
    hours: int = 168,
    cycle_minutes: int = 5,
    _auth=Depends(_require_api_key),
) -> Dict:
    """Return throughput and capital-exposure diagnostics by cycle, split by SPOT and FUTURES."""
    if hours <= 0:
        raise HTTPException(status_code=400, detail="hours must be > 0")
    if cycle_minutes <= 0:
        raise HTTPException(status_code=400, detail="cycle_minutes must be > 0")
    start_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return _trade_throughput_exposure_range(start_ts, datetime.now(timezone.utc), cycle_minutes=cycle_minutes)


@app.get("/metrics/profit-realization")
async def get_profit_realization_metrics(_auth=Depends(_require_api_key)) -> Dict:
    """Return live profit-realization health across SPOT and FUTURES."""
    try:
        from init_components import get_components
        from profit_realization_supervisor import PROFIT_START_KEY, parse_dt as _parse_profit_dt
        comp = get_components()
        _spot_pm = getattr(comp, 'position_manager', None)
        _spot_client = getattr(comp, 'binance_client', None)

        _now = datetime.now(timezone.utc)
        _spot_locked = 0
        _spot_unlocked = 0
        _spot_hold_minutes: List[int] = []
        _spot_giveback = 0.0
        _spot_peak = 0.0
        _spot_positive = 0

        if _spot_pm and _spot_client:
            for _symbol, _pos_list in (_spot_pm.get_all_positions() or {}).items():
                if not _pos_list:
                    continue
                try:
                    _price = float(_spot_client.get_symbol_price(_symbol))
                except Exception:
                    continue
                for _pos in _pos_list:
                    try:
                        _entry = float(getattr(_pos, 'entry_price', 0) or 0)
                        if _entry <= 0:
                            continue
                        _pnl_pct = (_price - _entry) / _entry
                        if _pnl_pct <= 0:
                            continue
                        _spot_positive += 1
                        _spot_meta = dict(getattr(_pos, 'metadata', {}) or {})
                        _locked = float(getattr(_pos, 'stop_loss', 0) or 0) >= _entry or float(_spot_meta.get('trailing_peak', 0) or 0) > _entry
                        if _locked:
                            _spot_locked += 1
                        else:
                            _spot_unlocked += 1
                            _spot_ref = _parse_profit_dt(_spot_meta.get(PROFIT_START_KEY)) or getattr(_pos, 'entry_time', None) or getattr(_pos, 'created_at', None)
                            if _spot_ref:
                                _spot_hold_minutes.append(max(0, int((_now - _spot_ref).total_seconds() // 60)))
                        _spot_peak_pct = max(0.0, (float(_spot_meta.get('trailing_peak', _entry) or _entry) - _entry) / _entry)
                        _spot_peak += _spot_peak_pct
                        _spot_giveback += max(0.0, _spot_peak_pct - _pnl_pct)
                    except Exception:
                        continue

        _futures_open = []
        try:
            from run_trades import _futures_client as _fc_ref
            if _fc_ref:
                _futures_open = [p for p in _fc_ref._open_positions.values() if not p.closed]
        except Exception:
            _futures_open = []

        _fut_locked = 0
        _fut_unlocked = 0
        _fut_hold_minutes: List[int] = []
        _fut_giveback = 0.0
        _fut_peak = 0.0
        _fut_positive = 0
        for _pos in _futures_open:
            try:
                _fc_price = float(_fc_ref.get_price(_pos.symbol)) if _fc_ref else 0.0
                _entry = float(getattr(_pos, 'entry_price', 0) or 0)
                _qty = float(getattr(_pos, 'qty', 0) or 0)
                if _entry <= 0 or _qty <= 0:
                    continue
                _pnl_pct = _pos.unrealized_pnl(_fc_price) / (_entry * _qty)
                if _pnl_pct <= 0:
                    continue
                _fut_positive += 1
                _meta = dict(getattr(_pos, '_smart_meta', {}) or {})
                _locked = bool(_meta.get('breakeven_done')) or bool(_meta.get('trail_best_price'))
                if _locked:
                    _fut_locked += 1
                else:
                    _fut_unlocked += 1
                    _fut_ref = _parse_profit_dt(_meta.get(PROFIT_START_KEY)) or getattr(_pos, 'opened_at', None)
                    if _fut_ref:
                        _fut_hold_minutes.append(max(0, int((_now - _fut_ref).total_seconds() // 60)))
                _fut_peak_pct = max(0.0, float(_meta.get('peak_pnl_pct', 0.0) or 0.0))
                _fut_peak += _fut_peak_pct
                _fut_giveback += max(0.0, _fut_peak_pct - _pnl_pct)
            except Exception:
                continue

        _total_positive = _spot_positive + _fut_positive
        _total_unlocked = _spot_unlocked + _fut_unlocked
        _total_locked = _spot_locked + _fut_locked
        _avg_minutes = 0.0
        _minutes_pool = _spot_hold_minutes + _fut_hold_minutes
        if _minutes_pool:
            _avg_minutes = round(sum(_minutes_pool) / len(_minutes_pool), 1)
        _giveback_ratio = round((_spot_giveback + _fut_giveback) / (_spot_peak + _fut_peak), 4) if (_spot_peak + _fut_peak) > 0 else 0.0

        _supervisor_24h = 0
        for _ts_col in ("ts", "created_at"):
            try:
                _audit_rows = db_client.execute_query(
                    f"SELECT COUNT(*) AS n FROM trades_audit WHERE event LIKE 'profit_realization_supervisor%%' AND {_ts_col} >= NOW() - INTERVAL '24 hours'"
                )
                _supervisor_24h = int((_audit_rows[0].get('n') if _audit_rows else 0) or 0)
                break
            except Exception:
                continue

        return {
            "ok": True,
            "spot": {
                "positive_positions": _spot_positive,
                "positive_unlocked_positions": _spot_unlocked,
                "positive_locked_positions": _spot_locked,
                "avg_minutes_in_profit_without_realization": round(sum(_spot_hold_minutes) / len(_spot_hold_minutes), 1) if _spot_hold_minutes else 0.0,
            },
            "futures": {
                "positive_positions": _fut_positive,
                "positive_unlocked_positions": _fut_unlocked,
                "positive_locked_positions": _fut_locked,
                "avg_minutes_in_profit_without_realization": round(sum(_fut_hold_minutes) / len(_fut_hold_minutes), 1) if _fut_hold_minutes else 0.0,
            },
            "totals": {
                "positive_positions": _total_positive,
                "positive_unlocked_positions": _total_unlocked,
                "positive_locked_positions": _total_locked,
                "avg_minutes_in_profit_without_realization": _avg_minutes,
                "giveback_ratio_live": _giveback_ratio,
                "supervisor_audit_events_24h": _supervisor_24h,
            },
            "timestamp": _now.isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/metrics/unified-governance")
async def get_unified_governance_metrics(_auth=Depends(_require_api_key)) -> Dict:
    """Return live governance state across SPOT/FUTURES from metadata and audit trail."""
    try:
        from init_components import get_components
        from unified_governance_supervisor import LOSS_STATE_KEY, LAST_DECISION_KEY

        comp = get_components()
        _spot_pm = getattr(comp, 'position_manager', None)

        _now = datetime.now(timezone.utc)
        _actions = {"hold": 0, "watchdog": 0, "escalate": 0, "close": 0}
        _loss_positions = 0
        _watchdog_levels: List[int] = []

        if _spot_pm:
            for _symbol, _positions in (_spot_pm.get_all_positions() or {}).items():
                for _pos in (_positions or []):
                    _meta = dict(getattr(_pos, 'metadata', {}) or {})
                    _last = _meta.get(LAST_DECISION_KEY) or {}
                    _loss = _meta.get(LOSS_STATE_KEY) or {}
                    _action = str(_last.get('action') or '').strip().lower()
                    if _action in _actions:
                        _actions[_action] += 1
                    if _loss:
                        _loss_positions += 1
                        _watchdog_levels.append(int(_loss.get('watchdog_level', 0) or 0))

        _futures_positions = []
        try:
            from run_trades import _futures_client as _fc_ref
            if _fc_ref:
                _futures_positions = [p for p in _fc_ref._open_positions.values() if not p.closed]
        except Exception:
            _futures_positions = []

        for _pos in _futures_positions:
            _meta = dict(getattr(_pos, '_smart_meta', {}) or {})
            _last = _meta.get(LAST_DECISION_KEY) or {}
            _loss = _meta.get(LOSS_STATE_KEY) or {}
            _action = str(_last.get('action') or '').strip().lower()
            if _action in _actions:
                _actions[_action] += 1
            if _loss:
                _loss_positions += 1
                _watchdog_levels.append(int(_loss.get('watchdog_level', 0) or 0))

        _audit_n = 0
        for _ts_col in ("created_at", "ts"):
            try:
                _audit_24h = db_client.execute_query(
                    f"SELECT COUNT(*) AS n FROM trades_audit WHERE event='unified_governance_decision' AND {_ts_col} >= NOW() - INTERVAL '24 hours'"
                )
                _audit_n = int((_audit_24h[0].get('n') if _audit_24h else 0) or 0)
                break
            except Exception:
                continue

        return {
            "ok": True,
            "actions_open_positions": _actions,
            "loss_state_positions": _loss_positions,
            "avg_watchdog_level": round(sum(_watchdog_levels) / len(_watchdog_levels), 3) if _watchdog_levels else 0.0,
            "audit_events_24h": _audit_n,
            "timestamp": _now.isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/metrics/rollout-readiness")
async def get_rollout_readiness(_auth=Depends(_require_api_key)) -> Dict:
    """Return a single operational verdict for rollout based on health, drift, and changepoint."""
    return _get_rollout_readiness_snapshot()


@app.get("/metrics/transition-health")
async def get_transition_health(_auth=Depends(_require_api_key)) -> Dict:
    """Return lightweight transition-health KPIs from runtime state and orchestrator memory."""
    try:
        _rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='regime_transition_state' LIMIT 1"
        )
        _payload = {}
        _updated_at = None
        if _rows:
            _payload = _rows[0].get('value') or {}
            if isinstance(_payload, str):
                try:
                    _payload = json.loads(_payload)
                except Exception:
                    _payload = {"raw": _payload}
            _updated_at = _rows[0].get('updated_at')
        _pre = _payload.get('pre_switch') or {}
        return {
            "ok": True,
            "transition_active": bool(_payload.get('active')),
            "from_mode": _pre.get('from_mode'),
            "to_mode": _pre.get('to_mode'),
            "confidence": _pre.get('confidence'),
            "block_new_shorts": bool(_pre.get('block_new_shorts')),
            "cycle_failures": getattr(orchestrator, 'consecutive_cycle_failures', 0),
            "last_cycle_error": getattr(orchestrator, 'last_cycle_error', None),
            "updated_at": _updated_at.isoformat() if _updated_at else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/metrics/binance-truth")
async def get_binance_truth_metrics(_auth=Depends(_require_api_key)) -> Dict:
    """Return Binance parity state (futures + spot) used by runtime gates."""
    try:
        status = get_truth_status(db_client)
        status = status if isinstance(status, dict) else {"raw": status}
        status["ok"] = bool(status.get("ok", str(status.get("severity", "")).lower() in ("ok", "unknown")))
        status_spot = get_truth_status_spot(db_client)
        status_spot = status_spot if isinstance(status_spot, dict) else {"raw": status_spot}
        status_spot["ok"] = bool(status_spot.get("ok", str(status_spot.get("severity", "")).lower() in ("ok", "unknown")))
        status["spot"] = status_spot
        status["timestamp"] = datetime.now(timezone.utc).isoformat()
        return status
    except Exception as e:
        return {"ok": False, "severity": "error", "error": str(e)}


@app.get("/metrics/transition-kpis")
async def get_transition_kpis(hours: int = 168, _auth=Depends(_require_api_key)) -> Dict:
    """Return transition-quality KPIs from closed trades metadata.

    KPIs include switch-loss, regime/profile mismatches, hold-time lag, and close-reason mix.
    """
    if hours <= 0:
        raise HTTPException(status_code=400, detail="hours must be > 0")
    try:
        _rows = db_client.execute_query(
            """
            SELECT pnl, close_reason, entry_time, exit_time, metadata
            FROM trades_closed
            WHERE exit_time > NOW() - (%s || ' hours')::interval
            ORDER BY exit_time DESC
            """,
            (int(hours),),
        )
        _total = len(_rows or [])
        _switch_trades = 0
        _switch_loss_sum = 0.0
        _cross_regime = 0
        _cross_profile = 0
        _lag_minutes = []
        _reason_mix: Dict[str, int] = {}

        for _r in _rows or []:
            _pnl = float(_r.get('pnl') or 0.0)
            _reason = str(_r.get('close_reason') or 'unknown')
            _reason_mix[_reason] = _reason_mix.get(_reason, 0) + 1

            _meta = _r.get('metadata') or {}
            if isinstance(_meta, str):
                try:
                    _meta = json.loads(_meta)
                except Exception:
                    _meta = {}
            if not isinstance(_meta, dict):
                _meta = {}

            _entry_reg = str(_meta.get('regime_at_entry') or '').strip().upper()
            _exit_reg = str(_meta.get('regime_at_exit') or '').strip().upper()
            _entry_prof = str(_meta.get('profile_at_entry') or '').strip().upper()
            _exit_prof = str(_meta.get('profile_at_exit') or '').strip().upper()
            _switch_id = str(_meta.get('switch_id') or '').strip()

            if _entry_reg and _exit_reg and _entry_reg != _exit_reg:
                _cross_regime += 1
            if _entry_prof and _exit_prof and _entry_prof != _exit_prof:
                _cross_profile += 1

            if _switch_id:
                _switch_trades += 1
                if _pnl < 0:
                    _switch_loss_sum += _pnl

            try:
                _et = _r.get('entry_time')
                _xt = _r.get('exit_time')
                if isinstance(_et, str):
                    _et = datetime.fromisoformat(_et.replace('Z', '+00:00'))
                if isinstance(_xt, str):
                    _xt = datetime.fromisoformat(_xt.replace('Z', '+00:00'))
                if _et is not None and _xt is not None:
                    if getattr(_et, 'tzinfo', None) is None:
                        _et = _et.replace(tzinfo=timezone.utc)
                    if getattr(_xt, 'tzinfo', None) is None:
                        _xt = _xt.replace(tzinfo=timezone.utc)
                    _mins = (_xt - _et).total_seconds() / 60.0
                    if _mins >= 0:
                        _lag_minutes.append(_mins)
            except Exception:
                pass

        _sorted_reason_mix = sorted(_reason_mix.items(), key=lambda kv: kv[1], reverse=True)
        _avg_lag = (sum(_lag_minutes) / len(_lag_minutes)) if _lag_minutes else 0.0
        _p95_lag = 0.0
        if _lag_minutes:
            _s = sorted(_lag_minutes)
            _idx = int(max(0, min(len(_s) - 1, round(0.95 * (len(_s) - 1)))))
            _p95_lag = _s[_idx]

        return {
            "ok": True,
            "hours": int(hours),
            "sample": {
                "closed_trades": _total,
                "switch_tagged_trades": _switch_trades,
            },
            "kpis": {
                "switch_loss_total_usdt": round(_switch_loss_sum, 4),
                "switch_loss_avg_usdt": round((_switch_loss_sum / _switch_trades), 4) if _switch_trades else 0.0,
                "cross_regime_count": _cross_regime,
                "cross_profile_count": _cross_profile,
                "regime_mismatch_rate": round((_cross_regime / _total), 4) if _total else 0.0,
                "profile_mismatch_rate": round((_cross_profile / _total), 4) if _total else 0.0,
                "avg_hold_minutes": round(_avg_lag, 2),
                "p95_hold_minutes": round(_p95_lag, 2),
            },
            "close_reason_mix": [
                {"reason": _k, "count": int(_v), "share": round((_v / _total), 4) if _total else 0.0}
                for _k, _v in _sorted_reason_mix
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "hours": int(hours)}


@app.get("/metrics/spot-bull-root-cause")
async def get_spot_bull_root_cause(
    hours: int = 168,
    cycle_minutes: int = 5,
    _auth=Depends(_require_api_key),
) -> Dict:
    """Quantify relative contribution of entry-quality, exit-policy and sizing fragmentation for SPOT and FUTURES."""
    if hours <= 0:
        raise HTTPException(status_code=400, detail="hours must be > 0")
    if cycle_minutes <= 0:
        raise HTTPException(status_code=400, detail="cycle_minutes must be > 0")
    return _quantify_spot_bull_root_causes(hours=hours, cycle_minutes=cycle_minutes)


@app.get("/tuning/candidates/spot-bull-v1")
async def get_spot_bull_candidate_v1(_auth=Depends(_require_api_key)) -> Dict:
    """Return the first SPOT_BULL candidate package with explicit FUTURES mirror and current-vs-candidate deltas."""
    payload = _build_spot_bull_candidate_snapshot()
    latest_backup = None
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='config_snapshot_futures_working_latest' LIMIT 1"
        )
        if rows:
            latest_backup = rows[0]
    except Exception:
        latest_backup = None
    return {
        "ok": True,
        "candidate": payload,
        "latest_futures_backup": latest_backup,
    }


@app.post("/tuning/candidates/spot-bull-v1/validate-start")
async def start_spot_bull_candidate_validation(
    background_tasks: BackgroundTasks,
    body: Dict,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Start Smart Backtest validation run for SPOT_BULL candidate v1 using explicit SPOT+FUTURES mirror params."""
    candidate = _build_spot_bull_candidate_snapshot()
    validation_body = {
        "period": body.get("period", "1y"),
        "simulations": int(body.get("simulations", 0) or 0),
        "auto_recommend_sims": bool(body.get("auto_recommend_sims", True)),
        "strict_data": bool(body.get("strict_data", False)),
    }
    if body.get("capital") is not None:
        validation_body["capital"] = body.get("capital")

    validation_context = {
        "candidate_label": candidate.get("candidate_label"),
        "candidate_params": candidate.get("candidate_params"),
        "mirror": candidate.get("futures_mirror_changes"),
        "notes": body.get("notes") or "spot_bull candidate validation",
        "baseline_run_ids": dict(_SMART_BASELINE_RUN_IDS),
    }
    return _start_smart_backtest_task(background_tasks, validation_body, validation_context=validation_context)


@app.get("/tuning/candidates/spot-bull-v1/decision/{root_session_id}")
async def get_spot_bull_candidate_decision(
    root_session_id: str,
    min_pnl_delta_pct: float = -3.0,
    min_win_rate_delta_pct: float = -5.0,
    max_drawdown_delta_pct: float = 15.0,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Return PASS/FAIL/INCONCLUSIVE go-no-go decision for SPOT_BULL candidate validation run."""
    validation = _evaluate_smart_backtest_validation(
        root_session_id=root_session_id,
        min_pnl_delta_pct=min_pnl_delta_pct,
        min_win_rate_delta_pct=min_win_rate_delta_pct,
        max_drawdown_delta_pct=max_drawdown_delta_pct,
    )
    overall = str(validation.get("overall") or "inconclusive")
    go_no_go = "NO_GO"
    if overall == "pass":
        go_no_go = "GO"
    elif overall == "inconclusive":
        go_no_go = "CONDITIONAL"
    return {
        "ok": True,
        "candidate_label": _SPOT_BULL_CANDIDATE_V1.get("candidate_label"),
        "root_session_id": root_session_id,
        "go_no_go": go_no_go,
        "validation": validation,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/positions/open/detail")
async def get_open_positions_detail() -> Dict:
    """Get open positions with full pnl_prediction detail."""
    try:
        positions = position_manager.get_all_positions()
        positions_data = []
        price_cache: Dict[str, float] = {}
        for symbol, position in positions.items():
            try:
                current_price = _get_request_price(price_cache, symbol)
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
        _runtime_ctx = _get_dashboard_context_reference()
        if not getattr(_s, 'futures_enabled', True):
            return {"enabled": False, "positions": [], "count": 0}
        if bool(getattr(_s, 'dry_run', False)):
            return {
                "enabled": True,
                "positions": [],
                "count": 0,
                "source": "dry_run",
            }
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

        # Paso 5: Pre-load DB escalamiento entries per symbol for enrichment
        _db_entries_map: dict = {}
        try:
            from init_components import get_components as _gc
            _comps = _gc()
            if _comps.db_client:
                _db_esc_rows = _comps.db_client.execute_query(
                    "SELECT symbol, quantity, price, entry_time, metadata "
                    "FROM trades_open WHERE metadata->>'trade_type' = 'futures' "
                    "ORDER BY symbol, entry_time ASC"
                )
                import json as _j
                from datetime import timezone as _tz
                _now_esc = datetime.utcnow().replace(tzinfo=_tz.utc)
                for _er in _db_esc_rows:
                    _emeta = _er.get('metadata') or {}
                    if isinstance(_emeta, str):
                        try: _emeta = _j.loads(_emeta)
                        except: _emeta = {}
                    _et = _er.get('entry_time')
                    _age_h = 0.0
                    if _et:
                        _et_aware = _et if hasattr(_et, 'tzinfo') and _et.tzinfo else _et.replace(tzinfo=_tz.utc) if hasattr(_et, 'replace') else None
                        if _et_aware:
                            _age_h = round((_now_esc - _et_aware).total_seconds() / 3600, 2)
                    _db_entries_map.setdefault(_er['symbol'], []).append({
                        "entry_price": float(_er['price']),
                        "qty": float(_er['quantity']),
                        "age_h": _age_h,
                        "escalonamiento_index": _emeta.get('escalonamiento_index', 1),
                        "es_escalonamiento": _emeta.get('es_escalonamiento', False),
                    })
        except Exception as _dbe:
            pass

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

            # VIS-4b: SL/TP lifecycle from _smart_meta (FIX-S3: infer from SL when meta missing)
            _vis_be = False
            _vis_trail_active = False
            _vis_trail_best = None
            _vis_tp_ext = 0
            _vis_dyn_trail = None
            _pnl_pct_vis = upnl / (entry * qty) if entry > 0 and qty > 0 else 0.0
            if _tpos and not _tpos.closed:
                _vis_be = _tpos._smart_meta.get('breakeven_done', False)
                _vis_trail_active = 'trail_best_price' in _tpos._smart_meta
                _vis_trail_best = _tpos._smart_meta.get('trail_best_price')
                _vis_tp_ext = _tpos._smart_meta.get('tp_extensions_count', 0)
                # FIX-S3: Infer trailing/breakeven from SL position when _smart_meta is empty
                if not _vis_be:
                    if side == "SHORT" and sl_price <= entry:
                        _vis_be = True
                    elif side == "LONG" and sl_price >= entry:
                        _vis_be = True
                if not _vis_trail_active and _pnl_pct_vis >= 0.015:
                    _vis_trail_active = True
                    if not _vis_trail_best:
                        _vis_trail_best = cur_price  # approximate current as best
                if _pnl_pct_vis >= 0.05:
                    _vis_dyn_trail = 0.7
                elif _pnl_pct_vis >= 0.03:
                    _vis_dyn_trail = 1.0
                else:
                    _vis_dyn_trail = 1.5

            _sym_entries = _db_entries_map.get(sym, [])
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
                "breakeven_done":  _vis_be,
                "trailing_active": _vis_trail_active,
                "trail_best_price": _vis_trail_best,
                "tp_extensions":   _vis_tp_ext,
                "dynamic_trail_dist_pct": _vis_dyn_trail,
                "escalamientos":   len(_sym_entries),
                "entries":         _sym_entries,
                **_normalize_transaction_context(getattr(_tpos, '_smart_meta', {}) if _tpos else {}, _runtime_ctx),
            })

        _ctx_counts = {
            "legacy": sum(1 for p in positions if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in positions if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in positions if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in positions if p.get('config_context_state') == 'unknown'),
        }
        return {"enabled": True, "positions": positions, "count": len(positions),
                "legacy_count": _ctx_counts["legacy"],
                "partial_context_count": _ctx_counts["partial"],
                "current_context_count": _ctx_counts["current"],
                "unknown_context_count": _ctx_counts["unknown"],
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"enabled": False, "positions": [], "count": 0, "error": str(e)}


@app.get("/price-history/{symbol}")
async def get_price_history(symbol: str, start_time: str, end_time: str):
    """
    Get historical price data for a symbol between start_time and end_time.
    Times should be ISO format strings or Unix timestamps in milliseconds.
    Returns klines data for charting.
    """
    try:
        # Parse timestamps
        if start_time.isdigit():
            start_ms = int(start_time)
        else:
            start_ms = int(datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp() * 1000)
        
        if end_time.isdigit():
            end_ms = int(end_time)
        else:
            end_ms = int(datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp() * 1000)
        
        # Get klines data directly from Binance API (public endpoint)
        import aiohttp
        import asyncio
        
        # Calculate number of intervals needed (max 1000 klines per request)
        duration_ms = end_ms - start_ms
        interval_ms = 5 * 60 * 1000  # 5 minutes
        num_intervals = min(1000, max(1, duration_ms // interval_ms))
        
        # Build URL for Binance public API
        base_url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": "5m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": num_intervals
        }
        
        # Fetch klines data
        _klines_timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=_klines_timeout) as session:
            async with session.get(base_url, params=params) as response:
                if response.status != 200:
                    return {"error": f"Binance API error: {response.status}", "symbol": symbol, "data": []}
                klines = await response.json()
        
        if not klines:
            return {"symbol": symbol, "data": [], "interval": "5m"}
        
        # Format data for frontend
        formatted_data = []
        for k in klines:
            formatted_data.append({
                "timestamp": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5])
            })
        
        return {
            "symbol": symbol,
            "interval": "5m",
            "data": formatted_data,
            "start_time": start_ms,
            "end_time": end_ms
        }
        
    except Exception as e:
        print(f"Error getting price history for {symbol}: {e}")
        return {"error": str(e), "symbol": symbol, "data": []}

@app.get("/futures/closed")
async def get_futures_closed_positions(limit: int = 20, days: int = 7) -> Dict:
    """Return closed futures positions queried live from Binance userTrades (last N days).
    Survives container restarts — does not depend on in-memory tracking.
    """
    try:
        from run_trades import _get_futures_client
        from config import get_settings as _get_s
        _s = _get_s()
        _runtime_ctx = _get_dashboard_context_reference()
        if not getattr(_s, 'futures_enabled', True):
            return {"enabled": False, "positions": [], "count": 0}
        if bool(getattr(_s, 'dry_run', False)):
            return {
                "enabled": True,
                "trades": [],
                "count": 0,
                "source": "dry_run",
            }

        _fc = _get_futures_client(_s)

        # Build symbol list from config to query even after restart (empty tracking)
        _extra = list(_cfg_param(_s, 'futures_symbols'))
        try:
            from pair_screener import get_screened_pairs
            _regime_mode = str(getattr(_s, 'current_regime_mode', 'SPOT_BULL')).strip().upper()
            _profile = _get_active_profile_summary(_regime_mode).get("label")
            _extra = list(dict.fromkeys(
                _extra + get_screened_pairs(
                    settings=_s,
                    regime_mode=_regime_mode,
                    profile_label=_profile,
                    db_client=db_client,
                ).get("futures", [])
            ))
        except Exception:
            pass

        raw = _fc.get_recent_trades(days=days, limit=limit, extra_symbols=_extra)

        # FIX-L2: build DB enrichment map from trades_closed for futures
        _db_closed_map: dict = {}
        try:
            from datetime import datetime as _dt3, timedelta as _td3
            _since_str = (_dt3.utcnow() - _td3(days=days)).isoformat()
            _db_rows = db_client.execute_query(
                """SELECT symbol, exit_price, entry_time, exit_time,
                          close_reason, pnl_prediction, prediction_accuracy, pnl, metadata
                   FROM trades_closed
                   WHERE metadata->>'trade_type' = 'futures'
                     AND exit_time >= %s
                   ORDER BY exit_time DESC""",
                (_since_str,)
            ) if db_client else []
            for _r in _db_rows:
                _key = _r['symbol']
                if _key not in _db_closed_map:
                    _db_closed_map[_key] = _r
        except Exception as _db2e:
            print(f"[FuturesClosed/DB] enrichment query failed: {_db2e}")

        closed = []
        for t in raw:
            closed_at_raw = t.get("closed_at")
            opened_at_raw = t.get("time") or t.get("timestamp")
            if not opened_at_raw and closed_at_raw:
                from datetime import datetime as _dt4, timedelta as _td4
                if isinstance(closed_at_raw, str):
                    try:
                        closed_dt = _dt4.fromisoformat(closed_at_raw.replace('Z', '+00:00'))
                        opened_dt = closed_dt - _td4(hours=6)
                        opened_at_raw = opened_dt.isoformat()
                    except Exception:
                        opened_at_raw = None
                else:
                    opened_at_raw = None

            # FIX-L2: enrich with DB data
            _sym = t["symbol"]
            _db_e = _db_closed_map.get(_sym, {})
            _pnl_pred = _db_e.get('pnl_prediction') if _db_e else None
            if isinstance(_pnl_pred, str):
                try:
                    import json as _j2
                    _pnl_pred = _j2.loads(_pnl_pred)
                except Exception:
                    _pnl_pred = None
            _pred_acc = float(_db_e['prediction_accuracy']) if _db_e and _db_e.get('prediction_accuracy') else None
            _close_reason = _db_e.get('close_reason') or t.get('close_reason') or ''
            _entry_time_raw = _db_e.get('entry_time') or opened_at_raw
            _exit_time_raw  = _db_e.get('exit_time') or closed_at_raw

            _db_meta = _db_e.get('metadata') or {} if _db_e else {}
            if isinstance(_db_meta, str):
                try:
                    import json as _jm2
                    _db_meta = _jm2.loads(_db_meta)
                except Exception:
                    _db_meta = {}
            closed.append({
                "symbol":              _sym,
                "side":                t["side"],
                "qty":                 t["qty"],
                "entry_price":         t.get("entry_price"),
                "exit_price":          t.get("exit_price") or (_db_e.get('exit_price') if _db_e else None),
                "sl_price":            t.get("sl_price"),
                "tp_price":            t.get("tp_price"),
                "pnl":                 t.get("pnl") or (_db_e.get('pnl') if _db_e else None),
                "opened_at":           to_mexico_time_str(_entry_time_raw) if _entry_time_raw else None,
                "closed_at":           to_mexico_time_str(_exit_time_raw) if _exit_time_raw else None,
                "close_reason":        _close_reason,
                "pnl_prediction":      _pnl_pred,
                "prediction_accuracy": _pred_acc,
                "trade_type":          "futures",
                "metadata":            _db_meta,
                "bounce_entry":        bool((_db_meta.get('smart_meta') or {}).get('bounce_entry', False)),
                **_normalize_transaction_context(_db_meta, _runtime_ctx),
            })

        _ctx_counts = {
            "legacy": sum(1 for p in closed if p.get('config_context_state') == 'legacy'),
            "partial": sum(1 for p in closed if p.get('config_context_state') == 'partial'),
            "current": sum(1 for p in closed if p.get('config_context_state') == 'current'),
            "unknown": sum(1 for p in closed if p.get('config_context_state') == 'unknown'),
        }

        return {
            "enabled": True,
            "positions": closed[:limit],
            "count": len(closed[:limit]),
            "legacy_count": _ctx_counts["legacy"],
            "partial_context_count": _ctx_counts["partial"],
            "current_context_count": _ctx_counts["current"],
            "unknown_context_count": _ctx_counts["unknown"],
            "period_days": days,
            "timestamp": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        return {"enabled": False, "positions": [], "count": 0, "error": str(e)}


# ── Backtest endpoints ────────────────────────────────────────────────────────

_backtest_tasks: Dict[str, Dict] = {}  # run_id -> status dict
_smart_backtest_tasks: Dict[str, Dict] = {}  # run_id -> status dict
_SMART_BASELINE_RUN_IDS: Dict[str, int] = {
    "spot_bull": 29600,
    "range_scalp": 48412,
    "preservation": 51631,
    "futures_short": 61556,
}


def _append_recent_output(task: Dict, line: str, limit: int = 120) -> None:
    """Store a bounded tail of process output lines for dashboard progress visibility."""
    if not isinstance(task, dict):
        return
    buf = task.setdefault("recent_output", [])
    if not isinstance(buf, list):
        buf = []
        task["recent_output"] = buf
    buf.append((line or "").rstrip())
    if len(buf) > limit:
        del buf[:-limit]


def _parse_optimizer_progress(line: str) -> Optional[Dict[str, Any]]:
    """Parse canonical optimizer progress lines like: '100/1000 | elapsed=12.3s | best_pnl=+45.2'."""
    import re

    m = re.search(r"(\d+)\/(\d+)\s+\|\s+elapsed=([0-9.]+)s\s+\|\s+best_pnl=([+\-]?[0-9.]+)", line)
    if not m:
        return None
    try:
        done = int(m.group(1))
        total = int(m.group(2))
        elapsed = float(m.group(3))
        best_pnl = float(m.group(4))
        pct = round((done / max(total, 1)) * 100.0, 2)
        return {
            "completed_runs": done,
            "total_runs": total,
            "elapsed_sec": elapsed,
            "best_pnl": best_pnl,
            "progress_pct": pct,
        }
    except Exception:
        return None


def _parse_smart_scenario_start(line: str) -> Optional[Dict[str, Any]]:
    """Parse smart runner scenario start lines."""
    import re

    m = re.search(r"\[Smart\]\s+Running\s+([a-z_]+):\s+regime=([A-Z_]+)\s+mode=([a-z\-]+)\s+runs=(\d+)", line)
    if not m:
        return None
    try:
        return {
            "scenario": m.group(1),
            "regime": m.group(2),
            "mode": m.group(3),
            "runs": int(m.group(4)),
        }
    except Exception:
        return None


def _fetch_backtest_checkpoints(session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Read persisted optimizer checkpoints from trades_audit for a specific session_id."""
    if not db_client or not session_id:
        return []
    try:
        rows = db_client.execute_query(
            """
                        SELECT ts, detail
            FROM trades_audit
            WHERE event = 'backtest_run_checkpoint'
              AND detail->>'session_id' = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_id, limit),
        )
    except Exception:
        return []

    out = []
    for r in reversed(rows or []):
        d = r.get("detail") or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                d = {}
        out.append({
            "created_at": to_mexico_time_str(str(r.get("ts"))) if r.get("ts") else None,
            "run_index": d.get("run_index"),
            "run_label": d.get("run_label"),
            "status": d.get("status"),
            "run_id": d.get("run_id"),
            "pnl_total": d.get("pnl_total"),
            "n_trades": d.get("n_trades"),
            "duration_ms": d.get("duration_ms"),
            "error": d.get("error"),
        })
    return out


def _fetch_smart_checkpoint_summary(root_session_id: str) -> Dict[str, Any]:
    """Aggregate optimizer checkpoints by smart scenario session suffix."""
    if not db_client or not root_session_id:
        return {"scenarios": []}
    try:
        rows = db_client.execute_query(
            """
            SELECT detail
            FROM trades_audit
            WHERE event = 'backtest_run_checkpoint'
              AND detail->>'session_id' LIKE %s
            ORDER BY id DESC
            LIMIT 400
            """,
            (f"{root_session_id}_%",),
        )
    except Exception:
        return {"scenarios": []}

    scenarios: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        d = r.get("detail") or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                d = {}
        sid = str(d.get("session_id") or "")
        if not sid.startswith(f"{root_session_id}_"):
            continue
        suffix = sid[len(root_session_id) + 1:]
        item = scenarios.setdefault(suffix, {
            "scenario": suffix,
            "completed_runs": 0,
            "failed_runs": 0,
            "last_status": None,
            "best_pnl": None,
            "best_run_id": None,
        })
        st = str(d.get("status") or "")
        if st == "completed":
            item["completed_runs"] += 1
            try:
                pnl = float(d.get("pnl_total")) if d.get("pnl_total") is not None else None
            except Exception:
                pnl = None
            if pnl is not None and (item["best_pnl"] is None or pnl > float(item["best_pnl"])):
                item["best_pnl"] = pnl
                item["best_run_id"] = d.get("run_id")
        elif st in ("failed", "persist_failed"):
            item["failed_runs"] += 1
        if item.get("last_status") is None:
            item["last_status"] = st

    return {"scenarios": sorted(scenarios.values(), key=lambda x: x["scenario"])}


def _fetch_backtest_run_metrics(run_id: int) -> Optional[Dict[str, Any]]:
    if not db_client:
        return None
    try:
        rows = db_client.execute_query(
            """
            SELECT id, label, pnl_total, return_pct, win_rate, profit_factor,
                   max_drawdown, n_trades, pnl_spot, pnl_futures, is_baseline
            FROM backtest_run
            WHERE id = %s
            LIMIT 1
            """,
            (run_id,),
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _fetch_best_backtest_run_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not db_client or not session_id:
        return None
    try:
        rows = db_client.execute_query(
            """
            SELECT br.id, br.label, br.pnl_total, br.return_pct, br.win_rate, br.profit_factor,
                   br.max_drawdown, br.n_trades, br.pnl_spot, br.pnl_futures, br.is_baseline
            FROM backtest_run br
            JOIN (
                SELECT (detail->>'run_id')::int AS run_id
                FROM trades_audit
                WHERE event = 'backtest_run_checkpoint'
                  AND detail->>'session_id' = %s
                  AND detail->>'status' = 'completed'
            ) ck ON ck.run_id = br.id
            ORDER BY br.pnl_total DESC
            LIMIT 1
            """,
            (session_id,),
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _delta_pct(candidate_value: Any, baseline_value: Any) -> Optional[float]:
    try:
        cand = float(candidate_value)
        base = float(baseline_value)
    except Exception:
        return None
    if base == 0:
        return None
    return round(((cand - base) / abs(base)) * 100.0, 4)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _evaluate_smart_backtest_validation(
    root_session_id: str,
    min_pnl_delta_pct: float = -3.0,
    min_win_rate_delta_pct: float = -5.0,
    max_drawdown_delta_pct: float = 15.0,
) -> Dict[str, Any]:
    scenarios: List[Dict[str, Any]] = []
    scenario_statuses: List[str] = []

    for scenario, baseline_run_id in _SMART_BASELINE_RUN_IDS.items():
        candidate_session_id = f"{root_session_id}_{scenario}"
        baseline = _fetch_backtest_run_metrics(baseline_run_id)
        candidate = _fetch_best_backtest_run_for_session(candidate_session_id)

        if not baseline or not candidate:
            scenarios.append({
                "scenario": scenario,
                "baseline_run_id": baseline_run_id,
                "candidate_session_id": candidate_session_id,
                "status": "missing_data",
                "baseline": baseline,
                "candidate": candidate,
                "checks": {},
                "reasons": ["baseline or candidate run not found"],
            })
            scenario_statuses.append("missing_data")
            continue

        baseline_n_trades = int(baseline.get("n_trades") or 0)
        candidate_n_trades = int(candidate.get("n_trades") or 0)
        baseline_pnl_total = _safe_float(baseline.get("pnl_total"))
        candidate_pnl_total = _safe_float(candidate.get("pnl_total"))
        baseline_win_rate = _safe_float(baseline.get("win_rate"))
        candidate_win_rate = _safe_float(candidate.get("win_rate"))
        baseline_drawdown = _safe_float(baseline.get("max_drawdown"))
        candidate_drawdown = _safe_float(candidate.get("max_drawdown"))
        baseline_profit_factor = _safe_float(baseline.get("profit_factor"))
        candidate_profit_factor = _safe_float(candidate.get("profit_factor"))

        metrics = {
            "pnl_total_delta_pct": _delta_pct(candidate_pnl_total, baseline_pnl_total),
            "return_pct_delta_pct": _delta_pct(candidate.get("return_pct"), baseline.get("return_pct")),
            "win_rate_delta_pct": round(candidate_win_rate - baseline_win_rate, 4),
            "profit_factor_delta_pct": _delta_pct(candidate_profit_factor, baseline_profit_factor),
            "max_drawdown_delta_pct": _delta_pct(candidate_drawdown, baseline_drawdown),
            "pnl_per_trade_delta_pct": _delta_pct(
                (candidate_pnl_total / candidate_n_trades) if candidate_n_trades else 0.0,
                (baseline_pnl_total / baseline_n_trades) if baseline_n_trades else 0.0,
            ),
        }

        checks = {
            "pnl_total": (metrics["pnl_total_delta_pct"] is not None and metrics["pnl_total_delta_pct"] >= min_pnl_delta_pct),
            "win_rate": metrics["win_rate_delta_pct"] >= min_win_rate_delta_pct,
            "drawdown": (metrics["max_drawdown_delta_pct"] is None or metrics["max_drawdown_delta_pct"] <= max_drawdown_delta_pct),
        }
        reasons = []
        if not checks["pnl_total"]:
            reasons.append("pnl_total below threshold")
        if not checks["win_rate"]:
            reasons.append("win_rate below threshold")
        if not checks["drawdown"]:
            reasons.append("max_drawdown above threshold")
        scenario_status = "pass" if all(checks.values()) else "fail"
        scenario_statuses.append(scenario_status)
        scenarios.append({
            "scenario": scenario,
            "baseline_run_id": baseline_run_id,
            "candidate_session_id": candidate_session_id,
            "status": scenario_status,
            "baseline": baseline,
            "candidate": candidate,
            "metrics": metrics,
            "checks": checks,
            "reasons": reasons,
        })

    overall = "pass"
    if any(status == "fail" for status in scenario_statuses):
        overall = "fail"
    elif any(status == "missing_data" for status in scenario_statuses):
        overall = "inconclusive"

    return {
        "ok": True,
        "root_session_id": root_session_id,
        "overall": overall,
        "thresholds": {
            "min_pnl_delta_pct": min_pnl_delta_pct,
            "min_win_rate_delta_pct": min_win_rate_delta_pct,
            "max_drawdown_delta_pct": max_drawdown_delta_pct,
        },
        "scenarios": scenarios,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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
    _backtest_tasks[run_id] = {
        "status": "running",
        "type": "single",
        "symbol": symbol,
        "interval": interval,
        "days_back": days_back,
        "run_id": run_id,
        "progress_pct": 0.0,
        "phase": "fetching_klines",
        "started_at": datetime.utcnow().isoformat(),
    }

    def _do_backtest():
        try:
            bt = Backtester(initial_capital=10_000.0, db_client=db_client)
            klines = bt.fetch_klines_from_binance(symbol, interval, days_back)
            _backtest_tasks[run_id].update({"progress_pct": 35.0, "phase": "running_backtest"})
            from param_optimizer import DEFAULT_GRID
            default_params = {k: v[len(v) // 2] for k, v in DEFAULT_GRID.items()}
            result = bt.run_backtest_with_params(klines, default_params, symbol=symbol)
            _backtest_tasks[run_id].update({"progress_pct": 80.0, "phase": "persisting_result"})
            bt.persist_result(result, symbol, interval, days_back)
            _backtest_tasks[run_id].update({
                "status": "completed",
                "progress_pct": 100.0,
                "phase": "completed",
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
            _backtest_tasks[run_id].update({"status": "error", "phase": "error", "error": str(e)})

    background_tasks.add_task(_do_backtest)
    return {"run_id": run_id, "status": "running",
            "message": f"Backtest started for {symbol} ({days_back}d)"}


@app.post("/backtest/optimize")
async def run_backtest_optimizer(
    background_tasks: BackgroundTasks,
    body: Dict,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Run the full backtest optimizer (LHS 1000 runs) in background."""
    try:
        import uuid
        import subprocess
        import os

        _runs = int(body.get("runs", 1000))
        _days = int(body.get("days", 20))
        _auto_apply = bool(body.get("auto_apply", False))
        _run_id = str(uuid.uuid4())
        _backtest_tasks[_run_id] = {
            "status": "running",
            "type": "optimizer",
            "run_id": _run_id,
            "session_id": _run_id,
            "runs": _runs,
            "days": _days,
            "completed_runs": 0,
            "total_runs": _runs,
            "progress_pct": 0.0,
            "phase": "booting",
            "elapsed_sec": 0.0,
            "best_pnl": None,
            "recent_output": [],
            "started_at": datetime.utcnow().isoformat(),
        }

        _script_path = os.path.join(os.path.dirname(__file__), "Dev", "backtest_optimizer.py")

        def _do_optimize():
            try:
                _cmd = [
                    "python", "-u", _script_path,
                    "--runs", str(_runs),
                    "--days", str(_days),
                    "--session-id", _run_id,
                ]
                if _auto_apply:
                    _cmd.append("--auto-apply")
                _proc = subprocess.Popen(
                    _cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                assert _proc.stdout is not None

                _stdout_lines: List[str] = []
                for _line in _proc.stdout:
                    _line = _line.rstrip("\n")
                    _stdout_lines.append(_line)
                    _append_recent_output(_backtest_tasks[_run_id], _line)
                    _backtest_tasks[_run_id]["phase"] = "running"
                    _parsed = _parse_optimizer_progress(_line)
                    if _parsed:
                        _backtest_tasks[_run_id].update(_parsed)

                _return_code = _proc.wait()
                _backtest_tasks[_run_id].update({
                    "status": "completed" if _return_code == 0 else "error",
                    "phase": "completed" if _return_code == 0 else "error",
                    "returncode": _return_code,
                    "stdout": "\n".join(_stdout_lines[-160:]) if _stdout_lines else "",
                    "completed_runs": _runs if _return_code == 0 else int(_backtest_tasks[_run_id].get("completed_runs", 0)),
                    "progress_pct": 100.0 if _return_code == 0 else _backtest_tasks[_run_id].get("progress_pct", 0.0),
                    "completed_at": datetime.utcnow().isoformat(),
                })
            except Exception as _e:
                _backtest_tasks[_run_id].update({"status": "error", "phase": "error", "error": str(_e)})

        background_tasks.add_task(_do_optimize)
        return {
            "ok": True,
            "run_id": _run_id,
            "status": "running",
            "message": f"Backtest optimizer started ({_runs} runs, {_days} days)",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


@app.get("/backtest/progress/{run_id}")
async def get_backtest_progress(run_id: str) -> Dict:
    """Get rich progress for backtest tasks, including persisted checkpoints when available."""
    task = _backtest_tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} not found")

    payload = dict(task)
    checkpoints: List[Dict[str, Any]] = []

    if payload.get("type") == "optimizer":
        checkpoints = _fetch_backtest_checkpoints(session_id=payload.get("session_id") or run_id, limit=40)
        payload["checkpoints"] = checkpoints
        completed_db = sum(1 for c in checkpoints if c.get("status") == "completed")
        failed_db = sum(1 for c in checkpoints if c.get("status") in ("failed", "persist_failed"))
        payload["completed_runs_db"] = completed_db
        payload["failed_runs_db"] = failed_db

        total_runs = int(payload.get("total_runs") or payload.get("runs") or 0)
        completed_mem = int(payload.get("completed_runs") or 0)
        completed = max(completed_mem, completed_db)
        payload["completed_runs"] = completed
        if total_runs > 0:
            payload["progress_pct"] = round((completed / total_runs) * 100.0, 2)

    return payload


def _start_smart_backtest_task(background_tasks: BackgroundTasks, body: Dict, validation_context: Optional[Dict[str, Any]] = None) -> Dict:
    import uuid
    import subprocess
    import os

    _period = str(body.get("period", "1y"))
    _simulations = int(body.get("simulations", 0) or 0)
    _auto_recommend = bool(body.get("auto_recommend_sims", _simulations <= 0))
    _strict_data = bool(body.get("strict_data", False))
    _capital = body.get("capital", None)

    _run_id = str(uuid.uuid4())
    _smart_backtest_tasks[_run_id] = {
        "status": "running",
        "type": "smart_backtest_validation" if validation_context else "smart_backtest",
        "run_id": _run_id,
        "session_id": _run_id,
        "period": _period,
        "simulations": _simulations,
        "auto_recommend_sims": _auto_recommend,
        "strict_data": _strict_data,
        "capital": _capital,
        "progress_pct": 0.0,
        "scenario_total": 4,
        "scenario_completed": 0,
        "current_scenario": None,
        "current_scenario_runs_total": 0,
        "current_scenario_runs_completed": 0,
        "phase": "booting",
        "recent_output": [],
        "started_at": datetime.utcnow().isoformat(),
        "validation_context": validation_context,
    }

    if validation_context and db_client:
        try:
            db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s,%s,%s,%s,%s)",
                (
                    "__config__",
                    "system",
                    "smart_backtest_validation_start",
                    json.dumps({"run_id": _run_id, **validation_context}, default=str),
                    False,
                ),
            )
        except Exception:
            pass

    _script_path = os.path.join(os.path.dirname(__file__), "Dev", "smart_backtest_optimizer.py")

    def _do_smart_backtest():
        try:
            _cmd = [
                "python", "-u", _script_path,
                "--period", _period,
                "--session-id", _run_id,
            ]
            if _simulations > 0:
                _cmd.extend(["--simulations", str(_simulations)])
            if _auto_recommend:
                _cmd.append("--auto-recommend-sims")
            if _strict_data:
                _cmd.append("--strict-data")
            if _capital is not None:
                _cmd.extend(["--capital", str(float(_capital))])

            _proc = subprocess.Popen(
                _cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert _proc.stdout is not None

            _stdout_lines: List[str] = []
            _seen_scenarios: List[str] = []
            _current = None

            for _line in _proc.stdout:
                _line = _line.rstrip("\n")
                _stdout_lines.append(_line)
                _append_recent_output(_smart_backtest_tasks[_run_id], _line)
                _smart_backtest_tasks[_run_id]["phase"] = "running"

                _sc = _parse_smart_scenario_start(_line)
                if _sc:
                    if _current and _current in _seen_scenarios:
                        _smart_backtest_tasks[_run_id]["scenario_completed"] = min(
                            int(_smart_backtest_tasks[_run_id].get("scenario_total", 4)),
                            len(_seen_scenarios),
                        )
                    _current = _sc["scenario"]
                    if _current not in _seen_scenarios:
                        _seen_scenarios.append(_current)
                    _smart_backtest_tasks[_run_id].update({
                        "current_scenario": _sc["scenario"],
                        "current_regime": _sc["regime"],
                        "current_mode": _sc["mode"],
                        "current_scenario_runs_total": _sc["runs"],
                        "current_scenario_runs_completed": 0,
                    })

                _op = _parse_optimizer_progress(_line)
                if _op and _smart_backtest_tasks[_run_id].get("current_scenario"):
                    _smart_backtest_tasks[_run_id].update({
                        "current_scenario_runs_completed": _op.get("completed_runs", 0),
                        "current_scenario_runs_total": _op.get("total_runs", 0),
                        "current_scenario_elapsed_sec": _op.get("elapsed_sec", 0.0),
                        "current_scenario_best_pnl": _op.get("best_pnl"),
                    })
                    _sc_total = int(_smart_backtest_tasks[_run_id].get("scenario_total", 4))
                    _sc_done = max(0, len(_seen_scenarios) - 1)
                    _local_ratio = (_op.get("completed_runs", 0) / max(_op.get("total_runs", 1), 1))
                    _smart_backtest_tasks[_run_id]["progress_pct"] = round(((_sc_done + _local_ratio) / max(_sc_total, 1)) * 100.0, 2)

            _return_code = _proc.wait()
            _smart_backtest_tasks[_run_id].update({
                "status": "completed" if _return_code == 0 else "error",
                "phase": "completed" if _return_code == 0 else "error",
                "returncode": _return_code,
                "stdout": "\n".join(_stdout_lines[-260:]) if _stdout_lines else "",
                "scenario_completed": int(_smart_backtest_tasks[_run_id].get("scenario_total", 4)) if _return_code == 0 else int(_smart_backtest_tasks[_run_id].get("scenario_completed", 0)),
                "progress_pct": 100.0 if _return_code == 0 else float(_smart_backtest_tasks[_run_id].get("progress_pct", 0.0)),
                "completed_at": datetime.utcnow().isoformat(),
            })
        except Exception as _e:
            _smart_backtest_tasks[_run_id].update({
                "status": "error",
                "phase": "error",
                "error": str(_e),
                "completed_at": datetime.utcnow().isoformat(),
            })

    background_tasks.add_task(_do_smart_backtest)
    return {
        "ok": True,
        "run_id": _run_id,
        "status": "running",
        "message": "Smart Backtest Optimizer iniciado",
        "params": {
            "period": _period,
            "simulations": _simulations,
            "auto_recommend_sims": _auto_recommend,
            "strict_data": _strict_data,
            "capital": _capital,
        },
        "validation_context": validation_context,
    }


@app.post("/smart-backtest/start")
async def start_smart_backtest(
    background_tasks: BackgroundTasks,
    body: Dict,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Start Smart Backtest Optimizer orchestration in background."""
    try:
        return _start_smart_backtest_task(background_tasks, body)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/smart-backtest/validation/start")
async def start_smart_backtest_validation(
    background_tasks: BackgroundTasks,
    body: Dict,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Start a Smart Backtest session explicitly marked as a pre-implementation validation run."""
    try:
        validation_context = {
            "candidate_label": body.get("candidate_label") or "unnamed_candidate",
            "candidate_params": body.get("candidate_params") or {},
            "notes": body.get("notes"),
            "baseline_run_ids": dict(_SMART_BASELINE_RUN_IDS),
        }
        return _start_smart_backtest_task(background_tasks, body, validation_context=validation_context)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/smart-backtest/status/{run_id}")
async def get_smart_backtest_status(
    run_id: str,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Get status for a Smart Backtest task started via API."""
    task = _smart_backtest_tasks.get(run_id)
    if task:
        return task
    raise HTTPException(status_code=404, detail=f"smart run_id {run_id} not found")


@app.get("/smart-backtest/active")
async def get_active_smart_backtest(
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Return active smart backtest task (running only), or None if no running tasks."""
    if not _smart_backtest_tasks:
        return {"ok": True, "task": None}

    def _sort_key(t: Dict[str, Any]) -> str:
        return str(
            t.get("started_at")
            or t.get("completed_at")
            or t.get("finished_at")
            or ""
        )

    tasks = list(_smart_backtest_tasks.values())
    running = [t for t in tasks if str(t.get("status")) == "running"]
    picked = max(running, key=_sort_key) if running else None
    return {"ok": True, "task": picked}


@app.get("/smart-backtest/progress/{run_id}")
async def get_smart_backtest_progress(
    run_id: str,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Get rich smart progress + scenario checkpoint summary from DB."""
    task = _smart_backtest_tasks.get(run_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"smart run_id {run_id} not found")

    payload = dict(task)
    payload["checkpoint_summary"] = _fetch_smart_checkpoint_summary(payload.get("session_id") or run_id)
    return payload


@app.get("/smart-backtest/validation/{root_session_id}")
async def get_smart_backtest_validation(
    root_session_id: str,
    min_pnl_delta_pct: float = -3.0,
    min_win_rate_delta_pct: float = -5.0,
    max_drawdown_delta_pct: float = 15.0,
    _auth: None = Depends(_require_api_key),
) -> Dict:
    """Evaluate a Smart Backtest root session against the 3y/60000 scenario baselines."""
    return _evaluate_smart_backtest_validation(
        root_session_id=root_session_id,
        min_pnl_delta_pct=min_pnl_delta_pct,
        min_win_rate_delta_pct=min_win_rate_delta_pct,
        max_drawdown_delta_pct=max_drawdown_delta_pct,
    )


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
    if bool(getattr(settings, 'dry_run', False)):
        checks["binance"] = {
            "status": "skipped",
            "detail": "dry_run mode",
            "testnet": settings.binance_use_testnet,
        }
    else:
        try:
            import time as _time, asyncio as _asyncio
            t0 = _time.monotonic()
            await _asyncio.to_thread(binance_client.get_account_balance)
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
        _now_utc = datetime.now(timezone.utc)
        _last_cycle_aware = last_cycle if last_cycle.tzinfo else last_cycle.replace(tzinfo=timezone.utc)
        age_min = (_now_utc - _last_cycle_aware).total_seconds() / 60
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

    _cycle_failures = int(getattr(orchestrator, 'consecutive_cycle_failures', 0) or 0)
    checks["cycle_failures"] = {
        "status": "error" if _cycle_failures >= 3 else ("warning" if _cycle_failures > 0 else "ok"),
        "consecutive": _cycle_failures,
        "last_error": getattr(orchestrator, 'last_cycle_error', None),
        "last_error_at": getattr(orchestrator, 'last_cycle_error_at', None).isoformat() if getattr(orchestrator, 'last_cycle_error_at', None) else None,
    }
    if _cycle_failures >= 3:
        overall = "error"
    elif _cycle_failures > 0 and overall != "error":
        overall = "warning"

    # Position sync: compare tracked symbols in DB vs their balances in Binance
    try:
        all_pos = position_manager.get_all_positions()
        db_count = sum(len(v) for v in all_pos.values())
        import asyncio as _asyncio
        balances = {} if bool(getattr(settings, 'dry_run', False)) else await _asyncio.to_thread(binance_client.get_account_balance)
        # Only check SPOT positions — futures positions (trade_type='futures') have no spot balance
        def _is_futures_pos(pos_list):
            return any(
                (p.metadata or {}).get('trade_type') == 'futures'
                for p in pos_list
            )
        spot_pos = {sym: ps for sym, ps in all_pos.items() if not _is_futures_pos(ps)}
        futures_pos = {sym: ps for sym, ps in all_pos.items() if _is_futures_pos(ps)}
        tracked_symbols = set(spot_pos.keys())

        def _sym_to_asset(sym: str) -> str:
            for quote in ("USDT", "BUSD", "BTC", "ETH"):
                if sym.endswith(quote) and len(sym) > len(quote):
                    return sym[:-len(quote)]
            return sym

        binance_holding_count = sum(
            1 for sym in tracked_symbols
            if float(balances.get(_sym_to_asset(sym), 0)) > 0
        )
        # mismatch: DB has SPOT entries for symbols with no Binance spot balance
        if bool(getattr(settings, 'dry_run', False)):
            symbols_missing_in_binance = []
            mismatch = 0
            sync_status = "skipped"
        else:
            symbols_missing_in_binance = [
                sym for sym in tracked_symbols
                if float(balances.get(_sym_to_asset(sym), 0)) == 0
            ]
            mismatch = len(symbols_missing_in_binance)
            sync_status = "ok" if mismatch == 0 else "warning"
        checks["position_sync"] = {
            "status": sync_status,
            "db_open_positions": db_count,
            "spot_tracked": len(tracked_symbols),
            "futures_tracked": len(futures_pos),
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
        _futures_enabled = getattr(settings, 'futures_enabled', True)
        if _futures_enabled:
            import urllib.request as _ur, time as _time, asyncio as _asyncio
            t0 = _time.monotonic()
            await _asyncio.to_thread(lambda: _ur.urlopen('https://fapi.binance.com/fapi/v1/ping', timeout=5).read())
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
                "open_shorts": _open_shorts, "open_longs": _open_longs, "leverage_max": getattr(_fc, '_active_leverage', 10),
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
        from run_trades import _get_futures_client as _health_gfc
        _fc_health = _health_gfc(comp.settings)
        _bal = comp.portfolio_manager.get_unified_balance(futures_client=_fc_health)
        _total = float(_bal.get("total_usdt", 0))
        _liquid = float(_bal.get("usdt_available", 0)) + float(_bal.get("futures_available", 0))
        # BNB commission reserve is cash-equivalent, not invested trading capital
        _spot_bnb_value = float(_bal.get("spot_bnb_usdt", 0.0))
        _invested = max(0.0, _total - _liquid - _spot_bnb_value)
        _exp_pct = _invested / _total if _total > 0 else 0.0
        _max_exp = getattr(settings, 'max_total_exposure_pct', 0.75)
        _exp_status = "ok" if _exp_pct < _max_exp * 0.85 else ("warning" if _exp_pct < _max_exp else "cap_reached")
        checks["portfolio_exposure"] = {
            "status": _exp_status,
            "invested_usdt": round(_invested, 2),
            "liquid_usdt": round(_liquid, 2),
            "total_usdt": round(_total, 2),
            "exposure_pct": round(_exp_pct * 100, 1),
            "max_exposure_pct": round(_max_exp * 100, 1),
            "spot_usdt": round(float(_bal.get("usdt_available", 0)), 2),
            "futures_usdt": round(float(_bal.get("futures_total", 0)), 2),
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
        "overflow_max_extra": _cfg_param(settings, 'overflow_max_extra'),
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

    # Disk storage
    try:
        import shutil as _shutil
        _du = _shutil.disk_usage('/app')
        _free_pct = _du.free / _du.total if _du.total > 0 else 1.0
        _disk_status = "ok" if _free_pct > 0.20 else ("warning" if _free_pct > 0.10 else "critical")
        checks["disk_storage"] = {
            "status": _disk_status,
            "total_gb": round(_du.total / 1e9, 1),
            "used_gb":  round(_du.used  / 1e9, 1),
            "free_gb":  round(_du.free  / 1e9, 1),
            "used_pct": round(_du.used  / _du.total * 100, 1),
        }
        if _disk_status == "critical" and overall == "ok":
            overall = "warning"
    except Exception as _de:
        checks["disk_storage"] = {"status": "error", "detail": str(_de)}

    return {
        "overall": overall,
        "checks": checks,
        "timestamp": to_mexico_time(datetime.utcnow()).isoformat(),
    }


@app.get("/health/results")
async def get_health_results(force: bool = False) -> Dict:
    """Result-scored health (SMART recheck) with SPOT/FUTURES split and adaptive cadence."""
    try:
        now_utc = datetime.now(timezone.utc)
        cached = _HEALTH_RESULTS_CACHE.get("snapshot")
        if isinstance(cached, dict) and not force:
            ts_raw = cached.get("timestamp")
            ts = None
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except Exception:
                    ts = None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts and (now_utc - ts).total_seconds() <= 20:
                return {**cached, "source": "cache"}

        snapshot = _compute_health_results_snapshot(trigger="manual_force" if force else "manual")
        _persist_health_results_snapshot(snapshot)
        _HEALTH_RESULTS_CACHE["snapshot"] = snapshot
        _HEALTH_RESULTS_CACHE["last_run_at"] = snapshot.get("timestamp")
        _HEALTH_RESULTS_CACHE["interval_seconds"] = int(((snapshot.get("smart_recheck") or {}).get("interval_seconds") or 300))
        _HEALTH_RESULTS_CACHE["last_trigger"] = "manual"
        _HEALTH_RESULTS_EVENT.set()
        return {**snapshot, "source": "fresh"}
    except Exception as e:
        ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR, f"health/results failed: {e}")
        return {
            "ok": False,
            "overall": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
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


@app.get("/health/full-check")
async def get_health_full_check() -> Dict:
    """
    Full system health check: runs internal assertions on config, regime logic,
    futures core, spot pipeline, Kelly sizing, and capital flow.
    Returns categorised PASS/FAIL/WARN report consumable by the dashboard.
    """
    from config import Settings
    import traceback

    HEALTH_VERSION = "2026-06-18-v1"
    categories = []
    total = passed = failed = warned = 0

    def _check(name: str, fn, warn_only: bool = False):
        nonlocal total, passed, failed, warned
        total += 1
        try:
            fn()
            passed += 1
            return {"name": name, "status": "pass"}
        except AssertionError as e:
            if warn_only:
                warned += 1
                return {"name": name, "status": "warn", "detail": str(e)}
            failed += 1
            return {"name": name, "status": "fail", "detail": str(e)}
        except Exception as e:
            if warn_only:
                warned += 1
                return {"name": name, "status": "warn", "detail": f"{type(e).__name__}: {e}"}
            failed += 1
            return {"name": name, "status": "fail", "detail": f"{type(e).__name__}: {e}"}

    # ── Cat 1: Config Integrity ───────────────────────────────────────────────
    cfg = Settings()
    cat1_checks = [
        _check("kelly_base_lt_kelly_max",
               lambda: assert_(cfg.kelly_base_usdt < cfg.kelly_max_usdt,
                               f"kelly_base={cfg.kelly_base_usdt} >= kelly_max={cfg.kelly_max_usdt}")),
        _check("futures_breakeven_lt_trailing_act",
               lambda: assert_(cfg.futures_breakeven_activation_pct < cfg.futures_trailing_activation_pct,
                               "breakeven must activate before trailing")),
        _check("futures_smart_tp_score_in_range",
               lambda: assert_(1 <= cfg.futures_smart_tp_extend_score_threshold <= 4,
                               f"score_threshold={cfg.futures_smart_tp_extend_score_threshold}")),
        _check("smart_tp_disabled_regimes_is_list",
               lambda: assert_(isinstance(cfg.smart_tp_extend_disabled_regimes, list),
                               "must be list")),
        _check("kelly_max_pct_liquid_lte_95pct",
               lambda: assert_(cfg.kelly_max_pct_liquid <= 0.95,
                               f"kelly_max_pct_liquid={cfg.kelly_max_pct_liquid} exceeds 0.95 cap"),
               warn_only=True),
        _check("futures_leverage_matches_backtest",
               lambda: assert_(cfg.futures_leverage == 10,
                               f"futures_leverage={cfg.futures_leverage} != 10 (backtest run_0812)")),
        _check("futures_kelly_max_pct_liquid_matches_deployed_config",
               lambda: assert_(cfg.futures_kelly_max_pct_liquid == 0.85,
                               f"futures_kelly_max_pct_liquid={cfg.futures_kelly_max_pct_liquid} != 0.85")),
        _check("max_positions_futures_short_matches_deployed_config",
               lambda: assert_(cfg.max_positions_futures_short == 5,
                               f"max_positions_futures_short={cfg.max_positions_futures_short} != 5")),
        _check("no_module_level_leverage_override",
               lambda: assert_(cfg.futures_leverage <= 10,
                               f"futures_leverage={cfg.futures_leverage} exceeds module cap 10")),
    ]
    categories.append({"name": "config_integrity", "label": "Config Integrity",
                        "checks": cat1_checks})

    # ── Cat 2: Market Regime ──────────────────────────────────────────────────
    from market_regime import _build_regime, _CONFIRM_CYCLES
    def _raw_regime(mode): return {
        "mode": mode, "regime": "test", "color": "#fff",
        "btc_7d_pct": -8.0, "btc_30d_pct": -18.0, "btc_day_pct": -1.0,
        "vol_ratio": 0.5, "ema9_vs_ema21": "DOWN", "bb_bandwidth": 0.03, "reason": "hc"
    }
    cat2_checks = [
        _check("anti_whipsaw_cycles_geq2",
               lambda: assert_(_CONFIRM_CYCLES >= 2, f"_CONFIRM_CYCLES={_CONFIRM_CYCLES}")),
        _check("futures_short_sl_lt_tp",
               lambda: assert_(_build_regime(_raw_regime("FUTURES_SHORT"), True).sl_pct <
                               _build_regime(_raw_regime("FUTURES_SHORT"), True).tp_pct,
                               "FUTURES_SHORT SL >= TP")),
        _check("range_scalp_kelly_reduced",
               lambda: assert_(_build_regime(_raw_regime("RANGE_SCALP"), True).kelly_multiplier < 1.0,
                               "RANGE_SCALP Kelly not reduced")),
        _check("preservation_narrative_only",
               lambda: assert_(_build_regime(_raw_regime("PRESERVATION"), True).narrative_only is True,
                               "PRESERVATION should be narrative_only")),
        _check("spot_bull_not_in_disabled_regimes",
               lambda: assert_("SPOT_BULL" not in cfg.smart_tp_extend_disabled_regimes,
                               "SPOT_BULL incorrectly disabled")),
    ]
    categories.append({"name": "market_regime", "label": "Market Regime Logic",
                        "checks": cat2_checks})

    # ── Cat 3: Futures Core ───────────────────────────────────────────────────
    from futures_client import FuturesClient, FuturesPosition
    from dataclasses import fields as _dc_fields
    cat3_checks = [
        _check("smart_meta_is_dataclass_field",
               lambda: assert_('_smart_meta' in {f.name for f in _dc_fields(FuturesPosition)},
                               "_smart_meta not in dataclass fields")),
        _check("smart_meta_not_shared",
               lambda: _test_smart_meta_isolation()),
        _check("update_sl_tp_exists",
               lambda: assert_(hasattr(FuturesClient, 'update_sl_tp'),
                               "update_sl_tp missing")),
        _check("evaluate_short_momentum_returns_minus1_without_client",
               lambda: _test_momentum_no_client()),
        _check("pre_entry_rejects_duplicate",
               lambda: _test_pre_entry_duplicate()),
    ]
    categories.append({"name": "futures_core", "label": "Futures Client Core",
                        "checks": cat3_checks})

    # ── Cat 4: SPOT Pipeline ──────────────────────────────────────────────────
    from run_trades import _check_sl_tp_positions, _wire_futures_client
    import inspect as _inspect
    cat4_checks = [
        _check("range_scalp_in_disabled_regimes",
               lambda: assert_("RANGE_SCALP" in cfg.smart_tp_extend_disabled_regimes,
                               "RANGE_SCALP not in disabled list")),
        _check("preservation_in_disabled_regimes",
               lambda: assert_("PRESERVATION" in cfg.smart_tp_extend_disabled_regimes,
                               "PRESERVATION not in disabled list")),
        _check("check_sl_tp_has_regime_param",
               lambda: assert_('regime' in _inspect.signature(_check_sl_tp_positions).parameters,
                               "regime param missing")),
        _check("wire_futures_client_works",
               lambda: _test_wire_futures_client()),
    ]
    categories.append({"name": "spot_pipeline", "label": "SPOT SL/TP Pipeline",
                        "checks": cat4_checks})

    # ── Cat 5: Kelly Sizing ───────────────────────────────────────────────────
    from run_trades import _kelly_position_size
    cat5_checks = [
        _check("kelly_hard_cap_not_exceeded",
               lambda: assert_(
                   _kelly_position_size(cfg, 0.99, {"success_probability": 0.99}, 1000.0,
                                       sl_pct=0.018, tp_pct=0.035) <= 1000.0 * cfg.kelly_max_pct_liquid + 0.01,
                   "Kelly exceeded hard cap")),
        _check("kelly_disabled_uses_max_per_trade",
               lambda: assert_(
                   abs(_kelly_position_size(type('S', (), {'kelly_enabled': False,
                                                           'max_per_trade_usdt': 75.0})(),
                                           0.99, {}, 1000.0) - 75.0) < 0.01,
                   "Kelly disabled not using max_per_trade")),
        _check("kelly_zero_liquid_no_crash",
               lambda: assert_(
                   _kelly_position_size(cfg, 0.65, {"success_probability": 0.65}, 0.0,
                                       sl_pct=0.018, tp_pct=0.035) >= 0,
                   "Negative result with 0 liquid")),
    ]
    categories.append({"name": "kelly_sizing", "label": "Kelly Sizing",
                        "checks": cat5_checks})

    # ── Cat 6: Capital Flow ───────────────────────────────────────────────────
    cat6_checks = [
        _check("capbridge_min_transfer_geq10",
               lambda: assert_(
                   _cfg_param(cfg, 'futures_capital_bridge_min_transfer') >= 10.0,
                   "Min transfer < $10")),
        _check("capbridge_enabled_flag_exists",
               lambda: assert_(
                   _cfg_param(cfg, 'futures_capital_bridge_enabled') is not None,
                   "futures_capital_bridge_enabled missing"),
               warn_only=True),
        _check("smart_rebalance_capital_exists",
               lambda: assert_(hasattr(FuturesClient, 'smart_rebalance_capital'),
                               "smart_rebalance_capital missing")),
    ]
    categories.append({"name": "capital_flow", "label": "CapBridge & Capital Flow",
                        "checks": cat6_checks})

    # ── Summary ───────────────────────────────────────────────────────────────
    overall = "ok" if failed == 0 else ("warn" if warned > 0 else "error")
    if failed > 0:
        overall = "error"
    elif warned > 0:
        overall = "warn"

    return {
        "version": HEALTH_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "summary": {"total": total, "passed": passed, "failed": failed, "warned": warned},
        "categories": categories,
    }


def assert_(condition: bool, msg: str = "") -> None:
    """Helper: raise AssertionError if condition is False."""
    if not condition:
        raise AssertionError(msg)


def _test_smart_meta_isolation():
    from futures_client import FuturesPosition
    a = FuturesPosition(symbol="BTC", side="SHORT", qty=1.0, entry_price=100.0,
                        sl_price=103.0, tp_price=93.0, order_id="A",
                        sl_order_id="SA", tp_order_id="TA")
    b = FuturesPosition(symbol="ETH", side="SHORT", qty=1.0, entry_price=100.0,
                        sl_price=103.0, tp_price=93.0, order_id="B",
                        sl_order_id="SB", tp_order_id="TB")
    a._smart_meta['x'] = 1
    assert_('x' not in b._smart_meta, "_smart_meta shared between instances!")


def _test_momentum_no_client():
    from futures_client import FuturesClient
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    fc._binance_client_ref = None
    score, _ = fc._evaluate_short_momentum("BTCUSDT", None)
    assert_(score == -1, f"Expected -1 score when no client ref, got {score}")


def _test_pre_entry_duplicate():
    import time
    from futures_client import FuturesClient, FuturesPosition
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    fc.max_shorts = 10
    pos = FuturesPosition(symbol="BTCUSDT", side="SHORT", qty=1.0, entry_price=100.0,
                          sl_price=103.0, tp_price=93.0, order_id="X",
                          sl_order_id="SX", tp_order_id="TX")
    fc._open_positions["BTCUSDT_SHORT"] = pos
    ok, reason = fc._pre_entry_checks("BTCUSDT", 100.0, "SHORT")
    assert_(not ok, "Should reject duplicate position")
    assert_("Already have" in reason, f"Unexpected reason: {reason}")


def _test_wire_futures_client():
    from futures_client import FuturesClient
    from run_trades import _wire_futures_client
    fc = FuturesClient(api_key="test", api_secret="test", dry_run=True)
    _wire_futures_client(fc, "mock", None)
    assert_(fc._binance_client_ref == "mock", "_binance_client_ref not set")


@app.get("/system/mission-status")
async def get_mission_status(_auth=Depends(_require_api_key)) -> Dict:
    """
    P3.3: Mission Planner — daily urgency based on realized PnL vs daily target.
    Returns urgency level, progress %, recommendation, and projected close time.
    """
    try:
        from daily_pnl_tracker import get_daily_realized_pnl as _gdpnl
        futures_pnl = _gdpnl(db_client, "futures")
        spot_pnl    = _gdpnl(db_client, "spot")
        total_pnl   = futures_pnl + spot_pnl

        daily_target = float(getattr(settings, 'sso_target_daily_usd', 40.0))
        progress_pct = (total_pnl / daily_target * 100) if daily_target > 0 else 0.0

        # Time remaining in the trading day (UTC)
        _now = datetime.now(timezone.utc)
        _eod = _now.replace(hour=23, minute=59, second=59)
        hours_left = (_eod - _now).total_seconds() / 3600.0

        # Urgency classification
        if progress_pct >= 100:
            urgency = "COMPLETE"
            recommendation = "🎯 Target diario alcanzado. Proteger capital, evitar nuevas entradas de riesgo."
        elif hours_left < 2 and progress_pct < 50:
            urgency = "CRITICAL"
            recommendation = "🔴 Menos de 2h y <50% del target. Revisar posiciones abiertas, no escalar."
        elif hours_left < 4 and progress_pct < 70:
            urgency = "HIGH"
            recommendation = "🟠 Tiempo reducido. Priorizar cierre de posiciones ganadoras."
        elif progress_pct < 30:
            urgency = "LOW"
            recommendation = "🟡 Inicio del día. Seguir criterios de entrada normales."
        else:
            urgency = "NORMAL"
            recommendation = "🟢 Progreso en línea. Mantener estrategia actual."

        return {
            "ok": True,
            "total_pnl_usd":  round(total_pnl, 2),
            "futures_pnl":    round(futures_pnl, 2),
            "spot_pnl":       round(spot_pnl, 2),
            "daily_target":   daily_target,
            "progress_pct":   round(progress_pct, 1),
            "urgency":        urgency,
            "recommendation": recommendation,
            "hours_left":     round(hours_left, 1),
            "timestamp":      _now.isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/system/runtime-state")
async def get_runtime_state(_auth=Depends(_require_api_key)) -> Dict:
    """
    D1: Return current trading_runtime_state from BD.
    Exposes daily_pnl_futures, daily_pnl_spot, regime_confidence,
    last_learning_run, and analysis_cache_freshness to the dashboard.
    """
    try:
        rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state ORDER BY key"
        )
        result = {}
        for r in rows:
            _value = r.get("value")
            if isinstance(_value, str):
                try:
                    _value = json.loads(_value)
                except Exception:
                    _value = {"value": _value}
            elif not isinstance(_value, dict):
                _value = {"value": _value}
            result[r["key"]] = {
                **_value,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        if "regime_transition_state" in result:
            _transition_state = result.get("regime_transition_state") or {}
            result["transition_summary"] = {
                "active": bool(_transition_state.get("active")),
                "from_mode": _transition_state.get("pre_switch", {}).get("from_mode"),
                "to_mode": _transition_state.get("pre_switch", {}).get("to_mode"),
                "confidence": _transition_state.get("pre_switch", {}).get("confidence"),
                "block_new_shorts": bool(_transition_state.get("pre_switch", {}).get("block_new_shorts")),
                "updated_at": _transition_state.get("updated_at"),
            }
        try:
            _summary_mode = str(getattr(orchestrator, 'current_regime_mode', '') or 'SPOT_BULL').strip().upper() or 'SPOT_BULL'
            result["effective_runtime"] = _get_effective_runtime_selection(_summary_mode)
        except Exception:
            pass
        try:
            result["pause_new_entries_status"] = _get_pause_new_entries_status()
        except Exception:
            pass
        # Enrich with live daily PnL from daily_pnl_tracker cache
        try:
            from daily_pnl_tracker import get_daily_realized_pnl as _gdpnl
            result.setdefault("daily_pnl_futures", {})["live_pnl"] = _gdpnl(db_client, "futures")
            result.setdefault("daily_pnl_spot",    {})["live_pnl"] = _gdpnl(db_client, "spot")
        except Exception:
            pass
        return {"ok": True, "state": result, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e), "state": {}}


@app.get("/system/effective-runtime")
async def get_effective_runtime(_auth=Depends(_require_api_key)) -> Dict:
    """Lightweight endpoint for near-real-time mode/profile truth from live engine state."""
    try:
        _summary_mode = str(getattr(orchestrator, 'current_regime_mode', '') or 'SPOT_BULL').strip().upper() or 'SPOT_BULL'
        _effective = _get_effective_runtime_selection(_summary_mode)
        return {
            "ok": True,
            "effective_runtime": _effective,
            "cycle_minutes": getattr(orchestrator, 'current_cycle_minutes', None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "effective_runtime": {}}


@app.get("/config/kelly")
async def get_kelly_config(_auth=Depends(_require_api_key)) -> Dict:
    """
    C1: Return current Kelly sizing configuration for SPOT and FUTURES.
    Includes active multipliers, tier thresholds, and regime kelly_mult.
    """
    try:
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings
        _regime = None
        try:
            import market_regime as _mr
            _regime = _mr.get_regime(_comp.binance_client, settings=_s)
        except Exception:
            pass

        # Correlation status
        _corr_avg_r, _corr_mult = 0.0, 1.0
        try:
            from futures_correlation import get_portfolio_correlation as _gpc
            from run_trades import _futures_client as _fc_ref
            if _fc_ref:
                _shorts = [p.symbol for p in _fc_ref._open_positions.values()
                           if not p.closed and p.side == "SHORT"]
                if len(_shorts) >= 2:
                    _corr_avg_r, _corr_mult = _gpc(_comp.binance_client, _shorts)
        except Exception:
            pass

        _regime_mode = getattr(_regime, 'mode', 'unknown') if _regime else 'unknown'
        _regime_conf = getattr(_regime, 'confidence', 0.5) if _regime else 0.5
        _regime_km   = getattr(_regime, 'kelly_multiplier', 1.0) if _regime else 1.0
        _confirmed   = getattr(_regime, 'confirmed', False) if _regime else False

        # Load any DB overrides
        _db_overrides = {}
        try:
            _ov_rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
            )
            if _ov_rows:
                _db_overrides = _ov_rows[0]['value'] or {}
        except Exception:
            pass

        return {
            "ok": True,
            "spot": {
                "base_usdt":       config_helpers.get_param(_s, 'kelly_base_usdt'),
                "max_usdt":        config_helpers.get_param(_s, 'kelly_max_usdt'),
                "cap_pct_liquid":  config_helpers.get_param(_s, 'kelly_max_pct_liquid'),
                "dynamic_base_pct":config_helpers.get_param(_s, 'kelly_dynamic_base_pct'),
                "max_pct_cap":     config_helpers.get_param(_s, 'kelly_max_pct_cap'),
                "tiers": {
                    "FUERTE": {"prob": 0.65, "strength": 0.80, "amount": "max_usdt"},
                    "ALTA":   {"prob": 0.60, "strength": 0.70, "amount": "base + 90% tramo"},
                    "MEDIA":  {"prob": 0.55, "strength": 0.55, "amount": "base + 50% tramo"},
                    "BASE":   {"prob": 0.50, "strength": 0.0,  "amount": "base × kelly_f×10"},
                },
                "fng_boost": {
                    "enabled":            getattr(_s, 'fng_kelly_boost_enabled', True),
                    "capitulacion_thr":   getattr(_s, 'fng_capitulation_threshold', 15),
                    "fear_thr":           getattr(_s, 'fng_extreme_fear_threshold', 20),
                    "greed_thr":          getattr(_s, 'fng_extreme_greed_threshold', 75),
                    "capitulacion_mult":  getattr(_s, 'fng_capitulation_multiplier', 1.50),
                    "fear_mult":          getattr(_s, 'fng_extreme_fear_multiplier', 1.35),
                    "greed_mult":         getattr(_s, 'fng_extreme_greed_multiplier', 0.65),
                },
                "conviction_m3": {
                    "enabled":   getattr(_s, 'kelly_conviction_enabled', True),
                    "vol2x_mult":getattr(_s, 'kelly_conviction_vol2x', 1.8),
                    "vol3x_mult":getattr(_s, 'kelly_conviction_vol3x', 2.5),
                    "max_pct":   getattr(_s, 'kelly_conviction_max_pct', 0.25),
                },
            },
            "futures": {
                "base_usdt":      config_helpers.get_param(_s, 'futures_kelly_base_usdt'),
                "max_usdt":       config_helpers.get_param(_s, 'futures_kelly_max_usdt'),
                "cap_pct_liquid": config_helpers.get_param(_s, 'futures_kelly_max_pct_liquid'),
                "cap_pct_margin": config_helpers.get_param(_s, 'futures_kelly_cap_pct'),
                "kelly_multiplier_table": {
                    "not_confirmed":    0.5,
                    "confirmed_low":    0.6,
                    "confirmed_medium": 0.8,
                    "confirmed_high":   1.0,
                    "confidence_thresholds": {"low": 0.60, "high": 0.80},
                },
                "correlation": {
                    "floor": 0.60,
                    "kelly_min": 0.65,
                    "current_avg_r": round(_corr_avg_r, 4),
                    "current_mult":  round(_corr_mult, 3),
                },
            },
            "profit_realization": {
                "enabled": getattr(_s, 'profit_realization_enabled', True),
                "spot": {
                    "enabled": getattr(_s, 'profit_realization_spot_enabled', True),
                    "min_profit_pct": getattr(_s, 'profit_realization_spot_min_profit_pct', 0.0030),
                    "partial_minutes": getattr(_s, 'profit_realization_spot_partial_minutes', 60.0),
                    "full_minutes": getattr(_s, 'profit_realization_spot_full_minutes', 120.0),
                    "drawback_pct": getattr(_s, 'profit_realization_spot_drawback_pct', 0.0060),
                    "partial_close_pct": getattr(_s, 'profit_realization_spot_partial_close_pct', 0.50),
                    "max_extensions": getattr(_s, 'profit_realization_spot_max_extensions', 3),
                },
                "futures": {
                    "enabled": getattr(_s, 'profit_realization_futures_enabled', True),
                    "min_profit_pct": getattr(_s, 'profit_realization_futures_min_profit_pct', 0.0040),
                    "partial_minutes": getattr(_s, 'profit_realization_futures_partial_minutes', 45.0),
                    "full_minutes": getattr(_s, 'profit_realization_futures_full_minutes', 90.0),
                    "drawback_pct": getattr(_s, 'profit_realization_futures_drawback_pct', 0.0050),
                    "partial_close_pct": getattr(_s, 'profit_realization_futures_partial_close_pct', 0.50),
                    "max_extensions": getattr(_s, 'profit_realization_futures_max_extensions', 2),
                },
            },
            "regime": {
                "mode":           _regime_mode,
                "confirmed":      _confirmed,
                "confidence":     round(_regime_conf, 2),
                "kelly_mult":     round(_regime_km, 2),
                "active_label":   (
                    "ÓPTIMO" if _regime_km >= 0.9 else
                    "MODERADO" if _regime_km >= 0.6 else "RESTRINGIDO"
                ),
            },
            "db_overrides": _db_overrides,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Whitelist de campos editables via PATCH /config/kelly
_KELLY_EDITABLE_FIELDS = {
    "kelly_base_usdt", "kelly_max_usdt", "kelly_max_pct_liquid",
    "kelly_min_usdt", "kelly_dynamic_base_pct", "kelly_max_pct_cap",
    "futures_kelly_base_usdt", "futures_kelly_max_usdt",
    "futures_kelly_max_pct_liquid", "futures_kelly_cap_pct", "futures_kelly_min_usdt",
    "fng_capitulation_threshold", "fng_extreme_fear_threshold",
    "fng_extreme_greed_threshold", "fng_capitulation_multiplier",
    "fng_extreme_fear_multiplier", "fng_extreme_greed_multiplier",
    "fng_kelly_boost_enabled",
    "kelly_conviction_vol2x", "kelly_conviction_vol3x",
    "kelly_conviction_max_pct",
    "kelly_conviction_enabled",
    "stop_loss_pct", "take_profit_pct", "smart_tp_max_pnl_pct",
    "smart_tp_enabled", "smart_tp_max_extensions", "smart_tp_extend_score_threshold",
    "scale_out_enabled", "scale_out_first_close_pct",
    "futures_sl_pct", "futures_tp_pct",
    "futures_smart_tp_enabled", "futures_smart_tp_extend_score_threshold",
    "futures_max_new_per_cycle", "futures_confirm_red_candle",
    "futures_no_short_btc_4h_threshold", "futures_no_short_btc_3h_threshold", "futures_no_short_btc_1h_threshold",
    "futures_mass_sl_threshold", "futures_mass_sl_cooldown_hours",
    "min_signal_strength",
    "vol_spike_min",
    "futures_min_prob", "futures_vol_spike_min",
    "min_success_probability",
    "max_positions_spot_bull", "max_positions_range_scalp", "max_positions_preservation", "max_positions_futures_short",
    "portfolio_concentration_enabled", "portfolio_concentration_alpha",
    "portfolio_concentration_min_mult", "portfolio_concentration_max_mult",
    "portfolio_concentration_pressure_start", "portfolio_concentration_pressure_boost",
    "portfolio_concentration_baseline_spot_bull", "portfolio_concentration_baseline_range_scalp",
    "portfolio_concentration_baseline_preservation", "portfolio_concentration_baseline_futures_short",
    "max_total_exposure_pct", "max_total_exposure_spot_bull_fear",
    "min_hold_minutes", "min_hold_minutes_range_scalp",
    "max_rebalance_per_cycle",
    "rebalance_max_abs_pnl_pct",
    "min_hold_minutes_spot_bull", "min_hold_for_prediction_exit",
    "min_cycles_before_close_shorts",
    "futures_breakeven_activation_pct",
    "futures_trailing_activation_pct", "futures_sl_cooldown_hours",
    "futures_smart_tp_max_pnl_pct", "futures_smart_tp_max_extensions",
    "futures_bounce_rescue_enabled", "futures_bounce_rescue_max_peak_pct", "futures_bounce_rescue_min_hours", "futures_bounce_rescue_target_pnl",
    "spot_bounce_rescue_enabled", "spot_bounce_rescue_max_peak_pct", "spot_bounce_rescue_min_hours", "spot_bounce_rescue_target_pnl",
    "futures_momentum_reversal_enabled", "futures_momentum_reversal_min_peak", "futures_momentum_reversal_drawback",
    "futures_adverse_exit_enabled", "futures_adverse_exit_loss_pct", "futures_adverse_exit_min_hours", "futures_adverse_exit_min_strength",
    "profit_realization_enabled", "profit_realization_spot_enabled", "profit_realization_spot_min_profit_pct",
    "profit_realization_spot_partial_minutes", "profit_realization_spot_full_minutes", "profit_realization_spot_drawback_pct",
    "profit_realization_spot_partial_close_pct", "profit_realization_spot_max_extensions",
    "profit_realization_futures_enabled", "profit_realization_futures_min_profit_pct",
    "profit_realization_futures_partial_minutes", "profit_realization_futures_full_minutes", "profit_realization_futures_drawback_pct",
    "profit_realization_futures_partial_close_pct", "profit_realization_futures_max_extensions",
    "rescue_smart_gate_enabled", "rescue_stay_strength_min", "rescue_stay_ev_multiplier", "rescue_velocity_min_pct", "rescue_gate_min_concurrent",
    "rescue_gate_g1_min_pct", "rescue_gate_g1_spot_min_pct", "rescue_gate_vol_min_spike", "rescue_gate_max_blocks", "rescue_gate_min_pnl_improvement",
    "volspike_override_enabled", "volspike_override_min_spike", "volspike_override_max_rsi", "volspike_override_trend_required",
    "volspike_override_narrative_only", "volspike_override_min_strength", "volspike_override_max_positions", "volspike_override_daily_limit",
    "btc_dominance_filter_enabled", "btc_dominance_altcoin_threshold",
    "liquidity_filter_enabled", "min_liquidity_vol_usdt",
    "wr_screener_enabled", "wr_screener_consecutive_sl_block",
    "sso_target_daily_usd",
    "circuit_breaker_enabled", "circuit_breaker_consecutive_failures", "circuit_breaker_pause_minutes",
    "screener_min_vol_usdt", "screener_futures_min_vol",
    "screener_max_pairs", "screener_max_futures_pairs",
    "screener_cache_minutes", "screener_snapshot_max_age_minutes",
    "sso_slow_velocity_threshold", "sso_slow_min_hours",
    "sso_stagnant_min_hours", "sso_decay_min_hours",
    "sso_decay_threshold_pct",
    "sso_momentum_min_pnl_pct_spot", "sso_momentum_min_pnl_pct_futures",
    # SSO-1d: Flat Stagnation Timeout
    "sso_flat_stagnation_timeout_enabled",
    "sso_flat_stagnation_timeout_hours",
    "sso_flat_stagnation_timeout_hours_range_scalp",
    "sso_flat_stagnation_timeout_pnl_pct",
    "sso_flat_stagnation_timeout_pnl_pct_range_scalp",
    "slippage_pct",
    "use_bnb_for_commission",
    "hybrid_optimizer_enabled", "auto_apply_backtest", "backtest_safe_only",
    # s72: new per-regime fields — DB-governed, expected to differ from code defaults
    "range_scalp_max_rsi", "range_scalp_max_bb_position",
    "range_scalp_sl_pct", "range_scalp_tp_pct",
    "entry_quality_vol_spike_min_range_scalp",
    "min_hold_for_prediction_exit_futures_short",
    "sso_target_contribution_pct_range_scalp",  # s73 C2: DB-governed per profile
    # s75: category limit hot-config governance
    "category_max_positions_enabled",
}

_REGIME_MODES = {"SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"}
_ALLOWED_CATEGORY_FIELDS = {
    "min_prob", "min_pnl", "min_volspike",
    "stop_loss_pct", "take_profit_pct",
    "trailing_activation_pct", "trailing_distance_pct",
    "tp_extension_pct",
}
_SELECTOR_CONFIG_DEFAULTS = {
    "smart_total_enabled": True,
    "mode_selector_enabled": True,
    "mode_require_confirmed": True,
    "mode_min_hold_cycles": 2,
    "mode_min_confidence_delta": 0.03,
    "mode_audit_every_cycle": True,
    "profile_selector_enabled": True,
    "enabled": True,
    "min_hold_cycles": 3,
    "min_score_delta": 0.05,
    "profile_min_hold_cycles": 3,
    "profile_min_score_delta": 0.05,
    "audit_every_cycle": True,
    "profile_rank": {"A": 1000.0, "B": 200.0, "C": 100.0},
}


def _normalize_regime_mode(mode: Any) -> str:
    m = str(mode or "").strip().upper()
    if m not in _REGIME_MODES:
        raise ValueError(f"invalid regime '{mode}'")
    return m


def _normalize_profile_label(label: Any) -> str:
    p = str(label or "").strip().upper()
    if not p:
        raise ValueError("profile label is required")
    if len(p) > 24:
        raise ValueError("profile label too long (max 24)")
    return p


def _validate_profile_params(params: Any) -> Dict[str, Dict]:
    accepted: Dict[str, Any] = {}
    rejected: Dict[str, str] = {}
    if not isinstance(params, dict):
        return {"accepted": accepted, "rejected": {"params": "must be an object"}}

    for key, value in params.items():
        if key in ("category_thresholds", "futures_category_thresholds"):
            if not isinstance(value, dict):
                rejected[key] = "must be a dict"
                continue
            _normalized_cat = {}
            for cat, fields in value.items():
                if not isinstance(fields, dict):
                    rejected[f"{key}.{cat}"] = "must be a dict"
                    continue
                _cat_fields = {}
                for f, v in fields.items():
                    if f not in _ALLOWED_CATEGORY_FIELDS:
                        rejected[f"{key}.{cat}.{f}"] = "field not allowed"
                        continue
                    try:
                        _cat_fields[f] = float(v)
                    except Exception:
                        rejected[f"{key}.{cat}.{f}"] = "must be numeric"
                if _cat_fields:
                    _normalized_cat[str(cat)] = _cat_fields
            if _normalized_cat:
                accepted[key] = _normalized_cat
            continue

        if key not in _KELLY_EDITABLE_FIELDS:
            rejected[key] = "not in whitelist"
            continue
        accepted[key] = value

    return {"accepted": accepted, "rejected": rejected}


def _extract_regime_profile_key_parts(key: str) -> Optional[Dict[str, str]]:
    if not key:
        return None
    for mode in _REGIME_MODES:
        _active_prefix = f"regime_profile_active_{mode}"
        if key == _active_prefix:
            return {"kind": "active", "regime": mode, "profile": ""}
        _profile_prefix = f"regime_profile_{mode}_"
        if key.startswith(_profile_prefix):
            return {"kind": "profile", "regime": mode, "profile": key[len(_profile_prefix):]}
    return None


def _normalize_selector_config_payload(raw: Any) -> Dict[str, Any]:
    cfg = dict(_SELECTOR_CONFIG_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["smart_total_enabled"] = bool(cfg.get("smart_total_enabled", True))
    cfg["mode_selector_enabled"] = bool(cfg.get("mode_selector_enabled", True))
    cfg["mode_require_confirmed"] = bool(cfg.get("mode_require_confirmed", True))
    cfg["mode_audit_every_cycle"] = bool(cfg.get("mode_audit_every_cycle", True))
    cfg["profile_selector_enabled"] = bool(cfg.get("profile_selector_enabled", cfg.get("enabled", True)))
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["audit_every_cycle"] = bool(cfg.get("audit_every_cycle", True))
    try:
        cfg["mode_min_hold_cycles"] = max(0, int(float(cfg.get("mode_min_hold_cycles", 2))))
    except Exception:
        cfg["mode_min_hold_cycles"] = 2
    try:
        cfg["mode_min_confidence_delta"] = max(0.0, float(cfg.get("mode_min_confidence_delta", 0.03)))
    except Exception:
        cfg["mode_min_confidence_delta"] = 0.03
    try:
        cfg["min_hold_cycles"] = max(0, int(float(cfg.get("min_hold_cycles", 3))))
    except Exception:
        cfg["min_hold_cycles"] = 3
    try:
        cfg["min_score_delta"] = max(0.0, float(cfg.get("min_score_delta", 0.05)))
    except Exception:
        cfg["min_score_delta"] = 0.05
    try:
        cfg["profile_min_hold_cycles"] = max(
            0,
            int(float(cfg.get("profile_min_hold_cycles", cfg.get("min_hold_cycles", 3))))
        )
    except Exception:
        cfg["profile_min_hold_cycles"] = cfg["min_hold_cycles"]
    try:
        cfg["profile_min_score_delta"] = max(
            0.0,
            float(cfg.get("profile_min_score_delta", cfg.get("min_score_delta", 0.05)))
        )
    except Exception:
        cfg["profile_min_score_delta"] = cfg["min_score_delta"]
    _pr = cfg.get("profile_rank")
    if not isinstance(_pr, dict):
        _pr = {}
    _norm_pr = {}
    for k, v in _pr.items():
        _k = str(k or "").strip().upper()
        if not _k:
            continue
        try:
            _norm_pr[_k] = float(v)
        except Exception:
            continue
    if not _norm_pr:
        _norm_pr = dict(_SELECTOR_CONFIG_DEFAULTS["profile_rank"])
    cfg["profile_rank"] = _norm_pr
    return cfg


@app.get("/regime/profiles/selector-config")
async def get_regime_profile_selector_config(_auth=Depends(_require_api_key)) -> Dict:
    """Get current deterministic selector config + persisted per-regime selector state."""
    try:
        _cfg_rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='regime_profile_selector_config'"
        )
        _cfg_raw = _cfg_rows[0].get("value") if _cfg_rows else None
        if isinstance(_cfg_raw, str):
            try:
                _cfg_raw = json.loads(_cfg_raw)
            except Exception:
                _cfg_raw = {}
        _cfg = _normalize_selector_config_payload(_cfg_raw if isinstance(_cfg_raw, dict) else {})

        _state_rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state "
            "WHERE key LIKE 'regime_profile_selector_state_%%' OR key LIKE 'smart_total_profile_selector_state_%%' "
            "OR key LIKE 'regime_profile_active_%%' OR key='smart_total_mode_selector_state' OR key='smart_total_active_mode' "
            "ORDER BY key"
        )
        _state: Dict[str, Any] = {
            "mode_selector": {"selector_state": None, "active_mode": None},
            "profiles": {m: {"selector_state": None, "active_profile": None} for m in _REGIME_MODES},
        }
        for r in _state_rows:
            _key = str(r.get("key") or "")
            _value = r.get("value")
            if isinstance(_value, str):
                try:
                    _value = json.loads(_value)
                except Exception:
                    _value = {"raw": _value}
            if _key == "smart_total_mode_selector_state":
                _state["mode_selector"]["selector_state"] = {
                    "value": _value,
                    "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                }
                continue
            if _key == "smart_total_active_mode":
                _state["mode_selector"]["active_mode"] = {
                    "value": _value,
                    "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                }
                continue
            for _mode in _REGIME_MODES:
                _sk = f"regime_profile_selector_state_{_mode}"
                _sk2 = f"smart_total_profile_selector_state_{_mode}"
                _ak = f"regime_profile_active_{_mode}"
                if _key == _sk or _key == _sk2:
                    _state["profiles"][_mode]["selector_state"] = {
                        "value": _value,
                        "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                    }
                elif _key == _ak:
                    _state["profiles"][_mode]["active_profile"] = {
                        "value": _value,
                        "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                    }

        return {
            "ok": True,
            "config": _cfg,
            "state": _state,
            "updated_at": _cfg_rows[0].get("updated_at").isoformat() if _cfg_rows and _cfg_rows[0].get("updated_at") else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/regime/profiles/selector-config")
async def post_regime_profile_selector_config(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Set deterministic selector config (hysteresis, ranking, audit cadence) with atomic persistence + audit."""
    try:
        _cfg = _normalize_selector_config_payload(body or {})
        _cfg_json = json.dumps(_cfg)
        _snapshot_id = _create_config_snapshot(get_components().settings)
        _detail = {
            "config": _cfg,
            "snapshot_id": _snapshot_id,
            "changed_by": str(_auth) if _auth else None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        db_client.execute_transaction([
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES ('regime_profile_selector_config', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                (_cfg_json, _cfg_json),
            ),
            (
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                "VALUES ('__config__', 'system', 'regime_profile_selector_config', %s, false)",
                (json.dumps(_detail),),
            ),
        ])
        _log_config_change(
            source="regime_profile_selector_config",
            changed_by=str(_auth) if _auth else None,
            snapshot_id=_snapshot_id,
            diff={"selector_config": _cfg},
            full_config=None,
        )
        return {
            "ok": True,
            "config": _cfg,
            "snapshot_id": _snapshot_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/regime/profiles/selector-decisions")
async def get_regime_profile_selector_decisions(limit: int = 50, _auth=Depends(_require_api_key)) -> Dict:
    """Read the latest auto-selector decisions from trades_audit."""
    try:
        _limit = max(1, min(int(limit), 500))
        _rows = db_client.execute_query(
            "SELECT id, event, detail, created_at FROM trades_audit "
            "WHERE event IN ('regime_profile_selector_decision', 'smart_total_mode_selector_decision') "
            "ORDER BY id DESC LIMIT %s",
            (_limit,)
        )
        _decisions = []
        for r in _rows:
            _detail = r.get("detail")
            if isinstance(_detail, str):
                try:
                    _detail = json.loads(_detail)
                except Exception:
                    _detail = {"raw": _detail}
            _decisions.append({
                "id": r.get("id"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "detail": _detail,
            })
        return {"ok": True, "decisions": _decisions, "count": len(_decisions)}
    except Exception as e:
        return {"ok": False, "error": str(e), "decisions": []}


@app.get("/regime/profiles")
async def get_regime_profiles(regime: Optional[str] = None, _auth=Depends(_require_api_key)) -> Dict:
    """List stored regime profiles and active profile pointers (SPOT+FUTURES unified)."""
    try:
        _regime_filter = _normalize_regime_mode(regime) if regime else None
        rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state WHERE key LIKE 'regime_profile_%%' ORDER BY key"
        )
        result: Dict[str, Dict[str, Any]] = {}
        for mode in _REGIME_MODES:
            if _regime_filter and mode != _regime_filter:
                continue
            result[mode] = {"active": None, "profiles": {}}

        for r in rows:
            parts = _extract_regime_profile_key_parts(str(r.get("key") or ""))
            if not parts:
                continue
            mode = parts["regime"]
            if _regime_filter and mode != _regime_filter:
                continue
            payload = r.get("value")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"raw": payload}
            if parts["kind"] == "active":
                result[mode]["active"] = {
                    "profile": (payload or {}).get("profile") if isinstance(payload, dict) else None,
                    "payload": payload,
                    "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                }
            else:
                label = parts["profile"]
                result[mode]["profiles"][label] = {
                    "payload": payload,
                    "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
                }

        return {"ok": True, "regimes": result, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/regime/profiles")
async def post_regime_profile(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Create or update a named profile under a specific regime."""
    try:
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings

        _regime = _normalize_regime_mode(body.get("regime"))
        _profile = _normalize_profile_label(body.get("profile"))
        _validation = _validate_profile_params(body.get("params", {}))
        _accepted = _validation["accepted"]
        _rejected = _validation["rejected"]

        if not _accepted:
            return {"ok": False, "error": "No valid params in profile", "rejected": _rejected}

        _snapshot_id = _create_full_config_snapshot(_s)
        _meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        _payload = {
            "regime": _regime,
            "profile": _profile,
            "params": _accepted,
            "meta": {
                **_meta,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_by": str(_auth) if _auth else None,
                "snapshot_id": _snapshot_id,
            },
        }
        _key = f"regime_profile_{_regime}_{_profile}"
        _payload_json = json.dumps(_payload)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (_key, _payload_json, _payload_json)
        )

        _diff = {f"profile.{k}": {"old": None, "new": v} for k, v in _accepted.items()}
        _log_config_change(
            source="regime_profile_save",
            changed_by=str(_auth) if _auth else None,
            snapshot_id=_snapshot_id,
            diff={"regime": _regime, "profile": _profile, "changes": _diff},
            full_config=None,
        )
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__config__', 'system', 'regime_profile_saved', %s, false)",
            (json.dumps({
                "regime": _regime,
                "profile": _profile,
                "snapshot_id": _snapshot_id,
                "accepted": _accepted,
                "rejected": _rejected,
            }),)
        )

        return {
            "ok": True,
            "regime": _regime,
            "profile": _profile,
            "snapshot_id": _snapshot_id,
            "accepted": _accepted,
            "rejected": _rejected,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/regime/profiles/activate")
async def post_activate_regime_profile(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Activate a profile for a regime, with audit trail and optional immediate apply."""
    try:
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings
        _regime = _normalize_regime_mode(body.get("regime"))
        _profile = _normalize_profile_label(body.get("profile"))
        _reason = str(body.get("reason") or "manual_switch")

        _profile_key = f"regime_profile_{_regime}_{_profile}"
        _rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s", (_profile_key,)
        )
        if not _rows or _rows[0].get("value") is None:
            return {"ok": False, "error": f"Profile {_profile} for {_regime} not found"}

        _snapshot_id = _create_full_config_snapshot(_s)
        _active_key = f"regime_profile_active_{_regime}"
        _updated_at = datetime.now(timezone.utc).isoformat()
        _active_payload = {
            "regime": _regime,
            "profile": _profile,
            "reason": _reason,
            "updated_at": _updated_at,
            "updated_by": str(_auth) if _auth else None,
            "snapshot_id": _snapshot_id,
        }
        _active_json = json.dumps(_active_payload)

        # Restart-safe runtime snapshots required by rollout closeout gates.
        _runtime_selection_payload = {
            "selected_mode": _regime,
            "selected_profile": _profile,
            "source": "regime_profile_activate",
            "reason": _reason,
            "snapshot_id": _snapshot_id,
            "updated_at": _updated_at,
        }
        _runtime_selection_json = json.dumps(_runtime_selection_payload)

        _transition_payload = {
            "active": False,
            "from_mode": None,
            "to_mode": _regime,
            "from_profile": None,
            "to_profile": _profile,
            "reason": _reason,
            "source": "regime_profile_activate",
            "snapshot_id": _snapshot_id,
            "updated_at": _updated_at,
        }
        _transition_json = json.dumps(_transition_payload)

        db_client.execute_transaction([
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                (_active_key, _active_json, _active_json),
            ),
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ("effective_runtime_selection_snapshot", _runtime_selection_json, _runtime_selection_json),
            ),
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ("regime_transition_state", _transition_json, _transition_json),
            ),
            (
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                "VALUES ('__config__', 'system', 'regime_profile_activated', %s, false)",
                (json.dumps({
                    "regime": _regime,
                    "profile": _profile,
                    "reason": _reason,
                    "snapshot_id": _snapshot_id,
                }),),
            ),
        ])

        _apply_now = bool(body.get("apply_now", True))
        _applied = {"skipped": True, "reason": "not_requested"}
        if _apply_now:
            try:
                import market_regime as _mr
                _current = _mr.get_regime(_comp.binance_client, settings=_s)
                if getattr(_current, "confirmed", False) and getattr(_current, "mode", None) == _regime:
                    _applied = run_trades._apply_regime_params(_s, _regime, db_client)
                else:
                    _applied = {
                        "skipped": True,
                        "reason": "regime_not_active_or_unconfirmed",
                        "current_mode": getattr(_current, "mode", None),
                        "current_confirmed": bool(getattr(_current, "confirmed", False)),
                    }
            except Exception as _ae:
                _applied = {"skipped": True, "reason": f"apply_failed: {_ae}"}

        # Clear screener cache after any profile activation so next cycle uses fresh profile-scoped filters.
        try:
            from pair_screener import invalidate_cache as _invalidate_screener_cache
            _invalidate_screener_cache()
        except Exception:
            pass

        _log_config_change(
            source="regime_profile_activate",
            changed_by=str(_auth) if _auth else None,
            snapshot_id=_snapshot_id,
            diff={"regime": _regime, "profile": _profile, "reason": _reason, "apply": _applied},
            full_config=None,
        )

        return {
            "ok": True,
            "regime": _regime,
            "profile": _profile,
            "snapshot_id": _snapshot_id,
            "apply": _applied,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _apply_kelly_config_patch(body: Dict[str, Any]) -> Dict[str, Any]:
    """Core logic reused by PATCH /config/kelly and Super MIMIC deploy.

    Applies only whitelisted fields, updates in-memory settings, persists to
    trading_runtime_state and writes a trades_audit entry.
    """
    from init_components import get_components
    _comp = get_components()
    _s = _comp.settings

    applied = {}
    rejected = {}

    _ALLOWED_CAT_FIELDS = {"min_prob", "min_pnl", "min_volspike",
                           "stop_loss_pct", "take_profit_pct",
                           "trailing_activation_pct", "trailing_distance_pct",
                           "tp_extension_pct"}

    def _apply_category_thresholds(attr_name: str, cat_patch: Dict):
        """Partial merge of category thresholds for SPOT or FUTURES."""
        if not isinstance(cat_patch, dict):
            rejected[attr_name] = "value must be a dict"
            return
        _current_ct = dict(getattr(_s, attr_name, {}))
        _merged = {}
        for cat, fields in cat_patch.items():
            if not isinstance(fields, dict):
                rejected[f"{attr_name}.{cat}"] = "value must be a dict"
                continue
            _cat_old = dict(_current_ct.get(cat, {}))
            for cf, cv in fields.items():
                if cf not in _ALLOWED_CAT_FIELDS:
                    rejected[f"{attr_name}.{cat}.{cf}"] = "field not allowed"
                    continue
                _cat_old[cf] = float(cv)
                _merged[f"{cat}.{cf}"] = {"old": _current_ct.get(cat, {}).get(cf), "new": float(cv)}
            _current_ct[cat] = _cat_old
        if _merged:
            setattr(_s, attr_name, _current_ct)
            applied[attr_name] = {"merged": _merged}

    # Special handlers: category thresholds for SPOT and FUTURES
    if "category_thresholds" in body:
        _apply_category_thresholds("category_thresholds", body.pop("category_thresholds"))
    if "futures_category_thresholds" in body:
        _apply_category_thresholds("futures_category_thresholds", body.pop("futures_category_thresholds"))

    for field, value in body.items():
        if field not in _KELLY_EDITABLE_FIELDS:
            rejected[field] = "not in whitelist"
            continue
        try:
            _current = getattr(_s, field, None)
            setattr(_s, field, type(_current)(value) if _current is not None else value)
            applied[field] = {"old": _current, "new": value}
        except Exception as _fe:
            rejected[field] = str(_fe)

    if not applied:
        return {"ok": False, "error": "No valid fields applied", "rejected": rejected}

    # Persist to trading_runtime_state — MERGE into existing override so all values survive restart
    import json as _json
    _existing_ov = {}
    try:
        _ex_rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
        )
        if _ex_rows and _ex_rows[0].get('value'):
            _ex_raw = _ex_rows[0]['value']
            if isinstance(_ex_raw, str):
                _ex_raw = _json.loads(_ex_raw)
            if isinstance(_ex_raw, dict):
                _existing_ov = _ex_raw
    except Exception:
        pass
    _existing_ov.update(applied)
    _payload = _json.dumps(_existing_ov)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) "
        "VALUES ('kelly_config_override', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (_payload, _payload)
    )

    # Persist category_thresholds separately so it can be reloaded on restart
    for _ct_key in ("category_thresholds", "futures_category_thresholds"):
        if _ct_key in applied:
            _ct_payload = _json.dumps(getattr(_s, _ct_key, {}))
            _state_key = f"{_ct_key}_override"
            db_client.execute_update(
                "INSERT INTO trading_runtime_state (key, value, updated_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                (_state_key, _ct_payload, _ct_payload)
            )

    # Trazabilidad: trades_audit entry
    db_client.execute_update(
        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
        "VALUES ('__config__', 'system', 'kelly_config_change', %s, false)",
        (_json.dumps({"applied": applied, "rejected": rejected}),)
    )

    return {
        "ok": True,
        "applied": applied,
        "rejected": rejected,
        "message": f"{len(applied)} campos actualizados — activos inmediatamente (sin restart)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.patch("/config/kelly")
async def patch_kelly_config(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """
    C2: Hot-config — update Kelly parameters without restart.
    Only whitelisted fields are accepted. Changes persisted in trading_runtime_state
    and recorded in trades_audit for 100% traceability.
    """
    try:
        return _apply_kelly_config_patch(body)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/config/commission")
async def get_commission_status(_auth=Depends(_require_api_key)) -> Dict:
    """
    Return current commission configuration and BNB discount status.
    Also triggers a BNB balance check if auto_bnb_for_commission is enabled.
    """
    try:
        from init_components import get_components
        from run_trades import _get_futures_client
        from binance_commission import get_config, auto_manage_bnb_discount
        _comp = get_components()
        _s = _comp.settings
        _cfg = get_config(_s)
        _bnb_status = auto_manage_bnb_discount(
            settings=_s,
            spot_client=_comp.binance_client,
            futures_client=_get_futures_client(_s),
            db_client=db_client,
            regime_mode=getattr(_s, 'current_regime_mode', 'SPOT_BULL'),
        )
        return {
            "ok": True,
            "spot_rate": _cfg.spot_rate,
            "futures_rate": _cfg.futures_rate,
            "bnb_spot_rate": _cfg.bnb_spot_rate,
            "bnb_futures_rate": _cfg.bnb_futures_rate,
            "use_bnb": _bnb_status.get('use_bnb', _cfg.use_bnb),
            "slippage_pct": _cfg.slippage_pct,
            "bnb_status": _bnb_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/dashboard/commission")
async def get_dashboard_commission(_auth=Depends(_require_api_key)) -> Dict:
    """
    Return commission/BNB data formatted for dashboard consumption.
    Includes recent commission totals from trades_closed for visibility.
    """
    try:
        from init_components import get_components
        from run_trades import _get_futures_client
        from binance_commission import get_config, auto_manage_bnb_discount
        _comp = get_components()
        _s = _comp.settings
        _cfg = get_config(_s)
        _bnb_status = auto_manage_bnb_discount(
            settings=_s,
            spot_client=_comp.binance_client,
            futures_client=_get_futures_client(_s),
            db_client=db_client,
            regime_mode=getattr(_s, 'current_regime_mode', 'SPOT_BULL'),
        )

        # Recent commission totals from trades_closed (today + 7d)
        # NOTE: trades_closed does not currently store trade_type. We show total paid
        # and split by a simple heuristic: positions whose symbol is in futures_symbols
        # are counted as FUTURES; everything else as SPOT.
        _totals = {"spot": {"today": 0.0, "7d": 0.0}, "futures": {"today": 0.0, "7d": 0.0}}
        try:
            _futures_symbols = set(_cfg_param(_s, 'futures_symbols') or [])
            rows = db_client.execute_query("""
                SELECT symbol,
                    COALESCE(SUM(CASE WHEN exit_time >= NOW() - INTERVAL '1 day' THEN COALESCE(entry_commission,0) + COALESCE(exit_commission,0) ELSE 0 END), 0) as today,
                    COALESCE(SUM(CASE WHEN exit_time >= NOW() - INTERVAL '7 days' THEN COALESCE(entry_commission,0) + COALESCE(exit_commission,0) ELSE 0 END), 0) as d7
                FROM trades_closed
                WHERE exit_time >= NOW() - INTERVAL '7 days'
                GROUP BY symbol
            """)
            for r in rows:
                sym = r.get('symbol', '')
                is_futures = sym in _futures_symbols
                key = 'futures' if is_futures else 'spot'
                _totals[key]['today'] += float(r.get('today', 0) or 0)
                _totals[key]['7d'] += float(r.get('d7', 0) or 0)
        except Exception as _e:
            _totals["error"] = str(_e)

        return {
            "ok": True,
            "spot_rate": _cfg.spot_rate,
            "futures_rate": _cfg.futures_rate,
            "bnb_spot_rate": _cfg.bnb_spot_rate,
            "bnb_futures_rate": _cfg.bnb_futures_rate,
            "use_bnb": _bnb_status.get('use_bnb', _cfg.use_bnb),
            "auto_bnb_enabled": getattr(_s, 'auto_bnb_for_commission', True),
            "slippage_pct": _cfg.slippage_pct,
            "bnb_status": _bnb_status,
            "bnb_reserve_usdt": _bnb_status.get('total_reserve_usdt', 0.0),
            "commission_totals": _totals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/dashboard/screener")
async def get_dashboard_screener(_auth=Depends(_require_api_key)) -> Dict:
    """
    Return the latest regime-aware pair screening decision for dashboard consumption.
    """
    try:
        from pair_screener import get_screened_pairs
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings
        _regime = getattr(_s, 'current_regime_mode', 'SPOT_BULL')
        _profile = _get_active_profile_summary(_regime).get("label")
        _screened = get_screened_pairs(
            settings=_s,
            regime_mode=_regime,
            profile_label=_profile,
            db_client=_comp.db_client,
        )
        _analysis_universe = select_analysis_universe(
            settings=_s,
            regime_mode=_regime,
            profile_label=_profile,
            db_client=_comp.db_client,
        )
        return {
            "ok": True,
            "regime_mode": _regime,
            "profile_label": _profile,
            "spot_count": len(_screened.get("spot", [])),
            "futures_count": len(_screened.get("futures", [])),
            "spot": _screened.get("spot", []),
            "futures": _screened.get("futures", []),
            "top_bullish": _screened.get("_meta", {}).get("top_bullish", []),
            "top_bearish": _screened.get("_meta", {}).get("top_bearish", []),
            "fetched_at": _screened.get("_meta", {}).get("fetched_at"),
            "analysis_universe": {
                "symbols": _analysis_universe.get("symbols", []),
                "source": _analysis_universe.get("source"),
                "reason": _analysis_universe.get("reason"),
                "cap": _analysis_universe.get("cap"),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/screener/decisions")
async def get_screener_decisions(
    limit: int = 50,
    regime_mode: str = None,
    acknowledged: bool = None,
    _auth=Depends(_require_api_key)
) -> Dict:
    """
    Return the last N screener decisions persisted to screener_decisions.
    Optional filters: regime_mode, acknowledged.
    """
    try:
        _where = []
        _params = []
        if regime_mode:
            _where.append("regime_mode = %s")
            _params.append(regime_mode)
        if acknowledged is not None:
            _where.append("rejected_reason IS " + ("NOT NULL" if acknowledged else "NULL"))
        _sql = "SELECT * FROM screener_decisions"
        if _where:
            _sql += " WHERE " + " AND ".join(_where)
        _sql += " ORDER BY created_at DESC LIMIT %s"
        _params.append(limit)
        rows = db_client.execute_query(_sql, tuple(_params))
        return {
            "ok": True,
            "count": len(rows),
            "decisions": [
                {
                    "id": r.get("id"),
                    "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                    "regime_mode": r.get("regime_mode"),
                    "spot_symbols": r.get("spot_symbols") if isinstance(r.get("spot_symbols"), list) else json.loads(r.get("spot_symbols") or "[]"),
                    "futures_symbols": r.get("futures_symbols") if isinstance(r.get("futures_symbols"), list) else json.loads(r.get("futures_symbols") or "[]"),
                    "scores_json": r.get("scores_json") if isinstance(r.get("scores_json"), dict) else json.loads(r.get("scores_json") or "{}"),
                    "rejected_reason": r.get("rejected_reason"),
                }
                for r in rows
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "decisions": []}


@app.get("/config/kelly/log")
async def get_kelly_log(limit: int = 50, trade_type: str = None,
                        _auth=Depends(_require_api_key)) -> Dict:
    """
    C3: Return last N Kelly sizing decisions from kelly_sizing_log.
    Filterable by trade_type (spot/futures).
    """
    try:
        _where = "WHERE trade_type = %s" if trade_type else ""
        _params = (trade_type, limit) if trade_type else (limit,)
        rows = db_client.execute_query(
            f"SELECT ts, symbol, trade_type, regime, prob, strength, tier, "
            f"base_usdt, sized_usdt, final_usdt, multipliers, liquid_usdt, cap_usdt, action "
            f"FROM kelly_sizing_log {_where} ORDER BY ts DESC LIMIT %s",
            _params
        )
        return {
            "ok": True,
            "count": len(rows),
            "log": [
                {**r, "ts": r["ts"].isoformat() if r.get("ts") else None}
                for r in rows
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "log": []}


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


# ── Backtest Optimizer endpoints ──────────────────────────────────────────────

@app.get("/backtest/runs")
async def get_backtest_runs(
    limit: int = 50,
    only_baseline: bool = False,
    _auth=Depends(_require_api_key)
) -> Dict:
    """List backtest optimization runs sorted by PnL."""
    try:
        where = "WHERE is_baseline = TRUE" if only_baseline else ""
        rows = db_client.execute_query(
            f"""SELECT id, label, ts, window_start, window_end,
                       n_trades, n_spot, n_futures, n_wins, n_losses,
                       pnl_total, pnl_spot, pnl_futures,
                       win_rate, profit_factor, sharpe, max_drawdown,
                       capital_start, capital_end, return_pct,
                       is_baseline, rank_pnl, duration_ms
                FROM backtest_run {where}
                ORDER BY pnl_total DESC
                LIMIT %s""",
            (limit,)
        )
        baseline = db_client.execute_query(
            "SELECT id, label, pnl_total, return_pct, win_rate, sharpe, n_trades, is_baseline "
            "FROM backtest_run WHERE is_baseline = TRUE ORDER BY ts DESC LIMIT 1"
        )
        total_count = db_client.execute_query("SELECT COUNT(*) AS c FROM backtest_run")
        return {
            "ok": True,
            "total_runs": total_count[0]["c"] if total_count else 0,
            "baseline": baseline[0] if baseline else None,
            "runs": rows,
            "ts": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/backtest/runs/{run_id}")
async def get_backtest_run_detail(
    run_id: int,
    limit_trades: int = 100,
    _auth=Depends(_require_api_key)
) -> Dict:
    """Get detailed info for a specific backtest run including its trades."""
    try:
        run = db_client.execute_query(
            "SELECT * FROM backtest_run WHERE id = %s", (run_id,)
        )
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        trades = db_client.execute_query(
            """SELECT symbol, trade_type, side, entry_ts, exit_ts,
                      entry_price, exit_price, usdt_size, pnl_usdt, pnl_pct,
                      tier, regime, close_reason, kelly_mult
               FROM backtest_trade
               WHERE run_id = %s
               ORDER BY entry_ts
               LIMIT %s""",
            (run_id, limit_trades)
        )
        return {
            "ok": True,
            "run": run[0],
            "trades": trades,
            "ts": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/backtest/best")
async def get_backtest_best(_auth=Depends(_require_api_key)) -> Dict:
    """Return best config found vs baseline, with parameter diff and apply suggestion."""
    try:
        best = db_client.execute_query(
            """SELECT id, label, params, pnl_total, return_pct, win_rate,
                      profit_factor, sharpe, max_drawdown, n_trades, rank_pnl
               FROM backtest_run
               WHERE is_baseline = FALSE
               ORDER BY pnl_total DESC
               LIMIT 1"""
        )
        baseline = db_client.execute_query(
            """SELECT id, label, params, pnl_total, return_pct, win_rate,
                      profit_factor, sharpe, max_drawdown, n_trades
               FROM backtest_run
               WHERE is_baseline = TRUE
               ORDER BY ts DESC
               LIMIT 1"""
        )
        if not best or not baseline:
            return {"ok": False, "detail": "No backtest runs found. Run backtest_optimizer.py first."}

        best_params    = best[0]["params"] if isinstance(best[0]["params"], dict) else {}
        base_params    = baseline[0]["params"] if isinstance(baseline[0]["params"], dict) else {}

        # Build diff
        diff = {}
        for key in best_params:
            bv = base_params.get(key)
            ov = best_params.get(key)
            if bv is not None and ov is not None:
                diff[key] = {"baseline": bv, "optimal": ov, "delta": round(float(ov) - float(bv), 6)}

        # All params from diff become the full patch suggestion
        # Mapping: backtest param name -> Settings field name + scale factor + safety rules
        # min/max are in BACKTEST raw scale (decimal), not Settings scale
        _PARAM_MAP = {
            "kelly_base_usdt":        {"field": "kelly_base_usdt",        "scale": 1.0,   "min": 50.0,   "max": 300.0},
            "kelly_max_usdt":         {"field": "kelly_max_usdt",         "scale": 1.0,   "min": 200.0,  "max": 1200.0},
            "kelly_max_pct_liquid":   {"field": "kelly_max_pct_liquid",   "scale": 1.0,   "min": 0.20,   "max": 0.90},
            "futures_kelly_max_usdt": {"field": "futures_kelly_max_usdt", "scale": 1.0,   "min": 300.0,  "max": 2000.0},
            "futures_sl_pct":         {"field": "futures_sl_pct",         "scale": 100.0, "min": 0.010,  "max": 0.050},
            "futures_tp_pct":         {"field": "futures_tp_pct",         "scale": 100.0, "min": 0.020,  "max": 0.100},
            "spot_sl_pct":            {"field": "stop_loss_pct",          "scale": 100.0, "min": 0.010,  "max": 0.040},
            "spot_tp_pct":            {"field": "take_profit_pct",        "scale": 100.0, "min": 0.018,  "max": 0.080},
            "min_strength":           {"field": "min_signal_strength",      "scale": 1.0,   "min": 0.40,   "max": 0.90},
            "min_prob":               {"field": "min_prob",                 "scale": 1.0,   "min": 0.45,   "max": 0.80},
            "vol_spike_min":          {"field": "vol_spike_min",            "scale": 1.0,   "min": 1.0,    "max": 3.0},
        }
        # Safety: params that are locked from auto-apply unless optimal value is within safe range
        # jun28: relaxed after Backtest 1000 validated run_0812 as baseline=best
        _UNSAFE = set()
        _NEVER_AUTO = set()

        def _build_param_patch(bt_key: str, mapping: Dict) -> Dict:
            if bt_key not in best_params:
                return None
            _raw = float(best_params[bt_key])
            _scaled = round(_raw * mapping["scale"], 6)
            _is_safe = mapping["min"] <= _raw <= mapping["max"]
            return {
                "field": mapping["field"],
                "value": _scaled,
                "raw": _raw,
                "safe": _is_safe,
                "unsafe": bt_key in _UNSAFE,
                "auto_apply": bt_key not in _NEVER_AUTO and _is_safe,
            }

        kelly_patch = {}
        for bt_key, mapping in _PARAM_MAP.items():
            _patch = _build_param_patch(bt_key, mapping)
            if _patch:
                kelly_patch[bt_key] = _patch

        # Build category patch suggestions for SPOT and FUTURES
        from init_components import get_components as _gc
        _live_s = _gc().settings
        _opt_min_prob = float(best_params.get("min_prob", 0))
        _opt_spot_tp  = round(float(best_params.get("spot_tp_pct", 0)) * 100.0, 4)
        _opt_fut_tp   = round(float(best_params.get("futures_tp_pct", 0)) * 100.0, 4)
        _opt_fut_sl   = round(float(best_params.get("futures_sl_pct", 0)) * 100.0, 4)

        _ALLOWED_CAT_FIELDS = {"min_prob", "stop_loss_pct", "take_profit_pct",
                               "trailing_activation_pct", "trailing_distance_pct"}

        def _build_cat_patch(live_ct: Dict, cat_names: List[str], opt_min_prob: float,
                             opt_tp: float, opt_sl: float = None) -> Dict:
            patch = {}
            for _cat in cat_names:
                _ct = live_ct.get(_cat, {})
                _changes = {}
                if opt_min_prob > 0 and float(_ct.get("min_prob", 0)) < opt_min_prob and "min_prob" in _ALLOWED_CAT_FIELDS:
                    _changes["min_prob"] = {"current": _ct.get("min_prob"), "optimal": round(opt_min_prob, 4)}
                if opt_tp > 0 and float(_ct.get("take_profit_pct", 0)) < opt_tp and "take_profit_pct" in _ALLOWED_CAT_FIELDS:
                    _changes["take_profit_pct"] = {"current": _ct.get("take_profit_pct"), "optimal": round(opt_tp, 4)}
                if opt_sl is not None and opt_sl > 0 and float(_ct.get("stop_loss_pct", 0)) < opt_sl and "stop_loss_pct" in _ALLOWED_CAT_FIELDS:
                    _changes["stop_loss_pct"] = {"current": _ct.get("stop_loss_pct"), "optimal": round(opt_sl, 4)}
                if _changes:
                    patch[_cat] = _changes
            return patch

        cat_patch = _build_cat_patch(
            getattr(_live_s, "category_thresholds", {}) or {},
            ["LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"],
            _opt_min_prob, _opt_spot_tp
        )
        futures_cat_patch = _build_cat_patch(
            getattr(_live_s, "futures_category_thresholds", {}) or {},
            list(getattr(_live_s, "futures_category_thresholds", {}).keys()) or ["LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"],
            _opt_min_prob, _opt_fut_tp, _opt_fut_sl
        )

        return {
            "ok": True,
            "best": best[0],
            "baseline": baseline[0],
            "param_diff": diff,
            "kelly_patch_suggestion": kelly_patch,
            "category_patch_suggestion": cat_patch,
            "futures_category_patch_suggestion": futures_cat_patch,
            "unsafe_params": list(_UNSAFE),
            "never_auto_apply": list(_NEVER_AUTO),
            "improvement_usdt": round(float(best[0]["pnl_total"]) - float(baseline[0]["pnl_total"]), 2),
            "improvement_pct":  round(float(best[0]["return_pct"]) - float(baseline[0]["return_pct"]), 2),
            "spot_vs_futures": {
                "pnl_spot": round(float(best[0].get("pnl_spot", 0)), 2),
                "pnl_futures": round(float(best[0].get("pnl_futures", 0)), 2),
            },
            "ts": to_mexico_time(datetime.utcnow()).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Config snapshots + rollback helpers ─────────────────────────────────────

def _create_config_snapshot(settings) -> str:
    """Create a point-in-time snapshot of current editable config and return snapshot_id."""
    import json as _json
    _snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scalars": {field: getattr(settings, field, None) for field in _KELLY_EDITABLE_FIELDS},
        "category_thresholds": getattr(settings, "category_thresholds", {}),
        "futures_category_thresholds": getattr(settings, "futures_category_thresholds", {}),
    }
    _snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    _payload = _json.dumps(_snapshot)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (f"config_snapshot_{_snapshot_id}", _payload, _payload)
    )
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES ('config_snapshot_latest', %s::jsonb, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s::jsonb, updated_at=NOW()",
        (json.dumps(_snapshot_id), json.dumps(_snapshot_id))
    )
    db_client.execute_update(
        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
        "VALUES ('__config__', 'system', 'config_snapshot_created', %s, false)",
        (_json.dumps({"snapshot_id": _snapshot_id}),)
    )
    return _snapshot_id


def _restore_config_snapshot(snapshot_id: str, settings) -> Dict:
    """Restore editable config from a snapshot."""
    import json as _json
    _rows = db_client.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (f"config_snapshot_{snapshot_id}",)
    )
    if not _rows or not _rows[0].get("value"):
        return {"ok": False, "error": f"Snapshot {snapshot_id} not found"}
    _snap = _rows[0]["value"]
    if isinstance(_snap, str):
        _snap = _json.loads(_snap)
    _restored = {}
    _rejected = {}
    for field, value in _snap.get("scalars", {}).items():
        if value is None or field not in _KELLY_EDITABLE_FIELDS:
            continue
        try:
            _cur = getattr(settings, field, None)
            setattr(settings, field, type(_cur)(value) if _cur is not None else value)
            _restored[field] = value
        except Exception as _e:
            _rejected[field] = str(_e)
    for _ct_attr in ("category_thresholds", "futures_category_thresholds"):
        if _ct_attr in _snap:
            setattr(settings, _ct_attr, _snap[_ct_attr])
            _restored[_ct_attr] = "restored"
    # Persist restored state as current override
    _payload = _json.dumps({"restored": _restored, "snapshot_id": snapshot_id})
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES ('kelly_config_override', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (_payload, _payload)
    )
    db_client.execute_update(
        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
        "VALUES ('__config__', 'system', 'config_rollback', %s, false)",
        (_json.dumps({"snapshot_id": snapshot_id, "restored": _restored, "rejected": _rejected}),)
    )
    return {"ok": True, "restored": _restored, "rejected": _rejected, "snapshot_id": snapshot_id}


def _log_config_change(
    source: str,
    changed_by: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    diff: Optional[Dict] = None,
    full_config: Optional[Dict] = None,
) -> None:
    """Persist a configuration change row for 100% traceability."""
    try:
        db_client.execute_update(
            "INSERT INTO config_change_log (source, changed_by, snapshot_id, diff, full_config) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                source,
                changed_by,
                snapshot_id,
                json.dumps(diff or {}),
                json.dumps(full_config) if full_config is not None else None,
            ),
        )
    except Exception as e:
        print(f"[ConfigGovernance] failed to log config change: {e}")


def _create_full_config_snapshot(settings) -> str:
    """Create a point-in-time snapshot of the full runtime config."""
    _snapshot = dump_active_config(settings)
    _snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    _payload = json.dumps(_snapshot)
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (f"config_snapshot_{_snapshot_id}", _payload, _payload),
    )
    db_client.execute_update(
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES ('config_snapshot_latest', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (json.dumps(_snapshot_id), json.dumps(_snapshot_id)),
    )
    _log_config_change(
        source="snapshot",
        snapshot_id=_snapshot_id,
        diff={},
        full_config=_snapshot,
    )
    return _snapshot_id


async def _config_snapshot_loop() -> None:
    """Background task: save a full config snapshot every 15 minutes."""
    while True:
        try:
            await asyncio.sleep(900)
            _comp = get_components()
            if _comp and _comp.settings:
                _sid = _create_full_config_snapshot(_comp.settings)
                print(f"[ConfigGovernance] periodic snapshot created: {_sid}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[ConfigGovernance] periodic snapshot failed: {e}")


_BACKTEST_BASELINE: Dict[str, Any] = {
    'kelly_base_usdt': 150.0,           # concentrated: base digna para $5,100
    'kelly_max_usdt': 1500.0,           # concentrated: cap por posición elevado
    'kelly_max_pct_liquid': 0.85,       # concentrated: hasta 85% del líquido
    'kelly_max_pct_cap': 0.30,
    'max_per_trade_usdt': 1500.0,
    'futures_leverage': 10,
    'futures_max_shorts': 5,            # concentrated: 3-5 shorts simultáneos
    'futures_max_new_per_cycle': 2,
    'futures_usdt_per_trade': 150.0,
    'futures_sl_pct': 2.0,              # concentrated: mayor tolerancia al ruido
    'futures_tp_pct': 4.0,              # concentrated: mejor R:R
    'futures_kelly_base_usdt': 150.0,
    'futures_kelly_max_usdt': 1500.0,
    'futures_kelly_max_pct_liquid': 0.85,
    'stop_loss_pct': 3.36,              # run_0677
    'take_profit_pct': 4.14,            # run_0677
    'min_signal_strength': 0.60,        # concentrated: solo señales fuertes
    'min_success_probability': 0.60,      # concentrated: edge estadístico alto
    'futures_min_prob': 0.499,
    'vol_spike_min': 0.91,              # run_0677
    'slippage_pct': 0.00052,            # run_0677
    'use_bnb_for_commission': True,
}


# Severity for each backtest baseline field. Anything else is 'warning' by default.
_BACKTEST_BASELINE_SEVERITY: Dict[str, str] = {
    'futures_leverage': 'critical',
    'futures_max_shorts': 'critical',
    'futures_usdt_per_trade': 'critical',
    'futures_sl_pct': 'critical',
    'futures_tp_pct': 'critical',
    'kelly_max_pct_liquid': 'critical',
    'max_per_trade_usdt': 'warning',
    'stop_loss_pct': 'warning',
    'take_profit_pct': 'warning',
}


def _check_config_drift(settings) -> List[Dict]:
    """Compare runtime config against canonical defaults and backtest baseline."""
    _drifts: List[Dict] = []
    _actual = dump_active_config(settings)
    # These are intentionally environment-specific or operator-controlled and not drift.
    _EXCLUDED = {
        # Env/safety overrides
        'dry_run', 'allow_real_trading',
        # Symbol/category lists (deployment-specific)
        'analysis_symbols', 'futures_symbols', 'futures_largecap_symbols',
        'trading_excluded_symbols', 'narrative_tokens',
        'token_categories', 'category_thresholds', 'futures_category_thresholds',
        # Dynamic runtime state
        'current_regime_mode',
        # Runtime label — populated from DB at startup; '' sentinel in code is intentional
        'current_regime_profile_label',
        # Docker-compose operational overrides — intentionally tuned per deployment
        'max_per_trade_usdt', 'max_positions', 'max_positions_per_symbol',
        'max_total_exposure_pct', 'kelly_dynamic_base_pct',
        'trailing_stop_activation_pct', 'trailing_stop_distance_pct',
        'futures_max_new_per_cycle', 'overflow_max_extra',
        'min_hold_minutes', 'max_rebalance_per_cycle',
        'btc_dominance_altcoin_threshold', 'use_unified_portfolio_manager',
        'futures_overflow_min_strength',
        # s71: Per-regime SSO/PRS/position fields modified by harmony Profile multipliers.
        # These legitimately differ from canonical defaults when a profile is active.
        'sso_stagnant_min_hours_spot_bull', 'sso_stagnant_min_hours_range_scalp',
        'sso_stagnant_min_hours_preservation', 'sso_stagnant_min_hours_futures_short',
        'sso_slow_min_hours_spot_bull', 'sso_slow_min_hours_range_scalp',
        'sso_slow_min_hours_preservation', 'sso_slow_min_hours_futures_short',
        'sso_decay_min_hours_spot_bull', 'sso_decay_min_hours_range_scalp',
        'sso_decay_min_hours_preservation', 'sso_decay_min_hours_futures_short',
        'sso_target_daily_usd', 'sso_slow_velocity_threshold',
        # Per-regime limits modified by harmony
        'max_positions_spot_bull', 'max_positions_range_scalp',
        'max_positions_preservation', 'max_positions_futures_short',
        'min_hold_minutes_spot_bull', 'min_hold_minutes_range_scalp',
        'futures_long_sl_pct', 'futures_long_tp_pct', 'max_rebalance_per_cycle',
        # Futures ATR trail fields — modified by harmony Profile multipliers
        'futures_trailing_activation_pct', 'futures_trailing_distance_pct',
        'futures_breakeven_activation_pct', 'futures_trail_floor_pct',
        'futures_trail_ceiling_pct', 'futures_trail_atr_multiplier_low',
        'futures_trail_atr_multiplier_mid', 'futures_trail_atr_multiplier_high',
        'futures_trail_atr_multiplier_deep',
        # Entry/prediction params modified by harmony or DB overrides
        'prediction_add_min_strength', 'prediction_add_min_ev_ratio',
        'range_scalp_sl_pct', 'range_scalp_tp_pct',
        'spot_bounce_rescue_target_pnl', 'futures_bounce_rescue_target_pnl',
        'kelly_conviction_max_pct',
        # Cycle parameters governed by kelly_config_override in DB
        'cycle_range_scalp', 'cycle_spot_bull', 'cycle_preservation',
        # Kelly/signal fields governed by kelly_config_override in DB (not code defaults)
        'kelly_base_usdt', 'kelly_max_usdt', 'kelly_max_pct_liquid', 'kelly_max_pct_cap',
        'kelly_dynamic_base_pct',
        'futures_kelly_base_usdt', 'futures_kelly_max_usdt', 'futures_kelly_max_pct_liquid',
        'futures_kelly_cap_pct', 'futures_kelly_max_usdt',
        'min_signal_strength', 'min_success_probability', 'vol_spike_min',
        'futures_sl_pct', 'futures_tp_pct', 'futures_usdt_per_trade', 'futures_kelly_max_usdt',
        'stop_loss_pct', 'take_profit_pct', 'slippage_pct',
        'max_per_trade_usdt',
        # s72/s75: fields governed by kelly_config_override in DB (expected to differ from defaults)
        'category_max_positions_enabled',
        'entry_quality_vol_spike_min_range_scalp',
        'range_scalp_max_rsi', 'range_scalp_max_bb_position',
        'sso_target_contribution_pct_range_scalp',
        # SSO-1d: Flat Stagnation Timeout — DB-governed
        'sso_flat_stagnation_timeout_enabled',
        'sso_flat_stagnation_timeout_hours',
        'sso_flat_stagnation_timeout_hours_range_scalp',
        'sso_flat_stagnation_timeout_pnl_pct',
        'sso_flat_stagnation_timeout_pnl_pct_range_scalp',
    }
    # Canonical default drift (SPOT+FUTURES parameters) — skip keys already covered by baseline
    for k, expected in _PARAM_DEFAULTS.items():
        if k in _EXCLUDED or k in _BACKTEST_BASELINE:
            continue
        val = _actual.get(k)
        if val != expected:
            _drifts.append({
                "severity": "critical" if k in _BACKTEST_BASELINE_SEVERITY else "warning",
                "param": k,
                "expected": expected,
                "actual": val,
                "source": "canonical_default",
            })
    # Backtest baseline drift (profit-critical subset) — single source of truth for these keys
    for k, expected in _BACKTEST_BASELINE.items():
        if k in _EXCLUDED:
            continue
        val = _actual.get(k)
        if val != expected:
            _drifts.append({
                "severity": _BACKTEST_BASELINE_SEVERITY.get(k, "warning"),
                "param": k,
                "expected": expected,
                "actual": val,
                "source": "backtest_baseline",
            })
    return _drifts


def _persist_drift_alerts(drifts: List[Dict]) -> int:
    """Insert new drift alerts, deduplicating unacknowledged identical rows."""
    _inserted = 0
    for d in drifts:
        try:
            # Skip if identical unacknowledged alert already exists in last 24h
            _dup = db_client.execute_query(
                "SELECT 1 FROM config_drift_alerts "
                "WHERE param=%s AND source=%s AND expected=%s AND actual=%s "
                "AND acknowledged=FALSE AND detected_at > NOW() - INTERVAL '24 hours' "
                "LIMIT 1",
                (d["param"], d["source"], json.dumps(d["expected"]), json.dumps(d["actual"]))
            )
            if _dup:
                continue
            db_client.execute_update(
                "INSERT INTO config_drift_alerts (severity, param, expected, actual, source) "
                "VALUES (%s, %s, %s, %s, %s)",
                (d["severity"], d["param"], json.dumps(d["expected"]), json.dumps(d["actual"]), d["source"])
            )
            _inserted += 1
        except Exception as e:
            print(f"[DriftAlert] failed to insert alert for {d.get('param')}: {e}")
    return _inserted


async def _config_drift_loop() -> None:
    """Background task: check config drift every 15 minutes and persist alerts."""
    while True:
        try:
            await asyncio.sleep(900)
            _comp = get_components()
            if not _comp or not _comp.settings:
                continue
            _drifts = _check_config_drift(_comp.settings)
            if _drifts:
                _n = _persist_drift_alerts(_drifts)
                if _n:
                    print(f"[DriftAlert] {_n} new drift alert(s) persisted")
            else:
                print("[DriftAlert] runtime config aligned with baseline")
            # BNB discount health check
            _bnb_drift = _check_bnb_drift(_comp)
            if _bnb_drift:
                _persist_drift_alerts([_bnb_drift])
                print(f"[DriftAlert] BNB discount drift persisted: {_bnb_drift}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[DriftAlert] drift check failed: {e}")


def _level_rank(status: str) -> int:
    return _HEALTH_RESULTS_LEVEL.get(str(status or "unknown").lower(), 4)


def _count_unacked_drift_alerts() -> Dict[str, int]:
    try:
        rows = db_client.execute_query(
            "SELECT severity, COUNT(*) AS cnt FROM config_drift_alerts WHERE acknowledged=FALSE GROUP BY severity"
        )
        out = {"critical": 0, "warning": 0, "info": 0, "total": 0}
        for row in rows or []:
            sev = str(row.get("severity") or "warning").lower()
            cnt = int(row.get("cnt") or 0)
            out[sev] = out.get(sev, 0) + cnt
            out["total"] += cnt
        return out
    except Exception:
        return {"critical": 0, "warning": 0, "info": 0, "total": 0}


def _get_active_critical_drift_blockers() -> Dict[str, Any]:
    try:
        _drifts = _check_config_drift(settings)
        _critical = [d for d in (_drifts or []) if str(d.get("severity") or "").lower() == "critical"]
        return {
            "ok": True,
            "count": len(_critical),
            "blockers": _critical,
        }
    except Exception as e:
        return {"ok": False, "count": 0, "blockers": [], "error": str(e)}


def _extract_scope_stats(window: Dict[str, Any], scope: str) -> Dict[str, Any]:
    try:
        by_scope = ((window or {}).get("by_scope") or {})
        stats = (by_scope.get(scope) or {})
        n_trades = int(stats.get("n_trades") or 0)
        pnl_sum = float(stats.get("pnl_sum") or 0.0)
        fees_sum = float(stats.get("fees_sum") or 0.0)
        return {
            "n_trades": n_trades,
            "pnl_sum": round(pnl_sum, 4),
            "fees_sum": round(fees_sum, 4),
            "net_pnl": round(pnl_sum - fees_sum, 4),
            "win_rate_pct": float(stats.get("win_rate_pct") or 0.0),
        }
    except Exception:
        return {"n_trades": 0, "pnl_sum": 0.0, "fees_sum": 0.0, "net_pnl": 0.0, "win_rate_pct": 0.0}


def _compute_market_health(
    market: str,
    current_stats: Dict[str, Any],
    previous_stats: Dict[str, Any],
    audit_consistency: Dict[str, Any],
    err_summary: Dict[str, Any],
    drift_counts: Dict[str, int],
    active_drift_count: int = 0,
) -> Dict[str, Any]:
    score = 100
    reasons: List[str] = []
    critical_flags: List[str] = []
    block_flags: List[str] = []

    cur_trades = int(current_stats.get("n_trades") or 0)
    cur_net = float(current_stats.get("net_pnl") or 0.0)
    prev_net = float(previous_stats.get("net_pnl") or 0.0)
    parity_gap_pct = None
    if abs(prev_net) > 1e-8:
        parity_gap_pct = round((cur_net - prev_net) / abs(prev_net) * 100.0, 2)

    drift_critical = int(drift_counts.get("critical") or 0)
    drift_warning = int(drift_counts.get("warning") or 0)
    errors_5m = int(err_summary.get("errors_last_5min") or 0)
    warnings_5m = int(err_summary.get("warnings_last_5min") or 0)
    missing_audit = int((audit_consistency or {}).get("signals_missing_audit") or 0)
    coverage_pct = float((audit_consistency or {}).get("coverage_pct") or 100.0)

    if active_drift_count > 0:
        score -= 35
        critical_flags.append("config_drift_critical")
        block_flags.append("config_drift_critical")
        reasons.append(f"{active_drift_count} drift(s) críticos activos")
    elif drift_critical > 0:
        score -= 10
        reasons.append(f"{drift_critical} drift(s) críticos históricos sin ack")
    elif drift_warning > 0:
        score -= 10
        reasons.append(f"{drift_warning} drift(s) warning sin ack")

    if errors_5m > 0:
        score -= 25
        critical_flags.append("errors_recent")
        block_flags.append("errors_recent")
        reasons.append(f"{errors_5m} error(es) en 5 min")
    elif warnings_5m > 3:
        score -= 8
        reasons.append(f"{warnings_5m} warning(s) en 5 min")

    if missing_audit > 0:
        score -= 20
        critical_flags.append("audit_gap")
        block_flags.append("audit_gap")
        reasons.append(f"{missing_audit} señales sin auditoría")
    elif coverage_pct < 98.0:
        score -= 8
        reasons.append(f"cobertura de auditoría {coverage_pct:.2f}%")

    if parity_gap_pct is not None:
        abs_gap = abs(parity_gap_pct)
        if abs_gap >= 15.0:
            score -= 22
            reasons.append(f"parity gap {parity_gap_pct:.2f}%")
        elif abs_gap >= 7.0:
            score -= 10
            reasons.append(f"parity gap moderado {parity_gap_pct:.2f}%")

    if cur_trades < 3:
        score -= 10
        reasons.append("muestra post-ajuste insuficiente")
    elif cur_trades < 8:
        score -= 4
        reasons.append("muestra post-ajuste limitada")

    if cur_net < -20:
        score -= 8
        reasons.append(f"net_pnl negativo ({cur_net:.2f})")

    score = max(0, min(100, score))
    if critical_flags or score < 55:
        status = "critical"
    elif score < 80:
        status = "warning"
    else:
        status = "ok"

    if cur_trades < 3 and not block_flags:
        status = "inconclusive"

    return {
        "market": market,
        "status": status,
        "score": score,
        "reasons": reasons[:6],
        "critical_flags": critical_flags,
        "block_new_entries": bool(block_flags),
        "block_flags": block_flags,
        "kpis": {
            "n_trades": cur_trades,
            "net_pnl": round(cur_net, 4),
            "win_rate_pct": round(float(current_stats.get("win_rate_pct") or 0.0), 2),
            "parity_gap_pct": parity_gap_pct,
            "audit_coverage_pct": round(coverage_pct, 2),
            "errors_last_5min": errors_5m,
            "warnings_last_5min": warnings_5m,
        },
    }


def _compute_health_results_snapshot(trigger: str = "manual") -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    err_summary = ec.get_summary(minutes=5)
    drift_counts = _count_unacked_drift_alerts()
    active_drift = _get_active_critical_drift_blockers()
    active_drift_count = int(active_drift.get("count") or 0)
    audit_consistency = check_signal_audit_consistency(db_client, limit=150)
    changepoint = _get_performance_changepoint_metrics()
    harmony = _get_mode_profile_harmony_snapshot()
    harmony_drift = _get_mode_profile_harmony_drift()
    parity_validation = _get_parity_validation_snapshot()

    current_window = (changepoint.get("current_window") or {}) if changepoint.get("ok") else {}
    previous_window = (changepoint.get("previous_window") or {}) if changepoint.get("ok") else {}

    spot_current = _extract_scope_stats(current_window, "spot")
    futures_current = _extract_scope_stats(current_window, "futures")
    spot_prev = _extract_scope_stats(previous_window, "spot")
    futures_prev = _extract_scope_stats(previous_window, "futures")

    spot_health = _compute_market_health(
        market="spot",
        current_stats=spot_current,
        previous_stats=spot_prev,
        audit_consistency=audit_consistency,
        err_summary=err_summary,
        drift_counts=drift_counts,
        active_drift_count=active_drift_count,
    )
    futures_health = _compute_market_health(
        market="futures",
        current_stats=futures_current,
        previous_stats=futures_prev,
        audit_consistency=audit_consistency,
        err_summary=err_summary,
        drift_counts=drift_counts,
        active_drift_count=active_drift_count,
    )

    overall_status = "ok"
    if _level_rank(spot_health.get("status")) > _level_rank(overall_status):
        overall_status = spot_health.get("status")
    if _level_rank(futures_health.get("status")) > _level_rank(overall_status):
        overall_status = futures_health.get("status")

    if not changepoint.get("ok") and overall_status == "ok":
        overall_status = "inconclusive"

    if overall_status in ("critical", "error"):
        interval_seconds = 45
    elif overall_status == "warning":
        interval_seconds = 120
    elif overall_status == "inconclusive":
        interval_seconds = 180
    else:
        interval_seconds = 600

    if int(err_summary.get("errors_last_5min") or 0) > 0:
        interval_seconds = max(30, min(interval_seconds, 60))

    snapshot = {
        "ok": True,
        "overall": overall_status,
        "block_new_entries": bool(
            (spot_health.get("block_new_entries") if isinstance(spot_health, dict) else False)
            or (futures_health.get("block_new_entries") if isinstance(futures_health, dict) else False)
        ),
        "block_reasons": list(dict.fromkeys(
            list((spot_health.get("block_flags") or []) if isinstance(spot_health, dict) else [])
            + list((futures_health.get("block_flags") or []) if isinstance(futures_health, dict) else [])
        )),
        "spot": spot_health,
        "futures": futures_health,
        "errors": err_summary,
        "audit_consistency": {
            "available": bool(audit_consistency.get("available", True)),
            "coverage_pct": round(float(audit_consistency.get("coverage_pct") or 0.0), 2),
            "signals_missing_audit": int(audit_consistency.get("signals_missing_audit") or 0),
            "auditable_signals": int(audit_consistency.get("auditable_signals") or 0),
        },
        "drift_alerts": drift_counts,
        "changepoint": {
            "ok": bool(changepoint.get("ok")),
            "marker": (changepoint.get("marker") or {}),
            "delta": (changepoint.get("delta") or {}),
        },
        "harmony": {
            "ok": bool(harmony.get("ok")),
            "snapshot": (harmony.get("snapshot") or {}),
            "updated_at": harmony.get("updated_at"),
        },
        "harmony_drift": {
            "ok": bool(harmony_drift.get("ok")),
            "drift_count": int(harmony_drift.get("drift_count") or 0),
            "mismatches": harmony_drift.get("mismatches") or [],
            "effective_runtime": harmony_drift.get("effective_runtime") or {},
            "snapshot_updated_at": harmony_drift.get("snapshot_updated_at"),
        },
        "parity_validation": parity_validation,
        "smart_recheck": {
            "trigger": trigger,
            "interval_seconds": interval_seconds,
            "last_run_at": now_utc.isoformat(),
            "next_run_eta_seconds": interval_seconds,
        },
        "timestamp": now_utc.isoformat(),
    }
    return snapshot


def _persist_health_results_snapshot(snapshot: Dict[str, Any]) -> None:
    try:
        payload = json.dumps(snapshot)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("health_results_latest", payload, payload),
        )
    except Exception as e:
        print(f"[SMART-HEALTH] failed to persist runtime snapshot: {e}")


def _audit_health_event(event: str, detail: Dict[str, Any]) -> None:
    try:
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            ("__health__", "system", event, json.dumps(detail), False),
        )
    except Exception as e:
        print(f"[SMART-HEALTH] failed to write audit event {event}: {e}")


def _load_persisted_health_results_snapshot() -> Optional[Dict[str, Any]]:
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='health_results_latest'"
        )
        if not rows:
            return None
        raw = rows[0].get("value")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception:
        return None
    return None


def _consume_health_recheck_request() -> Optional[Dict[str, Any]]:
    """Consume a DB-backed recheck trigger request emitted by runtime components."""
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='health_results_recheck_request'"
        )
        if not rows:
            return None
        raw = rows[0].get("value")
        if isinstance(raw, str):
            raw = json.loads(raw)
        db_client.execute_update(
            "DELETE FROM trading_runtime_state WHERE key='health_results_recheck_request'"
        )
        return raw if isinstance(raw, dict) else {"reason": "runtime_request"}
    except Exception:
        return None


async def _health_results_recheck_loop() -> None:
    previous_overall = None
    try:
        restored = _load_persisted_health_results_snapshot()
        if restored and isinstance(restored, dict):
            _HEALTH_RESULTS_CACHE["snapshot"] = restored
            _HEALTH_RESULTS_CACHE["last_run_at"] = restored.get("timestamp")
            previous_overall = str(restored.get("overall") or "ok").lower()
    except Exception:
        previous_overall = None

    while True:
        try:
            wait_seconds = int(_HEALTH_RESULTS_CACHE.get("interval_seconds") or 300)
            trigger = "interval"
            trigger_reason = None
            _deadline = time.monotonic() + max(15, wait_seconds)
            while True:
                _req = _consume_health_recheck_request()
                if _req:
                    trigger = "event_db"
                    trigger_reason = str(_req.get("reason") or "runtime_request")
                    break
                _remaining = _deadline - time.monotonic()
                if _remaining <= 0:
                    trigger = "interval"
                    break
                try:
                    await asyncio.wait_for(_HEALTH_RESULTS_EVENT.wait(), timeout=min(5.0, _remaining))
                    _HEALTH_RESULTS_EVENT.clear()
                    trigger = "event"
                    break
                except asyncio.TimeoutError:
                    continue

            trigger_tag = f"{trigger}:{trigger_reason}" if trigger_reason else trigger
            snapshot = _compute_health_results_snapshot(trigger=trigger_tag)
            _persist_health_results_snapshot(snapshot)
            _HEALTH_RESULTS_CACHE["snapshot"] = snapshot
            _HEALTH_RESULTS_CACHE["last_run_at"] = snapshot.get("timestamp")
            _HEALTH_RESULTS_CACHE["interval_seconds"] = int(((snapshot.get("smart_recheck") or {}).get("interval_seconds") or 300))
            _HEALTH_RESULTS_CACHE["last_trigger"] = trigger

            current_overall = str(snapshot.get("overall") or "ok").lower()
            _audit_health_event("health_recheck_run", {
                "overall": current_overall,
                "spot": snapshot.get("spot", {}).get("status"),
                "futures": snapshot.get("futures", {}).get("status"),
                "interval_seconds": _HEALTH_RESULTS_CACHE.get("interval_seconds"),
                "trigger": trigger,
            })
            if current_overall in ("critical", "error") and previous_overall not in ("critical", "error"):
                _audit_health_event("health_recheck_alert", {
                    "prev_overall": previous_overall,
                    "current_overall": current_overall,
                    "spot": snapshot.get("spot"),
                    "futures": snapshot.get("futures"),
                })
            if current_overall == "ok" and previous_overall in ("critical", "error", "warning", "inconclusive"):
                _audit_health_event("health_recheck_recovery", {
                    "prev_overall": previous_overall,
                    "current_overall": current_overall,
                })
            previous_overall = current_overall
        except asyncio.CancelledError:
            break
        except Exception as e:
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR, f"SMART health recheck loop failed: {e}")
            await asyncio.sleep(30)


_bnb_drift_cache: Dict[str, Any] = {"consecutive_off": 0}


def _check_bnb_drift(_comp) -> Optional[Dict]:
    """Return a drift alert if BNB discount has been off for 2+ consecutive checks."""
    try:
        from binance_commission import get_config
        _cfg = get_config(_comp.settings)
        if _cfg.use_bnb:
            _bnb_drift_cache["consecutive_off"] = 0
            return None
        _bnb_drift_cache["consecutive_off"] += 1
        if _bnb_drift_cache["consecutive_off"] < 2:
            return None
        return {
            "severity": "warning",
            "param": "use_bnb_for_commission",
            "expected": True,
            "actual": False,
            "source": "bnb_discount_health",
            "notes": "BNB discount has been off for 2+ consecutive drift checks; check BNB balance or auto_bnb_for_commission",
        }
    except Exception as e:
        print(f"[DriftAlert] BNB drift check failed: {e}")
        return None


def _normalize_backtest_params(params: Any) -> Dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, str):
        try:
            parsed = json.loads(params)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_close_reason(reason: Any) -> str:
    r = str(reason or '').strip().lower()
    if not r:
        return 'unknown'
    if r.startswith('stop_loss') or r.startswith('sl_'):
        return 'stop_loss'
    if r.startswith('take_profit') or r.startswith('tp_') or r.startswith('smart_tp'):
        return 'take_profit'
    if r.startswith('trailing_stop'):
        return 'trailing_stop'
    if 'rebal' in r or r.startswith('reallocation_'):
        return 'portfolio_rebalancing'
    if r.startswith('manual'):
        return 'manual'
    return r


def _trade_summary(rows: List[Dict], pnl_key: str = 'pnl') -> Dict[str, Any]:
    total = len(rows)
    pnl_total = sum(float(r.get(pnl_key, 0.0) or 0.0) for r in rows)
    winners = sum(1 for r in rows if float(r.get(pnl_key, 0.0) or 0.0) > 0)
    losses = total - winners
    win_rate = (winners / total) if total else 0.0
    avg_pnl = (pnl_total / total) if total else 0.0
    by_trade_type = Counter(str((r.get('trade_type') or 'unknown')).lower() for r in rows)
    by_reason = Counter(str(r.get('close_reason_norm') or _normalize_close_reason(r.get('close_reason')))
                        for r in rows)
    return {
        'n_trades': total,
        'n_wins': winners,
        'n_losses': losses,
        'win_rate': round(win_rate, 4),
        'avg_pnl': round(avg_pnl, 6),
        'pnl_total': round(pnl_total, 6),
        'by_trade_type': dict(by_trade_type),
        'close_reason_dist': dict(by_reason),
    }


def _parity_gap_pct(backtest_value: float, live_value: float) -> Optional[float]:
    if live_value == 0:
        return None
    return round(((backtest_value - live_value) / abs(live_value)) * 100.0, 4)


def _active_trade_type_for_mode(mode: Any) -> str:
    _mode = str(mode or '').strip().upper()
    return 'futures' if _mode == 'FUTURES_SHORT' else 'spot'


def _build_parity_metrics(backtest_summary: Dict[str, Any], live_summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'pnl_total_gap_pct': _parity_gap_pct(backtest_summary['pnl_total'], live_summary['pnl_total']),
        'win_rate_gap_pct': round((backtest_summary['win_rate'] - live_summary['win_rate']) * 100.0, 4),
        'avg_pnl_gap_pct': _parity_gap_pct(backtest_summary['avg_pnl'], live_summary['avg_pnl']),
        'n_trades_gap': int(backtest_summary['n_trades'] or 0) - int(live_summary['n_trades'] or 0),
    }


def _resolve_rollout_baseline() -> Dict[str, Any]:
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key LIKE %s ORDER BY updated_at DESC, key DESC LIMIT 1",
            ('performance_changepoint_%',),
        )
        if not rows:
            return {'ok': False, 'status': 'unavailable', 'reason': 'no performance changepoint marker found'}
        value = rows[0].get('value') or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {}

        candidate_run_id = None
        if isinstance(value, dict) and value.get('base_run_id') is not None:
            candidate_run_id = int(value.get('base_run_id'))
        baseline_key = value.get('baseline_key') if isinstance(value, dict) else None
        if candidate_run_id is None and baseline_key:
            base_rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key=%s LIMIT 1",
                (baseline_key,),
            )
            if base_rows:
                base_value = base_rows[0].get('value') or {}
                if isinstance(base_value, str):
                    try:
                        base_value = json.loads(base_value)
                    except Exception:
                        base_value = {}
                if isinstance(base_value, dict) and base_value.get('base_run_id') is not None:
                    candidate_run_id = int(base_value.get('base_run_id'))

        if candidate_run_id is None:
            return {'ok': False, 'status': 'unavailable', 'reason': 'no base_run_id found in changepoint marker'}

        exists_rows = db_client.execute_query(
            "SELECT id FROM backtest_run WHERE id=%s LIMIT 1",
            (candidate_run_id,),
        )
        if not exists_rows:
            return {
                'ok': False,
                'status': 'unavailable',
                'reason': f'baseline run_id {candidate_run_id} not found',
                'run_id': candidate_run_id,
            }

        return {'ok': True, 'status': 'ok', 'run_id': candidate_run_id}
    except Exception as e:
        return {'ok': False, 'status': 'error', 'reason': str(e), 'error': str(e)}


def _resolve_rollout_baseline_run_id() -> Optional[int]:
    resolved = _resolve_rollout_baseline()
    if resolved.get('ok') and resolved.get('run_id') is not None:
        return int(resolved.get('run_id'))
    return None


def _build_backtest_compare_vs_live(
    run_id: int,
    active_trade_type: Optional[str] = None,
    active_mode: Optional[str] = None,
) -> Dict[str, Any]:
    run_rows = db_client.execute_query(
        "SELECT id, label, params, pnl_total, pnl_spot, pnl_futures, win_rate, n_trades, n_spot, n_futures, rank_pnl, window_start, window_end FROM backtest_run WHERE id = %s",
        (run_id,)
    )
    if not run_rows:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    run = run_rows[0]
    params = _normalize_backtest_params(run.get('params'))
    run_meta = _normalize_backtest_params(params.get('__run_meta', {}))
    window_start = run.get('window_start')
    window_end = run.get('window_end')
    regime = run_meta.get('regime') or params.get('regime') or 'unknown'
    session_id = run_meta.get('session_id')

    backtest_trades = db_client.execute_query(
        """
        SELECT symbol, trade_type, side, entry_ts, exit_ts, entry_price, exit_price,
               usdt_size, pnl_usdt, pnl_pct, tier, regime, close_reason, kelly_mult
        FROM backtest_trade
        WHERE run_id = %s
        ORDER BY entry_ts ASC
        """,
        (run_id,)
    )
    bt_symbols = sorted({r.get('symbol') for r in backtest_trades if r.get('symbol')})
    resolved_active_mode = str(
        active_mode
        or getattr(orchestrator, 'current_regime_mode', '')
        or getattr(settings, 'current_regime_mode', '')
        or regime
        or 'SPOT_BULL'
    ).strip().upper() or 'SPOT_BULL'
    resolved_active_trade_type = str(active_trade_type or _active_trade_type_for_mode(resolved_active_mode)).strip().lower()
    if resolved_active_trade_type not in ('spot', 'futures'):
        resolved_active_trade_type = _active_trade_type_for_mode(resolved_active_mode)

    live_rows = db_client.execute_query(
        """
        SELECT symbol, side, quantity, entry_time, exit_time, pnl, pnl_percentage,
               close_reason, metadata, entry_commission, exit_commission
        FROM trades_closed
        WHERE entry_time >= %s AND entry_time < %s
        ORDER BY entry_time ASC
        """,
        (window_start, window_end)
    )
    live_trades: List[Dict[str, Any]] = []
    live_symbols_all = set()
    live_symbols_used = set()
    filtered_out_symbol_count = 0
    live_missing_trade_type_count = 0
    for row in live_rows:
        symbol = row.get('symbol')
        if symbol:
            live_symbols_all.add(symbol)
        meta = row.get('metadata') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if bt_symbols and symbol not in bt_symbols:
            filtered_out_symbol_count += 1
            continue
        if symbol:
            live_symbols_used.add(symbol)

        inferred_type = str(meta.get('trade_type') or '').strip().lower()
        if inferred_type not in ('spot', 'futures'):
            live_missing_trade_type_count += 1
            inferred_type = 'spot'

        raw_reason = row.get('close_reason') or meta.get('close_reason') or 'unknown'
        live_trades.append({
            'symbol': symbol,
            'trade_type': inferred_type,
            'side': row.get('side'),
            'entry_time': row.get('entry_time'),
            'exit_time': row.get('exit_time'),
            'pnl': float(row.get('pnl') or 0.0),
            'pnl_percentage': float(row.get('pnl_percentage') or 0.0),
            'close_reason': raw_reason,
            'close_reason_norm': _normalize_close_reason(raw_reason),
            'metadata': meta,
        })

    bt_summary = _trade_summary(backtest_trades, pnl_key='pnl_usdt')
    live_summary = _trade_summary(live_trades, pnl_key='pnl')
    bt_by_type = {
        trade_type: _trade_summary([r for r in backtest_trades if r.get('trade_type') == trade_type], pnl_key='pnl_usdt')
        for trade_type in ('spot', 'futures')
    }
    live_by_type = {
        trade_type: _trade_summary([r for r in live_trades if r.get('trade_type') == trade_type], pnl_key='pnl')
        for trade_type in ('spot', 'futures')
    }

    parity = _build_parity_metrics(bt_summary, live_summary)
    active_bt_summary = bt_by_type.get(resolved_active_trade_type) or _trade_summary([], pnl_key='pnl_usdt')
    active_live_summary = live_by_type.get(resolved_active_trade_type) or _trade_summary([], pnl_key='pnl')
    active_parity = _build_parity_metrics(active_bt_summary, active_live_summary)

    active_bt_trades = int(active_bt_summary.get('n_trades') or 0)
    active_live_trades = int(active_live_summary.get('n_trades') or 0)
    sample_target = 3
    coverage_ratio = None
    if active_bt_trades > 0:
        coverage_ratio = round(active_live_trades / active_bt_trades, 6)
    symbol_overlap_ratio = None
    if live_symbols_all:
        symbol_overlap_ratio = round(len(live_symbols_used) / len(live_symbols_all), 6)

    comparability_status = 'comparable'
    sample_status = 'sufficient'
    comparability_reasons: List[str] = []
    if active_bt_trades <= 0:
        comparability_status = 'not_comparable'
        sample_status = 'none'
        comparability_reasons.append(f'no backtest trades in active {resolved_active_trade_type} slice')
    elif active_live_trades <= 0:
        comparability_status = 'not_comparable'
        sample_status = 'none'
        comparability_reasons.append(f'no live trades in active {resolved_active_trade_type} slice')
    elif active_live_trades < sample_target:
        comparability_status = 'limited'
        sample_status = 'limited'
        comparability_reasons.append(
            f'active {resolved_active_trade_type} sample limited ({active_live_trades}/{sample_target} trades)'
        )

    if coverage_ratio is not None and coverage_ratio < 0.05:
        if comparability_status == 'comparable':
            comparability_status = 'limited'
        if sample_status == 'sufficient':
            sample_status = 'limited'
        comparability_reasons.append(f'active coverage low ({coverage_ratio * 100.0:.2f}% of baseline trades)')

    if symbol_overlap_ratio is not None and symbol_overlap_ratio < 0.5:
        if comparability_status == 'comparable':
            comparability_status = 'limited'
        comparability_reasons.append(
            f'symbol overlap low ({symbol_overlap_ratio * 100.0:.2f}% of live symbols aligned to baseline)'
        )

    if resolved_active_trade_type == 'futures' and live_missing_trade_type_count > 0:
        if comparability_status == 'comparable':
            comparability_status = 'limited'
        comparability_reasons.append(
            f'live trade_type missing on {live_missing_trade_type_count} rows; futures slice may be understated'
        )

    active_scope = {
        'mode': resolved_active_mode,
        'trade_type': resolved_active_trade_type,
        'backtest': {'summary': active_bt_summary},
        'live': {'summary': active_live_summary},
        'parity': active_parity,
        'comparability': {
            'status': comparability_status,
            'sample_status': sample_status,
            'sample_target': sample_target,
            'coverage_ratio': coverage_ratio,
            'symbol_overlap_ratio': symbol_overlap_ratio,
            'filtered_out_symbol_count': filtered_out_symbol_count,
            'live_missing_trade_type_count': live_missing_trade_type_count,
            'reasons': comparability_reasons,
        },
    }

    return {
        'ok': True,
        'run_id': run_id,
        'session_id': session_id,
        'regime': regime,
        'active_mode': resolved_active_mode,
        'active_trade_type': resolved_active_trade_type,
        'window_start': window_start.isoformat() if hasattr(window_start, 'isoformat') else str(window_start),
        'window_end': window_end.isoformat() if hasattr(window_end, 'isoformat') else str(window_end),
        'symbols': bt_symbols,
        'backtest': {
            'run': {
                'id': run.get('id'),
                'label': run.get('label'),
                'pnl_total': float(run.get('pnl_total') or 0.0),
                'pnl_spot': float(run.get('pnl_spot') or 0.0),
                'pnl_futures': float(run.get('pnl_futures') or 0.0),
                'win_rate': float(run.get('win_rate') or 0.0),
                'n_trades': int(run.get('n_trades') or 0),
                'n_spot': int(run.get('n_spot') or 0),
                'n_futures': int(run.get('n_futures') or 0),
                'rank_pnl': run.get('rank_pnl'),
            },
            'summary': bt_summary,
            'by_trade_type': bt_by_type,
        },
        'live': {
            'summary': live_summary,
            'by_trade_type': live_by_type,
        },
        'parity': parity,
        'active_scope': active_scope,
        'diagnostics': {
            'live_symbol_count': len(live_symbols_all),
            'matched_live_symbol_count': len(live_symbols_used),
            'filtered_out_symbol_count': filtered_out_symbol_count,
            'live_missing_trade_type_count': live_missing_trade_type_count,
        },
        'ts': to_mexico_time(datetime.utcnow()).isoformat(),
    }


@app.get("/backtest/compare-vs-live")
async def get_backtest_compare_vs_live(
    run_id: int,
    _auth=Depends(_require_api_key),
) -> Dict:
    """Compare one backtest run against live closed trades in the same window."""
    try:
        return _build_backtest_compare_vs_live(run_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/backtest/apply")
async def post_backtest_apply(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Apply a backtest run configuration to live settings with snapshot + audit."""
    try:
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings

        _run_id = body.get("run_id")
        _mode = body.get("mode", "manual")  # manual | auto | hybrid
        _selected_fields = set(body.get("fields", []))
        _selected_cat = body.get("category_thresholds", {})  # {cat: [field, ...]}
        _selected_futures_cat = body.get("futures_category_thresholds", {})
        _auto = body.get("auto_apply", False)

        # Fetch run (best if run_id is None)
        if _run_id is None:
            _run = db_client.execute_query(
                """SELECT id, label, params, pnl_total, return_pct, is_baseline
                   FROM backtest_run WHERE is_baseline=FALSE ORDER BY pnl_total DESC LIMIT 1"""
            )
        else:
            _run = db_client.execute_query(
                "SELECT id, label, params, pnl_total, return_pct, is_baseline FROM backtest_run WHERE id=%s",
                (_run_id,)
            )
        if not _run:
            return {"ok": False, "error": "Run not found"}
        _run = _run[0]
        if _run.get("is_baseline"):
            return {"ok": False, "error": "Cannot apply baseline run directly"}
        _params = _run["params"] if isinstance(_run["params"], dict) else {}

        # Snapshot before any change
        _snapshot_id = _create_config_snapshot(_s)

        # Build request body for PATCH /config/kelly
        _patch_body = {}

        # Scalar mapping from backtest param -> Settings field + scale
        # min/max are in BACKTEST raw scale (decimal), not Settings scale
        _PARAM_MAP = {
            "kelly_base_usdt":        {"field": "kelly_base_usdt",        "scale": 1.0,   "min": 50.0,   "max": 300.0},
            "kelly_max_usdt":         {"field": "kelly_max_usdt",         "scale": 1.0,   "min": 200.0,  "max": 1200.0},
            "kelly_max_pct_liquid":   {"field": "kelly_max_pct_liquid",   "scale": 1.0,   "min": 0.20,   "max": 0.90},
            "futures_kelly_max_usdt": {"field": "futures_kelly_max_usdt", "scale": 1.0,   "min": 300.0,  "max": 2000.0},
            "futures_sl_pct":         {"field": "futures_sl_pct",         "scale": 100.0, "min": 0.010,  "max": 0.050},
            "futures_tp_pct":         {"field": "futures_tp_pct",         "scale": 100.0, "min": 0.020,  "max": 0.100},
            "spot_sl_pct":            {"field": "stop_loss_pct",          "scale": 100.0, "min": 0.010,  "max": 0.040},
            "spot_tp_pct":            {"field": "take_profit_pct",        "scale": 100.0, "min": 0.018,  "max": 0.080},
            "min_strength":           {"field": "min_signal_strength",      "scale": 1.0,   "min": 0.30,   "max": 0.90},
            "min_prob":               {"field": "min_success_probability",  "scale": 1.0,   "min": 0.40,   "max": 0.80},
            "futures_min_prob":       {"field": "futures_min_prob",           "scale": 1.0,   "min": 0.40,   "max": 0.80},
            "vol_spike_min":          {"field": "vol_spike_min",            "scale": 1.0,   "min": 0.5,    "max": 3.0},
            "futures_vol_spike_min":  {"field": "futures_vol_spike_min",    "scale": 1.0,   "min": 0.5,    "max": 3.0},
            "slippage_pct":           {"field": "slippage_pct",             "scale": 1.0,   "min": 0.0001, "max": 0.001},
        }
        # jun28: relaxed after Backtest 1000 validated run_0812 as baseline=best
        _NEVER_AUTO = set()
        _safe_only = getattr(_s, "backtest_safe_only", True)

        _applied_scalars = {}
        _skipped = {}
        for bt_key, mapping in _PARAM_MAP.items():
            if bt_key not in _params:
                continue
            # Filter by explicit field selection (manual mode)
            if _selected_fields and bt_key not in _selected_fields:
                continue
            _raw = float(_params[bt_key])
            _safe = mapping["min"] <= _raw <= mapping["max"]
            if (_mode == "auto" or _auto) and (bt_key in _NEVER_AUTO or not _safe):
                _skipped[bt_key] = "auto-apply blocked by safety rules"
                continue
            if _safe_only and not _safe:
                _skipped[bt_key] = "outside safe range"
                continue
            _scaled = round(_raw * mapping["scale"], 6)
            _patch_body[mapping["field"]] = _scaled
            _applied_scalars[bt_key] = _scaled

        # Special: min_prob -> SPOT category_thresholds
        _opt_min_prob = float(_params.get("min_prob", 0))
        _opt_spot_tp = round(float(_params.get("spot_tp_pct", 0)) * 100.0, 4)
        _opt_fut_tp = round(float(_params.get("futures_tp_pct", 0)) * 100.0, 4)
        _opt_fut_sl = round(float(_params.get("futures_sl_pct", 0)) * 100.0, 4)

        _cat_patch = {}
        _live_ct = getattr(_s, "category_thresholds", {}) or {}
        for _cat in ["LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"]:
            _fields = _selected_cat.get(_cat, []) if isinstance(_selected_cat, dict) else []
            _ct = _live_ct.get(_cat, {})
            _changes = {}
            if (not _selected_fields or "min_prob" in _fields) and _opt_min_prob > 0 and float(_ct.get("min_prob", 0)) < _opt_min_prob:
                _changes["min_prob"] = _opt_min_prob
            if (not _selected_fields or "take_profit_pct" in _fields) and _opt_spot_tp > 0 and float(_ct.get("take_profit_pct", 0)) < _opt_spot_tp:
                _changes["take_profit_pct"] = _opt_spot_tp
            if _changes:
                _cat_patch[_cat] = _changes
        if _cat_patch:
            _patch_body["category_thresholds"] = _cat_patch

        _futures_cat_patch = {}
        _live_fct = getattr(_s, "futures_category_thresholds", {}) or {}
        for _cat in list(_live_fct.keys()) or ["LARGE_CAP", "MIDCAP_ALT", "NARRATIVE", "MEME"]:
            _fields = _selected_futures_cat.get(_cat, []) if isinstance(_selected_futures_cat, dict) else []
            _ct = _live_fct.get(_cat, {})
            _changes = {}
            if (not _selected_fields or "min_prob" in _fields) and _opt_min_prob > 0 and float(_ct.get("min_prob", 0)) < _opt_min_prob:
                _changes["min_prob"] = _opt_min_prob
            if (not _selected_fields or "take_profit_pct" in _fields) and _opt_fut_tp > 0 and float(_ct.get("take_profit_pct", 0)) < _opt_fut_tp:
                _changes["take_profit_pct"] = _opt_fut_tp
            if (not _selected_fields or "stop_loss_pct" in _fields) and _opt_fut_sl > 0 and float(_ct.get("stop_loss_pct", 0)) < _opt_fut_sl:
                _changes["stop_loss_pct"] = _opt_fut_sl
            if _changes:
                _futures_cat_patch[_cat] = _changes
        if _futures_cat_patch:
            _patch_body["futures_category_thresholds"] = _futures_cat_patch

        if not _patch_body:
            return {"ok": False, "error": "No parameters selected or all blocked by safety rules", "skipped": _skipped, "snapshot_id": _snapshot_id}

        # Apply via internal helper (reuse PATCH /config/kelly logic)
        _patch_result = await patch_kelly_config(_patch_body, _auth)
        if not _patch_result.get("ok"):
            _patch_result["snapshot_id"] = _snapshot_id
            return _patch_result

        # Audit event specifically for backtest apply
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__config__', 'system', 'backtest_apply', %s, false)",
            (json.dumps({
                "run_id": _run["id"],
                "run_label": _run["label"],
                "mode": _mode,
                "applied_scalars": _applied_scalars,
                "category_thresholds": _cat_patch,
                "futures_category_thresholds": _futures_cat_patch,
                "skipped": _skipped,
                "snapshot_id": _snapshot_id,
            }),)
        )

        return {
            "ok": True,
            "run_id": _run["id"],
            "run_label": _run["label"],
            "mode": _mode,
            "snapshot_id": _snapshot_id,
            "applied": _patch_result.get("applied", {}),
            "rejected": _patch_result.get("rejected", {}),
            "skipped": _skipped,
            "message": "Backtest configuration applied successfully",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/config/drift")
async def get_config_drift(_auth=Depends(_require_api_key)) -> Dict:
    """
    K2: Compare live runtime settings against the canonical backtest baseline (run_0812)
    and module-level safety caps. Returns a list of drift issues.
    """
    try:
        from config_helpers import validate_config, dump_active_config
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings
        _validation = validate_config(_s)
        _snapshot = dump_active_config(_s)
        _backtest_baseline = {
            'kelly_base_usdt': 150.0,           # concentrated: base digna para $5,100
            'kelly_max_usdt': 1500.0,           # concentrated: cap por posición elevado
            'kelly_max_pct_liquid': 0.85,       # concentrated: hasta 85% del líquido
            'futures_kelly_base_usdt': 150.0,
            'futures_kelly_max_usdt': 1500.0,
            'futures_kelly_max_pct_liquid': 0.85,
            'futures_leverage': 10,
            'futures_max_shorts': 5,            # concentrated: 3-5 shorts simultáneos
            'futures_max_new_per_cycle': 2,
            'futures_sl_pct': 2.0,              # concentrated: mayor tolerancia al ruido
            'futures_tp_pct': 4.0,              # concentrated: mejor R:R
            'stop_loss_pct': 3.36,              # run_0677
            'take_profit_pct': 4.14,            # run_0677
            'min_signal_strength': 0.60,        # concentrated: solo señales fuertes
            'min_success_probability': 0.60,      # concentrated: edge estadístico alto
            'futures_min_prob': 0.499,
            'vol_spike_min': 0.91,              # run_0677
            'slippage_pct': 0.00052,            # run_0677
            'use_bnb_for_commission': True,
        }
        _drift = []
        for k, expected in _backtest_baseline.items():
            actual = _snapshot.get(k)
            if actual != expected:
                _drift.append({'param': k, 'expected': expected, 'actual': actual, 'severity': 'warning'})
        return {
            'ok': True,
            'validation': _validation,
            'drift': _drift,
            'snapshot': _snapshot,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@app.post("/config/apply-backtest")
async def post_config_apply_backtest(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """
    K2: Apply a backtest run configuration to runtime state.
    Reads the run from backtest_results, maps params to Settings fields, persists
    a snapshot and the override in trading_runtime_state, and writes an audit row.
    Container restart is required for env-var based fields to take full effect.
    """
    try:
        from init_components import get_components
        _comp = get_components()
        _run_id = body.get('run_id')
        if not _run_id:
            return {'ok': False, 'error': 'run_id required'}
        # Load backtest run result
        _rows = db_client.execute_query(
            "SELECT run_id, params, metrics FROM backtest_results WHERE run_id = %s ORDER BY created_at DESC LIMIT 1",
            (_run_id,)
        )
        if not _rows:
            return {'ok': False, 'error': f'backtest run {_run_id} not found'}
        _params = _rows[0].get('params') or {}
        _metrics = _rows[0].get('metrics') or {}

        # Map backtest param names (decimals) to Settings field names
        _MAP = {
            'spot_sl_pct': 'stop_loss_pct',
            'spot_tp_pct': 'take_profit_pct',
            'futures_sl_pct': 'futures_sl_pct',
            'futures_tp_pct': 'futures_tp_pct',
            'kelly_base_usdt': 'kelly_base_usdt',
            'kelly_max_usdt': 'kelly_max_usdt',
            'kelly_max_pct_liquid': 'kelly_max_pct_liquid',
            'futures_kelly_base_usdt': 'futures_kelly_base_usdt',
            'futures_kelly_max_usdt': 'futures_kelly_max_usdt',
            'futures_kelly_max_pct_liquid': 'futures_kelly_max_pct_liquid',
            'futures_leverage': 'futures_leverage',
            'futures_max_shorts': 'futures_max_shorts',
            'futures_max_new_per_cycle': 'futures_max_new_per_cycle',
            'min_signal_strength': 'min_signal_strength',
            'min_success_probability': 'min_success_probability',
            'vol_spike_min': 'vol_spike_min',
        }
        _mapped = {}
        _rejected = {}
        for _src, _dst in _MAP.items():
            if _src in _params:
                _mapped[_dst] = _params[_src]
            else:
                _rejected[_src] = 'not present in run params'

        # Snapshot current config before applying
        from config_helpers import dump_active_config
        _snapshot = dump_active_config(_comp.settings)
        _snapshot_id = f"pre_apply_{_run_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (f"config_snapshot_{_snapshot_id}", json.dumps(_snapshot))
        )
        # Persist the override — MERGE into existing override to preserve manual s62/s63 values
        _existing_override = {}
        try:
            _ex_rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
            )
            if _ex_rows and _ex_rows[0].get('value'):
                _ex_raw = _ex_rows[0]['value']
                if isinstance(_ex_raw, str):
                    import json as _jj; _ex_raw = _jj.loads(_ex_raw)
                if isinstance(_ex_raw, dict):
                    _existing_override = _ex_raw
        except Exception:
            pass
        # Wrap new backtest values in {"new": v} format for consistency with restore logic
        _backtest_wrapped = {k: {"new": v} for k, v in _mapped.items()}
        _existing_override.update(_backtest_wrapped)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            ("kelly_config_override", json.dumps(_existing_override))
        )
        # Audit row
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
            ('__config__', 'system', 'config_apply_backtest', json.dumps({
                'run_id': _run_id,
                'applied': _mapped,
                'rejected': _rejected,
                'metrics': _metrics,
                'snapshot_id': _snapshot_id,
            }), False)
        )
        return {
            'ok': True,
            'run_id': _run_id,
            'applied': _mapped,
            'rejected': _rejected,
            'snapshot_id': _snapshot_id,
            'message': 'Backtest config persisted. Restart container for env-var fields to take full effect.',
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@app.get("/config/snapshots")
async def get_config_snapshots(limit: int = 10, _auth=Depends(_require_api_key)) -> Dict:
    """List recent config snapshots for rollback."""
    try:
        _rows = db_client.execute_query(
            "SELECT key, value, updated_at FROM trading_runtime_state WHERE key LIKE 'config_snapshot_%%' ORDER BY updated_at DESC LIMIT %s",
            (limit,)
        )
        _snapshots = []
        for r in _rows:
            _key = r["key"]
            if _key == "config_snapshot_latest":
                continue
            _snap_id = _key.replace("config_snapshot_", "")
            _snapshots.append({"snapshot_id": _snap_id, "updated_at": r["updated_at"].isoformat()})
        return {"ok": True, "snapshots": _snapshots, "count": len(_snapshots)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/rollback")
async def post_config_rollback(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Rollback to a previous config snapshot."""
    try:
        from init_components import get_components
        _comp = get_components()
        _s = _comp.settings
        _snapshot_id = body.get("snapshot_id")
        if not _snapshot_id:
            return {"ok": False, "error": "snapshot_id required"}
        return _restore_config_snapshot(_snapshot_id, _s)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/snapshot")
async def post_config_snapshot(_auth=Depends(_require_api_key)) -> Dict:
    """Create a point-in-time snapshot of the full runtime config."""
    try:
        _comp = get_components()
        _sid = _create_full_config_snapshot(_comp.settings)
        return {"ok": True, "snapshot_id": _sid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/verify")
async def post_config_verify(body: Dict = None, _auth=Depends(_require_api_key)) -> Dict:
    """Verify runtime config against canonical baseline (same as tmp_verify_runtime_config.py)."""
    try:
        _comp = get_components()
        _actual = dump_active_config(_comp.settings)
        _exclude = {
            # Env/safety overrides
            'dry_run', 'allow_real_trading',
            # Symbol/category lists (deployment-specific)
            'analysis_symbols', 'futures_symbols', 'futures_largecap_symbols',
            'trading_excluded_symbols', 'narrative_tokens',
            'token_categories', 'category_thresholds', 'futures_category_thresholds',
            'current_regime_mode', 'current_regime_profile_label',
            # Docker-compose operational overrides — intentionally tuned per deployment
            'max_per_trade_usdt', 'max_positions', 'max_positions_per_symbol',
            'max_total_exposure_pct', 'kelly_dynamic_base_pct',
            'trailing_stop_activation_pct', 'trailing_stop_distance_pct',
            'futures_max_new_per_cycle', 'overflow_max_extra',
            'min_hold_minutes', 'max_rebalance_per_cycle',
            'btc_dominance_altcoin_threshold', 'use_unified_portfolio_manager',
            'futures_overflow_min_strength',
            # Per-regime limits modified by harmony Profile multipliers
            'max_positions_spot_bull', 'max_positions_range_scalp',
            'max_positions_preservation', 'max_positions_futures_short',
            'min_hold_minutes_spot_bull', 'min_hold_minutes_range_scalp',
            'futures_long_sl_pct', 'futures_long_tp_pct',
            # SSO/timing modified by harmony
            'sso_stagnant_min_hours_spot_bull', 'sso_stagnant_min_hours_range_scalp',
            'sso_stagnant_min_hours_preservation', 'sso_stagnant_min_hours_futures_short',
            'sso_slow_min_hours_spot_bull', 'sso_slow_min_hours_range_scalp',
            'sso_slow_min_hours_preservation', 'sso_slow_min_hours_futures_short',
            'sso_decay_min_hours_spot_bull', 'sso_decay_min_hours_range_scalp',
            'sso_decay_min_hours_preservation', 'sso_decay_min_hours_futures_short',
            'sso_target_daily_usd', 'sso_slow_velocity_threshold',
            # Futures ATR trail fields modified by harmony
            'futures_trailing_activation_pct', 'futures_trailing_distance_pct',
            'futures_breakeven_activation_pct', 'futures_trail_floor_pct',
            'futures_trail_ceiling_pct', 'futures_trail_atr_multiplier_low',
            'futures_trail_atr_multiplier_mid', 'futures_trail_atr_multiplier_high',
            'futures_trail_atr_multiplier_deep',
            # Entry/prediction params modified by harmony or DB overrides
            'prediction_add_min_strength', 'prediction_add_min_ev_ratio',
            'range_scalp_sl_pct', 'range_scalp_tp_pct',
            'spot_bounce_rescue_target_pnl', 'futures_bounce_rescue_target_pnl',
            'kelly_conviction_max_pct',
            # Cycle parameters governed by kelly_config_override in DB
            'cycle_range_scalp', 'cycle_spot_bull', 'cycle_preservation',
            # Kelly/signal fields governed by kelly_config_override in DB
            'kelly_base_usdt', 'kelly_max_usdt', 'kelly_max_pct_liquid', 'kelly_max_pct_cap',
            'kelly_dynamic_base_pct',
            'futures_kelly_base_usdt', 'futures_kelly_max_usdt', 'futures_kelly_max_pct_liquid',
            'futures_kelly_cap_pct',
            'min_signal_strength', 'min_success_probability', 'vol_spike_min',
            'futures_sl_pct', 'futures_tp_pct', 'futures_usdt_per_trade',
            'stop_loss_pct', 'take_profit_pct', 'slippage_pct',
            'max_per_trade_usdt',
            # s72/s75: fields governed by kelly_config_override in DB
            'category_max_positions_enabled',
            'entry_quality_vol_spike_min_range_scalp',
            'range_scalp_max_rsi', 'range_scalp_max_bb_position',
            'sso_target_contribution_pct_range_scalp',
            # SSO-1d: Flat Stagnation Timeout — DB-governed
            'sso_flat_stagnation_timeout_enabled',
            'sso_flat_stagnation_timeout_hours',
            'sso_flat_stagnation_timeout_hours_range_scalp',
            'sso_flat_stagnation_timeout_pnl_pct',
            'sso_flat_stagnation_timeout_pnl_pct_range_scalp',
        }
        if body and isinstance(body, dict):
            _exclude.update(body.get('exclude', []))
        _mismatches = {}
        for k, expected in _PARAM_DEFAULTS.items():
            if k in _exclude:
                continue
            val = _actual.get(k)
            if val != expected:
                _mismatches[k] = {"expected": expected, "actual": val}
        _ok = not _mismatches
        return {
            "ok": _ok,
            "mismatches": _mismatches,
            "mismatch_count": len(_mismatches),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/config/change-log")
async def get_config_change_log(limit: int = 50, _auth=Depends(_require_api_key)) -> Dict:
    """Return recent configuration changes for 100% traceability."""
    try:
        _rows = db_client.execute_query(
            "SELECT id, changed_at, source, changed_by, snapshot_id, diff "
            "FROM config_change_log ORDER BY changed_at DESC LIMIT %s",
            (limit,)
        )
        _changes = []
        for r in _rows:
            _diff = r.get("diff")
            if isinstance(_diff, str):
                _diff = json.loads(_diff)
            _changes.append({
                "id": r.get("id"),
                "changed_at": r.get("changed_at").isoformat() if r.get("changed_at") else None,
                "source": r.get("source"),
                "changed_by": r.get("changed_by"),
                "snapshot_id": r.get("snapshot_id"),
                "diff": _diff,
            })
        return {"ok": True, "changes": _changes, "count": len(_changes)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/config/drift-alerts")
async def get_config_drift_alerts(acknowledged: Optional[bool] = None, limit: int = 50, _auth=Depends(_require_api_key)) -> Dict:
    """Return config drift alerts (optionally filtered by acknowledged)."""
    try:
        _params: List[Any] = []
        _sql = (
            "SELECT id, detected_at, severity, param, expected, actual, source, acknowledged, "
            "acknowledged_at, acknowledged_by, notes "
            "FROM config_drift_alerts "
        )
        if acknowledged is not None:
            _sql += "WHERE acknowledged=%s "
            _params.append(acknowledged)
        _sql += "ORDER BY detected_at DESC LIMIT %s"
        _params.append(limit)
        _rows = db_client.execute_query(_sql, tuple(_params))
        _alerts = []
        for r in _rows:
            _exp, _act = r.get("expected"), r.get("actual")
            if isinstance(_exp, str):
                _exp = json.loads(_exp)
            if isinstance(_act, str):
                _act = json.loads(_act)
            _alerts.append({
                "id": r.get("id"),
                "detected_at": r.get("detected_at").isoformat() if r.get("detected_at") else None,
                "severity": r.get("severity"),
                "param": r.get("param"),
                "expected": _exp,
                "actual": _act,
                "source": r.get("source"),
                "acknowledged": r.get("acknowledged"),
                "acknowledged_at": r.get("acknowledged_at").isoformat() if r.get("acknowledged_at") else None,
                "acknowledged_by": r.get("acknowledged_by"),
                "notes": r.get("notes"),
            })
        return {"ok": True, "alerts": _alerts, "count": len(_alerts)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/drift-alerts/{alert_id}/acknowledge")
async def post_acknowledge_drift_alert(alert_id: int, body: Dict = None, _auth=Depends(_require_api_key)) -> Dict:
    """Acknowledge a drift alert with optional notes."""
    try:
        _notes = (body or {}).get("notes")
        _by = (body or {}).get("acknowledged_by") or (str(_auth) if _auth else None)
        db_client.execute_update(
            "UPDATE config_drift_alerts SET acknowledged=TRUE, acknowledged_at=NOW(), "
            "acknowledged_by=%s, notes=%s WHERE id=%s",
            (_by, _notes, alert_id)
        )
        return {"ok": True, "alert_id": alert_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/drift-check")
async def post_config_drift_check(_auth=Depends(_require_api_key)) -> Dict:
    """Manually trigger a drift check and return current alerts."""
    try:
        _comp = get_components()
        _drifts = _check_config_drift(_comp.settings)
        _n = _persist_drift_alerts(_drifts)
        return {"ok": True, "drift_count": len(_drifts), "new_alerts": _n, "drifts": _drifts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/apply")
async def post_config_apply(body: Dict, _auth=Depends(_require_api_key)) -> Dict:
    """Apply a configuration patch (full or partial) with snapshot, diff, and audit log."""
    try:
        _comp = get_components()
        _s = _comp.settings
        _exclude = {
            'dry_run', 'allow_real_trading', 'analysis_symbols', 'futures_symbols',
            'futures_largecap_symbols', 'trading_excluded_symbols', 'narrative_tokens',
            'token_categories', 'category_thresholds', 'current_regime_mode',
        }
        _applied = {}
        _rejected = {}
        _diff = {}
        # Snapshot before change
        _before = dump_active_config(_s)
        _sid = _create_full_config_snapshot(_s)
        for key, new_value in body.items():
            if key in _exclude:
                _rejected[key] = "not allowed"
                continue
            if not hasattr(_s, key):
                _rejected[key] = "unknown setting"
                continue
            try:
                _old = getattr(_s, key)
                _new = new_value
                # Preserve type when possible
                if _old is not None:
                    _type = type(_old)
                    if _type in (bool, int, float, str, list, dict, tuple):
                        _new = _type(_new)
                setattr(_s, key, _new)
                _applied[key] = _new
                _diff[key] = {"old": _old, "new": _new}
            except Exception as _e:
                _rejected[key] = str(_e)
        _after = dump_active_config(_s)
        # Persist applied scalar overrides so they survive container restart
        # MERGE into existing override — never replace, to preserve manual PATCH /config/kelly values
        if _applied:
            _persist_obj = {
                k: {
                    "old": _diff.get(k, {}).get("old"),
                    "new": _diff.get(k, {}).get("new"),
                }
                for k in _applied.keys()
            }
            _existing_apply_ov = {}
            try:
                _ex = db_client.execute_query(
                    "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
                )
                if _ex and _ex[0].get('value'):
                    _raw = _ex[0]['value']
                    if isinstance(_raw, str):
                        _raw = json.loads(_raw)
                    if isinstance(_raw, dict):
                        _existing_apply_ov = _raw
            except Exception:
                pass
            _existing_apply_ov.update(_persist_obj)
            _persist = json.dumps(_existing_apply_ov)
            _audit_detail = json.dumps({
                "event": "config_apply",
                "snapshot_id": _sid,
                "applied": _persist_obj,
                "rejected": _rejected,
            })
            db_client.execute_transaction([
                (
                    "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES ('kelly_config_override', %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (_persist, _persist),
                ),
                (
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                    "VALUES ('__config__', 'system', 'config_apply', %s, false)",
                    (_audit_detail,),
                ),
            ])
        _log_config_change(
            source="api",
            changed_by=str(_auth) if _auth else None,
            snapshot_id=_sid,
            diff=_diff,
            full_config=_after,
        )
        return {
            "ok": True,
            "snapshot_id": _sid,
            "applied": _applied,
            "rejected": _rejected,
            "diff": _diff,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID OPTIMIZER ENDPOINT (quantum + swarm + ML)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/hybrid/recommend")
async def get_hybrid_recommend(
    top_n: int = 10,
    _auth=Depends(_require_api_key)
) -> Dict:
    """Return a hybrid recommendation for the next configuration using QIO+swarm+ML."""
    try:
        from init_components import get_components
        from hybrid_optimizer import recommend_config
        _comp = get_components()
        _s = _comp.settings

        # Fetch top backtest runs
        _rows = db_client.execute_query(
            """SELECT id, label, params, pnl_total, return_pct, sharpe, n_trades
               FROM backtest_run
               WHERE is_baseline = FALSE
               ORDER BY pnl_total DESC
               LIMIT %s""",
            (top_n,)
        )
        if not _rows:
            return {"ok": False, "error": "No backtest runs found"}

        # Fetch recent PnL for allocation (trades_closed uses exit_time)
        _spot_pnl = db_client.execute_query(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades_closed "
            "WHERE side IN ('BUY','SELL') AND exit_time > NOW() - INTERVAL '7 days'"
        )
        _fut_pnl = db_client.execute_query(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades_closed "
            "WHERE side IN ('SHORT','LONG') AND exit_time > NOW() - INTERVAL '7 days'"
        )
        _spot_pnl_7d = float(_spot_pnl[0]["pnl"] if _spot_pnl else 0)
        _fut_pnl_7d = float(_fut_pnl[0]["pnl"] if _fut_pnl else 0)
        _regime = getattr(_comp, "current_regime_mode", "SPOT_BULL") or getattr(
            _comp.orchestrator if hasattr(_comp, "orchestrator") else None, "current_regime_mode", "SPOT_BULL"
        ) or "SPOT_BULL"

        _rec = recommend_config(
            top_runs=_rows,
            regime=_regime,
            spot_pnl_7d=_spot_pnl_7d,
            futures_pnl_7d=_fut_pnl_7d,
            settings=_s,
        )

        # Audit
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__hybrid__', 'system', 'hybrid_optimizer_suggestion', %s, false)",
            (json.dumps({
                "confidence": _rec.get("confidence"),
                "safe": _rec.get("safe"),
                "regime": _regime,
                "predicted_pnl": _rec.get("predicted_pnl"),
                "capital_allocation": _rec.get("capital_allocation"),
            }),)
        )
        return _rec
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── MIMIC endpoints ───────────────────────────────────────────────────────────

_mimic_scheduler: Optional[Any] = None
_mimic_worker_task: Optional[Any] = None
_mimic_worker_running: bool = False


def _get_mimic_scheduler() -> Any:
    global _mimic_scheduler
    if _mimic_scheduler is None:
        from simulation.mimic_scheduler import get_scheduler
        _mimic_scheduler = get_scheduler(db_client=db_client)
    return _mimic_scheduler


async def _mimic_autorun_loop() -> None:
    """Background loop that runs MIMIC micro-universe cycles when the schedule allows."""
    global _mimic_worker_running
    _mimic_worker_running = True
    while _mimic_worker_running:
        ran_cycle = False
        try:
            scheduler = _get_mimic_scheduler()
            state = scheduler.get_state()
            if state.get("status") == "paused":
                pass
            elif state.get("status") == "running":
                # A persisted 'running' state without an active thread means the container
                # was restarted mid-cycle; resume it immediately.
                print(f"[MIMIC AutoRun] resuming stuck running state — phase={state.get('phase')}")
                ran_cycle = True
                result = await asyncio.to_thread(scheduler.run_once)
                print(f"[MIMIC AutoRun] cycle finished: ok={result.get('ok')} phase={result.get('phase')} error={result.get('error')}")
                continue
            else:
                can_run = scheduler.can_run_now()
                if can_run.get("can_run"):
                    ran_cycle = True
                    print(f"[MIMIC AutoRun] starting cycle — reason={can_run.get('reason')}")
                    result = await asyncio.to_thread(scheduler.run_once)
                    print(f"[MIMIC AutoRun] cycle finished: ok={result.get('ok')} phase={result.get('phase')} error={result.get('error')}")
                    # If still able to run, loop immediately without sleeping to maximize window usage
                    continue
        except Exception as exc:
            print(f"[MIMIC AutoRun] ERROR: {exc}")
        if not ran_cycle:
            await asyncio.sleep(60)  # 1 minute when idle/waiting
        else:
            await asyncio.sleep(5)  # brief pause between consecutive cycles


@app.on_event("startup")
async def _start_mimic_autorun() -> None:
    global _mimic_worker_task
    if _mimic_worker_task is None:
        _mimic_worker_task = asyncio.create_task(_mimic_autorun_loop())


@app.get("/mimic/status", dependencies=[Depends(_require_api_key)])
async def mimic_status():
    scheduler = _get_mimic_scheduler()
    summary = scheduler.get_summary()
    summary["autorun_worker"] = {
        "active": _mimic_worker_running,
        "task_created": _mimic_worker_task is not None,
    }
    return summary


@app.get("/mimic/universes", dependencies=[Depends(_require_api_key)])
async def mimic_universes():
    from simulation.mimic_universe_selector import get_selector
    selector = get_selector()
    return {
        "summary": selector.get_summary(),
        "universes": selector.get_queue(),
    }


@app.post("/mimic/select-universe", dependencies=[Depends(_require_api_key)])
async def mimic_select_universe(request: Request):
    body = await request.json()
    universe_id = body.get("micro_universe_id")
    if not universe_id:
        raise HTTPException(status_code=400, detail="micro_universe_id required")
    from simulation.mimic_universe_selector import get_selector
    selector = get_selector()
    if not selector.force_next(universe_id):
        raise HTTPException(status_code=404, detail="micro universe not found")
    return {"ok": True, "forced_id": universe_id}


@app.post("/mimic/start", dependencies=[Depends(_require_api_key)])
async def mimic_start(request: Request):
    body = await request.json() if await request.body() else {}
    force = body.get("force", False)
    scheduler = _get_mimic_scheduler()
    can_run = scheduler.can_run_now()
    if not can_run.get("can_run") and not force:
        return {"ok": False, "can_run": can_run}
    scheduler.start()
    return {"ok": True, "state": scheduler.get_state()}


@app.post("/mimic/run", dependencies=[Depends(_require_api_key)])
async def mimic_run(request: Request):
    body = await request.json() if await request.body() else {}
    force = body.get("force", False)
    scheduler = _get_mimic_scheduler()
    can_run = scheduler.can_run_now()
    if not can_run.get("can_run") and not force:
        return {"ok": False, "can_run": can_run}
    result = scheduler.run_once()
    return {"ok": result.get("ok", False), "result": result, "state": scheduler.get_state()}


@app.post("/mimic/pause", dependencies=[Depends(_require_api_key)])
async def mimic_pause():
    scheduler = _get_mimic_scheduler()
    scheduler.pause()
    return {"ok": True, "state": scheduler.get_state()}


@app.post("/mimic/resume", dependencies=[Depends(_require_api_key)])
async def mimic_resume():
    scheduler = _get_mimic_scheduler()
    scheduler.resume()
    return {"ok": True, "state": scheduler.get_state()}


@app.post("/mimic/retry", dependencies=[Depends(_require_api_key)])
async def mimic_retry(request: Request):
    body = await request.json() if await request.body() else {}
    universe_id = body.get("micro_universe_id")
    scheduler = _get_mimic_scheduler()
    if universe_id:
        scheduler.selector.mark_status(universe_id, "pending")
        scheduler._state["current_micro_universe_id"] = universe_id
        scheduler._save_state()
    else:
        scheduler.resume()
    return {"ok": True, "state": scheduler.get_state()}


@app.post("/mimic/reset", dependencies=[Depends(_require_api_key)])
async def mimic_reset(request: Request):
    body = await request.json() if await request.body() else {}
    confirm = body.get("confirm", False)
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    scheduler = _get_mimic_scheduler()
    scheduler.reset()
    return {"ok": True, "state": scheduler.get_state()}


@app.get("/mimic/schedule", dependencies=[Depends(_require_api_key)])
async def mimic_schedule_get():
    from simulation.mimic_window_controller import get_window_controller
    controller = get_window_controller()
    return controller.get_schedule()


@app.post("/mimic/schedule", dependencies=[Depends(_require_api_key)])
async def mimic_schedule_post(request: Request):
    body = await request.json()
    from simulation.mimic_window_controller import get_window_controller
    controller = get_window_controller()
    controller.set_schedule(body)
    return {"ok": True, "schedule": controller.get_schedule()}


@app.get("/mimic/report", dependencies=[Depends(_require_api_key)])
async def mimic_report():
    from simulation.mimic_universe_selector import get_selector
    selector = get_selector()
    scheduler = _get_mimic_scheduler()
    return {
        "state": scheduler.get_state(),
        "queue_summary": selector.get_summary(),
        "universes": selector.get_queue(),
    }


# In-memory flag to prevent concurrent MIMIC sweeps; progress lives on disk.
_mimic_sweep_running = False


@app.post("/mimic/external/download", dependencies=[Depends(_require_api_key)])
async def mimic_external_download(request: Request):
    """Download external data (funding, ticker 24h, BTC dominance, F&G) for a date range."""
    body = await request.json()
    start = body.get("start")
    end = body.get("end")
    # Default to full available range when dates not provided
    if not start:
        start = "2026-01-01"
    if not end:
        import datetime as _dt
        end = _dt.date.today().isoformat()

    script_path = os.path.join(os.path.dirname(__file__), "Dev", "scripts", "mimic_download_external.py")
    if not os.path.exists(script_path):
        return {"ok": False, "error": f"Download script not found: {script_path}"}

    def _run():
        import subprocess
        _env = os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        _env["PYTHONUNBUFFERED"] = "1"
        _log_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_external_download.log")
        with open(_log_path, "a", encoding="utf-8") as _log_file:
            subprocess.run(
                ["python", "-u", script_path, "--start", start, "--end", end],
                cwd=os.path.dirname(__file__),
                env=_env,
                stdout=_log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"External data download started for {start} to {end}."}


@app.post("/mimic/window/build", dependencies=[Depends(_require_api_key)])
async def mimic_window_build(request: Request):
    """Build a continuous MIMIC simulation window manifest."""
    body = await request.json()
    start = body.get("start")
    end = body.get("end")
    symbols = body.get("symbols")
    top_n = body.get("top_n", 40)
    if not start or not end:
        raise HTTPException(status_code=400, detail="start and end dates required")

    script_path = os.path.join(os.path.dirname(__file__), "Dev", "scripts", "mimic_build_window.py")
    if not os.path.exists(script_path):
        return {"ok": False, "error": f"Window build script not found: {script_path}"}

    cmd = ["python", "-u", script_path, "--start", start, "--end", end, "--top-n", str(top_n), "--skip-external-check"]
    if symbols:
        cmd += ["--symbols", ",".join(symbols)]

    def _run():
        import subprocess
        _env = os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        _env["PYTHONUNBUFFERED"] = "1"
        _log_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_window_build.log")
        with open(_log_path, "a", encoding="utf-8") as _log_file:
            subprocess.run(
                cmd,
                cwd=os.path.dirname(__file__),
                env=_env,
                stdout=_log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"Window build started for {start} to {end}."}


@app.post("/mimic/sweep", dependencies=[Depends(_require_api_key)])
async def mimic_sweep_start(request: Request):
    """Start the Mode × Profile MIMIC parameter sweep in a background subprocess."""
    global _mimic_sweep_running
    if _mimic_sweep_running:
        return {"ok": False, "error": "A sweep is already running."}

    body = await request.json()
    window_ids = body.get("windows")
    strategy = body.get("strategy", "full")
    max_cycles = body.get("max_cycles")
    max_workers = body.get("max_workers", 1)
    clear_results = bool(body.get("clear_results", False))

    script_path = os.path.join(os.path.dirname(__file__), "Dev", "scripts", "mimic_mode_profile_sweep.py")
    if not os.path.exists(script_path):
        return {"ok": False, "error": f"Sweep script not found: {script_path}"}

    _mimic_sweep_running = True

    def _mark_finished():
        global _mimic_sweep_running
        _mimic_sweep_running = False

    cmd = ["python", script_path, "--strategy", strategy]
    if window_ids:
        cmd += ["--windows", ",".join(window_ids)]
    if max_cycles is not None:
        cmd += ["--max-cycles", str(max_cycles)]
    if max_workers is not None and max_workers > 1:
        cmd += ["--max-workers", str(max_workers)]
    if clear_results:
        cmd += ["--clear-results"]

    def _run_sweep_subprocess():
        import subprocess
        _env = os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        _log_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_sweep.log")
        try:
            with open(_log_path, "a", encoding="utf-8") as _log_file:
                subprocess.run(
                    cmd,
                    cwd=os.path.dirname(__file__),
                    env=_env,
                    stdout=_log_file,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
        finally:
            _mark_finished()

    import threading
    threading.Thread(target=_run_sweep_subprocess, daemon=True).start()
    return {"ok": True, "message": "MIMIC Mode×Profile sweep started in background.", "command": cmd}


@app.get("/mimic/sweep-status", dependencies=[Depends(_require_api_key)])
async def mimic_sweep_status():
    """Return the current progress of a running MIMIC sweep."""
    import json
    status_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_sweep_status.json")
    default = {
        "state": "idle",
        "total_jobs": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "errors": [],
        "eta_seconds": None,
        "current_job": None,
    }
    if not os.path.exists(status_path):
        return {"ok": True, "running": _mimic_sweep_running or default.get("state") == "running", "status": default}
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            status = json.load(f)
        status["running"] = _mimic_sweep_running or status.get("state") == "running"
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e), "status": default}


@app.post("/mimic/apply-recommendation", dependencies=[Depends(_require_api_key)])
async def mimic_apply_recommendation(request: Request):
    """Apply MIMIC sweep recommendation to production config (MERGE, never REPLACE).

    Accepts:
      params: dict of scalar config fields (applied via setattr + kelly_config_override merge)
      profile_overrides: dict of regime_profile_* overrides
      category_thresholds: dict merged into kelly_config_override.category_thresholds
      futures_category_thresholds: dict merged into kelly_config_override.futures_category_thresholds
      profile: str label for audit
      dry_run: bool — if true, validate only, do not persist
    """
    body = await request.json()
    params = body.get("params") or {}
    profile_overrides = body.get("profile_overrides") or {}
    category_thresholds = body.get("category_thresholds") or {}
    futures_category_thresholds = body.get("futures_category_thresholds") or {}
    profile = body.get("profile", "B")
    dry_run = bool(body.get("dry_run", False))

    applied: dict = {"scalars": {}, "profile_overrides": [], "category_thresholds": [], "futures_category_thresholds": []}
    rejected: dict = {}

    # Apply scalar params (in-memory + kelly_config_override MERGE)
    if params:
        try:
            _s = get_settings()
            current_override: dict = {}
            if db_client:
                try:
                    row = db_client.fetchone(
                        "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
                    )
                    if row and row.get("value"):
                        current_override = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
                except Exception:
                    pass
            for key, new_value in params.items():
                try:
                    _old = getattr(_s, key, None)
                    if _old is not None:
                        _type = type(_old)
                        if _type in (bool, int, float, str):
                            new_value = _type(new_value)
                    if not dry_run:
                        setattr(_s, key, new_value)
                    current_override[key] = new_value
                    applied["scalars"][key] = new_value
                except Exception as _e:
                    rejected[key] = str(_e)
            if not dry_run and db_client and current_override:
                db_client.execute_update(
                    "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    ("kelly_config_override", json.dumps(current_override), json.dumps(current_override)),
                )
        except Exception as e:
            return {"ok": False, "error": f"scalar apply failed: {e}"}

    # Merge category_thresholds into kelly_config_override
    for threshold_key, threshold_dict, applied_list in [
        ("category_thresholds", category_thresholds, applied["category_thresholds"]),
        ("futures_category_thresholds", futures_category_thresholds, applied["futures_category_thresholds"]),
    ]:
        if threshold_dict and db_client:
            try:
                row = db_client.fetchone(
                    "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
                )
                current_override = {}
                if row and row.get("value"):
                    current_override = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
                existing = current_override.get(threshold_key, {})
                existing.update(threshold_dict)
                current_override[threshold_key] = existing
                if not dry_run:
                    db_client.execute_update(
                        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                        ("kelly_config_override", json.dumps(current_override), json.dumps(current_override)),
                    )
                applied_list.extend(list(threshold_dict.keys()))
            except Exception as e:
                rejected[threshold_key] = str(e)

    # Persist profile overrides to trading_runtime_state
    if profile_overrides and db_client:
        for key, value in profile_overrides.items():
            if not isinstance(value, dict):
                continue
            clean_key = key if key.startswith("regime_profile_") else f"regime_profile_{key.upper()}"
            try:
                if not dry_run:
                    db_client.execute_update(
                        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                        (clean_key, json.dumps({"params": value}), json.dumps({"params": value})),
                    )
                applied["profile_overrides"].append(clean_key)
            except Exception as e:
                rejected[clean_key] = str(e)

    # Audit event
    if not dry_run and db_client:
        try:
            db_client.execute_update(
                "INSERT INTO trades_audit (event, detail, created_at) VALUES (%s, %s, NOW())",
                ("mimic_config_applied", json.dumps({
                    "profile": profile,
                    "scalars": applied["scalars"],
                    "profile_overrides": applied["profile_overrides"],
                    "category_thresholds": applied["category_thresholds"],
                    "rejected": rejected,
                })),
            )
        except Exception:
            pass

    # F3: Save pre-apply config snapshot for rollback support
    if not dry_run and db_client:
        try:
            _snap = {"applied": applied, "params_before": {}, "applied_at": datetime.now(timezone.utc).isoformat()}
            _s_snap = get_settings()
            for k in applied.get("scalars", {}):
                _snap["params_before"][k] = getattr(_s_snap, k, None)
            db_client.execute_update(
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ("mimic_last_apply_snapshot", json.dumps(_snap), json.dumps(_snap)),
            )
        except Exception:
            pass

    return {
        "ok": True,
        "dry_run": dry_run,
        "applied": applied,
        "rejected": rejected,
        "profile": profile,
        "note": "dry_run — nothing persisted" if dry_run else "applied and audited",
    }


@app.post("/mimic/rollback-check", dependencies=[Depends(_require_api_key)])
async def mimic_rollback_check(request: Request):
    """F3: Evaluate post-apply performance and auto-rollback if drawdown>5% or PF<1.0.

    Reads recent closed trades since the last apply event, computes realised PnL
    and profit factor, and reverts ``kelly_config_override`` to the pre-apply
    snapshot if thresholds are breached.

    Body (all optional):
      max_drawdown_pct: float (default 5.0) — rollback if realised DD > this
      min_profit_factor: float (default 1.0) — rollback if PF < this
      min_trades: int (default 5) — require at least this many trades before evaluating
      dry_run: bool (default false) — evaluate only, do not revert
    """
    body = await request.json() if await request.body() else {}
    max_dd = float(body.get("max_drawdown_pct", 5.0))
    min_pf = float(body.get("min_profit_factor", 1.0))
    min_trades = int(body.get("min_trades", 5))
    dry_run_rb = bool(body.get("dry_run", False))

    if not db_client:
        return {"ok": False, "error": "db_client not available"}

    # Load snapshot
    try:
        snap_row = db_client.fetchone(
            "SELECT value FROM trading_runtime_state WHERE key='mimic_last_apply_snapshot'"
        )
        if not snap_row or not snap_row.get("value"):
            return {"ok": False, "error": "No apply snapshot found — call /mimic/apply-recommendation first"}
        snap = json.loads(snap_row["value"]) if isinstance(snap_row["value"], str) else snap_row["value"]
        applied_at = snap.get("applied_at")
    except Exception as e:
        return {"ok": False, "error": f"Snapshot load failed: {e}"}

    # Fetch trades closed since apply
    try:
        trades = db_client.fetchall(
            "SELECT pnl_usdt FROM trades_closed WHERE closed_at > %s AND pnl_usdt IS NOT NULL",
            (applied_at,),
        ) or []
    except Exception as e:
        return {"ok": False, "error": f"Trade fetch failed: {e}", "trades_evaluated": 0}

    if len(trades) < min_trades:
        return {
            "ok": True,
            "rollback_triggered": False,
            "reason": f"Only {len(trades)} trades since apply; need {min_trades} to evaluate",
            "trades_evaluated": len(trades),
        }

    pnl_values = [float(t.get("pnl_usdt", 0) or 0) for t in trades]
    gross_wins = sum(p for p in pnl_values if p > 0)
    gross_losses = sum(abs(p) for p in pnl_values if p < 0)
    pf = round(gross_wins / gross_losses, 4) if gross_losses > 0 else None
    cumulative = [sum(pnl_values[:i+1]) for i in range(len(pnl_values))]
    peak_cum = 0.0
    max_dd_realised = 0.0
    for c in cumulative:
        if c > peak_cum:
            peak_cum = c
        dd = (peak_cum - c) if peak_cum > 0 else 0.0
        if dd > max_dd_realised:
            max_dd_realised = dd
    # Convert dd to pct of initial portfolio (estimate: 2910 USDT)
    initial_est = 2910.0
    max_dd_pct = round(max_dd_realised / initial_est * 100, 4)

    triggers = []
    if max_dd_pct > max_dd:
        triggers.append(f"drawdown {max_dd_pct:.2f}% > {max_dd}%")
    if pf is not None and pf < min_pf:
        triggers.append(f"profit_factor {pf} < {min_pf}")

    if not triggers:
        return {
            "ok": True,
            "rollback_triggered": False,
            "reason": "Performance within thresholds",
            "trades_evaluated": len(trades),
            "max_drawdown_pct": max_dd_pct,
            "profit_factor": pf,
        }

    # Rollback: restore pre-apply params to kelly_config_override
    if not dry_run_rb:
        try:
            row = db_client.fetchone(
                "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
            )
            current_override = {}
            if row and row.get("value"):
                current_override = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
            for k, v in snap.get("params_before", {}).items():
                if v is not None:
                    current_override[k] = v
                    _live = get_settings()
                    try:
                        setattr(_live, k, v)
                    except Exception:
                        pass
            db_client.execute_update(
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ("kelly_config_override", json.dumps(current_override), json.dumps(current_override)),
            )
            db_client.execute_update(
                "INSERT INTO trades_audit (event, detail, created_at) VALUES (%s, %s, NOW())",
                ("mimic_config_rollback", json.dumps({
                    "triggers": triggers,
                    "max_dd_pct": max_dd_pct,
                    "profit_factor": pf,
                    "restored": snap.get("params_before", {}),
                    "trades_evaluated": len(trades),
                })),
            )
        except Exception as e:
            return {"ok": False, "error": f"Rollback failed: {e}", "triggers": triggers}

    return {
        "ok": True,
        "rollback_triggered": True,
        "dry_run": dry_run_rb,
        "triggers": triggers,
        "max_drawdown_pct": max_dd_pct,
        "profit_factor": pf,
        "trades_evaluated": len(trades),
        "note": "dry_run — rollback NOT applied" if dry_run_rb else "Config reverted to pre-apply snapshot",
    }


@app.get("/mimic/sweep-report", dependencies=[Depends(_require_api_key)])
async def mimic_sweep_report():
    """Return the latest MIMIC parameter sweep report if available."""
    import json
    report_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_sweep_report.json")
    if not os.path.exists(report_path):
        return {"ok": True, "report": None}
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e), "report": None}


@app.get("/mimic/sensitivity-report", dependencies=[Depends(_require_api_key)])
async def mimic_sensitivity_report():
    """Return the latest MIMIC sensitivity sweep report if available."""
    report_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_sensitivity_report.json")
    if not os.path.exists(report_path):
        return {"ok": True, "report": None}
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e), "report": None}


@app.post("/mimic/sensitivity", dependencies=[Depends(_require_api_key)])
async def mimic_sensitivity_start(request: Request):
    """Run MIMIC sensitivity sweep (1 param at a time) in background subprocess."""
    body = await request.json() if await request.body() else {}
    window_id = body.get("window")
    profile = body.get("profile", "B")
    max_cycles = body.get("max_cycles")

    script_path = os.path.join(os.path.dirname(__file__), "Dev", "scripts", "mimic_sensitivity_sweep.py")
    if not os.path.exists(script_path):
        return {"ok": False, "error": f"Script not found: {script_path}"}

    cmd = ["python", script_path, "--profile", profile]
    if window_id:
        cmd += ["--window", window_id]
    if max_cycles is not None:
        cmd += ["--max-cycles", str(max_cycles)]

    def _run():
        import subprocess
        _env = os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        log_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_sensitivity.log")
        with open(log_path, "a", encoding="utf-8") as lf:
            subprocess.run(cmd, cwd=os.path.dirname(__file__), env=_env, stdout=lf, stderr=subprocess.STDOUT, check=False)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Sensitivity sweep started.", "command": cmd}


@app.get("/mimic/bayesian-report", dependencies=[Depends(_require_api_key)])
async def mimic_bayesian_report():
    """Return the latest MIMIC Bayesian optimization report if available."""
    report_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_bayesian_report.json")
    if not os.path.exists(report_path):
        return {"ok": True, "report": None}
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e), "report": None}


@app.post("/mimic/bayesian", dependencies=[Depends(_require_api_key)])
async def mimic_bayesian_start(request: Request):
    """Run MIMIC Bayesian optimization in background subprocess."""
    body = await request.json() if await request.body() else {}
    window_id = body.get("window")
    profile = body.get("profile", "B")
    trials = body.get("trials", 50)
    max_cycles = body.get("max_cycles")
    timeout = body.get("timeout")

    script_path = os.path.join(os.path.dirname(__file__), "Dev", "scripts", "mimic_bayesian_opt.py")
    if not os.path.exists(script_path):
        return {"ok": False, "error": f"Script not found: {script_path}"}

    cmd = ["python", script_path, "--profile", profile, "--trials", str(trials)]
    if window_id:
        cmd += ["--window", window_id]
    if max_cycles is not None:
        cmd += ["--max-cycles", str(max_cycles)]
    if timeout is not None:
        cmd += ["--timeout", str(timeout)]

    def _run():
        import subprocess
        _env = os.environ.copy()
        _env["PYTHONIOENCODING"] = "utf-8"
        log_path = os.path.join(os.path.dirname(__file__), "Dev", "benchmarks", "mimic_bayesian.log")
        with open(log_path, "a", encoding="utf-8") as lf:
            subprocess.run(cmd, cwd=os.path.dirname(__file__), env=_env, stdout=lf, stderr=subprocess.STDOUT, check=False)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"Bayesian optimization started ({trials} trials).", "command": cmd}


@app.post("/mimic/paper/start", dependencies=[Depends(_require_api_key)])
async def mimic_paper_start(request: Request):
    """Start a MIMIC paper trade session against live prices without sending real orders."""
    body = await request.json() if await request.body() else {}
    duration_hours = float(body.get("duration_hours", 48))
    cycle_minutes = int(body.get("cycle_minutes", 60))
    initial_usdt = float(body.get("initial_usdt", 2910.0))
    symbols = body.get("symbols") or ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT"]
    try:
        from simulation.mimic_paper_trader import start_paper_trade
        from config import get_settings
        _paper_comp = get_components()
        paper_settings = get_settings()
        paper_settings.dry_run = True
        paper_settings.allow_real_trading = False
        status = start_paper_trade(
            settings=paper_settings,
            symbols=symbols,
            binance_client=_paper_comp.binance_client,
            duration_hours=duration_hours,
            cycle_minutes=cycle_minutes,
            initial_usdt=initial_usdt,
        )
        return {"ok": True, "message": f"Paper trade started ({duration_hours}h)", "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/mimic/paper/stop", dependencies=[Depends(_require_api_key)])
async def mimic_paper_stop():
    """Stop the running paper trade session."""
    try:
        from simulation.mimic_paper_trader import stop_paper_trade
        status = stop_paper_trade()
        return {"ok": True, "message": "Paper trade stopped", "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/mimic/paper/status", dependencies=[Depends(_require_api_key)])
async def mimic_paper_status():
    """Return current paper trade session status and PnL."""
    try:
        from simulation.mimic_paper_trader import get_paper_status
        return {"ok": True, "status": get_paper_status()}
    except Exception as e:
        return {"ok": False, "error": str(e), "status": {"status": "idle"}}
