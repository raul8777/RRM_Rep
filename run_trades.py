import math
import time
import types
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import error_collector as ec
from binance_client import BinanceClient
from market_regime import get_regime, MarketRegime, prime_cache_from_db as _prime_regime_from_db
from futures_client import FuturesClient
from news_client import NewsClient
from signal_store import get_signals, add_signal, mark_signal_as_processed, update_signal_decision as _store_update_signal_decision, expire_stale_signals
from strategy import rank_buy_signals, rank_with_technical, normalize_symbol, rank_for_portfolio_manager
from analysis_cache import analysis_cache
from autonomous_signals import AutonomousSignalGenerator
from trade_utils import align_quantity_to_step, get_symbol_filters, is_quantity_valid, compute_commission, get_binance_sell_qty
from binance_commission import get_config as _get_commission_config
import pnl_service
import json
import state_persistence
from peak_capture_engine import (
    PeakCaptureContext,
    PeakCaptureEngine,
    PeakCaptureDecision,
)
from config_helpers import get_param as _get_param
from profit_realization_supervisor import (
    ProfitRealizationContext,
    evaluate as _evaluate_profit_realization,
    mark_profit_started as _mark_profit_started,
    parse_dt as _parse_profit_dt,
)
from unified_governance_supervisor import (
    UnifiedGovernanceContext,
    evaluate as _evaluate_unified_governance,
)
from binance_truth_guard import (
    evaluate_futures_parity,
    evaluate_spot_parity,
    persist_truth_status,
    persist_truth_status_spot,
    audit_truth_mismatches,
    compute_basket_reset_decision,
)
from universe_selector import select_analysis_universe
from portfolio_selector import Candidate, select_for_regime, get_max_k_for_regime
from spot_correlation import get_correlation_matrix as _get_spot_correlation_matrix
from futures_correlation import get_correlation_matrix as _get_futures_correlation_matrix


# Symbols confirmed invalid on Binance (delisted) — skip without API call
_INVALID_SYMBOLS: set = {"EONUSDT", "ADDUSDT", "MEETONEUSDT", "ATDUSDT", "EOPUSDT", "ETHWUSDT"}
# Stablecoins/non-tradeable confirmed invalid on Binance Futures — never short these
_INVALID_FUTURES_SYMBOLS: set = {
    "USD1USDT", "PAXGUSDT", "XAUTUSDT", "RLUSDUSDT", "USDCUSDT", "BUSDUSDT",
    "TUSDUSDT", "FDUSDUSDT", "DAIUSDT", "USDDUSDT", "GUSDUSDT", "UUSDUSDT",
    "1000PEPEUSDT",
}
_INVALID_SYMBOLS_STATE_LOADED: bool = False

# VolSpike override cooldown: {symbol: datetime_of_last_override_entry}
# Prevents re-entering the same narrative token breakout every 5min cycle
_VOLSPIKE_COOLDOWN: Dict[str, datetime] = {}

# V2: Tracking de métricas VolSpike Override (jun 16 2026)
_VOLSPIKE_POSITIONS: set = set()           # Symbols con override activo
_OVERRIDE_DAILY_COUNT: Dict[str, int] = {}  # date_str -> count
_OVERRIDE_PERFORMANCE: list = []            # Lista de resultados para análisis

# Futures client singleton (lazy-initialized)
_futures_client: FuturesClient = None

# Cycle-scoped spot price cache to collapse duplicate lookups within one loop.
_cycle_price_cache: Dict[str, float] = {}
_selection_context: Dict[str, Any] = {}

# BUG-E2: Cycle-scoped ASG instance — reset each execute_once() call so all
# consumers (BTC trend check, futures inject, pnl_service) share one instance
# and benefit from the per-symbol signal cache without redundant re-instantiation.
_asg_cycle_instance: "AutonomousSignalGenerator" = None
_asg_cycle_id: int = 0  # incremented each cycle to know when to reset

# Whipsaw protection: count consecutive cycles where regime is NOT a short-friendly mode.
# Shorts are only closed when this counter reaches min_cycles_before_close_shorts (config).
_non_short_regime_cycles: int = 0
_transition_deadline_hits: int = 0

# Regime-specific parameter auto-loading: track the last regime we applied params for
# to avoid re-applying the same params every cycle.
_last_applied_regime_mode: Optional[str] = None
_last_applied_regime_profile: Optional[str] = None
_regime_param_baseline: Optional[Dict[str, Any]] = None

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
    "profile_rank": {"A": 200.0, "B": 200.0, "C": 200.0},  # s73 B1: equal base — ranking emerges from metadata performance
}


_FUTURES_DECISION_REASONS = {
    'MASS_SL_COOLDOWN', 'INVALID_FUTURES_SYM', 'NOT_FUTURES_SYM', 'SL_COOLDOWN',
    'LARGECAP_CAP', 'MAX_ESCALAMIENTOS', 'MAX_SHORTS', 'INSUF_MARGIN', 'BTC_MOMENTUM',
    'BTC_1H_BOUNCE', 'BTC_3H_RALLY', 'NO_RED_CANDLE', 'STAGGER_LIMIT', 'FUTURES_SHORT',
    'FUTURES_BLOCKED', 'FUTURES_LONG', 'FUTURES_LONG_BULL',
}


def _infer_trade_type_from_reason(reason: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    if not reason:
        return 'unknown'
    if reason.startswith('FUTURES_') or reason in _FUTURES_DECISION_REASONS:
        return 'futures'
    if reason in {
        'QTY_INVALIDA', 'SIN_SALDO', 'ERROR_ORDEN', 'MAX_POSIC', 'LIM_SIMBOLO',
        'ADD_STR_LOW', 'ADD_EV_LOW', 'ADD_NO_BASELINE', 'LIQUIDEZ', 'FUNDING_RATE',
        'PRESERVATION', 'PRSV_RSI', 'RANGE_RSI', 'RANGE_BB', 'EXCLUIDO', 'BTC_DOM',
        'NARRATIVE_CD', 'PNL_BAJO', 'ERROR_PRECIO', 'WR_BLOCK_3SL', 'WR_BLOCK_4SL',
        'EJECUTADO',
    } or reason.startswith('WR_BLOCK_'):
        return 'spot'
    return 'unknown'


def _audit_signal_decision(symbol: str, decision: str, reason: str, signal_id: Optional[str] = None,
                           trade_type: Optional[str] = None, binance_confirmed: bool = False) -> None:
    """Persist a signal decision into trades_audit and update signal_store."""
    inferred_trade_type = _infer_trade_type_from_reason(reason, explicit=trade_type)
    if inferred_trade_type == 'unknown':
        print(f"[Audit/Decision] WARNING: unknown trade_type for reason='{reason}' on {symbol} — defaulting to 'system'")
        inferred_trade_type = 'system'
    try:
        if _db_client_ref:
            db_detail = {
                'symbol': symbol,
                'decision': decision,
                'reason': reason,
                'signal_id': signal_id,
            }
            _db_client_ref.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                (symbol, inferred_trade_type, 'signal_decision', json.dumps(db_detail), binance_confirmed)
            )
    except Exception as _audit_err:
        print(f"[Audit/Decision] failed for {symbol}: {_audit_err}")
    _selection_record_decision(symbol, decision, reason, inferred_trade_type)
    _store_update_signal_decision(symbol, decision, reason, signal_id=signal_id)


update_signal_decision = _audit_signal_decision


def _selection_begin_cycle(db_client, regime_mode: str, profile_label: Optional[str]) -> str:
    global _selection_context
    cycle_id = str(uuid.uuid4())
    _selection_context = {
        "cycle_id": cycle_id,
        "db_client": db_client,
        "regime_mode": regime_mode,
        "profile_label": profile_label,
        "candidate_count": 0,
        "selected_count": 0,
        "executed_count": 0,
    }
    if db_client:
        try:
            db_client.execute_update(
                "INSERT INTO entry_selection_cycles (cycle_id, regime_mode, profile_label) VALUES (%s, %s, %s)",
                (cycle_id, regime_mode, profile_label),
            )
        except Exception as exc:
            print(f"[Selection] cycle persistence failed: {exc}")
    return cycle_id


def _selection_capture_candidates(candidates: List[Any], predictions: Dict[str, Dict], stage: str) -> None:
    context = _selection_context
    db_client = context.get("db_client")
    cycle_id = context.get("cycle_id")
    if not cycle_id:
        return
    for candidate in candidates:
        signal = candidate if isinstance(candidate, dict) else {"symbol": candidate}
        symbol = signal.get("symbol")
        if not symbol:
            continue
        prediction = predictions.get(symbol, {}) or {}
        trade_type = str(signal.get("trade_type") or ("futures" if context.get("regime_mode") == "FUTURES_SHORT" else "spot"))
        payload = {
            "strength": signal.get("strength"),
            "score": signal.get("score"),
            "profit_score": signal.get("profit_score"),
            "profit_score_components": signal.get("profit_score_components"),
            "vol_spike": signal.get("vol_spike"),
            "rsi": signal.get("rsi"),
            "higher_tf_trend": signal.get("higher_tf_trend"),
            "expected_pnl": prediction.get("expected_pnl"),
            "success_probability": prediction.get("success_probability"),
            "peak_quality_profile": signal.get("peak_quality_profile"),
        }
        if db_client:
            try:
                db_client.execute_update(
                    "INSERT INTO entry_selection_candidates (cycle_id, symbol, trade_type, stage, decision, data) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (cycle_id, symbol) DO UPDATE SET trade_type=EXCLUDED.trade_type, "
                    "stage=EXCLUDED.stage, data=entry_selection_candidates.data || EXCLUDED.data, updated_at=NOW()",
                    (cycle_id, symbol, trade_type, stage, "pending", json.dumps(payload)),
                )
            except Exception as exc:
                print(f"[Selection] candidate persistence failed for {symbol}: {exc}")
    context["candidate_count"] = max(context.get("candidate_count", 0), len(candidates))


def _selection_mark_stage(symbol: str, stage: str, reason: Optional[str] = None) -> None:
    context = _selection_context
    db_client = context.get("db_client")
    cycle_id = context.get("cycle_id")
    if not db_client or not cycle_id:
        return
    try:
        db_client.execute_update(
            "UPDATE entry_selection_candidates SET stage=%s, reason=%s, updated_at=NOW() WHERE cycle_id=%s AND symbol=%s",
            (stage, reason, cycle_id, symbol),
        )
    except Exception as exc:
        print(f"[Selection] stage persistence failed for {symbol}: {exc}")


def _selection_record_decision(symbol: str, decision: str, reason: str, trade_type: str) -> None:
    context = _selection_context
    db_client = context.get("db_client")
    cycle_id = context.get("cycle_id")
    if not db_client or not cycle_id:
        return
    stage = "executed" if decision == "executed" else "rejected"
    try:
        db_client.execute_update(
            "UPDATE entry_selection_candidates SET decision=%s, reason=%s, stage=%s, updated_at=NOW() "
            "WHERE cycle_id=%s AND symbol=%s",
            (decision, reason, stage, cycle_id, symbol),
        )
        if decision == "executed":
            context["executed_count"] = int(context.get("executed_count", 0)) + 1
    except Exception as exc:
        print(f"[Selection] decision persistence failed for {symbol}: {exc}")


def _selection_complete_cycle(summary: Optional[Dict[str, Any]] = None) -> None:
    global _selection_context
    context = _selection_context
    db_client = context.get("db_client")
    cycle_id = context.get("cycle_id")
    if db_client and cycle_id:
        payload = dict(summary or {})
        payload.setdefault("candidate_count", context.get("candidate_count", 0))
        payload.setdefault("selected_count", context.get("selected_count", 0))
        payload.setdefault("executed_count", context.get("executed_count", 0))
        try:
            db_client.execute_update(
                "UPDATE entry_selection_cycles SET completed_at=NOW(), candidate_count=%s, selected_count=%s, "
                "executed_count=%s, summary=%s::jsonb WHERE cycle_id=%s",
                (payload["candidate_count"], payload["selected_count"], payload["executed_count"], json.dumps(payload), cycle_id),
            )
        except Exception as exc:
            print(f"[Selection] cycle completion persistence failed: {exc}")
    _selection_context = {}


def _audit_screener_selection(db_client, regime_mode: str, screened: dict, profile_label: Optional[str] = None) -> None:
    """Persist screener candidate selection to trades_audit for full traceability."""
    if not db_client:
        return
    try:
        _meta = screened.get("_meta", {})
        _detail = {
            "regime_mode": regime_mode,
            "profile_label": profile_label,
            "spot_count": _meta.get("spot_count", 0),
            "futures_count": _meta.get("futures_count", 0),
            "total_candidates": _meta.get("total_candidates", 0),
            "top_bearish": _meta.get("top_bearish", []),
            "top_bullish": _meta.get("top_bullish", []),
            "futures_symbols": screened.get("futures", []),
            "spot_symbols": screened.get("spot", []),
        }
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("SCREENER", "system", "screener_selection", json.dumps(_detail), True)
        )
        _bridge_detail = {
            "regime_mode": regime_mode,
            "profile_label": profile_label,
            "spot_count": _meta.get("spot_count", 0),
            "futures_count": _meta.get("futures_count", 0),
            "total_candidates": _meta.get("total_candidates", 0),
        }
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("SCREENER", "system", "profile_screener_bridge", json.dumps(_bridge_detail), True)
        )
        print(f"[Audit/Screener] Persisted screener_selection for regime={regime_mode}")
    except Exception as _ae:
        print(f"[Audit/Screener] Failed to persist screener_selection: {_ae}")


def _get_futures_client(settings, db_client=None, injected_client=None) -> FuturesClient:
    """Lazy singleton for FuturesClient. Reuses instance across cycles.

    `injected_client` is used by MIMIC to pass a simulation mock. When provided,
    it overrides the singleton and is configured with the current settings/db.
    """
    global _futures_client
    if injected_client is not None:
        fc = injected_client
        fc._cached_settings = settings
        fc._active_leverage = min(getattr(settings, 'futures_leverage', 10), 10)
        fc.max_shorts = min(getattr(settings, 'futures_max_shorts', 5), 12)
        fc.max_longs = getattr(settings, 'futures_max_longs', 2)
        fc.max_largecap_shorts = getattr(settings, 'futures_max_largecap_shorts', 3)
        fc.largecap_symbols = set(getattr(settings, 'futures_largecap_symbols', ['BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','SOLUSDT']))
        if db_client and not fc.db_client:
            fc.db_client = db_client
        return fc
    # FIX-G2: Auto-resolve db_client from components if not provided
    if db_client is None:
        try:
            from init_components import get_components as _get_comp
            _comp = _get_comp()
            db_client = getattr(_comp, 'db_client', None)
        except Exception:
            db_client = None
    if _futures_client is None:
        _futures_client = FuturesClient(
            api_key    = settings.binance_api_key,
            api_secret = settings.binance_api_secret,
            dry_run    = settings.dry_run,
            max_shorts = getattr(settings, 'futures_max_shorts', 5),
            max_longs  = getattr(settings, 'futures_max_longs', 2),
            db_client  = db_client,  # FIX-G: DB persistence
            settings   = settings,     # FIX-G3: settings for commission/config lookups
        )
        if getattr(settings, 'futures_hedge_mode_enabled', True):
            _futures_client.enable_hedge_mode()
        print("[Futures] FuturesClient initialized")
    # FIX-G: Attach db_client if it became available after first init (e.g. DB reconnect)
    if db_client and not _futures_client.db_client:
        _futures_client.db_client = db_client
    # Always sync category cap and leverage from settings (survives hot-config changes)
    _futures_client._cached_settings = settings
    _futures_client._active_leverage = min(
        getattr(settings, 'futures_leverage', 10), 10
    )
    _futures_client.max_shorts = min(
        getattr(settings, 'futures_max_shorts', 5), 12
    )
    _futures_client.max_largecap_shorts = getattr(settings, 'futures_max_largecap_shorts', 3)
    _lc_raw = getattr(settings, 'futures_largecap_symbols', ['BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','SOLUSDT'])
    _futures_client.largecap_symbols = set(_lc_raw)
    return _futures_client


def _get_cycle_asg(client: "BinanceClient") -> "AutonomousSignalGenerator":
    """Return the cycle-scoped ASG instance, creating it if needed.
    Shared across _buy_top_symbols() and execute_once() futures inject to avoid
    redundant instantiation (BUG-E2 fix). Reset at the start of each execute_once()."""
    global _asg_cycle_instance
    if _asg_cycle_instance is None:
        _asg_cycle_instance = AutonomousSignalGenerator(client)
    return _asg_cycle_instance


def _wire_futures_client(fc: FuturesClient, client, settings) -> None:
    """Attach BinanceClient reference to FuturesClient for momentum evaluation."""
    fc._binance_client_ref = client


def _get_cycle_price(client: BinanceClient, symbol: str) -> float:
    """Return a symbol price using a per-cycle cache first, then BinanceClient."""
    cached = _cycle_price_cache.get(symbol)
    if cached is not None:
        return cached
    price = client.get_symbol_price(symbol)
    _cycle_price_cache[symbol] = price
    return price


_VOLSPIKE_COOLDOWN_HOURS = 4.0

# NARRATIVE token buy cooldown: prevent buying the same symbol multiple times
# within a single block of cycles (prevents 3 FET entries in 10 minutes)
_NARRATIVE_BUY_COOLDOWN: Dict[str, datetime] = {}
_NARRATIVE_BUY_COOLDOWN_HOURS = 4.0

# Overflow positions: symbols opened beyond the regime max via the quality-overflow mechanism.
# Cleared on Docker restart — slots replenish organically on the next cycle.
_overflow_positions: set = set()

# Futures overflow: SHORTs opened beyond futures_max_shorts when signal is exceptional.
_futures_overflow_positions: set = set()

# FIX-F1: Futures SHORT SL cooldown — prevent re-entering same symbol for N minutes after SL hit.
# {symbol: datetime_of_sl_hit}  (UTC)
_FUTURES_SL_COOLDOWN: Dict[str, datetime] = {}

# PB2: Mass SL circuit breaker — global pause when 3+ positions stop in one check cycle
_FUTURES_MASS_SL_COOLDOWN_UNTIL: Optional[datetime] = None  # UTC — None = not paused

# B3: SPOT mass-SL cooldown — pause new SPOT entries when N SLs hit in same cycle
_SPOT_MASS_SL_COOLDOWN_UNTIL: Optional[datetime] = None  # UTC — None = not paused

# SSO-7: Best candidate cache — best non-held signal from previous cycle's scanner output.
# Populated after Step 2 predictions; consumed at next cycle's Step 0 monitoring.
# Format: {"symbol": str, "strength": float, "prob": float, "vol_spike": float, "ev": float}
_SSO7_BEST_CANDIDATE_CACHE: Optional[Dict] = None

# ── Circuit breaker: consecutive order failures (technical errors only) ──────
_consecutive_failures: int = 0
_circuit_breaker_until: datetime = None  # UTC — None = not paused

# ── Funding rate cache: {symbol: (rate_float, fetched_at_monotonic)} ─────────
_funding_rate_cache: Dict[str, tuple] = {}

# ── DB client reference for cooldown persistence (set in execute_once) ───────
_db_client_ref = None

# ── Audit startup flag: run excluded-asset cleanup only once per process ─────
_AUDIT_STARTUP_DONE: bool = False


def _request_smart_recheck(reason: str, context: Optional[Dict[str, Any]] = None) -> None:
    """Emit an event-driven SMART health recheck request through trading_runtime_state."""
    if not _db_client_ref:
        return
    try:
        _payload = json.dumps({
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "source": "run_trades",
            "reason": str(reason or "cycle_event"),
            "context": context or {},
        })
        _db_client_ref.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("health_results_recheck_request", _payload, _payload),
        )
    except Exception as _rq_err:
        print(f"[SMART-HEALTH] request from run_trades failed: {_rq_err}")


def _get_spot_daily_pnl() -> float:
    """P1.1: Return today's realized SPOT PnL from BD via daily_pnl_tracker (thread-safe, restart-safe)."""
    try:
        from daily_pnl_tracker import get_daily_realized_pnl as _gdpnl
        return _gdpnl(_db_client_ref, trade_type='spot')
    except Exception:
        return 0.0


def _get_active_profile_label(db_client, regime: str) -> Optional[str]:
    """Read active regime profile label (A/B/C) from runtime state."""
    if db_client is None or not regime:
        return None
    try:
        _rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s",
            (f"regime_profile_active_{regime}",),
        )
        if not _rows:
            return None
        _v = _rows[0].get('value')
        if isinstance(_v, str):
            try:
                _v = json.loads(_v)
            except Exception:
                _v = _v.strip() if isinstance(_v, str) else _v
        if isinstance(_v, dict):
            _p = str(_v.get('profile') or _v.get('label') or '').strip().upper()
            return _p or None
        _p = str(_v or '').strip().upper()
        return _p or None
    except Exception:
        return None


def _set_cooldown(symbol: str, cooldown_type: str, hours: float, metadata: dict = None) -> None:
    """Persist a cooldown to DB + update RAM dict. Type: 'volspike'|'narrative'|'futures_sl'|'override_daily'."""
    from datetime import timedelta as _td
    _now = datetime.now(timezone.utc)
    _expires = _now + _td(hours=hours)
    if cooldown_type == 'volspike':
        _VOLSPIKE_COOLDOWN[symbol] = _now
    elif cooldown_type == 'narrative':
        _NARRATIVE_BUY_COOLDOWN[symbol] = _now
    elif cooldown_type == 'futures_sl':
        _FUTURES_SL_COOLDOWN[symbol] = _now
    if _db_client_ref and getattr(_db_client_ref, 'execute_update', None):
        try:
            import json as _json
            _db_client_ref.execute_update(
                """INSERT INTO trading_cooldowns (symbol, cooldown_type, activated_at, expires_at, metadata)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (symbol, cooldown_type) DO UPDATE
                   SET activated_at=EXCLUDED.activated_at, expires_at=EXCLUDED.expires_at, metadata=EXCLUDED.metadata""",
                (symbol, cooldown_type, _now.isoformat(), _expires.isoformat(),
                 _json.dumps(metadata) if metadata else None)
            )
        except Exception as _cd_err:
            print(f"[Cooldown] Failed to persist {cooldown_type} for {symbol}: {_cd_err}")


def _check_cooldown_db(symbol: str, cooldown_type: str) -> bool:
    """Check DB for an active cooldown (fallback when RAM is empty, e.g. after restart)."""
    if not _db_client_ref:
        return False
    try:
        rows = _db_client_ref.execute_query(
            "SELECT expires_at FROM trading_cooldowns WHERE symbol=%s AND cooldown_type=%s",
            (symbol, cooldown_type)
        )
        if rows:
            from datetime import datetime as _dt_db
            _exp = rows[0]['expires_at']
            if isinstance(_exp, str):
                _exp = _dt_db.fromisoformat(_exp.replace('Z', '+00:00'))
            if _exp.tzinfo is None:
                _exp = _exp.replace(tzinfo=timezone.utc)
            return _exp > datetime.now(timezone.utc)
    except Exception:
        pass
    return False


def _load_cooldowns_from_db() -> None:
    """Repopulate RAM cooldown dicts from DB at startup (survives Docker restart)."""
    if not _db_client_ref:
        return
    try:
        rows = _db_client_ref.execute_query(
            "SELECT symbol, cooldown_type, activated_at FROM trading_cooldowns WHERE expires_at > NOW()"
        )
        loaded = 0
        for row in rows:
            _sym = row['symbol']
            _type = row['cooldown_type']
            _act = row['activated_at']
            if isinstance(_act, str):
                _act = datetime.fromisoformat(_act.replace('Z', '+00:00'))
            if _act.tzinfo is None:
                _act = _act.replace(tzinfo=timezone.utc)
            if _type == 'volspike':
                _VOLSPIKE_COOLDOWN[_sym] = _act
            elif _type == 'narrative':
                _NARRATIVE_BUY_COOLDOWN[_sym] = _act
            elif _type == 'futures_sl':
                _FUTURES_SL_COOLDOWN[_sym] = _act
            loaded += 1
        if loaded:
            print(f"[Cooldowns] Loaded {loaded} active cooldowns from DB")
    except Exception as _le:
        print(f"[Cooldowns] Could not load from DB: {_le}")


def _get_max_positions_for_regime(settings, regime: str) -> int:
    """Return the optimal max-open-positions for the current market regime.
    Falls back to settings.max_positions if regime is unknown."""
    _mapping = {
        "SPOT_BULL":     _get_param(settings, 'max_positions_spot_bull'),
        "RANGE_SCALP":   _get_param(settings, 'max_positions_range_scalp'),
        "PRESERVATION":  _get_param(settings, 'max_positions_preservation'),
        "FUTURES_SHORT": _get_param(settings, 'max_positions_futures_short'),
    }
    return _mapping.get(regime, _get_param(settings, 'max_positions'))


def _ensure_regime_param_baseline(settings, scalar_targets: List[str]) -> Dict[str, Any]:
    """Capture immutable baseline values used to reset absent regime/profile params.

    This prevents parameter bleed-through when a profile only defines a partial payload.
    """
    global _regime_param_baseline
    if _regime_param_baseline is None:
        _regime_param_baseline = {
            'scalars': {},
            'category_thresholds': deepcopy(getattr(settings, 'category_thresholds', {}) or {}),
            'futures_category_thresholds': deepcopy(getattr(settings, 'futures_category_thresholds', {}) or {}),
        }
    for _attr in scalar_targets:
        if _attr not in _regime_param_baseline['scalars']:
            _regime_param_baseline['scalars'][_attr] = deepcopy(getattr(settings, _attr, None))
    return _regime_param_baseline


def _apply_regime_params(settings, regime: str, db_client=None) -> Dict:
    """
    Load regime-specific parameter overrides from trading_runtime_state and apply them
    to the live settings object. Only runs when the regime changes to avoid redundant work.
    Persisted under keys like 'regime_params_SPOT_BULL'.
    Mirrors the audit trail used by PATCH /config/kelly.
    """
    global _last_applied_regime_mode, _last_applied_regime_profile
    if not regime:
        return {"applied": {}, "skipped": True, "reason": "no_regime"}

    if db_client is None:
        return {"applied": {}, "skipped": True, "reason": "no_db_client"}

    try:
        import json as _json
        _active_profile = None
        _active_profile_key = f"regime_profile_active_{regime}"
        _active_rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s", (_active_profile_key,)
        )
        if _active_rows and _active_rows[0].get('value') is not None:
            _av = _active_rows[0]['value']
            if isinstance(_av, str):
                try:
                    _av = _json.loads(_av)
                except Exception:
                    pass
            if isinstance(_av, dict):
                _active_profile = str(_av.get('profile') or _av.get('label') or '').strip() or None
            elif isinstance(_av, (int, float, bool)):
                _active_profile = str(_av)
            elif isinstance(_av, str):
                _active_profile = _av.strip() or None

        _profile_token = _active_profile or "__default__"
        if regime == _last_applied_regime_mode and _profile_token == (_last_applied_regime_profile or "__default__"):
            return {"applied": {}, "skipped": True, "reason": "already_active"}

        _state_key = f"regime_params_{regime}"
        _raw = None

        # New profile-aware source (preferred): regime_profile_<REGIME>_<LABEL>
        if _active_profile:
            _profile_key = f"regime_profile_{regime}_{_active_profile}"
            _profile_rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key=%s", (_profile_key,)
            )
            if _profile_rows and _profile_rows[0].get('value') is not None:
                _raw = _profile_rows[0]['value']
                _state_key = _profile_key

        # Legacy fallback: regime_params_<REGIME>
        if _raw is None:
            _rows = db_client.execute_query(
                "SELECT value FROM trading_runtime_state WHERE key=%s", (_state_key,)
            )
            if not _rows or _rows[0].get('value') is None:
                return {"applied": {}, "skipped": True, "reason": "no_regime_params_stored", "profile": _active_profile}
            _raw = _rows[0]['value']

        if isinstance(_raw, str):
            _raw = _json.loads(_raw)
        if isinstance(_raw, dict) and isinstance(_raw.get('params'), dict):
            _raw = _raw.get('params')
        if not isinstance(_raw, dict):
            return {"applied": {}, "skipped": True, "reason": "invalid_regime_params_format"}

        # Scalar params mapped directly to settings attributes
        _scalar_map = {
            'kelly_base_usdt': 'kelly_base_usdt',
            'kelly_max_usdt': 'kelly_max_usdt',
            'kelly_max_pct_liquid': 'kelly_max_pct_liquid',
            'kelly_min_usdt': 'kelly_min_usdt',
            'kelly_dynamic_base_pct': 'kelly_dynamic_base_pct',
            'kelly_max_pct_cap': 'kelly_max_pct_cap',
            'kelly_conviction_max_pct': 'kelly_conviction_max_pct',
            'futures_kelly_max_usdt': 'futures_kelly_max_usdt',
            'futures_kelly_base_usdt': 'futures_kelly_base_usdt',
            'futures_kelly_max_pct_liquid': 'futures_kelly_max_pct_liquid',
            'futures_kelly_cap_pct': 'futures_kelly_cap_pct',
            'futures_kelly_min_usdt': 'futures_kelly_min_usdt',
            'fng_capitulation_threshold': 'fng_capitulation_threshold',
            'fng_extreme_fear_threshold': 'fng_extreme_fear_threshold',
            'fng_extreme_greed_threshold': 'fng_extreme_greed_threshold',
            'fng_capitulation_multiplier': 'fng_capitulation_multiplier',
            'fng_extreme_fear_multiplier': 'fng_extreme_fear_multiplier',
            'fng_extreme_greed_multiplier': 'fng_extreme_greed_multiplier',
            'fng_kelly_boost_enabled': 'fng_kelly_boost_enabled',
            'kelly_conviction_enabled': 'kelly_conviction_enabled',
            'kelly_conviction_vol2x': 'kelly_conviction_vol2x',
            'kelly_conviction_vol3x': 'kelly_conviction_vol3x',
            'kelly_conviction_max_pct': 'kelly_conviction_max_pct',
            'min_signal_strength': 'min_signal_strength',
            'min_success_probability': 'min_success_probability',
            'vol_spike_min': 'vol_spike_min',
            'futures_min_prob': 'futures_min_prob',
            'futures_vol_spike_min': 'futures_vol_spike_min',
            'stop_loss_pct': 'stop_loss_pct',
            'take_profit_pct': 'take_profit_pct',
            'smart_tp_max_pnl_pct': 'smart_tp_max_pnl_pct',
            'smart_tp_enabled': 'smart_tp_enabled',
            'smart_tp_max_extensions': 'smart_tp_max_extensions',
            'smart_tp_extend_score_threshold': 'smart_tp_extend_score_threshold',
            'scale_out_enabled': 'scale_out_enabled',
            'scale_out_first_close_pct': 'scale_out_first_close_pct',
            'futures_sl_pct': 'futures_sl_pct',
            'futures_tp_pct': 'futures_tp_pct',
            'futures_smart_tp_enabled': 'futures_smart_tp_enabled',
            'futures_max_new_per_cycle': 'futures_max_new_per_cycle',
            'futures_confirm_red_candle': 'futures_confirm_red_candle',
            'futures_no_short_btc_4h_threshold': 'futures_no_short_btc_4h_threshold',
            'futures_no_short_btc_3h_threshold': 'futures_no_short_btc_3h_threshold',
            'futures_no_short_btc_1h_threshold': 'futures_no_short_btc_1h_threshold',
            'futures_mass_sl_threshold': 'futures_mass_sl_threshold',
            'futures_mass_sl_cooldown_hours': 'futures_mass_sl_cooldown_hours',
            'max_positions_spot_bull': 'max_positions_spot_bull',
            'max_positions_range_scalp': 'max_positions_range_scalp',
            'max_positions_preservation': 'max_positions_preservation',
            'max_positions_futures_short': 'max_positions_futures_short',
            'portfolio_concentration_enabled': 'portfolio_concentration_enabled',
            'portfolio_concentration_alpha': 'portfolio_concentration_alpha',
            'portfolio_concentration_min_mult': 'portfolio_concentration_min_mult',
            'portfolio_concentration_max_mult': 'portfolio_concentration_max_mult',
            'portfolio_concentration_pressure_start': 'portfolio_concentration_pressure_start',
            'portfolio_concentration_pressure_boost': 'portfolio_concentration_pressure_boost',
            'portfolio_concentration_baseline_spot_bull': 'portfolio_concentration_baseline_spot_bull',
            'portfolio_concentration_baseline_range_scalp': 'portfolio_concentration_baseline_range_scalp',
            'portfolio_concentration_baseline_preservation': 'portfolio_concentration_baseline_preservation',
            'portfolio_concentration_baseline_futures_short': 'portfolio_concentration_baseline_futures_short',
            'max_total_exposure_pct': 'max_total_exposure_pct',
            'max_total_exposure_spot_bull_fear': 'max_total_exposure_spot_bull_fear',
            'min_hold_minutes': 'min_hold_minutes',
            'min_hold_minutes_range_scalp': 'min_hold_minutes_range_scalp',
            'max_rebalance_per_cycle': 'max_rebalance_per_cycle',
            'min_hold_minutes_spot_bull': 'min_hold_minutes_spot_bull',
            'min_hold_for_prediction_exit': 'min_hold_for_prediction_exit',
            'min_hold_for_prediction_exit_range_scalp': 'min_hold_for_prediction_exit_range_scalp',  # s71 BUG-4
            'min_hold_for_prediction_exit_futures_short': 'min_hold_for_prediction_exit_futures_short',
            # Wolf Hunter Peak Capture Engine
            'profit_predexit_enabled': 'profit_predexit_enabled',
            'profit_predexit_capture_margin_A': 'profit_predexit_capture_margin_A',
            'profit_predexit_capture_margin_B': 'profit_predexit_capture_margin_B',
            'profit_predexit_capture_margin_C': 'profit_predexit_capture_margin_C',
            'profit_predexit_size_sensitivity': 'profit_predexit_size_sensitivity',
            'profit_predexit_conviction_sensitivity': 'profit_predexit_conviction_sensitivity',
            'profit_predexit_min_profit_pct': 'profit_predexit_min_profit_pct',
            'profit_predexit_absolute_min_usd': 'profit_predexit_absolute_min_usd',
            'profit_predexit_horizon_minutes': 'profit_predexit_horizon_minutes',
            'profit_predexit_giveback_penalty_weight': 'profit_predexit_giveback_penalty_weight',
            'peak_reconstruction_enabled': 'peak_reconstruction_enabled',
            'peak_reconstruction_lookback_candles': 'peak_reconstruction_lookback_candles',
            'min_cycles_before_close_shorts': 'min_cycles_before_close_shorts',
            'futures_breakeven_activation_pct': 'futures_breakeven_activation_pct',
            'futures_trailing_activation_pct': 'futures_trailing_activation_pct',
            'futures_sl_cooldown_hours': 'futures_sl_cooldown_hours',
            'futures_adverse_exit_enabled': 'futures_adverse_exit_enabled',
            'futures_adverse_exit_loss_pct': 'futures_adverse_exit_loss_pct',
            'futures_adverse_exit_min_hours': 'futures_adverse_exit_min_hours',
            'futures_adverse_exit_min_strength': 'futures_adverse_exit_min_strength',
            'futures_smart_tp_max_pnl_pct': 'futures_smart_tp_max_pnl_pct',
            'futures_smart_tp_max_extensions': 'futures_smart_tp_max_extensions',
            'futures_smart_tp_extend_score_threshold': 'futures_smart_tp_extend_score_threshold',
            'futures_bounce_rescue_enabled': 'futures_bounce_rescue_enabled',
            'futures_bounce_rescue_max_peak_pct': 'futures_bounce_rescue_max_peak_pct',
            'futures_bounce_rescue_min_hours': 'futures_bounce_rescue_min_hours',
            'futures_bounce_rescue_target_pnl': 'futures_bounce_rescue_target_pnl',
            'spot_bounce_rescue_enabled': 'spot_bounce_rescue_enabled',
            'spot_bounce_rescue_max_peak_pct': 'spot_bounce_rescue_max_peak_pct',
            'spot_bounce_rescue_min_hours': 'spot_bounce_rescue_min_hours',
            'spot_bounce_rescue_target_pnl': 'spot_bounce_rescue_target_pnl',
            'futures_momentum_reversal_enabled': 'futures_momentum_reversal_enabled',
            'futures_momentum_reversal_min_peak': 'futures_momentum_reversal_min_peak',
            'futures_momentum_reversal_drawback': 'futures_momentum_reversal_drawback',
            'rescue_smart_gate_enabled': 'rescue_smart_gate_enabled',
            'rescue_stay_strength_min': 'rescue_stay_strength_min',
            'rescue_stay_ev_multiplier': 'rescue_stay_ev_multiplier',
            'rescue_velocity_min_pct': 'rescue_velocity_min_pct',
            'rescue_gate_min_concurrent': 'rescue_gate_min_concurrent',
            'rescue_gate_g1_min_pct': 'rescue_gate_g1_min_pct',
            'rescue_gate_g1_spot_min_pct': 'rescue_gate_g1_spot_min_pct',
            'rescue_gate_vol_min_spike': 'rescue_gate_vol_min_spike',
            'rescue_gate_max_blocks': 'rescue_gate_max_blocks',
            'rescue_gate_min_pnl_improvement': 'rescue_gate_min_pnl_improvement',
            'volspike_override_enabled': 'volspike_override_enabled',
            'volspike_override_min_spike': 'volspike_override_min_spike',
            'volspike_override_max_rsi': 'volspike_override_max_rsi',
            'volspike_override_trend_required': 'volspike_override_trend_required',
            'volspike_override_narrative_only': 'volspike_override_narrative_only',
            'volspike_override_min_strength': 'volspike_override_min_strength',
            'volspike_override_max_positions': 'volspike_override_max_positions',
            'volspike_override_daily_limit': 'volspike_override_daily_limit',
            'btc_dominance_filter_enabled': 'btc_dominance_filter_enabled',
            'btc_dominance_altcoin_threshold': 'btc_dominance_altcoin_threshold',
            'liquidity_filter_enabled': 'liquidity_filter_enabled',
            'min_liquidity_vol_usdt': 'min_liquidity_vol_usdt',
            'wr_screener_enabled': 'wr_screener_enabled',
            'wr_screener_consecutive_sl_block': 'wr_screener_consecutive_sl_block',
            'sso_target_daily_usd': 'sso_target_daily_usd',
            'profit_realization_enabled': 'profit_realization_enabled',
            'profit_realization_spot_enabled': 'profit_realization_spot_enabled',
            'profit_realization_spot_min_profit_pct': 'profit_realization_spot_min_profit_pct',
            'profit_realization_spot_partial_minutes': 'profit_realization_spot_partial_minutes',
            'profit_realization_spot_full_minutes': 'profit_realization_spot_full_minutes',
            'profit_realization_spot_drawback_pct': 'profit_realization_spot_drawback_pct',
            'profit_realization_spot_partial_close_pct': 'profit_realization_spot_partial_close_pct',
            'profit_realization_spot_max_extensions': 'profit_realization_spot_max_extensions',
            'profit_realization_futures_enabled': 'profit_realization_futures_enabled',
            'profit_realization_futures_min_profit_pct': 'profit_realization_futures_min_profit_pct',
            'profit_realization_futures_partial_minutes': 'profit_realization_futures_partial_minutes',
            'profit_realization_futures_full_minutes': 'profit_realization_futures_full_minutes',
            'profit_realization_futures_drawback_pct': 'profit_realization_futures_drawback_pct',
            'profit_realization_futures_partial_close_pct': 'profit_realization_futures_partial_close_pct',
            'profit_realization_futures_max_extensions': 'profit_realization_futures_max_extensions',
            'circuit_breaker_enabled': 'circuit_breaker_enabled',
            'circuit_breaker_consecutive_failures': 'circuit_breaker_consecutive_failures',
            'circuit_breaker_pause_minutes': 'circuit_breaker_pause_minutes',
            'screener_min_vol_usdt': 'screener_min_vol_usdt',
            'screener_futures_min_vol': 'screener_futures_min_vol',
            'screener_max_pairs': 'screener_max_pairs',
            'screener_max_futures_pairs': 'screener_max_futures_pairs',
            'screener_cache_minutes': 'screener_cache_minutes',
            'screener_snapshot_max_age_minutes': 'screener_snapshot_max_age_minutes',
            'sso_slow_velocity_threshold': 'sso_slow_velocity_threshold',
            'sso_slow_min_hours': 'sso_slow_min_hours',
            'sso_stagnant_min_hours': 'sso_stagnant_min_hours',
            'sso_decay_min_hours': 'sso_decay_min_hours',
            'sso_decay_threshold_pct': 'sso_decay_threshold_pct',
            'sso_momentum_min_pnl_pct_spot': 'sso_momentum_min_pnl_pct_spot',
            'sso_momentum_min_pnl_pct_futures': 'sso_momentum_min_pnl_pct_futures',
            # SSO-1d: Flat Stagnation Timeout
            'sso_flat_stagnation_timeout_enabled': 'sso_flat_stagnation_timeout_enabled',
            'sso_flat_stagnation_timeout_hours': 'sso_flat_stagnation_timeout_hours',
            'sso_flat_stagnation_timeout_hours_range_scalp': 'sso_flat_stagnation_timeout_hours_range_scalp',
            'sso_flat_stagnation_timeout_pnl_pct': 'sso_flat_stagnation_timeout_pnl_pct',
            'sso_flat_stagnation_timeout_pnl_pct_range_scalp': 'sso_flat_stagnation_timeout_pnl_pct_range_scalp',
            'slippage_pct': 'slippage_pct',
            'use_bnb_for_commission': 'use_bnb_for_commission',
            'hybrid_optimizer_enabled': 'hybrid_optimizer_enabled',
            'auto_apply_backtest': 'auto_apply_backtest',
            'backtest_safe_only': 'backtest_safe_only',
            'rebalance_max_abs_pnl_pct': 'rebalance_max_abs_pnl_pct',  # I3: s66 field — allow regime profiles to tune rebalance cap
            # s70: SSO per-regime timing — absolute final values, globals NEVER modified
            'sso_stagnant_min_hours_spot_bull':     'sso_stagnant_min_hours_spot_bull',
            'sso_stagnant_min_hours_range_scalp':   'sso_stagnant_min_hours_range_scalp',
            'sso_stagnant_min_hours_preservation':  'sso_stagnant_min_hours_preservation',
            'sso_stagnant_min_hours_futures_short': 'sso_stagnant_min_hours_futures_short',
            'sso_slow_min_hours_spot_bull':         'sso_slow_min_hours_spot_bull',
            'sso_slow_min_hours_range_scalp':       'sso_slow_min_hours_range_scalp',
            'sso_slow_min_hours_preservation':      'sso_slow_min_hours_preservation',
            'sso_slow_min_hours_futures_short':     'sso_slow_min_hours_futures_short',
            'sso_decay_min_hours_spot_bull':        'sso_decay_min_hours_spot_bull',
            'sso_decay_min_hours_range_scalp':      'sso_decay_min_hours_range_scalp',
            'sso_decay_min_hours_preservation':     'sso_decay_min_hours_preservation',
            'sso_decay_min_hours_futures_short':    'sso_decay_min_hours_futures_short',
            # s70: entry quality per regime
            'entry_quality_max_failures_spot_bull':      'entry_quality_max_failures_spot_bull',
            'entry_quality_max_failures_range_scalp':    'entry_quality_max_failures_range_scalp',
            'entry_quality_max_failures_preservation':   'entry_quality_max_failures_preservation',
            'entry_quality_max_failures_futures_short':  'entry_quality_max_failures_futures_short',
            'entry_quality_vol_spike_min_spot_bull':     'entry_quality_vol_spike_min_spot_bull',
            'entry_quality_vol_spike_floor_spot_bull':   'entry_quality_vol_spike_floor_spot_bull',
            'entry_quality_rsi_min_spot_bull':           'entry_quality_rsi_min_spot_bull',
            'entry_quality_rsi_max_spot_bull':           'entry_quality_rsi_max_spot_bull',
            'entry_quality_breakout_min_vol_spot_bull':  'entry_quality_breakout_min_vol_spot_bull',
            'entry_quality_breakout_rsi_max_spot_bull':  'entry_quality_breakout_rsi_max_spot_bull',
            'entry_quality_breakout_min_strength_spot_bull': 'entry_quality_breakout_min_strength_spot_bull',
            # s70: SmartTP EXTEND per regime
            'smart_tp_extend_enabled_spot_bull':    'smart_tp_extend_enabled_spot_bull',
            'smart_tp_extend_enabled_range_scalp':  'smart_tp_extend_enabled_range_scalp',
            'smart_tp_extend_enabled_preservation': 'smart_tp_extend_enabled_preservation',
            'smart_tp_extend_enabled_futures_short':'smart_tp_extend_enabled_futures_short',
            'smart_tp_score_threshold_range_scalp': 'smart_tp_score_threshold_range_scalp',
            # s70: BB touch tolerance per mode
            'bb_lower_touch_tolerance_pct':                 'bb_lower_touch_tolerance_pct',
            'bb_lower_touch_tolerance_range_scalp_pct':     'bb_lower_touch_tolerance_range_scalp_pct',
            # s70: RANGE_SCALP new fields
            'range_scalp_min_rsi':          'range_scalp_min_rsi',
            'range_scalp_breakeven_pct':    'range_scalp_breakeven_pct',
            'range_scalp_sl_pct':           'range_scalp_sl_pct',
            'range_scalp_tp_pct':           'range_scalp_tp_pct',
            # s72: RANGE_SCALP entry gate new fields — fully DB-governable per profile
            'range_scalp_max_rsi':                        'range_scalp_max_rsi',
            'range_scalp_max_bb_position':                'range_scalp_max_bb_position',
            'entry_quality_vol_spike_min_range_scalp':    'entry_quality_vol_spike_min_range_scalp',
            # s72: FUTURES_SHORT PredExit hold floor
            'min_hold_for_prediction_exit_futures_short': 'min_hold_for_prediction_exit_futures_short',
            # s73 C2: SSO-4 target contribution per regime
            'sso_target_contribution_pct_range_scalp':    'sso_target_contribution_pct_range_scalp',
            # s75: category limit hot-config governance
            'category_max_positions_enabled': 'category_max_positions_enabled',
        }

        _applied = {}
        _rejected = {}

        _scalar_targets = list(dict.fromkeys(_scalar_map.values()))
        _baseline = _ensure_regime_param_baseline(settings, _scalar_targets)
        _baseline_scalars = _baseline.get('scalars', {})

        for _src, _dst in _scalar_map.items():
            try:
                _cur = getattr(settings, _dst, None)
                _target = _raw.get(_src, _baseline_scalars.get(_dst, _cur))
                if _cur is None:
                    _new_val = _target
                elif _target is None:
                    _new_val = None
                else:
                    _new_val = type(_cur)(_target)
                if _new_val != _cur:
                    setattr(settings, _dst, _new_val)
                    _applied[_dst] = {"old": _cur, "new": _new_val}
            except Exception as _ae:
                _rejected[_dst] = str(_ae)

        # Category thresholds (spot and futures) — reset to baseline + optional profile patch.
        for _ct_attr, _ct_key in (
            ('category_thresholds', 'category_thresholds'),
            ('futures_category_thresholds', 'futures_category_thresholds'),
        ):
            _base_ct = deepcopy(_baseline.get(_ct_attr, {}) or {})
            _ct_patch = _raw.get(_ct_key)
            _patched = {}
            if _ct_patch is not None:
                if not isinstance(_ct_patch, dict):
                    _rejected[_ct_attr] = "must be dict"
                else:
                    for _cat, _fields in _ct_patch.items():
                        if not isinstance(_fields, dict):
                            _rejected[f"{_ct_attr}.{_cat}"] = "must be dict"
                            continue
                        _cat_old = dict(_base_ct.get(_cat, {}))
                        for _cf, _cv in _fields.items():
                            _cat_old[_cf] = float(_cv)
                            _patched[f"{_cat}.{_cf}"] = {"new": float(_cv)}
                        _base_ct[_cat] = _cat_old
            _current_ct = deepcopy(getattr(settings, _ct_attr, {}) or {})
            if _current_ct != _base_ct:
                setattr(settings, _ct_attr, _base_ct)
                _applied[_ct_attr] = {"reset_to_baseline": True, "patched": _patched}

        if not _applied:
            return {"applied": {}, "skipped": True, "reason": "no_applicable_fields"}

        # Persist scalar overrides as MERGE into existing kelly_config_override — never replace.
        # This prevents stale regime_profile values from wiping manual hot-config overrides (BUG-1 s64).
        _ov_rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='kelly_config_override'"
        )
        _existing_ov = {}
        if _ov_rows and _ov_rows[0].get('value'):
            _raw_ov = _ov_rows[0]['value']
            if isinstance(_raw_ov, str):
                try:
                    _raw_ov = _json.loads(_raw_ov)
                except Exception:
                    pass
            if isinstance(_raw_ov, dict):
                _existing_ov = _raw_ov
        # Only overlay the fields this profile explicitly defines — leave the rest untouched.
        _profile_patch = {
            _dst: {"new": getattr(settings, _dst, None)}
            for _src, _dst in _scalar_map.items()
            if _src in _raw and _dst not in _rejected
        }
        _existing_ov.update(_profile_patch)
        _merged_snapshot = _existing_ov
        _persist_writes = [
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) "
                "VALUES ('kelly_config_override', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (_json.dumps(_merged_snapshot),),
            )
        ]

        # Persist full current category thresholds for both SPOT and FUTURES.
        for _ct_attr, _ct_state_key in (
            ('category_thresholds', 'category_thresholds_override'),
            ('futures_category_thresholds', 'futures_category_thresholds_override'),
        ):
            _ct_payload = _json.dumps(getattr(settings, _ct_attr, {}) or {})
            _persist_writes.append((
                "INSERT INTO trading_runtime_state (key, value, updated_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                (_ct_state_key, _ct_payload, _ct_payload),
            ))
        db_client.execute_transaction(_persist_writes)

        # Audit trail
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__config__', 'system', 'regime_params_applied', %s, false)",
            (_json.dumps({
                "regime": regime,
                "profile": _active_profile,
                "applied": _applied,
                "rejected": _rejected,
                "state_key": _state_key,
            }),)
        )

        _last_applied_regime_mode = regime
        _last_applied_regime_profile = _profile_token
        print(f"[RegimeParams] Applied {len(_applied)} overrides for {regime}")
        return {"applied": _applied, "rejected": _rejected, "skipped": False, "profile": _active_profile, "raw_params": _raw}

    except Exception as _re_err:
        print(f"[RegimeParams] Error applying regime params for {regime}: {_re_err}")
        return {"applied": {}, "rejected": {"_error": str(_re_err)}, "skipped": True, "reason": "exception"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _get_kelly_context_multiplier(settings, regime: str = None, trade_type: str = 'spot') -> float:
    """Return a conservative profile/mode multiplier for Kelly sizing."""
    _profile = str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or 'B'
    _regime = str(regime or getattr(settings, 'current_regime_mode', '') or '').strip().upper()

    _profile_mult = {
        'A': 1.18,   # jul07: 1.12→1.18 (wolf-hunter s63: A entra con más fuerza)
        'B': 1.00,
        'C': 0.88,
    }.get(_profile, 1.00)
    _regime_mult = {
        'SPOT_BULL': 1.05,
        'RANGE_SCALP': 1.05,  # s76: 0.95→1.05 (sweep: 0 SL exits, CDR~50% → aggressive sizing)
        'PRESERVATION': 0.85,
        'FUTURES_SHORT': 1.00,
    }.get(_regime, 1.00)

    if str(trade_type).strip().lower() == 'futures':
        _profile_mult *= 1.02 if _profile == 'A' else (0.98 if _profile == 'C' else 1.0)
    # Wolf Hunter s63: Perfil A + SPOT_BULL tiene cap 1.35 (vs 1.20 general)
    # para permitir posiciones más grandes en el régimen más favorable
    _cap = 1.35 if (_profile == 'A' and _regime in ('SPOT_BULL', 'RANGE_SCALP')) else 1.20  # s76: RS aggressive
    return max(0.75, min(_cap, _profile_mult * _regime_mult))


def _get_kpi_context_multiplier(db_client) -> float:
    """Return a health-aware multiplier derived from KPI/health gates.

    Warning or inconclusive health should slow new risk deployment even if the
    strategy is still trading; critical/error is already handled elsewhere, but
    this keeps the Kelly path aligned with the system health state.
    """
    if db_client is None:
        return 1.0
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='health_results_latest'"
        )
        if not rows or rows[0].get('value') is None:
            return 1.0
        raw = rows[0].get('value')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return 1.0
        if not isinstance(raw, dict):
            return 1.0
        overall = str(raw.get('overall') or '').strip().lower()
        if overall in ('error', 'critical'):
            return 0.70
        if overall == 'warning':
            return 0.88
        if overall == 'inconclusive':
            return 0.95
    except Exception:
        return 1.0
    return 1.0


def _clamp_value(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _compute_portfolio_concentration_multiplier(
    settings,
    regime: str,
    trade_type: str,
    open_slots: int,
    effective_slots: int,
) -> Tuple[float, Dict[str, Any]]:
    """Return a bounded sizing multiplier from slot-capacity context.

    The multiplier is intentionally conservative and only scales sizing before
    existing Kelly caps; it never bypasses any hard risk limits.
    """
    _regime = str(regime or '').strip().upper()
    _trade_type = str(trade_type or 'spot').strip().lower()
    _eff_slots = max(1, int(effective_slots or 1))
    _open_slots = max(0, int(open_slots or 0))

    _diag: Dict[str, Any] = {
        'enabled': False,
        'trade_type': _trade_type,
        'regime': _regime,
        'open_slots': _open_slots,
        'effective_slots': _eff_slots,
    }

    if not bool(_get_param(settings, 'portfolio_concentration_enabled')):
        _diag['reason'] = 'feature_disabled'
        return 1.0, _diag

    _baseline_map = {
        'SPOT_BULL': int(_get_param(settings, 'portfolio_concentration_baseline_spot_bull')),
        'RANGE_SCALP': int(_get_param(settings, 'portfolio_concentration_baseline_range_scalp')),
        'PRESERVATION': int(_get_param(settings, 'portfolio_concentration_baseline_preservation')),
        'FUTURES_SHORT': int(_get_param(settings, 'portfolio_concentration_baseline_futures_short')),
    }
    _baseline_slots = max(1, int(_baseline_map.get(_regime, _eff_slots)))
    _alpha = max(0.0, float(_get_param(settings, 'portfolio_concentration_alpha')))
    _min_mult = max(0.10, float(_get_param(settings, 'portfolio_concentration_min_mult')))
    _max_mult = max(_min_mult, float(_get_param(settings, 'portfolio_concentration_max_mult')))
    _pressure_start = max(0.0, min(1.0, float(_get_param(settings, 'portfolio_concentration_pressure_start'))))
    _pressure_boost = max(0.0, float(_get_param(settings, 'portfolio_concentration_pressure_boost')))

    _capacity_ratio = _baseline_slots / float(_eff_slots)
    _slot_pressure = min(2.0, _open_slots / float(_eff_slots))
    # Reward unused capacity directly so fewer open positions can receive a larger ticket.
    # Headroom is deliberately a stronger signal than pressure because the goal is faster capital deployment.
    _headroom = max(0.0, 1.0 - _slot_pressure)
    _underuse_mult = 1.0 + _headroom * _pressure_boost
    _pressure_mult = 1.0 + max(0.0, _slot_pressure - _pressure_start) * _pressure_boost
    _raw_mult = (_capacity_ratio ** _alpha) * _underuse_mult * _pressure_mult
    _mult = _clamp_value(_raw_mult, _min_mult, _max_mult)

    _diag.update({
        'enabled': True,
        'baseline_slots': _baseline_slots,
        'capacity_ratio': round(_capacity_ratio, 6),
        'slot_pressure': round(_slot_pressure, 6),
        'headroom': round(_headroom, 6),
        'underuse_mult': round(_underuse_mult, 6),
        'pressure_mult': round(_pressure_mult, 6),
        'alpha': round(_alpha, 6),
        'min_mult': round(_min_mult, 6),
        'max_mult': round(_max_mult, 6),
        'raw_mult': round(_raw_mult, 6),
        'multiplier': round(_mult, 6),
    })
    return _mult, _diag


def _apply_mode_profile_harmony(settings, regime: str, profile: str, profile_params: Optional[Dict] = None) -> Dict[str, Any]:
    """Apply an idempotent mode/profile overlay on top of the base regime params.

    s70: profile_params passed from _apply_regime_params payload so that the signature
    includes a hash of the active profile content — ensures re-application when the profile
    changes in DB even if regime/label stay the same, preventing stale multiplier skips.
    """
    _regime = str(regime or '').strip().upper()
    _profile = str(profile or '').strip().upper() or 'B'
    if settings is None:
        return {"applied": {}, "regime": _regime, "profile": _profile}

    # s70: include profile payload hash so signature invalidates when profile content changes
    _payload_hash = hash(str(sorted((profile_params or {}).items()))) if profile_params else 0
    _signature = f"{_regime}:{_profile}:{_payload_hash}"
    _last_signature = str(getattr(settings, '_mode_profile_harmony_signature', '') or '')
    if _last_signature == _signature:
        return {"applied": {}, "regime": _regime, "profile": _profile, "signature": _signature, "skipped": True, "reason": "already_applied"}

    _regime_mult = {
        'SPOT_BULL': {
            'trailing_stop_activation_pct': 1.08,
            'trailing_stop_distance_pct': 1.10,
            'futures_breakeven_activation_pct': 0.95,
            'futures_trailing_activation_pct': 1.00,
            'futures_trailing_distance_pct': 1.00,
            'futures_trail_floor_pct': 0.98,
            'futures_trail_ceiling_pct': 0.98,
            'futures_trail_atr_multiplier_low': 0.98,
            'futures_trail_atr_multiplier_mid': 0.98,
            'futures_trail_atr_multiplier_high': 0.98,
            'futures_trail_atr_multiplier_deep': 0.98,
            'max_positions_spot_bull': 1.08,
            'min_hold_minutes_spot_bull': 1.08,
            'max_rebalance_per_cycle': 0.95,
            'futures_long_sl_pct': 1.00,
            'futures_long_tp_pct': 1.02,
            'sso_target_daily_usd': 1.05,
            # s70: sso_stagnant_min_hours removed — now governed by sso_stagnant_min_hours_spot_bull (per-regime field)
            'sso_slow_velocity_threshold': 0.95,
            'sso_reentry_min_strength_diff': 1.03,
            'spot_bounce_rescue_target_pnl': 1.00,
            'futures_bounce_rescue_target_pnl': 1.00,
        },
        'RANGE_SCALP': {
            'trailing_stop_activation_pct': 0.95,
            'trailing_stop_distance_pct': 0.90,
            'futures_breakeven_activation_pct': 0.90,
            'futures_trailing_activation_pct': 0.92,
            'futures_trailing_distance_pct': 0.90,
            'futures_trail_floor_pct': 0.95,
            'futures_trail_ceiling_pct': 0.92,
            'futures_trail_atr_multiplier_low': 0.94,
            'futures_trail_atr_multiplier_mid': 0.94,
            'futures_trail_atr_multiplier_high': 0.95,
            'futures_trail_atr_multiplier_deep': 0.96,
            'max_positions_range_scalp': 0.92,
            'min_hold_minutes_range_scalp': 0.88,
            'max_rebalance_per_cycle': 1.10,
            'futures_long_sl_pct': 0.95,
            'futures_long_tp_pct': 0.92,
            'sso_target_daily_usd': 0.90,
            # s70: sso_stagnant_min_hours removed — now governed by sso_stagnant_min_hours_range_scalp (per-regime field)
            'sso_slow_velocity_threshold': 1.06,
            'sso_reentry_min_strength_diff': 0.98,
            'range_scalp_sl_pct': 0.95,
            'range_scalp_tp_pct': 0.92,
        },
        'PRESERVATION': {
            'trailing_stop_activation_pct': 0.92,
            'trailing_stop_distance_pct': 0.88,
            'futures_breakeven_activation_pct': 0.88,
            'futures_trailing_activation_pct': 0.88,
            'futures_trailing_distance_pct': 0.86,
            'futures_trail_floor_pct': 0.92,
            'futures_trail_ceiling_pct': 0.88,
            'futures_trail_atr_multiplier_low': 0.90,
            'futures_trail_atr_multiplier_mid': 0.90,
            'futures_trail_atr_multiplier_high': 0.92,
            'futures_trail_atr_multiplier_deep': 0.94,
            'max_positions_preservation': 0.88,
            'min_hold_minutes': 1.12,
            'max_rebalance_per_cycle': 0.85,
            'futures_long_sl_pct': 0.94,
            'futures_long_tp_pct': 0.96,
            'sso_target_daily_usd': 0.85,
            # s70: sso_stagnant_min_hours removed — now governed by sso_stagnant_min_hours_preservation (per-regime field)
            'sso_slow_velocity_threshold': 1.10,
            'sso_reentry_min_strength_diff': 0.95,
            'preservation_sl_pct': 0.94,
            'preservation_tp_pct': 0.96,
        },
        'FUTURES_SHORT': {
            'trailing_stop_activation_pct': 1.00,
            'trailing_stop_distance_pct': 1.00,
            'futures_breakeven_activation_pct': 0.85,
            'futures_trailing_activation_pct': 0.88,
            'futures_trailing_distance_pct': 0.92,
            'futures_trail_floor_pct': 0.92,
            'futures_trail_ceiling_pct': 0.88,
            'futures_trail_atr_multiplier_low': 0.88,
            'futures_trail_atr_multiplier_mid': 0.90,
            'futures_trail_atr_multiplier_high': 0.92,
            'futures_trail_atr_multiplier_deep': 0.94,
            'max_positions_futures_short': 1.05,
            'futures_max_new_per_cycle': 1.00,
            'futures_long_sl_pct': 0.98,
            'futures_long_tp_pct': 1.00,
            'sso_target_daily_usd': 0.95,
            # s70: sso_stagnant_min_hours removed — now governed by sso_stagnant_min_hours_futures_short (per-regime field)
            'sso_slow_velocity_threshold': 1.02,
            'sso_reentry_min_strength_diff': 1.00,
        },
    }.get(_regime, {})

    _profile_mult = {
        'A': {
            'trailing_stop_activation_pct': 1.06,
            'trailing_stop_distance_pct': 1.08,
            'futures_breakeven_activation_pct': 0.94,
            'futures_trailing_activation_pct': 0.95,
            'futures_trailing_distance_pct': 0.97,
            'futures_trail_floor_pct': 0.98,
            'futures_trail_ceiling_pct': 0.96,
            'futures_trail_atr_multiplier_low': 0.96,
            'futures_trail_atr_multiplier_mid': 0.96,
            'futures_trail_atr_multiplier_high': 0.97,
            'futures_trail_atr_multiplier_deep': 0.98,
            'max_positions_spot_bull': 1.12,
            'max_total_exposure_pct': 1.05,
            'kelly_conviction_max_pct': 1.10,
            'overflow_max_extra': 1.35,
            'prediction_add_min_strength': 0.90,
            'prediction_add_min_ev_ratio': 0.90,
            'max_positions_preservation': 1.00,
            'max_positions_futures_short': 1.10,
            'futures_max_new_per_cycle': 1.10,
            'min_hold_minutes': 0.92,
            'min_hold_minutes_spot_bull': 0.94,
            'min_hold_minutes_range_scalp': 0.90,
            'sso_target_daily_usd': 1.10,
            # s71 BUG-2: removed 'sso_stagnant_min_hours' (global NEVER modified by harmony).
            # Profile A multipliers applied to per-regime fields instead:
            'sso_stagnant_min_hours_spot_bull': 1.08,       # Profile A: more patience in SPOT_BULL runner
            'sso_stagnant_min_hours_range_scalp': 0.93,    # Profile A: faster rotation in lateral scalp
            'sso_stagnant_min_hours_preservation': 1.05,   # Profile A: slightly more patience in PRESERVATION
            'sso_stagnant_min_hours_futures_short': 1.05,  # Profile A: patience for SHORT runners
            'sso_decay_min_hours_spot_bull': 1.08,
            'sso_decay_min_hours_range_scalp': 0.92,
            'sso_slow_min_hours_spot_bull': 1.08,
            'sso_slow_min_hours_range_scalp': 0.92,
            'sso_slow_velocity_threshold': 0.92,
            'sso_reentry_min_strength_diff': 1.05,
            'spot_bounce_rescue_target_pnl': 1.05,
            'futures_bounce_rescue_target_pnl': 1.05,
            'futures_long_sl_pct': 1.04,
            'futures_long_tp_pct': 1.05,
        },
        'B': {},
        'C': {
            'trailing_stop_activation_pct': 0.94,
            'trailing_stop_distance_pct': 0.90,
            'futures_breakeven_activation_pct': 0.90,
            'futures_trailing_activation_pct': 0.88,
            'futures_trailing_distance_pct': 0.90,
            'futures_trail_floor_pct': 0.94,
            'futures_trail_ceiling_pct': 0.90,
            'futures_trail_atr_multiplier_low': 0.92,
            'futures_trail_atr_multiplier_mid': 0.92,
            'futures_trail_atr_multiplier_high': 0.94,
            'futures_trail_atr_multiplier_deep': 0.95,
            'max_positions_spot_bull': 0.88,
            'max_positions_range_scalp': 0.90,
            'max_positions_preservation': 0.82,
            'max_positions_futures_short': 0.88,
            'futures_max_new_per_cycle': 0.90,
            'min_hold_minutes': 1.10,
            'min_hold_minutes_spot_bull': 1.08,
            'min_hold_minutes_range_scalp': 1.05,
            'sso_target_daily_usd': 0.90,
            # s71 BUG-2: removed 'sso_stagnant_min_hours' (global NEVER modified by harmony).
            # Profile C multipliers applied to per-regime fields instead:
            'sso_stagnant_min_hours_spot_bull': 0.92,      # Profile C: less patience in SPOT_BULL
            'sso_stagnant_min_hours_range_scalp': 1.05,   # Profile C: more patient in lateral (defensive)
            'sso_stagnant_min_hours_preservation': 1.08,  # Profile C: more patience in PRESERVATION
            'sso_stagnant_min_hours_futures_short': 0.95, # Profile C: slightly tighter in FUTURES_SHORT
            'sso_decay_min_hours_range_scalp': 1.05,
            'sso_slow_min_hours_range_scalp': 1.05,
            'sso_slow_velocity_threshold': 1.08,
            'sso_reentry_min_strength_diff': 0.90,
            'spot_bounce_rescue_target_pnl': 0.92,
            'futures_bounce_rescue_target_pnl': 0.92,
            'futures_long_sl_pct': 0.96,
            'futures_long_tp_pct': 0.95,
        },
    }.get(_profile, {})

    _applied: Dict[str, Dict[str, float]] = {}

    def _apply_field(field: str, minimum: float = None, maximum: float = None, base_key: Optional[str] = None) -> None:
        cur = getattr(settings, field, None)
        if cur is None:
            return
        mult = 1.0
        if field in _regime_mult:
            mult *= _regime_mult[field]
        if field in _profile_mult:
            mult *= _profile_mult[field]
        if mult == 1.0:
            return
        new_val = float(cur) * mult
        if minimum is not None and maximum is not None:
            new_val = _clamp_value(new_val, minimum, maximum)
        elif minimum is not None:
            new_val = max(minimum, new_val)
        elif maximum is not None:
            new_val = min(maximum, new_val)
        setattr(settings, field, type(cur)(new_val))
        _applied[field] = {"old": float(cur), "new": float(getattr(settings, field, None)), "mult": round(mult, 4)}

    for _f, _bounds in (
        ('trailing_stop_activation_pct', (0.5, 6.0)),
        ('trailing_stop_distance_pct', (0.4, 4.0)),
        ('futures_breakeven_activation_pct', (0.2, 4.0)),
        ('futures_trailing_activation_pct', (0.5, 6.0)),
        ('futures_trailing_distance_pct', (0.4, 4.0)),
        ('futures_trail_floor_pct', (0.2, 1.2)),
        ('futures_trail_ceiling_pct', (0.6, 2.0)),
        ('futures_trail_atr_multiplier_low', (0.4, 2.5)),
        ('futures_trail_atr_multiplier_mid', (0.4, 2.5)),
        ('futures_trail_atr_multiplier_high', (0.4, 2.5)),
        ('futures_trail_atr_multiplier_deep', (0.4, 2.5)),
        ('sso_target_daily_usd', (10.0, 250.0)),
        # s70: sso_stagnant_min_hours (global) removed — globals are NEVER modified by harmony.
        # Per-regime SSO fields below receive profile-only multipliers (A/C).
        ('sso_slow_velocity_threshold', (0.0003, 0.01)),
        ('sso_reentry_min_strength_diff', (0.05, 1.0)),
        ('spot_bounce_rescue_target_pnl', (-2.0, -0.05)),
        ('futures_bounce_rescue_target_pnl', (-2.0, -0.05)),
        ('range_scalp_sl_pct', (0.5, 3.0)),
        ('range_scalp_tp_pct', (0.8, 4.0)),
        ('preservation_sl_pct', (0.8, 4.0)),
        ('preservation_tp_pct', (1.0, 6.0)),
        ('max_positions_spot_bull', (5, 40)),
        ('max_positions_range_scalp', (4, 20)),  # s71 BUG-8: floor raised from 3→4
        ('max_positions_preservation', (1, 12)),
        ('max_positions_futures_short', (4, 24)),
        ('overflow_max_extra', (1, 8)),
        ('futures_max_new_per_cycle', (1, 10)),
        ('min_hold_minutes', (30, 360)),
        ('min_hold_minutes_spot_bull', (30, 360)),
        ('min_hold_minutes_range_scalp', (15, 180)),
        ('futures_long_sl_pct', (1.0, 5.0)),
        ('futures_long_tp_pct', (2.0, 10.0)),
        ('max_rebalance_per_cycle', (1, 5)),
        ('max_total_exposure_pct', (0.20, 0.95)),
        ('kelly_conviction_max_pct', (0.05, 0.35)),  # s76: 0.25→0.35 (aggressive RS profiles use 0.30)
        ('prediction_add_min_strength', (0.30, 0.90)),
        ('prediction_add_min_ev_ratio', (0.80, 2.50)),
        # s70: per-regime SSO timing — profile multiplier only (regime value already baked into field default)
        ('sso_stagnant_min_hours_spot_bull',     (0.5, 12.0)),
        ('sso_stagnant_min_hours_range_scalp',   (0.3, 6.0)),
        ('sso_stagnant_min_hours_preservation',  (0.5, 12.0)),
        ('sso_stagnant_min_hours_futures_short', (0.5, 12.0)),
        ('sso_slow_min_hours_spot_bull',         (0.5, 8.0)),
        ('sso_slow_min_hours_range_scalp',       (0.2, 4.0)),
        ('sso_slow_min_hours_preservation',      (0.5, 8.0)),
        ('sso_slow_min_hours_futures_short',     (0.3, 6.0)),
        ('sso_decay_min_hours_spot_bull',        (0.5, 8.0)),
        ('sso_decay_min_hours_range_scalp',      (0.3, 5.0)),
        ('sso_decay_min_hours_preservation',     (0.5, 8.0)),
        ('sso_decay_min_hours_futures_short',    (0.5, 8.0)),
    ):
        _apply_field(_f, minimum=_bounds[0], maximum=_bounds[1])

    # Guarantee breakeven always activates before futures trailing.
    try:
        _be = float(getattr(settings, 'futures_breakeven_activation_pct', 1.0) or 1.0)
        _tr = float(getattr(settings, 'futures_trailing_activation_pct', 2.0) or 2.0)
        if _be >= _tr:
            _safe_be = _clamp_value(_tr - 0.1, 0.2, max(0.2, _tr - 0.01))
            setattr(settings, 'futures_breakeven_activation_pct', type(getattr(settings, 'futures_breakeven_activation_pct', 1.0))(_safe_be))
            _applied['futures_breakeven_activation_pct_guardrail'] = {
                "old": _be,
                "new": float(getattr(settings, 'futures_breakeven_activation_pct', _safe_be)),
                "reason": "enforce_be_before_trailing",
            }
    except Exception:
        pass

    for _ct_attr in ('category_thresholds', 'futures_category_thresholds'):
        _ct = getattr(settings, _ct_attr, None)
        if not isinstance(_ct, dict):
            continue
        _ct_updates = {}
        for _cat, _vals in _ct.items():
            if not isinstance(_vals, dict):
                continue
            _new_vals = dict(_vals)
            if 'stop_loss_pct' in _new_vals:
                _new_vals['stop_loss_pct'] = float(_new_vals['stop_loss_pct'])
            if 'take_profit_pct' in _new_vals:
                _new_vals['take_profit_pct'] = float(_new_vals['take_profit_pct'])
            if 'trailing_activation_pct' in _new_vals:
                _new_vals['trailing_activation_pct'] = float(_new_vals['trailing_activation_pct'])
            if 'trailing_distance_pct' in _new_vals:
                _new_vals['trailing_distance_pct'] = float(_new_vals['trailing_distance_pct'])
            _mult = 1.0
            if _regime == 'SPOT_BULL':
                _mult *= 1.04
            elif _regime == 'RANGE_SCALP':
                _mult *= 0.95
            elif _regime == 'PRESERVATION':
                _mult *= 0.92
            elif _regime == 'FUTURES_SHORT':
                _mult *= 1.02
            if _profile == 'A':
                _mult *= 1.06
            elif _profile == 'C':
                _mult *= 0.92
            if 'stop_loss_pct' in _new_vals:
                _new_vals['stop_loss_pct'] = round(_clamp_value(float(_new_vals['stop_loss_pct']) * _mult, 0.5, 12.0), 4)
            if 'take_profit_pct' in _new_vals:
                _new_vals['take_profit_pct'] = round(_clamp_value(float(_new_vals['take_profit_pct']) * (1.0 + ((_mult - 1.0) * 0.6)), 0.8, 20.0), 4)
            if 'trailing_activation_pct' in _new_vals:
                _new_vals['trailing_activation_pct'] = round(_clamp_value(float(_new_vals['trailing_activation_pct']) * (_mult if _mult > 1.0 else 1.0), 0.5, 20.0), 4)
            if 'trailing_distance_pct' in _new_vals:
                _new_vals['trailing_distance_pct'] = round(_clamp_value(float(_new_vals['trailing_distance_pct']) * (_mult if _mult > 1.0 else 1.0), 0.4, 20.0), 4)
            _ct_updates[_cat] = _new_vals
        if _ct_updates:
            setattr(settings, _ct_attr, _ct_updates)
            _applied[_ct_attr] = {"categories": list(_ct_updates.keys())}

    setattr(settings, '_mode_profile_harmony_signature', _signature)

    return {
        "applied": _applied,
        "regime": _regime,
        "profile": _profile,
        "signature": _signature,
        "skipped": False,
    }


def _persist_transition_state(db_client, payload: Dict[str, Any]) -> None:
    if db_client is None:
        return
    try:
        _raw = json.dumps(payload)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("regime_transition_state", _raw, _raw),
        )
    except Exception as _pt_err:
        print(f"[Transition] state persist failed: {_pt_err}")


def _persist_runtime_selection_snapshot(db_client, payload: Dict[str, Any]) -> None:
    """Persist the effective mode/profile/params selection atomically per cycle."""
    if db_client is None:
        return
    try:
        _raw = json.dumps(payload)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("effective_runtime_selection_snapshot", _raw, _raw),
        )
    except Exception as _prs_err:
        print(f"[RuntimeSelection] snapshot persist failed: {_prs_err}")


def _persist_mode_profile_harmony_snapshot(db_client, payload: Dict[str, Any]) -> None:
    """Persist the effective Kelly/KPI/SSO harmony state for post-run analysis."""
    if db_client is None:
        return
    try:
        _raw = json.dumps(payload)
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            ("mode_profile_harmony_snapshot", _raw, _raw),
        )
    except Exception as _harmony_err:
        print(f"[Harmony] snapshot persist failed: {_harmony_err}")


def _load_invalid_symbol_state(db_client) -> None:
    global _INVALID_SYMBOLS_STATE_LOADED, _INVALID_SYMBOLS, _INVALID_FUTURES_SYMBOLS
    if _INVALID_SYMBOLS_STATE_LOADED or db_client is None:
        return
    try:
        _rows = db_client.execute_query(
            "SELECT key, value FROM trading_runtime_state WHERE key IN ('invalid_spot_symbols', 'invalid_futures_symbols')"
        )
        for _row in _rows or []:
            _key = _row.get('key')
            _value = _row.get('value') or []
            if isinstance(_value, str):
                try:
                    _value = json.loads(_value)
                except Exception:
                    _value = []
            if not isinstance(_value, list):
                continue
            if _key == 'invalid_spot_symbols':
                _INVALID_SYMBOLS.update(str(v).strip().upper() for v in _value if str(v).strip())
            elif _key == 'invalid_futures_symbols':
                _INVALID_FUTURES_SYMBOLS.update(str(v).strip().upper() for v in _value if str(v).strip())
        _INVALID_SYMBOLS_STATE_LOADED = True
    except Exception as _is_err:
        print(f"[InvalidSymbols] load failed: {_is_err}")


def _persist_invalid_symbol_state(db_client) -> None:
    if db_client is None:
        return
    try:
        _spot = json.dumps(sorted(_INVALID_SYMBOLS))
        _fut = json.dumps(sorted(_INVALID_FUTURES_SYMBOLS))
        db_client.execute_transaction([
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ('invalid_spot_symbols', _spot, _spot),
            ),
            (
                "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ('invalid_futures_symbols', _fut, _fut),
            ),
        ])
    except Exception as _pis_err:
        print(f"[InvalidSymbols] persist failed: {_pis_err}")


def _build_transition_id(from_mode: Optional[str], to_mode: Optional[str], profile_label: Optional[str] = None) -> str:
    _ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _from = str(from_mode or "UNKNOWN").strip().upper() or "UNKNOWN"
    _to = str(to_mode or "UNKNOWN").strip().upper() or "UNKNOWN"
    _profile = str(profile_label or "").strip().upper()
    return f"{_from}__{_to}__{_profile or 'NA'}__{_ts}"


def _get_transition_policy(from_mode: Optional[str], to_mode: Optional[str]) -> Dict[str, Any]:
    """Return operational policy for inherited positions across regime transitions."""
    _from = str(from_mode or "").strip().upper()
    _to = str(to_mode or "").strip().upper()
    _default = {
        "spot": "keep",
        "futures_short": "keep",
        "deadline_cycles": int(getattr(settings, 'min_cycles_before_close_shorts', 3)) if 'settings' in globals() else 3,
        "protect_profit": True,
    }
    _matrix = {
        ("FUTURES_SHORT", "RANGE_SCALP"): {"spot": "keep", "futures_short": "deadline_close", "deadline_cycles": 2, "protect_profit": True},
        ("FUTURES_SHORT", "SPOT_BULL"): {"spot": "keep", "futures_short": "deadline_close", "deadline_cycles": 1, "protect_profit": False},
        ("PRESERVATION", "SPOT_BULL"): {"spot": "keep", "futures_short": "deadline_close", "deadline_cycles": 1, "protect_profit": False},
        ("PRESERVATION", "RANGE_SCALP"): {"spot": "keep", "futures_short": "deadline_close", "deadline_cycles": 2, "protect_profit": True},
        ("SPOT_BULL", "FUTURES_SHORT"): {"spot": "trim_profit_only", "futures_short": "keep", "deadline_cycles": 0, "protect_profit": True},
        ("RANGE_SCALP", "FUTURES_SHORT"): {"spot": "trim_profit_only", "futures_short": "keep", "deadline_cycles": 0, "protect_profit": True},
    }
    return dict(_default, **_matrix.get((_from, _to), {}))


def _reevaluate_open_spot_positions(position_manager, settings, regime_mode: str, profile_label: str, switch_id: Optional[str] = None) -> int:
    """Refresh SPOT open-position metadata and thresholds after a mode/profile change."""
    if not position_manager:
        return 0
    _token_categories = getattr(settings, 'token_categories', {}) or {}
    _cat_thresholds = getattr(settings, 'category_thresholds', {}) or {}
    _updated = 0
    for _symbol, _positions in position_manager.get_all_positions().items():
        if not _positions:
            continue
        _cat_beh = _token_categories.get(_symbol, {}).get('market_behavior', 'MIDCAP_ALT')
        _cat_th = _cat_thresholds.get(_cat_beh, {})
        _sl_pct = float(_cat_th.get('stop_loss_pct', _get_param(settings, 'stop_loss_pct')) or 0.0) / 100.0
        _tp_pct = float(_cat_th.get('take_profit_pct', _get_param(settings, 'take_profit_pct')) or 0.0) / 100.0
        for _pos in _positions:
            try:
                _new_sl = float(_pos.entry_price) * (1 - _sl_pct)
                _new_tp = float(_pos.entry_price) * (1 + _tp_pct)
                position_manager.update_position_sl(_pos.position_id, _new_sl)
                position_manager.update_position_tp(_pos.position_id, _new_tp)
                _meta = dict(_pos.metadata or {}) if isinstance(_pos.metadata, dict) else {}
                _meta['current_regime_mode'] = regime_mode
                _meta['current_profile_label'] = profile_label
                _meta['last_transition_switch_id'] = switch_id
                _meta['_meta_version'] = int(_meta.get('_meta_version', 0) or 0) + 1
                _meta['_meta_updated_at'] = datetime.now(timezone.utc).isoformat()
                position_manager.update_position_metadata(_pos.position_id, _meta)
                _updated += 1
            except Exception as _re_pos_err:
                print(f"[Transition] spot re-eval failed for {_symbol}: {_re_pos_err}")
    return _updated


def _build_pre_switch_state(mode_selector_result: Dict[str, Any], regime_obj: Optional[MarketRegime]) -> Dict[str, Any]:
    """Build a reversible pre-switch state from selector/regime signals.

    We only activate when a switch is blocked by confirmation/hysteresis but confidence is already elevated.
    """
    _state = {
        "active": False,
        "from_mode": None,
        "to_mode": None,
        "reason": None,
        "confidence": 0.0,
        "block_new_shorts": False,
    }
    if not mode_selector_result:
        return _state

    _blocked = bool(mode_selector_result.get("blocked"))
    _detected = str(mode_selector_result.get("detected_mode") or "").strip().upper()
    _selected = str(mode_selector_result.get("selected_mode") or "").strip().upper()
    _reason = str(mode_selector_result.get("reason") or "")
    _conf = _safe_float(getattr(regime_obj, "confidence", 0.0), 0.0)

    if _blocked and _detected and _selected and _detected != _selected and _conf >= 0.60:
        _state.update({
            "active": True,
            "from_mode": _selected,
            "to_mode": _detected,
            "reason": _reason,
            "confidence": _conf,
            # If we're currently short-friendly but detection points to a non-short mode, stop adding new shorts.
            "block_new_shorts": _selected in ("FUTURES_SHORT", "PRESERVATION") and _detected not in ("FUTURES_SHORT", "PRESERVATION"),
        })
    return _state


def _parse_json_like(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _load_regime_selector_config(db_client) -> Dict[str, Any]:
    cfg = dict(_SELECTOR_CONFIG_DEFAULTS)
    if db_client is None:
        return cfg
    try:
        rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key='regime_profile_selector_config'"
        )
        if rows and rows[0].get('value') is not None:
            raw = _parse_json_like(rows[0]['value'])
            if isinstance(raw, dict):
                cfg.update(raw)
    except Exception as _cfg_err:
        print(f"[RegimeSelector] config load failed: {_cfg_err}")

    cfg['smart_total_enabled'] = bool(cfg.get('smart_total_enabled', True))
    cfg['mode_selector_enabled'] = bool(cfg.get('mode_selector_enabled', True))
    cfg['mode_require_confirmed'] = bool(cfg.get('mode_require_confirmed', True))
    cfg['mode_min_hold_cycles'] = max(0, int(_safe_float(cfg.get('mode_min_hold_cycles', 2), 2)))
    cfg['mode_min_confidence_delta'] = max(0.0, _safe_float(cfg.get('mode_min_confidence_delta', 0.03), 0.03))
    cfg['mode_audit_every_cycle'] = bool(cfg.get('mode_audit_every_cycle', True))

    cfg['profile_selector_enabled'] = bool(cfg.get('profile_selector_enabled', cfg.get('enabled', True)))
    cfg['audit_every_cycle'] = bool(cfg.get('audit_every_cycle', True))
    cfg['min_hold_cycles'] = max(0, int(_safe_float(cfg.get('min_hold_cycles', 3), 3)))
    cfg['min_score_delta'] = max(0.0, _safe_float(cfg.get('min_score_delta', 0.05), 0.05))
    cfg['profile_min_hold_cycles'] = max(
        0,
        int(_safe_float(cfg.get('profile_min_hold_cycles', cfg.get('min_hold_cycles', 3)), 3))
    )
    cfg['profile_min_score_delta'] = max(
        0.0,
        _safe_float(cfg.get('profile_min_score_delta', cfg.get('min_score_delta', 0.05)), 0.05)
    )
    _pr = cfg.get('profile_rank')
    if not isinstance(_pr, dict):
        _pr = {}
    cfg['profile_rank'] = {
        str(k).strip().upper(): _safe_float(v, 0.0)
        for k, v in _pr.items()
        if str(k).strip()
    }
    if not cfg['profile_rank']:
        cfg['profile_rank'] = dict(_SELECTOR_CONFIG_DEFAULTS['profile_rank'])
    return cfg


def _run_smart_total_mode_selector(settings, regime_obj: Optional[MarketRegime], db_client=None) -> Dict[str, Any]:
    """
    SMART Total mode selector with dedicated hysteresis/cooldown state.
    Persists state independently from profile selector.
    """
    if db_client is None:
        return {"ok": False, "skipped": True, "reason": "no_db_client"}
    if regime_obj is None:
        return {"ok": False, "skipped": True, "reason": "no_regime"}

    _cfg = _load_regime_selector_config(db_client)
    if not bool(_cfg.get('smart_total_enabled', True)):
        return {"ok": True, "skipped": True, "reason": "smart_total_disabled"}
    if not bool(_cfg.get('mode_selector_enabled', True)):
        return {"ok": True, "skipped": True, "reason": "mode_selector_disabled"}

    _detected_mode = str(getattr(regime_obj, 'mode', '') or '').strip().upper()
    if not _detected_mode:
        return {"ok": False, "skipped": True, "reason": "invalid_detected_mode"}
    _detected_confirmed = bool(getattr(regime_obj, 'confirmed', False))
    _detected_confidence = _safe_float(getattr(regime_obj, 'confidence', 0.0), 0.0)

    _active_key = 'smart_total_active_mode'
    _state_key = 'smart_total_mode_selector_state'

    _active_rows = db_client.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (_active_key,)
    )
    _active_mode = None
    if _active_rows and _active_rows[0].get('value') is not None:
        _av = _parse_json_like(_active_rows[0]['value'])
        if isinstance(_av, dict):
            _active_mode = str(_av.get('mode') or _av.get('selected_mode') or '').strip().upper() or None
        elif isinstance(_av, str):
            _active_mode = _av.strip().upper() or None

    _state_rows = db_client.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (_state_key,)
    )
    _state = {}
    if _state_rows and _state_rows[0].get('value') is not None:
        _sv = _parse_json_like(_state_rows[0]['value'])
        if isinstance(_sv, dict):
            _state = _sv

    _current_mode = _active_mode or str(_state.get('last_selected') or '').strip().upper() or None
    _hold_cycles = int(_safe_float(_state.get('cycles_since_switch', 999), 999))
    _current_score = _safe_float(_state.get('last_selected_score', 0.0), 0.0)
    _min_hold = int(_cfg.get('mode_min_hold_cycles', 2))
    _min_delta = _safe_float(_cfg.get('mode_min_confidence_delta', 0.03), 0.03)

    _selected_mode = _current_mode or _detected_mode
    _switch = False
    _blocked = False
    _reason = 'keep_current'

    if _current_mode is None:
        _selected_mode = _detected_mode
        _switch = True
        _reason = 'no_active_mode'
    elif _detected_mode == _current_mode:
        _selected_mode = _current_mode
        _reason = 'detected_mode_already_active'
    else:
        if bool(_cfg.get('mode_require_confirmed', True)) and not _detected_confirmed:
            _selected_mode = _current_mode
            _blocked = True
            _reason = 'blocked_unconfirmed_detected_mode'
        elif _hold_cycles < _min_hold:
            _selected_mode = _current_mode
            _blocked = True
            _reason = f'blocked_hold_cycles({_hold_cycles}<{_min_hold})'
        elif (_detected_confidence - _current_score) < _min_delta:
            _selected_mode = _current_mode
            _blocked = True
            _reason = (
                f'blocked_confidence_delta('
                f'{(_detected_confidence - _current_score):.4f}<{_min_delta:.4f})'
            )
        else:
            _selected_mode = _detected_mode
            _switch = True
            _reason = (
                f'switch_mode_confidence_delta('
                f'{(_detected_confidence - _current_score):.4f}>={_min_delta:.4f})'
            )

    _now_iso = datetime.now(timezone.utc).isoformat()
    _selected_score = _detected_confidence if _selected_mode == _detected_mode else _current_score
    _new_cycles = 0 if _switch else (_hold_cycles + 1)
    _state_payload = {
        'last_selected': _selected_mode,
        'last_selected_score': _selected_score,
        'detected_mode': _detected_mode,
        'detected_confirmed': _detected_confirmed,
        'detected_confidence': _detected_confidence,
        'active_before': _current_mode,
        'switched': _switch,
        'blocked': _blocked,
        'decision_reason': _reason,
        'cycles_since_switch': _new_cycles,
        'last_decision_at': _now_iso,
        'selector_config': {
            'mode_min_hold_cycles': _min_hold,
            'mode_min_confidence_delta': _min_delta,
            'mode_require_confirmed': bool(_cfg.get('mode_require_confirmed', True)),
            'mode_audit_every_cycle': bool(_cfg.get('mode_audit_every_cycle', True)),
        },
    }

    _audit_detail = {
        'active_before': _current_mode,
        'selected_mode': _selected_mode,
        'detected_mode': _detected_mode,
        'detected_confirmed': _detected_confirmed,
        'detected_confidence': _detected_confidence,
        'current_score': _current_score,
        'delta_vs_current': (_detected_confidence - _current_score),
        'switched': _switch,
        'blocked': _blocked,
        'reason': _reason,
        'state': _state_payload,
    }

    _writes = []
    if _switch or _active_mode is None:
        _active_payload = {
            'mode': _selected_mode,
            'reason': 'smart_total_mode_selector',
            'updated_at': _now_iso,
            'detected_mode': _detected_mode,
            'detected_confirmed': _detected_confirmed,
            'detected_confidence': _detected_confidence,
            'decision_reason': _reason,
        }
        _active_json = json.dumps(_active_payload)
        _writes.append((
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (_active_key, _active_json, _active_json),
        ))

    _state_json = json.dumps(_state_payload)
    _writes.append((
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (_state_key, _state_json, _state_json),
    ))
    if bool(_cfg.get('mode_audit_every_cycle', True)) or _switch:
        _writes.append((
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__config__', 'system', 'smart_total_mode_selector_decision', %s, false)",
            (json.dumps(_audit_detail),),
        ))

    if len(_writes) == 1:
        db_client.execute_update(_writes[0][0], _writes[0][1])
    else:
        db_client.execute_transaction(_writes)

    if _switch:
        try:
            from pair_screener import invalidate_cache as _invalidate_screener_cache
            _invalidate_screener_cache()
        except Exception:
            pass

    return {
        'ok': True,
        'skipped': False,
        'active_before': _current_mode,
        'selected_mode': _selected_mode,
        'detected_mode': _detected_mode,
        'switched': _switch,
        'blocked': _blocked,
        'reason': _reason,
        'delta_vs_current': (_detected_confidence - _current_score),
    }


def _score_profile_candidate(label: str, payload: Dict[str, Any], regime_obj: Optional[MarketRegime], cfg: Dict[str, Any]) -> Tuple[float, Dict[str, float]]:
    _label = str(label or '').strip().upper()
    _meta = payload.get('meta', {}) if isinstance(payload, dict) else {}
    if not isinstance(_meta, dict):
        _meta = {}

    # Deterministic weighted score; explicit meta.score dominates, then profitability/risk proxies.
    _parts = {
        'rank_base': _safe_float(cfg.get('profile_rank', {}).get(_label, 0.0), 0.0),
        'meta_score': _safe_float(_meta.get('score'), 0.0),
        'expected_return_pct': _safe_float(_meta.get('expected_return_pct'), 0.0) * 1.50,
        'expected_pnl_usd': _safe_float(_meta.get('expected_pnl_usd'), 0.0) * 0.01,
        'win_rate_pct': _safe_float(_meta.get('win_rate_pct'), 0.0) * 0.40,
        'sharpe': _safe_float(_meta.get('sharpe'), 0.0) * 25.0,
        'max_drawdown_penalty': -abs(_safe_float(_meta.get('max_drawdown_pct'), 0.0)) * 0.80,
    }
    if regime_obj is not None:
        _parts['regime_confidence_boost'] = _safe_float(getattr(regime_obj, 'confidence', 0.0), 0.0) * 5.0
    else:
        _parts['regime_confidence_boost'] = 0.0

    _total = round(sum(_parts.values()), 6)
    return _total, _parts


def _run_regime_profile_auto_selector(settings, regime_obj: Optional[MarketRegime], db_client=None) -> Dict[str, Any]:
    """
    Deterministic profile selector with hysteresis.
    - Reads all profiles for current regime.
    - Scores candidates using deterministic weighted metadata.
    - Applies anti-flap rules: min_hold_cycles + min_score_delta.
    - Persists selector state in trading_runtime_state and writes audit decisions.
    """
    if db_client is None:
        return {"ok": False, "skipped": True, "reason": "no_db_client"}
    if regime_obj is None:
        return {"ok": False, "skipped": True, "reason": "no_regime"}

    _regime = str(getattr(regime_obj, 'mode', '') or '').strip().upper()
    if not _regime:
        return {"ok": False, "skipped": True, "reason": "invalid_regime"}
    if not bool(getattr(regime_obj, 'confirmed', False)):
        return {"ok": False, "skipped": True, "reason": "regime_unconfirmed", "regime": _regime}

    _cfg = _load_regime_selector_config(db_client)
    if not _cfg.get('profile_selector_enabled', _cfg.get('enabled', True)):
        return {"ok": True, "skipped": True, "reason": "selector_disabled", "regime": _regime}

    _prefix = f"regime_profile_{_regime}_"
    _rows = db_client.execute_query(
        "SELECT key, value, updated_at FROM trading_runtime_state WHERE key LIKE %s ORDER BY key",
        (f"{_prefix}%",)
    )
    _profiles: Dict[str, Dict[str, Any]] = {}
    for _r in _rows:
        _key = str(_r.get('key') or '')
        if not _key.startswith(_prefix):
            continue
        _label = _key[len(_prefix):].strip().upper()
        if not _label:
            continue
        _val = _parse_json_like(_r.get('value'))
        if isinstance(_val, dict):
            _profiles[_label] = _val

    if not _profiles:
        return {"ok": True, "skipped": True, "reason": "no_profiles_for_regime", "regime": _regime}

    _active_key = f"regime_profile_active_{_regime}"
    _active_rows = db_client.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (_active_key,)
    )
    _active_profile = None
    if _active_rows and _active_rows[0].get('value') is not None:
        _av = _parse_json_like(_active_rows[0]['value'])
        if isinstance(_av, dict):
            _active_profile = str(_av.get('profile') or _av.get('label') or '').strip().upper() or None
        elif isinstance(_av, str):
            _active_profile = _av.strip().upper() or None

    _state_key = f"smart_total_profile_selector_state_{_regime}"
    _state_rows = db_client.execute_query(
        "SELECT value FROM trading_runtime_state WHERE key=%s", (_state_key,)
    )
    _state = {}
    if not _state_rows:
        # Backward-compatible fallback for previous selector state key.
        _state_rows = db_client.execute_query(
            "SELECT value FROM trading_runtime_state WHERE key=%s",
            (f"regime_profile_selector_state_{_regime}",)
        )
    if _state_rows and _state_rows[0].get('value') is not None:
        _sv = _parse_json_like(_state_rows[0]['value'])
        if isinstance(_sv, dict):
            _state = _sv

    _scored = []
    for _label, _payload in _profiles.items():
        _score, _parts = _score_profile_candidate(_label, _payload, regime_obj, _cfg)
        _scored.append((_label, _score, _parts))
    _scored.sort(key=lambda x: (-x[1], x[0]))

    _best_label, _best_score, _best_parts = _scored[0]
    _score_map = {lbl: sc for (lbl, sc, _parts) in _scored}
    _top3 = [{"profile": lbl, "score": sc, "parts": parts} for (lbl, sc, parts) in _scored[:3]]

    _current = (_active_profile or '').strip().upper() or None
    _current_score = _score_map.get(_current, None)
    _hold_cycles = int(_safe_float(_state.get('cycles_since_switch', 999), 999))
    _delta = (_best_score - (_current_score if _current_score is not None else -1e9))

    _switch = False
    _reason = "keep_current"
    _selected = _current or _best_label
    if _current is None:
        _selected = _best_label
        _switch = True
        _reason = "no_active_profile"
    elif _current not in _score_map:
        _selected = _best_label
        _switch = True
        _reason = "active_profile_missing"
    elif _best_label == _current:
        _selected = _current
        _reason = "best_already_active"
    else:
        _min_hold = int(_cfg.get('profile_min_hold_cycles', _cfg.get('min_hold_cycles', 3)))
        _min_delta = _safe_float(
            _cfg.get('profile_min_score_delta', _cfg.get('min_score_delta', 0.05)),
            0.05,
        )
        if _hold_cycles < _min_hold:
            _selected = _current
            _reason = f"hysteresis_hold_cycles({_hold_cycles}<{_min_hold})"
        elif _delta < _min_delta:
            _selected = _current
            _reason = f"hysteresis_score_delta({_delta:.4f}<{_min_delta:.4f})"
        else:
            _selected = _best_label
            _switch = True
            _reason = f"switch_better_score(delta={_delta:.4f})"

    _now_iso = datetime.now(timezone.utc).isoformat()
    _new_cycles = 0 if _switch else (_hold_cycles + 1)
    _state_payload = {
        "regime": _regime,
        "last_selected": _selected,
        "last_selected_score": _score_map.get(_selected, _best_score),
        "best_candidate": _best_label,
        "best_candidate_score": _best_score,
        "active_before": _current,
        "switched": _switch,
        "decision_reason": _reason,
        "cycles_since_switch": _new_cycles,
        "last_decision_at": _now_iso,
        "selector_config": {
            "profile_min_hold_cycles": _cfg.get('profile_min_hold_cycles', _cfg.get('min_hold_cycles')),
            "profile_min_score_delta": _cfg.get('profile_min_score_delta', _cfg.get('min_score_delta')),
            "audit_every_cycle": _cfg.get('audit_every_cycle'),
        },
    }

    _audit_detail = {
        "regime": _regime,
        "confirmed": bool(getattr(regime_obj, 'confirmed', False)),
        "confidence": _safe_float(getattr(regime_obj, 'confidence', 0.0), 0.0),
        "active_before": _current,
        "selected_profile": _selected,
        "best_candidate": _best_label,
        "best_score": _best_score,
        "best_parts": _best_parts,
        "delta_vs_current": _delta,
        "switched": _switch,
        "reason": _reason,
        "top_candidates": _top3,
        "state": _state_payload,
    }

    _writes = []
    if _switch:
        _active_payload = {
            "regime": _regime,
            "profile": _selected,
            "reason": "auto_selector",
            "updated_at": _now_iso,
            "updated_by": "system:auto_selector",
            "best_candidate": _best_label,
            "best_score": _best_score,
            "delta_vs_current": _delta,
            "decision_reason": _reason,
        }
        _active_json = json.dumps(_active_payload)
        _writes.append((
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (_active_key, _active_json, _active_json),
        ))

    _state_json = json.dumps(_state_payload)
    _writes.append((
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (_state_key, _state_json, _state_json),
    ))
    # Backward-compatible mirrored write for old key used by existing dashboards/queries.
    _legacy_state_key = f"regime_profile_selector_state_{_regime}"
    _writes.append((
        "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
        (_legacy_state_key, _state_json, _state_json),
    ))
    if bool(_cfg.get('audit_every_cycle', True)) or _switch:
        _writes.append((
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES ('__config__', 'system', 'regime_profile_selector_decision', %s, false)",
            (json.dumps(_audit_detail),),
        ))

    if len(_writes) == 1:
        db_client.execute_update(_writes[0][0], _writes[0][1])
    else:
        db_client.execute_transaction(_writes)

    if _switch:
        try:
            from pair_screener import invalidate_cache as _invalidate_screener_cache
            _invalidate_screener_cache()
        except Exception:
            pass

    return {
        "ok": True,
        "skipped": False,
        "regime": _regime,
        "active_before": _current,
        "selected_profile": _selected,
        "switched": _switch,
        "reason": _reason,
        "best_candidate": _best_label,
        "best_score": _best_score,
        "delta_vs_current": _delta,
    }


def _apply_fng_kelly_boost(trade_allocation: float, fng_value: int, settings) -> float:
    """R1: Behavioral Finance — scale Kelly size by Fear & Greed reading.
    Capitulation (<= fng_capitulation_threshold): max boost (captures fear bottoms aggressively).
    Extreme Fear (<= fng_extreme_fear_threshold): strong boost.
    Extreme Greed (>= fng_extreme_greed_threshold): reduce (avoid buying tops).
    All other values: no change.
    """
    if not _get_param(settings, 'fng_kelly_boost_enabled'):
        return trade_allocation
    cap_thr  = _get_param(settings, 'fng_capitulation_threshold')
    fear_thr = _get_param(settings, 'fng_extreme_fear_threshold')
    greed_thr = _get_param(settings, 'fng_extreme_greed_threshold')
    if fng_value <= cap_thr:
        mult = _get_param(settings, 'fng_capitulation_multiplier')
        label = f"CAPITULACION F&G={fng_value}"
    elif fng_value <= fear_thr:
        mult = _get_param(settings, 'fng_extreme_fear_multiplier')
        label = f"EXTREME_FEAR F&G={fng_value}"
    elif fng_value >= greed_thr:
        mult = _get_param(settings, 'fng_extreme_greed_multiplier')
        label = f"EXTREME_GREED F&G={fng_value}"
    else:
        return trade_allocation
    boosted = round(trade_allocation * mult, 2)
    print(f"[F&G Kelly] {label} → ${trade_allocation:.2f} × {mult} = ${boosted:.2f}")
    return boosted


def _get_funding_rate(symbol: str, settings) -> float:
    """R6: On-chain — fetch & cache funding rate for a symbol.
    Returns None on error so callers can fail safe.
    Cache TTL = funding_rate_cache_minutes (default 10 min).
    """
    global _funding_rate_cache
    ttl_sec = getattr(settings, 'funding_rate_cache_minutes', 10) * 60
    now = time.monotonic()
    cached = _funding_rate_cache.get(symbol)
    if cached and (now - cached[1]) < ttl_sec:
        return cached[0]
    try:
        import urllib.request as _ureq, json as _json
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        with _ureq.urlopen(url, timeout=4) as resp:
            data = _json.loads(resp.read())
        rate = float(data[0].get('fundingRate', 0)) if data else 0.0
        _funding_rate_cache[symbol] = (rate, now)
        return rate
    except Exception as exc:
        logger.warning("Funding rate fetch failed for %s: %s", symbol, exc)
        return None


def _check_liquidity_and_slippage(symbol: str, vol_24h_usdt: float,
                                   price: float, tp_pct: float, settings) -> tuple:
    """R3: Microestructura — returns (ok: bool, reason: str).
    Filters:
      1. vol_24h_usdt < min_liquidity_vol_usdt → illiquid, skip
      2. estimated_spread / TP > max_slippage_tp_ratio → slippage eats profit
    Estimated spread uses a conservative 0.05% of price (Binance USDT pairs).
    """
    if not getattr(settings, 'liquidity_filter_enabled', True):
        return True, ""
    min_vol = getattr(settings, 'min_liquidity_vol_usdt', 3_000_000.0)
    if vol_24h_usdt > 0 and vol_24h_usdt < min_vol:
        return False, f"BAJA_LIQUIDEZ vol24h=${vol_24h_usdt/1e6:.1f}M < ${min_vol/1e6:.0f}M"
    max_ratio = getattr(settings, 'max_slippage_tp_ratio', 0.25)
    est_spread_pct = 0.0005  # 0.05% conservative spread for Binance spot
    if tp_pct > 0 and (est_spread_pct / tp_pct) > max_ratio:
        return False, f"SLIPPAGE_ALTO spread={est_spread_pct*100:.2f}% > {max_ratio*100:.0f}% of TP={tp_pct*100:.1f}%"
    return True, ""


def _consecutive_sl_count(symbol: str, position_manager, n: int = 3) -> bool:
    """WR Screener: returns True if the last N closed trades for symbol were ALL stop_loss.
    Uses only clean closes (not rebalancing/manual) to detect genuine losing streaks.
    MEJO-G: Only considers trades from the last 30 days to avoid blocking pairs indefinitely.
    """
    try:
        from datetime import timedelta
        _cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        closed = position_manager.get_closed_positions(limit=200)
        sym_trades = []
        for p in closed:
            if getattr(p, 'symbol', '') != symbol:
                continue
            if 'rebalancing' in (getattr(p, 'close_reason', '') or ''):
                continue
            if 'manual' in (getattr(p, 'close_reason', '') or ''):
                continue
            # MEJO-G: skip trades older than 30 days
            _exit = getattr(p, 'exit_time', None)
            if _exit is not None:
                try:
                    if isinstance(_exit, str):
                        _exit = datetime.fromisoformat(_exit.replace('Z', '+00:00'))
                    if hasattr(_exit, 'tzinfo') and _exit.tzinfo is None:
                        _exit = _exit.replace(tzinfo=timezone.utc)
                    if _exit < _cutoff:
                        continue
                except Exception:
                    pass
            sym_trades.append(p)
        if len(sym_trades) < n:
            return False
        recent_n = sym_trades[:n]
        return all('stop_loss' in (getattr(t, 'close_reason', '') or '') for t in recent_n)
    except Exception:
        return False


def _check_category_limit(symbol: str, position_manager, settings) -> tuple:
    """R4: Correlaciones — returns (ok: bool, reason: str).
    Enforces per-category position limits to avoid over-concentration in
    correlated buckets (e.g. 4 MEMEs crashing together).
    """
    if not getattr(settings, 'category_max_positions_enabled', True):
        return True, ""
    if position_manager is None:
        return True, ""
    cat_limits = _get_param(settings, 'category_max_positions')
    sym_cat = getattr(settings, 'token_categories', {}).get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
    max_cat = cat_limits.get(sym_cat, 99)
    all_positions = position_manager.get_all_positions()
    cat_map = getattr(settings, 'token_categories', {})
    count_cat = sum(
        1 for sym in all_positions
        if all_positions[sym] and cat_map.get(sym, {}).get('market_behavior', 'MIDCAP_ALT') == sym_cat
    )
    if count_cat >= max_cat:
        return False, f"LÍM_CATEGORÍA {sym_cat} ({count_cat}/{max_cat})"
    return True, ""


def _circuit_breaker_check(settings) -> bool:
    """R5: Circuit breaker — returns True if system is paused due to consecutive failures.
    Only reacts to technical errors (API failures), NOT to market losses.
    """
    global _circuit_breaker_until
    if not getattr(settings, 'circuit_breaker_enabled', True):
        return False
    if _circuit_breaker_until is None:
        return False
    now_utc = datetime.now(timezone.utc)
    if _circuit_breaker_until.tzinfo is None:
        _circuit_breaker_until = _circuit_breaker_until.replace(tzinfo=timezone.utc)
    if now_utc >= _circuit_breaker_until:
        _circuit_breaker_until = None
        print("[CircuitBreaker] Pause expired — resuming normal operation")
        return False
    remaining = (_circuit_breaker_until - now_utc).total_seconds() / 60
    print(f"[CircuitBreaker] PAUSED — {remaining:.1f} min remaining")
    return True


def _record_order_failure(settings) -> None:
    """Increment consecutive failure counter; trigger circuit breaker if threshold reached."""
    global _consecutive_failures, _circuit_breaker_until
    if not getattr(settings, 'circuit_breaker_enabled', True):
        return
    _consecutive_failures += 1
    threshold = getattr(settings, 'circuit_breaker_consecutive_failures', 5)
    if _consecutive_failures >= threshold:
        pause_min = getattr(settings, 'circuit_breaker_pause_minutes', 10)
        from datetime import timedelta
        _circuit_breaker_until = datetime.now(timezone.utc) + timedelta(minutes=pause_min)
        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_WARNING,
                     f"Circuit breaker TRIGGERED: {_consecutive_failures} consecutive failures → "
                     f"pausing {pause_min} min until {_circuit_breaker_until.strftime('%H:%M')} UTC")
        print(f"[CircuitBreaker] TRIGGERED after {_consecutive_failures} failures — "
              f"pausing {pause_min} min")
        _request_smart_recheck(
            "circuit_breaker_triggered",
            {"consecutive_failures": _consecutive_failures, "pause_minutes": pause_min},
        )
    else:
        _request_smart_recheck(
            "order_failure",
            {"consecutive_failures": _consecutive_failures, "threshold": threshold},
        )
    if _db_client_ref:
        try:
            state_persistence.save_state(
                _db_client_ref,
                futures_sl_cooldown=_FUTURES_SL_COOLDOWN,
                futures_mass_sl_cooldown_until=_FUTURES_MASS_SL_COOLDOWN_UNTIL,
                volspike_cooldown=_VOLSPIKE_COOLDOWN,
                volspike_positions=_VOLSPIKE_POSITIONS,
                override_daily_count=_OVERRIDE_DAILY_COUNT,
                circuit_breaker_until=_circuit_breaker_until,
                consecutive_failures=_consecutive_failures,
            )
        except Exception as _cbp_err:
            print(f"[CircuitBreaker] persist failure state failed: {_cbp_err}")


def _record_order_success() -> None:
    """Reset consecutive failure counter on any successful order."""
    global _consecutive_failures
    if _consecutive_failures > 0:
        print(f"[CircuitBreaker] Resetting failure counter (was {_consecutive_failures})")
        _consecutive_failures = 0
        _request_smart_recheck("order_success_recovery", {"reset_from": "consecutive_failures"})
        if _db_client_ref:
            try:
                state_persistence.save_state(
                    _db_client_ref,
                    futures_sl_cooldown=_FUTURES_SL_COOLDOWN,
                    futures_mass_sl_cooldown_until=_FUTURES_MASS_SL_COOLDOWN_UNTIL,
                    volspike_cooldown=_VOLSPIKE_COOLDOWN,
                    volspike_positions=_VOLSPIKE_POSITIONS,
                    override_daily_count=_OVERRIDE_DAILY_COUNT,
                    circuit_breaker_until=_circuit_breaker_until,
                    consecutive_failures=_consecutive_failures,
                )
            except Exception as _cbp_ok_err:
                print(f"[CircuitBreaker] persist success state failed: {_cbp_ok_err}")


def _is_overflow_eligible(symbol: str, strength: float, prediction: dict, settings) -> bool:
    """Return True if the signal qualifies for an extra slot beyond the regime max.
    Requires Kelly FUERTE tier quality: strength >= overflow_min_strength AND
    success_probability >= overflow_min_prob AND expected_pnl > 0."""
    _min_str  = _get_param(settings, 'overflow_min_strength')
    _min_prob = _get_param(settings, 'overflow_min_prob')
    _max_extra = _get_param(settings, 'overflow_max_extra')
    if len(_overflow_positions) >= _max_extra:
        print(f"[Overflow] {symbol}: all {_max_extra} overflow slots occupied")
        return False
    if strength < _min_str:
        print(f"[Overflow] {symbol}: strength={strength:.2f} < min={_min_str} — not eligible")
        return False
    _prob = float((prediction or {}).get('success_probability', 0))
    _epnl = float((prediction or {}).get('expected_pnl', 0))
    if _prob < _min_prob:
        print(f"[Overflow] {symbol}: prob={_prob:.2f} < min={_min_prob} — not eligible")
        return False
    if _epnl <= 0:
        print(f"[Overflow] {symbol}: expected_pnl={_epnl:.3f} <= 0 — not eligible")
        return False
    return True


def _is_futures_overflow_eligible(
    symbol: str, strength: float, vol_spike: float,
    chg24: float, btc_4h_chg: float, settings, fc
) -> bool:
    """Return True if a futures SHORT qualifies for an extra slot beyond futures_max_shorts.
    Simplified: in FUTURES_SHORT regime (confirmed bear), only require strength + 24h drop.
    BTC momentum and vol_spike are already checked at entry time by separate filters.
    Max simultaneous overflow slots controlled by futures_overflow_max_extra (default 3)."""
    _min_str   = getattr(settings, 'futures_overflow_min_strength', 0.55)
    _min_drop  = getattr(settings, 'futures_overflow_min_drop_24h', -3.0)
    _max_extra = getattr(settings, 'futures_overflow_max_extra', 1)
    _cur_ovfl  = len(_futures_overflow_positions)
    if _cur_ovfl >= _max_extra:
        print(f"[Futures/Overflow] {symbol}: all {_max_extra} overflow slots occupied ({_cur_ovfl}/{_max_extra})")
        return False
    if strength < _min_str:
        print(f"[Futures/Overflow] {symbol}: strength={strength:.2f} < min={_min_str} — not eligible")
        return False
    if chg24 > _min_drop:
        print(f"[Futures/Overflow] {symbol}: chg24={chg24:.2f}% > {_min_drop}% drop threshold — not eligible")
        return False
    # Already have this symbol in overflow
    if symbol in _futures_overflow_positions:
        print(f"[Futures/Overflow] {symbol}: already in overflow set")
        return False
    print(f"[Futures/Overflow] {symbol}: ELIGIBLE — str={strength:.2f}, chg24={chg24:.1f}%")
    return True


def _try_replace_weakest_position(
    new_symbol: str,
    new_strength: float,
    new_prediction: dict,
    position_manager,
    portfolio_manager,
    client,
    settings,
    regime_mode: str,
) -> bool:
    """Attempt to replace the weakest open position with a better new signal.
    Returns True if a replacement was executed (sell done), False otherwise.

    Conditions (all must pass):
    1. new EV >= worst_open_ev * replace_ev_min_ratio
    2. new strength > avg_strength_open + replace_strength_min_delta
    3. new success_probability > worst_open success_probability
    4. worst position held >= min_hold_minutes
    5. worst position is not in profit (replace_only_losing=True by default)
    """
    if not position_manager or not portfolio_manager:
        return False
    try:
        _all_pos = position_manager.get_all_positions()
        if not _all_pos:
            return False

        # Build current positions dict for prediction
        _current_pos = {}
        for _sym, _plist in _all_pos.items():
            if _plist:
                _p = _plist[0]
                _current_pos[_sym] = {'quantity': float(_p.quantity), 'entry_price': float(_p.entry_price)}

        if not _current_pos:
            return False

        # Get predictor EV for all open positions
        _open_preds = portfolio_manager.get_position_predictions(_current_pos)
        if not _open_preds:
            return False

        # Identify worst position by expected_pnl
        _worst_sym = min(_open_preds, key=lambda s: _open_preds[s].get('expected_pnl', 0))
        _worst_pred = _open_preds[_worst_sym]
        _worst_ev   = float(_worst_pred.get('expected_pnl', 0))
        _worst_prob = float(_worst_pred.get('success_probability', 0.5))

        # Avg strength of open positions — compute from signal metadata stored at entry
        _strengths = []
        for _s, _plist in _all_pos.items():
            if _plist:
                _meta_s = float(getattr(_plist[0], 'metadata', {}).get('strength', 0) or 0)
                if _meta_s > 0:
                    _strengths.append(_meta_s)
        _avg_strength = sum(_strengths) / len(_strengths) if _strengths else 0.45  # default: conservative

        # Check: new EV vs worst open EV (BUG-C fix: also check when worst_ev < 0)
        _ev_ratio = getattr(settings, 'replace_ev_min_ratio', 1.1)
        _new_ev = float((new_prediction or {}).get('expected_pnl', 0))
        _new_prob = float((new_prediction or {}).get('success_probability', 0))

        if _new_ev <= _worst_ev:
            print(f"[Replace] {new_symbol}: new_ev={_new_ev:.3f} <= worst_ev={_worst_ev:.3f} — not an improvement")
            return False
        if _worst_ev >= 0 and _new_ev < _worst_ev * _ev_ratio:
            print(f"[Replace] {new_symbol}: new_ev={_new_ev:.3f} < worst_ev={_worst_ev:.3f}×{_ev_ratio} — skip")
            return False
        if _new_prob <= _worst_prob:
            print(f"[Replace] {new_symbol}: new_prob={_new_prob:.2f} <= worst_prob={_worst_prob:.2f} — skip")
            return False

        # Check: strength delta vs real avg (BUG-B fix: use computed avg, not hardcoded 0.5)
        _str_delta = getattr(settings, 'replace_strength_min_delta', 0.05)
        if new_strength < _avg_strength + _str_delta:
            print(f"[Replace] {new_symbol}: strength={new_strength:.2f} < avg+delta={_avg_strength+_str_delta:.2f} — skip")
            return False

        # Check: worst position must have been held >= min_hold_minutes (dynamic by regime)
        if regime_mode == 'RANGE_SCALP':
            _min_hold = getattr(settings, 'min_hold_minutes_range_scalp', 30)
        elif regime_mode == 'SPOT_BULL':
            _min_hold = _get_param(settings, 'min_hold_minutes_spot_bull')
        else:
            _min_hold = _get_param(settings, 'min_hold_minutes')
        _worst_pos_list = _all_pos.get(_worst_sym, [])
        if _worst_pos_list:
            _wp = _worst_pos_list[0]
            _entry_ts = getattr(_wp, 'entry_time', None)
            if _entry_ts is not None:
                try:
                    if isinstance(_entry_ts, str):
                        _entry_ts = datetime.fromisoformat(_entry_ts.replace('Z', '+00:00'))
                    if hasattr(_entry_ts, 'tzinfo') and _entry_ts.tzinfo is None:
                        _entry_ts = _entry_ts.replace(tzinfo=timezone.utc)
                    _age_min = (datetime.now(timezone.utc) - _entry_ts).total_seconds() / 60
                    if _age_min < _min_hold:
                        print(f"[Replace] {_worst_sym}: age={_age_min:.1f}min < min_hold={_min_hold}min — protected")
                        return False
                except Exception as _ts_err:
                    print(f"[Replace] {_worst_sym}: entry_ts parse error ({_ts_err}) — skipping min_hold check")

        # Check: replace_only_losing — skip if worst position is in profit
        if getattr(settings, 'replace_only_losing', True):
            try:
                _current_price = _get_cycle_price(client, _worst_sym)
                _entry_price = float(_worst_pos_list[0].entry_price) if _worst_pos_list else 0
                if _entry_price > 0 and _current_price > _entry_price:
                    print(f"[Replace] {_worst_sym}: in profit ({(_current_price/_entry_price-1)*100:+.2f}%) — protected by replace_only_losing")
                    return False
            except Exception:
                pass

        # All checks passed — execute the replacement sell
        print(f"[Replace] Replacing {_worst_sym} (ev={_worst_ev:.3f}) with {new_symbol} (ev={_new_ev:.3f}, str={new_strength:.2f})")
        from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid
        try:
            _balances = client.get_account_balance()
            _asset = _worst_sym.replace("USDT", "")
            _qty_held = float(_balances.get(_asset, 0))
            if _qty_held <= 0:
                print(f"[Replace] {_worst_sym}: no balance to sell")
                return False
            _filters = get_symbol_filters(client, _worst_sym)
            _qty = align_quantity_to_step(_qty_held, _filters["step"])
            _valid, _reason = is_quantity_valid(_qty, _get_cycle_price(client, _worst_sym), _filters)
            if not _valid:
                print(f"[Replace] {_worst_sym}: invalid qty — {_reason}")
                return False
            _order = client.create_market_order(symbol=_worst_sym, side="SELL", quantity=_qty)
            if not client.verify_order_filled(_order, f"Replace SELL {_worst_sym}"):
                print(f"[WARN] Replace SELL {_worst_sym}: order NOT confirmed filled — aborting replacement")
                return False
            _exit_price = _get_cycle_price(client, _worst_sym)
            commission_rate = _get_commission_config(settings).rate_for('spot')
            position_manager.close_all_positions_for_symbol(
                _worst_sym,
                _exit_price,
                "active_replacement",
                commission_rate,
                exit_context={
                    'regime_mode': regime_mode,
                    'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                },
            )
            _overflow_positions.discard(_worst_sym)   # INEF-4: free overflow slot if used
            _VOLSPIKE_POSITIONS.discard(_worst_sym)   # INEF-3: free volspike slot if used
            add_signal({"symbol": _worst_sym, "action": "SELL", "price": _exit_price,
                        "reason": f"active_replacement→{new_symbol}", "source": "replacement"})
            print(f"[Replace] Sold {_qty} {_worst_sym} @ {_exit_price:.4f} — slot freed for {new_symbol}")
            return True
        except Exception as _sell_err:
            print(f"[Replace] Sell failed for {_worst_sym}: {_sell_err}")
            return False

    except Exception as _e:
        print(f"[Replace] Error in _try_replace_weakest_position: {_e}")
        return False


def _get_news_scores(symbols: List[str], news_client: NewsClient, settings) -> Dict[str, float]:
    if not symbols:
        return {}

    recent = news_client.get_recent_news(symbols, settings.news_lookback_hours)
    scores: Dict[str, float] = {}
    for article in recent:
        text = f"{article.get('title', '')} {article.get('summary', '')}".upper()
        for symbol in symbols:
            if symbol.upper() in text:
                scores[symbol] = scores.get(symbol, 0.0) + 1.0
    return scores


def _quality_score_for_regime(
    regime_mode: str,
    price_change_pct_abs: float,
    price_change_pct_signed: float,
    volatility_limit: float,
    volume_usdt: float = 0.0,
    min_volume_usdt: float = 10_000_000.0,
) -> float:
    """
    Return a mode-aware quality score used by screener top-N prefilter.

    Higher score means better rank inside the active regime objective.
    """
    _regime = str(regime_mode or '').strip().upper() or 'SPOT_BULL'
    _abs = max(0.0, float(price_change_pct_abs))
    _signed = float(price_change_pct_signed)
    _vol_limit = max(0.01, float(volatility_limit))
    _vol_usdt = max(0.0, float(volume_usdt or 0.0))
    _min_vol = max(1.0, float(min_volume_usdt or 1.0))

    # Liquidity proxy: better execution quality for short-term opportunities.
    # Saturates above ~10x the minimum volume threshold.
    _liq = min(1.0, _vol_usdt / (_min_vol * 10.0)) if _min_vol > 0 else 0.0
    _liq_mult = 0.80 + 0.40 * _liq

    if _regime == 'SPOT_BULL':
        # Prefer constructive momentum, penalize high-vol downside moves.
        upside = max(0.0, _signed)
        downside_penalty = max(0.0, -_signed) * 2.0
        volatility_penalty = min(1.0, _abs / _vol_limit) * 0.25
        return (upside - downside_penalty - volatility_penalty) * _liq_mult

    if _regime == 'RANGE_SCALP':
        # Prefer mean-reversion opportunities with tradable oscillation.
        # Target moderate oscillation; extremely low/high movement is less useful.
        _target = max(0.01, min(0.06, _vol_limit * 0.30))
        _distance = abs(_abs - _target)
        _shape_bonus = max(0.0, 1.0 - (_distance / max(_target, 1e-6)))
        return (_abs * 0.70 + _shape_bonus * 0.30) * _liq_mult

    if _regime == 'PRESERVATION':
        # Defensive mode: prefer calmer assets with mild positive drift.
        calmness = max(0.0, _vol_limit - _abs)
        stability = max(0.0, _signed)
        return (calmness + stability * 0.20) * (0.90 + 0.20 * _liq)

    if _regime == 'FUTURES_SHORT':
        # For short bias, prioritize overbought bounces that can mean-revert down,
        # while still allowing continuation setups.
        bounce = max(0.0, _signed)
        downside = max(0.0, -_signed)
        return (bounce * 1.10 + downside * 0.55 + _abs * 0.20) * _liq_mult

    return max(0.0, _signed)


def _filter_by_quality(
    symbols: List[str],
    client,
    settings,
    target_count: int = 100,
    regime_mode: Optional[str] = None,
) -> List[str]:
    """
    Regime-aware quality prefilter for screener symbols.

    Shared hard filters:
    - max volatility
    - minimum price
    """
    max_volatility = float(_get_param(settings, 'screener_quality_max_volatility'))
    min_vol_usdt = float(_get_param(settings, 'screener_quality_min_vol_usdt'))
    min_price = float(_get_param(settings, 'screener_quality_min_price_usdt'))
    _regime = str(regime_mode or _get_param(settings, 'current_regime_mode', 'SPOT_BULL')).strip().upper() or 'SPOT_BULL'

    accepted: List[Tuple[str, float]] = []
    fallback_symbols: List[str] = []

    for symbol in symbols:
        try:
            ticker = client.get_client().get_ticker(symbol=symbol)
            price_change_signed = float(ticker.get('priceChangePercent', 0.0)) / 100.0
            price_change_abs = abs(price_change_signed)
            last_price = float(ticker.get('lastPrice', 0.0))
            vol_usdt = float(ticker.get('quoteVolume', 0.0) or 0.0)

            if price_change_abs > max_volatility:
                continue
            if last_price < min_price:
                continue
            if vol_usdt < min_vol_usdt:
                continue

            score = _quality_score_for_regime(
                _regime,
                price_change_pct_abs=price_change_abs,
                price_change_pct_signed=price_change_signed,
                volatility_limit=max_volatility,
                volume_usdt=vol_usdt,
                min_volume_usdt=min_vol_usdt,
            )
            accepted.append((symbol, score))
        except Exception:
            # Keep unknown data as fallback only; known-quality symbols rank first.
            fallback_symbols.append(symbol)

    accepted.sort(key=lambda x: x[1], reverse=True)
    filtered = [sym for sym, _ in accepted[:target_count]]

    if len(filtered) < target_count and fallback_symbols:
        for symbol in fallback_symbols:
            if symbol in filtered:
                continue
            filtered.append(symbol)
            if len(filtered) >= target_count:
                break

    # Adaptive relaxation to avoid collapsing coverage in stressed markets.
    _min_required = max(1, int(target_count * 0.30))
    if len(filtered) >= _min_required:
        return filtered

    relaxed = list(filtered)
    relaxed_volatility = min(0.90, float(max_volatility) + 0.25)
    for symbol in symbols:
        if symbol in relaxed:
            continue
        try:
            ticker = client.get_client().get_ticker(symbol=symbol)
            price_change_pct = abs(float(ticker.get('priceChangePercent', 0))) / 100
            last_price = float(ticker.get('lastPrice', 0))
            if price_change_pct > relaxed_volatility:
                continue
            if last_price < min_price:
                continue
            relaxed.append(symbol)
            if len(relaxed) >= target_count:
                break
        except Exception:
            continue

    if len(relaxed) > len(filtered):
        print(
            f"[Screener v2] quality relax enabled: {len(filtered)} -> {len(relaxed)} "
            f"(target={target_count}, regime={_regime}, max_vol={max_volatility:.2f}->{relaxed_volatility:.2f})"
        )
    return relaxed


def _analysis_cache_age_minutes(symbol: str) -> Optional[float]:
    _entry = analysis_cache.get(symbol)
    if not isinstance(_entry, dict):
        return None
    _ts = _entry.get('timestamp')
    if not _ts:
        return None
    try:
        _dt = datetime.fromisoformat(str(_ts).replace('Z', '+00:00'))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - _dt).total_seconds() / 60.0
    except Exception:
        return None


def _signal_max_age_minutes_for_regime(regime: str) -> int:
    _ttl = {
        'SPOT_BULL': 10,
        'RANGE_SCALP': 5,
        'PRESERVATION': 20,
        'FUTURES_SHORT': 3,
    }
    return int(_ttl.get(str(regime or 'SPOT_BULL').strip().upper(), 10))


def _is_analysis_cache_fresh(symbol: str, regime: str) -> bool:
    _age_min = _analysis_cache_age_minutes(symbol)
    if _age_min is None:
        return False
    return _age_min <= _signal_max_age_minutes_for_regime(regime)


def _get_top_buy_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager=None, regime: str = "SPOT_BULL", db_client=None) -> List[Dict]:
    _active_profile_label = _get_active_profile_label(db_client or _db_client_ref, regime)
    # Expire autonomous signals — TTL aligned to regime cycle (B-v2)
    _regime_for_ttl = regime if regime else "SPOT_BULL"
    _ttl_by_regime = {"SPOT_BULL": 45, "RANGE_SCALP": 15, "PRESERVATION": 90, "FUTURES_SHORT": 24}
    _signal_ttl = _ttl_by_regime.get(_regime_for_ttl, 30)
    expired = expire_stale_signals(max_age_minutes=_signal_ttl)
    if expired:
        print(f"Expired {expired} stale autonomous signal(s) (TTL={_signal_ttl}min for {_regime_for_ttl})")

    # First try to get signals from external sources (TradingView webhook)
    # Filter out autonomous signals to avoid using stale autonomous signals
    all_signals = get_signals(include_processed=False)
    external_signals = [s for s in all_signals if s.get('source') != 'autonomous']

    # Regime-aware freshness gate — discard external signals that are too old for the active mode.
    fresh_external = []
    _max_signal_age_min = _signal_max_age_minutes_for_regime(regime)
    for sig in external_signals:
        received_raw = sig.get('received_at')
        if received_raw:
            try:
                received_dt = datetime.fromisoformat(received_raw.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - received_dt).total_seconds() / 60
                if age_min > _max_signal_age_min:
                    print(f"Discarding stale external signal for {sig.get('symbol')} (age={age_min:.1f}min > {_max_signal_age_min}min, regime={regime})")
                    mark_signal_as_processed(sig.get('symbol'))
                    continue
            except Exception:
                pass
        fresh_external.append(sig)
    external_signals = fresh_external

    # If no external signals, use autonomous signal generation
    if not external_signals:
        print("No external signals found, using autonomous signal generation...")
        try:
            _universe = select_analysis_universe(
                settings=settings,
                regime_mode=regime,
                profile_label=_active_profile_label,
                db_client=db_client,
            )
            _static_symbols = _universe.get('static_symbols') or getattr(settings, 'analysis_symbols', [
                'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT'
            ])
            _screened_spot = list((_universe.get('spot') or []))
            _screened_futures = list((_universe.get('futures') or []))
            if _get_param(settings, 'screener_top_n_enabled') and _screened_spot:
                symbols = _filter_by_quality(
                    _screened_spot,
                    client,
                    settings,
                    target_count=settings.screener_top_n_count,
                    regime_mode=regime,
                )
                print(f"[Screener v2] Selected top {len(symbols)} quality assets (target: {settings.screener_top_n_count})")
                _new = [s for s in symbols if s not in _static_symbols]
                if _new:
                    print(f"[Screener v2] New vs static: {_new[:15]}{'...' if len(_new) > 15 else ''}")
            else:
                symbols = list((_universe.get('symbols') or []))
                if not symbols:
                    symbols = list(_screened_spot or _screened_futures or _static_symbols)
                _new = [s for s in symbols if s not in _static_symbols]
                if _new:
                    print(f"[Screener] Added {len(_new)} dynamic pairs: {_new[:10]}")

            _invalid_analysis_symbols = _INVALID_SYMBOLS | _INVALID_FUTURES_SYMBOLS
            _before_filter_count = len(symbols)
            symbols = [s for s in symbols if s not in _invalid_analysis_symbols]
            if len(symbols) != _before_filter_count:
                print(
                    f"[Screener] Removed {_before_filter_count - len(symbols)} invalid symbol(s) from analysis universe"
                )

            signal_generator = AutonomousSignalGenerator(client, symbols)  # reuse shared client
            signal_generator._regime_mode = regime  # s72 A4: expose to analyze_symbol for per-regime 4h filter
            _sel_max = _get_max_positions_for_regime(settings, regime or 'SPOT_BULL')
            autonomous_signals = signal_generator.get_top_buy_signals(
                interval='1h',
                limit=_sel_max + 5,  # +5 buffer: candidates for replacement/overflow evaluation
                news_client=news_client,
                regime_mode=regime,
                settings=settings,
                profile_label=_active_profile_label,
            )
            
            # Convert autonomous signals to expected format and add to signal store
            top_signals = []
            for signal in autonomous_signals:
                # MEJO-F: Obtain vol_24h_usdt for liquidity filter — use quote_volume from 24h ticker
                _vol_24h_usdt = 0.0
                try:
                    _ticker_24h = client.client.get_ticker(symbol=signal['symbol'])
                    _vol_24h_usdt = float(_ticker_24h.get('quoteVolume', 0.0))
                except Exception:
                    pass
                signal_dict = {
                    "symbol": signal['symbol'],
                    "score": signal['strength'] * 100,  # Convert to 0-100 scale
                    "reason": signal['reason'],
                    "indicators": signal.get('indicators', {}),
                    "strength": signal['strength'],
                    "volume_confirmed": signal.get('volume_confirmed', False),
                    "higher_tf_trend": signal.get('higher_tf_trend', 'FLAT'),
                    "expected_side": signal.get('expected_side', 'SHORT' if str(regime).upper() == 'FUTURES_SHORT' else 'LONG'),
                    "trade_type": signal.get('trade_type', 'futures' if str(regime).upper() == 'FUTURES_SHORT' else 'spot'),
                    # M3/M4: propagate vol_spike and breakout flag for conviction sizing
                    "vol_spike": signal.get('vol_spike', signal.get('indicators', {}).get('vol_spike', 0.0)),
                    "breakout_12h": signal.get('breakout_12h', False),
                    "rsi": signal.get('indicators', {}).get('rsi', 50.0),
                    # MEJO-F: vol_24h_usdt needed by liquidity filter
                    "volume_24h_usdt": _vol_24h_usdt,
                }
                top_signals.append(signal_dict)
                
                # Add autonomous signal to signal store for analysis cycle
                try:
                    # Include all technical indicators needed for VolSpike Override
                    signal_data = {
                        "symbol": signal['symbol'],
                        "action": "BUY",
                        "price": signal.get('current_price', 0),
                        "strength": signal['strength'],
                        "reason": signal['reason'],
                        "source": "autonomous"
                    }
                    
                    # Add technical indicators if available (needed for VolSpike Override)
                    # NOTE: TechnicalIndicators.analyze_signal() uses different key names:
                    #   volume_spike (bool) → we use the float vol_spike from signal root (set by autonomous_signals)
                    #   stoch_rsi_k → stoch_k
                    #   no bb_position → computed from lower/upper/price
                    #   atr (absolute) → atr_pct computed as atr/price*100
                    if 'indicators' in signal:
                        indicators = signal['indicators']
                        _price = indicators.get('price', signal.get('current_price', 1) or 1)
                        _lower = indicators.get('lower_band', _price)
                        _upper = indicators.get('upper_band', _price)
                        _bb_range = (_upper - _lower) if (_upper - _lower) > 0 else 1
                        _bb_pos = round((_price - _lower) / _bb_range, 4) if _bb_range > 0 else 0.5
                        _atr = indicators.get('atr', 0)
                        _atr_pct = round(_atr / _price * 100, 4) if _price > 0 and _atr else 0
                        signal_data.update({
                            "vol_spike": signal.get('vol_spike', 0),  # float ratio from autonomous_signals root
                            "rsi": indicators.get('rsi', 50),
                            "trend_4h": indicators.get('trend_4h', 0),
                            "atr_pct": _atr_pct,
                            "bb_position": _bb_pos,
                            "ema_cross": indicators.get('ema_cross', 0),
                            "macd": indicators.get('macd', 0),
                            "stoch_k": indicators.get('stoch_rsi_k', indicators.get('stoch_k', 0.5))
                        })
                    
                    add_signal(signal_data)
                    print(f"Added autonomous signal for {signal['symbol']} to signal store with indicators")
                except Exception as e:
                    print(f"Failed to add signal to store: {e}")
            
            print(f"Generated {len(top_signals)} autonomous buy signals")
            return top_signals
            
        except Exception as e:
            print(f"Error in autonomous signal generation: {e}")
            return []
    
    # Use external signals if available
    symbols = sorted({normalize_symbol(signal.get("symbol", "")) for signal in external_signals if signal.get("symbol")})
    news_scores = _get_news_scores(symbols, news_client, settings)

    # ── Mejora C: Enrich external signals with 4h trend + volume + strength ──
    try:
        sig_gen_ext = AutonomousSignalGenerator(client, symbols)
        for sig in external_signals:
            sym = normalize_symbol(sig.get("symbol", ""))
            if not sym:
                continue
            analysis = sig_gen_ext.analyze_symbol(sym, interval='1h', use_multi_timeframe=True, news_client=news_client)
            sig['strength'] = analysis.get('strength', 0.5)
            sig['higher_tf_trend'] = analysis.get('higher_tf_trend', 'FLAT')
            sig['volume_confirmed'] = analysis.get('volume_confirmed', False)
            if analysis.get('higher_tf_trend') == 'DOWN' and sig.get('action', '').upper() == 'BUY':
                print(f"External signal {sym}: 4h trend DOWN — strength halved to {sig['strength']:.2f}")
    except Exception as ext_err:
        print(f"External signal enrichment error: {ext_err}")
    
    # Get technical scores from analysis cache
    technical_scores: Dict[str, float] = {}
    for symbol in symbols:
        cached = analysis_cache.get(symbol)
        if cached:
            technical_scores[symbol] = cached.get("technical_score", 50.0)
        else:
            technical_scores[symbol] = 50.0  # Default neutral score if no analysis yet
    
    # Use Portfolio Manager if available
    if portfolio_manager:
        # Get risk metrics for all symbols
        from risk_calculator import RiskCalculator
        risk_calc = RiskCalculator()
        risk_metrics = {}
        
        for symbol in symbols:
            # For now, use default risk metrics
            # In production, this would use historical price data
            risk_metrics[symbol] = {"risk_score": 50.0}  # Default medium risk
        
        # Use Portfolio Manager ranking
        signal_scores = rank_for_portfolio_manager(
            external_signals, news_scores, technical_scores, risk_metrics,
            min_confidence=settings.min_pair_confidence_score, regime=regime
        )
        
        # Convert to list of dicts sorted by score
        _sel_max_ext = _get_max_positions_for_regime(settings, regime or 'SPOT_BULL')
        top_signals = [
            {"symbol": symbol, "score": score}
            for symbol, score in sorted(signal_scores.items(), key=lambda x: x[1], reverse=True)
        ][:_sel_max_ext + 5]  # +5 buffer for replacement/overflow candidates
    else:
        # Use existing ranking logic
        _sel_max_fb = _get_max_positions_for_regime(settings, regime or 'SPOT_BULL')
        if any(v != 50.0 for v in technical_scores.values()):
            top_signals = rank_with_technical(external_signals, news_scores, technical_scores, _sel_max_fb + 5, regime=regime)
        else:
            top_signals = rank_buy_signals(external_signals, news_scores, _sel_max_fb + 5)
    
    return top_signals


def _evaluate_tp_momentum(client: BinanceClient, symbol: str, current_price: float,
                           pnl_pct: float, settings, position_size_usdt: float = 0.0) -> Dict:
    """
    Analyse current momentum at Take-Profit trigger to decide whether to extend,
    scale-out, or close the position.

    Returns a dict:
        {
          "decision": "extend" | "scale_out" | "close",
          "momentum_score": int (0-4),
          "vol_spike": float,
          "rsi": float,
          "trend_4h": float,
          "success_prob": float,
        }
    Fallback on any error: {"decision": "scale_out", "momentum_score": -1, ...}
    """
    result = {
        "decision": "scale_out",
        "momentum_score": -1,
        "vol_spike": 0.0,
        "rsi": 50.0,
        "trend_4h": 0.0,
        "success_prob": 0.0,
    }
    try:
        from autonomous_signals import AutonomousSignalGenerator
        sig_gen = AutonomousSignalGenerator(client)
        sig = sig_gen.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
        ind = sig.get("indicators", {})

        vol_spike   = float(ind.get("vol_spike") or ind.get("volume_ratio") or 1.0)
        rsi         = float(ind.get("rsi") or 50.0)
        trend_4h    = float(ind.get("trend_4h") or 0.0)

        # Run predictor with current indicators — use real position size for accurate probability
        _max_trade = float(getattr(settings, 'max_per_trade_usdt', 500.0))
        position_size = min(position_size_usdt, _max_trade) if position_size_usdt > 0 else _max_trade
        prediction, _ = pnl_service.predict_for_symbol(
            symbol=symbol, client=client, settings=settings,
            position_size_usdt=position_size, trade_type='spot'
        )
        success_prob = float((prediction or {}).get("success_probability", 0.0))

        result.update({
            "vol_spike": round(vol_spike, 3),
            "rsi": round(rsi, 2),
            "trend_4h": round(trend_4h, 2),
            "success_prob": round(success_prob, 4),
        })

        # ── F2: Reversal guard — if price already dropped >2% from recent peak,
        # the move is in reversal and we should NOT extend, regardless of score.
        _reversal_detected = False
        try:
            _k15 = client.client.get_klines(symbol=symbol, interval='15m', limit=8)
            if _k15:
                _recent_high_15m = max(float(k[2]) for k in _k15)
                _pct_from_high = (current_price - _recent_high_15m) / _recent_high_15m
                if _pct_from_high < -0.02:
                    _reversal_detected = True
                    print(f"[SmartTP] {symbol}: reversal detected — price {current_price:.4f} is "
                          f"{_pct_from_high*100:.1f}% below recent 2h peak {_recent_high_15m:.4f} → forcing CLOSE")
                result["recent_high_15m"] = round(_recent_high_15m, 8)
                result["pct_from_high"] = round(_pct_from_high * 100, 2)
        except Exception:
            pass

        if _reversal_detected:
            result["decision"] = "close_reversal"
            result["momentum_score"] = 0
            return result

        # ── Score: 1 point per bullish condition ───────────────────────
        score = 0
        if vol_spike >= 1.5:
            score += 1
        if trend_4h >= 1.0:
            score += 1
        if 45.0 <= rsi <= 65.0:
            score += 1
        if success_prob >= 0.60:
            score += 1

        result["momentum_score"] = score

        _extend_threshold = int(getattr(settings, 'smart_tp_extend_score_threshold', 2))
        if score >= _extend_threshold:
            result["decision"] = "extend"
        elif score >= 1:
            result["decision"] = "scale_out"
        else:
            result["decision"] = "close"

    except Exception as _e:
        print(f"[SmartTP] momentum evaluation failed for {symbol}: {_e} — falling back to scale_out")

    return result


def _calc_atr_sl_pct(client: BinanceClient, symbol: str, settings, category_sl_pct: float) -> float:
    """
    M1: ATR-Based dynamic SL. Returns SL% = max(category_sl, atr_mult × ATR14_pct).
    Capped at atr_sl_max_pct to prevent monster stops.
    Falls back to category_sl if data unavailable.
    """
    if not getattr(settings, 'atr_sl_enabled', True):
        return category_sl_pct
    try:
        _kl = client.client.get_klines(symbol=symbol, interval='1h', limit=16)
        if not _kl or len(_kl) < 15:
            return category_sl_pct
        _highs = [float(k[2]) for k in _kl]
        _lows  = [float(k[3]) for k in _kl]
        _cls   = [float(k[4]) for k in _kl]
        # ATR14 = average of True Range over last 14 periods
        _trs = []
        for _i in range(1, 15):
            _tr = max(_highs[_i] - _lows[_i],
                      abs(_highs[_i] - _cls[_i-1]),
                      abs(_lows[_i]  - _cls[_i-1]))
            _trs.append(_tr)
        _atr = sum(_trs) / len(_trs)
        _price = _cls[-1]
        _atr_pct = _atr / _price if _price > 0 else 0.0
        _mult  = _get_param(settings, 'atr_sl_multiplier')
        _max   = _get_param(settings, 'atr_sl_max_pct')
        _min   = _get_param(settings, 'atr_sl_min_pct')
        _atr_sl = max(_min, min(_max, _atr_pct * _mult))
        _final  = max(category_sl_pct, _atr_sl)
        return _final
    except Exception:
        return category_sl_pct


_spot_atr_cache: Dict[str, tuple] = {}  # symbol → (atr_pct, timestamp)
_SPOT_ATR_CACHE_TTL = 300  # 5 minutes


def _compute_spot_trail_distance(client: BinanceClient, symbol: str, pnl_pct: float, settings, cat_trail_dist: float = 0.008) -> float:
    """Compute ATR-scaled trailing distance for SPOT positions.
    
    Returns trail distance as a decimal (e.g., 0.008 = 0.8%).
    Formula: trail_dist = max(floor, min(ceiling, ATR_5m * multiplier))
    I1: cat_trail_dist is the category-configured distance (e.g. NARRATIVE=0.03);
        used as floor so ATR never caps below what the config demands.
    """
    import time as _t
    _atr_enabled = getattr(settings, 'futures_trail_atr_enabled', True)
    # s78-fix: tighter trailing for RANGE_SCALP so a small profit isn't fully surrendered
    _floor = max(0.0025, cat_trail_dist)
    # I1: ceiling = max(1.5%, 2× category dist) — allows NARRATIVE to trail up to 6%
    _ceiling = max(0.015, cat_trail_dist * 2.0)

    if not _atr_enabled:
        # Fallback to category-based static distance (not hardcoded 0.8%)
        return cat_trail_dist

    # s78-fix: lower multipliers at low profit to trail closer
    if pnl_pct >= 0.08:
        mult = 0.8
    elif pnl_pct >= 0.05:
        mult = 1.0
    elif pnl_pct >= 0.03:
        mult = 1.2
    else:
        mult = 1.5

    # Get or compute ATR
    now = _t.time()
    cached = _spot_atr_cache.get(symbol)
    if cached and (now - cached[1]) < _SPOT_ATR_CACHE_TTL:
        atr_pct = cached[0]
    else:
        try:
            _klines = client.client.get_klines(symbol=symbol, interval='5m', limit=15)
            if _klines and len(_klines) >= 15:
                tr_values = []
                for i in range(1, len(_klines)):
                    h = float(_klines[i][2])
                    l = float(_klines[i][3])
                    prev_c = float(_klines[i - 1][4])
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    tr_values.append(tr)
                atr = sum(tr_values[-14:]) / 14
                price = float(_klines[-1][4])
                atr_pct = (atr / price) if price > 0 else 0.005
            else:
                atr_pct = 0.005  # fallback
        except Exception:
            atr_pct = 0.005
        _spot_atr_cache[symbol] = (atr_pct, now)

    trail_dist = atr_pct * mult
    trail_dist = max(_floor, min(_ceiling, trail_dist))
    return trail_dist


def _spot_net_pnl(pos, current_price: float, settings) -> tuple[float, float, float]:
    """Return (gross_pnl_usdt, net_pnl_usdt, net_pnl_pct) for a SPOT position after Binance fees."""
    entry_price = float(pos.entry_price or 0)
    qty = float(pos.quantity or 0)
    if entry_price <= 0 or qty <= 0:
        return 0.0, 0.0, 0.0
    gross_pnl_usdt = (current_price - entry_price) * qty
    entry_comm = float(pos.entry_commission or 0)
    if entry_comm <= 0:
        entry_comm = compute_commission(qty, entry_price, settings, 'spot')
    exit_comm = compute_commission(qty, current_price, settings, 'spot')
    net_pnl_usdt = gross_pnl_usdt - entry_comm - exit_comm
    net_pnl_pct = net_pnl_usdt / (entry_price * qty) if entry_price * qty > 0 else 0.0
    return gross_pnl_usdt, net_pnl_usdt, net_pnl_pct


def _spot_net_pnl_aggregate(pos_list, current_price: float, settings) -> tuple[float, float, float]:
    """Return (gross_pnl_usdt, net_pnl_usdt, net_pnl_pct) for an aggregated symbol position (escalation)."""
    total_cost = sum(float(p.entry_price or 0) * float(p.quantity or 0) for p in pos_list)
    total_qty = sum(float(p.quantity or 0) for p in pos_list)
    avg_entry = total_cost / total_qty if total_qty > 0 else 0.0
    if total_cost <= 0 or total_qty <= 0:
        return 0.0, 0.0, 0.0
    gross_pnl_usdt = (current_price - avg_entry) * total_qty
    entry_comm = sum(float(p.entry_commission or 0) for p in pos_list)
    if entry_comm <= 0:
        entry_comm = sum(compute_commission(float(p.quantity or 0), float(p.entry_price or 0), settings, 'spot') for p in pos_list)
    exit_comm = compute_commission(total_qty, current_price, settings, 'spot')
    net_pnl_usdt = gross_pnl_usdt - entry_comm - exit_comm
    net_pnl_pct = net_pnl_usdt / total_cost
    return gross_pnl_usdt, net_pnl_usdt, net_pnl_pct


def _check_sl_tp_positions(client: BinanceClient, settings, position_manager, news_client=None, regime: str = None, monitor_only: bool = False, sso7_candidate_cache: Optional[Dict] = None) -> List[Dict]:
    """
    Check all open positions against Stop-Loss, Take-Profit, and Trailing Stop rules.
    Also performs prediction-based exit when EV deteriorates (C1).
    Sells any position that has breached its exit threshold.
    Returns list of closed position dicts.
    sso7_candidate_cache: best non-held candidate from previous cycle scanner (for SSO-7 Opp Collapse).
    """
    global _SPOT_MASS_SL_COOLDOWN_UNTIL
    closed = []
    if not position_manager:
        return closed

    _spot_sl_count_this_cycle = [0]  # B3: mutable counter for mass-SL detection

    # Wolf Hunter Peak Capture Engine — evaluates whether to take profit or wait for larger peak
    _peak_capture_engine = PeakCaptureEngine(settings)

    # Portfolio total estimate for position-size-aware profit decisions
    _spot_total = 0.0
    try:
        _spot_total = float(client.get_account_balance().get("USDT", 0))
    except Exception:
        pass

    _global_sl_pct = _get_param(settings, 'stop_loss_pct')
    _global_tp_pct = _get_param(settings, 'take_profit_pct')
    trailing_enabled = _get_param(settings, 'trailing_stop_enabled')
    _global_trail_act = _get_param(settings, 'trailing_stop_activation_pct')
    _global_trail_dist = _get_param(settings, 'trailing_stop_distance_pct')
    commission_rate = _get_commission_config(settings).rate_for('spot')
    _token_categories = getattr(settings, 'token_categories', {})
    _cat_thresholds = getattr(settings, 'category_thresholds', {})

    try:
        all_positions = position_manager.get_all_positions()
    except Exception as e:
        print(f"SL/TP check: cannot get positions: {e}")
        return closed

    _spot_signal_cache: Dict[str, Dict[str, Any]] = {}
    _spot_signal_generator = None

    def _get_spot_signal_snapshot(_symbol: str) -> Dict[str, Any]:
        nonlocal _spot_signal_generator
        if _symbol in _spot_signal_cache:
            return _spot_signal_cache[_symbol]
        _ctx = {"strength": None, "direction": None, "timestamp": None}
        try:
            from autonomous_signals import AutonomousSignalGenerator
            if _spot_signal_generator is None:
                _spot_signal_generator = AutonomousSignalGenerator(client)
            _sig = _spot_signal_generator.analyze_symbol(_symbol, interval='1h', use_multi_timeframe=True)
            if isinstance(_sig, dict):
                _ctx["strength"] = float(_sig.get("strength", 0.0) or 0.0)
                _ctx["direction"] = str(_sig.get("signal") or "").strip().upper() or None
                _ctx["timestamp"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            pass
        _spot_signal_cache[_symbol] = _ctx
        return _ctx

    for symbol, pos_list in all_positions.items():
        # ── Per-category SL/TP/trailing thresholds ────────────────────────
        _cat_beh = _token_categories.get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
        _cat_th = _cat_thresholds.get(_cat_beh, {})
        sl_pct = _cat_th.get('stop_loss_pct', _global_sl_pct) / 100.0
        tp_pct = _cat_th.get('take_profit_pct', _global_tp_pct) / 100.0
        trail_activate = _cat_th.get('trailing_activation_pct', _global_trail_act) / 100.0
        trail_dist = _cat_th.get('trailing_distance_pct', _global_trail_dist) / 100.0
        # s70: RANGE_SCALP fast breakeven — activate at range_scalp_breakeven_pct (0.40%) not full trail_activate
        if regime == 'RANGE_SCALP' and hasattr(settings, 'range_scalp_breakeven_pct'):
            _rs_be = float(getattr(settings, 'range_scalp_breakeven_pct', 0.40))
            _rs_be = min(_rs_be, 0.15)
            if _rs_be > 0:
                trail_activate = min(trail_activate, _rs_be / 100.0)
        # M1: override sl_pct with ATR-based dynamic SL (prevents premature stops on volatile tokens)
        sl_pct = _calc_atr_sl_pct(client, symbol, settings, sl_pct)

        for pos in pos_list:
            try:
                current_price = _get_cycle_price(client, symbol)
                entry_price = float(pos.entry_price)
                if entry_price <= 0:
                    continue

                # ── Dust detection: position value below min_notional — auto-close ──
                try:
                    _dust_filters = get_symbol_filters(client, symbol)
                    _dust_notional = float(pos.quantity) * current_price
                    _dust_min = _dust_filters.get('min_notional', 1.0)
                    if 0 < _dust_notional < _dust_min:
                        _dust_msg = (f"[DUST] {symbol}: qty={pos.quantity} value=${_dust_notional:.4f} "
                                     f"< min_notional=${_dust_min:.2f} — auto-closing dust position")
                        print(_dust_msg)
                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING, _dust_msg)
                        if position_manager:
                            position_manager.close_all_positions_for_symbol(
                                symbol, current_price, 'dust_below_min_notional', commission_rate)
                        continue
                except Exception as _dust_err:
                    print(f"[DUST] {symbol}: dust check failed: {_dust_err}")

                # C0: gross PnL for price-based SL/TP triggers; net PnL for supervisors/Kelly/decisions
                pnl_pct_gross = (current_price - entry_price) / entry_price
                _pnl_gross_usdt, pnl_usdt_net, pnl_pct_net = _spot_net_pnl(pos, current_price, settings)
                pnl_pct = pnl_pct_gross  # keep gross for price triggers

                close_reason = None
                supervisor_decision = None
                supervisor_close_pct = 1.0
                supervisor_force_close = False
                metadata = getattr(pos, 'metadata', {}) or {}
                _ctx_signal_strength = None
                _ctx_signal_direction = None
                _ctx_signal_timestamp = None
                _ctx_pnl_velocity = None

                # Wolf Hunter: reconstruct real peak PnL from metadata + 1m candles
                _peak_pnl_pct_real = _peak_capture_engine.reconstruct_peak_pnl_pct(
                    client, symbol, pos, current_price, metadata
                )
                if metadata.get('peak_pnl_pct_real') != _peak_pnl_pct_real:
                    try:
                        position_manager.update_position_metadata(pos.position_id, metadata)
                    except Exception:
                        pass

                # Base signal/velocity context for supervisors (can be refined by SSO path below).
                try:
                    from smart_scale_out import compute_pnl_velocity as _compute_pnl_velocity
                    _v_snap = metadata.get('sso_pnl_snapshot')
                    _v_val, _v_new = _compute_pnl_velocity(pnl_pct, _v_snap)
                    _ctx_pnl_velocity = _v_val
                    if _v_new != _v_snap:
                        metadata['sso_pnl_snapshot'] = _v_new
                        try:
                            position_manager.update_position_metadata(pos.position_id, metadata)
                        except Exception:
                            pass
                except Exception:
                    pass
                _sig_ctx = _get_spot_signal_snapshot(symbol)
                _ctx_signal_strength = _sig_ctx.get("strength")
                _ctx_signal_direction = _sig_ctx.get("direction")
                _ctx_signal_timestamp = _sig_ctx.get("timestamp")
                if pnl_pct_net > 0:
                    _profit_started_dirty = _mark_profit_started(metadata, pnl_pct_net)
                    if _profit_started_dirty:
                        try:
                            position_manager.update_position_metadata(pos.position_id, metadata)
                        except Exception:
                            pass

                # ── C1: Re-evaluación predictiva (loss branch + Wolf Hunter profit branch) ──
                _peak_capture_advisory = None
                _pc_fng_value = 50.0
                try:
                    if news_client:
                        _pc_fng_data = news_client.get_fear_greed()
                        _pc_fng_value = float(_pc_fng_data.get('value', 50)) if isinstance(_pc_fng_data, dict) else 50.0
                except Exception:
                    pass

                if getattr(settings, 'prediction_exit_enabled', True):
                    try:
                        _min_hold_pred = int(getattr(settings, 'min_hold_for_prediction_exit', 90))
                        # s71 BUG-4 / s72 B1: per-regime override — each mode has its own hold floor
                        _pred_regime = str(getattr(settings, 'current_regime_mode', '') or '').upper()
                        if _pred_regime == 'RANGE_SCALP':
                            _min_hold_pred = int(getattr(settings, 'min_hold_for_prediction_exit_range_scalp', 20))
                        elif _pred_regime == 'FUTURES_SHORT':
                            _min_hold_pred = int(getattr(settings, 'min_hold_for_prediction_exit_futures_short', 30))
                        _entry_ts = getattr(pos, 'entry_time', None)
                        _age_ok = False
                        if _entry_ts:
                            if hasattr(_entry_ts, 'tzinfo') and _entry_ts.tzinfo is None:
                                _entry_ts = _entry_ts.replace(tzinfo=timezone.utc)
                            _age_min = (datetime.now(timezone.utc) - _entry_ts).total_seconds() / 60
                            _age_ok = _age_min >= _min_hold_pred
                        if _age_ok:
                            _pos_size = float(pos.quantity) * current_price
                            _pred_exit, _ind_exit = pnl_service.predict_for_symbol(
                                symbol, client, settings, _pos_size, trade_type='spot'
                            )
                            if _pred_exit:
                                _ev = float(_pred_exit.get('expected_pnl', 0))
                                _prob = float(_pred_exit.get('success_probability', 1.0))
                                _cat_beh_p = _token_categories.get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
                                _min_pnl_cat = _cat_thresholds.get(_cat_beh_p, {}).get('min_pnl', 0.0)
                                _min_prob_cat = _cat_thresholds.get(_cat_beh_p, {}).get('min_prob', 0.5)
                                _trend_exit = 'FLAT'
                                if _ind_exit:
                                    _trend_exit = str(_ind_exit.get('trend_4h', 'FLAT'))

                                # Persist prediction snapshot in metadata
                                metadata['last_pred_pnl'] = round(_ev, 4)
                                metadata['last_pred_prob'] = round(_prob, 4)
                                metadata['last_pred_time'] = datetime.now(timezone.utc).isoformat()
                                metadata['reevaluation_count'] = int(metadata.get('reevaluation_count', 0)) + 1

                                if pnl_pct_net < 0:
                                    # R2: FLAT allowed when EV is extreme (3x threshold) — catches stagnant bear positions
                                    _flat_extreme = (_trend_exit == 'FLAT'
                                                     and _min_pnl_cat < 0
                                                     and _ev < _min_pnl_cat * 3.0)
                                    _is_deteriorated = (_ev < _min_pnl_cat and _prob < _min_prob_cat
                                                        and (_trend_exit == 'DOWN' or _flat_extreme))
                                    if _is_deteriorated:
                                        metadata['prediction_exit_triggered'] = True
                                    if _is_deteriorated:
                                        print(f"[PredExit] {symbol}: EV={_ev:.2f} < min={_min_pnl_cat:.2f}, prob={_prob:.2f} < {_min_prob_cat:.2f}, trend={_trend_exit}, pnl_net={pnl_pct_net*100:.2f}% → CLOSING")
                                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                                     f"[PredExit] {symbol} prediction deteriorated: EV={_ev:.2f} trend={_trend_exit} pnl_net={pnl_pct_net*100:.2f}%")
                                        close_reason = f"prediction_exit_deteriorated (EV={_ev:.2f}, prob={_prob:.2f}, trend={_trend_exit})"
                                    else:
                                        print(f"[PredEval] {symbol}: EV={_ev:.2f} prob={_prob:.2f} trend={_trend_exit} pnl_net={pnl_pct_net*100:.2f}% → holding")
                                elif pnl_pct_net > 0 and getattr(settings, 'profit_predexit_enabled', True):
                                    # Wolf Hunter: decide whether to take profit or wait for larger peak
                                    _pc_indicators = _ind_exit or {}
                                    _pc_signal_strength = _ctx_signal_strength or 0.0
                                    _pc_signal_direction = _ctx_signal_direction or 'HOLD'

                                    # Inline momentum exhaustion for context
                                    _pc_momentum_exhausted = False
                                    _pc_rsi_raw = _pc_indicators.get('rsi')
                                    _pc_rsi = None
                                    if isinstance(_pc_rsi_raw, dict):
                                        _pc_rsi = _pc_rsi_raw.get('value')
                                    elif _pc_rsi_raw is not None:
                                        _pc_rsi = _pc_rsi_raw
                                    _pc_vol_spike = _pc_indicators.get('vol_spike') or _pc_indicators.get('volume_ratio') or 1.0
                                    if _pc_rsi is not None:
                                        _pc_rsi = float(_pc_rsi)
                                        if _pc_signal_direction == 'BUY' and _pc_rsi >= 70:
                                            _pc_momentum_exhausted = True
                                        elif _pc_signal_direction == 'SELL' and _pc_rsi <= 30:
                                            _pc_momentum_exhausted = True

                                    # Trend reversal detection
                                    _pc_trend_reversal = (
                                        _pc_signal_direction == 'BUY' and _trend_exit == 'DOWN'
                                    ) or (
                                        _pc_signal_direction == 'SELL' and _trend_exit == 'UP'
                                    )

                                    _pc_portfolio_total = max(
                                        _spot_total + _pos_size,
                                        _pos_size
                                    )
                                    _pc_ctx = PeakCaptureContext(
                                        symbol=symbol,
                                        trade_type='spot',
                                        side='BUY',
                                        position_size_usdt=_pos_size,
                                        portfolio_total_usdt=_pc_portfolio_total,
                                        current_price=current_price,
                                        entry_price=entry_price,
                                        current_pnl_usdt=pnl_usdt_net,
                                        current_pnl_pct=pnl_pct_net,
                                        peak_pnl_pct=_peak_pnl_pct_real,
                                        regime_mode=regime,
                                        profile_label=str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                        signal_strength=_pc_signal_strength,
                                        signal_direction=_pc_signal_direction,
                                        pnl_velocity_1h=_ctx_pnl_velocity,
                                        indicators=_pc_indicators,
                                        fear_greed_value=_pc_fng_value,
                                        metadata=metadata,
                                        leverage=1.0,
                                        prediction_1h=_pred_exit,
                                        prediction_4h=None,
                                        momentum_exhausted=_pc_momentum_exhausted,
                                        trend_reversal=_pc_trend_reversal,
                                        current_sl_price=float(pos.stop_loss) if pos.stop_loss else None,
                                        current_tp_price=float(pos.take_profit) if pos.take_profit else None,
                                    )
                                    _peak_capture_advisory = _peak_capture_engine.evaluate(_pc_ctx)

                                    # Audit every profit PredExit evaluation
                                    try:
                                        position_manager._audit(symbol, 'profit_predexit_decision', {
                                            'side': 'BUY',
                                            'should_close': bool(_peak_capture_advisory.should_close),
                                            'reason': _peak_capture_advisory.reason,
                                            'detail': _peak_capture_advisory.detail,
                                            'close_pct': float(_peak_capture_advisory.close_pct or 0.0),
                                            'current_pnl_usdt': round(pnl_usdt_net, 4),
                                            'expected_future_pnl': round(_peak_capture_advisory.expected_future_pnl, 4),
                                            'prob_higher_peak': round(_peak_capture_advisory.prob_higher_peak, 4),
                                            'capture_margin': round(_peak_capture_advisory.capture_margin, 4),
                                            'peak_pnl_pct_real': round(_peak_pnl_pct_real, 6),
                                            'pnl_velocity_1h': float(_ctx_pnl_velocity or 0.0),
                                            'fear_greed_value': float(_pc_fng_value),
                                            'regime_mode': regime,
                                            'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                            'metrics': _peak_capture_advisory.metrics,
                                        })
                                    except Exception:
                                        pass

                                    if _peak_capture_advisory.should_close:
                                        print(f"[PredExit/Profit] {symbol}: advisory={_peak_capture_advisory.reason} "
                                              f"close_pct={_peak_capture_advisory.close_pct*100:.0f}% "
                                              f"prob_higher={_peak_capture_advisory.prob_higher_peak:.2f}")
                                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                                     f"[PredExit/Profit] {symbol}: {_peak_capture_advisory.detail}")
                                try:
                                    position_manager.update_position_metadata(pos.position_id, metadata)
                                except Exception:
                                    pass
                    except Exception as _pe:
                        print(f"[PredEval] {symbol}: prediction check failed: {_pe}")

                # ── SSO-6 SPOT: Bounce rescue hook ──────────────────────────
                # Auto-tag SPOT positions stuck in a downtrend bounce + run rescue close
                _spot_bounce_rescue = getattr(settings, 'spot_bounce_rescue_enabled', True)
                _spot_meta = getattr(pos, 'metadata', {}) or {}
                _spot_bounce_entry_flag = _spot_meta.get('bounce_entry', False)
                if close_reason is None and _spot_bounce_rescue and not _spot_bounce_entry_flag:
                    try:
                        _sp_opened = getattr(pos, 'entry_time', None)
                        if _sp_opened:
                            if isinstance(_sp_opened, str):
                                _sp_opened = datetime.fromisoformat(_sp_opened.replace('Z', '+00:00'))
                            if _sp_opened.tzinfo is None:
                                _sp_opened = _sp_opened.replace(tzinfo=timezone.utc)
                            _sp_age_h = (datetime.now(timezone.utc) - _sp_opened).total_seconds() / 3600
                            _sp_max_peak = getattr(settings, 'spot_bounce_rescue_max_peak_pct', 0.8) / 100.0
                            _sp_max_peak = min(_sp_max_peak, 0.002)
                            _sp_min_h = getattr(settings, 'spot_bounce_rescue_min_hours', 6.0)
                            _sp_min_h = min(_sp_min_h, 2.0)
                            _sp_peak = float(_spot_meta.get('trailing_peak', 0) or 0)
                            _sp_peak_pct = (_sp_peak - entry_price) / entry_price if entry_price > 0 and _sp_peak > 0 else 0.0
                            if _sp_age_h >= _sp_min_h and _sp_peak_pct < _sp_max_peak:
                                _spot_meta['bounce_entry'] = True
                                _spot_bounce_entry_flag = True
                                try:
                                    position_manager.update_position_metadata(pos.position_id, _spot_meta)
                                except Exception:
                                    pass
                                print(f"[SSO-6/SPOT] {symbol}: TAGGED bounce_entry "
                                      f"(peak={_sp_peak_pct*100:.2f}%<{_sp_max_peak*100:.1f}%, age={_sp_age_h:.1f}h)")
                    except Exception:
                        pass
                # NOTE: SPOT SSO-6 rescue is now handled inside the SSO evaluate() block below
                # (SSOPositionData.bounce_entry=True + rescue_prediction_ev/signal fed in).

                # ── SSO: Smart Scale-Out (SPOT) ──────────────────────────────
                _sso_s_action = None  # always defined; advisory result passed to UG
                _sso_enabled_spot = getattr(settings, 'sso_enabled', True)
                # s78-fix: evaluate SSO also on negative PnL so loss-cut / rescue triggers can fire
                if close_reason is None and _sso_enabled_spot:
                    try:
                        from smart_scale_out import SmartScaleOut, SSOPositionData, compute_pnl_velocity, extract_sso_level_from_reason
                        metadata = getattr(pos, 'metadata', {}) or {}
                        # ── SSO-AGG: Aggregate PnL across ALL entries for this symbol ──
                        # When symbol has N entries (escalamiento), SSO-4 must see combined
                        # PnL so Target Contribution threshold fires correctly.
                        _sso_sym_all = pos_list  # all entries for this symbol (outer loop variable)
                        _sso_sym_cost = sum(float(p.entry_price) * float(p.quantity) for p in _sso_sym_all)
                        _sso_sym_qty  = sum(float(p.quantity) for p in _sso_sym_all)
                        _sso_sym_avg_entry = _sso_sym_cost / _sso_sym_qty if _sso_sym_qty > 0 else entry_price
                        _sso_sym_gross, _sso_sym_pnl_usdt, _sso_sym_pnl_pct = _spot_net_pnl_aggregate(_sso_sym_all, current_price, settings)
                        if len(_sso_sym_all) > 1:
                            print(f"[SSO/S] {symbol}: {len(_sso_sym_all)} entradas escaladas — "
                                  f"PnL combinado NETO=${_sso_sym_pnl_usdt:.2f} ({_sso_sym_pnl_pct*100:.2f}%) "
                                  f"vs individual=${(current_price - entry_price)*float(pos.quantity):.2f}")
                        _sso_spot_at_open = metadata.get('spot_total_at_open')
                        _sso_spot_live = float(client.get_account_balance().get("USDT", 0)) if client else None
                        _sso_spot_total = _sso_spot_at_open or _sso_spot_live
                        if _sso_spot_at_open and _sso_spot_live and abs(_sso_spot_live - _sso_spot_at_open) > 20:
                            print(f"[SSO/S] {symbol} using spot_total_at_open=${_sso_spot_at_open:.0f} (current=${_sso_spot_live:.0f}) — balance changed")
                        _sso_spot = SmartScaleOut(
                            settings,
                            spot_total=_sso_spot_total,
                        )
                        # Velocity tracking
                        _sso_s_snap = metadata.get('sso_pnl_snapshot')
                        _sso_s_vel, _sso_s_new_snap = compute_pnl_velocity(pnl_pct, _sso_s_snap)
                        _ctx_pnl_velocity = _sso_s_vel
                        if _sso_s_new_snap != _sso_s_snap:
                            metadata['sso_pnl_snapshot'] = _sso_s_new_snap
                            try:
                                position_manager.update_position_metadata(pos.position_id, metadata)
                            except Exception:
                                pass
                        # ATR 4h
                        _sso_s_atr = None
                        try:
                            _kl4h = client.client.get_klines(symbol=symbol, interval='4h', limit=15)
                            if _kl4h and len(_kl4h) >= 15:
                                _tr4h = []
                                for _i4 in range(1, len(_kl4h)):
                                    _h4 = float(_kl4h[_i4][2])
                                    _l4 = float(_kl4h[_i4][3])
                                    _pc4 = float(_kl4h[_i4-1][4])
                                    _tr4h.append(max(_h4 - _l4, abs(_h4 - _pc4), abs(_l4 - _pc4)))
                                _atr4 = sum(_tr4h[-14:]) / 14
                                _sso_s_atr = _atr4 / current_price if current_price > 0 else None
                        except Exception:
                            pass
                        # RSI / vol_spike from cached signal data
                        _sso_s_rsi = None
                        _sso_s_vol = None
                        _sso_s_sig = None
                        try:
                            from autonomous_signals import AutonomousSignalGenerator
                            _sso_s_sg = AutonomousSignalGenerator(client)
                            _sso_s_sig = _sso_s_sg.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
                            _sso_s_ind = _sso_s_sig.get("indicators", {}) if _sso_s_sig else {}
                            _sso_s_rsi = float(_sso_s_ind.get("rsi", 50))
                            _sso_s_vol = float(_sso_s_ind.get("vol_spike") or _sso_s_ind.get("volume_ratio") or 1.0)
                            if _sso_s_sig:
                                _ctx_signal_strength = float(_sso_s_sig.get('strength', 0.0) or 0.0)
                                _ctx_signal_direction = str(_sso_s_sig.get('signal') or '').strip().upper() or None
                                _ctx_signal_timestamp = datetime.now(timezone.utc).isoformat()
                        except Exception:
                            pass
                        # SSO-6 Smart Gate SPOT: compute rescue_prediction_ev + signal
                        _sp_rescue_pred_ev = None
                        _sp_rescue_signal = None
                        _sp_rescue_signal_str = 0.0
                        _sp_rescue_target = getattr(settings, 'spot_bounce_rescue_target_pnl', -0.50)
                        _sp_rescue_target = max(_sp_rescue_target, -0.10)
                        _sp_pnl_usdt = pnl_usdt_net  # C0: rescue target evaluated on net PnL
                        if _sso_s_sig:
                            _sp_rescue_signal = _sso_s_sig.get('signal')
                            _sp_rescue_signal_str = float(_sso_s_sig.get('strength', 0.0))
                        if _spot_bounce_entry_flag and _sp_pnl_usdt >= _sp_rescue_target:
                            try:
                                import pnl_service as _sp_pnl_svc
                                _sp_gate_pred, _ = _sp_pnl_svc.predict_for_symbol(
                                    symbol=symbol, client=client, settings=settings,
                                    position_size_usdt=round(_sso_sym_cost, 2),
                                    precomputed_signal=_sso_s_sig,
                                    trade_type='spot',
                                )
                                if _sp_gate_pred:
                                    _sp_rescue_pred_ev = float(_sp_gate_pred.get('expected_pnl', 0.0))
                            except Exception:
                                pass
                        # Build position data
                        _sso_s_opened = getattr(pos, 'entry_time', None)
                        if _sso_s_opened and isinstance(_sso_s_opened, str):
                            try:
                                _sso_s_opened = datetime.fromisoformat(_sso_s_opened.replace('Z', '+00:00'))
                            except Exception:
                                _sso_s_opened = None
                        if _sso_s_opened and _sso_s_opened.tzinfo is None:
                            _sso_s_opened = _sso_s_opened.replace(tzinfo=timezone.utc)
                        # Use Wolf Hunter reconstructed peak PnL for accurate SSO profit decay
                        _sso_s_peak_pnl_pct = float(_peak_pnl_pct_real)
                        # SSO-7: extract stagnation state from metadata
                        _sso7_cycles = int(metadata.get('sso7_stagnation_cycles', 0) or 0)
                        _sso7_last_adv = metadata.get('sso7_last_advance_pnl')
                        # SSO-7: best candidate from previous cycle (must not be this symbol)
                        _sso7_cand = sso7_candidate_cache or {}
                        _sso7_cand_sym = _sso7_cand.get('symbol', '')
                        _sso7_cand_str = float(_sso7_cand.get('strength', 0.0)) if _sso7_cand_sym != symbol else 0.0
                        _sso7_cand_prob = float(_sso7_cand.get('prob', 0.0)) if _sso7_cand_sym != symbol else 0.0
                        _sso7_cand_vol = float(_sso7_cand.get('vol_spike', 0.0)) if _sso7_cand_sym != symbol else 0.0
                        _sso7_cand_ev = float(_sso7_cand.get('ev', 0.0)) if _sso7_cand_sym != symbol else 0.0
                        _sso_s_pos = SSOPositionData(
                            symbol=symbol,
                            side="BUY",
                            pnl_pct=_sso_sym_pnl_pct,
                            pnl_usdt=_sso_sym_pnl_usdt,
                            position_size_usdt=_sso_sym_cost,
                            entry_price=_sso_sym_avg_entry,
                            current_price=current_price,
                            opened_at=_sso_s_opened,
                            peak_pnl_pct=_sso_s_peak_pnl_pct,
                            last_pnl_snapshot=_sso_s_snap,
                            pnl_velocity_1h=_sso_s_vel,
                            sso_levels_done=metadata.get('sso_levels_done', []),
                            rsi_1h=_sso_s_rsi,
                            vol_spike=_sso_s_vol,
                            atr_4h_pct=_sso_s_atr,
                            today_realized_pnl=_get_spot_daily_pnl(),  # P1.1: real from BD, thread-safe
                            daily_target=getattr(settings, 'sso_target_daily_usd', 40.0),
                            trade_type="spot",
                            regime_mode=regime,
                            profile_label=str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                            bounce_entry=_spot_bounce_entry_flag,
                            rescue_prediction_ev=_sp_rescue_pred_ev,
                            rescue_signal=_sp_rescue_signal,
                            rescue_signal_strength=_sp_rescue_signal_str,
                            _gate_block_count=metadata.get('bounce_gate_blocked_count') if _spot_bounce_entry_flag else None,
                            _gate_last_pnl_pct=metadata.get('bounce_gate_first_pnl_pct') if _spot_bounce_entry_flag else None,
                            current_sl_price=float(pos.stop_loss or 0) or None,
                            sso7_stagnation_cycles=_sso7_cycles,
                            sso7_last_advance_pnl=float(_sso7_last_adv) if _sso7_last_adv is not None else None,
                            best_candidate_strength=_sso7_cand_str,
                            best_candidate_prob=_sso7_cand_prob,
                            best_candidate_vol_spike=_sso7_cand_vol,
                            best_candidate_pred_ev=_sso7_cand_ev,
                            current_pred_ev=_sp_rescue_pred_ev,
                        )
                        _sso_s_action = _sso_spot.evaluate(_sso_s_pos)
                        # Full SSO visibility — always log evaluation result
                        if _sso_s_action.should_close:
                            print(f"[SSO/S] {symbol}: → CLOSE {_sso_s_action.close_pct*100:.0f}% "
                                  f"{_sso_s_action.reason} | {_sso_s_action.detail}")
                        else:
                            print(f"[SSO/S] {symbol}: NO ACTION | {_sso_s_action.diagnosis}")
                        try:
                            position_manager._audit(symbol, 'smart_scale_out_decision', {
                                'side': 'BUY',
                                'should_close': bool(_sso_s_action.should_close),
                                'action': _sso_s_action.action,
                                'reason': _sso_s_action.reason,
                                'detail': _sso_s_action.detail,
                                'diagnosis': _sso_s_action.diagnosis,
                                'trigger': _sso_s_action.trigger,
                                'close_pct': float(_sso_s_action.close_pct or 0.0),
                                'pnl_pct': float(_sso_sym_pnl_pct),
                                'pnl_usdt': float(_sp_pnl_usdt),
                                'peak_pnl_pct': float(_sso_s_peak_pnl_pct),
                                'pnl_velocity_1h': float(_sso_s_vel or 0.0),
                                'current_signal_strength': float(_sp_rescue_signal_str or 0.0),
                                'fear_greed_value': float(_ug_fng_value or 50.0),
                                'regime_mode': regime,
                                'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                'bounce_entry': bool(_spot_bounce_entry_flag),
                                'sso_levels_done': metadata.get('sso_levels_done', []),
                            })
                        except Exception:
                            pass
                        # SSO-6 Smart Gate SPOT: persist bounce_gate_* state to metadata + audit
                        if _spot_bounce_entry_flag:
                            _sp_diag = _sso_s_action.diagnosis or ""
                            _sp_gate_blocked_now = "GATE_BLOCKED" in _sp_diag
                            _sp_gate_expired_now = "gate_expired" in _sp_diag and _sso_s_action.should_close
                            _sp_now_iso = datetime.now(timezone.utc).isoformat()
                            _sp_meta_changed = False
                            if _sp_gate_blocked_now:
                                _sp_block_count = metadata.get('bounce_gate_blocked_count', 0) + 1
                                metadata['bounce_gate_blocked_count'] = _sp_block_count
                                metadata['bounce_gate_last_eval'] = _sp_now_iso
                                metadata['bounce_gate_blocked_reason'] = _sp_diag
                                metadata['bounce_gate_last_signal'] = _sp_rescue_signal
                                metadata['bounce_gate_last_signal_strength'] = _sp_rescue_signal_str
                                metadata['bounce_gate_last_ev'] = _sp_rescue_pred_ev
                                metadata['bounce_gate_last_velocity'] = _sso_s_vel
                                if 'bounce_gate_first_qualified_at' not in metadata and _sp_pnl_usdt >= _sp_rescue_target:
                                    metadata['bounce_gate_first_qualified_at'] = _sp_now_iso
                                if 'bounce_gate_first_pnl_pct' not in metadata:
                                    metadata['bounce_gate_first_pnl_pct'] = _sso_sym_pnl_pct
                                _sp_meta_changed = True
                                try:
                                    position_manager._audit(symbol, 'bounce_gate_blocked', {
                                        'side': 'BUY', 'pnl_usdt': round(_sp_pnl_usdt, 4),
                                        'gate_blocked_by': _sp_diag,
                                        'signal': _sp_rescue_signal, 'signal_strength': _sp_rescue_signal_str,
                                        'rescue_ev': _sp_rescue_pred_ev, 'velocity': _sso_s_vel,
                                        'block_count_so_far': _sp_block_count, 'and_partial': True,
                                    })
                                except Exception:
                                    pass
                                print(f"[SSO-6/Gate/SPOT] {symbol}: BLOCKED (#{_sp_block_count}) — {_sp_diag}")
                            elif not _sp_gate_blocked_now and metadata.get('bounce_gate_blocked_count', 0) > 0:
                                # Gate cleared without close — reset expiry counter
                                metadata['bounce_gate_blocked_count'] = 0
                                metadata.pop('bounce_gate_first_pnl_pct', None)
                                _sp_meta_changed = True
                            if _sp_gate_expired_now:
                                _sp_block_count = metadata.get('bounce_gate_blocked_count', 0)
                                try:
                                    position_manager._audit(symbol, 'bounce_gate_expired', {
                                        'side': 'BUY', 'pnl_usdt': round(_sp_pnl_usdt, 4),
                                        'total_blocks': _sp_block_count, 'reason': _sp_diag,
                                    })
                                except Exception:
                                    pass
                                print(f"[SSO-6/Gate/SPOT] {symbol}: EXPIRY — rescue forzado tras {_sp_block_count} bloqueos")
                            if _sso_s_action.should_close and 'sso_bounce_rescue' in (_sso_s_action.reason or ''):
                                _sp_block_count = metadata.get('bounce_gate_blocked_count', 0)
                                _sp_first_q = metadata.get('bounce_gate_first_qualified_at')
                                _sp_held_min = 0
                                if _sp_first_q:
                                    try:
                                        _sp_fq_dt = datetime.fromisoformat(_sp_first_q)
                                        _sp_held_min = int((datetime.now(timezone.utc) - _sp_fq_dt).total_seconds() / 60)
                                    except Exception:
                                        pass
                                try:
                                    position_manager._audit(symbol, 'bounce_gate_fired', {
                                        'side': 'BUY', 'pnl_usdt': round(_sp_pnl_usdt, 4),
                                        'total_blocks_before': _sp_block_count,
                                        'gate_held_minutes': _sp_held_min,
                                        'final_signal': _sp_rescue_signal, 'final_ev': _sp_rescue_pred_ev,
                                        'rescue_reason': 'sso_bounce_rescue',
                                        'expired': _sp_gate_expired_now,
                                    })
                                except Exception:
                                    pass
                            if _sp_meta_changed:
                                try:
                                    position_manager.update_position_metadata(pos.position_id, metadata)
                                except Exception:
                                    pass
                        # ── SSO-7: Persist stagnation state + execute SL update ──────────
                        _sso7_reason = _sso_s_action.reason or ""
                        _sso7_detail = _sso_s_action.detail or ""
                        _sso7_meta_changed = False
                        if _sso7_reason == "sso7_advance_reset":
                            # Position advancing — reset stagnation counter
                            if metadata.get('sso7_stagnation_cycles', 0) != 0:
                                metadata['sso7_stagnation_cycles'] = 0
                                metadata['sso7_last_advance_pnl'] = round(_sso_sym_pnl_usdt, 4)
                                _sso7_meta_changed = True
                        elif _sso7_reason in ("sso7_stagnation_count", "sso7_stagnation_trail",
                                              "sso7_opportunity_collapse"):
                            # Increment stagnation cycle counter
                            _sso7_new_cyc = _sso7_cycles + 1
                            metadata['sso7_stagnation_cycles'] = _sso7_new_cyc
                            _sso7_meta_changed = True
                            # SL update for trail or collapse
                            if "sso7_stagnation_trail:" in _sso7_detail or "sso7_opportunity_collapse:" in _sso7_detail:
                                try:
                                    _sso7_new_sl_str = _sso7_detail.split(":")[1].split()[0]
                                    _sso7_new_sl = float(_sso7_new_sl_str)
                                    _sso7_cur_sl = float(pos.stop_loss or 0)
                                    if _sso7_new_sl > _sso7_cur_sl and _sso7_new_sl > 0:
                                        position_manager.update_position_sl(pos.position_id, round(_sso7_new_sl, 8))
                                        print(f"[SSO-7] {symbol}: SL updated {_sso7_cur_sl:.6f}→{_sso7_new_sl:.6f} "
                                              f"({_sso7_reason}) cycles={_sso7_new_cyc}")
                                except Exception as _sso7_sl_err:
                                    print(f"[SSO-7] {symbol}: SL update failed: {_sso7_sl_err}")
                        if _sso7_meta_changed:
                            try:
                                position_manager.update_position_metadata(pos.position_id, metadata)
                            except Exception:
                                pass

                        if _sso_s_action.should_close:
                            if _sso_s_action.close_pct >= 1.0:
                                close_reason = f"{_sso_s_action.reason} ({pnl_pct*100:.2f}%)"
                                # s73 E1: trazabilidad SSO full-close en trades_audit
                                try:
                                    if _db_client_ref:
                                        _db_client_ref.execute_update(
                                            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                                            "VALUES (%s, %s, %s, %s, %s)",
                                            (
                                                symbol, 'spot', 'sso_close',
                                                __import__('json').dumps({
                                                    'trigger': _sso_s_action.trigger,
                                                    'reason': _sso_s_action.reason,
                                                    'close_pct': 1.0,
                                                    'pnl_pct': round(pnl_pct * 100, 4),
                                                    'pnl_usdt': round(_sso_sym_pnl_usdt, 4),
                                                    'diagnosis': (_sso_s_action.diagnosis or '')[:500],
                                                    'regime': regime,
                                                    'profile': str(getattr(settings, 'current_regime_profile_label', '') or '').upper(),
                                                    'ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                                                }),
                                                False,
                                            )
                                        )
                                except Exception:
                                    pass
                                else:
                                # Partial close for SPOT using real Binance balance
                                    try:
                                        _sso_s_filters = get_symbol_filters(client, symbol)
                                        _sso_s_full_qty = float(pos.quantity)
                                        # Target qty from SSO close_pct, then cap to actual Binance balance
                                        _sso_s_target_qty = align_quantity_to_step(
                                            _sso_s_full_qty * _sso_s_action.close_pct, _sso_s_filters["step"])
                                        _sso_s_sell_check = get_binance_sell_qty(
                                            client, symbol, _sso_s_target_qty, current_price)
                                        if _sso_s_sell_check.get('is_ghost'):
                                            print(f"[GHOST] SSO/SPOT {symbol}: DB qty={_sso_s_full_qty} but Binance balance={_sso_s_sell_check.get('balance_qty')} — closing DB record")
                                            if position_manager:
                                                position_manager.close_all_positions_for_symbol(
                                                    symbol, current_price, 'ghost_balance_mismatch', commission_rate)
                                            continue
                                        _sso_s_sell_qty = _sso_s_sell_check.get('qty_to_sell', 0.0)
                                        if _sso_s_sell_qty <= 0:
                                            print(f"[SSO/SPOT] {symbol}: computed sell qty is zero (balance={_sso_s_sell_check.get('balance_qty')}) — skipping partial close")
                                            continue
                                        _sso_s_valid, _sso_s_reason = is_quantity_valid(_sso_s_sell_qty, current_price, _sso_s_filters)
                                        if not _sso_s_valid:
                                            print(f"[SSO/SPOT] {symbol}: invalid sell qty {_sso_s_sell_qty}: {_sso_s_reason}")
                                            continue
                                        _sso_s_order = client.create_market_order(symbol=symbol, side="SELL", quantity=_sso_s_sell_qty)
                                        _sso_s_fill = client.verify_and_fetch_fills(_sso_s_order, f"SSO/SPOT SELL {symbol}")
                                        if not _sso_s_fill:
                                            print(f"[WARN] SSO/SPOT SELL {symbol}: order NOT confirmed filled — skipping")
                                            continue
                                        _sso_s_qty_sold = float(_sso_s_fill.get('qty', _sso_s_sell_qty))
                                        _sso_s_remaining = align_quantity_to_step(
                                            _sso_s_full_qty - _sso_s_qty_sold, _sso_s_filters["step"])
                                        # Dust guard: if remaining cannot be sold later, convert to full close
                                        if 0 < _sso_s_remaining * current_price < _sso_s_filters.get('min_notional', 1.0):
                                            print(f"[SSO/SPOT] {symbol}: remaining ${_sso_s_remaining * current_price:.4f} < min_notional — converting to full close")
                                            _sso_s_qty_sold = _sso_s_full_qty
                                            _sso_s_remaining = 0.0
                                            # Sell the balance we actually have, not DB qty
                                            _final_sell_check = get_binance_sell_qty(client, symbol, _sso_s_full_qty, current_price)
                                            _final_qty = _final_sell_check.get('qty_to_sell', _sso_s_qty_sold)
                                            if _final_qty > 0 and _final_qty != _sso_s_sell_qty:
                                                _sso_s_order2 = client.create_market_order(symbol=symbol, side="SELL", quantity=_final_qty)
                                                _sso_s_fill2 = client.verify_and_fetch_fills(_sso_s_order2, f"SSO/SPOT SELL2 {symbol}")
                                                if _sso_s_fill2:
                                                    _sso_s_qty_sold = float(_sso_s_fill2.get('qty', _final_qty))
                                        # Persist the partial close in DB via PositionManager with the EXACT filled qty
                                        _sso_s_closed = position_manager.close_position(
                                            symbol=symbol,
                                            exit_price=current_price,
                                            close_reason='sso_partial_close',
                                            commission_rate=commission_rate,
                                            position_id=pos.position_id,
                                            qty_closed=_sso_s_qty_sold,
                                            exit_context={
                                                'regime_mode': regime,
                                                'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').upper(),
                                                'exit_order_id': str(_sso_s_fill.get('order_id', '')),
                                                'exit_filled_qty': float(_sso_s_qty_sold),
                                                'exit_avg_price': float(_sso_s_fill.get('avg_price', current_price)),
                                                'exit_commission': float(_sso_s_fill.get('commission', 0.0)),
                                                'binance_exit_confirmed': True,
                                            },
                                        )
                                        if _sso_s_closed is None:
                                            print(f"[WARN] SSO/SPOT {symbol}: position_manager.close_position returned None — falling back to metadata-only update")
                                            try:
                                                position_manager.update_position_quantity(pos.position_id, _sso_s_remaining)
                                            except Exception:
                                                pass
                                        # Update SSO metadata (level tracking, last trigger) on the residual position
                                        metadata = getattr(pos, 'metadata', {}) or {}
                                        if not isinstance(metadata, dict):
                                            metadata = {}
                                        metadata['sso_last_trigger'] = _sso_s_action.trigger
                                        metadata['sso_last_reason'] = _sso_s_action.reason
                                        metadata['scale_out_done'] = True
                                        _sso_s_lvl = extract_sso_level_from_reason(_sso_s_action.reason)
                                        if _sso_s_lvl:
                                            _sso_s_done = metadata.get('sso_levels_done', [])
                                            if _sso_s_lvl not in _sso_s_done:
                                                _sso_s_done.append(_sso_s_lvl)
                                            metadata['sso_levels_done'] = _sso_s_done
                                        try:
                                            position_manager.update_position_metadata(pos.position_id, metadata)
                                            if _sso_s_remaining > 0:
                                                position_manager.update_position_sl(pos.position_id, round(entry_price, 8))
                                        except Exception:
                                            pass
                                        print(f"[SSO/SPOT] {symbol}: {_sso_s_action.trigger} "
                                              f"SCALE {_sso_s_action.close_pct*100:.0f}% "
                                              f"qty={_sso_s_qty_sold} | {_sso_s_action.detail}")
                                        closed.append({"symbol": symbol, "reason": _sso_s_action.reason,
                                                       "price": current_price, "order": _sso_s_order,
                                                       "sso_trigger": _sso_s_action.trigger})
                                        # s73 E1: trazabilidad SSO partial-close en trades_audit
                                        try:
                                            if _db_client_ref:
                                                _db_client_ref.execute_update(
                                                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                                                    "VALUES (%s, %s, %s, %s, %s)",
                                                    (
                                                        symbol, 'spot', 'sso_partial_close',
                                                        __import__('json').dumps({
                                                            'trigger': _sso_s_action.trigger,
                                                            'reason': _sso_s_action.reason,
                                                            'close_pct': round(_sso_s_action.close_pct, 4),
                                                            'qty_sold': float(_sso_s_qty_sold),
                                                            'qty_remaining': float(_sso_s_remaining),
                                                            'pnl_pct': round(pnl_pct * 100, 4),
                                                            'pnl_usdt': round(_sso_sym_pnl_usdt, 4),
                                                            'diagnosis': (_sso_s_action.diagnosis or '')[:500],
                                                            'regime': regime,
                                                            'profile': str(getattr(settings, 'current_regime_profile_label', '') or '').upper(),
                                                            'ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                                                        }),
                                                        False,
                                                    )
                                                )
                                        except Exception:
                                            pass
                                        continue  # partial done, move to next position
                                    except Exception as _sso_s_err:
                                        print(f"[SSO/SPOT] {symbol} partial close error: {_sso_s_err}")
                    except Exception as _sso_s_exc:
                        print(f"[SSO/SPOT] {symbol} evaluation error: {_sso_s_exc}")

                # ── Profit Realization Supervisor (advisory only — UG arbitrates) ──
                _spot_binance_validated = False
                try:
                    _vts = metadata.get('binance_validated_at')
                    _vqty = float(metadata.get('binance_validated_qty', 0.0) or 0.0)
                    if _vts:
                        _vdt = datetime.fromisoformat(str(_vts).replace('Z', '+00:00'))
                        if _vdt.tzinfo is None:
                            _vdt = _vdt.replace(tzinfo=timezone.utc)
                        _vage = (datetime.now(timezone.utc) - _vdt).total_seconds()
                        _spot_binance_validated = (_vage <= 60 * 60 and _vqty > 0)
                except Exception:
                    _spot_binance_validated = False

                # F&G value for exit modifier (F8) — shared by PRS/SSO/UG audits
                _ug_fng_value = None
                try:
                    if news_client:
                        _fng_data = news_client.get_fear_greed()
                        _ug_fng_value = float(_fng_data.get('value', 50)) if isinstance(_fng_data, dict) else 50.0
                except Exception:
                    pass

                _prs_advisory = None
                # s78-fix: evaluate PRS also on negative PnL so it can propose a loss cut
                if _spot_binance_validated:
                    _profit_started_at = _parse_profit_dt(metadata.get('profit_realization_started_at'))
                    # Use Wolf Hunter reconstructed peak price
                    _prs_peak_price = entry_price * (1.0 + _peak_pnl_pct_real)
                    _peak_gross_usdt, _peak_net_usdt, _peak_pnl_pct_net = _spot_net_pnl(pos, _prs_peak_price, settings)
                    _tp_ext_count = int(metadata.get('tp_extensions_count', 0) or 0)
                    _supervisor_ctx = ProfitRealizationContext(
                        symbol=symbol,
                        trade_type='spot',
                        side='LONG',
                        pnl_pct=pnl_pct_net,
                        pnl_usdt=pnl_usdt_net,
                        entry_price=entry_price,
                        current_price=current_price,
                        opened_at=getattr(pos, 'entry_time', None) or getattr(pos, 'created_at', None),
                        profit_started_at=_profit_started_at,
                        peak_pnl_pct=max(_peak_pnl_pct_net, pnl_pct_net),
                        pnl_velocity_1h=_ctx_pnl_velocity,
                        current_signal_strength=_ctx_signal_strength,
                        regime_mode=regime,
                        profile_label=str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                        breakeven_done=bool(trailing_enabled and pnl_pct_net >= trail_activate and float(pos.stop_loss or 0) >= entry_price),
                        trailing_active=bool(trailing_enabled and pnl_pct_net >= trail_activate and float(pos.stop_loss or 0) > 0),
                        tp_extensions=_tp_ext_count,
                        take_profit_price=float(getattr(pos, 'take_profit', 0) or 0) if getattr(pos, 'take_profit', 0) else None,
                        metadata=metadata,
                    )
                    _prs_advisory = _evaluate_profit_realization(_supervisor_ctx, settings)
                    if _prs_advisory.should_close:
                        print(f"[PRS/SPOT/advisory] {symbol}: {_prs_advisory.reason} close={_prs_advisory.close_pct*100:.0f}% → passing to UG")
                    try:
                        position_manager._audit(symbol, 'profit_realization_supervisor_decision', {
                            'side': 'BUY',
                            'should_close': bool(_prs_advisory.should_close),
                            'action': _prs_advisory.action,
                            'reason': _prs_advisory.reason,
                            'detail': _prs_advisory.detail,
                            'close_pct': float(_prs_advisory.close_pct or 0.0),
                            'pnl_pct': float(pnl_pct_net),
                            'peak_pnl_pct': float(max(_peak_pnl_pct_net, pnl_pct_net)),
                            'pnl_velocity_1h': float(_ctx_pnl_velocity or 0.0),
                            'current_signal_strength': float(_ctx_signal_strength or 0.0),
                            'fear_greed_value': float(_ug_fng_value or 50.0),
                            'regime_mode': regime,
                            'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                            'tp_extensions': _tp_ext_count,
                        })
                    except Exception:
                        pass

                # ── Unified Governance Supervisor (single arbiter: profit + loss) ─
                _soft_reason_pre_ug = close_reason
                if (close_reason is None or not str(close_reason).startswith(("stop_loss", "take_profit", "trailing_stop"))) and _spot_binance_validated:
                    try:
                        _sl_floor_pre = entry_price * (1.0 - sl_pct)
                        _sl_saved_pre = float(pos.stop_loss) if pos.stop_loss and float(pos.stop_loss) > 0 else _sl_floor_pre
                        _effective_sl_pre = max(_sl_floor_pre, _sl_saved_pre)
                        _ug_distance_to_sl = None
                        if current_price > 0 and _effective_sl_pre > 0:
                            _ug_distance_to_sl = ((current_price - _effective_sl_pre) / current_price) * 100.0

                        # C0: net peak PnL for UG context using Wolf Hunter reconstructed peak
                        _ug_peak_price = entry_price * (1.0 + _peak_pnl_pct_real)
                        _ug_peak_gross, _ug_peak_net, _ug_peak_pnl_pct_net = _spot_net_pnl(pos, _ug_peak_price, settings)
                        _ug_ctx = UnifiedGovernanceContext(
                            symbol=symbol,
                            trade_type='spot',
                            side='LONG',
                            pnl_pct=pnl_pct_net,
                            pnl_usdt=pnl_usdt_net,
                            entry_price=entry_price,
                            current_price=current_price,
                            opened_at=getattr(pos, 'entry_time', None) or getattr(pos, 'created_at', None),
                            metadata=metadata,
                            regime_mode=regime,
                            profile_label=str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                            distance_to_sl_pct=_ug_distance_to_sl,
                            pnl_velocity_1h=_ctx_pnl_velocity,
                            current_signal_strength=_ctx_signal_strength,
                            current_signal_direction=_ctx_signal_direction,
                            tp_extensions=int(metadata.get('tp_extensions_count', 0) or 0),
                            peak_pnl_pct=_ug_peak_pnl_pct_net,
                            profit_started_at=_parse_profit_dt(metadata.get('profit_realization_started_at')),
                            breakeven_done=bool(trailing_enabled and pnl_pct_net >= trail_activate and float(pos.stop_loss or 0) >= entry_price),
                            trailing_active=bool(trailing_enabled and pnl_pct_net >= trail_activate and float(pos.stop_loss or 0) > 0),
                            take_profit_price=float(getattr(pos, 'take_profit', 0) or 0) if getattr(pos, 'take_profit', 0) else None,
                            monitor_only=bool(monitor_only),
                            tier1_soft_reason=_soft_reason_pre_ug,
                            sso_advisory_should_close=bool(_sso_s_action and _sso_s_action.should_close and _sso_s_action.close_pct >= 1.0),
                            sso_advisory_close_pct=float(_sso_s_action.close_pct if _sso_s_action and _sso_s_action.should_close else 0.0),
                            sso_advisory_reason=str(_sso_s_action.reason if _sso_s_action and _sso_s_action.should_close else ''),
                            sso_advisory_trigger=str(_sso_s_action.trigger if _sso_s_action and _sso_s_action.should_close else ''),
                            prs_advisory_should_close=bool(_prs_advisory and _prs_advisory.should_close),
                            prs_advisory_close_pct=float(_prs_advisory.close_pct if _prs_advisory and _prs_advisory.should_close else 0.0),
                            prs_advisory_reason=str(_prs_advisory.reason if _prs_advisory and _prs_advisory.should_close else ''),
                            prs_advisory_detail=str(_prs_advisory.detail if _prs_advisory and _prs_advisory.should_close else ''),
                            peak_capture_should_close=bool(_peak_capture_advisory and _peak_capture_advisory.should_close),
                            peak_capture_close_pct=float(_peak_capture_advisory.close_pct if _peak_capture_advisory and _peak_capture_advisory.should_close else 0.0),
                            peak_capture_reason=str(_peak_capture_advisory.reason if _peak_capture_advisory and _peak_capture_advisory.should_close else ''),
                            peak_capture_detail=str(_peak_capture_advisory.detail if _peak_capture_advisory and _peak_capture_advisory.should_close else ''),
                            peak_capture_prob_higher_peak=float(_peak_capture_advisory.prob_higher_peak if _peak_capture_advisory else 0.0),
                            peak_capture_expected_future_pnl=float(_peak_capture_advisory.expected_future_pnl if _peak_capture_advisory else 0.0),
                            fear_greed_value=_ug_fng_value,
                        )
                        _ug_decision = _evaluate_unified_governance(_ug_ctx, settings)
                        if _ug_decision.state_patch:
                            metadata.update(_ug_decision.state_patch)
                            try:
                                position_manager.update_position_metadata(pos.position_id, metadata)
                            except Exception:
                                pass
                        try:
                            position_manager._audit(symbol, 'unified_governance_decision', {
                                'side': 'BUY',
                                'action': _ug_decision.action,
                                'reason': _ug_decision.reason,
                                'detail': _ug_decision.detail,
                                'priority': _ug_decision.priority,
                                'diagnostics': _ug_decision.diagnostics,
                                'close_pct': float(_ug_decision.close_pct or 0.0),
                                'pnl_pct': float(pnl_pct_net),
                                'pnl_usdt': float(pnl_usdt_net),
                                'peak_pnl_pct': float(_ug_peak_pnl_pct_net),
                                'pnl_velocity_1h': float(_ctx_pnl_velocity or 0.0),
                                'current_signal_strength': float(_ctx_signal_strength or 0.0),
                                'fear_greed_value': float(_ug_fng_value or 50.0),
                                'regime_mode': regime,
                                'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                'sso_advisory_should_close': bool(_sso_s_action and _sso_s_action.should_close and _sso_s_action.close_pct >= 1.0),
                                'prs_advisory_should_close': bool(_prs_advisory and _prs_advisory.should_close),
                                'prs_advisory_close_pct': float(_prs_advisory.close_pct if _prs_advisory and _prs_advisory.should_close else 0.0),
                                'prs_advisory_reason': str(_prs_advisory.reason if _prs_advisory and _prs_advisory.should_close else ''),
                            })
                        except Exception:
                            pass

                        # Unified authority among soft engines: can veto local soft closes.
                        if _soft_reason_pre_ug and (not _ug_decision.should_close) and _ug_decision.action in ("hold", "watchdog"):
                            close_reason = None

                        if _ug_decision.should_close:
                            close_reason = f"{_ug_decision.reason} ({pnl_pct*100:.2f}%)"
                            supervisor_decision = _ug_decision
                            supervisor_close_pct = float(_ug_decision.close_pct or 1.0)
                            supervisor_force_close = supervisor_close_pct >= 1.0
                            try:
                                position_manager._audit(symbol, 'unified_governance_close_triggered', {
                                    'side': 'BUY',
                                    'action': _ug_decision.action,
                                    'reason': _ug_decision.reason,
                                    'detail': _ug_decision.detail,
                                    'priority': _ug_decision.priority,
                                    'diagnostics': _ug_decision.diagnostics,
                                    'close_pct': supervisor_close_pct,
                                    'pnl_pct': float(pnl_pct_net),
                                    'pnl_usdt': float(pnl_usdt_net),
                                    'peak_pnl_pct': float(_ug_peak_pnl_pct_net),
                                    'pnl_velocity_1h': float(_ctx_pnl_velocity or 0.0),
                                    'current_signal_strength': float(_ctx_signal_strength or 0.0),
                                    'fear_greed_value': float(_ug_fng_value or 50.0),
                                    'regime_mode': regime,
                                    'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                    'prs_advisory_should_close': bool(_prs_advisory and _prs_advisory.should_close),
                                    'prs_advisory_close_pct': float(_prs_advisory.close_pct if _prs_advisory and _prs_advisory.should_close else 0.0),
                                    'prs_advisory_reason': str(_prs_advisory.reason if _prs_advisory and _prs_advisory.should_close else ''),
                                })
                            except Exception:
                                pass
                    except Exception as _ug_err:
                        print(f"[UnifiedGov/SPOT] {symbol}: evaluation error: {_ug_err}")
                elif not _spot_binance_validated:
                    try:
                        position_manager._audit(symbol, 'soft_supervisor_skip_unconfirmed', {
                            'side': 'BUY',
                            'reason': 'spot_not_recently_validated_against_binance',
                            'has_validation_ts': bool(metadata.get('binance_validated_at')),
                        }, binance_confirmed=False)
                    except Exception:
                        pass

                # ── Stop-Loss ──────────────────────────────────────────────
                # Fix 1: use effective SL = max(category floor, saved pos.stop_loss)
                # Prevents SL from retreating when SmartTP EXTEND or Scale-Out moved it forward
                _sl_floor = entry_price * (1.0 - sl_pct)
                _sl_saved = float(pos.stop_loss) if pos.stop_loss and float(pos.stop_loss) > 0 else _sl_floor
                _effective_sl_price = max(_sl_floor, _sl_saved)
                if close_reason is None and current_price <= _effective_sl_price:
                    close_reason = f"stop_loss ({pnl_pct*100:.2f}%, sl={_effective_sl_price:.6f})"
                    _spot_sl_count_this_cycle[0] += 1  # B3: track mass-SL events this cycle

                # ── Take-Profit ────────────────────────────────────────────
                if close_reason is None and pnl_pct >= tp_pct:
                    close_reason = f"take_profit ({pnl_pct*100:.2f}%)"

                if close_reason and supervisor_close_pct < 1.0 and any(
                        close_reason.startswith(p) for p in (
                            "profit_realization_supervisor",
                            "prs_partial",
                            "unified_governance",
                        )
                ):
                    try:
                        filters = get_symbol_filters(client, symbol)
                        full_qty = float(pos.quantity)
                        _prs_target_qty = align_quantity_to_step(full_qty * supervisor_close_pct, filters["step"])

                        # ── Use real Binance balance as single source of truth for qty
                        _prs_sell_check = get_binance_sell_qty(client, symbol, _prs_target_qty, current_price)
                        if _prs_sell_check.get('is_ghost'):
                            print(f"[GHOST] ProfitRealization/SPOT {symbol}: DB qty={full_qty} but Binance balance={_prs_sell_check.get('balance_qty')} — closing DB record")
                            if position_manager:
                                position_manager.close_all_positions_for_symbol(
                                    symbol, current_price, 'ghost_balance_mismatch', commission_rate)
                            continue
                        if _prs_sell_check.get('is_dust'):
                            print(f"[DUST] ProfitRealization/SPOT {symbol}: balance notional too low — skipping partial close")
                            close_reason = None
                            continue
                        sell_qty = _prs_sell_check.get('qty_to_sell', 0.0)
                        if sell_qty <= 0:
                            print(f"[ProfitRealization/SPOT] {symbol}: computed sell qty is zero — skipping partial close")
                            close_reason = None
                            continue
                        if _prs_sell_check.get('reason') == 'balance_capped':
                            print(f"[ProfitRealization/SPOT] {symbol}: capping sell qty to Binance balance {_prs_sell_check.get('balance_qty')} (DB had {full_qty})")

                        valid, reason = is_quantity_valid(sell_qty, current_price, filters)
                        if not valid:
                            print(f"[ProfitRealization/SPOT] {symbol}: invalid partial qty {sell_qty} — {reason}")
                            close_reason = None
                            continue

                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=sell_qty)
                        _prs_fill = client.verify_and_fetch_fills(order, f"ProfitRealization SELL {symbol}")
                        if not _prs_fill:
                            print(f"[WARN] ProfitRealization SELL {symbol}: order NOT confirmed filled — skipping")
                            close_reason = None
                            continue
                        _prs_qty_sold = float(_prs_fill.get('qty', sell_qty))

                        remaining_qty = align_quantity_to_step(full_qty - _prs_qty_sold, filters["step"])
                        # Dust guard: if remaining < min_notional, treat as full close
                        if 0 < remaining_qty * current_price < filters.get('min_notional', 1.0):
                            print(f"[ProfitRealization] {symbol}: remaining ${remaining_qty * current_price:.4f} < min_notional — converting to full close")
                            _prs_final_check = get_binance_sell_qty(client, symbol, full_qty, current_price)
                            _prs_final_qty = _prs_final_check.get('qty_to_sell', remaining_qty)
                            if _prs_final_qty > 0 and _prs_final_qty != _prs_qty_sold:
                                _prs_order2 = client.create_market_order(symbol=symbol, side="SELL", quantity=_prs_final_qty)
                                _prs_fill2 = client.verify_and_fetch_fills(_prs_order2, f"ProfitRealization SELL2 {symbol}")
                                if _prs_fill2:
                                    _prs_qty_sold = float(_prs_fill2.get('qty', _prs_final_qty))
                            remaining_qty = 0.0

                        metadata['profit_realization_last_triggered_at'] = datetime.now(timezone.utc).isoformat()
                        metadata['profit_realization_partial_qty'] = _prs_qty_sold
                        metadata['profit_realization_partial_reason'] = supervisor_decision.reason if supervisor_decision else 'profit_realization_supervisor_partial'
                        _exit_ctx = {
                            'regime_mode': regime,
                            'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                            'exit_order_id': str(_prs_fill.get('order_id', '')),
                            'exit_filled_qty': float(_prs_qty_sold),
                            'exit_avg_price': float(_prs_fill.get('avg_price', current_price)),
                            'exit_commission': float(_prs_fill.get('commission', 0.0)),
                            'binance_exit_confirmed': True,
                        }
                        try:
                            position_manager.update_position_metadata(pos.position_id, metadata)
                            position_manager.close_position(
                                symbol=symbol,
                                exit_price=current_price,
                                close_reason=metadata['profit_realization_partial_reason'],
                                commission_rate=commission_rate,
                                position_id=pos.position_id,
                                qty_closed=_prs_qty_sold,
                                exit_context=_exit_ctx,
                            )
                            position_manager._audit(symbol, 'profit_realization_supervisor_partial', {
                                'side': 'BUY',
                                'qty_closed': _prs_qty_sold,
                                'qty_remaining': remaining_qty,
                                'reason': metadata['profit_realization_partial_reason'],
                                'detail': supervisor_decision.detail if supervisor_decision else '',
                                'diagnostics': supervisor_decision.diagnostics if supervisor_decision else '',
                            })
                        except Exception:
                            pass
                        print(f"[ProfitRealization/SPOT] {symbol}: partial close {supervisor_close_pct*100:.0f}% qty={_prs_qty_sold} reason={close_reason}")
                        add_signal({
                            "symbol": symbol,
                            "action": "SELL",
                            "price": current_price,
                            "reason": metadata['profit_realization_partial_reason'],
                            "pnl_pct": round(pnl_pct * 100, 4),
                            "source": "profit_realization_supervisor",
                        })
                        closed.append({"symbol": symbol, "reason": metadata['profit_realization_partial_reason'], "price": current_price, "order": order, "partial": True, "pnl_pct": round(pnl_pct * 100, 4)})
                        continue
                    except Exception as _pr_err:
                        print(f"[ProfitRealization/SPOT] partial close failed for {symbol}: {_pr_err}")
                        close_reason = None
                        continue

                # ── Breakeven SL (SPOT) ───────────────────────────────────
                # Move SL to entry price once position reaches trailing_stop_activation_pct profit
                # H1: use pnl_pct_net (net of commissions) for consistency with PRS/UG
                # Prevents any trailing-active position from going negative
                if close_reason is None and trailing_enabled and pnl_pct_net >= trail_activate:
                    _cur_sl_val = float(pos.stop_loss or 0)
                    if _cur_sl_val < entry_price:
                        try:
                            position_manager.update_position_sl(pos.position_id, round(entry_price, 8))
                            print(f"[Breakeven] {symbol}: SL → entry={entry_price:.6f} (PnL net={pnl_pct_net*100:.2f}%)")
                            # H3: trazabilidad en BD — registrar activación de breakeven
                            try:
                                if _db_client_ref:
                                    import json as _json_be
                                    _db_client_ref.execute_update(
                                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                                        (
                                            symbol,
                                            'spot',
                                            'breakeven_activated',
                                            _json_be.dumps({
                                                'position_id': pos.position_id,
                                                'entry_price': entry_price,
                                                'current_price': current_price,
                                                'pnl_pct_net': round(pnl_pct_net * 100, 4),
                                                'trail_activate_pct': round(trail_activate * 100, 4),
                                                'regime': regime,
                                                'ts': datetime.now(timezone.utc).isoformat(),
                                            }),
                                            False,
                                        )
                                    )
                            except Exception:
                                pass
                        except Exception:
                            pass

                # ── Trailing Stop ──────────────────────────────────────────
                # H1: use pnl_pct_net for trailing activation (consistent with breakeven and PRS/UG)
                if close_reason is None and trailing_enabled and pnl_pct_net >= trail_activate:
                    # ATR-scaled trail: respects per-symbol volatility to avoid wick stop-outs
                    # I1: pass category trail_dist as floor; I2: pass pnl_pct_net (not gross)
                    trail_dist = _compute_spot_trail_distance(client, symbol, pnl_pct_net, settings, trail_dist)
                    # Peak is stored in position metadata; update if new high
                    metadata = getattr(pos, 'metadata', {}) or {}
                    peak_price = float(metadata.get('trailing_peak', entry_price))
                    # F1: Reconstruct real peak from last 30 1m candles to capture
                    # intrabar highs that occur between 20-min cycles.
                    try:
                        _klines_1m = client.client.get_klines(symbol=symbol, interval='1m', limit=30)
                        if _klines_1m:
                            _candle_peak = max(float(k[2]) for k in _klines_1m)
                            if _candle_peak > peak_price:
                                print(f"[Trail/F1] {symbol}: candle high {_candle_peak:.6f} > stored peak {peak_price:.6f} → updating")
                                peak_price = _candle_peak
                    except Exception:
                        pass
                    if current_price > peak_price:
                        peak_price = current_price
                    if peak_price > float(metadata.get('trailing_peak', entry_price)):
                        metadata['trailing_peak'] = peak_price
                        try:
                            position_manager.update_position_metadata(pos.position_id, metadata)
                        except Exception:
                            pass
                    trail_stop_price = peak_price * (1.0 - trail_dist)
                    # FIX-H2: Persist trailing SL to PositionManager so it survives restarts
                    try:
                        _cur_pos_sl = float(pos.stop_loss or 0)
                        if trail_stop_price > _cur_pos_sl:
                            position_manager.update_position_sl(pos.position_id, round(trail_stop_price, 8))
                    except Exception:
                        pass
                    if current_price <= trail_stop_price:
                        close_reason = f"trailing_stop (peak={peak_price:.4f}, trail={trail_stop_price:.4f})"

                if close_reason:
                    # ── Anti-wick: confirm SL/TS with last closed candle ───────
                    _is_sl_trigger = close_reason.startswith("stop_loss")
                    _is_ts_trigger = close_reason.startswith("trailing_stop")
                    if (_is_sl_trigger or _is_ts_trigger) and not close_reason.startswith("profit_realization_supervisor"):
                        _confirm_n = getattr(settings, 'sl_confirmation_candles', 1)
                        if _confirm_n > 0:
                            try:
                                _klines = client.client.get_klines(symbol=symbol, interval='1m', limit=2)
                                _last_close = float(_klines[-2][4]) if len(_klines) >= 2 else current_price
                                _wick_threshold = trail_stop_price if _is_ts_trigger else _effective_sl_price
                                if _last_close > _wick_threshold:
                                    _trigger_label = "TS" if _is_ts_trigger else "SL"
                                    print(f"[AntiWick] {_trigger_label} skip {symbol}: spot={current_price:.6f} < threshold={_wick_threshold:.6f} "
                                          f"but last 1m close={_last_close:.6f} > threshold — wick detected, holding")
                                    continue
                            except Exception as _aw_err:
                                print(f"[AntiWick] klines fetch failed for {symbol}: {_aw_err} — proceeding with {close_reason[:2].upper()}")
                    try:
                        filters = get_symbol_filters(client, symbol)
                        full_qty = float(pos.quantity)
                        _scale_out = getattr(settings, 'scale_out_enabled', True)
                        _scale_pct = getattr(settings, 'scale_out_first_close_pct', 0.5)
                        _is_tp = close_reason.startswith("take_profit")
                        _smart_tp = getattr(settings, 'smart_tp_enabled', True)
                        # s70: per-regime SmartTP EXTEND flag — replaces smart_tp_extend_disabled_regimes list
                        _ext_regime_field = f'smart_tp_extend_enabled_{regime.lower()}' if regime else None
                        _smart_tp_extend_ok = (
                            bool(getattr(settings, _ext_regime_field, True))
                            if (_ext_regime_field and hasattr(settings, _ext_regime_field))
                            else regime not in getattr(settings, 'smart_tp_extend_disabled_regimes', ['RANGE_SCALP', 'PRESERVATION'])
                        )
                        if not _smart_tp_extend_ok:
                            _smart_tp = False
                            print(f"[SmartTP] {symbol}: EXTEND disabled in regime={regime} — will scale-out or close")

                        # ── Smart TP: evaluate momentum before acting on TP ───────────
                        if _is_tp and _smart_tp:
                            metadata = getattr(pos, 'metadata', {}) or {}
                            _ext_count = int(metadata.get('tp_extensions_count', 0))
                            _max_ext = int(getattr(settings, 'smart_tp_max_extensions', 3))
                            _max_pnl = float(getattr(settings, 'smart_tp_max_pnl_pct', 15.0)) / 100.0
                            _can_extend = (_ext_count < _max_ext) and (pnl_pct < _max_pnl)

                            _pos_size_usdt = float(pos.quantity) * current_price
                            _tp_eval = _evaluate_tp_momentum(client, symbol, current_price, pnl_pct, settings,
                                                              position_size_usdt=_pos_size_usdt)
                            _decision = _tp_eval["decision"]
                            print(f"[SmartTP] {symbol}: pnl={pnl_pct*100:.2f}% score={_tp_eval['momentum_score']}/4"
                                  f" vol={_tp_eval['vol_spike']}x rsi={_tp_eval['rsi']} trend4h={_tp_eval['trend_4h']}"
                                  f" prob={_tp_eval['success_prob']:.2f} → {_decision.upper()}"
                                  f" (extensions={_ext_count}/{_max_ext})")

                            # ── EXTEND: strong momentum → raise TP, SL → current TP ──
                            if _decision == "extend" and _can_extend:
                                _ext_pct = _cat_th.get('tp_extension_pct', 2.0) / 100.0
                                _new_tp = round(current_price * (1.0 + _ext_pct), 8)
                                # New SL = current TP price (locks in the gain reached)
                                _smart_sl_buffer = sl_pct * 0.40  # FIX-H1: 40% of category SL (not hardcoded 0.5%)
                                _new_sl = round(current_price * (1.0 - _smart_sl_buffer), 8)
                                # But if we already have a profitable SL, keep the higher one
                                _cur_sl = float(pos.stop_loss or entry_price)
                                _new_sl = max(_new_sl, _cur_sl)

                                # Record this decision in metadata for the LearningEngine
                                _tp_decisions = metadata.get('tp_decisions', [])
                                _tp_decisions.append({
                                    "tp_level": _ext_count + 1,
                                    "pnl_pct_at_decision": round(pnl_pct * 100, 4),
                                    "decision": "extend",
                                    "momentum_score": _tp_eval["momentum_score"],
                                    "vol_spike": _tp_eval["vol_spike"],
                                    "rsi": _tp_eval["rsi"],
                                    "trend_4h": _tp_eval["trend_4h"],
                                    "success_prob": _tp_eval["success_prob"],
                                    "old_tp": round(current_price, 8),
                                    "new_tp": _new_tp,
                                    "new_sl": _new_sl,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                                metadata['tp_decisions'] = _tp_decisions
                                metadata['tp_extensions_count'] = _ext_count + 1
                                metadata['trailing_peak'] = current_price
                                try:
                                    position_manager.update_position_tp(pos.position_id, _new_tp)
                                    position_manager.update_position_sl(pos.position_id, _new_sl)
                                    position_manager.update_position_metadata(pos.position_id, metadata)
                                except Exception:
                                    pass
                                ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                             f"[SmartTP] EXTEND {symbol}: TP {current_price:.4f}→{_new_tp:.4f} SL→{_new_sl:.4f} (ext {_ext_count+1}/{_max_ext})")
                                add_signal({"symbol": symbol, "action": "HOLD", "price": current_price,
                                            "reason": f"smart_tp_extended_{_ext_count+1} score={_tp_eval['momentum_score']}/4",
                                            "pnl_pct": round(pnl_pct * 100, 4), "source": "smart_tp"})
                                continue  # do NOT sell — let it run

                            # ── CLOSE / CLOSE_REVERSAL: sell everything immediately ──
                            elif _decision in ("close", "close_reversal"):
                                # Record the close decision in metadata before selling
                                _tp_decisions = metadata.get('tp_decisions', [])
                                _tp_decisions.append({
                                    "tp_level": _ext_count + 1,
                                    "pnl_pct_at_decision": round(pnl_pct * 100, 4),
                                    "decision": _decision,
                                    "momentum_score": _tp_eval["momentum_score"],
                                    "vol_spike": _tp_eval["vol_spike"],
                                    "rsi": _tp_eval["rsi"],
                                    "trend_4h": _tp_eval["trend_4h"],
                                    "success_prob": _tp_eval["success_prob"],
                                    "pct_from_high": _tp_eval.get("pct_from_high"),
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                                metadata['tp_decisions'] = _tp_decisions
                                try:
                                    position_manager.update_position_metadata(pos.position_id, metadata)
                                except Exception:
                                    pass
                                _cr_suffix = "reversal" if _decision == "close_reversal" else "close"
                                close_reason = f"smart_tp_{_cr_suffix} ({pnl_pct*100:.2f}%)"
                                # fall through to full-close below

                            # ── SCALE_OUT: neutral momentum → sell 50%, hold rest ───
                            else:
                                _decision = "scale_out"  # normalise (covers !_can_extend path too)
                                if _scale_out and full_qty * _scale_pct >= filters.get("min_qty", 0):
                                    sell_qty = align_quantity_to_step(full_qty * _scale_pct, filters["step"])
                                    valid, reason = is_quantity_valid(sell_qty, current_price, filters)
                                    if valid:
                                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=sell_qty)
                                        if not client.verify_order_filled(order, f"SCALE-OUT SELL {symbol}"):
                                            print(f"[WARN] SCALE-OUT SELL {symbol}: order NOT confirmed filled — skipping")
                                            continue
                                        remaining_qty = align_quantity_to_step(full_qty - sell_qty, filters["step"])
                                        # Dust guard: if remaining < min_notional, treat as full close
                                        if 0 < remaining_qty * current_price < filters.get('min_notional', 1.0):
                                            print(f"[SCALE-OUT] {symbol}: remaining ${remaining_qty * current_price:.4f} < min_notional — converting to full close")
                                            sell_qty = full_qty
                                            remaining_qty = 0.0
                                        print(f"SCALE-OUT {symbol}: sold {sell_qty} ({_scale_pct*100:.0f}%) @ {current_price:.4f}, remaining={remaining_qty}, SL→breakeven={entry_price:.4f}")
                                        _tp_decisions = metadata.get('tp_decisions', [])
                                        _tp_decisions.append({
                                            "tp_level": _ext_count + 1,
                                            "pnl_pct_at_decision": round(pnl_pct * 100, 4),
                                            "decision": "scale_out",
                                            "momentum_score": _tp_eval["momentum_score"],
                                            "vol_spike": _tp_eval["vol_spike"],
                                            "rsi": _tp_eval["rsi"],
                                            "trend_4h": _tp_eval["trend_4h"],
                                            "success_prob": _tp_eval["success_prob"],
                                            "timestamp": datetime.now(timezone.utc).isoformat(),
                                        })
                                        metadata['tp_decisions'] = _tp_decisions
                                        metadata['scale_out_done'] = True
                                        metadata['scale_out_qty_sold'] = sell_qty
                                        metadata['scale_out_avg_price'] = current_price
                                        metadata['trailing_peak'] = current_price
                                        try:
                                            position_manager.update_position_metadata(pos.position_id, metadata)
                                            position_manager.update_position_sl(pos.position_id, entry_price)
                                            position_manager.update_position_quantity(pos.position_id, remaining_qty)
                                        except Exception:
                                            pass
                                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                                     f"[SmartTP] SCALE-OUT {symbol}: {sell_qty} @ {current_price:.4f}")
                                        add_signal({"symbol": symbol, "action": "SELL", "price": current_price,
                                                    "reason": f"smart_tp_scale_out ({pnl_pct*100:.2f}%)",
                                                    "pnl_pct": round(pnl_pct * 100, 4), "source": "smart_tp"})
                                        closed.append({"symbol": symbol, "reason": "smart_tp_scale_out",
                                                       "price": current_price, "order": order,
                                                       "pnl_pct": round(pnl_pct * 100, 4)})
                                        continue
                        # ── Use real Binance balance as single source of truth for qty ─────
                        _sell_check = get_binance_sell_qty(client, symbol, full_qty, current_price)
                        if _sell_check.get('is_ghost'):
                            _ghost_msg = (
                                f"[GHOST] {symbol}: DB qty={full_qty:.0f} but Binance balance={_sell_check.get('balance_qty')} "
                                f"(ratio={_sell_check.get('ratio'):.6f}) — auto-closing ghost position"
                            )
                            print(_ghost_msg)
                            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_ERROR, _ghost_msg)
                            if position_manager:
                                try:
                                    position_manager.close_all_positions_for_symbol(
                                        symbol, current_price, 'ghost_balance_mismatch', commission_rate
                                    )
                                except Exception as _ghost_err:
                                    print(f"[GHOST] close failed for {symbol}: {_ghost_err}")
                            continue
                        if _sell_check.get('is_dust'):
                            _dust_msg = (
                                f"[DUST] {symbol}: balance notional ${_sell_check.get('balance_qty') * current_price:.4f} "
                                f"below min_notional={_sell_check.get('min_notional')} — skipping SL/TP sell"
                            )
                            print(_dust_msg)
                            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING, _dust_msg)
                            continue
                        qty = _sell_check.get('qty_to_sell', full_qty)
                        if qty <= 0:
                            print(f"[SL/TP] {symbol}: computed sell qty is zero (balance={_sell_check.get('balance_qty')}) — skipping")
                            continue
                        if _sell_check.get('reason') == 'balance_capped':
                            print(f"[SL/TP] {symbol}: capping sell qty to Binance balance {_sell_check.get('balance_qty')} (DB had {full_qty})")
                        valid, reason = is_quantity_valid(qty, current_price, filters)
                        if not valid:
                            print(f"SL/TP: cannot sell {symbol}: {reason}")
                            continue
                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
                        _sell_fill = client.verify_and_fetch_fills(order, f"SL/TP SELL {symbol}")
                        if not _sell_fill:
                            print(f"[WARN] SL/TP SELL {symbol}: order NOT confirmed filled — skipping close record")
                            continue
                        _sold_qty = _sell_fill.get('qty', qty)
                        _exit_commission = _sell_fill.get('commission', 0.0)
                        print(f"SL/TP EXIT {symbol}: {close_reason} @ {current_price:.4f} (sold={_sold_qty})")
                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                     f"SL/TP exit {symbol}: {close_reason} @ {current_price:.4f} sold={_sold_qty}")
                        position_manager.close_all_positions_for_symbol(
                            symbol, current_price, close_reason.split(" ")[0], commission_rate,
                            exit_context={
                                'regime_mode': regime,
                                'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                'exit_order_id': str(_sell_fill.get('order_id', '')),
                                'exit_filled_qty': float(_sold_qty),
                                'exit_avg_price': float(_sell_fill.get('avg_price', current_price)),
                                'exit_commission': float(_exit_commission),
                                'binance_exit_confirmed': True,
                            },
                            qty_closed=_sold_qty,
                        )
                        _overflow_positions.discard(symbol)  # free overflow slot if used
                        # V2: Tracking métricas VolSpike Override (jun 16 2026)
                        if symbol in _VOLSPIKE_POSITIONS:
                            _VOLSPIKE_POSITIONS.discard(symbol)
                            # Calcular y registrar performance
                            _pnl_actual = (current_price - entry_price) * float(pos.quantity)
                            _pnl_expected = float(pos.pnl_prediction.get('expected_pnl', 0)) if pos.pnl_prediction else 0
                            _accuracy = (_pnl_actual / _pnl_expected * 100) if _pnl_expected else 0
                            _duration_hours = 0
                            try:
                                _entry_dt = datetime.fromisoformat(pos.entry_time.replace('Z', '+00:00'))
                                _duration_hours = (datetime.now(timezone.utc) - _entry_dt).total_seconds() / 3600
                            except:
                                pass
                            _OVERRIDE_PERFORMANCE.append({
                                'symbol': symbol,
                                'expected_pnl': _pnl_expected,
                                'actual_pnl': _pnl_actual,
                                'accuracy_pct': _accuracy,
                                'close_reason': close_reason,
                                'duration_hours': _duration_hours,
                                'entry_time': pos.entry_time,
                                'exit_time': datetime.now(timezone.utc).isoformat(),
                            })
                            print(f"[VolSpike Metrics] {symbol}: Actual={_pnl_actual:.2f}, Expected={_pnl_expected:.2f}, Accuracy={_accuracy:.1f}%, Duration={_duration_hours:.1f}h")
                        add_signal({
                            "symbol": symbol, "action": "SELL",
                            "price": current_price,
                            "reason": close_reason,
                            "pnl_pct": round(pnl_pct * 100, 4),
                            "source": close_reason.split("_")[0],
                        })
                        closed.append({"symbol": symbol, "reason": close_reason,
                                       "price": current_price, "order": order,
                                       "pnl_usdt": round(pnl_usdt_net, 4),
                                       "pnl_pct": round(pnl_pct_net * 100, 4)})
                    except Exception as exc:
                        import traceback
                        _tb = traceback.format_exc()
                        print(f"SL/TP sell failed for {symbol}: {exc}\n{_tb}")
                        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR,
                                     f"SL/TP sell failed {symbol}: {exc}")
            except Exception as e:
                print(f"SL/TP check error for {symbol}: {e}")

    # B3: SPOT mass-SL cooldown — activate global pause if N SLs hit in this cycle
    _mass_thr = int(getattr(settings, 'spot_mass_sl_threshold', 3))
    if _spot_sl_count_this_cycle[0] >= _mass_thr:
        from datetime import timedelta as _td_sms
        _cd_h = float(getattr(settings, 'spot_mass_sl_cooldown_hours', 2.0))
        _SPOT_MASS_SL_COOLDOWN_UNTIL = datetime.now(timezone.utc) + _td_sms(hours=_cd_h)
        print(f'[SPOT/MASS-SL] {_spot_sl_count_this_cycle[0]} SLs in one cycle! '
              f'COOLDOWN {_cd_h}h — no new SPOT buys until {_SPOT_MASS_SL_COOLDOWN_UNTIL.strftime("%H:%M UTC")}')

    return closed


def _sell_holdings_not_in_top(client: BinanceClient, settings, balances: Dict[str, float], top_symbols: List[str], portfolio_manager=None, position_manager=None) -> List[Dict]:
    """Clean up orphan assets — assets held in Binance balance with NO open position record in
    the PositionManager (leftovers from before the system existed or from external trades).
    Registered positions are NEVER touched here — they are managed exclusively by SL/TP checker
    and the slot-pressure reallocation (Step 3).
    """
    orders = []
    _cleanup_count = 0
    _max_cleanup = _get_param(settings, 'max_rebalance_per_cycle')

    for asset, amount in balances.items():
        if asset.upper() == "USDT" or amount <= 0:
            continue
        if _cleanup_count >= _max_cleanup:
            print(f"[Cleanup] max_cleanup_per_cycle={_max_cleanup} reached — deferring remaining orphans")
            break

        symbol = f"{asset.upper()}USDT"
        if symbol in _INVALID_SYMBOLS:
            continue

        # Keep BNB out of orphan cleanup when commission discount is active.
        if symbol == "BNBUSDT" and (
            bool(_get_param(settings, 'use_bnb_for_commission'))
            or bool(_get_param(settings, 'auto_bnb_for_commission'))
        ):
            continue

        # GATE: if there is ANY registered position for this symbol → NEVER touch it here.
        # SL/TP checker and Step 3 (slot-pressure) handle registered positions.
        if position_manager:
            try:
                pos_list = position_manager.get_all_positions().get(symbol, [])
                if pos_list:
                    print(f"[Cleanup] Skipping {symbol}: has {len(pos_list)} registered position(s) — managed by SL/TP")
                    continue
            except Exception:
                pass

        try:
            filters = get_symbol_filters(client, symbol)
            price = _get_cycle_price(client, symbol)
        except Exception as exc:
            if "-1121" in str(exc):
                _INVALID_SYMBOLS.add(symbol)
                _persist_invalid_symbol_state(_db_client_ref)
            else:
                print(f"[Cleanup] Skip {symbol}: {exc}")
            continue

        qty = align_quantity_to_step(amount, filters["step"])
        valid, reason = is_quantity_valid(qty, price, filters)
        if not valid:
            print(f"[Cleanup] Skipping orphan {symbol}: {reason}")
            continue

        try:
            print(f"[Cleanup] Selling orphan {symbol}: {qty} units @ {price:.6f} (no PM record)")
            order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
            if not client.verify_order_filled(order, f"Cleanup SELL {symbol}"):
                print(f"[WARN] Cleanup SELL {symbol}: order NOT confirmed filled")
                continue
            orders.append({"symbol": symbol, "side": "SELL", "quantity": qty, "order": order})
            _overflow_positions.discard(symbol)
            _cleanup_count += 1
            add_signal({
                "symbol": symbol, "action": "SELL",
                "price": price,
                "reason": "orphan_asset_cleanup",
                "source": "cleanup",
            })
        except Exception as exc:
            print(f"[Cleanup] Sell failed for orphan {symbol}: {exc}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Orphan cleanup sell failed {symbol}: {exc}")
    return orders


def _kelly_position_size(settings, strength: float, prediction: dict,
                         usdt_liquid: float, sl_pct: float = 0.015,
                         tp_pct: float = 0.025,
                         base_override: float = None,
                         max_override: float = None,
                         cap_pct_override: float = None,
                         min_usdt: float = None,
                         concentration_multiplier: float = 1.0,
                         db_client=None, symbol: str = None,
                         trade_type: str = 'spot', regime: str = None,
                         action: str = 'open', extra_multipliers: dict = None) -> float:
    """
    Half-Kelly position sizing scaled by signal quality.  (K1: bases subidas)

    Tiers (prob / strength thresholds):
      FUERTE  prob >= 0.65 + strength >= 0.80  → kelly_max_usdt  ($500 SPOT / $800 FUT)
      ALTA    prob >= 0.60 + strength >= 0.70  → 90% of max       (K1: 80%→90%)
      MEDIA   prob >= 0.55 + strength >= 0.55  → 60% of max
      BASE    prob >= 0.50  (passed validation) → kelly_base_usdt  ($100)

    Fallback (kelly disabled): max_per_trade_usdt.
    Hard cap: kelly_max_pct_liquid of available USDT.
    base_override / max_override: optional per-call overrides (used by Unified Portfolio Manager
    to supply Futures-specific floors/ceilings without changing SPOT Kelly behavior).
    db_client / symbol / trade_type / regime: used to write to kelly_sizing_log (C3 trazabilidad).
    """
    if not _get_param(settings, "kelly_enabled"):
        return _get_param(settings, "max_per_trade_usdt")

    fixed_base = base_override if base_override is not None else _get_param(settings, "kelly_base_usdt")
    dyn_pct = _get_param(settings, "kelly_dynamic_base_pct")
    base = max(fixed_base, usdt_liquid * dyn_pct)
    # Smart Kelly: max scales with liquid capital instead of fixed cap
    max_usdt_floor = max_override if max_override is not None else _get_param(settings, "kelly_max_usdt")
    max_usdt_pct_cap = _get_param(settings, "kelly_max_pct_cap")
    max_usdt = max(max_usdt_floor, usdt_liquid * max_usdt_pct_cap)
    cap_pct = cap_pct_override if cap_pct_override is not None else _get_param(settings, "kelly_max_pct_liquid")

    # Absolute minimum ticket floor (per-mode); 0 disables it
    _min_usdt_key = 'futures_kelly_min_usdt' if str(trade_type).strip().lower() == 'futures' else 'kelly_min_usdt'
    _min_usdt = float(min_usdt if min_usdt is not None else _get_param(settings, _min_usdt_key, 0.0))

    prob = float((prediction or {}).get("success_probability", 0.50))
    r = tp_pct / sl_pct if sl_pct > 0 else 1.67

    # Adjust reward/risk for round-trip Binance fees (net Kelly)
    from binance_commission import get_config as _get_kelly_comm_config
    _kelly_comm_rate = _get_kelly_comm_config(settings).rate_for(trade_type)
    r_net = max(0.1, (tp_pct - 2 * _kelly_comm_rate) / max(sl_pct + 2 * _kelly_comm_rate, 0.0001))

    # Half-Kelly fraction using net reward/risk
    kelly_f = max(0.0, (prob - (1 - prob) / r_net)) * 0.5

    # Map to tier — FUERTE/ALTA/MEDIA use tier amount directly (kelly_f already near max).
    # BASE tier uses kelly_f scaling to soften weak signals.
    if prob >= 0.65 and strength >= 0.80:
        tier, amount = "FUERTE", max_usdt
        sized = amount  # at max conviction, use full tier
    elif prob >= 0.60 and strength >= 0.70:
        tier, amount = "ALTA", base + (max_usdt - base) * 0.90  # K1: 80%→90%
        sized = amount  # high conviction, no kelly_f discount
    elif prob >= 0.55 and strength >= 0.55:
        tier, amount = "MEDIA", base + (max_usdt - base) * 0.50
        sized = amount  # medium conviction, use full tier
    else:
        tier, amount = "BASE", base
        # BASE tier: scale by kelly_f so weak signals get smaller sizes
        scaled = amount * min(1.0, kelly_f * 10)
        sized = max(base * 0.5, scaled)  # floor at 50% of base, not full base

    _context_mult = _get_kelly_context_multiplier(settings, regime=regime, trade_type=trade_type)
    _kpi_mult = _get_kpi_context_multiplier(db_client)
    _context_mult *= _kpi_mult
    if _context_mult != 1.0:
        sized *= _context_mult

    _conc_mult = max(0.10, float(concentration_multiplier or 1.0))
    if _conc_mult != 1.0:
        sized *= _conc_mult

    # Hard cap: fraction of liquid USDT
    liquid_cap = usdt_liquid * cap_pct
    final = min(sized, liquid_cap, max_usdt)

    # Enforce absolute minimum ticket floor; skip trade if we cannot meet it
    if _min_usdt > 0 and final < _min_usdt:
        if liquid_cap >= _min_usdt and max_usdt >= _min_usdt:
            final = min(_min_usdt, liquid_cap, max_usdt)
        else:
            final = 0.0

    print(f"Kelly sizing: prob={prob:.2f}, strength={strength:.2f}, kelly_f={kelly_f:.3f} "
          f"ctx={_context_mult:.3f}, conc={_conc_mult:.3f} → tier={tier}, "
          f"sized=${sized:.2f}, cap=${liquid_cap:.2f}, min=${_min_usdt:.2f} → final=${final:.2f} USDT")

    # C3: Persist kelly decision to kelly_sizing_log for 100% traceability
    if db_client and symbol:
        try:
            import json as _json
            _mults = dict(extra_multipliers or {})
            _mults.setdefault('context_multiplier', round(_context_mult, 4))
            _mults.setdefault('kpi_multiplier', round(_kpi_mult, 4))
            _mults.setdefault('concentration_multiplier', round(_conc_mult, 4))
            _mults.setdefault('min_usdt', round(_min_usdt, 2))
            _mults.setdefault('current_regime_mode', str(getattr(settings, 'current_regime_mode', '') or '').strip().upper() or None)
            _mults.setdefault('current_regime_profile_label', str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None)
            db_client.execute_update(
                "INSERT INTO kelly_sizing_log "
                "(symbol, trade_type, regime, prob, strength, tier, "
                " base_usdt, sized_usdt, final_usdt, multipliers, liquid_usdt, cap_usdt, action) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (symbol, trade_type, regime or 'unknown', round(prob, 4), round(strength, 4),
                 tier, round(base, 2), round(sized, 2), round(final, 2),
                 _json.dumps(_mults), round(usdt_liquid, 2), round(liquid_cap, 2), action)
            )
        except Exception as _kl_err:
            pass  # fail-silent — log failure must never block a trade

    return final


_PROFILE_OVERRIDES = {
    "A": {"base_usdt": 500.0, "max_usdt": 2500.0, "cap_pct": 0.50},
    "B": {"base_usdt": 250.0, "max_usdt": 1200.0, "cap_pct": 0.35},
    "C": {"base_usdt": 100.0, "max_usdt": 600.0, "cap_pct": 0.25},
}


def _assign_kelly_profile(
    strength: float,
    prediction: Optional[Dict[str, Any]],
    signal_meta: Dict[str, Any],
    symbol: str,
) -> str:
    """Map a BUY candidate to A/B/C based on peak_quality profile.

    If `AutonomousSignalGenerator` has already assigned a `peak_quality_profile`
    (A/B/C/discarded) use it directly.  Otherwise fall back to the existing
    prob/strength tiers so production keeps working while the predictive
    pipeline matures.
    """
    meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}
    profile = str(meta.get("peak_quality_profile", "")).strip().upper()
    if profile in ("A", "B", "C"):
        return profile
    prob = float((prediction or {}).get("success_probability", 0.0))
    if prob >= 0.65 and strength >= 0.80:
        return "A"
    if prob >= 0.60 and strength >= 0.70:
        return "B"
    return "C"


def _check_entry_quality(
    symbol: str,
    signal_meta: Dict[str, Any],
    settings,
    regime_mode: str,
    active_profile_label: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Behavior-first entry gate.

    Returns (is_valid, reason).  Only approve assets whose current price
    behavior matches a short-term profitable pattern for the active regime and
    profile.  The asset's name is irrelevant; only its current behavior matters.

    Profile modifiers affect patience/selectivity, never the hard behavioral
    floor.  Profile A is slightly more permissive, profile C slightly tighter.
    """
    _regime = str(regime_mode or "").strip().upper() or "SPOT_BULL"
    _profile = str(active_profile_label or "").strip().upper() or "B"
    meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}

    strength = float(meta.get("strength", 0.0) or 0.0)
    rsi_raw = meta.get("rsi")
    rsi = float(rsi_raw.get("value") if isinstance(rsi_raw, dict) else (rsi_raw or 50.0))
    vol_spike = float(meta.get("vol_spike", 1.0) or 1.0)
    bb_position = meta.get("bb_position")
    bb_lower_touch = bool(meta.get("bb_lower_touch", False))
    bb_near_lower = bool(meta.get("bb_near_lower", False))
    trend_4h = str(meta.get("higher_tf_trend", "FLAT") or "FLAT").strip().upper()
    ema200_trend = str(meta.get("ema200_trend", "UNKNOWN") or "UNKNOWN").strip().upper()

    # Profile modifier: A = more aggressive, C = more selective, B = baseline.
    # Applied to upper/lower bounds so that the hard floor is never breached.
    _profile_mult = {"A": 0.92, "B": 1.0, "C": 1.12}.get(_profile, 1.0)

    if _regime == "SPOT_BULL":
        # Want: rising assets, entering on pullbacks, with volume.
        # Volume floor is lower than the 4h expert rule (1.5x) because the 1h
        # signal generator is more reactive; volume_confirmed is still a sizing
        # booster.
        _min_vol_base = float(_get_param(settings, 'entry_quality_vol_spike_min_spot_bull', 0.85))
        _rsi_min_base = float(_get_param(settings, 'entry_quality_rsi_min_spot_bull', 33.0))
        _rsi_max_base = float(_get_param(settings, 'entry_quality_rsi_max_spot_bull', 68.0))
        _breakout_min_vol = float(_get_param(settings, 'entry_quality_breakout_min_vol_spot_bull', 1.20))
        _breakout_rsi_max = float(_get_param(settings, 'entry_quality_breakout_rsi_max_spot_bull', 78.0))
        _breakout_min_strength = float(_get_param(settings, 'entry_quality_breakout_min_strength_spot_bull', 0.58))
        _vol_floor = float(_get_param(settings, 'entry_quality_vol_spike_floor_spot_bull', 0.7))
        _min_vol = max(_vol_floor, _min_vol_base * _profile_mult)
        _rsi_min = max(25.0, _rsi_min_base * (2.0 - _profile_mult))
        _rsi_max = min(72.0, _rsi_max_base / _profile_mult)
        _breakout_ok = (
            rsi > _rsi_max
            and rsi <= _breakout_rsi_max
            and vol_spike >= _breakout_min_vol
            and strength >= _breakout_min_strength
        )
        if trend_4h == "DOWN":
            return False, f"SPOT_BULL_4H_DOWN (trend={trend_4h})"
        if ema200_trend == "DOWN":
            return False, "SPOT_BULL_EMA200_DOWN"
        if vol_spike < _min_vol and not _breakout_ok:
            return False, f"SPOT_BULL_VOL (vol_spike={vol_spike:.2f} < {_min_vol:.2f})"
        if not (_rsi_min <= rsi <= _rsi_max) and not _breakout_ok:
            return False, f"SPOT_BULL_RSI (rsi={rsi:.1f})"
        if strength < float(getattr(settings, "entry_quality_min_strength", 0.40)):
            return False, f"SPOT_BULL_STRENGTH (strength={strength:.2f})"
        return True, "OK"

    if _regime == "RANGE_SCALP":
        # Want: price in the lower half of the Bollinger range, mean-reversion
        # pressure, with volume.  Either a lower-band touch/near-touch OR a low
        # bb_position confirms the behavior.
        # s77: read DB-governable override; fall back to formula if not set
        _vol_rs_override = float(getattr(settings, 'entry_quality_vol_spike_min_range_scalp', 0.0) or 0.0)
        _min_vol = _vol_rs_override if _vol_rs_override > 0 else max(0.4, 0.6 * _profile_mult)
        _rsi_min = float(getattr(settings, "range_scalp_min_rsi", 25.0))
        _rsi_max = min(70.0, float(getattr(settings, "range_scalp_max_rsi", 60.0)) / _profile_mult)
        _max_bb_pos = 0.45 / _profile_mult
        if not (_rsi_min <= rsi <= _rsi_max):
            return False, f"RANGE_RSI (rsi={rsi:.1f})"
        if vol_spike < _min_vol:
            return False, f"RANGE_VOL (vol_spike={vol_spike:.2f} < {_min_vol:.2f})"
        _bb_pos_ok = bb_position is not None and float(bb_position) <= _max_bb_pos
        _bb_touch_ok = bb_lower_touch or bb_near_lower
        # s78: if bb_position is None AND no touch/near flags, skip gate — missing
        # data should not reject an otherwise valid signal (autonomous signals may
        # not carry bb_position). Only reject when we have data that shows price is
        # NOT at the lower band.
        if bb_position is None and not _bb_touch_ok:
            pass  # gate skipped: no BB data available
        elif not (_bb_pos_ok or _bb_touch_ok):
            _bb_pos_str = f"{float(bb_position):.2f}" if bb_position is not None else "?"
            return False, f"RANGE_BB (bb_pos={_bb_pos_str} > {_max_bb_pos:.2f}, touch={bb_lower_touch}, near={bb_near_lower})"
        if strength < float(getattr(settings, "entry_quality_min_strength", 0.40)):
            return False, f"RANGE_STRENGTH (strength={strength:.2f})"
        return True, "OK"

    if _regime == "PRESERVATION":
        # Want: only narrative/trending catalysts with strong volume and not overbought.
        _min_vol = max(0.8, 1.0 * _profile_mult)
        _rsi_max = min(70.0, 60.0 / _profile_mult)
        if vol_spike < _min_vol:
            return False, f"PRESERVATION_VOL (vol_spike={vol_spike:.2f} < {_min_vol:.2f})"
        if rsi > _rsi_max:
            return False, f"PRESERVATION_RSI (rsi={rsi:.1f})"
        if strength < float(getattr(settings, "entry_quality_min_strength", 0.40)):
            return False, f"PRESERVATION_STRENGTH (strength={strength:.2f})"
        return True, "OK"

    if _regime == "FUTURES_SHORT":
        # Want: assets bouncing within a bearish structure — declining price is
        # the product.  We need moderate RSI (not oversold), trend DOWN, volume.
        _min_vol = max(0.5, 0.8 * _profile_mult)
        _rsi_min = min(55.0, 45.0 * _profile_mult)
        _rsi_max = 75.0
        if trend_4h != "DOWN":
            return False, f"FUTURES_SHORT_TREND (trend={trend_4h})"
        if vol_spike < _min_vol:
            return False, f"FUTURES_SHORT_VOL (vol_spike={vol_spike:.2f} < {_min_vol:.2f})"
        if not (_rsi_min <= rsi <= _rsi_max):
            return False, f"FUTURES_SHORT_RSI (rsi={rsi:.1f})"
        if strength < float(getattr(settings, "futures_min_inject_strength", 0.50)):
            return False, f"FUTURES_SHORT_STRENGTH (strength={strength:.2f})"
        return True, "OK"

    return False, f"UNKNOWN_REGIME ({_regime})"


# Path to the calibrated per-regime posterior model produced by the shadow log sweep.
_EVIDENCE_GATE_CALIBRATION_PATH = "/app/Dev/benchmarks/forced_regime_sweep/calibration/evidence_gate_calibration.json"
_calibrated_posterior_model: Optional[Dict[str, Any]] = None


def _load_calibrated_posterior_model() -> Dict[str, Any]:
    """Load (and cache) the per-regime logistic posterior calibration."""
    global _calibrated_posterior_model
    if _calibrated_posterior_model is None:
        try:
            with open(_EVIDENCE_GATE_CALIBRATION_PATH, "r") as _f:
                _calibrated_posterior_model = json.load(_f).get("posterior_model", {})
        except Exception:
            _calibrated_posterior_model = {}
    return _calibrated_posterior_model or {}


def _evidence_gate_posterior(
    cand: Dict[str, Any],
    signal_meta: Dict[str, Any],
    settings,
    regime_mode: str,
    active_profile_label: Optional[str] = None,
) -> float:
    """Compute a calibrated Bayesian posterior probability for a candidate.

    When a per-regime logistic calibration exists (trained on the shadow log), it
    returns the model's predicted probability of a positive PnL outcome using the
    predictor probability and expected PnL as features.  Falls back to the
    original hand-tuned log-odds formula when no calibration is available.
    """
    prior = float(getattr(settings, "evidence_gate_prior", 0.50))
    symbol = cand.get("symbol", "")
    meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}

    prob = float(
        cand.get("predictor_prob")
        or cand.get("success_probability")
        or meta.get("predictor_prob")
        or meta.get("success_probability")
        or prior
    )
    prob = max(0.001, min(0.999, prob))

    # Calibrated logistic model per regime (shadow-log trained).
    calibration = _load_calibrated_posterior_model().get(regime_mode)
    if calibration:
        ev = float(
            cand.get("predictor_ev")
            or cand.get("expected_pnl")
            or meta.get("predictor_ev")
            or meta.get("expected_pnl")
            or 0.0
        )
        logit = math.log(prob / (1.0 - prob))
        z = (
            calibration["intercept"]
            + calibration["logit_coef"] * logit
            + calibration["ev_coef"] * ev
        )
        z = max(-20.0, min(20.0, z))
        posterior = 1.0 / (1.0 + math.exp(-z))
        return max(0.0, min(1.0, posterior))

    # Fallback: original hand-tuned log-odds formula.
    pq_profile = cand.get("peak_quality_profile") or meta.get("peak_quality_profile")
    pq_mult = {"A": 1.25, "B": 1.0, "C": 0.85, "discarded": 0.0}.get(pq_profile, 1.0)

    strength = float(cand.get("signal_strength") or meta.get("signal_strength") or 0.5)
    strength_mult = 0.5 + strength  # 0.5..1.5

    log_odds = math.log(prob / (1.0 - prob)) - math.log(prior / (1.0 - prior))
    log_likelihood = math.log(max(0.01, pq_mult * strength_mult))
    posterior_log_odds = math.log(prior / (1.0 - prior)) + log_odds + log_likelihood
    posterior = 1.0 / (1.0 + math.exp(-posterior_log_odds))
    return max(0.0, min(1.0, posterior))


def _filter_buy_candidates(
    candidates: List[Any],
    signal_meta: Dict[str, Any],
    settings,
    regime_mode: str,
    active_profile_label: Optional[str] = None,
) -> List[Any]:
    """
    Pre-filter candidate signals using the behavior-first entry gate.

    Accepts either symbol strings or dicts with a 'symbol' key.  Returns the same
    shape filtered to candidates whose current behavior matches the active
    regime/profile objective.
    """
    passed: List[Any] = []
    for cand in candidates:
        if isinstance(cand, dict):
            symbol = cand.get("symbol", "")
            meta = cand
        else:
            symbol = cand
            meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}

        # Peak-quality hard discard gate: signals not in A/B/C are dropped.
        if meta.get("peak_quality_profile") == "discarded":
            print(f"[FilterBuy] Rejected {symbol}: PEAK_QUALITY_DISCARDED")
            update_signal_decision(symbol, "rejected", "PEAK_QUALITY_DISCARDED")
            continue

        # Audit the first 3 candidates' peak-quality profile/thresholds in production.
        if len(passed) < 3:
            print(
                f"[EvidenceGateAudit] {symbol}: peak_profile={meta.get('peak_quality_profile')} "
                f"thresholds={meta.get('thresholds')} "
                f"expected_gain={meta.get('expected_gain')} "
                f"prob={meta.get('success_probability')} "
                f"strength={meta.get('signal_strength')}"
            )

        _ok, _reason = _check_entry_quality(
            symbol, signal_meta, settings, regime_mode, active_profile_label
        )
        if not _ok:
            print(f"[FilterBuy] Rejected {symbol}: {_reason}")
            update_signal_decision(symbol, "rejected", _reason.split(" ")[0])
            continue

        # Optional Bayesian evidence gate (disabled by default until calibrated).
        if getattr(settings, "evidence_gate_enabled", False):
            posterior = _evidence_gate_posterior(
                meta, signal_meta, settings, regime_mode, active_profile_label
            )
            per_regime = getattr(settings, "evidence_gate_min_posterior_per_regime", {
                "SPOT_BULL": 0.60, "FUTURES_SHORT": 0.80, "RANGE_SCALP": 0.52, "PRESERVATION": 0.80
            }) or {}
            min_posterior = float(
                per_regime.get(regime_mode)
                or getattr(settings, "evidence_gate_min_posterior", 0.65)
            )
            if posterior < min_posterior:
                print(
                    f"[FilterBuy] Rejected {symbol}: EVIDENCE_GATE_LOW "
                    f"(posterior={posterior:.3f} < {min_posterior})"
                )
                update_signal_decision(symbol, "rejected", "EVIDENCE_GATE_LOW")
                continue

        passed.append(cand)
    return passed


def _buy_top_symbols(client: BinanceClient, settings, top_signals_with_meta: List, portfolio_manager=None, position_manager=None, precomputed_predictions: Dict = None, news_client=None, regime_obj: "MarketRegime" = None, regime_mode_override: str = None, screened_pairs_cache: dict = None, pre_switch_state: Optional[Dict[str, Any]] = None, futures_client=None) -> List[Dict]:
    # Accept either plain symbol strings or dicts with metadata (for aggressive mode)
    if top_signals_with_meta and isinstance(top_signals_with_meta[0], dict):
        top_symbols = [s['symbol'] for s in top_signals_with_meta]
        signal_meta = {s['symbol']: s for s in top_signals_with_meta}
    else:
        top_symbols = list(top_signals_with_meta)
        signal_meta = {}

    orders = []
    if not top_symbols:
        return orders

    # ── BTC macro trend filter ─────────────────────────────────────────────────
    btc_trend = 'FLAT'
    btc_4h_chg_pct = 0.0  # last closed 4h candle change %
    try:
        sig_gen = _get_cycle_asg(client)  # BUG-E2: reuse cycle-scoped instance
        btc_trend = sig_gen.get_btc_market_trend(interval='4h')
        _btc_kl = client.client.get_klines(symbol='BTCUSDT', interval='4h', limit=3)
        if _btc_kl and len(_btc_kl) >= 2:
            _prev = _btc_kl[-2]  # last fully closed candle
            btc_4h_chg_pct = (float(_prev[4]) - float(_prev[1])) / float(_prev[1]) * 100
        print(f"BTC 4h trend: {btc_trend} | last 4h candle: {btc_4h_chg_pct:+.2f}%")
    except Exception as e:
        print(f"BTC trend check failed: {e}")

    # ── Market Regime — use pre-detected from execute_once(); fallback to own detection ──
    _regime: MarketRegime = regime_obj
    _regime_mode = regime_mode_override or "SPOT_BULL"
    if _regime is None and getattr(settings, 'market_regime_enabled', True):
        try:
            _regime = get_regime(client, settings=settings)
            _regime_mode = _regime.mode
            print(f"[Regime/fallback] {_regime_mode} | {_regime.regime} | confirmed={_regime.confirmed}")
        except Exception as _re:
            print(f"[Regime] detection failed: {_re}")
    elif _regime is not None:
        print(f"[Regime/cached] {_regime_mode} | {_regime.regime} | confirmed={_regime.confirmed}")

    _active_profile_label = _get_active_profile_label(_db_client_ref, _regime_mode)

    # Persist active regime so the screener/dashboard always see the current mode
    if settings is not None:
        try:
            settings.current_regime_mode = _regime_mode
            settings.current_regime_profile_label = _active_profile_label
        except Exception:
            pass

    # ── Behavior-first candidate filter ───────────────────────────────────────
    # NOTE: Primary filtering now runs in execute_once() BEFORE QUBO.
    # This secondary pass only applies when _buy_top_symbols is called directly
    # (e.g., from tests or alternative paths) and entry_quality_gate_mode is blocking.
    _eq_mode_bts = str(getattr(settings, 'entry_quality_gate_mode', 'blocking')).lower()
    if _eq_mode_bts == "blocking":
        top_signals_with_meta = _filter_buy_candidates(
            top_signals_with_meta, signal_meta, settings, _regime_mode, _active_profile_label
        )
    else:
        # Advisory/disabled: only hard-reject peak_quality_profile == "discarded"
        top_signals_with_meta = [
            c for c in top_signals_with_meta
            if not (isinstance(c, dict) and str(c.get('peak_quality_profile', '')).lower() == 'discarded')
        ]
    if top_signals_with_meta and isinstance(top_signals_with_meta[0], dict):
        top_symbols = [s['symbol'] for s in top_signals_with_meta]
    else:
        top_symbols = list(top_signals_with_meta)
    if not top_symbols:
        return orders

    balances = client.get_account_balance()
    usdt = float(balances.get("USDT", 0.0))
    usdt_liquid = usdt  # default; overridden below if portfolio_manager available
    
    # Use Portfolio Manager for allocation if available
    if portfolio_manager:
        # Use unified balance (spot+futures) when UnifiedPM is enabled so that
        # _total_portfolio reflects real capital across both wallets.
        _use_unified = _get_param(settings, 'use_unified_portfolio_manager')
        if _use_unified:
            try:
                _fc_bal = _get_futures_client(settings, injected_client=futures_client)
                balance = portfolio_manager.get_unified_balance(_fc_bal)
            except Exception as _ub_err:
                print(f"[Portfolio] unified balance failed ({_ub_err}), falling back to spot-only")
                balance = portfolio_manager.get_total_balance()
        else:
            balance = portfolio_manager.get_total_balance()
        # Use liquid USDT as the actual cap — not total portfolio value
        # Prefer BNB-reserve-aware balance so commission buffer is never traded away
        usdt_liquid = float(balance.get("usdt_available_after_reserve", balance.get("usdt_available", usdt)))
        _total_portfolio = float(balance.get("total_usdt", usdt_liquid))
        # BNB held as commission reserve is cash-equivalent, not invested trading capital
        _spot_bnb_value = float(balance.get("spot_bnb_usdt", 0.0))
        _invested = max(0.0, _total_portfolio - usdt_liquid - _spot_bnb_value)
        _exposure_pct = _invested / _total_portfolio if _total_portfolio > 0 else 0.0
        _max_exposure = getattr(settings, 'max_total_exposure_pct', 0.75)
        # Aggressive mode: SPOT_BULL + F&G Fear → higher exposure cap to deploy more capital
        _fng_early = 50
        try:
            if news_client:
                _fng_early = int(news_client.get_fear_greed().get('value', 50))
        except Exception:
            pass
        _fng_fear_thr = getattr(settings, 'max_total_exposure_spot_bull_fear_fng_threshold', 40)
        _spot_bull_fear_cap = getattr(settings, 'max_total_exposure_spot_bull_fear', 0.95)
        if _regime_mode == "SPOT_BULL" and _fng_early <= _fng_fear_thr:
            _max_exposure = _spot_bull_fear_cap
            print(f"[Exposure] SPOT_BULL + F&G={_fng_early} (Fear) → aggressive cap {_max_exposure:.0%}")
        print(f"USDT liquid: {usdt_liquid:.2f}, total: {_total_portfolio:.2f}, invested: {_invested:.2f} ({_exposure_pct:.1%}) — cap: {_max_exposure:.0%}")
        _spot_exposure_block = _exposure_pct >= _max_exposure and _regime_mode != "FUTURES_SHORT"
        if _spot_exposure_block:
            print(f"[Exposure] Portfolio {_exposure_pct:.1%} >= cap {_max_exposure:.0%} — skipping new spot buys this cycle")
            return orders
        allocation = min(settings.max_per_trade_usdt, usdt_liquid / max(len(top_symbols), 1))
    else:
        if usdt <= 0:
            print("No USDT available for buys")
            return orders
        allocation = min(settings.max_per_trade_usdt, usdt / len(top_symbols))
        print(f"USDT available: {usdt}, allocation per symbol: {allocation}")

    # ── Bear mode derived from market_regime (single source of truth) ──────────
    # _bear_mode_active replaced by _regime.mode PRESERVATION/FUTURES_SHORT
    # This eliminates the triple Kelly penalty bug (btc_trend×0.5 + bear×0.5 + regime×0.5 = $7.50)
    _bear_mode_active = (_regime_mode in ("PRESERVATION", "FUTURES_SHORT")) and (_regime is not None and _regime.confirmed)
    if _bear_mode_active:
        print(f"[Regime] Bear mode active via {_regime_mode} (confirmed) — NARRATIVE-only filter enabled")

    # ── BTC dominance filter — skip altcoins if BTC.D too high ────────────────
    _btc_dom_high = False
    if getattr(settings, 'btc_dominance_filter_enabled', True):
        try:
            import urllib.request as _ur, json as _json, socket as _sock
            with _ur.urlopen('https://api.coingecko.com/api/v3/global', timeout=6) as _r:
                _gdata = _json.loads(_r.read())
            _btc_dom = _gdata.get('data', {}).get('market_cap_percentage', {}).get('btc', 0)
            _dom_thresh = _get_param(settings, 'btc_dominance_altcoin_threshold')
            if _btc_dom > _dom_thresh:
                _btc_dom_high = True
                print(f"BTC Dominance {_btc_dom:.1f}% > {_dom_thresh}% — altcoin entries restricted to NARRATIVE only")
        except (_sock.timeout, TimeoutError):
            print("[BtcDom] timeout — skipping BTC dominance filter this cycle")
        except Exception:
            pass

    # ── R5: Circuit breaker — abort buys if system has consecutive API failures ──
    if _circuit_breaker_check(settings):
        return orders  # buys paused — SL/TP and closes are unaffected

    # ── R1/R2: Fetch F&G once per cycle (cached 1h by news_client) ────────────
    _fng_value = 50  # default: Neutral
    try:
        if news_client:
            _fng_data = news_client.get_fear_greed()
            _fng_value = int(_fng_data.get('value', 50))
    except Exception:
        pass

    # ── R2: Capitulation PnL boost — relax min_pnl further when F&G ≤ 15 ──────
    # Applied in pnl_service.validate() via news_client.get_fear_greed_modifier(),
    # but here we also track the value for the Kelly boost below.

    # ── BTC bearish market: allocation handled by regime Kelly multiplier ────────
    # Removed: allocation *= 0.5 on btc_trend==DOWN — regime.kelly_multiplier already
    # applies the correct reduction (PRESERVATION×0.5, FUTURES_SHORT×0.3) without
    # double-penalizing with an additional 50% cut on top.

    # If USDT is too low, try to sell non-top assets first to get more USDT
    if usdt < allocation * len(top_symbols) and portfolio_manager:
        print(f"USDT available ({usdt}) is less than needed ({allocation * len(top_symbols)}), selling non-top assets...")
        sell_orders = _sell_holdings_not_in_top(client, settings, balances, top_symbols, portfolio_manager, position_manager)
        orders.extend(sell_orders)
        
        # Refresh USDT balance after selling
        balances = client.get_account_balance()
        usdt = float(balances.get("USDT", 0.0))
        print(f"USDT after selling non-top assets: {usdt}")

    _regime_max = _get_max_positions_for_regime(settings, _regime_mode)
    # BUG-E5 fix: fetch balance once before the loop; refresh only after a successful buy
    usdt_liquid_now = float(client.get_account_balance().get("USDT", usdt_liquid))
    # PB1: Stagger entries — track new shorts opened this cycle (max N per cycle)
    _pb_new_shorts_this_cycle = 0
    _pb_max_new_per_cycle = int(getattr(settings, 'futures_max_new_per_cycle', 5))

    # ── A3: BTC 1h correlation filter — reduce new entries on BTC downdraft ─────
    _btc_1h_drop_pct = 0.0
    _btc_corr_max_new = None  # None = no restriction
    if getattr(settings, 'btc_correlation_filter_enabled', True):
        try:
            _btc_1h_kl = client.client.get_klines(symbol='BTCUSDT', interval='1h', limit=2)
            if _btc_1h_kl:
                _btc_open_1h  = float(_btc_1h_kl[-1][1])
                _btc_close_1h = float(_btc_1h_kl[-1][4])
                _btc_1h_drop_pct = (_btc_close_1h - _btc_open_1h) / _btc_open_1h * 100
                _btc_corr_thr = getattr(settings, 'btc_correlation_drop_threshold_pct', 0.3)
                if _btc_1h_drop_pct < -_btc_corr_thr:
                    _btc_corr_max_new = int(getattr(settings, 'btc_correlation_max_new_entries', 2))
                    print(f"[A3/BtcCorr] BTC 1h={_btc_1h_drop_pct:+.2f}% < -{_btc_corr_thr:.1f}% → SPOT new entries cap={_btc_corr_max_new} this cycle")
        except Exception as _btc_corr_err:
            pass
    _spot_new_entries_this_cycle = 0

    for symbol in top_symbols:
        _signal_id = None
        volspike_override = False
        if isinstance(signal_meta, dict):
            _signal_id = signal_meta.get(symbol, {}).get('signal_id')
        # ── FUTURES_SHORT mode — evaluated BEFORE spot position limits ────────
        # Futures positions are in a separate wallet — they must NOT be blocked
        # by the spot max_positions counter (which fills up with open spot trades).
        if _regime and _regime.confirmed and _regime_mode == "FUTURES_SHORT":
            if getattr(settings, 'futures_enabled', True):
                if isinstance(pre_switch_state, dict) and pre_switch_state.get("active") and pre_switch_state.get("block_new_shorts"):
                    _ps_to = pre_switch_state.get("to_mode")
                    _ps_conf = _safe_float(pre_switch_state.get("confidence"), 0.0)
                    print(f"[Futures/PreSwitch] SHORT {symbol} SKIPPED: pre_switch to {_ps_to} (confidence={_ps_conf:.2f})")
                    update_signal_decision(symbol, "rejected", "PRE_SWITCH")
                    continue
                # PB2: Mass SL circuit breaker — skip ALL new shorts during global cooldown
                if _FUTURES_MASS_SL_COOLDOWN_UNTIL and datetime.now(timezone.utc) < _FUTURES_MASS_SL_COOLDOWN_UNTIL:
                    _cd_rem = (_FUTURES_MASS_SL_COOLDOWN_UNTIL - datetime.now(timezone.utc)).total_seconds() / 3600
                    print(f'[Futures/MASS-SL] {symbol}: GLOBAL COOLDOWN active ({_cd_rem:.1f}h remaining) — no new shorts')
                    update_signal_decision(symbol, 'rejected', 'MASS_SL_COOLDOWN')
                    continue
                _futures_symbols = list(_get_param(settings, 'futures_symbols'))
                if screened_pairs_cache:
                    _futures_symbols = list(dict.fromkeys(_futures_symbols + screened_pairs_cache.get("futures", [])))
                elif getattr(settings, 'screener_enabled', True):
                    try:
                        from pair_screener import get_screened_pairs
                        _futures_symbols = list(dict.fromkeys(
                            _futures_symbols + get_screened_pairs(
                                settings=settings,
                                regime_mode=_regime_mode,
                                profile_label=_active_profile_label,
                                db_client=_db_client_ref,
                            ).get("futures", [])
                        ))
                    except Exception:
                        pass
                if symbol in _INVALID_FUTURES_SYMBOLS:
                    print(f"[Futures] {symbol} in _INVALID_FUTURES_SYMBOLS (stablecoin/invalid) — skipping")
                    update_signal_decision(symbol, "rejected", "INVALID_FUTURES_SYM")
                    continue
                if symbol not in _futures_symbols:
                    print(f"[Futures] {symbol} not in futures_symbols — skipping short, no spot entry in FUTURES_SHORT mode")
                    update_signal_decision(symbol, "rejected", "NOT_FUTURES_SYM")
                    continue
                # FIX-F1: Skip if symbol is in SL cooldown
                _sl_cd_hours = _get_param(settings, 'futures_sl_cooldown_hours')
                _sl_cd_hit = _FUTURES_SL_COOLDOWN.get(symbol)
                if _sl_cd_hit is None:
                    _sl_cd_hit_db = _check_cooldown_db(symbol, 'futures_sl')
                    if _sl_cd_hit_db:
                        print(f'[Futures/SL-CD] {symbol}: SL cooldown active (from DB) — skip re-entry')
                        update_signal_decision(symbol, 'rejected', 'SL_COOLDOWN')
                        continue
                elif (datetime.now(timezone.utc) - _sl_cd_hit).total_seconds() < _sl_cd_hours * 3600:
                    _cd_remaining = _sl_cd_hours - (datetime.now(timezone.utc) - _sl_cd_hit).total_seconds() / 3600
                    print(f'[Futures/SL-CD] {symbol}: SL cooldown active ({_cd_remaining:.1f}h remaining) — skip re-entry')
                    update_signal_decision(symbol, 'rejected', 'SL_COOLDOWN')
                    continue
                # Enforce futures-specific max shorts (separate from spot limit)
                _fc = _get_futures_client(settings, injected_client=futures_client)
                _max_shorts = getattr(settings, 'futures_max_shorts', 5)
                _open_shorts = len([p for p in _fc._open_positions.values() if not p.closed and p.side == "SHORT"])
                # LARGECAP guard: limit large-cap SHORTs to force MID diversification (slots 6-7)
                _FUTURES_LARGE_CAPS = set(getattr(settings, 'futures_largecap_symbols',
                                                  ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT']))
                _max_largecap = getattr(settings, 'futures_max_largecap_shorts', 3)
                if symbol in _FUTURES_LARGE_CAPS:
                    _open_largecap = sum(1 for p in _fc._open_positions.values()
                                         if not p.closed and p.side == 'SHORT' and p.symbol in _FUTURES_LARGE_CAPS)
                    if _open_largecap >= _max_largecap:
                        print(f"[Futures] {symbol} SKIPPED: large-cap slots {_open_largecap}/{_max_largecap} full — prioritizing MIDs")
                        update_signal_decision(symbol, "rejected", "LARGECAP_CAP")
                        continue
                # Escalamiento guard — limit entries per symbol to avoid concentration risk
                _max_esc = getattr(settings, 'futures_max_escalamientos', 2)
                if _max_esc > 0 and _fc.db_client:
                    try:
                        _sym_db_side = 'SELL'  # futures SHORTs are stored as SELL
                        _esc_rows = _fc.db_client.execute_query(
                            "SELECT COUNT(*) as cnt FROM trades_open WHERE symbol = %s AND side = %s "
                            "AND metadata->>'trade_type' = 'futures'",
                            (symbol, _sym_db_side)
                        )
                        _esc_count = int((_esc_rows[0]['cnt'] if _esc_rows else 0) or 0)
                        if _esc_count >= _max_esc:
                            print(f"[Futures] SHORT {symbol} SKIPPED: escalamiento cap {_esc_count}/{_max_esc} — max entries per symbol reached")
                            update_signal_decision(symbol, "rejected", "MAX_ESCALAMIENTOS")
                            continue
                    except Exception as _esc_err:
                        print(f"[Futures] Escalamiento check error for {symbol}: {_esc_err}")

                if _open_shorts >= _max_shorts:
                    # Try overflow: open extra slot only if signal is exceptional
                    _ovfl_meta     = signal_meta.get(symbol, {})
                    _ovfl_strength = float(_ovfl_meta.get('strength', 0.0))
                    _ovfl_volspike = float(_ovfl_meta.get('vol_spike', 0.0))
                    _ovfl_chg24    = float(_ovfl_meta.get('change_24h_pct', 0.0))
                    if _is_futures_overflow_eligible(
                        symbol, _ovfl_strength, _ovfl_volspike, _ovfl_chg24, btc_4h_chg_pct, settings, _fc
                    ):
                        _futures_overflow_positions.add(symbol)
                        print(f"[Futures/Overflow] Opening extra SHORT slot for {symbol} "
                              f"({_open_shorts}/{_max_shorts} + overflow={len(_futures_overflow_positions)}/"
                              f"{getattr(settings,'futures_overflow_max_extra',1)})")
                        # fall through to open the position
                    else:
                        print(f"[Futures] SHORT {symbol} SKIPPED: {_open_shorts}/{_max_shorts} shorts already open")
                        update_signal_decision(symbol, "rejected", "MAX_SHORTS")
                        continue

                # F5: Close residual LONG on same symbol before opening SHORT (regime change artifact)
                _residual_long_key = _fc._pos_key(symbol, "LONG")
                if _residual_long_key in _fc._open_positions and not _fc._open_positions[_residual_long_key].closed:
                    print(f"[Futures] Closing residual LONG on {symbol} before opening SHORT (regime change)")
                    try:
                        _fc.close_long(symbol, reason="regime_change_to_short")
                    except Exception as _cl_err:
                        print(f"[Futures] Could not close residual LONG {symbol}: {_cl_err}")

                _f_sl    = _get_param(settings, 'futures_sl_pct')
                _f_tp    = _get_param(settings, 'futures_tp_pct')
                _f_lev   = _get_param(settings, 'futures_leverage')
                # FIX-P1: Call real predictor for futures SHORT (was hardcoded expected_pnl=1.0)
                _f_sig_meta = signal_meta.get(symbol, {})
                _f_prediction, _f_indicators = None, None
                try:
                    _f_prediction, _f_indicators = pnl_service.predict_for_symbol(
                        symbol, client, settings,
                        position_size_usdt=_get_param(settings, 'futures_usdt_per_trade'),
                        trade_type='futures',
                        trade_side='SHORT',
                    )
                    if _f_prediction:
                        print(f"[Futures/Pred] {symbol}: EV={_f_prediction.get('expected_pnl', 0):.2f} USDT "
                              f"prob={_f_prediction.get('success_probability', 0):.2f}")
                except Exception as _fp_err:
                    print(f"[Futures/Pred] {symbol}: predictor failed ({_fp_err}) — using signal fallback")
                _f_real_prob = float(_f_sig_meta.get('success_probability', float(_f_sig_meta.get('strength', 0.45))))
                _f_real_prob = max(0.45, min(0.75, _f_real_prob))
                _f_pred = _f_prediction or {'success_probability': _f_real_prob, 'expected_pnl': 1.0}
                # UnifiedPM: use futures wallet as Kelly reference when enabled
                _f_base_ov = _f_max_ov = _f_cap_ov = _f_kelly_liquid = None
                if _get_param(settings, 'use_unified_portfolio_manager') and portfolio_manager:
                    try:
                        _unified_short = portfolio_manager.get_unified_balance(_fc)
                        _f_kelly_liquid = _unified_short.get('futures_available_after_reserve', _unified_short.get('futures_available', usdt_liquid))
                        _f_base_ov = _get_param(settings, 'futures_kelly_base_usdt')
                        _f_max_ov  = _get_param(settings, 'futures_kelly_max_usdt')
                        _f_cap_ov  = _get_param(settings, 'futures_kelly_max_pct_liquid')
                        print(f"[UnifiedPM] Futures SHORT Kelly: futures_avail=${_f_kelly_liquid:.2f} "
                              f"base=${_f_base_ov} max=${_f_max_ov} cap_pct={_f_cap_ov}")
                    except Exception as _upm_err:
                        print(f"[UnifiedPM] fallback to spot usdt_liquid ({_upm_err})")
                        _f_kelly_liquid = usdt_liquid
                else:
                    _f_kelly_liquid = usdt_liquid
                _f_conc_mult, _f_conc_diag = _compute_portfolio_concentration_multiplier(
                    settings,
                    regime='FUTURES_SHORT',
                    trade_type='futures',
                    open_slots=_open_shorts,
                    effective_slots=_max_shorts,
                )
                _f_usdt  = _kelly_position_size(
                    settings, strength=float(_f_sig_meta.get('strength', 0.5)),
                    prediction=_f_pred, usdt_liquid=_f_kelly_liquid,
                    # Kelly reward/risk must reflect price movement (not leveraged margin), so divide by leverage
                    sl_pct=_f_sl / _f_lev / 100, tp_pct=_f_tp / _f_lev / 100,
                    base_override=_f_base_ov, max_override=_f_max_ov,
                    cap_pct_override=_f_cap_ov,
                    concentration_multiplier=_f_conc_mult,
                    db_client=_db_client_ref, symbol=symbol,
                    trade_type='futures', regime='FUTURES_SHORT',
                    extra_multipliers={'portfolio_concentration': _f_conc_diag},
                )
                if _get_param(settings, 'kelly_conviction_enabled'):
                    _f_vs = float(_f_sig_meta.get('vol_spike', 0.0))
                    _f_conv3x = _get_param(settings, 'kelly_conviction_vol3x')
                    _f_conv2x = _get_param(settings, 'kelly_conviction_vol2x')
                    _f_conv_max_pct = _get_param(settings, 'kelly_conviction_max_pct')
                    _f_conv_mult = _f_conv3x if _f_vs >= 3.0 else (_f_conv2x if _f_vs >= 2.0 else 1.0)
                    if _f_conv_mult > 1.0:
                        _f_hard_cap = float(_f_kelly_liquid) * float(_f_conv_max_pct)
                        _f_boosted = min(_f_usdt * _f_conv_mult, _f_hard_cap)
                        print(f"[M3/F] {symbol}: vol_spike={_f_vs:.1f}x → conviction ×{_f_conv_mult} ${_f_usdt:.2f}→${_f_boosted:.2f} (cap=${_f_hard_cap:.2f})")
                        _f_usdt = _f_boosted
                # F4: Dynamic cap — max(per_trade_floor, futures_margin × kelly_cap_pct)
                _futures_kelly_cap = getattr(settings, 'futures_kelly_cap_pct', 0.30)
                _futures_margin_avail = _fc.get_available_usdt() if not _fc.dry_run else _f_kelly_liquid * 0.3
                _f_usdt_cap = max(
                    getattr(settings, 'futures_usdt_per_trade', 150.0),
                    _futures_margin_avail * _futures_kelly_cap
                )
                _f_usdt = min(_f_usdt, _f_usdt_cap)

                # P3.2: Dynamic correlation guard — reduce Kelly when open SHORTs are highly correlated
                try:
                    from futures_correlation import get_portfolio_correlation as _get_corr
                    _open_short_syms = [p.symbol for p in _fc._open_positions.values()
                                        if not p.closed and p.side == "SHORT"]
                    if len(_open_short_syms) >= 2:
                        _avg_r, _corr_kelly = _get_corr(client, _open_short_syms)
                        if _corr_kelly < 1.0:
                            _f_usdt_pre = _f_usdt
                            _f_usdt = round(_f_usdt * _corr_kelly, 2)
                            print(f"[Futures/Corr] {symbol}: avg_|r|={_avg_r:.2f} → kelly_mult={_corr_kelly:.2f} "
                                  f"${_f_usdt_pre:.1f}→${_f_usdt:.1f}")
                except Exception as _corr_err:
                    logger.warning("[Futures/Corr] correlation guard failed for %s: %s", symbol, _corr_err)
                    _f_usdt = round(_f_usdt * 0.65, 2)

                try:
                    if news_client:
                        _fng = news_client.get_fear_greed()
                        _fng_val = int(_fng.get('value', 50)) if isinstance(_fng, dict) else 50
                        if _fng_val < 25:
                            _f_usdt = round(_f_usdt * 1.3, 2)
                            print(f"[Futures] F&G Extreme Fear ({_fng_val}) → boosted margin ${_f_usdt}")
                        elif _fng_val > 75:
                            _f_usdt = round(_f_usdt * 0.8, 2)
                            print(f"[Futures] F&G Extreme Greed ({_fng_val}) → reduced short margin ${_f_usdt}")
                except Exception:
                    pass
                # FIX-F3: Hard cap to available futures margin to prevent 'Insufficient margin' loops.
                # Keep a 2% reserve (min $2) so Binance doesn't reject with exact-balance orders.
                _avail_for_cap = _fc.get_available_usdt() if not _fc.dry_run else usdt_liquid * 0.3
                _avail_reserve = max(2.0, round(_avail_for_cap * 0.02, 2))
                _avail_net = _avail_for_cap - _avail_reserve
                if _avail_net < 10.0:
                    print(f'[Futures] FIX-F3: insufficient free margin ${_avail_net:.2f} (avail=${_avail_for_cap:.2f} - reserve=${_avail_reserve:.2f}) — skip')
                    update_signal_decision(symbol, 'rejected', 'INSUF_MARGIN')
                    continue
                if _f_usdt > _avail_net:
                    print(f'[Futures] FIX-F3: margin cap ${_f_usdt:.2f} → ${_avail_net:.2f} (available-reserve)')
                    _f_usdt = round(_avail_net, 2)
                _no_short_threshold = getattr(settings, 'futures_no_short_btc_4h_threshold', 2.0)
                if btc_4h_chg_pct >= _no_short_threshold:
                    print(f"[Futures] SHORT {symbol} SKIPPED: BTC 4h candle={btc_4h_chg_pct:+.2f}% >= {_no_short_threshold}% — no shorting into BTC momentum")
                    update_signal_decision(symbol, "rejected", "BTC_MOMENTUM")
                    continue
                # FIX-F2: Block new SHORTs if BTC is in an intraday bounce (1h candle)
                _no_short_btc_1h_threshold = getattr(settings, 'futures_no_short_btc_1h_threshold', 0.8)
                try:
                    _btc_1h_klines = client.client.get_klines(symbol='BTCUSDT', interval='1h', limit=2)
                    _btc_1h_chg = (float(_btc_1h_klines[-1][4]) - float(_btc_1h_klines[-1][1])) / float(_btc_1h_klines[-1][1]) * 100 if _btc_1h_klines else 0.0
                    if _btc_1h_chg >= _no_short_btc_1h_threshold:
                        print(f'[Futures] SHORT {symbol} SKIPPED: BTC 1h bounce={_btc_1h_chg:+.2f}% >= {_no_short_btc_1h_threshold}% — no new SHORTs in rally')
                        update_signal_decision(symbol, 'rejected', 'BTC_1H_BOUNCE')
                        continue
                except Exception:
                    print(f'[Futures] SHORT {symbol} SKIPPED: BTC 1h bounce check unavailable')
                    update_signal_decision(symbol, 'rejected', 'BTC_1H_BOUNCE_CHECK')
                    continue
                # PB5: BTC 3h rolling momentum filter — catches gradual rallies
                _no_short_btc_3h_threshold = getattr(settings, 'futures_no_short_btc_3h_threshold', 1.0)
                try:
                    _btc_3h_klines = client.client.get_klines(symbol='BTCUSDT', interval='1h', limit=4)  # 4 candles = last 3 completed + current
                    if _btc_3h_klines and len(_btc_3h_klines) >= 4:
                        _btc_3h_open = float(_btc_3h_klines[-4][1])  # open of 3h ago candle
                        _btc_3h_close = float(_btc_3h_klines[-1][4])  # close of current candle
                        _btc_3h_chg = (_btc_3h_close - _btc_3h_open) / _btc_3h_open * 100
                        if _btc_3h_chg >= _no_short_btc_3h_threshold:
                            print(f'[Futures/PB5] SHORT {symbol} SKIPPED: BTC 3h momentum={_btc_3h_chg:+.2f}% >= {_no_short_btc_3h_threshold}% — sustained rally, no new shorts')
                            update_signal_decision(symbol, 'rejected', 'BTC_3H_RALLY')
                            continue
                except Exception:
                    print(f'[Futures/PB5] SHORT {symbol} SKIPPED: BTC 3h momentum check unavailable')
                    update_signal_decision(symbol, 'rejected', 'BTC_3H_RALLY_CHECK')
                    continue
                # PB3: Confirm entry with red 5m candle — don't short into active green momentum
                _confirm_red = getattr(settings, 'futures_confirm_red_candle', True)
                if _confirm_red:
                    try:
                        _5m_klines = client.client.get_klines(symbol=symbol, interval='5m', limit=2)
                        if _5m_klines and len(_5m_klines) >= 2:
                            _last_5m = _5m_klines[-2]  # last COMPLETED 5m candle
                            _5m_open = float(_last_5m[1])
                            _5m_close = float(_last_5m[4])
                            if _5m_close > _5m_open:  # green candle = price rising
                                # Fallback: check BTC 5m instead
                                _btc_5m = client.client.get_klines(symbol='BTCUSDT', interval='5m', limit=2)
                                _btc_5m_close = float(_btc_5m[-2][4]) if _btc_5m and len(_btc_5m) >= 2 else 0
                                _btc_5m_open = float(_btc_5m[-2][1]) if _btc_5m and len(_btc_5m) >= 2 else 0
                                if _btc_5m_close > _btc_5m_open:
                                    print(f'[Futures/PB3] SHORT {symbol} SKIPPED: last 5m candle GREEN ({symbol} & BTC) — wait for red confirmation')
                                    update_signal_decision(symbol, 'rejected', 'NO_RED_CANDLE')
                                    continue
                    except Exception:
                        print(f'[Futures/PB3] SHORT {symbol} SKIPPED: red candle confirmation unavailable')
                        update_signal_decision(symbol, 'rejected', 'NO_RED_CANDLE_CHECK')
                        continue
                # PB1: Stagger check — max N new shorts per cycle
                if _pb_new_shorts_this_cycle >= _pb_max_new_per_cycle:
                    print(f'[Futures/PB1] SHORT {symbol} SKIPPED: max {_pb_max_new_per_cycle} new shorts/cycle reached — queued for next cycle')
                    update_signal_decision(symbol, 'rejected', 'STAGGER_LIMIT')
                    continue
                print(f"[Futures] FUTURES_SHORT mode: opening short {symbol} margin=${_f_usdt}")
                _f_peak_profile = str(_f_sig_meta.get('peak_quality_profile', '') or '').strip().upper()
                _f_peak_quality = _f_sig_meta.get('peak_quality')
                _pos = _fc.open_short(symbol, usdt_amount=_f_usdt,
                                      sl_pct=_f_sl, tp_pct=_f_tp, leverage=_f_lev,
                                      pnl_prediction=_f_prediction, indicators_at_entry=_f_indicators,
                                      signal_id=_signal_id,
                                      peak_quality_profile=_f_peak_profile if _f_peak_profile in ('A','B','C') else None,
                                      peak_quality_score=_f_peak_quality)
                if _pos:
                    _pb_new_shorts_this_cycle += 1
                    orders.append({
                        "symbol": symbol,
                        "side": "SHORT_FUTURES",
                        "qty": _pos.qty,
                        "entry_price": _pos.entry_price,
                        "sl": _pos.sl_price,
                        "tp": _pos.tp_price,
                        "entry_metadata": {
                            "regime_mode": _regime_mode,
                            "profile_label": _active_profile_label,
                            "peak_quality_profile": _f_sig_meta.get("peak_quality_profile"),
                            "peak_quality": _f_sig_meta.get("peak_quality"),
                            "expected_gain": _f_sig_meta.get("expected_gain"),
                            "success_probability": _f_sig_meta.get("success_probability"),
                            "signal_strength": _f_sig_meta.get("signal_strength"),
                            "predictor_prob": _f_prediction.get("success_probability") if _f_prediction else None,
                            "predictor_ev": _f_prediction.get("expected_pnl") if _f_prediction else None,
                        },
                    })
                    update_signal_decision(symbol, "executed", "FUTURES_SHORT")
                elif _pos is None and "-1121" in str(getattr(_fc, '_last_error', '')):
                    # Auto-learn: symbol invalid on Binance Futures — block permanently this session
                    _INVALID_FUTURES_SYMBOLS.add(symbol)
                    _persist_invalid_symbol_state(_db_client_ref)
                    print(f"[Futures] {symbol}: auto-added to _INVALID_FUTURES_SYMBOLS (-1121)")
                    update_signal_decision(symbol, "rejected", "INVALID_FUTURES_SYM")
                else:
                    update_signal_decision(symbol, "rejected", "FUTURES_BLOCKED")
                continue  # skip spot buy logic
            else:
                # futures_enabled=False but mode is FUTURES_SHORT → treat as PRESERVATION
                pass  # fall through to spot logic below (PRESERVATION path)

        # Enforce global MAX_POSITIONS limit (dynamic by regime)
        if position_manager:
            total_open = sum(len(v) for v in position_manager.get_all_positions().values())
            if total_open >= _regime_max:
                # — Compute prediction for this candidate (needed by replace + overflow) —
                _cand_meta     = signal_meta.get(symbol, {})
                _cand_strength = float(_cand_meta.get('strength', 0.0))
                if precomputed_predictions and symbol in precomputed_predictions:
                    _cand_pred = precomputed_predictions[symbol]
                else:
                    _cand_pred, _ = pnl_service.predict_for_symbol(symbol, client, settings,
                                                                    getattr(settings, 'max_per_trade_usdt', 500.0),
                                                                    trade_type='spot')

                # Step A: try active replacement of the weakest open position
                _replaced = _try_replace_weakest_position(
                    symbol, _cand_strength, _cand_pred,
                    position_manager, portfolio_manager, client, settings, _regime_mode
                )
                if _replaced:
                    print(f"[Replace] Slot freed — proceeding to buy {symbol}")
                    # fall through to normal buy logic below
                else:
                    # Step B: try overflow slot if signal quality is exceptional
                    if _is_overflow_eligible(symbol, _cand_strength, _cand_pred, settings):
                        _overflow_positions.add(symbol)
                        print(f"[Overflow] Opening extra slot for {symbol} "
                              f"(str={_cand_strength:.2f}, prob={float((_cand_pred or {}).get('success_probability',0)):.2f}). "
                              f"Overflow slots used: {len(_overflow_positions)}/{_get_param(settings,'overflow_max_extra')}")
                        # fall through to normal buy logic below
                    else:
                        print(f"Skipping buy {symbol}: regime max ({total_open}/{_regime_max}) reached, "
                              f"no replacement or overflow eligible")
                        update_signal_decision(symbol, "rejected", "MAX_POSIC")
                        continue
        # Enforce per-symbol MAX_POSITIONS_PER_SYMBOL limit
        # C2: Allow prediction-based add if new EV is significantly better
        if position_manager:
            count = position_manager.get_positions_count(symbol)
            max_per = _get_param(settings, 'max_positions_per_symbol')
            if count >= max_per:
                print(f"Skipping buy {symbol}: per-symbol limit ({count}/{max_per}) reached")
                update_signal_decision(symbol, "rejected", "LIM_SIMBOLO")
                continue
            if count >= 1 and count < 2:
                _allow_add = getattr(settings, 'allow_prediction_based_add', True)
                if _allow_add:
                    _meta_c2 = signal_meta.get(symbol, {})
                    _str_c2 = float(_meta_c2.get('strength', 0.0))
                    _min_str_add = getattr(settings, 'prediction_add_min_strength', 0.70)
                    if _str_c2 < _min_str_add:
                        print(f"Skipping buy {symbol}: {count} open position(s), strength={_str_c2:.2f} < {_min_str_add:.2f} for prediction_add")
                        update_signal_decision(symbol, "rejected", "ADD_STR_LOW")
                        continue
                    _existing_positions = position_manager.get_all_positions().get(symbol, [])
                    _existing_pred_pnl = None
                    if _existing_positions:
                        _ep_meta = getattr(_existing_positions[0], 'metadata', {}) or {}
                        _existing_pred_pnl = _ep_meta.get('entry_pred_pnl')
                    if _existing_pred_pnl is not None:
                        if precomputed_predictions and symbol in precomputed_predictions:
                            _new_pred_c2 = precomputed_predictions[symbol]
                        else:
                            _new_pred_c2, _ = pnl_service.predict_for_symbol(symbol, client, settings, allocation, trade_type='spot')
                        _new_ev = float((_new_pred_c2 or {}).get('expected_pnl', 0))
                        _ratio_req = getattr(settings, 'prediction_add_min_ev_ratio', 1.5)
                        if _existing_pred_pnl <= 0:
                            _ev_qualifies = _new_ev > 0
                        else:
                            _ev_qualifies = (_new_ev / _existing_pred_pnl) >= _ratio_req
                        if not _ev_qualifies:
                            print(f"Skipping buy {symbol}: {count} open position(s), new EV={_new_ev:.2f} not {_ratio_req}x better than existing EV={_existing_pred_pnl:.2f}")
                            update_signal_decision(symbol, "rejected", "ADD_EV_LOW")
                            continue
                        print(f"[PredAdd] {symbol}: {count} existing position(s), new EV={_new_ev:.2f} vs existing={_existing_pred_pnl:.2f} — allowing prediction_add (Kelly×{getattr(settings,'prediction_add_kelly_factor',0.6)})")
                    else:
                        print(f"Skipping buy {symbol}: {count} open position(s), no entry_pred_pnl to compare — blocked")
                        update_signal_decision(symbol, "rejected", "ADD_NO_BASELINE")
                        continue
                else:
                    print(f"Skipping buy {symbol}: {count} open position(s), prediction_add disabled")
                    update_signal_decision(symbol, "rejected", "LIM_SIMBOLO")
                    continue

        # ── R4: Correlaciones — límite por categoría ─────────────────────────
        _cat_ok, _cat_reason = _check_category_limit(symbol, position_manager, settings)
        if not _cat_ok:
            print(f"Skipping buy {symbol}: {_cat_reason}")
            update_signal_decision(symbol, "rejected", _cat_reason)
            continue

        # ── WR Screener: bloquear pares con N SL consecutivos recientes ─────────
        if position_manager and getattr(settings, 'wr_screener_enabled', True):
            _wr_n = int(getattr(settings, 'wr_screener_consecutive_sl_block', 5))
            if _wr_n > 0 and _consecutive_sl_count(symbol, position_manager, _wr_n):
                print(f"[WRScreener] Skipping {symbol}: {_wr_n} consecutive SL — pair on losing streak")
                update_signal_decision(symbol, "rejected", f"WR_BLOCK_{_wr_n}SL")
                try:
                    if _db_client_ref:
                        _db_client_ref.execute_update(
                            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (symbol, 'spot', 'wr_screener_block',
                             json.dumps({'consecutive_sl': _wr_n, 'regime': _regime_mode}), False),
                        )
                except Exception:
                    pass
                continue

        # ── A2: Regime gate — block SPOT entries when regime not confirmed ────
        if getattr(settings, 'spot_require_confirmed_regime', True):
            if _regime is None or not _regime.confirmed:
                print(f"Skipping buy {symbol}: regime not confirmed (mode={_regime_mode}) — A2 gate")
                update_signal_decision(symbol, "rejected", "REGIME_UNCONFIRMED")
                continue

        # ── B3: SPOT mass-SL cooldown gate ────────────────────────────────
        if _SPOT_MASS_SL_COOLDOWN_UNTIL and datetime.now(timezone.utc) < _SPOT_MASS_SL_COOLDOWN_UNTIL:
            _cd_rem = (_SPOT_MASS_SL_COOLDOWN_UNTIL - datetime.now(timezone.utc)).total_seconds() / 3600
            print(f'[SPOT/MASS-SL] {symbol}: SPOT cooldown active ({_cd_rem:.1f}h remaining) — no new spot buys')
            update_signal_decision(symbol, 'rejected', 'SPOT_MASS_SL_COOLDOWN')
            continue

        # ── A3: BTC correlation cap enforcement ─────────────────────────────
        if _btc_corr_max_new is not None and _spot_new_entries_this_cycle >= _btc_corr_max_new:
            print(f"Skipping buy {symbol}: BTC 1h={_btc_1h_drop_pct:+.2f}% drop — cycle entry cap {_btc_corr_max_new} reached — A3 gate")
            update_signal_decision(symbol, "rejected", "BTC_CORR_CAP")
            continue

        try:
            price = _get_cycle_price(client, symbol)
            filters = get_symbol_filters(client, symbol)
        except Exception as exc:
            print(f"Skip buy {symbol}: {exc}")
            update_signal_decision(symbol, "rejected", "ERROR_PRECIO")
            continue

        # ── R3: Microestructura — liquidez y slippage implícito ──────────────
        _vol_24h = float(signal_meta.get(symbol, {}).get('volume_24h_usdt', 0))
        _tp_pct_filter = float(signal_meta.get(symbol, {}).get('tp_pct', 0.025))
        _liq_ok, _liq_reason = _check_liquidity_and_slippage(symbol, _vol_24h, price, _tp_pct_filter, settings)
        if not _liq_ok:
            print(f"Skipping buy {symbol}: {_liq_reason}")
            update_signal_decision(symbol, "rejected", "LIQUIDEZ")
            continue

        # ── R6: On-chain — funding rate filter (spot long vs sobreapalancamiento) ─
        if (getattr(settings, 'funding_rate_spot_filter_enabled', True)
                and _regime_mode in ('SPOT_BULL', 'RANGE_SCALP')):
            _fr = _get_funding_rate(symbol, settings)
            _fr_max = getattr(settings, 'funding_rate_spot_max', 0.0008)
            if _fr is None:
                print(f"Skipping buy {symbol}: funding_rate unavailable — fail-safe block")
                update_signal_decision(symbol, "rejected", "FUNDING_RATE_UNAVAILABLE")
                continue
            if _fr > _fr_max:
                print(f"Skipping buy {symbol}: funding_rate={_fr:.4%} > {_fr_max:.4%} — longs sobreapalancados")
                update_signal_decision(symbol, "rejected", "FUNDING_RATE")
                continue

        # ── Bear mode (PRESERVATION/FUTURES_SHORT) — restrict non-NARRATIVE ──
        narrative_set = set(getattr(settings, 'narrative_tokens', []))
        if _bear_mode_active and _regime_mode == "PRESERVATION" and symbol not in narrative_set:
            _bear_min_spike = getattr(settings, 'bear_market_volspike_min', 2.0)
            _meta_spike = float(signal_meta.get(symbol, {}).get('vol_spike', 0))
            if _meta_spike < _bear_min_spike:
                print(f"Skipping buy {symbol}: PRESERVATION mode, not NARRATIVE, vol_spike={_meta_spike:.2f} < {_bear_min_spike}")
                update_signal_decision(symbol, "rejected", "PRESERVATION")
                continue

        # ── Market Regime gate — mode-specific entry filters ─────────────
        if _regime and _regime.confirmed:
            if _regime_mode == "PRESERVATION":
                # Only NARRATIVE tokens allowed in preservation mode
                if getattr(settings, 'preservation_narrative_only', True) and symbol not in narrative_set:
                    print(f"Skipping buy {symbol}: PRESERVATION mode — only NARRATIVE allowed")
                    update_signal_decision(symbol, "rejected", "PRESERVATION")
                    continue
                # RSI check for PRESERVATION
                _prsv_max_rsi = getattr(settings, 'preservation_max_rsi', 65.0)
                _meta_rsi = float(signal_meta.get(symbol, {}).get('rsi', 50))
                if _meta_rsi > _prsv_max_rsi:
                    print(f"Skipping buy {symbol}: PRESERVATION mode RSI={_meta_rsi:.1f} > {_prsv_max_rsi}")
                    update_signal_decision(symbol, "rejected", "PRSV_RSI")
                    continue

            elif _regime_mode == "RANGE_SCALP":
                # Only enter when RSI < range_scalp_max_rsi (oversold — near BB lower)
                _rs_max_rsi = getattr(settings, 'range_scalp_max_rsi', 60.0)  # s73 A2: fallback 48→60
                _meta_rsi   = float(signal_meta.get(symbol, {}).get('rsi', 50))
                if _meta_rsi > _rs_max_rsi:
                    print(f"Skipping buy {symbol}: RANGE_SCALP mode RSI={_meta_rsi:.1f} > {_rs_max_rsi} (not at BB lower)")
                    update_signal_decision(symbol, "rejected", "RANGE_RSI")
                    continue
                # B1: Check BB position — s70: accepts bb_near_lower; s72: also accepts bb_position <= range_scalp_max_bb_position (40%)
                if getattr(settings, 'range_scalp_require_bb_lower', True):
                    _sym_meta = signal_meta.get(symbol, {})
                    _meta_bb_touch = _sym_meta.get('bb_lower_touch', None)
                    _meta_bb_near  = _sym_meta.get('bb_near_lower', None)
                    _meta_bb_pos   = _sym_meta.get('bb_position', None)  # s72: (price-BBL)/(BBU-BBL)
                    _rs_max_bb_pos = float(getattr(settings, 'range_scalp_max_bb_position', 0.40))
                    _bb_pos_ok = (_meta_bb_pos is not None and float(_meta_bb_pos) <= _rs_max_bb_pos)
                    _bb_ok = bool(_meta_bb_near) or (_meta_bb_touch is not False) or _bb_pos_ok
                    if not _bb_ok and _meta_bb_touch is False:
                        print(f"Skipping buy {symbol}: RANGE_SCALP — price not near BB lower band (bb_pos={_meta_bb_pos})")
                        update_signal_decision(symbol, "rejected", "RANGE_BB")
                        continue
                # B2: s70 — minimum RSI floor to avoid free-fall entries
                _rs_min_rsi = float(getattr(settings, 'range_scalp_min_rsi', 25.0))
                if _meta_rsi < _rs_min_rsi:
                    print(f"Skipping buy {symbol}: RANGE_SCALP RSI={_meta_rsi:.1f} < {_rs_min_rsi} (free-fall guard)")
                    update_signal_decision(symbol, "rejected", "RANGE_RSI_FLOOR")
                    continue
                # LONG futuros: si precio en BB inferior + futures habilitado, abrir LONG en futuros
                if (getattr(settings, 'futures_enabled', True)
                        and _get_param(settings, 'futures_long_enabled')
                        and _get_param(settings, 'futures_long_in_range_scalp')
                        and (signal_meta.get(symbol, {}).get('bb_lower_touch', False)
                             or signal_meta.get(symbol, {}).get('bb_near_lower', False))):
                    _f_sym_list = list(_get_param(settings, 'futures_symbols'))
                    if screened_pairs_cache:
                        _f_sym_list = list(dict.fromkeys(_f_sym_list + screened_pairs_cache.get("futures", [])))
                    elif getattr(settings, 'screener_enabled', True):
                        try:
                            from pair_screener import get_screened_pairs
                            _f_sym_list = list(dict.fromkeys(
                                _f_sym_list + get_screened_pairs(
                                    settings=settings,
                                    regime_mode=_regime_mode,
                                    profile_label=_active_profile_label,
                                    db_client=_db_client_ref,
                                ).get("futures", [])
                            ))
                        except Exception:
                            pass
                    if symbol in _f_sym_list:
                        _fc_l = _get_futures_client(settings, injected_client=futures_client)
                        _l_usdt = getattr(settings, 'futures_usdt_per_trade', 150.0)
                        _l_sl   = getattr(settings, 'futures_long_sl_pct', 2.5)
                        _l_tp   = getattr(settings, 'futures_long_tp_pct', 5.0)
                        _l_lev  = getattr(settings, 'futures_leverage', 10)
                        print(f"[Futures] RANGE_SCALP BB-lower: opening LONG {symbol} margin=${_l_usdt}")
                        _l_sig_meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}
                        _l_peak_profile = str(_l_sig_meta.get('peak_quality_profile', '') or '').strip().upper()
                        _l_peak_quality = _l_sig_meta.get('peak_quality')
                        _lpos = _fc_l.open_long(symbol, usdt_amount=_l_usdt,
                                                sl_pct=_l_sl, tp_pct=_l_tp, leverage=_l_lev,
                                                signal_id=_signal_id,
                                                peak_quality_profile=_l_peak_profile if _l_peak_profile in ('A','B','C') else None,
                                                peak_quality_score=_l_peak_quality)
                        if _lpos:
                            orders.append({
                                "symbol": symbol,
                                "side": "LONG_FUTURES",
                                "qty": _lpos.qty,
                                "entry_price": _lpos.entry_price,
                                "sl": _lpos.sl_price,
                                "tp": _lpos.tp_price,
                                "entry_metadata": {
                                    "regime_mode": _regime_mode,
                                    "profile_label": _active_profile_label,
                                    "peak_quality_profile": _l_sig_meta.get("peak_quality_profile"),
                                    "peak_quality": _l_sig_meta.get("peak_quality"),
                                    "expected_gain": _l_sig_meta.get("expected_gain"),
                                    "success_probability": _l_sig_meta.get("success_probability"),
                                    "signal_strength": _l_sig_meta.get("signal_strength"),
                                    "predictor_prob": _l_prediction.get("success_probability") if _l_prediction else None,
                                    "predictor_ev": _l_prediction.get("expected_pnl") if _l_prediction else None,
                                },
                            })
                            update_signal_decision(symbol, "executed", "FUTURES_LONG")

        # ── SPOT_BULL + RSI oversold: LONG futuros más eficiente que spot ──
        if (_regime_mode == "SPOT_BULL" and _regime is not None and _regime.confirmed
                and getattr(settings, 'futures_enabled', True)
                and _get_param(settings, 'futures_long_enabled')
                and _get_param(settings, 'futures_long_in_spot_bull')):
            _l_rsi_thresh = _get_param(settings, 'futures_long_rsi_threshold')
            _l_rsi_val    = float(signal_meta.get(symbol, {}).get('rsi', 50))
            if _l_rsi_val < _l_rsi_thresh and btc_trend == 'UP':
                _l_sym_list = list(_get_param(settings, 'futures_symbols'))
                if screened_pairs_cache:
                    _l_sym_list = list(dict.fromkeys(_l_sym_list + screened_pairs_cache.get("futures", [])))
                elif getattr(settings, 'screener_enabled', True):
                    try:
                        from pair_screener import get_screened_pairs
                        _l_sym_list = list(dict.fromkeys(
                            _l_sym_list + get_screened_pairs(
                                settings=settings,
                                regime_mode=_regime_mode,
                                profile_label=_active_profile_label,
                                db_client=_db_client_ref,
                            ).get("futures", [])
                        ))
                    except Exception:
                        pass
                if symbol in _l_sym_list:
                    _fc_sb = _get_futures_client(settings, injected_client=futures_client)
                    _l_sl   = getattr(settings, 'futures_long_sl_pct', 2.5)
                    _l_tp   = getattr(settings, 'futures_long_tp_pct', 5.0)
                    _l_lev  = getattr(settings, 'futures_leverage', 10)
                    # MEJO-D: Kelly sizing para LONG (igual que SHORT), cap futures_usdt_per_trade
                    _l_pred_sb = {'success_probability': 0.55}  # oversold en uptrend = mayor prob
                    # UnifiedPM: use futures wallet as Kelly reference when enabled
                    _l_base_ov = _l_max_ov = _l_cap_ov = None
                    if _get_param(settings, 'use_unified_portfolio_manager') and portfolio_manager:
                        try:
                            _unified_long = portfolio_manager.get_unified_balance(_fc_sb)
                            _l_kelly_liq = _unified_long.get('futures_available_after_reserve', _unified_long.get('futures_available', usdt_liquid))
                            _l_base_ov = _get_param(settings, 'futures_kelly_base_usdt')
                            _l_max_ov  = _get_param(settings, 'futures_kelly_max_usdt')
                            _l_cap_ov  = _get_param(settings, 'futures_kelly_max_pct_liquid')
                        except Exception:
                            _l_kelly_liq = usdt_liquid
                    else:
                        _l_kelly_liq = usdt_liquid
                    _l_open = len([
                        _lp for _lp in _fc_sb._open_positions.values()
                        if not _lp.closed and _lp.side == 'LONG'
                    ])
                    _l_eff_slots = _get_max_positions_for_regime(settings, _regime_mode)
                    _l_conc_mult, _l_conc_diag = _compute_portfolio_concentration_multiplier(
                        settings,
                        regime=_regime_mode,
                        trade_type='futures',
                        open_slots=_l_open,
                        effective_slots=_l_eff_slots,
                    )
                    _l_usdt = _kelly_position_size(
                        settings, strength=float(signal_meta.get(symbol, {}).get('strength', 0.5)),
                        prediction=_l_pred_sb, usdt_liquid=_l_kelly_liq,
                        # Kelly reward/risk must reflect price movement (not leveraged margin), so divide by leverage
                        sl_pct=_l_sl / _l_lev / 100, tp_pct=_l_tp / _l_lev / 100,
                        base_override=_l_base_ov, max_override=_l_max_ov,
                        cap_pct_override=_l_cap_ov,
                        concentration_multiplier=_l_conc_mult,
                        db_client=_db_client_ref, symbol=symbol,
                        trade_type='futures', regime=_regime_mode,
                        extra_multipliers={'portfolio_concentration': _l_conc_diag},
                    )
                    if _get_param(settings, 'kelly_conviction_enabled'):
                        _l_vs = float(signal_meta.get(symbol, {}).get('vol_spike', 0.0))
                        _l_conv3x = _get_param(settings, 'kelly_conviction_vol3x')
                        _l_conv2x = _get_param(settings, 'kelly_conviction_vol2x')
                        _l_conv_max_pct = _get_param(settings, 'kelly_conviction_max_pct')
                        _l_conv_mult = _l_conv3x if _l_vs >= 3.0 else (_l_conv2x if _l_vs >= 2.0 else 1.0)
                        if _l_conv_mult > 1.0:
                            _l_hard_cap = float(_l_kelly_liq) * float(_l_conv_max_pct)
                            _l_boosted = min(_l_usdt * _l_conv_mult, _l_hard_cap)
                            print(f"[M3/LF] {symbol}: vol_spike={_l_vs:.1f}x → conviction ×{_l_conv_mult} ${_l_usdt:.2f}→${_l_boosted:.2f} (cap=${_l_hard_cap:.2f})")
                            _l_usdt = _l_boosted
                    # Apply F&G boost (oversold + Fear = strong conviction entry)
                    _l_usdt = _apply_fng_kelly_boost(_l_usdt, _fng_value, settings)
                    # F4: dynamic cap — max(per_trade_floor, futures_margin × kelly_cap_pct)
                    _l_usdt_cap = max(
                        getattr(settings, 'futures_usdt_per_trade', 150.0),
                        _l_kelly_liq * getattr(settings, 'futures_kelly_cap_pct', 0.30)
                    )
                    _l_usdt = min(_l_usdt, _l_usdt_cap)
                    print(f"[Futures] SPOT_BULL oversold LONG {symbol}: RSI={_l_rsi_val:.1f}<{_l_rsi_thresh}, BTC={btc_trend}, margin=${_l_usdt:.2f}")
                    _l_sig_meta = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}
                    _l_peak_profile = str(_l_sig_meta.get('peak_quality_profile', '') or '').strip().upper()
                    _l_peak_quality = _l_sig_meta.get('peak_quality')
                    _lpos_sb = _fc_sb.open_long(symbol, usdt_amount=_l_usdt,
                                                sl_pct=_l_sl, tp_pct=_l_tp, leverage=_l_lev,
                                                signal_id=_signal_id,
                                                peak_quality_profile=_l_peak_profile if _l_peak_profile in ('A','B','C') else None,
                                                peak_quality_score=_l_peak_quality)
                    if _lpos_sb:
                        orders.append({
                            "symbol": symbol,
                            "side": "LONG_FUTURES",
                            "qty": _lpos_sb.qty,
                            "entry_price": _lpos_sb.entry_price,
                            "sl": _lpos_sb.sl_price,
                            "tp": _lpos_sb.tp_price,
                            "entry_metadata": {
                                "regime_mode": _regime_mode,
                                "profile_label": _active_profile_label,
                                "peak_quality_profile": _l_sig_meta.get("peak_quality_profile"),
                                "peak_quality": _l_sig_meta.get("peak_quality"),
                                "expected_gain": _l_sig_meta.get("expected_gain"),
                                "success_probability": _l_sig_meta.get("success_probability"),
                                "signal_strength": _l_sig_meta.get("signal_strength"),
                                "predictor_prob": _l_prediction.get("success_probability") if _l_prediction else None,
                                "predictor_ev": _l_prediction.get("expected_pnl") if _l_prediction else None,
                            },
                        })
                        update_signal_decision(symbol, "executed", "FUTURES_LONG_BULL")

        # ── Exclusion list — gold tokens, stablecoins, artificially-volatile ─
        _excluded_syms = set(getattr(settings, 'trading_excluded_symbols', []))
        if symbol in _excluded_syms:
            print(f"Skipping buy {symbol}: in trading_excluded_symbols list")
            update_signal_decision(symbol, "rejected", "EXCLUIDO")
            continue

        # ── BTC dominance filter — restrict non-NARRATIVE when BTC.D high ─
        if _btc_dom_high and symbol not in narrative_set:
            print(f"Skipping buy {symbol}: BTC.D high — only NARRATIVE allowed")
            update_signal_decision(symbol, "rejected", "BTC_DOM")
            continue

        # ── NARRATIVE token buy cooldown ─────────────────────────────────
        if symbol in narrative_set:
            from datetime import datetime as _dt2
            _last_buy = _NARRATIVE_BUY_COOLDOWN.get(symbol)
            if not _last_buy and _check_cooldown_db(symbol, 'narrative'):
                # H2: DB says cooldown active but RAM is empty (post-restart) — block directly
                print(f"Skipping buy {symbol}: NARRATIVE cooldown active (from DB — post-restart)")
                update_signal_decision(symbol, "rejected", "NARRATIVE_CD")
                continue
            if _last_buy:
                _h_since = (_dt2.now(timezone.utc) - _last_buy).total_seconds() / 3600
                if _h_since < _NARRATIVE_BUY_COOLDOWN_HOURS:
                    print(f"Skipping buy {symbol}: NARRATIVE cooldown active ({_h_since:.1f}h < {_NARRATIVE_BUY_COOLDOWN_HOURS}h)")
                    update_signal_decision(symbol, "rejected", "NARRATIVE_CD")
                    continue

        # ── Signal metadata ───────────────────────────────────────────────
        meta = signal_meta.get(symbol, {})
        strength = float(meta.get('strength', 0.0))

        # ── PnL prediction (needed for Kelly sizing) ───────────────────────
        if precomputed_predictions and symbol in precomputed_predictions:
            prediction = precomputed_predictions[symbol]
            formatted_indicators = None
        else:
            prediction, formatted_indicators = pnl_service.predict_for_symbol(
                symbol, client, settings, allocation, trade_type='spot'
            )
        # ── SPOT_BULL PnL boost — relax predictor thresholds in confirmed uptrend ─
        _pnl_boosted_settings = settings
        if (getattr(settings, 'spot_bull_pnl_boost_enabled', True)
                and _regime_mode == "SPOT_BULL"
                and _regime is not None and _regime.confirmed
                and (not getattr(settings, 'spot_bull_btc_4h_up_required', True) or btc_trend == 'UP')):
            _large_cap_set = {'BTCUSDT', 'ETHUSDT', 'BNBUSDT'}
            _boost_pnl = (getattr(settings, 'spot_bull_pnl_boost_large_cap', 0.0)
                          if symbol in _large_cap_set
                          else getattr(settings, 'spot_bull_pnl_boost_midcap', -0.10))
            _boost_prob_delta = (float(getattr(settings, 'spot_bull_pnl_boost_prob_large_cap', -0.02))
                                 if symbol in _large_cap_set
                                 else float(getattr(settings, 'spot_bull_pnl_boost_prob_midcap', -0.04)))
            _cat_th = getattr(settings, 'category_thresholds', {})
            _sym_cat = getattr(settings, 'token_categories', {}).get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
            _cur_min_pnl = _cat_th.get(_sym_cat, {}).get('min_pnl', 0.0)
            _cur_min_prob = float(_cat_th.get(_sym_cat, {}).get('min_prob', _get_param(settings, 'min_success_probability')))
            _boosted_min_prob = max(0.45, _cur_min_prob + _boost_prob_delta)
            if (_boost_pnl < _cur_min_pnl) or (_boosted_min_prob < _cur_min_prob):
                import copy
                _pnl_boosted_settings = copy.copy(settings)
                _new_th = copy.deepcopy(_cat_th)
                if _sym_cat not in _new_th:
                    _new_th[_sym_cat] = {}
                _new_th[_sym_cat]['min_pnl'] = min(_boost_pnl, _cur_min_pnl)
                _new_th[_sym_cat]['min_prob'] = min(_boosted_min_prob, _cur_min_prob)
                _pnl_boosted_settings.category_thresholds = _new_th
                print(
                    f"[SPOT_BULL boost] {symbol} ({_sym_cat}): "
                    f"min_pnl {_cur_min_pnl:.2f}→{_new_th[_sym_cat]['min_pnl']:.2f}, "
                    f"min_prob {_cur_min_prob:.2f}→{_new_th[_sym_cat]['min_prob']:.2f} "
                    f"(BTC 4h={btc_4h_chg_pct:+.2f}%)"
                )
        is_valid, reason = pnl_service.validate(prediction, _pnl_boosted_settings, symbol=symbol, news_client=news_client)
        # ── Peaks-first advisory mode: Profile A/B bypass predictor gate ──────
        _predictor_gate_mode = str(getattr(settings, 'predictor_gate_mode', 'blocking')).lower()
        _sig_peak_profile = str(signal_meta.get(symbol, {}).get('peak_quality_profile', '')).upper()
        _peaks_bypass = False
        if not is_valid and _predictor_gate_mode == "advisory" and _sig_peak_profile in ("A", "B"):
            _peaks_bypass = True
            _gate_warning = {
                "gate": "predictor_pnl",
                "reason": reason,
                "expected_pnl": float((prediction or {}).get('expected_pnl', 0)),
                "success_probability": float((prediction or {}).get('success_probability', 0)),
                "peak_profile": _sig_peak_profile,
                "action": "advisory_bypass",
            }
            # Collect warning in signal_meta for later DB persistence
            if symbol in signal_meta and isinstance(signal_meta[symbol], dict):
                signal_meta[symbol].setdefault('gate_warnings', []).append(_gate_warning)
            print(f"[PeaksFirst] {symbol}: predictor VETOED ({reason}) but Profile={_sig_peak_profile} → ADVISORY BYPASS")
        elif not is_valid and _predictor_gate_mode == "disabled":
            _peaks_bypass = True
            print(f"[PeaksFirst] {symbol}: predictor gate DISABLED → bypass")
        if not is_valid and not _peaks_bypass:
            ep = prediction.get('expected_pnl', 0) if prediction else 0
            sp = prediction.get('success_probability', 0) if prediction else 0
            # ── VolSpike override for narrative tokens ─────────────────────
            volspike_override = False
            if getattr(settings, 'volspike_override_enabled', True):
                narrative_set = set(getattr(settings, 'narrative_tokens', []))
                only_narrative = getattr(settings, 'volspike_override_narrative_only', True)
                if not only_narrative or symbol in narrative_set:
                    try:
                        _kl = client.client.get_klines(symbol=symbol, interval='4h', limit=26)
                        _closes = [float(k[4]) for k in _kl]
                        _vols   = [float(k[5]) for k in _kl]
                        _avg_vol = sum(_vols[:-1]) / max(len(_vols) - 1, 1)
                        _spike   = _vols[-1] / _avg_vol if _avg_vol > 0 else 0
                        # RSI 14 on 4h closes
                        _gains = _losses = 0.0
                        for _i in range(1, 15):
                            _d = _closes[_i] - _closes[_i - 1]
                            if _d > 0: _gains += _d
                            else: _losses += -_d
                        _ag = _gains / 14; _al = _losses / 14
                        _rsi = 100 - 100 / (1 + _ag / _al) if _al > 0 else 100
                        # EMA9 vs EMA21 for 4h trend
                        _k9 = 2/10; _k21 = 2/22; _e9 = _closes[0]; _e21 = _closes[0]
                        for _c in _closes[1:]: _e9 = _c*_k9 + _e9*(1-_k9); _e21 = _c*_k21 + _e21*(1-_k21)
                        _trend_up = _e9 > _e21
                        _min_spike = getattr(settings, 'volspike_override_min_spike', 2.0)
                        _max_rsi   = min(getattr(settings, 'volspike_override_max_rsi', 65.0), 75.0)  # A1: hard cap at 75 — no override in overbought (OPGUSDT RSI=87.77 case)
                        _need_trend = getattr(settings, 'volspike_override_trend_required', True)
                        # ── Funding Rate filter ────────────────────────────────
                        _funding_ok = True
                        try:
                            _fr_val = _get_funding_rate(symbol, settings)
                            if _fr_val is None:
                                _funding_ok = False
                                print(f"VolSpike OVERRIDE {symbol}: funding check unavailable — block entry")
                            elif _fr_val > 0.0005:  # > 0.05% = longs muy apalancados → peligro
                                _funding_ok = False
                                print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} > 0.05% — skip (overleveraged longs)")
                            else:
                                print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} ✓")
                        except Exception as _fr_err:
                            _funding_ok = False
                            print(f"VolSpike OVERRIDE {symbol}: funding check error ({_fr_err}) — block entry")
                        _meta_rsi_override = float(signal_meta.get(symbol, {}).get('rsi', 50.0))
                        if _meta_rsi_override >= _max_rsi and _spike >= _min_spike:
                            print(f"VolSpike OVERRIDE {symbol}: RSI_1h={_meta_rsi_override:.1f} >= {_max_rsi} — bloqueado (sobrecomprado en 1h)")
                        if (_spike >= _min_spike and _rsi < _max_rsi
                                and _meta_rsi_override < _max_rsi
                                and (not _need_trend or _trend_up) and _funding_ok):
                            # V2: Validaciones adicionales más selectivas (jun 16 2026)
                            # Check 1: Strength mínimo requerido
                            _min_strength = getattr(settings, 'volspike_override_min_strength', 0.75)
                            if strength < _min_strength:
                                print(f"VolSpike OVERRIDE {symbol}: rechazado - strength {strength:.2f} < {_min_strength} (señal débil)")
                                continue  # Skip this symbol, don't mark as override
                            
                            # Check 2: Límite de posiciones override simultáneas
                            _max_override_pos = getattr(settings, 'volspike_override_max_positions', 1)
                            if len(_VOLSPIKE_POSITIONS) >= _max_override_pos:
                                print(f"VolSpike OVERRIDE {symbol}: rechazado - límite de {_max_override_pos} posiciones override alcanzado ({len(_VOLSPIKE_POSITIONS)} activas)")
                                continue
                            
                            # Check 3: Límite diario
                            from datetime import datetime as _dt  # BUG-1: import before first use
                            _daily_limit = getattr(settings, 'volspike_override_daily_limit', 1)
                            _today_str = _dt.now(timezone.utc).strftime('%Y-%m-%d')
                            _today_count = _OVERRIDE_DAILY_COUNT.get(_today_str, 0)
                            if _today_count >= _daily_limit:
                                print(f"VolSpike OVERRIDE {symbol}: rechazado - límite diario de {_daily_limit} alcanzado ({_today_count} hoy)")
                                continue
                            
                            # Check cooldown — no re-entry within 4h of last override
                            _last = _VOLSPIKE_COOLDOWN.get(symbol)
                            _now_utc = _dt.now(timezone.utc)
                            if not _last and _check_cooldown_db(symbol, 'volspike'):
                                print(f"VolSpike OVERRIDE {symbol}: cooldown active (from DB — post-restart) — skip")
                                continue
                            if _last:
                                _hours_since = (_now_utc - _last).total_seconds() / 3600
                                if _hours_since < _VOLSPIKE_COOLDOWN_HOURS:
                                    print(f"VolSpike OVERRIDE {symbol}: cooldown active ({_hours_since:.1f}h < {_VOLSPIKE_COOLDOWN_HOURS}h) — skip")
                                else:
                                    # Check max 1 open override position for this symbol
                                    _open_count = position_manager.get_positions_count(symbol) if position_manager else 0
                                    if _open_count >= 1:
                                        print(f"VolSpike OVERRIDE {symbol}: already {_open_count} open position(s) — skip")
                                    else:
                                        volspike_override = True
                                        _set_cooldown(symbol, 'volspike', _VOLSPIKE_COOLDOWN_HOURS, {'spike': _spike, 'rsi': _rsi})
                                        _VOLSPIKE_POSITIONS.add(symbol)
                                        _OVERRIDE_DAILY_COUNT[_today_str] = _today_count + 1
                                        _set_cooldown(f'__daily__{_today_str}', 'override_daily', 24.0, {'count': _today_count + 1})
                                        print(f"VolSpike OVERRIDE {symbol}: ✅ APROBADO (strength={strength:.2f}, spike={_spike:.2f}x, RSI4h={_rsi:.1f}) → bypassing PnL veto (pred={ep:.2f}, prob={sp:.2f})")
                                        print(f"VolSpike OVERRIDE {symbol}: Stats - posiciones override: {len(_VOLSPIKE_POSITIONS)}/{_max_override_pos}, hoy: {_today_count + 1}/{_daily_limit}")
                            else:
                                # First time — check no open position exists
                                _open_count = position_manager.get_positions_count(symbol) if position_manager else 0
                                if _open_count >= 1:
                                    print(f"VolSpike OVERRIDE {symbol}: already {_open_count} open position(s) — skip")
                                else:
                                    volspike_override = True
                                    _set_cooldown(symbol, 'volspike', _VOLSPIKE_COOLDOWN_HOURS, {'spike': _spike, 'rsi': _rsi})
                                    _VOLSPIKE_POSITIONS.add(symbol)
                                    _OVERRIDE_DAILY_COUNT[_today_str] = _today_count + 1
                                    _set_cooldown(f'__daily__{_today_str}', 'override_daily', 24.0, {'count': _today_count + 1})
                                    print(f"VolSpike OVERRIDE {symbol}: ✅ APROBADO (strength={strength:.2f}, spike={_spike:.2f}x, RSI4h={_rsi:.1f}) → bypassing PnL veto (pred={ep:.2f}, prob={sp:.2f})")
                                    print(f"VolSpike OVERRIDE {symbol}: Stats - posiciones override: {len(_VOLSPIKE_POSITIONS)}/{_max_override_pos}, hoy: {_today_count + 1}/{_daily_limit}")
                    except Exception as _ve:
                        print(f"VolSpike override check failed for {symbol}: {_ve}")
            if not volspike_override:
                print(f"Skipping buy {symbol}: {reason} (pred={ep:.2f} USDT, prob={sp:.2f})")
                update_signal_decision(symbol, "rejected", "PNL_BAJO")
                continue
        if prediction and (is_valid or _peaks_bypass):
            print(f"Prediction {'OK' if is_valid else 'BYPASSED(Peaks)'} for {symbol}: {prediction['expected_pnl']:.2f} USDT, prob={prediction['success_probability']:.2f}")

        # ── CoinGecko Trending boost ────────────────────────────────────
        if news_client:
            try:
                if news_client.is_trending(symbol):
                    strength = min(1.0, strength + 0.15)
                    print(f"CoinGecko Trending BOOST {symbol}: strength → {strength:.2f}")
            except Exception:
                pass

        # ── Kelly dynamic position sizing ───────────────────────────────
        # Fase E: SL/TP base from category_thresholds (per token type) — fallback to global settings
        _sym_cat_e = getattr(settings, 'token_categories', {}).get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
        _cat_th_e  = getattr(settings, 'category_thresholds', {}).get(_sym_cat_e, {})
        sl_pct = _cat_th_e.get('stop_loss_pct', _get_param(settings, 'stop_loss_pct')) / 100.0
        tp_pct = _cat_th_e.get('take_profit_pct', _get_param(settings, 'take_profit_pct')) / 100.0
        # M1: ATR-Based SL — Kelly sizes trade against real volatility, not fixed %
        sl_pct = _calc_atr_sl_pct(client, symbol, settings, sl_pct)

        # Apply regime-specific SL/TP overrides (régimen sobreescribe categoría)
        if _regime and _regime.confirmed:
            if _regime_mode == "RANGE_SCALP":
                sl_pct = getattr(settings, 'range_scalp_sl_pct', 0.8) / 100.0
                tp_pct = getattr(settings, 'range_scalp_tp_pct', 1.8) / 100.0
                print(f"[Regime] RANGE_SCALP: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")
            elif _regime_mode == "PRESERVATION":
                sl_pct = _get_param(settings, 'preservation_sl_pct') / 100.0
                tp_pct = _get_param(settings, 'preservation_tp_pct') / 100.0
                print(f"[Regime] PRESERVATION: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")
            else:
                print(f"[Kelly] {symbol} ({_sym_cat_e}): SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

        _spot_open_slots = 0
        if position_manager:
            try:
                _spot_open_slots = sum(len(v) for v in position_manager.get_all_positions().values())
            except Exception:
                _spot_open_slots = 0
        _spot_conc_mult, _spot_conc_diag = _compute_portfolio_concentration_multiplier(
            settings,
            regime=_regime_mode,
            trade_type='spot',
            open_slots=_spot_open_slots,
            effective_slots=_regime_max,
        )

        # Predictive profile assignment: A = ultra-Kelly, B = super-Kelly, C = aggressive-Kelly
        _kelly_profile = _assign_kelly_profile(strength, prediction, signal_meta, symbol)
        _profile_ovr = _PROFILE_OVERRIDES.get(_kelly_profile, _PROFILE_OVERRIDES["C"])
        print(f"[PredictiveProfile] {symbol}: profile={_kelly_profile}, strength={strength:.2f}, "
              f"prob={prediction.get('success_probability', 0):.2f}")

        trade_allocation = _kelly_position_size(
            settings, strength, prediction, usdt_liquid_now, sl_pct, tp_pct,
            base_override=_profile_ovr["base_usdt"],
            max_override=_profile_ovr["max_usdt"],
            cap_pct_override=_profile_ovr["cap_pct"],
            concentration_multiplier=_spot_conc_mult,
            db_client=_db_client_ref, symbol=symbol,
            trade_type='spot', regime=_regime_mode,
            extra_multipliers={
                'portfolio_concentration': _spot_conc_diag,
                'predictive_profile': _kelly_profile,
            },
        )
        # C2: Kelly reducido para prediction_add (segunda entrada en mismo par)
        if position_manager and position_manager.get_positions_count(symbol) >= 1:
            _pred_add_factor = getattr(settings, 'prediction_add_kelly_factor', 0.6)
            trade_allocation *= _pred_add_factor
            print(f"[PredAdd] {symbol}: Kelly×{_pred_add_factor} for prediction_add → ${trade_allocation:.2f}")
        # Regime Kelly multiplier — single application, no double-counting
        if _regime and _regime.confirmed and _regime_mode != "SPOT_BULL":
            _rmult = _regime.kelly_multiplier
            trade_allocation *= _rmult
            print(f"[Regime] {_regime_mode}: Kelly ×{_rmult} → ${trade_allocation:.2f} for {symbol}")

        # ── M3: Kelly conviction multiplier ─────────────────────────────────
        # Scale position size when signal has high vol_spike conviction
        if getattr(settings, 'kelly_conviction_enabled', True):
            _meta_vs = float(signal_meta.get(symbol, {}).get('vol_spike', 0))
            _conv3x = getattr(settings, 'kelly_conviction_vol3x', 2.5)
            _conv2x = getattr(settings, 'kelly_conviction_vol2x', 1.8)
            _conv_max_pct = getattr(settings, 'kelly_conviction_max_pct', 0.25)
            if _meta_vs >= 3.0:
                _conv_mult = _conv3x
            elif _meta_vs >= 2.0:
                _conv_mult = _conv2x
            else:
                _conv_mult = 1.0
            if _conv_mult > 1.0:
                _total_portfolio_val = float(client.get_account_balance().get("USDT", usdt_liquid_now)) + sum(
                    float(p.quantity) * _get_cycle_price(client, s)
                    for s, pl in (position_manager.get_all_positions() if position_manager else {}).items()
                    for p in pl if True
                ) if position_manager else usdt_liquid_now * 4
                _hard_cap = _total_portfolio_val * _conv_max_pct
                _boosted = min(trade_allocation * _conv_mult, _hard_cap)
                print(f"[M3] {symbol}: vol_spike={_meta_vs:.1f}x → conviction ×{_conv_mult} ${trade_allocation:.2f}→${_boosted:.2f} (cap=${_hard_cap:.2f})")
                trade_allocation = _boosted

        # ── R1: Behavioral Finance — F&G Kelly boost ─────────────────────────
        trade_allocation = _apply_fng_kelly_boost(trade_allocation, _fng_value, settings)

        qty = align_quantity_to_step(trade_allocation / price, filters["step"])
        _signal_id = signal_meta.get(symbol, {}).get('signal_id') if isinstance(signal_meta, dict) else None
        _entry_notional = qty * price
        _spot_min_entry_notional = max(10.0, float(getattr(settings, 'spot_min_entry_notional_usdt', 10.0)))
        if _entry_notional < _spot_min_entry_notional:
            print(
                f"Skipping buy {symbol}: entry notional ${_entry_notional:.4f} < "
                f"${_spot_min_entry_notional:.2f} minimum"
            )
            update_signal_decision(symbol, "rejected", "NOTIONAL_MINIMO", signal_id=_signal_id)
            continue

        valid, reason = is_quantity_valid(qty, price, filters)

        if not valid:
            print(f"Skipping buy {symbol}: {reason}")
            update_signal_decision(symbol, "rejected", "QTY_INVALIDA", signal_id=_signal_id)
            continue

        # Guard: verify liquid USDT is sufficient before placing order
        # Use real spot commission rate + 0.05% slippage buffer
        _comm_rate = _get_commission_config(settings).rate_for('spot')
        cost_estimate = qty * price * (1.0 + _comm_rate + 0.0005)
        if usdt_liquid_now < cost_estimate:
            print(f"Skipping buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f} needed)")
            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                         f"Skipped buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f})")
            update_signal_decision(symbol, "rejected", "SIN_SALDO", signal_id=_signal_id)
            continue

        try:
            # Register NARRATIVE cooldown BEFORE placing order to prevent race condition
            if symbol in narrative_set:
                _set_cooldown(symbol, 'narrative', _NARRATIVE_BUY_COOLDOWN_HOURS)

            order = client.create_market_order(symbol=symbol, side="BUY", quantity=qty)
            _fill = client.verify_and_fetch_fills(order, f"BUY {symbol}")
            if not _fill:
                print(f"[WARN] BUY {symbol}: order NOT confirmed filled — skipping position record")
                continue
            _record_order_success()  # R5: reset circuit breaker on success
            # FIX-8: use real executed qty and commission from Binance fill
            _executed_qty = _fill.get('qty', 0.0)
            if _executed_qty > 0 and abs(_executed_qty - qty) / max(qty, 1e-12) > 0.01:
                print(f"[FIX-8] BUY {symbol}: requested={qty} executed={_executed_qty} (using executed)")
                qty = _executed_qty
            _binance_confirmed = True
            _real_commission = _fill.get('commission', 0.0)
            orders.append({
                "symbol": symbol,
                "side": "BUY",
                "quantity": qty,
                "order": order,
                "entry_metadata": {
                    "regime_mode": _regime_mode,
                    "profile_label": _active_profile_label,
                    "peak_quality_profile": signal_meta.get(symbol, {}).get("peak_quality_profile"),
                    "peak_quality": signal_meta.get(symbol, {}).get("peak_quality"),
                    "expected_gain": signal_meta.get(symbol, {}).get("expected_gain"),
                    "success_probability": signal_meta.get(symbol, {}).get("success_probability"),
                    "signal_strength": signal_meta.get(symbol, {}).get("signal_strength"),
                    "predictor_prob": prediction.get("success_probability") if prediction else None,
                    "predictor_ev": prediction.get("expected_pnl") if prediction else None,
                },
            })
            usdt_liquid_now = max(0.0, usdt_liquid_now - qty * price)  # BUG-E5: local deduction, avoids re-fetch
            _spot_new_entries_this_cycle += 1  # A3: track entries for BTC correlation cap
            print(f"Bought {qty} {symbol}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_INFO, f"Bought {qty} {symbol} @ ~{price:.4f} USDT")
            update_signal_decision(symbol, "executed", "EJECUTADO", signal_id=_signal_id)
            mark_signal_as_processed(symbol, signal_id=_signal_id)

            # Track position in Position Manager if available
            if position_manager:
                try:
                    from position_manager import Position
                    from technical_indicators import TechnicalIndicators
                    # Fase 2.3: prefer real commission from Binance fills; fallback to estimated
                    entry_commission = _real_commission if (_binance_confirmed and _real_commission > 0) else compute_commission(qty, price, settings, trade_type='spot')

                    # ── ATR-based dynamic SL/TP ──────────────────────────────────
                    sl_price = tp_price = None
                    try:
                        sig_gen_atr = AutonomousSignalGenerator(client, [symbol])
                        klines_atr = sig_gen_atr.fetch_klines(symbol, '15m', limit=50)
                        if klines_atr and len(klines_atr) >= 15:
                            highs_a  = [k['high']  for k in klines_atr]
                            lows_a   = [k['low']   for k in klines_atr]
                            closes_a = [k['close'] for k in klines_atr]
                            atr_vals = TechnicalIndicators.atr(highs_a, lows_a, closes_a, 14)
                            if atr_vals:
                                atr = atr_vals[-1]
                                sl_price = price - atr * 1.5
                                tp_price = price + atr * 2.5
                                print(f"ATR-based SL/TP for {symbol}: ATR={atr:.5f} SL={sl_price:.5f} TP={tp_price:.5f}")
                    except Exception as atr_err:
                        print(f"ATR calc failed for {symbol}, using pct fallback: {atr_err}")

                    # Floor ATR-based SL at category minimum to prevent micro-stops from
                    # dominating exits; tight 15m ATR*1.5 stops bypass category SL%.
                    if sl_price is not None or tp_price is not None:
                        _tok_cat = getattr(settings, 'token_categories', {}).get(symbol, {})
                        _cat_beh = _tok_cat.get('market_behavior', 'MIDCAP_ALT')
                        _cat_th  = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {})

                    if sl_price is not None:
                        _cat_sl_pct = _cat_th.get('stop_loss_pct', _get_param(settings, 'stop_loss_pct')) / 100.0
                        _min_sl_price = price * (1 - _cat_sl_pct)
                        if sl_price > _min_sl_price:
                            sl_price = _min_sl_price
                            print(f"ATR SL for {symbol} floored to category {_cat_beh} SL={_cat_sl_pct*100:.1f}%")

                    # Floor ATR-based TP at category minimum to prevent ATR-derived targets
                    # that are too tight from producing negative R:R (e.g. SL 4.25% / TP 1.1%).
                    if tp_price is not None:
                        _cat_tp_pct = _cat_th.get('take_profit_pct', _get_param(settings, 'take_profit_pct')) / 100.0
                        _min_tp_price = price * (1 + _cat_tp_pct)
                        if tp_price < _min_tp_price:
                            tp_price = _min_tp_price
                            print(f"ATR TP for {symbol} floored to category {_cat_beh} TP={_cat_tp_pct*100:.1f}%")

                    # Fallback to percentage-based if ATR unavailable
                    if sl_price is None:
                        _tok_cat = getattr(settings, 'token_categories', {}).get(symbol, {})
                        _cat_beh = _tok_cat.get('market_behavior', 'MIDCAP_ALT')
                        _cat_th  = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {})
                        sl_pct = _cat_th.get('stop_loss_pct',  _get_param(settings, 'stop_loss_pct')) / 100.0
                        tp_pct = _cat_th.get('take_profit_pct', _get_param(settings, 'take_profit_pct')) / 100.0
                        sl_price = price * (1 - sl_pct)
                        tp_price = price * (1 + tp_pct)
                        print(f"Category SL/TP for {symbol} ({_cat_beh}): SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

                    # Ensure indicators_at_entry is always captured for LearningEngine.
                    # If pnl_service.predict_for_symbol failed, fall back to
                    # formatting the signal's raw indicators directly.
                    if formatted_indicators is None:
                        try:
                            from trade_utils import format_indicators_for_predictor
                            _raw_ind = meta.get('indicators') if isinstance(meta, dict) else {}
                            if _raw_ind:
                                formatted_indicators = format_indicators_for_predictor(_raw_ind, price)
                        except Exception:
                            formatted_indicators = {}
                    _indicators_to_save = formatted_indicators or {}

                    # Prepare metadata with prediction
                    # C5: Enriquecer con entry_regime + entry_pred_pnl para re-evaluación y prediction_add
                    _is_pred_add = (position_manager.get_positions_count(symbol) >= 1) if position_manager else False
                    _signal_for_symbol = signal_meta.get(symbol, {}) if isinstance(signal_meta, dict) else {}
                    _peak_quality_profile = str(_signal_for_symbol.get('peak_quality_profile', '') or '').strip().upper()
                    _peak_quality_score = _signal_for_symbol.get('peak_quality')
                    try:
                        _peak_quality_score = float(_peak_quality_score) if _peak_quality_score is not None else None
                    except Exception:
                        _peak_quality_score = None
                    metadata = {
                        "order_id": order.get("orderId"),
                        "signal_id": _signal_id,
                        "indicators_at_entry": _indicators_to_save,
                        "rsi_at_entry": float(_signal_for_symbol.get('rsi', 50.0)),
                        "vol_spike_at_entry": float(_signal_for_symbol.get('vol_spike', 0.0)),
                        "override_used": volspike_override,
                        "strength": float(_signal_for_symbol.get('strength', strength)),
                        "peak_quality_profile": _peak_quality_profile if _peak_quality_profile in ('A', 'B', 'C') else None,
                        "peak_quality": _peak_quality_score,
                        "entry_regime": _regime_mode,
                        "entry_profile_label": _active_profile_label,
                        "regime_mode": _regime_mode,
                        "profile_label": _active_profile_label,
                        "entry_pred_pnl": float(prediction.get('expected_pnl', 0)) if prediction else None,
                        "entry_pred_prob": float(prediction.get('success_probability', 0)) if prediction else None,
                        "entry_type": "prediction_add" if _is_pred_add else "normal",
                        "reevaluation_count": 0,
                        "spot_total_at_open": usdt_liquid_now,  # SSO gate anchor — protects position from balance changes
                        "btc_price_at_entry": _get_cycle_price(client, 'BTCUSDT') if client else None,  # SSO-6: BTC ref for bounce detection
                        # Fase 2.3: trazabilidad Binance real para SPOT
                        "entry_order_id": str(_fill.get("order_id", order.get("orderId", ""))),
                        "entry_filled_qty": float(_fill.get('qty', qty)),
                        "entry_avg_price": float(_fill.get('avg_price', price)),
                        "entry_commission": float(_fill.get('commission', entry_commission)),
                        "binance_entry_confirmed": bool(_fill.get('binance_confirmed', False)),
                    }
                    # Persist gate_warnings collected during advisory mode
                    _gate_warnings_for_sym = _signal_for_symbol.get('gate_warnings', [])
                    if _gate_warnings_for_sym:
                        metadata['gate_warnings'] = _gate_warnings_for_sym
                    if prediction:
                        metadata['pnl_prediction'] = prediction

                    position = Position(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty,
                        entry_price=price,
                        entry_time=datetime.utcnow().isoformat(),
                        stop_loss=sl_price,
                        take_profit=tp_price,
                        metadata=metadata,
                        entry_commission=entry_commission,
                        pnl_prediction=prediction,
                        indicators_at_entry=_indicators_to_save
                    )
                    position_manager.add_position(position)
                    print(f"Position tracked with entry commission: {entry_commission:.4f} USDT")
                    if prediction:
                        print(f"Position includes PnL prediction: {prediction['expected_pnl']:.2f} USDT")
                except Exception as e:
                    print(f"Failed to track position in Position Manager: {e}")
                    ec.add_error(ec.SOURCE_POSITION_MANAGER, ec.LEVEL_WARNING, f"Failed to track position {symbol}: {e}")
        except Exception as exc:
            print(f"Buy order failed for {symbol}: {exc}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR, f"Buy order failed for {symbol}: {exc}")
            _record_order_failure(settings)  # R5: track consecutive failures
            update_signal_decision(symbol, "rejected", "ERROR_ORDEN", signal_id=_signal_id)
    return orders


def _select_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager, regime: str = "SPOT_BULL", db_client=None) -> List[Dict]:
    """Step 1: Resolve top buy symbols (as signal dicts) from signals or autonomous generation."""
    top_signals = _get_top_buy_symbols(settings, news_client, client, portfolio_manager, regime=regime, db_client=db_client)
    symbols = [s["symbol"] for s in top_signals]
    print("Top buy symbols:", symbols)
    return top_signals


def _compute_profit_score(
    top_signals: List[Dict],
    predictions: Dict[str, Dict],
    regime_mode: str,
) -> List[Dict]:
    """
    GAP-B: Enrich each signal dict with a composite profit_score before QUBO.

    profit_score = EV_norm×0.40 + prob×0.30 + strength×0.20 + regime_bonus×0.10

    - EV_norm  : expected_pnl normalised to [0,1] within this batch (rank-safe).
    - prob     : success_probability [0,1].
    - strength : technical signal strength [0,1].
    - regime_bonus: regime-specific alignment score [0,1]:
        SPOT_BULL     → trend_4h_score  (momentum alignment)
        RANGE_SCALP   → bb_lower_touch + (1 − bb_position)  (near support)
        FUTURES_SHORT → RSI overbought proximity (higher RSI = better short)
        PRESERVATION  → 0.5 (neutral)

    Signals are re-sorted by profit_score (descending) after scoring.
    The function is a pure transformation — never drops candidates.
    """
    if not top_signals:
        return top_signals

    _regime = str(regime_mode or 'SPOT_BULL').upper()

    ev_values = []
    for sig in top_signals:
        pred = predictions.get(sig.get('symbol', ''), {})
        ev_values.append(float((pred or {}).get('expected_pnl', 0.0) or 0.0))

    _ev_min = min(ev_values) if ev_values else 0.0
    _ev_max = max(ev_values) if ev_values else 1.0
    _ev_range = (_ev_max - _ev_min) if (_ev_max - _ev_min) > 1e-9 else 1.0

    # Peak quality scores for normalization
    _pq_values = [float(s.get('peak_quality_score', 0.0) or 0.0) for s in top_signals]
    _pq_min = min(_pq_values) if _pq_values else 0.0
    _pq_max = max(_pq_values) if _pq_values else 1.0
    _pq_range = (_pq_max - _pq_min) if (_pq_max - _pq_min) > 1e-9 else 1.0

    scored = []
    for sig in top_signals:
        sym = sig.get('symbol', '')
        pred = predictions.get(sym, {}) or {}
        ev_raw  = float(pred.get('expected_pnl', 0.0) or 0.0)
        prob    = float(pred.get('success_probability', sig.get('strength', 0.5)) or 0.5)
        strength = float(sig.get('strength', 0.5))
        pq_raw  = float(sig.get('peak_quality_score', 0.0) or 0.0)

        ev_norm = (ev_raw - _ev_min) / _ev_range
        pq_norm = (pq_raw - _pq_min) / _pq_range

        if _regime == 'SPOT_BULL':
            _tf = sig.get('higher_tf_trend', 'FLAT')
            regime_bonus = 1.0 if _tf == 'UP' else (0.5 if _tf == 'FLAT' else 0.1)
        elif _regime == 'RANGE_SCALP':
            _bb_touch = 1.0 if sig.get('bb_lower_touch') or sig.get('bb_near_lower') else 0.0
            _bb_pos   = float(sig.get('bb_position', 0.5) or 0.5)
            regime_bonus = min(1.0, _bb_touch * 0.6 + (1.0 - _bb_pos) * 0.4)
        elif _regime == 'FUTURES_SHORT':
            _rsi = float(sig.get('rsi', sig.get('indicators', {}).get('rsi', 50.0)) or 50.0)
            regime_bonus = min(1.0, max(0.0, (_rsi - 50.0) / 30.0))
        else:
            regime_bonus = 0.5

        # Peaks-first formula: peak_quality dominates ranking
        profit_score = round(
            pq_norm  * 0.40
            + ev_norm  * 0.20
            + prob   * 0.20
            + strength * 0.10
            + regime_bonus * 0.10,
            4,
        )
        sig = dict(sig)
        sig['profit_score'] = profit_score
        sig['profit_score_components'] = {
            'pq_norm': round(pq_norm, 4), 'ev_norm': round(ev_norm, 4),
            'prob': round(prob, 4), 'strength': round(strength, 4),
            'regime_bonus': round(regime_bonus, 4),
        }
        scored.append(sig)

    scored.sort(key=lambda s: s.get('profit_score', 0.0), reverse=True)

    _top5 = scored[:5]
    print(
        f"[ProfitScore/{_regime}] top-5: "
        + " | ".join(
            f"{s['symbol']} ps={s['profit_score']:.3f}"
            f"(pq={s['profit_score_components']['pq_norm']:.2f}"
            f" ev={s['profit_score_components']['ev_norm']:.2f}"
            f" p={s['profit_score_components']['prob']:.2f}"
            f" s={s['profit_score_components']['strength']:.2f}"
            f" r={s['profit_score_components']['regime_bonus']:.2f})"
            for s in _top5
        )
    )
    return scored


def _apply_qubo_selector(
    settings,
    client: BinanceClient,
    top_signals: List[Dict],
    predictions: Dict[str, Dict],
    regime_mode: str,
    position_manager=None,
) -> Tuple[List[Dict], Dict[str, Dict]]:
    """
    Run combinatorial QUBO portfolio selection on filtered candidates.

    Returns a tuple (selected_signals, filtered_predictions).
    If QUBO is disabled, regime is PRESERVATION, or there are too few candidates,
    returns the inputs unchanged.
    """
    if not getattr(settings, 'qubo_enabled', True):
        return top_signals, predictions

    if regime_mode == "PRESERVATION":
        print("[QUBO] PRESERVATION mode — no portfolio selection, no new entries")
        return [], {}

    if not top_signals:
        return [], {}

    _min_candidates = int(getattr(settings, 'qubo_min_candidates', 2))
    if len(top_signals) < _min_candidates:
        print(f"[QUBO] Only {len(top_signals)} candidate(s) (< {_min_candidates}) — skipping combinatorial selection")
        return top_signals, predictions

    # Build candidate list from signals + predictions
    candidates: List[Candidate] = []
    signal_meta: Dict[str, Dict] = {}
    for sig in top_signals:
        if not isinstance(sig, dict):
            continue
        sym = sig.get('symbol')
        if not sym:
            continue
        signal_meta[sym] = sig
        pred = predictions.get(sym, {})
        expected_pnl = float(pred.get('expected_pnl', 0.0) or 0.0)
        success_prob = float(pred.get('success_probability', sig.get('strength', 0.5)) or 0.5)
        risk_score = float(pred.get('risk_metrics', {}).get('risk_score', 50.0) or 50.0)
        atr_pct = float(sig.get('atr_pct', pred.get('atr_pct', 0.0)) or 0.0)
        candidates.append(Candidate(
            symbol=sym,
            expected_pnl=expected_pnl,
            success_probability=success_prob,
            risk_score=risk_score,
            atr_pct=atr_pct,
            peak_quality_score=float(sig.get('peak_quality_score', 0.0) or 0.0),
            metadata={'signal': sig, 'prediction': pred},
        ))

    if len(candidates) < _min_candidates:
        return top_signals, predictions

    # Collect symbols for correlation matrix (include open positions when configured)
    symbols = [c.symbol for c in candidates]
    open_symbols: List[str] = []
    if getattr(settings, 'qubo_include_open_positions', True) and position_manager is not None:
        try:
            open_symbols = list(position_manager.get_all_positions().keys())
            # Only include open symbols that are also candidate symbols
            open_symbols = [s for s in open_symbols if s in symbols]
        except Exception:
            open_symbols = []

    all_symbols = list(dict.fromkeys(symbols + open_symbols))

    # Fetch correlation matrix based on regime
    try:
        if regime_mode == "FUTURES_SHORT":
            corr_matrix = _get_futures_correlation_matrix(client, all_symbols)
        else:
            corr_matrix = _get_spot_correlation_matrix(client, all_symbols)
    except Exception as e:
        print(f"[QUBO] Correlation matrix failed: {e} — falling back to ranking-only selection")
        return top_signals, predictions

    result = select_for_regime(
        candidates=candidates,
        correlation_matrix=corr_matrix,
        settings=settings,
        regime_mode=regime_mode,
        open_symbols=open_symbols if open_symbols else None,
    )

    selected_symbols = [c.symbol for c in result.selected]
    print(
        f"[QUBO] Selected {len(selected_symbols)} of {len(candidates)} candidates "
        f"(solver={result.solver}, avg_corr={result.avg_correlation:.3f}, "
        f"kelly_mult={result.kelly_multiplier}, epnl_total={result.expected_pnl_total:.2f}): "
        f"{selected_symbols}"
    )

    selected_signals = []
    for s in selected_symbols:
        if s not in signal_meta:
            continue
        sig = signal_meta[s]
        sig['qubo_weight'] = result.weights.get(s, 0.0)
        selected_signals.append(sig)
    filtered_predictions = {s: predictions[s] for s in selected_symbols if s in predictions}

    return selected_signals, filtered_predictions


def _build_predictions(top_symbols: List[str], client: BinanceClient, settings, portfolio_manager, signal_meta: Dict = None, trade_type: str = 'spot', regime_mode: str = 'SPOT_BULL') -> Dict:
    """Step 2: Generate PnL predictions once for all top symbols (reused in buy step).
    BUG-E4: accepts signal_meta to reuse pre-computed indicators from Step 1,
    avoiding redundant analyze_symbol() calls inside pnl_service."""
    predictions = {}
    if not portfolio_manager or not top_symbols:
        return predictions
    try:
        _default_side = 'SHORT' if (str(trade_type).lower() == 'futures' and str(regime_mode or '').strip().upper() == 'FUTURES_SHORT') else 'LONG'
        _pm_balance = portfolio_manager.get_total_balance()
        usdt_liquid = float(_pm_balance.get("usdt_available_after_reserve", _pm_balance.get("usdt_available", 0)))
        alloc_per = min(settings.max_per_trade_usdt, usdt_liquid / len(top_symbols))
        for symbol in top_symbols:
            _pre_sig = (signal_meta or {}).get(symbol)
            _side = str((_pre_sig or {}).get('expected_side', _default_side) or _default_side).strip().upper()
            if _side not in ('LONG', 'SHORT'):
                _side = _default_side
            pred, _ = pnl_service.predict_for_symbol(
                symbol,
                client,
                settings,
                alloc_per,
                precomputed_signal=_pre_sig,
                trade_type=trade_type,
                trade_side=_side,
                current_price=_get_cycle_price(client, symbol),
            )
            if pred:
                predictions[symbol] = pred
                print(f"Prediction for {symbol}: {pred['expected_pnl']:.2f} USDT")
        if predictions:
            _all_zero = all(abs(float((pred or {}).get('expected_pnl', 0.0) or 0.0)) < 1e-9 for pred in predictions.values())
            if _all_zero:
                _warn = {
                    'trade_type': trade_type,
                    'symbols': list(top_symbols),
                    'prediction_count': len(predictions),
                    'detected_at': datetime.now(timezone.utc).isoformat(),
                }
                print(f"[Pred/WARN] All predictions returned 0.00 USDT for {trade_type}: {top_symbols[:10]}")
                ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                             f"All predictions returned 0.00 USDT for {trade_type}")
                if _db_client_ref:
                    try:
                        _raw = json.dumps(_warn)
                        _db_client_ref.execute_update(
                            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                            ('prediction_zero_warning', _raw, _raw),
                        )
                    except Exception as _pz_err:
                        print(f"[Pred/WARN] persist failed: {_pz_err}")
    except Exception as e:
        print(f"Error generating predictions: {e}")
    return predictions


def _close_prediction_based(top_symbols: List[str], client: BinanceClient, settings,
                            portfolio_manager, position_manager, predictions: Dict,
                            regime_mode: str = "SPOT_BULL") -> None:
    """Step 3: Slot-pressure reallocation — close the weakest position ONLY when MAX_POSITIONS
    is reached AND the new opportunity is genuinely better AND the position shows a technical
    sell signal (same Opción B criteria as the SL/TP checker).
    Never closes positions just because they are not in top_symbols.
    """
    if not portfolio_manager or not position_manager or not predictions:
        return
    try:
        # ── Gate 1: only act when position slots are FULL ─────────────────
        _regime_max = _get_max_positions_for_regime(settings, regime_mode)
        all_positions = position_manager.get_all_positions()
        _open_count = sum(1 for pl in all_positions.values() if pl)
        if _open_count < _regime_max:
            print(f"[SlotPressure] {_open_count}/{_regime_max} positions open — no slot pressure, skipping reallocation")
            return

        current_positions = {}
        for sym, pos_list in all_positions.items():
            if pos_list:
                pos = pos_list[0]
                current_positions[sym] = {
                    'quantity': float(pos.quantity),
                    'entry_price': float(pos.entry_price),
                }
        if not current_positions:
            return

        # ── Gate 2: find the weakest current position vs best new opportunity ─
        # Use decide_position_closures (prediction-only path, fallback disabled)
        from portfolio_manager import CapitalAllocation
        _pm_balance_r = portfolio_manager.get_total_balance()
        usdt_liquid = float(_pm_balance_r.get("usdt_available_after_reserve", _pm_balance_r.get("usdt_available", 0)))
        alloc_per = min(settings.max_per_trade_usdt, usdt_liquid / max(len(top_symbols), 1))
        new_allocations = [
            CapitalAllocation(
                symbol=sym,
                capital_usdt=alloc_per,
                percentage=alloc_per / usdt_liquid * 100 if usdt_liquid else 0,
                risk_adjusted=False,
            )
            for sym in top_symbols
        ]
        symbols_to_close = portfolio_manager.decide_position_closures(
            current_positions=current_positions,
            new_allocations=new_allocations,
            new_predictions=predictions,
        )
        if not symbols_to_close:
            return

        # ── Gate 3: apply all protective filters for each candidate ───────
        if regime_mode == 'RANGE_SCALP':
            _min_hold_min = getattr(settings, 'min_hold_minutes_range_scalp', 30)
        elif regime_mode == 'SPOT_BULL':
            _min_hold_min = _get_param(settings, 'min_hold_minutes_spot_bull')
        else:
            _min_hold_min = _get_param(settings, 'min_hold_minutes')
        _protect = getattr(settings, 'protect_profitable_positions', True)
        _protect_pct = getattr(settings, 'protect_profit_min_pct', 0.3) / 100.0
        _rebalance_max_abs_pnl_pct = float(getattr(settings, 'rebalance_max_abs_pnl_pct', 3.0)) / 100.0
        _excluded_syms = set(getattr(settings, 'rebalance_excluded_symbols', []))  # F4: stablecoins etc.
        approved = []
        for sym in symbols_to_close:
            # F4: skip symbols explicitly excluded from rebalancing (stablecoins)
            if sym in _excluded_syms:
                print(f"[SlotPressure] {sym}: in rebalance_excluded_symbols — skipping")
                continue
            pos_list = all_positions.get(sym, [])
            if not pos_list:
                continue
            pos = pos_list[0]
            try:
                current_price = _get_cycle_price(client, sym)
            except Exception:
                print(f"[SlotPressure] {sym}: price fetch failed — skipping")
                continue

            # min_hold check
            _entry_ts = getattr(pos, 'entry_time', None) or getattr(pos, 'created_at', None)
            if _entry_ts is not None:
                if hasattr(_entry_ts, 'tzinfo') and _entry_ts.tzinfo is None:
                    _entry_ts = _entry_ts.replace(tzinfo=timezone.utc)
                _age_min = (datetime.now(timezone.utc) - _entry_ts).total_seconds() / 60
                if _age_min < _min_hold_min:
                    print(f"[SlotPressure] {sym}: age={_age_min:.1f}min < min_hold={_min_hold_min}min — skipping")
                    continue

            # protect_profit check
            ep = float(pos.entry_price)
            pnl_pct = (current_price - ep) / ep if ep > 0 else 0.0
            if abs(pnl_pct) > _rebalance_max_abs_pnl_pct:
                print(
                    f"[SlotPressure] {sym}: abs pnl {pnl_pct*100:.2f}% > rebalance cap "
                    f"{_rebalance_max_abs_pnl_pct*100:.2f}% — leave to SL/TP/SSO"
                )
                continue
            if _protect and pnl_pct >= _protect_pct:
                print(f"[SlotPressure] {sym}: profitable {pnl_pct*100:.2f}% >= protect {_protect_pct*100:.1f}% — skipping")
                continue

            # Fix D: defer to SL checker if near SL threshold
            if pnl_pct < 0:
                _cat_beh = getattr(settings, 'token_categories', {}).get(sym, {}).get('market_behavior', 'MIDCAP_ALT')
                _sl_pct = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {}).get('stop_loss_pct',
                    _get_param(settings, 'stop_loss_pct')) / 100.0
                if abs(pnl_pct) >= _sl_pct * 0.75:
                    print(f"[SlotPressure] {sym}: near SL ({pnl_pct*100:.2f}%) — deferring to SL checker")
                    continue

            # Opción B: require a technical sell signal before displacing the position
            _has_tech_signal = False
            _close_reason = "reallocation_slot_pressure"
            if getattr(settings, 'rebalance_skip_if_bullish_signal', True):
                try:
                    _klines_sp = client.client.get_klines(symbol=sym, interval='1h', limit=20)
                    if _klines_sp and len(_klines_sp) >= 14:
                        _cls = [float(k[4]) for k in _klines_sp]
                        _gains = [max(0.0, _cls[i] - _cls[i-1]) for i in range(1, len(_cls))]
                        _losses = [max(0.0, _cls[i-1] - _cls[i]) for i in range(1, len(_cls))]
                        _ag = sum(_gains[-14:]) / 14
                        _al = sum(_losses[-14:]) / 14
                        _rsi = 100 - (100 / (1 + _ag / _al)) if _al > 0 else 50.0
                        _ema9 = _cls[0]
                        _a = 2 / (9 + 1)
                        for _c in _cls:
                            _ema9 = _c * _a + _ema9 * (1 - _a)
                        _rsi_thr = getattr(settings, 'rebalance_rsi_overbought', 60.0)
                        _rsi_neg = _rsi > _rsi_thr
                        _ema9_neg = _cls[-1] < _ema9 * 0.999
                        if _rsi_neg or _ema9_neg:
                            _has_tech_signal = True
                            _close_reason = "reallocation_rsi_overbought" if _rsi_neg else "reallocation_ema9_bearish"
                            print(f"[SlotPressure] {sym}: tech signal confirmed RSI={_rsi:.1f} rsi_neg={_rsi_neg} ema9_neg={_ema9_neg} — approved for reallocation")
                        else:
                            print(f"[SlotPressure] {sym}: no technical sell signal (RSI={_rsi:.1f}<={_rsi_thr}, EMA9 OK) — skipping displacement")
                            continue
                    else:
                        print(f"[SlotPressure] {sym}: insufficient klines — skipping displacement")
                        continue
                except Exception as _te:
                    print(f"[SlotPressure] {sym}: technical check failed ({_te}) — skipping displacement")
                    continue
            else:
                _has_tech_signal = True

            approved.append((sym, _close_reason))

        if not approved:
            print(f"[SlotPressure] No positions approved for displacement after filters")
            return

        # ── Execute sells for approved positions ──────────────────────────
        from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid
        _max_displace = _get_param(settings, 'max_rebalance_per_cycle')
        for sym, _close_reason in approved[:_max_displace]:  # INEF-1: use max_rebalance_per_cycle
            try:
                balances = client.get_account_balance()
                asset = sym.replace("USDT", "")
                qty_held = float(balances.get(asset, 0))
                if qty_held <= 0:
                    print(f"[SlotPressure] {sym}: no balance held, skipping sell")
                    continue
                filters = get_symbol_filters(client, sym)
                qty = align_quantity_to_step(qty_held, filters["step"])
                exit_price_sp = _get_cycle_price(client, sym)
                valid, reason = is_quantity_valid(qty, exit_price_sp, filters)
                if not valid:
                    print(f"[SlotPressure] {sym}: invalid qty {qty} — {reason}")
                    continue
                order = client.create_market_order(symbol=sym, side="SELL", quantity=qty)
                if not client.verify_order_filled(order, f"SlotPressure SELL {sym}"):
                    print(f"[WARN] SlotPressure SELL {sym}: order NOT confirmed filled — skipping")
                    continue
                exit_price_sp = float((order.get("fills") or [{}])[0].get("price", 0) or exit_price_sp)
                for pos in position_manager.get_all_positions().get(sym, []):
                    position_manager.close_position(
                        symbol=sym,
                        position_id=pos.position_id,
                        exit_price=exit_price_sp,
                        close_reason=_close_reason,
                        commission_rate=_get_commission_config(settings).rate_for('spot'),
                        exit_context={
                            'regime_mode': regime_mode,
                            'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                        },
                    )
                _overflow_positions.discard(sym)
                _VOLSPIKE_POSITIONS.discard(sym)
                print(f"[SlotPressure] {sym}: displaced @ {exit_price_sp:.4f} reason={_close_reason}")
            except Exception as sell_err:
                print(f"[SlotPressure] {sym}: sell failed — {sell_err}")
        time.sleep(1)
    except Exception as e:
        print(f"Error in slot-pressure reallocation: {e}")


def _is_new_entries_paused(db_client) -> bool:
    """Check if new entries are paused via trading_runtime_state."""
    if db_client is None:
        return False
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key = 'pause_new_entries'"
        )
        if rows:
            val = rows[0].get('value') or {}
            if isinstance(val, str):
                val = json.loads(val)
            if not isinstance(val, dict):
                return False
            paused = bool(val.get('paused', False))
            if not paused:
                return False

            def _parse_dt(raw):
                if isinstance(raw, datetime):
                    return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
                if isinstance(raw, str):
                    try:
                        _dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                        return _dt if _dt.tzinfo else _dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        return None
                return None

            now = datetime.now(timezone.utc)
            started_at = _parse_dt(val.get('started_at')) or rows[0].get('updated_at')
            expires_at = _parse_dt(val.get('expires_at'))
            if expires_at is None and started_at is not None:
                ttl_min_raw = val.get('ttl_minutes', val.get('max_minutes'))
                if ttl_min_raw is not None:
                    try:
                        ttl_min = max(1.0, float(ttl_min_raw))
                        expires_at = started_at + timedelta(minutes=ttl_min)
                    except Exception:
                        expires_at = None

            if expires_at is not None and now >= expires_at:
                try:
                    db_client.execute_update(
                        "DELETE FROM trading_runtime_state WHERE key = 'pause_new_entries'",
                        (),
                    )
                    db_client.execute_update(
                        "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                        (
                            '__config__',
                            'system',
                            'pause_new_entries_auto_resume',
                            json.dumps({
                                'reason': val.get('reason', 'pause_ttl_expired'),
                                'started_at': started_at.isoformat() if started_at else None,
                                'expires_at': expires_at.isoformat() if expires_at else None,
                                'resumed_at': now.isoformat(),
                            }),
                            False,
                        ),
                    )
                    print(
                        f"[PauseCheck] pause_new_entries TTL expired at {expires_at.isoformat() if expires_at else '?'} — auto-resumed entries"
                    )
                except Exception as _resume_err:
                    print(f"[PauseCheck] auto-resume failed after TTL expiration: {_resume_err}")
                return False

            return True
    except Exception as e:
        print(f"[PauseCheck] error reading pause_new_entries: {e}")
    return False


def _is_health_results_critical(db_client, max_age_minutes: int = 20) -> bool:
    """Return True when latest SMART health snapshot is critical and still fresh."""
    if db_client is None:
        return False
    try:
        rows = db_client.execute_query(
            "SELECT value, updated_at FROM trading_runtime_state WHERE key='health_results_latest'"
        )
        if not rows:
            return False
        payload = rows[0].get('value') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return False
        status = str(payload.get('overall') or '').strip().lower()
        block_new_entries = payload.get('block_new_entries')
        if block_new_entries is None:
            if status not in ('critical', 'error'):
                return False
        elif not bool(block_new_entries):
            return False

        ts_raw = payload.get('timestamp')
        ts = None
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
            except Exception:
                ts = None
        if ts is None:
            ts = rows[0].get('updated_at')
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return age_min <= max_age_minutes
    except Exception as _he:
        print(f"[HealthGate] error reading health_results_latest: {_he}")
        return False


def _persist_operator_alert(db_client, alert_type: str, message: str) -> None:
    """Persist a non-blocking operator alert to DB for dashboard visibility.

    Unlike the old auto-pause gates, this never stops new entries.
    The operator sees the alert in the dashboard and decides to press STOP or not.
    Alerts are upserted per alert_type so the dashboard always shows the latest state.
    Also appended to trades_audit for full trazabilidad.
    """
    if db_client is None:
        return
    try:
        _ts = datetime.now(timezone.utc).isoformat()
        _payload = json.dumps({
            "alert_type": alert_type,
            "message": message,
            "ts": _ts,
            "acknowledged": False,
        })
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (f"operator_alert_{alert_type}", _payload, _payload),
        )
        db_client.execute_update(
            "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("__system__", "system", f"operator_alert_{alert_type}",
             json.dumps({"alert_type": alert_type, "message": message, "ts": _ts}), False),
        )
    except Exception as _ae:
        print(f"[OperatorAlert] persist failed (non-fatal): {_ae}")


def _persist_safepoint(db_client, settings, position_manager) -> None:
    """Persist a pre-decision state snapshot for restart/forensic traceability."""
    if not db_client:
        return
    try:
        _snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "regime": str(getattr(settings, 'current_regime_mode', '') or '').strip().upper(),
            "profile": str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper(),
            "pause_new_entries": bool(_is_new_entries_paused(db_client)),
            "health_critical": bool(_is_health_results_critical(db_client)),
            "open_positions": {
                sym: sum(float(p.quantity or 0) for p in plist)
                for sym, plist in (position_manager.get_all_positions().items() if position_manager else {})
            },
        }
        _key = f"safepoint_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        db_client.execute_update(
            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
            (_key, json.dumps(_snapshot), json.dumps(_snapshot)),
        )
    except Exception as e:
        print(f"[Safepoint] failed to persist: {e}")


def execute_once(monitor_only: bool = False, components=None):
    global _db_client_ref, _FUTURES_MASS_SL_COOLDOWN_UNTIL, _VOLSPIKE_POSITIONS, _OVERRIDE_DAILY_COUNT
    global _consecutive_failures, _circuit_breaker_until, _SSO7_BEST_CANDIDATE_CACHE
    if components is None:
        from init_components import get_components
        comp = get_components()
    else:
        comp = components
    settings = comp.settings
    client = comp.binance_client
    news_client = comp.news_client
    position_manager = comp.position_manager
    portfolio_manager = comp.portfolio_manager

    # ── C3: Inicializar referencia DB para cooldowns persistentes ───────────────
    if _db_client_ref is None and hasattr(comp, 'db_client') and comp.db_client:
        _db_client_ref = comp.db_client
        _load_cooldowns_from_db()
        # Load additional volatile state (mass-SL, volspike positions, override counters)
        _loaded_state = state_persistence.load_state(_db_client_ref)
        if _loaded_state:
            _FUTURES_MASS_SL_COOLDOWN_UNTIL = _loaded_state.get('futures_mass_sl_cooldown_until') or _FUTURES_MASS_SL_COOLDOWN_UNTIL
            _VOLSPIKE_POSITIONS = set(_loaded_state.get('volspike_positions', [])) or _VOLSPIKE_POSITIONS
            _OVERRIDE_DAILY_COUNT = _loaded_state.get('override_daily_count', {}) or _OVERRIDE_DAILY_COUNT
            _consecutive_failures = int(_loaded_state.get('consecutive_failures', _consecutive_failures) or 0)
            _circuit_breaker_until = _loaded_state.get('circuit_breaker_until') or _circuit_breaker_until
            print(f"[StatePersistence] Loaded volatile state: mass_sl={_FUTURES_MASS_SL_COOLDOWN_UNTIL}, "
                f"volspike_positions={len(_VOLSPIKE_POSITIONS)}, override_counts={len(_OVERRIDE_DAILY_COUNT)}, "
                f"cb_failures={_consecutive_failures}, cb_until={_circuit_breaker_until}")

    # SPOT reconciliation: keep DB aligned with Binance balance on every cycle.
    # This is intentionally run even when the system is paused so restart drift
    # is corrected without opening new trades.
    try:
        if position_manager and client:
            _reconciled = position_manager.sync_with_exchange_balance(
                client, settings, min_value_usdt=5.0
            )
            if _reconciled:
                print(f"[SPOT-Reconcile] Imported orphan positions: {_reconciled}")
    except Exception as _sync_err:
        print(f"[SPOT-Reconcile] cycle sync failed: {_sync_err}")

    # Early checkpoint: persist current volatile state at cycle start to minimize
    # loss window on mid-cycle crash/restart. End-of-cycle checkpoint is kept too.
    if _db_client_ref:
        try:
            state_persistence.save_state(
                _db_client_ref,
                futures_sl_cooldown=_FUTURES_SL_COOLDOWN,
                futures_mass_sl_cooldown_until=_FUTURES_MASS_SL_COOLDOWN_UNTIL,
                volspike_cooldown=_VOLSPIKE_COOLDOWN,
                volspike_positions=_VOLSPIKE_POSITIONS,
                override_daily_count=_OVERRIDE_DAILY_COUNT,
                circuit_breaker_until=_circuit_breaker_until,
                consecutive_failures=_consecutive_failures,
            )
        except Exception as _sp_start_err:
            print(f"[StatePersistence] start checkpoint failed: {_sp_start_err}")

    # Fase 2.4: pre-decision safepoint for forensic traceability
    try:
        _persist_safepoint(_db_client_ref, settings, position_manager)
    except Exception as _safe_err:
        print(f"[Safepoint] cycle start safepoint failed: {_safe_err}")

    # ── Regime cache warm-up: restore confirmed regime from DB after restart ─────
    # Ensures the first cycle after a restart doesn't block with confirmed=False.
    # Anti-whipsaw is preserved for actual regime *changes*.
    try:
        _prime_regime_from_db(_db_client_ref)
    except Exception as _prc_err:
        print(f"[MarketRegime] prime_cache_from_db error (non-fatal): {_prc_err}")

    # ── K2: Pause new entries ONLY if operator explicitly requested it ───────────
    # Auto-pauses by the system are replaced with operator_alert notifications.
    # Only a manual pause (source='operator') blocks new entries here.
    if not monitor_only and _is_new_entries_paused(_db_client_ref):
        print("[execute_once] pause_new_entries=True (operator request) — running SL/TP monitor only")
        monitor_only = True

    # ── Health critical: notify operator instead of auto-pausing ─────────────────
    # SL/TP and all protections continue running. Operator decides whether to STOP.
    if _is_health_results_critical(_db_client_ref):
        _persist_operator_alert(_db_client_ref, "health_critical", "SMART health=critical — revise config drift o errores. Las protecciones SL/TP siguen activas.")
        print("[execute_once] ⚠️  health_results_latest=critical — ALERTA al operador (no auto-pausa). Revisa el dashboard.")

    # Binance-first gate: runtime-vs-exchange parity for futures must be healthy
    # before allowing new entries. Hard protections (SL/TP) stay enabled.
    _binance_truth_status = {"ok": True, "severity": "unknown", "mismatch_count": 0}
    try:
        if getattr(settings, 'futures_enabled', True):
            _fc_truth = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None), injected_client=getattr(comp, 'futures_client', None))
            _wire_futures_client(_fc_truth, client, settings)
            _binance_truth_status = evaluate_futures_parity(_fc_truth)
            persist_truth_status(_db_client_ref, _binance_truth_status, source='execute_once_pretrade')
            if (_binance_truth_status.get('mismatch_count') or 0) > 0:
                audit_truth_mismatches(_db_client_ref, _binance_truth_status, source='execute_once_pretrade')
                print(
                    f"[BinanceTruth] severity={_binance_truth_status.get('severity')} "
                    f"mismatches={_binance_truth_status.get('mismatch_count')}"
                )
            if (not monitor_only) and str(_binance_truth_status.get('severity', '')).lower() == 'critical':
                _persist_operator_alert(_db_client_ref, "binance_truth_critical", f"Binance futures parity critical — mismatches={_binance_truth_status.get('mismatch_count',0)}. Las protecciones SL/TP siguen activas.")
                print(f"[execute_once] ⚠️  binance_truth_status=critical — ALERTA al operador (no auto-pausa). mismatches={_binance_truth_status.get('mismatch_count',0)}")
    except Exception as _truth_gate_err:
        print(f"[BinanceTruth] pretrade gate failed: {_truth_gate_err}")

    try:
        _spot_truth_status = evaluate_spot_parity(position_manager, client, settings=settings, min_notional_usdt=5.0)
        persist_truth_status_spot(_db_client_ref, _spot_truth_status, source='execute_once_pretrade')
        if (_spot_truth_status.get('mismatch_count') or 0) > 0:
            audit_truth_mismatches(_db_client_ref, _spot_truth_status, source='execute_once_pretrade_spot')
            print(
                f"[SpotTruth] severity={_spot_truth_status.get('severity')} "
                f"mismatches={_spot_truth_status.get('mismatch_count')}"
            )
            # Anti-ghost auto-fix: close DB ghosts and import orphan positions > $5 immediately.
            for _mm in _spot_truth_status.get('mismatches', []):
                _mm_code = _mm.get('code')
                _mm_sym = _mm.get('symbol')
                if _mm_code == 'SPOT_GHOST_IN_DB':
                    try:
                        _mm_price = float(client.get_symbol_price(_mm_sym) or _mm.get('price', 0))
                        position_manager.close_all_positions_for_symbol(
                            _mm_sym, _mm_price, 'pretrade_ghost_auto_close',
                            float(getattr(settings, 'binance_commission_rate', 0.001))
                        )
                        print(f"[SpotTruth/AutoFix] Closed ghost DB position {_mm_sym}")
                    except Exception as _mm_err:
                        print(f"[SpotTruth/AutoFix] Failed to close ghost {_mm_sym}: {_mm_err}")
                elif _mm_code == 'SPOT_ORPHAN_IN_BINANCE':
                    try:
                        _mm_value = float(_mm.get('value_usdt') or 0.0)
                        if _mm_value >= 5.0:
                            _imported = position_manager.sync_with_exchange_balance(client, settings, min_value_usdt=5.0)
                            if _mm_sym in (_imported or []):
                                print(f"[SpotTruth/AutoFix] Imported orphan position {_mm_sym} (${_mm_value:.2f})")
                    except Exception as _mm_err:
                        print(f"[SpotTruth/AutoFix] Failed to import orphan {_mm_sym}: {_mm_err}")
    except Exception as _spot_truth_err:
        print(f"[SpotTruth] pretrade gate failed: {_spot_truth_err}")

    # ── PAUSED / monitor_only: solo ejecutar SL/TP y retornar ───────────────────
    if monitor_only:
        print("[execute_once] monitor_only=True (system PAUSED) — running SL/TP monitor only")
        _mon_closes = []
        try:
            _regime_mode_mon = "SPOT_BULL"
            if getattr(settings, 'market_regime_enabled', True):
                try:
                    _regime_mon = get_regime(client, settings=settings)
                    _regime_mode_mon = _regime_mon.mode
                except Exception:
                    pass
            _mon_closes = _check_sl_tp_positions(
                client, settings, position_manager,
                news_client=news_client, regime=_regime_mode_mon, monitor_only=True,
                sso7_candidate_cache=_SSO7_BEST_CANDIDATE_CACHE,
            )
            try:
                _fc_mon = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None), injected_client=getattr(comp, 'futures_client', None))
                _wire_futures_client(_fc_mon, client, settings)
                _fut_mon_closes = _fc_mon.check_positions(settings=settings, monitor_only=True)
                if _fut_mon_closes:
                    _mon_closes.extend(_fut_mon_closes)
                    print(f"[Monitor] Futures closed {len(_fut_mon_closes)} position(s) while PAUSED")
            except Exception as _fut_mon_err:
                print(f"[Monitor] Futures monitor error while PAUSED: {_fut_mon_err}")
            if _mon_closes:
                print(f"[Monitor] Closed {len(_mon_closes)} position(s) while PAUSED: "
                      f"{[c['symbol'] for c in _mon_closes]}")
        except Exception as _mon_err:
            print(f"[Monitor] SL/TP monitor error while PAUSED: {_mon_err}")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": [], "sl_tp_closes": _mon_closes}

    print(f"DRY_RUN={settings.dry_run}  ALLOW_REAL_TRADING={settings.allow_real_trading}  MAX_PER_TRADE_USDT={settings.max_per_trade_usdt}")

    # BUG-E2: Reset cycle-scoped ASG so all consumers share a fresh instance this cycle
    global _asg_cycle_instance
    _asg_cycle_instance = None
    global _cycle_price_cache
    _cycle_price_cache = {}

    # ── BUG-6: Cleanup expired cooldown entries (memory hygiene for 24/7 operation) ──
    try:
        from datetime import timedelta as _td
        _cd_cutoff = datetime.now(timezone.utc) - _td(hours=_VOLSPIKE_COOLDOWN_HOURS + 1)
        _expired_vs = [k for k, v in _VOLSPIKE_COOLDOWN.items() if v < _cd_cutoff]
        for _k in _expired_vs:
            _VOLSPIKE_COOLDOWN.pop(_k, None)
        _nc_cutoff = datetime.now(timezone.utc) - _td(hours=_NARRATIVE_BUY_COOLDOWN_HOURS + 1)
        _expired_nc = [k for k, v in _NARRATIVE_BUY_COOLDOWN.items() if v < _nc_cutoff]
        for _k in _expired_nc:
            _NARRATIVE_BUY_COOLDOWN.pop(_k, None)
        if _expired_vs or _expired_nc:
            print(f"[Cleanup] Removed {len(_expired_vs)} volspike + {len(_expired_nc)} narrative cooldown entries")
    except Exception:
        pass

    # ── Log Fear & Greed state ──────────────────────────────────────────────
    try:
        _fng = news_client.get_fear_greed()
        _fng_mod = news_client.get_fear_greed_modifier()
        print(f"Fear & Greed Index: {_fng.get('value')} ({_fng.get('label')}) → prob_modifier={_fng_mod:+.2f}")
        _trending = news_client.get_trending_symbols()
        if _trending:
            print(f"CoinGecko Trending: {_trending}")
    except Exception as _fng_err:
        print(f"F&G/Trending check skipped: {_fng_err}")

    if not settings.binance_use_testnet and not settings.allow_real_trading:
        print("Real trading is disabled. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    if settings.agent_mode != "auto":
        print("Agent mode is not auto. Skipping execute_once.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": []}

    _load_invalid_symbol_state(_db_client_ref)

    # ── Regime detection for execute_once (needed by Step 0.5 and Step 1) ──────
    _regime_exe: MarketRegime = None
    _regime_mode = "SPOT_BULL"
    _active_mode_before = str(getattr(settings, 'current_regime_mode', '') or '').strip().upper() or None
    _active_profile_before = str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None
    _transition_policy: Dict[str, Any] = {}
    _pre_switch_state: Dict[str, Any] = {
        "active": False,
        "from_mode": None,
        "to_mode": None,
        "reason": None,
        "confidence": 0.0,
        "block_new_shorts": False,
    }
    if getattr(settings, 'market_regime_enabled', True):
        try:
            _regime_exe = get_regime(client, settings=settings)
            _regime_mode = _regime_exe.mode
            print(f"[Regime] execute_once: {_regime_mode} | confirmed={_regime_exe.confirmed}")
            # Persist active regime so the screener/dashboard always see the current mode
            if settings is not None:
                try:
                    settings.current_regime_mode = _regime_mode
                except Exception:
                    pass
            # F4 fix: on fresh start, anti-whipsaw needs 2 detections to confirm.
            # If the raw signal is unambiguous (severe structural bear: 30d < -15%), prime
            # the cache and immediately re-detect so the first cycle runs with confirmed=True
            # and SHORTs / FUTURES_SHORT mode can activate without waiting an extra 45min.
            if not _regime_exe.confirmed and _regime_exe.btc_30d_pct <= -15.0:
                from market_regime import prime_cache as _prime_regime_cache
                _prime_regime_cache(_regime_exe.mode)
                _regime_exe = get_regime(client, settings=settings)
                _regime_mode = _regime_exe.mode
                if settings is not None:
                    try:
                        settings.current_regime_mode = _regime_mode
                    except Exception:
                        pass
                print(f"[Regime] execute_once warm-up (severe bear): {_regime_mode} | confirmed={_regime_exe.confirmed}")
        except Exception as _re_exe:
            print(f"[Regime] execute_once detection failed: {_re_exe}")

        try:
            _mode_selector_result = _run_smart_total_mode_selector(settings, _regime_exe, _db_client_ref)
            if _mode_selector_result.get('ok') and not _mode_selector_result.get('skipped'):
                _pre_switch_state = _build_pre_switch_state(_mode_selector_result, _regime_exe)
                if _pre_switch_state.get("active"):
                    print(
                        f"[Transition/PreSwitch] {_pre_switch_state.get('from_mode')} -> {_pre_switch_state.get('to_mode')} "
                        f"confidence={_pre_switch_state.get('confidence'):.2f} reason={_pre_switch_state.get('reason')}"
                    )
                _selected_mode = _mode_selector_result.get('selected_mode')
                if _selected_mode and _selected_mode != _regime_mode:
                    print(
                        f"[SMART-Total/Mode] {_regime_mode} -> {_selected_mode} "
                        f"switched={_mode_selector_result.get('switched')} "
                        f"blocked={_mode_selector_result.get('blocked')} "
                        f"reason={_mode_selector_result.get('reason')}"
                    )
                    _regime_mode = _selected_mode
                if settings is not None:
                    try:
                        settings.current_regime_mode = _regime_mode
                    except Exception:
                        pass
                _persist_transition_state(
                    _db_client_ref,
                    {
                        "active": bool(_pre_switch_state.get("active")),
                        "pre_switch": _pre_switch_state,
                        "selected_mode": _regime_mode,
                        "detected_mode": _mode_selector_result.get("detected_mode"),
                        "blocked": _mode_selector_result.get("blocked"),
                        "switched": _mode_selector_result.get("switched"),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            elif _mode_selector_result.get('reason') not in (
                'mode_selector_disabled', 'smart_total_disabled', 'no_regime'
            ):
                print(f"[SMART-Total/Mode] skipped: {_mode_selector_result.get('reason')}")
        except Exception as _mse:
            print(f"[SMART-Total/Mode] selector failed: {_mse}")

        # ── Regime-specific parameter auto-loading (only on confirmed regime change) ─
        if _regime_exe and _regime_exe.confirmed:
            _selected_profile_label = None
            try:
                _profile_regime_obj = _regime_exe
                if _regime_mode != str(getattr(_regime_exe, 'mode', '') or '').strip().upper():
                    _profile_regime_obj = types.SimpleNamespace(
                        mode=_regime_mode,
                        confirmed=bool(getattr(_regime_exe, 'confirmed', False)),
                        confidence=_safe_float(getattr(_regime_exe, 'confidence', 0.0), 0.0),
                    )
                _selector_result = _run_regime_profile_auto_selector(settings, _profile_regime_obj, _db_client_ref)
                if _selector_result.get('ok') and not _selector_result.get('skipped'):
                    _selected_profile_label = str(_selector_result.get('selected_profile') or '').strip().upper() or None
                    print(
                        f"[RegimeSelector] {_selector_result.get('regime')} "
                        f"{_selector_result.get('active_before')} -> {_selector_result.get('selected_profile')} "
                        f"switched={_selector_result.get('switched')} reason={_selector_result.get('reason')}"
                    )
                elif _selector_result.get('reason') not in ('selector_disabled', 'no_profiles_for_regime', 'regime_unconfirmed'):
                    print(f"[RegimeSelector] skipped: {_selector_result.get('reason')}")
            except Exception as _rse:
                print(f"[RegimeSelector] execute_once selector failed: {_rse}")
            try:
                _regime_apply_result = _apply_regime_params(settings, _regime_mode, _db_client_ref)
                if not _regime_apply_result.get('skipped'):
                    print(f"[RegimeParams] execute_once: applied={list(_regime_apply_result.get('applied', {}).keys())}")
                if settings is not None:
                    try:
                        settings.current_regime_mode = _regime_mode
                        _active_profile_label = str(
                            _regime_apply_result.get('profile')
                            or _selected_profile_label
                            or _get_active_profile_label(_db_client_ref, _regime_mode)
                            or 'A'
                        ).strip().upper() or 'A'
                        settings.current_regime_profile_label = _active_profile_label
                    except Exception:
                        pass
                _harmony_result = _apply_mode_profile_harmony(
                    settings, _regime_mode, getattr(settings, 'current_regime_profile_label', None),
                    profile_params=_regime_apply_result.get('raw_params') or {},
                )
                _persist_runtime_selection_snapshot(
                    _db_client_ref,
                    {
                        "selected_mode": _regime_mode,
                        "selected_profile": str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                        "detected_mode": str(getattr(_regime_exe, 'mode', '') or '').strip().upper() or None,
                        "mode_selector": _mode_selector_result if isinstance(_mode_selector_result, dict) else {},
                        "profile_selector": _selector_result if isinstance(_selector_result, dict) else {},
                        "apply_result": {
                            "skipped": bool(_regime_apply_result.get('skipped')),
                            "applied_keys": list((_regime_apply_result.get('applied') or {}).keys()),
                            "rejected_keys": list((_regime_apply_result.get('rejected') or {}).keys()),
                        },
                        "harmony_result": _harmony_result,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                try:
                    _kelly_ctx_mult = _get_kelly_context_multiplier(settings, regime=_regime_mode, trade_type='spot')
                    _kpi_ctx_mult = _get_kpi_context_multiplier(_db_client_ref)
                    _persist_mode_profile_harmony_snapshot(
                        _db_client_ref,
                        {
                            "selected_mode": _regime_mode,
                            "selected_profile": str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                            "detected_mode": str(getattr(_regime_exe, 'mode', '') or '').strip().upper() or None,
                            "kelly_context_multiplier": round(_kelly_ctx_mult, 4),
                            "kpi_multiplier": round(_kpi_ctx_mult, 4),
                            "combined_multiplier": round(_kelly_ctx_mult * _kpi_ctx_mult, 4),
                            "harmony_result": _harmony_result,
                            "sso_target_daily_usd": float(getattr(settings, 'sso_target_daily_usd', 40.0) or 40.0),
                            "sso_stagnant_min_hours": float(getattr(settings, 'sso_stagnant_min_hours', 2.5) or 2.5),
                            "sso_stagnant_min_hours_spot_bull": float(getattr(settings, 'sso_stagnant_min_hours_spot_bull', 3.75) or 3.75),
                            "sso_stagnant_min_hours_range_scalp": float(getattr(settings, 'sso_stagnant_min_hours_range_scalp', 1.5) or 1.5),
                            "sso_stagnant_min_hours_preservation": float(getattr(settings, 'sso_stagnant_min_hours_preservation', 2.25) or 2.25),
                            "sso_stagnant_min_hours_futures_short": float(getattr(settings, 'sso_stagnant_min_hours_futures_short', 2.38) or 2.38),
                            "sso_slow_min_hours_range_scalp": float(getattr(settings, 'sso_slow_min_hours_range_scalp', 1.0) or 1.0),
                            "sso_decay_min_hours_range_scalp": float(getattr(settings, 'sso_decay_min_hours_range_scalp', 1.7) or 1.7),
                            "range_scalp_sl_pct": float(getattr(settings, 'range_scalp_sl_pct', 0.8) or 0.8),
                            "range_scalp_tp_pct": float(getattr(settings, 'range_scalp_tp_pct', 1.8) or 1.8),
                            "range_scalp_max_rsi": float(getattr(settings, 'range_scalp_max_rsi', 60.0) or 60.0),  # s73 A3: fallback 48→60
                            "range_scalp_breakeven_pct": float(getattr(settings, 'range_scalp_breakeven_pct', 0.40) or 0.40),
                            "smart_tp_extend_enabled_range_scalp": bool(getattr(settings, 'smart_tp_extend_enabled_range_scalp', True)),
                            "entry_quality_max_failures_range_scalp": int(getattr(settings, 'entry_quality_max_failures_range_scalp', 1) or 1),
                            "sso_slow_velocity_threshold": float(getattr(settings, 'sso_slow_velocity_threshold', 0.001) or 0.001),
                            "sso_reentry_min_strength_diff": float(getattr(settings, 'sso_reentry_min_strength_diff', 0.3) or 0.3),
                            "futures_breakeven_activation_pct": float(getattr(settings, 'futures_breakeven_activation_pct', 1.0) or 1.0),
                            "futures_trailing_activation_pct": float(getattr(settings, 'futures_trailing_activation_pct', 2.0) or 2.0),
                            "futures_trailing_distance_pct": float(getattr(settings, 'futures_trailing_distance_pct', 1.5) or 1.5),
                            "futures_trail_floor_pct": float(getattr(settings, 'futures_trail_floor_pct', 0.4) or 0.4),
                            "futures_trail_ceiling_pct": float(getattr(settings, 'futures_trail_ceiling_pct', 1.2) or 1.2),
                            "futures_trail_atr_multiplier_low": float(getattr(settings, 'futures_trail_atr_multiplier_low', 1.5) or 1.5),
                            "futures_trail_atr_multiplier_mid": float(getattr(settings, 'futures_trail_atr_multiplier_mid', 1.2) or 1.2),
                            "futures_trail_atr_multiplier_high": float(getattr(settings, 'futures_trail_atr_multiplier_high', 1.0) or 1.0),
                            "futures_trail_atr_multiplier_deep": float(getattr(settings, 'futures_trail_atr_multiplier_deep', 0.8) or 0.8),
                            "max_positions_spot_bull": int(getattr(settings, 'max_positions_spot_bull', 20) or 20),
                            "max_positions_range_scalp": int(getattr(settings, 'max_positions_range_scalp', 10) or 10),
                            "max_positions_preservation": int(getattr(settings, 'max_positions_preservation', 5) or 5),
                            "max_positions_futures_short": int(getattr(settings, 'max_positions_futures_short', 12) or 12),
                            "portfolio_concentration_enabled": bool(getattr(settings, 'portfolio_concentration_enabled', True)),
                            "portfolio_concentration_alpha": float(getattr(settings, 'portfolio_concentration_alpha', 0.9) or 0.9),
                            "portfolio_concentration_min_mult": float(getattr(settings, 'portfolio_concentration_min_mult', 0.75) or 0.75),
                            "portfolio_concentration_max_mult": float(getattr(settings, 'portfolio_concentration_max_mult', 1.35) or 1.35),
                            "portfolio_concentration_pressure_start": float(getattr(settings, 'portfolio_concentration_pressure_start', 0.5) or 0.5),
                            "portfolio_concentration_pressure_boost": float(getattr(settings, 'portfolio_concentration_pressure_boost', 0.4) or 0.4),
                            "futures_max_new_per_cycle": int(getattr(settings, 'futures_max_new_per_cycle', 3) or 3),
                            "min_hold_minutes": int(getattr(settings, 'min_hold_minutes', 120) or 120),
                            "min_hold_minutes_spot_bull": int(getattr(settings, 'min_hold_minutes_spot_bull', 120) or 120),
                            "min_hold_minutes_range_scalp": int(getattr(settings, 'min_hold_minutes_range_scalp', 30) or 30),
                            "futures_long_sl_pct": float(getattr(settings, 'futures_long_sl_pct', 2.5) or 2.5),
                            "futures_long_tp_pct": float(getattr(settings, 'futures_long_tp_pct', 5.0) or 5.0),
                            "max_rebalance_per_cycle": int(getattr(settings, 'max_rebalance_per_cycle', 2) or 2),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception as _harmony_err:
                    print(f"[Harmony] execute_once snapshot failed: {_harmony_err}")
                _switch_id = None
                _mode_changed = bool(_active_mode_before and _active_mode_before != _regime_mode)
                _profile_changed = bool(_active_profile_before and _active_profile_before != settings.current_regime_profile_label)
                if _mode_changed or _profile_changed:
                    _switch_id = _build_transition_id(_active_mode_before, _regime_mode, settings.current_regime_profile_label)
                    _transition_policy = _get_transition_policy(_active_mode_before, _regime_mode)
                    _updated_positions = _reevaluate_open_spot_positions(
                        position_manager,
                        settings,
                        _regime_mode,
                        settings.current_regime_profile_label,
                        switch_id=_switch_id,
                    )
                    _updated_futures_positions = 0
                    try:
                        _fc_reval = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None), injected_client=getattr(comp, 'futures_client', None))
                        _updated_futures_positions = _fc_reval.reevaluate_open_positions(
                            settings,
                            _regime_mode,
                            settings.current_regime_profile_label,
                            switch_id=_switch_id,
                            transition_policy=_transition_policy,
                        )
                    except Exception as _reval_fut_err:
                        print(f"[Transition] futures re-eval failed: {_reval_fut_err}")
                    _persist_transition_state(
                        _db_client_ref,
                        {
                            "active": False,
                            "switch_id": _switch_id,
                            "from_mode": _active_mode_before,
                            "to_mode": _regime_mode,
                            "from_profile": _active_profile_before,
                            "to_profile": getattr(settings, 'current_regime_profile_label', None),
                            "policy": _transition_policy,
                            "updated_positions": _updated_positions,
                            "updated_futures_positions": _updated_futures_positions,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    print(
                        f"[Transition] switch_id={_switch_id} mode_changed={_mode_changed} "
                        f"profile_changed={_profile_changed} updated_spot_positions={_updated_positions} "
                        f"updated_futures_positions={_updated_futures_positions}"
                    )
            except Exception as _rpe:
                print(f"[RegimeParams] execute_once application failed: {_rpe}")

    # ── Auto BNB commission discount management (after regime detection) ───────────
    try:
        from binance_commission import auto_manage_bnb_discount
        _fc = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None), injected_client=getattr(comp, 'futures_client', None))
        _bnb_status = auto_manage_bnb_discount(
            settings=settings,
            spot_client=client,
            futures_client=_fc,
            db_client=_db_client_ref,
            regime_mode=_regime_mode,
        )
        print(f"[BNB] regime={_bnb_status.get('regime_mode')} spot={_bnb_status['spot_bnb']:.4f} "
              f"futures={_bnb_status['futures_bnb']:.4f} price={_bnb_status['bnb_price']:.2f} "
              f"use_bnb={_bnb_status['use_bnb']} source={_bnb_status.get('usdt_source')} "
              f"actions=[spot:{_bnb_status['spot_action']}, futures:{_bnb_status['futures_action']}]")
        if _bnb_status['errors']:
            print(f"[BNB] errors: {_bnb_status['errors']}")
    except Exception as _bnb_err:
        print(f"[BNB] auto-manage error: {_bnb_err}")

    # ── Step -1: Audit on startup — cierra activos de trading_excluded_symbols ──
    global _AUDIT_STARTUP_DONE
    if not _AUDIT_STARTUP_DONE and getattr(settings, 'auto_close_excluded_on_startup', True):
        try:
            from trade_utils import get_symbol_filters, align_quantity_to_step, is_quantity_valid
            _excl = set(getattr(settings, 'trading_excluded_symbols', []))
            if _excl:
                _bal_audit = client.get_account_balance()
                for _excl_sym in _excl:
                    _asset = _excl_sym.replace('USDT', '')
                    _qty_held = float(_bal_audit.get(_asset, 0))
                    if _qty_held <= 0:
                        continue
                    try:
                        _price_excl = _get_cycle_price(client, _excl_sym)
                        if _qty_held * _price_excl < 5.0:
                            continue
                        _filt_excl = get_symbol_filters(client, _excl_sym)
                        _qty_sell = align_quantity_to_step(_qty_held, _filt_excl['step'])
                        _valid_excl, _ = is_quantity_valid(_qty_sell, _price_excl, _filt_excl)
                        if not _valid_excl:
                            continue
                        _ord_excl = client.create_market_order(symbol=_excl_sym, side='SELL', quantity=_qty_sell)
                        if not client.verify_order_filled(_ord_excl, f"Excluded SELL {_excl_sym}"):
                            print(f"[WARN] Excluded SELL {_excl_sym}: order NOT confirmed filled")
                            continue
                        _exit_p = float((_ord_excl.get('fills') or [{}])[0].get('price', 0) or _price_excl)
                        if position_manager:
                            position_manager.close_all_positions_for_symbol(
                                _excl_sym,
                                _exit_p,
                                'excluded_symbol_cleanup',
                                exit_context={
                                    'regime_mode': _regime_mode,
                                    'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                },
                            )
                        _overflow_positions.discard(_excl_sym)
                        _VOLSPIKE_POSITIONS.discard(_excl_sym)
                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                     f"[AuditStartup] Sold excluded {_excl_sym}: {_qty_sell} @ {_exit_p:.4f}")
                        print(f"[AuditStartup] Sold excluded {_excl_sym}: {_qty_sell} @ {_exit_p:.4f}")
                    except Exception as _ae:
                        print(f"[AuditStartup] Could not sell {_excl_sym}: {_ae}")
        except Exception as _audit_err:
            print(f"[AuditStartup] Error: {_audit_err}")
        _AUDIT_STARTUP_DONE = True

    # ── Step 0: SL/TP monitor — runs every cycle before any new trades ─────────
    sl_tp_closes = _check_sl_tp_positions(
        client,
        settings,
        position_manager,
        news_client=news_client,
        regime=_regime_mode,
        monitor_only=False,
        sso7_candidate_cache=_SSO7_BEST_CANDIDATE_CACHE,
    )
    if sl_tp_closes:
        print(f"SL/TP closed {len(sl_tp_closes)} position(s): {[c['symbol'] for c in sl_tp_closes]}")

    # ── Step 0.5: Futures positions check + auto-fund on FUTURES_SHORT ──────────
    futures_closes = []
    _fc = None
    if getattr(settings, 'futures_enabled', True):
        try:
            _fc = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None), injected_client=getattr(comp, 'futures_client', None))

            # CapBridge: bidirectional autonomous spot↔futures capital rebalancing
            if getattr(settings, 'futures_capital_bridge_enabled', True):
                try:
                    _cb_spot_usdt   = float(client.get_account_balance().get('USDT', 0.0))
                    _cb_open_shorts = sum(1 for p in _fc._open_positions.values() if not p.closed and p.side == 'SHORT')
                    _cb_max_shorts  = getattr(settings, 'futures_max_shorts', 5)
                    _cb_open_spots  = len(position_manager.get_all_positions()) if position_manager else 0
                    # UnifiedPM: use futures_available from unified balance for more accurate kelly_margin
                    if _get_param(settings, 'use_unified_portfolio_manager') and portfolio_manager:
                        try:
                            _unified_cb = portfolio_manager.get_unified_balance(_fc)
                            _cb_futures_avail = _unified_cb.get('futures_available_after_reserve', _unified_cb.get('futures_available', _fc.get_available_usdt()))
                        except Exception:
                            _cb_futures_avail = _fc.get_available_usdt()
                    else:
                        _cb_futures_avail = _fc.get_available_usdt()
                    _cb_kelly_margin = max(
                        getattr(settings, 'futures_usdt_per_trade', 150.0),
                        _cb_futures_avail * getattr(settings, 'futures_kelly_cap_pct', 0.30)
                    )
                    _cb_kelly_spot  = _get_param(settings, 'kelly_base_usdt')
                    _cb_min_tx      = getattr(settings, 'futures_capital_bridge_min_transfer', 10.0)
                    _cb_result = _fc.smart_rebalance_capital(
                        spot_usdt        = _cb_spot_usdt,
                        open_shorts      = _cb_open_shorts,
                        max_shorts       = _cb_max_shorts,
                        kelly_margin     = _cb_kelly_margin,
                        open_spot_count  = _cb_open_spots,
                        kelly_spot_amount= _cb_kelly_spot,
                        regime           = _regime_mode,
                        spot_api_key     = settings.binance_api_key,
                        spot_api_secret  = settings.binance_api_secret,
                        min_transfer     = _cb_min_tx,
                    )
                    if _cb_result.get('error'):
                        print(f"[CapBridge] ERROR logged for analysis: dir={_cb_result['direction']} "
                              f"amount=${_cb_result['amount']:.2f} error={_cb_result['error']}")
                except Exception as _cb_err:
                    print(f"[CapBridge] ERROR: exception in capital bridge setup: {_cb_err}")

            # A3: Auto-close ALL shorts if regime switched OUT of bear modes
            # Whipsaw guard: only close after N consecutive non-short cycles
            global _non_short_regime_cycles
            _open_short_keys = [k for k, p in _fc._open_positions.items() if not p.closed and p.side == "SHORT"]
            _min_cycles = _get_param(settings, 'min_cycles_before_close_shorts')
            if _regime_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                _non_short_regime_cycles += 1
            else:
                _non_short_regime_cycles = 0  # reset — still in short-friendly mode

            # R1: Auto-close LONG futures when regime is FUTURES_SHORT — they are directionally wrong
            # No whipsaw guard needed: LONGs in a confirmed bear regime are immediately incorrect.
            if _regime_mode == "FUTURES_SHORT" and _regime_exe and _regime_exe.confirmed:
                _open_long_keys = [k for k, p in _fc._open_positions.items()
                                   if not p.closed and p.side == "LONG"]
                for _lkey in _open_long_keys:
                    _lpos = _fc._open_positions.get(_lkey)
                    if not _lpos:
                        continue
                    _lsym = _lpos.symbol
                    try:
                        _lcur = _fc.get_price(_lsym)
                        _lunr = _lpos.unrealized_pnl(_lcur)
                        _result = _fc.close_long(_lsym, _lpos.qty, reason="regime_futures_short")
                        if _result:
                            futures_closes.append({"symbol": _lsym, "reason": "regime_futures_short", "pnl": _lunr})
                            print(f"[Futures] R1: Closed LONG {_lsym} (PnL~${_lunr:.2f}) — FUTURES_SHORT regime, directionally wrong")
                    except Exception as _le:
                        print(f"[Futures] R1: Failed to close LONG {_lsym}: {_le}")

            # B2: Auto-close residual spot LONGs in FUTURES_SHORT regime — directionally wrong in bear
            # Only close positions with positive PnL (protect from crystallising losses on healthy spots)
            # Exception: stablecoins skip — detected by symbol prefix OR by price (0.98-1.02)
            if _regime_mode == "FUTURES_SHORT" and _regime_exe and _regime_exe.confirmed:
                try:
                    _spot_positions = position_manager.get_all_positions() if position_manager else {}
                    _commission_rate_b2 = _get_commission_config(settings).rate_for('spot')
                    for _b2_sym, _b2_pos_list in _spot_positions.items():
                        if not _b2_pos_list:
                            continue
                        # Skip true stablecoins by symbol prefix (USDC, BUSD, TUSD, DAI, etc.)
                        _STABLECOIN_PREFIXES = {'USDC', 'BUSD', 'TUSD', 'FDUSD', 'DAI', 'USD1', 'RLUS', 'PAXG', 'XAUT'}
                        _b2_asset_check = _b2_sym.replace('USDT', '')
                        if any(_b2_asset_check.startswith(p) for p in _STABLECOIN_PREFIXES):
                            continue  # known stablecoin asset — skip
                        _b2_price = None
                        try:
                            _b2_price = _get_cycle_price(client, _b2_sym)
                        except Exception:
                            continue
                        if _b2_price and 0.98 <= _b2_price <= 1.02:
                            continue  # stablecoin by price — skip
                        for _b2_pos in _b2_pos_list:
                            if getattr(_b2_pos, 'closed', False):
                                continue
                            _b2_entry = float(getattr(_b2_pos, 'entry_price', 0) or 0)
                            if _b2_entry <= 0 or _b2_price is None:
                                continue
                            _b2_pnl_pct = (_b2_price - _b2_entry) / _b2_entry * 100
                            # Only close if in profit (>+0.3%) to avoid locking in spot losses
                            if _b2_pnl_pct >= 0.3:
                                try:
                                    _b2_asset = _b2_sym.replace("USDT", "")
                                    _b2_bal = client.get_account_balance()
                                    _b2_qty_held = float(_b2_bal.get(_b2_asset, 0))
                                    if _b2_qty_held <= 0:
                                        continue
                                    _b2_filters = get_symbol_filters(client, _b2_sym)
                                    _b2_qty = align_quantity_to_step(_b2_qty_held, _b2_filters["step"])
                                    _b2_valid, _b2_reason = is_quantity_valid(_b2_qty, _b2_price, _b2_filters)
                                    if not _b2_valid:
                                        print(f"[B2] {_b2_sym}: invalid qty {_b2_qty} — {_b2_reason}")
                                        continue
                                    _b2_order = client.create_market_order(symbol=_b2_sym, side="SELL", quantity=_b2_qty)
                                    if not client.verify_order_filled(_b2_order, f"B2 SELL {_b2_sym}"):
                                        print(f"[WARN] B2 SELL {_b2_sym}: order NOT confirmed filled — skipping")
                                        continue
                                    _b2_exit = float(((_b2_order.get("fills") or [{}])[0]).get("price", 0) or _b2_price)
                                    position_manager.close_all_positions_for_symbol(
                                        _b2_sym,
                                        _b2_exit,
                                        "regime_futures_short_spot_cleanup",
                                        _commission_rate_b2,
                                        exit_context={
                                            'regime_mode': _regime_mode,
                                            'profile_label': str(getattr(settings, 'current_regime_profile_label', '') or '').strip().upper() or None,
                                        },
                                    )
                                    _overflow_positions.discard(_b2_sym)
                                    _VOLSPIKE_POSITIONS.discard(_b2_sym)
                                    print(f"[B2] Closed spot LONG {_b2_sym} @ {_b2_exit:.4f} pnl_pct={_b2_pnl_pct:+.2f}% — FUTURES_SHORT regime, in profit")
                                except Exception as _b2_err:
                                    print(f"[B2] Failed to close spot {_b2_sym}: {_b2_err}")
                except Exception as _b2_outer:
                    print(f"[B2] Spot residual close error: {_b2_outer}")

            if _open_short_keys and _regime_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                _transition_target_mode = None
                if isinstance(_pre_switch_state, dict) and _pre_switch_state.get("active"):
                    _transition_target_mode = _pre_switch_state.get("to_mode")
                _deadline_cycles = int(_transition_policy.get('deadline_cycles', _min_cycles) or _min_cycles)
                _protect_threshold = _get_param(settings, 'futures_protect_profit_usdt')
                _protect_profit = bool(_transition_policy.get('protect_profit', True))
                if _non_short_regime_cycles >= _deadline_cycles:
                    _closed_count = 0
                    for _key in _open_short_keys:
                        _pos = _fc._open_positions.get(_key)
                        if not _pos:
                            continue
                        _sym = _pos.symbol
                        try:
                            _cur_price = _fc.get_price(_sym)
                            _unr_pnl = _pos.unrealized_pnl(_cur_price)
                        except Exception:
                            _unr_pnl = 0.0
                        if _protect_profit and _unr_pnl >= _protect_threshold:
                            print(f"[Futures] SHORT {_sym}: PnL=${_unr_pnl:.2f} >= ${_protect_threshold} — PROTECTING (regime={_regime_mode}, letting SL/TP resolve)")
                        else:
                            try:
                                _result = _fc.close_short(_sym, _pos.qty, reason="regime_change")
                                futures_closes.append({"symbol": _sym, "reason": "regime_change", "result": _result})
                                _futures_overflow_positions.discard(_sym)
                                print(f"[Futures] Closed SHORT {_sym} (PnL=${_unr_pnl:.2f} < ${_protect_threshold}) due to regime → {_regime_mode}")
                                _closed_count += 1
                            except Exception as _ce:
                                print(f"[Futures] Failed to close {_sym} on regime change: {_ce}")
                    if _closed_count > 0:
                        _non_short_regime_cycles = 0
                    print(f"[Futures] Regime change {_regime_mode}: closed {_closed_count}/{len(_open_short_keys)} shorts (protected profitable ones)")
                else:
                    if _transition_target_mode and _transition_target_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                        print(
                            f"[Futures] Pre-switch to {_transition_target_mode} active "
                            f"({_non_short_regime_cycles}/{_deadline_cycles} cycles) — holding shorts with deadline"
                        )
                    else:
                        print(f"[Futures] Regime is {_regime_mode} ({_non_short_regime_cycles}/{_deadline_cycles} cycles) — holding shorts (whipsaw guard)")

            # CapBridge handles idle-funds return via smart_rebalance_capital (Case 3 above)

            # Normal SL/TP cycle check (passes settings for trailing/SmartTP and client ref for momentum)
            _wire_futures_client(_fc, client, settings)
            _cycle_closes = _fc.check_positions(settings=settings, monitor_only=False)
            if _cycle_closes:
                futures_closes.extend(_cycle_closes)
                print(f"[Futures] Closed {len(_cycle_closes)} position(s) via SL/TP: {_cycle_closes}")
                _sl_count_this_cycle = 0
                for _cc in _cycle_closes:
                    _cc_sym = _cc.get('symbol') if isinstance(_cc, dict) else getattr(_cc, 'symbol', None)
                    if _cc_sym:
                        _futures_overflow_positions.discard(_cc_sym)
                        # FIX-F1: Register SL cooldown to prevent immediate re-entry on the same symbol
                        if _cc.get('reason') == 'stop_loss':
                            _sl_count_this_cycle += 1
                            _sl_cd_hours = getattr(settings, 'futures_sl_cooldown_hours', 4.0)
                            _FUTURES_SL_COOLDOWN[_cc_sym] = datetime.now(timezone.utc)
                            _set_cooldown(_cc_sym, 'futures_sl', _sl_cd_hours, {'reason': 'sl_hit'})
                            print(f'[Futures/SL-CD] {_cc_sym}: cooldown {_sl_cd_hours}h — no re-entry until next window')
                # PB2: Mass SL circuit breaker — global pause if too many positions stopped together
                _mass_sl_threshold = int(getattr(settings, 'futures_mass_sl_threshold', 3))
                if _sl_count_this_cycle >= _mass_sl_threshold:
                    _mass_cd_hours = float(getattr(settings, 'futures_mass_sl_cooldown_hours', 4.0))
                    from datetime import timedelta as _td_mass
                    _FUTURES_MASS_SL_COOLDOWN_UNTIL = datetime.now(timezone.utc) + _td_mass(hours=_mass_cd_hours)
                    print(f'[Futures/MASS-SL] {_sl_count_this_cycle} positions stopped in same cycle! '
                          f'GLOBAL COOLDOWN {_mass_cd_hours}h — no new shorts until {_FUTURES_MASS_SL_COOLDOWN_UNTIL.strftime("%H:%M UTC")}')
        except Exception as _fce:
            print(f"[Futures] check_positions error: {_fce}")

    # Tactical reset policy: if combined open basket is weak (<= +$10), stale and with
    # low momentum, mark reset recommendation and optionally execute full close.
    try:
        _reset_positions = []
        _fut_hours_default = float(getattr(settings, 'basket_reset_futures_hours_default', 4.0) or 4.0)
        if _fc is not None:
            for _f in _fc.get_open_positions_summary() or []:
                _reset_positions.append({
                    'symbol': _f.get('symbol'),
                    'unrealized_pnl': float(_f.get('pnl', 0.0) or 0.0),
                    'hours_held': float(_f.get('hours_held', _fut_hours_default) or _fut_hours_default),
                    'pnl_pct': float(_f.get('pnl_pct', 0.0) or 0.0) / 100.0,
                    'momentum_score': float(_f.get('signal_strength', 0.0) or 0.0),
                })

        _spot_positions = position_manager.get_all_positions() if position_manager else {}
        for _sym, _plist in (_spot_positions or {}).items():
            if not _plist:
                continue
            _sp_price = _get_cycle_price(client, _sym)
            for _p in _plist:
                _entry = float(getattr(_p, 'entry_price', 0) or 0)
                _qty = float(getattr(_p, 'quantity', 0) or 0)
                if _entry <= 0 or _qty <= 0:
                    continue
                _pnl = (_sp_price - _entry) * _qty
                _opened = getattr(_p, 'entry_time', None)
                _hours = _fut_hours_default
                if isinstance(_opened, str) and _opened:
                    try:
                        _dt = datetime.fromisoformat(_opened.replace('Z', '+00:00'))
                        if _dt.tzinfo is None:
                            _dt = _dt.replace(tzinfo=timezone.utc)
                        _hours = max(0.0, (datetime.now(timezone.utc) - _dt).total_seconds() / 3600.0)
                    except Exception:
                        pass
                _reset_positions.append({
                    'symbol': _sym,
                    'unrealized_pnl': _pnl,
                    'hours_held': _hours,
                    'pnl_pct': ((_sp_price - _entry) / _entry) if _entry > 0 else 0.0,
                    'momentum_score': 0.0,
                })

        _reset_decision = compute_basket_reset_decision(
            _reset_positions,
            pnl_threshold_usd=float(getattr(settings, 'basket_reset_pnl_threshold_usd', 10.0) or 10.0),
            min_avg_age_hours=float(getattr(settings, 'basket_reset_min_avg_age_hours', 4.0) or 4.0),
            momentum_positive_count_limit=int(getattr(settings, 'basket_reset_max_positive_momentum_positions', 1) or 1),
        )

        if _db_client_ref:
            _db_client_ref.execute_update(
                "INSERT INTO trading_runtime_state (key, value, updated_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                ('basket_reset_decision', json.dumps(_reset_decision), json.dumps(_reset_decision)),
            )

        if _reset_decision.get('trigger'):
            _auto_reset = bool(getattr(settings, 'basket_reset_auto_execute', False))
            print(
                f"[BasketReset] trigger=True pnl=${_reset_decision.get('combined_pnl_usd')} "
                f"avg_age_h={_reset_decision.get('avg_age_hours')} auto_execute={_auto_reset}"
            )
            if _db_client_ref:
                _db_client_ref.execute_update(
                    "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) VALUES (%s, %s, %s, %s, %s)",
                    ('SYSTEM', 'system', 'basket_reset_triggered', json.dumps(_reset_decision), False),
                )
            if _auto_reset and not monitor_only:
                for _sym, _plist in (_spot_positions or {}).items():
                    if not _plist:
                        continue
                    try:
                        _exit = _get_cycle_price(client, _sym)
                        position_manager.close_all_positions_for_symbol(_sym, _exit, 'basket_reset_auto')
                    except Exception as _sp_reset_err:
                        print(f"[BasketReset] spot close failed {_sym}: {_sp_reset_err}")
                if _fc is not None:
                    for _k, _p in list(_fc._open_positions.items()):
                        if getattr(_p, 'closed', False):
                            continue
                        try:
                            if str(getattr(_p, 'side', '')).upper() == 'SHORT':
                                _fc.close_short(_p.symbol, _p.qty, reason='basket_reset_auto')
                            else:
                                _fc.close_long(_p.symbol, _p.qty, reason='basket_reset_auto')
                        except Exception as _fu_reset_err:
                            print(f"[BasketReset] futures close failed {_p.symbol}: {_fu_reset_err}")
    except Exception as _reset_err:
        print(f"[BasketReset] decision error: {_reset_err}")

    # ── Step 1: Determine top symbols ─────────────────────────────────────────
    _selection_begin_cycle(
        comp.db_client,
        _regime_mode,
        _get_active_profile_label(comp.db_client, _regime_mode),
    )
    top_signals = _select_symbols(settings, news_client, client, portfolio_manager, regime=_regime_mode, db_client=comp.db_client)

    # FUTURES_SHORT mode: always ensure futures_symbols are in the pool.
    # The long-signal generator only produces spot candidates — futures symbols
    # (BTC/ETH/SOL/BNB etc.) must be explicitly injected so open_short() can run.
    # BUG-E6: cache screened_pairs result for re-use within the same cycle
    _cached_screened_pairs: dict = {}
    if getattr(settings, 'screener_enabled', True):
        try:
            from pair_screener import get_screened_pairs as _gsp
            _active_profile_label = _get_active_profile_label(_db_client_ref, _regime_mode)
            _cached_screened_pairs = _gsp(
                settings=settings,
                regime_mode=_regime_mode,
                profile_label=_active_profile_label,
                db_client=comp.db_client,
            )
            # Persist screener candidate selection for full traceability
            _audit_screener_selection(
                comp.db_client,
                _regime_mode,
                _cached_screened_pairs,
                profile_label=_active_profile_label,
            )
        except Exception:
            pass

    if (_regime_mode == "FUTURES_SHORT" and _regime_exe is not None
            and _regime_exe.confirmed and getattr(settings, 'futures_enabled', True)):
        _f_syms = list(_get_param(settings, 'futures_symbols'))
        if _cached_screened_pairs:
            _f_syms = list(dict.fromkeys(_f_syms + _cached_screened_pairs.get("futures", [])))
        # Build set of symbols already in top_signals to avoid duplicates
        _existing_syms = {s['symbol'] if isinstance(s, dict) else s for s in top_signals}
        _asg = _get_cycle_asg(client)  # BUG-E2: reuse cycle-scoped instance
        _new_futures = []
        for _fsym in _f_syms:
            if _fsym in _existing_syms:
                continue
            # Enrich with real technical data so overflow eligibility check has real values
            _fsig = {"symbol": _fsym, "score": 50, "strength": 0.5, "reason": "FUTURES_SHORT_AUTO"}
            try:
                # Use AutonomousSignalGenerator which correctly fetches klines and runs analysis
                _ta = _asg.analyze_symbol(_fsym, interval=getattr(settings, 'interval', '1h'))
                _ta_signal = str((_ta or {}).get('signal') or '').strip().upper()
                _cache_fresh = _is_analysis_cache_fresh(_fsym, _regime_mode)
                _cache_age_min = _analysis_cache_age_minutes(_fsym)
                if not _cache_fresh:
                    _age_txt = f"{_cache_age_min:.1f}m" if _cache_age_min is not None else "unknown"
                    print(
                        f"[Futures/Inject] {_fsym}: analysis cache stale for regime={_regime_mode} "
                        f"(age={_age_txt}) — skipping injection"
                    )
                    continue
                # FUTURES_SHORT candidates: SELL always valid; HOLD can be valid on
                # overbought bounces with enough strength.
                _hold_min_strength = float(getattr(settings, 'futures_hold_short_min_strength', 0.55) or 0.55)
                _hold_min_rsi = float(getattr(settings, 'futures_hold_short_min_rsi', 60.0) or 60.0)
                if _ta and isinstance(_ta, dict):
                    _ta_strength = float(_ta.get("strength", 0.5))
                    _ta_ind      = _ta.get("indicators", {})
                    _ta_rsi      = float(_ta_ind.get("rsi", 50.0))
                    _is_short_candidate = (
                        _ta_signal == 'SELL'
                        or (_ta_signal == 'HOLD' and _ta_strength >= _hold_min_strength and _ta_rsi >= _hold_min_rsi)
                    )
                    if not _is_short_candidate:
                        if _ta_signal:
                            print(f"[Futures/Inject] {_fsym}: skipped signal={_ta_signal} (requires SELL or HOLD with strength>={_hold_min_strength:.2f} and RSI>={_hold_min_rsi:.1f})")
                        continue
                    _fsig["strength"]       = _ta_strength
                    _fsig["vol_spike"]      = float(_ta.get("vol_spike", _ta_ind.get("volume_spike", 0.0)))
                    _fsig["rsi"]            = _ta_rsi
                    _fsig["change_24h_pct"] = float(_ta.get("change_24h_pct", 0.0))
                    _fsig["score"]          = int(_ta_strength * 100)
                    _fsig["reason"]         = _ta.get("reason", "FUTURES_SHORT_AUTO")
                    _fsig["expected_side"]  = "SHORT"
                    _fsig["trade_type"]     = "futures"
                    print(f"[Futures/Inject] {_fsym}: strength={_ta_strength:.2f} rsi={_fsig['rsi']:.1f} signal={_ta_signal} candidate=SHORT")
                else:
                    continue
            except Exception as _te:
                print(f"[Futures/Inject] {_fsym}: analysis failed ({_te}) — skipping")
                continue
            # M1: Skip weak signals — avoid noisy injections
            _min_inject_strength = getattr(settings, 'futures_min_inject_strength', 0.50)
            if _fsig.get("strength", 0.0) < _min_inject_strength:
                continue
            _new_futures.append(_fsig)
        if _new_futures:
            print(f"[Futures] FUTURES_SHORT: injecting {[s['symbol'] for s in _new_futures]} into signal pool")
            top_signals = _new_futures + list(top_signals)  # futures first

    if not top_signals:
        print("No buy signals available; skipping new buys this cycle.")
        _selection_complete_cycle({"outcome": "no_signals", "reason": "NO_SIGNALS"})
        return {"top_symbols": [], "sell_orders": [], "buy_orders": [], "sl_tp_closes": sl_tp_closes}

    # Normalize: _select_symbols may return dicts or strings
    if top_signals and isinstance(top_signals[0], dict):
        top_symbols = [s['symbol'] for s in top_signals]
    else:
        top_symbols = list(top_signals)

    # ── Step 2: Generate predictions once (reused by step 3 and step 5) ───────
    # BUG-E4: pass signal_meta so pnl_service reuses pre-computed indicators from Step 1
    _step2_signal_meta = {s['symbol']: s for s in top_signals if isinstance(s, dict)}
    _prediction_trade_type = 'futures' if _regime_mode == 'FUTURES_SHORT' else 'spot'
    predictions = _build_predictions(
        top_symbols,
        client,
        settings,
        portfolio_manager,
        signal_meta=_step2_signal_meta,
        trade_type=_prediction_trade_type,
        regime_mode=_regime_mode,
    )
    _selection_capture_candidates(top_signals, predictions, "predicted")

    # ── Peaks-first: pre-filter before QUBO (hard-reject discarded, advisory for entry_quality) ─
    _eq_gate_mode = str(getattr(settings, 'entry_quality_gate_mode', 'blocking')).lower()
    _active_profile_label_exec = _get_active_profile_label(_db_client_ref, _regime_mode)
    if _eq_gate_mode == "blocking":
        # Full blocking mode — filter as before
        top_signals = _filter_buy_candidates(
            top_signals, _step2_signal_meta, settings, _regime_mode, _active_profile_label_exec
        )
    elif _eq_gate_mode == "advisory":
        # Advisory mode: only hard-reject "discarded" profiles; log other rejections as warnings
        _filtered = []
        for _sig in top_signals:
            _sym = _sig['symbol'] if isinstance(_sig, dict) else _sig
            _pq_prof = str((_sig if isinstance(_sig, dict) else {}).get('peak_quality_profile', '')).lower()
            if _pq_prof == 'discarded':
                print(f"[FilterBuy/Advisory] Hard-rejected {_sym}: PEAK_QUALITY_DISCARDED")
                update_signal_decision(_sym, "rejected", "PEAK_QUALITY_DISCARDED")
                continue
            _ok, _reason = _check_entry_quality(_sym, _step2_signal_meta, settings, _regime_mode, _active_profile_label_exec)
            if not _ok:
                # Advisory: log warning but keep the candidate
                if isinstance(_sig, dict):
                    _sig.setdefault('gate_warnings', []).append({
                        "gate": "entry_quality", "reason": _reason, "action": "advisory_pass",
                    })
                print(f"[FilterBuy/Advisory] {_sym}: WOULD_REJECT ({_reason}) → kept (Peaks advisory mode)")
            _filtered.append(_sig)
        top_signals = _filtered
    # else: "disabled" — skip entry_quality filter entirely

    if top_signals and isinstance(top_signals[0], dict):
        top_symbols = [s['symbol'] for s in top_signals]
    else:
        top_symbols = list(top_signals)
    if not top_signals:
        print("[FilterBuy] All candidates filtered — no entries this cycle.")
        _selection_complete_cycle({"outcome": "all_filtered", "reason": "ENTRY_QUALITY_GATE"})
        return {"top_symbols": [], "sell_orders": [], "buy_orders": [], "sl_tp_closes": sl_tp_closes}

    # ── GAP-B: Profit-potential scoring — rank by EV+prob+strength+regime ───────
    try:
        top_signals = _compute_profit_score(top_signals, predictions, _regime_mode)
    except Exception as _ps_err:
        print(f"[ProfitScore] scoring failed (keeping original order): {_ps_err}")

    # ── QUBO: combinatorial portfolio selection ─────────────────────────────
    # Replaces simple EV-sort with an explicit K-best orthogonal selector.
    # Uses correlation matrix + predicted PnL to maximize risk-adjusted return.
    _pre_qubo_symbols = set(top_symbols)
    try:
        top_signals, predictions = _apply_qubo_selector(
            settings, client, top_signals, predictions, _regime_mode,
            position_manager=position_manager,
        )
    except Exception as _qubo_err:
        print(f"[QUBO] Selector error (keeping original ranking): {_qubo_err}")

    if top_signals and isinstance(top_signals[0], dict):
        top_symbols = [s['symbol'] for s in top_signals]
    else:
        top_symbols = list(top_signals)
    _selection_capture_candidates(top_signals, predictions, "qubo_selected")
    for _qubo_excluded_symbol in _pre_qubo_symbols - set(top_symbols):
        _selection_mark_stage(_qubo_excluded_symbol, "qubo_excluded", "QUBO_EXCLUDED")
    _selection_context["selected_count"] = len(top_symbols)

    # ── SSO-7: Populate best candidate cache for next cycle's monitoring ────────
    try:
        _open_syms = set()
        if position_manager:
            try:
                _open_syms = set(position_manager.get_all_positions().keys())
            except Exception:
                pass
        _sso7_best: Optional[Dict] = None
        _sso7_best_ev = -9999.0
        for _sso7_sig in top_signals:
            _sso7_sym = _sso7_sig['symbol'] if isinstance(_sso7_sig, dict) else _sso7_sig
            if _sso7_sym in _open_syms:
                continue  # skip already-held symbols
            _sso7_pred = predictions.get(_sso7_sym, {})
            _sso7_ev = float(_sso7_pred.get('expected_pnl', 0.0))
            _sso7_prob = float(_sso7_pred.get('success_probability', 0.0))
            _sso7_str = float(_sso7_sig.get('strength', 0.0) if isinstance(_sso7_sig, dict) else 0.0)
            _sso7_vol = float(_sso7_sig.get('vol_spike', 0.0) if isinstance(_sso7_sig, dict) else 0.0)
            if _sso7_ev > _sso7_best_ev:
                _sso7_best_ev = _sso7_ev
                _sso7_best = {'symbol': _sso7_sym, 'strength': _sso7_str,
                              'prob': _sso7_prob, 'vol_spike': _sso7_vol, 'ev': _sso7_ev}
        _SSO7_BEST_CANDIDATE_CACHE = _sso7_best
        if _sso7_best:
            print(f"[SSO-7/Cache] Best candidate: {_sso7_best['symbol']} "
                  f"ev={_sso7_best['ev']:.2f} prob={_sso7_best['prob']:.2f} "
                  f"str={_sso7_best['strength']:.2f} vol={_sso7_best['vol_spike']:.1f}x")
    except Exception as _sso7_cache_err:
        print(f"[SSO-7/Cache] Error: {_sso7_cache_err}")

    # ── Step 3: Slot-pressure reallocation (only when MAX_POSITIONS reached) ──────
    _close_prediction_based(top_symbols, client, settings, portfolio_manager, position_manager, predictions, regime_mode=_regime_mode)

    # ── Step 4: Sell holdings no longer in top ─────────────────────────────────
    balances = client.get_account_balance()
    print("Balances:", balances)
    sell_orders = _sell_holdings_not_in_top(client, settings, balances, top_symbols, portfolio_manager, position_manager)
    if sell_orders:
        balances = client.get_account_balance()
        print("Balances after sells:", balances)

    # ── Step 5: Buy top symbols — pass full signal dicts for aggressive mode ───
    buy_orders = _buy_top_symbols(client, settings, top_signals, portfolio_manager, position_manager, precomputed_predictions=predictions, news_client=news_client, regime_obj=_regime_exe, regime_mode_override=_regime_mode, screened_pairs_cache=_cached_screened_pairs, pre_switch_state=_pre_switch_state, futures_client=comp.futures_client)

    # ── Dynamic cycle scheduling — return next interval based on active regime ──
    _cycle_by_regime = {
        "SPOT_BULL":     getattr(settings, 'cycle_spot_bull',    10),
        "RANGE_SCALP":   getattr(settings, 'cycle_range_scalp',   5),
        "PRESERVATION":  getattr(settings, 'cycle_preservation',  45),
        "FUTURES_SHORT": _get_param(settings, 'cycle_futures_short'),
    }
    _next_cycle = _cycle_by_regime.get(_regime_mode, getattr(settings, 'trade_cycle_minutes', 5))
    print(f"Completed cycle. Next cycle in {_next_cycle} min (regime={_regime_mode})")
    _selection_complete_cycle({
        "outcome": "executed" if buy_orders else "no_entry",
        "selected_symbols": top_symbols,
        "buy_order_count": len(buy_orders),
    })

    # Persist volatile state to DB for restart safety
    if _db_client_ref:
        state_persistence.save_state(
            _db_client_ref,
            futures_sl_cooldown=_FUTURES_SL_COOLDOWN,
            futures_mass_sl_cooldown_until=_FUTURES_MASS_SL_COOLDOWN_UNTIL,
            volspike_cooldown=_VOLSPIKE_COOLDOWN,
            volspike_positions=_VOLSPIKE_POSITIONS,
            override_daily_count=_OVERRIDE_DAILY_COUNT,
            circuit_breaker_until=_circuit_breaker_until,
            consecutive_failures=_consecutive_failures,
        )

    return {"top_symbols": top_symbols, "sell_orders": sell_orders,
            "buy_orders": buy_orders, "sl_tp_closes": sl_tp_closes,
            "futures_closes": futures_closes, "next_cycle_minutes": _next_cycle,
            "regime_mode": _regime_mode}


if __name__ == '__main__':
    settings = get_settings()
    from config_helpers import validate_config
    _validation = validate_config(settings)
    if not _validation.get('ok', False):
        raise SystemExit(f"Config validation failed: {_validation['issues']}")
    execute_once()
