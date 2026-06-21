import time
from datetime import datetime, timezone
from typing import Dict, List

import error_collector as ec
from binance_client import BinanceClient
from market_regime import get_regime, MarketRegime
from futures_client import FuturesClient
from news_client import NewsClient
from signal_store import get_signals, add_signal, mark_signal_as_processed, update_signal_decision, expire_stale_signals
from strategy import rank_buy_signals, rank_with_technical, normalize_symbol, rank_for_portfolio_manager
from analysis_cache import analysis_cache
from autonomous_signals import AutonomousSignalGenerator
from trade_utils import align_quantity_to_step, get_symbol_filters, is_quantity_valid, compute_commission
import pnl_service


# Symbols confirmed invalid on Binance (delisted) — skip without API call
_INVALID_SYMBOLS: set = {"EONUSDT", "ADDUSDT", "MEETONEUSDT", "ATDUSDT", "EOPUSDT", "ETHWUSDT"}

# VolSpike override cooldown: {symbol: datetime_of_last_override_entry}
# Prevents re-entering the same narrative token breakout every 5min cycle
_VOLSPIKE_COOLDOWN: Dict[str, datetime] = {}

# V2: Tracking de métricas VolSpike Override (jun 16 2026)
_VOLSPIKE_POSITIONS: set = set()           # Symbols con override activo
_OVERRIDE_DAILY_COUNT: Dict[str, int] = {}  # date_str -> count
_OVERRIDE_PERFORMANCE: list = []            # Lista de resultados para análisis

# Futures client singleton (lazy-initialized)
_futures_client: FuturesClient = None

# BUG-E2: Cycle-scoped ASG instance — reset each execute_once() call so all
# consumers (BTC trend check, futures inject, pnl_service) share one instance
# and benefit from the per-symbol signal cache without redundant re-instantiation.
_asg_cycle_instance: "AutonomousSignalGenerator" = None
_asg_cycle_id: int = 0  # incremented each cycle to know when to reset

# Whipsaw protection: count consecutive cycles where regime is NOT a short-friendly mode.
# Shorts are only closed when this counter reaches min_cycles_before_close_shorts (config).
_non_short_regime_cycles: int = 0


def _get_futures_client(settings, db_client=None) -> FuturesClient:
    """Lazy singleton for FuturesClient. Reuses instance across cycles."""
    global _futures_client
    if _futures_client is None:
        _futures_client = FuturesClient(
            api_key    = settings.binance_api_key,
            api_secret = settings.binance_api_secret,
            dry_run    = settings.dry_run,
            max_shorts = getattr(settings, 'futures_max_shorts', 2),
            max_longs  = getattr(settings, 'futures_max_longs', 3),
            db_client  = db_client,  # FIX-G: DB persistence
        )
        if getattr(settings, 'futures_hedge_mode_enabled', True):
            _futures_client.enable_hedge_mode()
        print("[Futures] FuturesClient initialized")
    # FIX-G: Attach db_client if it became available after first init (e.g. DB reconnect)
    if db_client and not _futures_client.db_client:
        _futures_client.db_client = db_client
    # Always sync category cap from settings (survives hot-config changes)
    _futures_client.max_largecap_shorts = getattr(settings, 'futures_max_largecap_shorts', 5)
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

# ── Circuit breaker: consecutive order failures (technical errors only) ──────
_consecutive_failures: int = 0
_circuit_breaker_until: datetime = None  # UTC — None = not paused

# ── Funding rate cache: {symbol: (rate_float, fetched_at_monotonic)} ─────────
_funding_rate_cache: Dict[str, tuple] = {}

# ── DB client reference for cooldown persistence (set in execute_once) ───────
_db_client_ref = None

# ── Audit startup flag: run excluded-asset cleanup only once per process ─────
_AUDIT_STARTUP_DONE: bool = False


def _set_cooldown(symbol: str, cooldown_type: str, hours: float, metadata: dict = None) -> None:
    """Persist a cooldown to DB + update RAM dict. Type: 'volspike'|'narrative'|'override_daily'."""
    from datetime import timedelta as _td
    _now = datetime.now(timezone.utc)
    _expires = _now + _td(hours=hours)
    if cooldown_type == 'volspike':
        _VOLSPIKE_COOLDOWN[symbol] = _now
    elif cooldown_type == 'narrative':
        _NARRATIVE_BUY_COOLDOWN[symbol] = _now
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
            pass  # fail-open: RAM dict is still set


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
        "SPOT_BULL":     getattr(settings, 'max_positions_spot_bull',    15),
        "RANGE_SCALP":   getattr(settings, 'max_positions_range_scalp',   8),
        "PRESERVATION":  getattr(settings, 'max_positions_preservation',  5),
        "FUTURES_SHORT": getattr(settings, 'max_positions_futures_short', 4),
    }
    return _mapping.get(regime, getattr(settings, 'max_positions', 12))


def _apply_fng_kelly_boost(trade_allocation: float, fng_value: int, settings) -> float:
    """R1: Behavioral Finance — scale Kelly size by Fear & Greed reading.
    Capitulation (<= fng_capitulation_threshold): max boost (captures fear bottoms aggressively).
    Extreme Fear (<= fng_extreme_fear_threshold): strong boost.
    Extreme Greed (>= fng_extreme_greed_threshold): reduce (avoid buying tops).
    All other values: no change.
    """
    if not getattr(settings, 'fng_kelly_boost_enabled', True):
        return trade_allocation
    cap_thr  = getattr(settings, 'fng_capitulation_threshold', 15)
    fear_thr = getattr(settings, 'fng_extreme_fear_threshold', 20)
    greed_thr = getattr(settings, 'fng_extreme_greed_threshold', 75)
    if fng_value <= cap_thr:
        mult = getattr(settings, 'fng_capitulation_multiplier', 1.50)
        label = f"CAPITULACION F&G={fng_value}"
    elif fng_value <= fear_thr:
        mult = getattr(settings, 'fng_extreme_fear_multiplier', 1.35)
        label = f"EXTREME_FEAR F&G={fng_value}"
    elif fng_value >= greed_thr:
        mult = getattr(settings, 'fng_extreme_greed_multiplier', 0.65)
        label = f"EXTREME_GREED F&G={fng_value}"
    else:
        return trade_allocation
    boosted = round(trade_allocation * mult, 2)
    print(f"[F&G Kelly] {label} → ${trade_allocation:.2f} × {mult} = ${boosted:.2f}")
    return boosted


def _get_funding_rate(symbol: str, settings) -> float:
    """R6: On-chain — fetch & cache funding rate for a symbol.
    Returns 0.0 on error (fail-open: don't block trade on API failure).
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
    except Exception:
        return 0.0


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
    cat_limits = getattr(settings, 'category_max_positions', {
        "LARGE_CAP": 3, "MIDCAP_ALT": 5, "NARRATIVE": 4, "MEME": 2,
    })
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


def _record_order_success() -> None:
    """Reset consecutive failure counter on any successful order."""
    global _consecutive_failures
    if _consecutive_failures > 0:
        print(f"[CircuitBreaker] Resetting failure counter (was {_consecutive_failures})")
        _consecutive_failures = 0


def _is_overflow_eligible(symbol: str, strength: float, prediction: dict, settings) -> bool:
    """Return True if the signal qualifies for an extra slot beyond the regime max.
    Requires Kelly FUERTE tier quality: strength >= overflow_min_strength AND
    success_probability >= overflow_min_prob AND expected_pnl > 0."""
    _min_str  = getattr(settings, 'overflow_min_strength', 0.80)
    _min_prob = getattr(settings, 'overflow_min_prob', 0.65)
    _max_extra = getattr(settings, 'overflow_max_extra', 3)
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
    Requires: high-strength signal + vol spike + confirmed drop + BTC in downtrend.
    Max simultaneous overflow slots controlled by futures_overflow_max_extra (default 3).
    NOTE: review after 1 week of live data to tune thresholds up/down."""
    _min_str   = getattr(settings, 'futures_overflow_min_strength', 0.82)
    _min_vol   = getattr(settings, 'futures_overflow_min_vol_spike', 2.5)
    _min_drop  = getattr(settings, 'futures_overflow_min_drop_24h', -5.0)
    _max_extra = getattr(settings, 'futures_overflow_max_extra', 3)
    _cur_ovfl  = len(_futures_overflow_positions)
    if _cur_ovfl >= _max_extra:
        print(f"[Futures/Overflow] {symbol}: all {_max_extra} overflow slots occupied ({_cur_ovfl}/{_max_extra})")
        return False
    if strength < _min_str:
        print(f"[Futures/Overflow] {symbol}: strength={strength:.2f} < min={_min_str} — not eligible")
        return False
    if vol_spike < _min_vol:
        print(f"[Futures/Overflow] {symbol}: vol_spike={vol_spike:.2f}x < min={_min_vol}x — not eligible")
        return False
    if chg24 > _min_drop:
        print(f"[Futures/Overflow] {symbol}: chg24={chg24:.2f}% > {_min_drop}% drop threshold — not eligible")
        return False
    if btc_4h_chg >= 0:
        print(f"[Futures/Overflow] {symbol}: BTC 4h={btc_4h_chg:+.2f}% — no overflow on BTC rebound")
        return False
    # Already have this symbol in overflow
    if symbol in _futures_overflow_positions:
        print(f"[Futures/Overflow] {symbol}: already in overflow set")
        return False
    print(f"[Futures/Overflow] {symbol}: ELIGIBLE — str={strength:.2f}, vol={vol_spike:.1f}x, chg24={chg24:.1f}%, btc4h={btc_4h_chg:+.2f}%")
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
            _min_hold = getattr(settings, 'min_hold_minutes_spot_bull', 120)
        else:
            _min_hold = getattr(settings, 'min_hold_minutes', 120)
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
                _current_price = client.get_symbol_price(_worst_sym)
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
            _valid, _reason = is_quantity_valid(_qty, client.get_symbol_price(_worst_sym), _filters)
            if not _valid:
                print(f"[Replace] {_worst_sym}: invalid qty — {_reason}")
                return False
            _order = client.create_market_order(symbol=_worst_sym, side="SELL", quantity=_qty)
            _exit_price = client.get_symbol_price(_worst_sym)
            commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
            position_manager.close_all_positions_for_symbol(_worst_sym, _exit_price, "active_replacement", commission_rate)
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


def _filter_by_quality(symbols: List[str], client, settings, target_count: int = 100) -> List[str]:
    """
    Apply quality filters to screener symbols for top 100 selection.
    Filters by: volatility, price minimum.
    """
    filtered = []
    max_volatility = getattr(settings, 'screener_quality_max_volatility', 0.50)
    min_price = getattr(settings, 'screener_quality_min_price_usdt', 0.0001)
    
    for symbol in symbols:
        try:
            # Get 24h ticker for volatility check
            ticker = client.get_client().get_ticker(symbol=symbol)
            price_change_pct = abs(float(ticker.get('priceChangePercent', 0))) / 100
            last_price = float(ticker.get('lastPrice', 0))
            
            # Filter by volatility
            if price_change_pct > max_volatility:
                continue
            
            # Filter by minimum price
            if last_price < min_price:
                continue
            
            filtered.append(symbol)
            
            if len(filtered) >= target_count:
                break
                
        except Exception:
            # If we can't get data, include the symbol (will be filtered later)
            filtered.append(symbol)
            if len(filtered) >= target_count:
                break
    
    return filtered


def _get_top_buy_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager=None, regime: str = "SPOT_BULL") -> List[Dict]:
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

    # ── Mejora D: TTL filter — discard external signals older than 15 min ─────────
    fresh_external = []
    for sig in external_signals:
        received_raw = sig.get('received_at')
        if received_raw:
            try:
                received_dt = datetime.fromisoformat(received_raw.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - received_dt).total_seconds() / 60
                if age_min > 15:
                    print(f"Discarding stale external signal for {sig.get('symbol')} (age={age_min:.1f}min > 15min)")
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
            # Configure symbols for analysis — merge static list with dynamic screener
            _static_symbols = getattr(settings, 'analysis_symbols', [
                'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT'
            ])
            
            # Check if top-n screener v2 is enabled (100 quality assets)
            if getattr(settings, 'screener_top_n_enabled', False):
                try:
                    from pair_screener import get_screened_pairs
                    _screened = get_screened_pairs(
                        spot_min_vol=settings.screener_quality_min_vol_usdt,
                        max_spot=settings.screener_top_n_count + 20,  # Request extra for filtering
                        settings=settings
                    )
                    # Apply additional quality filters
                    symbols = _filter_by_quality(
                        _screened["spot"],
                        client,
                        settings,
                        target_count=settings.screener_top_n_count
                    )
                    print(f"[Screener v2] Selected top {len(symbols)} quality assets (target: {settings.screener_top_n_count})")
                    _new = [s for s in symbols if s not in _static_symbols]
                    if _new:
                        print(f"[Screener v2] New vs static: {_new[:15]}{'...' if len(_new) > 15 else ''}")
                except Exception as _se:
                    print(f"[Screener v2] Error, falling back to screener v1: {_se}")
                    # Fallback to screener v1
                    if getattr(settings, 'screener_enabled', True):
                        from pair_screener import get_screened_pairs, merge_with_static
                        _screened = get_screened_pairs(settings=settings)
                        symbols = merge_with_static(
                            _screened["spot"],
                            _static_symbols,
                            max_total=getattr(settings, 'screener_max_pairs', 50),
                        )
                    else:
                        symbols = _static_symbols
            elif getattr(settings, 'screener_enabled', True):
                try:
                    from pair_screener import get_screened_pairs, merge_with_static
                    _screened = get_screened_pairs(settings=settings)
                    symbols = merge_with_static(
                        _screened["spot"],
                        _static_symbols,
                        max_total=getattr(settings, 'screener_max_pairs', 50),
                    )
                    _new = [s for s in symbols if s not in _static_symbols]
                    if _new:
                        print(f"[Screener] Added {len(_new)} dynamic pairs: {_new[:10]}")
                except Exception as _se:
                    print(f"[Screener] Error, falling back to static list: {_se}")
                    symbols = _static_symbols
            else:
                symbols = _static_symbols

            signal_generator = AutonomousSignalGenerator(client, symbols)  # reuse shared client
            _sel_max = _get_max_positions_for_regime(settings, regime or 'SPOT_BULL')
            autonomous_signals = signal_generator.get_top_buy_signals(
                interval='1h',
                min_strength=getattr(settings, 'min_signal_strength', 0.5),
                limit=_sel_max + 5,  # +5 buffer: candidates for replacement/overflow evaluation
                news_client=news_client,
                regime_mode=regime,
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
        _max_trade = float(getattr(settings, 'max_per_trade_usdt', 150.0))
        position_size = min(position_size_usdt, _max_trade) if position_size_usdt > 0 else _max_trade
        prediction, _ = pnl_service.predict_for_symbol(
            symbol=symbol, client=client, settings=settings,
            position_size_usdt=position_size
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
        _mult  = getattr(settings, 'atr_sl_multiplier', 1.5)
        _max   = getattr(settings, 'atr_sl_max_pct', 0.08)
        _min   = getattr(settings, 'atr_sl_min_pct', 0.01)
        _atr_sl = max(_min, min(_max, _atr_pct * _mult))
        _final  = max(category_sl_pct, _atr_sl)
        return _final
    except Exception:
        return category_sl_pct


def _check_sl_tp_positions(client: BinanceClient, settings, position_manager, news_client=None, regime: str = None) -> List[Dict]:
    """
    Check all open positions against Stop-Loss, Take-Profit, and Trailing Stop rules.
    Also performs prediction-based exit when EV deteriorates (C1).
    Sells any position that has breached its exit threshold.
    Returns list of closed position dicts.
    """
    closed = []
    if not position_manager:
        return closed

    _global_sl_pct = getattr(settings, 'stop_loss_pct', 1.8)
    _global_tp_pct = getattr(settings, 'take_profit_pct', 3.5)
    trailing_enabled = getattr(settings, 'trailing_stop_enabled', True)
    _global_trail_act = getattr(settings, 'trailing_stop_activation_pct', 2.0)
    _global_trail_dist = getattr(settings, 'trailing_stop_distance_pct', 1.0)
    commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
    if getattr(settings, 'use_bnb_for_commission', False):
        commission_rate = 0.0005
    _token_categories = getattr(settings, 'token_categories', {})
    _cat_thresholds = getattr(settings, 'category_thresholds', {})

    try:
        all_positions = position_manager.get_all_positions()
    except Exception as e:
        print(f"SL/TP check: cannot get positions: {e}")
        return closed

    for symbol, pos_list in all_positions.items():
        # ── Per-category SL/TP/trailing thresholds ────────────────────────
        _cat_beh = _token_categories.get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
        _cat_th = _cat_thresholds.get(_cat_beh, {})
        sl_pct = _cat_th.get('stop_loss_pct', _global_sl_pct) / 100.0
        tp_pct = _cat_th.get('take_profit_pct', _global_tp_pct) / 100.0
        trail_activate = _cat_th.get('trailing_activation_pct', _global_trail_act) / 100.0
        trail_dist = _cat_th.get('trailing_distance_pct', _global_trail_dist) / 100.0
        # M1: override sl_pct with ATR-based dynamic SL (prevents premature stops on volatile tokens)
        sl_pct = _calc_atr_sl_pct(client, symbol, settings, sl_pct)

        for pos in pos_list:
            try:
                current_price = client.get_symbol_price(symbol)
                entry_price = float(pos.entry_price)
                if entry_price <= 0:
                    continue

                pnl_pct = (current_price - entry_price) / entry_price
                close_reason = None

                # ── C1: Re-evaluación predictiva — predicción tiene prioridad absoluta ──
                if getattr(settings, 'prediction_exit_enabled', True) and pnl_pct < 0:
                    try:
                        _min_hold_pred = int(getattr(settings, 'min_hold_for_prediction_exit', 90))
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
                                symbol, client, settings, _pos_size
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
                                # R2: FLAT allowed when EV is extreme (3x threshold) — catches stagnant bear positions
                                # e.g. NARRATIVE min_pnl=-1.0 → FLAT triggers only if EV < -3.0
                                _flat_extreme = (_trend_exit == 'FLAT'
                                                 and _min_pnl_cat < 0
                                                 and _ev < _min_pnl_cat * 3.0)
                                _is_deteriorated = (_ev < _min_pnl_cat and _prob < _min_prob_cat
                                                    and (_trend_exit == 'DOWN' or _flat_extreme))
                                _meta_upd = getattr(pos, 'metadata', {}) or {}
                                _meta_upd['last_pred_pnl'] = round(_ev, 4)
                                _meta_upd['last_pred_prob'] = round(_prob, 4)
                                _meta_upd['last_pred_time'] = datetime.now(timezone.utc).isoformat()
                                _meta_upd['reevaluation_count'] = int(_meta_upd.get('reevaluation_count', 0)) + 1
                                if _is_deteriorated:
                                    _meta_upd['prediction_exit_triggered'] = True
                                try:
                                    position_manager.update_position_metadata(pos.position_id, _meta_upd)
                                except Exception:
                                    pass
                                if _is_deteriorated:
                                    print(f"[PredExit] {symbol}: EV={_ev:.2f} < min={_min_pnl_cat:.2f}, prob={_prob:.2f} < {_min_prob_cat:.2f}, trend={_trend_exit}, pnl={pnl_pct*100:.2f}% → CLOSING")
                                    ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                                 f"[PredExit] {symbol} prediction deteriorated: EV={_ev:.2f} trend={_trend_exit} pnl={pnl_pct*100:.2f}%")
                                    close_reason = f"prediction_exit_deteriorated (EV={_ev:.2f}, prob={_prob:.2f}, trend={_trend_exit})"
                                else:
                                    print(f"[PredEval] {symbol}: EV={_ev:.2f} prob={_prob:.2f} trend={_trend_exit} pnl={pnl_pct*100:.2f}% → holding")
                    except Exception as _pe:
                        print(f"[PredEval] {symbol}: prediction check failed: {_pe}")

                # ── Stop-Loss ──────────────────────────────────────────────
                # Fix 1: use effective SL = max(category floor, saved pos.stop_loss)
                # Prevents SL from retreating when SmartTP EXTEND or Scale-Out moved it forward
                _sl_floor = entry_price * (1.0 - sl_pct)
                _sl_saved = float(pos.stop_loss) if pos.stop_loss and float(pos.stop_loss) > 0 else _sl_floor
                _effective_sl_price = max(_sl_floor, _sl_saved)
                if close_reason is None and current_price <= _effective_sl_price:
                    close_reason = f"stop_loss ({pnl_pct*100:.2f}%, sl={_effective_sl_price:.6f})"

                # ── Take-Profit ────────────────────────────────────────────
                if close_reason is None and pnl_pct >= tp_pct:
                    close_reason = f"take_profit ({pnl_pct*100:.2f}%)"

                # ── Trailing Stop ──────────────────────────────────────────
                if close_reason is None and trailing_enabled and pnl_pct >= trail_activate:
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
                    if current_price <= trail_stop_price:
                        close_reason = f"trailing_stop (peak={peak_price:.4f}, trail={trail_stop_price:.4f})"

                if close_reason:
                    # ── Anti-wick: confirm SL/TS with last closed candle ───────
                    _is_sl_trigger = close_reason.startswith("stop_loss")
                    _is_ts_trigger = close_reason.startswith("trailing_stop")
                    if _is_sl_trigger or _is_ts_trigger:
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
                        # Regime-aware SmartTP: disable EXTEND in flat/defensive regimes
                        _disabled_regimes = getattr(settings, 'smart_tp_extend_disabled_regimes', ['RANGE_SCALP', 'PRESERVATION'])
                        if regime and regime in _disabled_regimes:
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
                                _new_sl = round(current_price * (1.0 - 0.005), 8)  # 0.5% below current price to avoid immediate trigger
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
                                        remaining_qty = align_quantity_to_step(full_qty - sell_qty, filters["step"])
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
                                                       "price": current_price, "order": order})
                                        continue
                        qty = align_quantity_to_step(full_qty, filters["step"])
                        # ── Clamp qty to actual Binance balance to prevent -2010 ──────────
                        try:
                            _asset = symbol.replace('USDT', '')
                            _bal = client.get_account_balance()
                            _avail = float(_bal.get(_asset, 0))
                            if _avail < qty:
                                _clamped = align_quantity_to_step(_avail, filters["step"])
                                print(f"[SL/TP] {symbol}: clamping qty {qty} → {_clamped} (Binance balance={_avail})")
                                qty = _clamped
                        except Exception as _be:
                            print(f"[SL/TP] balance clamp failed for {symbol}: {_be}")
                        valid, reason = is_quantity_valid(qty, current_price, filters)
                        if not valid:
                            print(f"SL/TP: cannot sell {symbol}: {reason}")
                            continue
                        order = client.create_market_order(symbol=symbol, side="SELL", quantity=qty)
                        print(f"SL/TP EXIT {symbol}: {close_reason} @ {current_price:.4f}")
                        ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_INFO,
                                     f"SL/TP exit {symbol}: {close_reason} @ {current_price:.4f}")
                        position_manager.close_all_positions_for_symbol(
                            symbol, current_price, close_reason.split(" ")[0], commission_rate
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
                                       "price": current_price, "order": order})
                    except Exception as exc:
                        print(f"SL/TP sell failed for {symbol}: {exc}")
                        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_ERROR,
                                     f"SL/TP sell failed {symbol}: {exc}")
            except Exception as e:
                print(f"SL/TP check error for {symbol}: {e}")
    return closed


def _sell_holdings_not_in_top(client: BinanceClient, settings, balances: Dict[str, float], top_symbols: List[str], portfolio_manager=None, position_manager=None) -> List[Dict]:
    """Clean up orphan assets — assets held in Binance balance with NO open position record in
    the PositionManager (leftovers from before the system existed or from external trades).
    Registered positions are NEVER touched here — they are managed exclusively by SL/TP checker
    and the slot-pressure reallocation (Step 3).
    """
    orders = []
    _cleanup_count = 0
    _max_cleanup = getattr(settings, 'max_rebalance_per_cycle', 2)

    for asset, amount in balances.items():
        if asset.upper() == "USDT" or amount <= 0:
            continue
        if _cleanup_count >= _max_cleanup:
            print(f"[Cleanup] max_cleanup_per_cycle={_max_cleanup} reached — deferring remaining orphans")
            break

        symbol = f"{asset.upper()}USDT"
        if symbol in _INVALID_SYMBOLS:
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
            price = client.get_symbol_price(symbol)
        except Exception as exc:
            if "-1121" in str(exc):
                _INVALID_SYMBOLS.add(symbol)
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
                         tp_pct: float = 0.025) -> float:
    """
    Half-Kelly position sizing scaled by signal quality.

    Tiers (prob / strength thresholds):
      FUERTE  prob >= 0.65 + strength >= 0.80  → kelly_max_usdt  ($150)
      ALTA    prob >= 0.60 + strength >= 0.70  → 80% of max       ($120)
      MEDIA   prob >= 0.55 + strength >= 0.55  → 60% of max       ($90)
      BASE    prob >= 0.50  (passed validation) → kelly_base_usdt  ($60)

    Fallback (kelly disabled): max_per_trade_usdt.
    Hard cap: kelly_max_pct_liquid of available USDT.
    """
    if not getattr(settings, "kelly_enabled", True):
        return getattr(settings, "max_per_trade_usdt", 50.0)

    fixed_base = getattr(settings, "kelly_base_usdt", 60.0)
    dyn_pct = getattr(settings, "kelly_dynamic_base_pct", 0.15)
    base = max(fixed_base, usdt_liquid * dyn_pct)
    # Smart Kelly: max scales with liquid capital instead of fixed cap
    max_usdt_floor = getattr(settings, "kelly_max_usdt", 350.0)
    max_usdt_pct_cap = getattr(settings, "kelly_max_pct_cap", 0.25)
    max_usdt = max(max_usdt_floor, usdt_liquid * max_usdt_pct_cap)
    cap_pct = getattr(settings, "kelly_max_pct_liquid", 0.40)

    prob = float((prediction or {}).get("success_probability", 0.50))
    r = tp_pct / sl_pct if sl_pct > 0 else 1.67

    # Half-Kelly fraction
    kelly_f = max(0.0, (prob - (1 - prob) / r)) * 0.5

    # Map to tier — FUERTE/ALTA/MEDIA use tier amount directly (kelly_f already near max).
    # BASE tier uses kelly_f scaling to soften weak signals.
    if prob >= 0.65 and strength >= 0.80:
        tier, amount = "FUERTE", max_usdt
        sized = amount  # at max conviction, use full tier
    elif prob >= 0.60 and strength >= 0.70:
        tier, amount = "ALTA", base + (max_usdt - base) * 0.80
        sized = amount  # high conviction, no kelly_f discount
    elif prob >= 0.55 and strength >= 0.55:
        tier, amount = "MEDIA", base + (max_usdt - base) * 0.50
        sized = amount  # medium conviction, use full tier
    else:
        tier, amount = "BASE", base
        # BASE tier: scale by kelly_f so weak signals get smaller sizes
        scaled = amount * min(1.0, kelly_f * 10)
        sized = max(base * 0.5, scaled)  # floor at 50% of base, not full base

    # Hard cap: fraction of liquid USDT
    liquid_cap = usdt_liquid * cap_pct
    final = min(sized, liquid_cap, max_usdt)

    print(f"Kelly sizing: prob={prob:.2f}, strength={strength:.2f}, kelly_f={kelly_f:.3f} "
          f"→ tier={tier}, sized=${sized:.2f}, cap=${liquid_cap:.2f} → final=${final:.2f} USDT")
    return final


def _buy_top_symbols(client: BinanceClient, settings, top_signals_with_meta: List, portfolio_manager=None, position_manager=None, precomputed_predictions: Dict = None, news_client=None, regime_obj: "MarketRegime" = None, regime_mode_override: str = None, screened_pairs_cache: dict = None) -> List[Dict]:
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

    balances = client.get_account_balance()
    usdt = float(balances.get("USDT", 0.0))
    usdt_liquid = usdt  # default; overridden below if portfolio_manager available
    
    # Use Portfolio Manager for allocation if available
    if portfolio_manager:
        balance = portfolio_manager.get_total_balance()
        # Use liquid USDT as the actual cap — not total portfolio value
        usdt_liquid = float(balance.get("usdt_available", usdt))
        _total_portfolio = float(balance.get("total_usdt", usdt_liquid))
        _invested = _total_portfolio - usdt_liquid
        _exposure_pct = _invested / _total_portfolio if _total_portfolio > 0 else 0.0
        _max_exposure = getattr(settings, 'max_total_exposure_pct', 0.65)
        # Aggressive mode: SPOT_BULL + F&G Fear → higher exposure cap to deploy more capital
        _fng_early = 50
        try:
            if news_client:
                _fng_early = int(news_client.get_fear_greed().get('value', 50))
        except Exception:
            pass
        _fng_fear_thr = getattr(settings, 'max_total_exposure_spot_bull_fear_fng_threshold', 40)
        _spot_bull_fear_cap = getattr(settings, 'max_total_exposure_spot_bull_fear', 0.80)
        if _regime_mode == "SPOT_BULL" and _fng_early <= _fng_fear_thr:
            _max_exposure = _spot_bull_fear_cap
            print(f"[Exposure] SPOT_BULL + F&G={_fng_early} (Fear) → aggressive cap {_max_exposure:.0%}")
        print(f"USDT liquid: {usdt_liquid:.2f}, total: {_total_portfolio:.2f}, invested: {_invested:.2f} ({_exposure_pct:.1%}) — cap: {_max_exposure:.0%}")
        if _exposure_pct >= _max_exposure:
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
            import urllib.request as _ur, json as _json
            with _ur.urlopen('https://api.coingecko.com/api/v3/global', timeout=3) as _r:
                _gdata = _json.loads(_r.read())
            _btc_dom = _gdata.get('data', {}).get('market_cap_percentage', {}).get('btc', 0)
            _dom_thresh = getattr(settings, 'btc_dominance_altcoin_threshold', 56.0)
            if _btc_dom > _dom_thresh:
                _btc_dom_high = True
                print(f"BTC Dominance {_btc_dom:.1f}% > {_dom_thresh}% — altcoin entries restricted to NARRATIVE only")
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
    for symbol in top_symbols:
        # ── FUTURES_SHORT mode — evaluated BEFORE spot position limits ────────
        # Futures positions are in a separate wallet — they must NOT be blocked
        # by the spot max_positions counter (which fills up with open spot trades).
        if _regime and _regime.confirmed and _regime_mode == "FUTURES_SHORT":
            if getattr(settings, 'futures_enabled', False):
                _futures_symbols = list(getattr(settings, 'futures_symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']))
                if screened_pairs_cache:
                    _futures_symbols = list(dict.fromkeys(_futures_symbols + screened_pairs_cache.get("futures", [])))
                elif getattr(settings, 'screener_enabled', True):
                    try:
                        from pair_screener import get_screened_pairs
                        _futures_symbols = list(dict.fromkeys(_futures_symbols + get_screened_pairs(settings=settings).get("futures", [])))
                    except Exception:
                        pass
                if symbol not in _futures_symbols:
                    print(f"[Futures] {symbol} not in futures_symbols — skipping short, no spot entry in FUTURES_SHORT mode")
                    update_signal_decision(symbol, "rejected", "NOT_FUTURES_SYM")
                    continue
                # FIX-F1: Skip if symbol is in SL cooldown
                _sl_cd_hours = getattr(settings, 'futures_sl_cooldown_hours', 1.0)
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
                _fc = _get_futures_client(settings)
                _max_shorts = getattr(settings, 'futures_max_shorts', 2)
                _open_shorts = len([p for p in _fc._open_positions.values() if not p.closed and p.side == "SHORT"])
                # LARGECAP guard: limit large-cap SHORTs to force MID diversification (slots 6-7)
                _FUTURES_LARGE_CAPS = set(getattr(settings, 'futures_largecap_symbols',
                                                  ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT']))
                _max_largecap = getattr(settings, 'futures_max_largecap_shorts', 5)
                if symbol in _FUTURES_LARGE_CAPS:
                    _open_largecap = sum(1 for p in _fc._open_positions.values()
                                         if not p.closed and p.side == 'SHORT' and p.symbol in _FUTURES_LARGE_CAPS)
                    if _open_largecap >= _max_largecap:
                        print(f"[Futures] {symbol} SKIPPED: large-cap slots {_open_largecap}/{_max_largecap} full — prioritizing MIDs")
                        update_signal_decision(symbol, "rejected", "LARGECAP_CAP")
                        continue
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
                              f"{getattr(settings,'futures_overflow_max_extra',3)})")
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
                        _fc.close_position(symbol, side="LONG", reason="regime_change_to_short")
                    except Exception as _cl_err:
                        print(f"[Futures] Could not close residual LONG {symbol}: {_cl_err}")

                _f_sl    = getattr(settings, 'futures_sl_pct', 3.0)
                _f_tp    = getattr(settings, 'futures_tp_pct', 7.0)
                _f_lev   = getattr(settings, 'futures_leverage', 2)
                # F4: Use real signal probability instead of hardcoded 0.45
                _f_sig_meta = signal_meta.get(symbol, {})
                _f_real_prob = float(_f_sig_meta.get('success_probability', float(_f_sig_meta.get('strength', 0.45))))
                _f_real_prob = max(0.45, min(0.75, _f_real_prob))
                _f_pred  = {'success_probability': _f_real_prob, 'expected_pnl': 1.0}
                _f_usdt  = _kelly_position_size(
                    settings, strength=float(_f_sig_meta.get('strength', 0.5)),
                    prediction=_f_pred, usdt_liquid=usdt_liquid,
                    sl_pct=_f_sl * _f_lev / 100, tp_pct=_f_tp * _f_lev / 100)
                # F4: Dynamic cap — max(per_trade_floor, futures_margin × kelly_cap_pct)
                _futures_kelly_cap = getattr(settings, 'futures_kelly_cap_pct', 0.15)
                _futures_margin_avail = _fc.get_available_usdt() if not _fc.dry_run else usdt_liquid * 0.3
                _f_usdt_cap = max(
                    getattr(settings, 'futures_usdt_per_trade', 20.0),
                    _futures_margin_avail * _futures_kelly_cap
                )
                _f_usdt = min(_f_usdt, _f_usdt_cap)
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
                    _btc_1h_klines = client.get_klines('BTCUSDT', '1h', limit=2)
                    _btc_1h_chg = (float(_btc_1h_klines[-1][4]) - float(_btc_1h_klines[-1][1])) / float(_btc_1h_klines[-1][1]) * 100 if _btc_1h_klines else 0.0
                    if _btc_1h_chg >= _no_short_btc_1h_threshold:
                        print(f'[Futures] SHORT {symbol} SKIPPED: BTC 1h bounce={_btc_1h_chg:+.2f}% >= {_no_short_btc_1h_threshold}% — no new SHORTs in rally')
                        update_signal_decision(symbol, 'rejected', 'BTC_1H_BOUNCE')
                        continue
                except Exception:
                    pass
                print(f"[Futures] FUTURES_SHORT mode: opening short {symbol} margin=${_f_usdt}")
                _pos = _fc.open_short(symbol, usdt_amount=_f_usdt,
                                      sl_pct=_f_sl, tp_pct=_f_tp, leverage=_f_lev)
                if _pos:
                    orders.append({"symbol": symbol, "side": "SHORT_FUTURES",
                                    "qty": _pos.qty, "entry_price": _pos.entry_price,
                                    "sl": _pos.sl_price, "tp": _pos.tp_price})
                    update_signal_decision(symbol, "executed", "FUTURES_SHORT")
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
                                                                    getattr(settings, 'max_per_trade_usdt', 50.0))

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
                              f"Overflow slots used: {len(_overflow_positions)}/{getattr(settings,'overflow_max_extra',3)}")
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
            max_per = getattr(settings, 'max_positions_per_symbol', 3)
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
                            _new_pred_c2, _ = pnl_service.predict_for_symbol(symbol, client, settings, allocation)
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
            _wr_n = int(getattr(settings, 'wr_screener_consecutive_sl_block', 3))
            if _wr_n > 0 and _consecutive_sl_count(symbol, position_manager, _wr_n):
                print(f"[WRScreener] Skipping {symbol}: {_wr_n} consecutive SL — pair on losing streak")
                update_signal_decision(symbol, "rejected", f"WR_BLOCK_{_wr_n}SL")
                continue

        try:
            price = client.get_symbol_price(symbol)
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
                _rs_max_rsi = getattr(settings, 'range_scalp_max_rsi', 40.0)
                _meta_rsi   = float(signal_meta.get(symbol, {}).get('rsi', 50))
                if _meta_rsi > _rs_max_rsi:
                    print(f"Skipping buy {symbol}: RANGE_SCALP mode RSI={_meta_rsi:.1f} > {_rs_max_rsi} (not at BB lower)")
                    update_signal_decision(symbol, "rejected", "RANGE_RSI")
                    continue
                # B1: Check real BB lower band touch (not just RSI proxy)
                if getattr(settings, 'range_scalp_require_bb_lower', True):
                    _meta_bb_touch = signal_meta.get(symbol, {}).get('bb_lower_touch', None)
                    if _meta_bb_touch is False:  # explicitly False (not None = unknown)
                        print(f"Skipping buy {symbol}: RANGE_SCALP — price not at BB lower band")
                        update_signal_decision(symbol, "rejected", "RANGE_BB")
                        continue
                # LONG futuros: si precio en BB inferior + futures habilitado, abrir LONG en futuros
                if (getattr(settings, 'futures_enabled', False)
                        and getattr(settings, 'futures_long_enabled', True)
                        and getattr(settings, 'futures_long_in_range_scalp', True)
                        and signal_meta.get(symbol, {}).get('bb_lower_touch', False)):
                    _f_sym_list = list(getattr(settings, 'futures_symbols', []))
                    if screened_pairs_cache:
                        _f_sym_list = list(dict.fromkeys(_f_sym_list + screened_pairs_cache.get("futures", [])))
                    elif getattr(settings, 'screener_enabled', True):
                        try:
                            from pair_screener import get_screened_pairs
                            _f_sym_list = list(dict.fromkeys(_f_sym_list + get_screened_pairs(settings=settings).get("futures", [])))
                        except Exception:
                            pass
                    if symbol in _f_sym_list:
                        _fc_l = _get_futures_client(settings)
                        _l_usdt = getattr(settings, 'futures_usdt_per_trade', 20.0)
                        _l_sl   = getattr(settings, 'futures_long_sl_pct', 2.5)
                        _l_tp   = getattr(settings, 'futures_long_tp_pct', 5.0)
                        _l_lev  = getattr(settings, 'futures_leverage', 2)
                        print(f"[Futures] RANGE_SCALP BB-lower: opening LONG {symbol} margin=${_l_usdt}")
                        _lpos = _fc_l.open_long(symbol, usdt_amount=_l_usdt,
                                                sl_pct=_l_sl, tp_pct=_l_tp, leverage=_l_lev)
                        if _lpos:
                            orders.append({"symbol": symbol, "side": "LONG_FUTURES",
                                           "qty": _lpos.qty, "entry_price": _lpos.entry_price,
                                           "sl": _lpos.sl_price, "tp": _lpos.tp_price})
                            update_signal_decision(symbol, "executed", "FUTURES_LONG")

        # ── SPOT_BULL + RSI oversold: LONG futuros más eficiente que spot ──
        if (_regime_mode == "SPOT_BULL" and _regime is not None and _regime.confirmed
                and getattr(settings, 'futures_enabled', False)
                and getattr(settings, 'futures_long_enabled', True)
                and getattr(settings, 'futures_long_in_spot_bull', True)):
            _l_rsi_thresh = getattr(settings, 'futures_long_rsi_threshold', 35.0)
            _l_rsi_val    = float(signal_meta.get(symbol, {}).get('rsi', 50))
            if _l_rsi_val < _l_rsi_thresh and btc_trend == 'UP':
                _l_sym_list = list(getattr(settings, 'futures_symbols', []))
                if screened_pairs_cache:
                    _l_sym_list = list(dict.fromkeys(_l_sym_list + screened_pairs_cache.get("futures", [])))
                elif getattr(settings, 'screener_enabled', True):
                    try:
                        from pair_screener import get_screened_pairs
                        _l_sym_list = list(dict.fromkeys(_l_sym_list + get_screened_pairs(settings=settings).get("futures", [])))
                    except Exception:
                        pass
                if symbol in _l_sym_list:
                    _fc_sb = _get_futures_client(settings)
                    _l_sl   = getattr(settings, 'futures_long_sl_pct', 2.5)
                    _l_tp   = getattr(settings, 'futures_long_tp_pct', 5.0)
                    _l_lev  = getattr(settings, 'futures_leverage', 2)
                    # MEJO-D: Kelly sizing para LONG (igual que SHORT), cap futures_usdt_per_trade
                    _l_pred_sb = {'success_probability': 0.55}  # oversold en uptrend = mayor prob
                    _l_usdt = _kelly_position_size(
                        settings, strength=float(signal_meta.get(symbol, {}).get('strength', 0.5)),
                        prediction=_l_pred_sb, usdt_liquid=usdt_liquid,
                        sl_pct=_l_sl * _l_lev / 100, tp_pct=_l_tp * _l_lev / 100)
                    _l_usdt = min(_l_usdt, getattr(settings, 'futures_usdt_per_trade', 20.0))
                    # Apply F&G boost (oversold + Fear = strong conviction entry)
                    _l_usdt = _apply_fng_kelly_boost(_l_usdt, _fng_value, settings)
                    _l_usdt = min(_l_usdt, getattr(settings, 'futures_usdt_per_trade', 20.0))
                    print(f"[Futures] SPOT_BULL oversold LONG {symbol}: RSI={_l_rsi_val:.1f}<{_l_rsi_thresh}, BTC={btc_trend}, margin=${_l_usdt:.2f}")
                    _lpos_sb = _fc_sb.open_long(symbol, usdt_amount=_l_usdt,
                                                sl_pct=_l_sl, tp_pct=_l_tp, leverage=_l_lev)
                    if _lpos_sb:
                        orders.append({"symbol": symbol, "side": "LONG_FUTURES",
                                       "qty": _lpos_sb.qty, "entry_price": _lpos_sb.entry_price,
                                       "sl": _lpos_sb.sl_price, "tp": _lpos_sb.tp_price})
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
            if not _last_buy:
                _last_buy = _check_cooldown_db(symbol, 'narrative') and _NARRATIVE_BUY_COOLDOWN.get(symbol)
            if _last_buy:
                _h_since = (_dt2.now(timezone.utc) - _last_buy).total_seconds() / 3600
                if _h_since < _NARRATIVE_BUY_COOLDOWN_HOURS:
                    print(f"Skipping buy {symbol}: NARRATIVE cooldown active ({_h_since:.1f}h < {_NARRATIVE_BUY_COOLDOWN_HOURS}h)")
                    update_signal_decision(symbol, "rejected", "NARRATIVE_CD")
                    continue
            elif _check_cooldown_db(symbol, 'narrative'):
                print(f"Skipping buy {symbol}: NARRATIVE cooldown active (from DB — post-restart)")
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
                symbol, client, settings, allocation
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
            _cat_th = getattr(settings, 'category_thresholds', {})
            _sym_cat = getattr(settings, 'token_categories', {}).get(symbol, {}).get('market_behavior', 'MIDCAP_ALT')
            _cur_min_pnl = _cat_th.get(_sym_cat, {}).get('min_pnl', 0.0)
            if _boost_pnl < _cur_min_pnl:
                import copy
                _pnl_boosted_settings = copy.copy(settings)
                _new_th = copy.deepcopy(_cat_th)
                if _sym_cat not in _new_th:
                    _new_th[_sym_cat] = {}
                _new_th[_sym_cat]['min_pnl'] = _boost_pnl
                _pnl_boosted_settings.category_thresholds = _new_th
                print(f"[SPOT_BULL boost] {symbol} ({_sym_cat}): min_pnl {_cur_min_pnl:.2f}→{_boost_pnl:.2f} (BTC 4h={btc_4h_chg_pct:+.2f}%)")
        is_valid, reason = pnl_service.validate(prediction, _pnl_boosted_settings, symbol=symbol, news_client=news_client)
        if not is_valid:
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
                        _min_spike = getattr(settings, 'volspike_override_min_spike', 1.5)
                        _max_rsi   = getattr(settings, 'volspike_override_max_rsi', 65.0)
                        _need_trend = getattr(settings, 'volspike_override_trend_required', True)
                        # ── Funding Rate filter ────────────────────────────────
                        _funding_ok = True
                        try:
                            _fr_url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
                            import urllib.request as _ureq
                            with _ureq.urlopen(_fr_url, timeout=4) as _fr_resp:
                                _fr_data = __import__('json').loads(_fr_resp.read())
                            if _fr_data:
                                _fr_val = float(_fr_data[0].get('fundingRate', 0))
                                if _fr_val > 0.0005:  # > 0.05% = longs muy apalancados → peligro
                                    _funding_ok = False
                                    print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} > 0.05% — skip (overleveraged longs)")
                                else:
                                    print(f"VolSpike OVERRIDE {symbol}: funding rate={_fr_val:.4%} ✓")
                        except Exception:
                            pass  # no futures pair or API error → allow entry
                        _meta_rsi_override = float(signal_meta.get(symbol, {}).get('rsi', 50.0))
                        if _meta_rsi_override >= _max_rsi and _spike >= _min_spike:
                            print(f"VolSpike OVERRIDE {symbol}: RSI_1h={_meta_rsi_override:.1f} >= {_max_rsi} — bloqueado (sobrecomprado en 1h)")
                        if (_spike >= _min_spike and _rsi < _max_rsi
                                and _meta_rsi_override < _max_rsi
                                and (not _need_trend or _trend_up) and _funding_ok):
                            # V2: Validaciones adicionales más selectivas (jun 16 2026)
                            # Check 1: Strength mínimo requerido
                            _min_strength = getattr(settings, 'volspike_override_min_strength', 0.65)
                            if strength < _min_strength:
                                print(f"VolSpike OVERRIDE {symbol}: rechazado - strength {strength:.2f} < {_min_strength} (señal débil)")
                                continue  # Skip this symbol, don't mark as override
                            
                            # Check 2: Límite de posiciones override simultáneas
                            _max_override_pos = getattr(settings, 'volspike_override_max_positions', 2)
                            if len(_VOLSPIKE_POSITIONS) >= _max_override_pos:
                                print(f"VolSpike OVERRIDE {symbol}: rechazado - límite de {_max_override_pos} posiciones override alcanzado ({len(_VOLSPIKE_POSITIONS)} activas)")
                                continue
                            
                            # Check 3: Límite diario
                            from datetime import datetime as _dt  # BUG-1: import before first use
                            _daily_limit = getattr(settings, 'volspike_override_daily_limit', 3)
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
        if prediction and is_valid:
            print(f"Prediction OK for {symbol}: {prediction['expected_pnl']:.2f} USDT, prob={prediction['success_probability']:.2f}")

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
        sl_pct = _cat_th_e.get('stop_loss_pct', getattr(settings, 'stop_loss_pct', 1.8)) / 100.0
        tp_pct = _cat_th_e.get('take_profit_pct', getattr(settings, 'take_profit_pct', 3.5)) / 100.0
        # M1: ATR-Based SL — Kelly sizes trade against real volatility, not fixed %
        sl_pct = _calc_atr_sl_pct(client, symbol, settings, sl_pct)

        # Apply regime-specific SL/TP overrides (régimen sobreescribe categoría)
        if _regime and _regime.confirmed:
            if _regime_mode == "RANGE_SCALP":
                sl_pct = getattr(settings, 'range_scalp_sl_pct', 1.2) / 100.0
                tp_pct = getattr(settings, 'range_scalp_tp_pct', 1.5) / 100.0
                print(f"[Regime] RANGE_SCALP: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")
            elif _regime_mode == "PRESERVATION":
                sl_pct = getattr(settings, 'preservation_sl_pct', 1.8) / 100.0
                tp_pct = getattr(settings, 'preservation_tp_pct', 3.0) / 100.0
                print(f"[Regime] PRESERVATION: SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")
            else:
                print(f"[Kelly] {symbol} ({_sym_cat_e}): SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

        trade_allocation = _kelly_position_size(
            settings, strength, prediction, usdt_liquid_now, sl_pct, tp_pct
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
            _conv3x = getattr(settings, 'kelly_conviction_vol3x', 2.0)
            _conv2x = getattr(settings, 'kelly_conviction_vol2x', 1.5)
            _conv_max_pct = getattr(settings, 'kelly_conviction_max_pct', 0.10)
            # Smart Kelly: in confirmed SPOT_BULL, raise conviction cap to 15% (was 10%)
            if _regime_mode == "SPOT_BULL" and _regime and _regime.confirmed:
                _conv_max_pct = min(0.15, _conv_max_pct * 1.5)
            if _meta_vs >= 3.0:
                _conv_mult = _conv3x
            elif _meta_vs >= 2.0:
                _conv_mult = _conv2x
            else:
                _conv_mult = 1.0
            if _conv_mult > 1.0:
                _total_portfolio_val = float(client.get_account_balance().get("USDT", usdt_liquid_now)) + sum(
                    float(p.quantity) * client.get_symbol_price(s)
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
        valid, reason = is_quantity_valid(qty, price, filters)
        if not valid:
            print(f"Skipping buy {symbol}: {reason}")
            update_signal_decision(symbol, "rejected", "QTY_INVALIDA")
            continue

        # Guard: verify liquid USDT is sufficient before placing order
        cost_estimate = qty * price * 1.002  # include ~0.2% buffer for fees
        if usdt_liquid_now < cost_estimate:
            print(f"Skipping buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f} needed)")
            ec.add_error(ec.SOURCE_TRADING_CYCLE, ec.LEVEL_WARNING,
                         f"Skipped buy {symbol}: insufficient USDT ({usdt_liquid_now:.4f} < {cost_estimate:.4f})")
            update_signal_decision(symbol, "rejected", "SIN_SALDO")
            continue

        try:
            # Register NARRATIVE cooldown BEFORE placing order to prevent race condition
            if symbol in narrative_set:
                _set_cooldown(symbol, 'narrative', _NARRATIVE_BUY_COOLDOWN_HOURS)

            order = client.create_market_order(symbol=symbol, side="BUY", quantity=qty)
            _record_order_success()  # R5: reset circuit breaker on success
            orders.append({"symbol": symbol, "side": "BUY", "quantity": qty, "order": order})
            usdt_liquid_now = max(0.0, usdt_liquid_now - qty * price)  # BUG-E5: local deduction, avoids re-fetch
            print(f"Bought {qty} {symbol}")
            ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_INFO, f"Bought {qty} {symbol} @ ~{price:.4f} USDT")
            update_signal_decision(symbol, "executed", "EJECUTADO")
            mark_signal_as_processed(symbol)

            # Track position in Position Manager if available
            if position_manager:
                try:
                    from position_manager import Position
                    from datetime import datetime
                    from technical_indicators import TechnicalIndicators
                    entry_commission = compute_commission(qty, price, settings)

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

                    # Fallback to percentage-based if ATR unavailable
                    if sl_price is None:
                        _tok_cat = getattr(settings, 'token_categories', {}).get(symbol, {})
                        _cat_beh = _tok_cat.get('market_behavior', 'MIDCAP_ALT')
                        _cat_th  = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {})
                        sl_pct = _cat_th.get('stop_loss_pct',  getattr(settings, 'stop_loss_pct',  1.8)) / 100.0
                        tp_pct = _cat_th.get('take_profit_pct', getattr(settings, 'take_profit_pct', 3.5)) / 100.0
                        sl_price = price * (1 - sl_pct)
                        tp_price = price * (1 + tp_pct)
                        print(f"Category SL/TP for {symbol} ({_cat_beh}): SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%")

                    # Ensure indicators_at_entry is always captured for LearningEngine.
                    # If pnl_service.predict_for_symbol failed, fall back to
                    # formatting the signal's raw indicators directly.
                    if formatted_indicators is None:
                        try:
                            from trade_utils import format_indicators_for_predictor
                            _raw_ind = sig.get('indicators') if isinstance(sig, dict) else {}
                            if _raw_ind:
                                formatted_indicators = format_indicators_for_predictor(_raw_ind, price)
                        except Exception:
                            formatted_indicators = {}
                    _indicators_to_save = formatted_indicators or {}

                    # Prepare metadata with prediction
                    # C5: Enriquecer con entry_regime + entry_pred_pnl para re-evaluación y prediction_add
                    _is_pred_add = (position_manager.get_positions_count(symbol) >= 1) if position_manager else False
                    metadata = {
                        "order_id": order.get("orderId"),
                        "indicators_at_entry": _indicators_to_save,
                        "rsi_at_entry": float(signal_meta.get(symbol, {}).get('rsi', 50.0)),
                        "vol_spike_at_entry": float(signal_meta.get(symbol, {}).get('vol_spike', 0.0)),
                        "override_used": volspike_override,
                        "strength": float(signal_meta.get(symbol, {}).get('strength', strength)),
                        "entry_regime": _regime_mode,
                        "entry_pred_pnl": float(prediction.get('expected_pnl', 0)) if prediction else None,
                        "entry_pred_prob": float(prediction.get('success_probability', 0)) if prediction else None,
                        "entry_type": "prediction_add" if _is_pred_add else "normal",
                        "reevaluation_count": 0,
                    }
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
            update_signal_decision(symbol, "rejected", "ERROR_ORDEN")
    return orders


def _select_symbols(settings, news_client: NewsClient, client: BinanceClient, portfolio_manager, regime: str = "SPOT_BULL") -> List[Dict]:
    """Step 1: Resolve top buy symbols (as signal dicts) from signals or autonomous generation."""
    top_signals = _get_top_buy_symbols(settings, news_client, client, portfolio_manager, regime=regime)
    symbols = [s["symbol"] for s in top_signals]
    print("Top buy symbols:", symbols)
    return top_signals


def _build_predictions(top_symbols: List[str], client: BinanceClient, settings, portfolio_manager, signal_meta: Dict = None) -> Dict:
    """Step 2: Generate PnL predictions once for all top symbols (reused in buy step).
    BUG-E4: accepts signal_meta to reuse pre-computed indicators from Step 1,
    avoiding redundant analyze_symbol() calls inside pnl_service."""
    predictions = {}
    if not portfolio_manager or not top_symbols:
        return predictions
    try:
        usdt_liquid = float(portfolio_manager.get_total_balance().get("usdt_available", 0))
        alloc_per = min(settings.max_per_trade_usdt, usdt_liquid / len(top_symbols))
        for symbol in top_symbols:
            _pre_sig = (signal_meta or {}).get(symbol)
            pred, _ = pnl_service.predict_for_symbol(symbol, client, settings, alloc_per, precomputed_signal=_pre_sig)
            if pred:
                predictions[symbol] = pred
                print(f"Prediction for {symbol}: {pred['expected_pnl']:.2f} USDT")
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
        usdt_liquid = float(portfolio_manager.get_total_balance().get("usdt_available", 0))
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
            _min_hold_min = getattr(settings, 'min_hold_minutes_spot_bull', 120)
        else:
            _min_hold_min = getattr(settings, 'min_hold_minutes', 120)
        _protect = getattr(settings, 'protect_profitable_positions', True)
        _protect_pct = getattr(settings, 'protect_profit_min_pct', 0.3) / 100.0
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
                current_price = client.get_symbol_price(sym)
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
            if _protect and pnl_pct >= _protect_pct:
                print(f"[SlotPressure] {sym}: profitable {pnl_pct*100:.2f}% >= protect {_protect_pct*100:.1f}% — skipping")
                continue

            # Fix D: defer to SL checker if near SL threshold
            if pnl_pct < 0:
                _cat_beh = getattr(settings, 'token_categories', {}).get(sym, {}).get('market_behavior', 'MIDCAP_ALT')
                _sl_pct = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {}).get('stop_loss_pct',
                    getattr(settings, 'stop_loss_pct', 1.8)) / 100.0
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
        _max_displace = getattr(settings, 'max_rebalance_per_cycle', 2)
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
                exit_price_sp = client.get_symbol_price(sym)
                valid, reason = is_quantity_valid(qty, exit_price_sp, filters)
                if not valid:
                    print(f"[SlotPressure] {sym}: invalid qty {qty} — {reason}")
                    continue
                order = client.create_market_order(symbol=sym, side="SELL", quantity=qty)
                exit_price_sp = float((order.get("fills") or [{}])[0].get("price", 0) or exit_price_sp)
                for pos in position_manager.get_all_positions().get(sym, []):
                    ep = float(pos.entry_price)
                    pnl = (exit_price_sp - ep) * float(pos.quantity)
                    position_manager.close_position(
                        position_id=pos.position_id,
                        exit_price=exit_price_sp,
                        pnl=pnl,
                        close_reason=_close_reason,
                    )
                _overflow_positions.discard(sym)
                _VOLSPIKE_POSITIONS.discard(sym)
                print(f"[SlotPressure] {sym}: displaced @ {exit_price_sp:.4f} reason={_close_reason}")
            except Exception as sell_err:
                print(f"[SlotPressure] {sym}: sell failed — {sell_err}")
        time.sleep(1)
    except Exception as e:
        print(f"Error in slot-pressure reallocation: {e}")


def execute_once(monitor_only: bool = False):
    global _db_client_ref
    from init_components import get_components
    comp = get_components()
    settings = comp.settings
    client = comp.binance_client
    news_client = comp.news_client
    position_manager = comp.position_manager
    portfolio_manager = comp.portfolio_manager

    # ── C3: Inicializar referencia DB para cooldowns persistentes ───────────────
    if _db_client_ref is None and hasattr(comp, 'db_client') and comp.db_client:
        _db_client_ref = comp.db_client
        _load_cooldowns_from_db()

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
                news_client=news_client, regime=_regime_mode_mon
            )
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

    # ── Regime detection for execute_once (needed by Step 0.5 and Step 1) ──────
    _regime_exe: MarketRegime = None
    _regime_mode = "SPOT_BULL"
    if getattr(settings, 'market_regime_enabled', True):
        try:
            _regime_exe = get_regime(client, settings=settings)
            _regime_mode = _regime_exe.mode
            print(f"[Regime] execute_once: {_regime_mode} | confirmed={_regime_exe.confirmed}")
            # F4 fix: on fresh start, anti-whipsaw needs 2 detections to confirm.
            # If the raw signal is unambiguous (severe structural bear: 30d < -15%), prime
            # the cache and immediately re-detect so the first cycle runs with confirmed=True
            # and SHORTs / FUTURES_SHORT mode can activate without waiting an extra 45min.
            if not _regime_exe.confirmed and _regime_exe.btc_30d_pct <= -15.0:
                from market_regime import prime_cache as _prime_regime_cache
                _prime_regime_cache(_regime_exe.mode)
                _regime_exe = get_regime(client, settings=settings)
                _regime_mode = _regime_exe.mode
                print(f"[Regime] execute_once warm-up (severe bear): {_regime_mode} | confirmed={_regime_exe.confirmed}")
        except Exception as _re_exe:
            print(f"[Regime] execute_once detection failed: {_re_exe}")

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
                        _price_excl = client.get_symbol_price(_excl_sym)
                        if _qty_held * _price_excl < 5.0:
                            continue
                        _filt_excl = get_symbol_filters(client, _excl_sym)
                        _qty_sell = align_quantity_to_step(_qty_held, _filt_excl['step'])
                        _valid_excl, _ = is_quantity_valid(_qty_sell, _price_excl, _filt_excl)
                        if not _valid_excl:
                            continue
                        _ord_excl = client.create_market_order(symbol=_excl_sym, side='SELL', quantity=_qty_sell)
                        _exit_p = float((_ord_excl.get('fills') or [{}])[0].get('price', 0) or _price_excl)
                        if position_manager:
                            position_manager.close_all_positions_for_symbol(_excl_sym, _exit_p, 'excluded_symbol_cleanup')
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
    sl_tp_closes = _check_sl_tp_positions(client, settings, position_manager, news_client=news_client, regime=_regime_mode)
    if sl_tp_closes:
        print(f"SL/TP closed {len(sl_tp_closes)} position(s): {[c['symbol'] for c in sl_tp_closes]}")

    # ── Step 0.5: Futures positions check + auto-fund on FUTURES_SHORT ──────────
    futures_closes = []
    if getattr(settings, 'futures_enabled', False):
        try:
            _fc = _get_futures_client(settings, db_client=getattr(comp, 'db_client', None))

            # CapBridge: bidirectional autonomous spot↔futures capital rebalancing
            if getattr(settings, 'futures_capital_bridge_enabled', True):
                try:
                    _cb_spot_usdt   = float(client.get_account_balance().get('USDT', 0.0))
                    _cb_open_shorts = sum(1 for p in _fc._open_positions.values() if not p.closed and p.side == 'SHORT')
                    _cb_max_shorts  = getattr(settings, 'futures_max_shorts', 5)
                    _cb_open_spots  = len(position_manager.get_all_positions()) if position_manager else 0
                    _cb_kelly_margin = max(
                        getattr(settings, 'futures_usdt_per_trade', 20.0),
                        _fc.get_available_usdt() * getattr(settings, 'futures_kelly_cap_pct', 0.15)
                    )
                    _cb_kelly_spot  = getattr(settings, 'kelly_base_usdt', 60.0)
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
            _min_cycles = getattr(settings, 'min_cycles_before_close_shorts', 3)
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
                    _commission_rate_b2 = getattr(settings, 'binance_commission_rate', 0.001)
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
                            _b2_price = client.get_symbol_price(_b2_sym)
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
                                    _b2_exit = float(((_b2_order.get("fills") or [{}])[0]).get("price", 0) or _b2_price)
                                    position_manager.close_all_positions_for_symbol(_b2_sym, _b2_exit, "regime_futures_short_spot_cleanup", _commission_rate_b2)
                                    _overflow_positions.discard(_b2_sym)
                                    _VOLSPIKE_POSITIONS.discard(_b2_sym)
                                    print(f"[B2] Closed spot LONG {_b2_sym} @ {_b2_exit:.4f} pnl_pct={_b2_pnl_pct:+.2f}% — FUTURES_SHORT regime, in profit")
                                except Exception as _b2_err:
                                    print(f"[B2] Failed to close spot {_b2_sym}: {_b2_err}")
                except Exception as _b2_outer:
                    print(f"[B2] Spot residual close error: {_b2_outer}")

            if _open_short_keys and _regime_mode not in ("PRESERVATION", "FUTURES_SHORT"):
                if _non_short_regime_cycles >= _min_cycles:
                    _protect_threshold = getattr(settings, 'futures_protect_profit_usdt', 1.0)
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
                        if _unr_pnl >= _protect_threshold:
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
                    print(f"[Futures] Regime is {_regime_mode} ({_non_short_regime_cycles}/{_min_cycles} cycles) — holding shorts (whipsaw guard)")

            # CapBridge handles idle-funds return via smart_rebalance_capital (Case 3 above)

            # Normal SL/TP cycle check (passes settings for trailing/SmartTP and client ref for momentum)
            _wire_futures_client(_fc, client, settings)
            _cycle_closes = _fc.check_positions(settings=settings)
            if _cycle_closes:
                futures_closes.extend(_cycle_closes)
                print(f"[Futures] Closed {len(_cycle_closes)} position(s) via SL/TP: {_cycle_closes}")
                for _cc in _cycle_closes:
                    _cc_sym = _cc.get('symbol') if isinstance(_cc, dict) else getattr(_cc, 'symbol', None)
                    if _cc_sym:
                        _futures_overflow_positions.discard(_cc_sym)
                        # FIX-F1: Register SL cooldown to prevent immediate re-entry on the same symbol
                        if _cc.get('reason') == 'stop_loss':
                            _sl_cd_hours = getattr(settings, 'futures_sl_cooldown_hours', 1.0)
                            _FUTURES_SL_COOLDOWN[_cc_sym] = datetime.now(timezone.utc)
                            _set_cooldown(_cc_sym, 'futures_sl', _sl_cd_hours, {'reason': 'sl_hit'})
                            print(f'[Futures/SL-CD] {_cc_sym}: cooldown {_sl_cd_hours}h — no re-entry until next window')
        except Exception as _fce:
            print(f"[Futures] check_positions error: {_fce}")

    # ── Step 1: Determine top symbols ─────────────────────────────────────────
    top_signals = _select_symbols(settings, news_client, client, portfolio_manager, regime=_regime_mode)

    # FUTURES_SHORT mode: always ensure futures_symbols are in the pool.
    # The long-signal generator only produces spot candidates — futures symbols
    # (BTC/ETH/SOL/BNB etc.) must be explicitly injected so open_short() can run.
    # BUG-E6: cache screened_pairs result for re-use within the same cycle
    _cached_screened_pairs: dict = {}
    if getattr(settings, 'screener_enabled', True):
        try:
            from pair_screener import get_screened_pairs as _gsp
            _cached_screened_pairs = _gsp(settings=settings)
        except Exception:
            pass

    if (_regime_mode == "FUTURES_SHORT" and _regime_exe is not None
            and _regime_exe.confirmed and getattr(settings, 'futures_enabled', False)):
        _f_syms = list(getattr(settings, 'futures_symbols', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']))
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
                if _ta and isinstance(_ta, dict) and _ta.get('signal') != 'HOLD':
                    _ta_strength = float(_ta.get("strength", 0.5))
                    _ta_ind      = _ta.get("indicators", {})
                    _fsig["strength"]       = _ta_strength
                    _fsig["vol_spike"]      = float(_ta.get("vol_spike", _ta_ind.get("volume_spike", 0.0)))
                    _fsig["rsi"]            = float(_ta_ind.get("rsi", 50.0))
                    _fsig["change_24h_pct"] = float(_ta.get("change_24h_pct", 0.0))
                    _fsig["score"]          = int(_ta_strength * 100)
                    _fsig["reason"]         = _ta.get("reason", "FUTURES_SHORT_AUTO")
                    print(f"[Futures/Inject] {_fsym}: strength={_ta_strength:.2f} rsi={_fsig['rsi']:.1f} signal={_ta.get('signal')}")
                elif _ta and _ta.get('signal') == 'HOLD':
                    _ta_strength = float(_ta.get("strength", 0.3))
                    _fsig["strength"] = _ta_strength
                    _fsig["score"]    = int(_ta_strength * 100)
                    _fsig["rsi"]      = float(_ta.get("indicators", {}).get("rsi", 50.0))
                    print(f"[Futures/Inject] {_fsym}: HOLD (strength={_ta_strength:.2f}) — kept at low strength")
            except Exception as _te:
                print(f"[Futures/Inject] {_fsym}: analysis failed ({_te}) — using default strength=0.50")
            _new_futures.append(_fsig)
        if _new_futures:
            print(f"[Futures] FUTURES_SHORT: injecting {[s['symbol'] for s in _new_futures]} into signal pool")
            top_signals = _new_futures + list(top_signals)  # futures first

    if not top_signals:
        print("No buy signals available; skipping new buys this cycle.")
        return {"top_symbols": [], "sell_orders": [], "buy_orders": [], "sl_tp_closes": sl_tp_closes}

    # Normalize: _select_symbols may return dicts or strings
    if top_signals and isinstance(top_signals[0], dict):
        top_symbols = [s['symbol'] for s in top_signals]
    else:
        top_symbols = list(top_signals)

    # ── Step 2: Generate predictions once (reused by step 3 and step 5) ───────
    # BUG-E4: pass signal_meta so pnl_service reuses pre-computed indicators from Step 1
    _step2_signal_meta = {s['symbol']: s for s in top_signals if isinstance(s, dict)}
    predictions = _build_predictions(top_symbols, client, settings, portfolio_manager, signal_meta=_step2_signal_meta)

    # ── MEJORA-A1: Re-sort by predicted EV so highest-EV candidates fill slots first ──
    if predictions and _regime_mode != "FUTURES_SHORT" and isinstance(top_signals[0], dict) if top_signals else False:
        def _ev_sort_key(s):
            sym = s['symbol'] if isinstance(s, dict) else s
            pred = predictions.get(sym)
            return float(pred.get('expected_pnl', 0)) if pred else 0.0
        top_signals = sorted(top_signals, key=_ev_sort_key, reverse=True)
        top_symbols = [s['symbol'] if isinstance(s, dict) else s for s in top_signals]
        print(f"[EV-sort] Re-ranked {len(top_symbols)} candidates by predicted EV: {[(s, round(predictions.get(s, {}).get('expected_pnl', 0), 2)) for s in top_symbols[:5]]}")

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
    buy_orders = _buy_top_symbols(client, settings, top_signals, portfolio_manager, position_manager, precomputed_predictions=predictions, news_client=news_client, regime_obj=_regime_exe, regime_mode_override=_regime_mode, screened_pairs_cache=_cached_screened_pairs)

    # ── Dynamic cycle scheduling — return next interval based on active regime ──
    _cycle_by_regime = {
        "SPOT_BULL":     getattr(settings, 'cycle_spot_bull',    10),
        "RANGE_SCALP":   getattr(settings, 'cycle_range_scalp',   5),
        "PRESERVATION":  getattr(settings, 'cycle_preservation',  45),
        "FUTURES_SHORT": getattr(settings, 'cycle_futures_short',  6),
    }
    _next_cycle = _cycle_by_regime.get(_regime_mode, getattr(settings, 'trade_cycle_minutes', 5))
    print(f"Completed cycle. Next cycle in {_next_cycle} min (regime={_regime_mode})")
    return {"top_symbols": top_symbols, "sell_orders": sell_orders,
            "buy_orders": buy_orders, "sl_tp_closes": sl_tp_closes,
            "futures_closes": futures_closes, "next_cycle_minutes": _next_cycle,
            "regime_mode": _regime_mode}


if __name__ == '__main__':
    settings = get_settings()
    execute_once()
