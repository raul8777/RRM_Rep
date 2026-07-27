"""
futures_client.py — Binance USDM Futures wrapper (fapi)

Provides SHORT position management with conservative risk controls:
  - Fixed 2x leverage (never higher)
  - Mandatory stop-loss on every position
  - Max 2 simultaneous short positions
  - Funding rate check before entry (skip if funding > 0.05%)
  - Dry-run support (mirrors binance_client.py dry_run flag)

API base: https://fapi.binance.com
Auth: same API key/secret as spot, requires Futures trading enabled on account.

Usage:
    fc = FuturesClient(api_key, api_secret, dry_run=False)
    pos = fc.open_short("BTCUSDT", usdt_amount=50.0, sl_pct=3.0, tp_pct=5.0)
    fc.close_short("BTCUSDT", pos["qty"])
"""

import json
import time
import hashlib
import hmac
import urllib.request
import urllib.parse
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from config_helpers import get_param
from loss_focus import build_loss_focus_snapshot, evaluate_loss_focus
import error_collector as ec
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
from ug_loss_policy import evaluate_shadow as _evaluate_ug_loss_shadow
from market_structure import build_snapshot as _build_lifecycle_structure_snapshot, evaluate as _evaluate_lifecycle_structure
from binance_truth_guard import (
    evaluate_futures_parity,
    auto_reconcile_futures,
    persist_truth_status,
    audit_truth_mismatches,
)


def _parse_utc_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_metadata(raw, default_trade_type: str = "futures"):
    if raw is None:
        return {"trade_type": default_trade_type}
    if isinstance(raw, dict):
        meta = dict(raw)
    elif isinstance(raw, str):
        try:
            meta = json.loads(raw)
        except Exception:
            return {"trade_type": default_trade_type, "_metadata_corrupt_raw": raw}
    else:
        return {"trade_type": default_trade_type, "_metadata_corrupt_raw": repr(raw)}
    if not isinstance(meta, dict):
        return {"trade_type": default_trade_type, "_metadata_corrupt_raw": repr(raw)}
    trade_type = str(meta.get("trade_type") or default_trade_type).strip().lower()
    meta["trade_type"] = trade_type if trade_type in ("spot", "futures") else default_trade_type
    return meta


# ── Constants ─────────────────────────────────────────────────────────────────
FAPI_BASE     = "https://fapi.binance.com"
# Global safe caps — these are UPPER BOUNDS, not the active configured values.
# Active values come from settings.futures_leverage / settings.futures_max_shorts.
LEVERAGE_MAX  = 10         # hard upper bound; active leverage is settings.futures_leverage (backtest run_0812: 10)
MAX_SHORTS    = 12         # hard upper bound; active max is settings.futures_max_shorts (backtest run_0812: 12)
FUNDING_LIMIT = 0.0005     # Skip entry if funding rate > 0.05% (overleveraged longs → risky to short)
_PRICE_CACHE_TTL = 15      # seconds


@dataclass
class FuturesPosition:
    symbol: str
    side: str                    # "SHORT"
    qty: float
    entry_price: float
    sl_price: float
    tp_price: float
    order_id: str
    sl_order_id: Optional[str]
    tp_order_id: Optional[str]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_prediction: Optional[Dict] = field(default=None, repr=False, compare=False)       # FIX-P2: predictor output at entry
    indicators_at_entry: Optional[Dict] = field(default=None, repr=False, compare=False)  # FIX-P2: technical indicators at entry
    _smart_meta: Dict = field(default_factory=dict, repr=False, compare=False)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty

    def is_sl_hit(self, current_price: float) -> bool:
        if self.side == "LONG":
            return current_price <= self.sl_price   # LONG SL is below entry
        return current_price >= self.sl_price       # SHORT SL is above entry

    def is_tp_hit(self, current_price: float) -> bool:
        if self.side == "LONG":
            return current_price >= self.tp_price   # LONG TP is above entry
        return current_price <= self.tp_price       # SHORT TP is below entry


class FuturesClient:
    def __init__(self, api_key: str, api_secret: str, dry_run: bool = False,
                 max_shorts: int = MAX_SHORTS, max_longs: int = 3,
                 db_client=None, settings=None):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.dry_run    = dry_run
        self.settings = settings
        # Active trading caps are read from settings; module constants are only safety upper bounds.
        from config_helpers import get_param
        self._active_leverage = min(
            get_param(settings, 'futures_leverage') if settings else 10,
            LEVERAGE_MAX
        )
        self.max_shorts = min(
            get_param(settings, 'futures_max_shorts') if settings else 12,
            MAX_SHORTS
        )
        self.max_longs  = max_longs        # configurable via settings.futures_max_longs
        self.max_largecap_shorts: int = get_param(settings, 'futures_max_largecap_shorts') if settings else 5
        self.largecap_symbols: set = set(get_param(settings, 'futures_largecap_symbols')) if settings else {'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'SOLUSDT'}
        # Key format: "{SYMBOL}_{SIDE}" e.g. "BTCUSDT_SHORT", "ETHUSDT_LONG"
        self._open_positions: Dict[str, FuturesPosition] = {}
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # {symbol: (price, ts)}
        self._leverage_set: set = set()   # symbols with leverage already configured
        self._hedge_mode: Optional[bool] = None  # None = unknown until checked
        self._binance_client_ref = None  # set by _get_futures_client() for momentum eval
        self.db_client = db_client        # FIX-G: DB persistence for futures trades
        self._cached_settings = settings  # FIX-G3: keep settings for commission/config lookups
        if not dry_run:
            self._sync_open_positions()

    # ── DB persistence helpers (FIX-G) ──────────────────────────────────────

    def _save_futures_open(self, pos: 'FuturesPosition') -> None:
        """Persist a newly opened futures position to trades_open."""
        if not self.db_client:
            return
        try:
            # Paso 4: detect escalonamiento — count existing DB records for same symbol
            _db_side = 'SELL' if pos.side == 'SHORT' else 'BUY'
            _existing = self.db_client.execute_query(
                "SELECT COUNT(*) as cnt FROM trades_open WHERE symbol = %s AND side = %s "
                "AND metadata->>'trade_type' = 'futures'",
                (pos.symbol, _db_side)
            )
            _existing_count = int((_existing[0]['cnt'] if _existing else 0) or 0)
            _esc_index = _existing_count + 1
            _es_esc = _existing_count > 0
            # Commission on entry
            from binance_commission import entry_commission
            _entry_comm = round(entry_commission(pos.qty, pos.entry_price, 'futures', self._cached_settings), 6)
            meta = json.dumps({
                'trade_type': 'futures',
                'order_id': pos.order_id,
                'sl_order_id': pos.sl_order_id,
                'tp_order_id': pos.tp_order_id,
                'escalonamiento_index': _esc_index,
                'es_escalonamiento': _es_esc,
                'entry_commission': _entry_comm,
                'regime_at_entry': pos._smart_meta.get('regime_at_entry'),
                'profile_at_entry': pos._smart_meta.get('profile_at_entry'),
                'signal_id': pos._smart_meta.get('signal_id', ''),
            })
            _pnl_pred_json = json.dumps(pos.pnl_prediction) if pos.pnl_prediction else None
            _ind_json      = json.dumps(pos.indicators_at_entry) if pos.indicators_at_entry else None
            self.db_client.execute_update(
                """
                INSERT INTO trades_open
                    (symbol, side, quantity, price, entry_time, stop_loss, take_profit,
                     metadata, entry_commission, pnl_prediction, indicators_at_entry)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pos.symbol,
                    'SELL' if pos.side == 'SHORT' else 'BUY',
                    pos.qty,
                    pos.entry_price,
                    pos.opened_at.isoformat(),
                    pos.sl_price,
                    pos.tp_price,
                    meta,
                    _entry_comm,
                    _pnl_pred_json,
                    _ind_json,
                )
            )
            print(f'[Futures/DB] {pos.symbol} {pos.side} OPEN saved to trades_open '
                  f'(pred={pos.pnl_prediction is not None}, esc_idx={_esc_index})')
            try:
                from futures_correlation import invalidate_cache as _corr_inv
                _corr_inv()
            except Exception:
                pass
            self._audit(pos.symbol, 'open', {
                'side': pos.side, 'qty': pos.qty, 'price': pos.entry_price,
                'sl': pos.sl_price, 'tp': pos.tp_price,
                'order_id': pos.order_id, 'esc_index': _esc_index,
                'signal_id': pos._smart_meta.get('signal_id', ''),
                'binance_confirmed': pos._smart_meta.get('binance_entry_confirmed', False),
            }, binance_confirmed=bool(pos._smart_meta.get('binance_entry_confirmed', False)))
        except Exception as e:
            print(f'[Futures/DB] save_open {pos.symbol} failed: {e}')

    def _persist_smart_meta(self, pos: 'FuturesPosition') -> None:
        """FIX-S1: Persist _smart_meta to trades_open metadata so it survives container restarts.
        Called after breakeven, trailing, and SmartTP updates."""
        if not self.db_client:
            return
        try:
            _db_side = 'SELL' if pos.side == 'SHORT' else 'BUY'
            # FIX-3: Order by proximity to entry_price to always pick the correct row
            # when multiple escalamiento entries exist for the same symbol
            rows = self.db_client.execute_query(
                "SELECT id, metadata FROM trades_open WHERE symbol = %s AND side = %s "
                "AND metadata->>'trade_type' = 'futures' "
                "ORDER BY ABS(price::float - %s) ASC LIMIT 1",
                (pos.symbol, _db_side, pos.entry_price)
            )
            if rows:
                _row = rows[0]
                _meta = _row.get('metadata') or {}
                if isinstance(_meta, str):
                    _meta = json.loads(_meta)
                _meta['_meta_version'] = int(_meta.get('_meta_version', 0) or 0) + 1
                _meta['_meta_updated_at'] = datetime.now(timezone.utc).isoformat()
                _meta['smart_meta'] = pos._smart_meta
                self.db_client.execute_update(
                    "UPDATE trades_open SET metadata = %s, stop_loss = %s, take_profit = %s WHERE id = %s",
                    (json.dumps(_meta), pos.sl_price, pos.tp_price, _row['id'])
                )
        except Exception as e:
            print(f'[Futures/DB] persist_smart_meta {pos.symbol} failed: {e}')

    def reevaluate_open_positions(self, settings, regime_mode: str, profile_label: str,
                                  switch_id: Optional[str] = None,
                                  transition_policy: Optional[Dict] = None) -> int:
        """Refresh active futures positions after a regime/profile switch.

        Updates metadata context for all open futures positions and realigns software SL/TP
        for positions that are not already in breakeven/trailing management.
        """
        _updated = 0
        _regime_mode = str(regime_mode or '').strip().upper() or 'SPOT_BULL'
        _profile_label = str(profile_label or '').strip().upper() or 'A'
        _sl_pct = float(get_param(settings, 'futures_sl_pct') if settings else 3.0)
        _tp_pct = float(get_param(settings, 'futures_tp_pct') if settings else 5.0)
        for _pos in list(self._open_positions.values()):
            if _pos.closed:
                continue
            _pos._smart_meta['current_regime_mode'] = _regime_mode
            _pos._smart_meta['current_profile_label'] = _profile_label
            _pos._smart_meta['last_transition_switch_id'] = switch_id
            if transition_policy:
                _pos._smart_meta['transition_policy'] = transition_policy

            # Keep existing trailing/breakeven management untouched; otherwise refresh software SL/TP.
            _managed = bool(_pos._smart_meta.get('breakeven_done') or _pos._smart_meta.get('trail_best_price'))
            if not _managed:
                if _pos.side == 'SHORT':
                    _new_sl = round(_pos.entry_price * (1 + _sl_pct / 100), 6)
                    _new_tp = round(_pos.entry_price * (1 - _tp_pct / 100), 6)
                else:
                    _new_sl = round(_pos.entry_price * (1 - _sl_pct / 100), 6)
                    _new_tp = round(_pos.entry_price * (1 + _tp_pct / 100), 6)
                self.update_sl_tp(_pos.symbol, _pos.side, new_sl=_new_sl, new_tp=_new_tp)
            self._persist_smart_meta(_pos)
            _updated += 1
        return _updated

    def _audit(self, symbol: str, event: str, detail: dict, binance_confirmed: bool = False) -> None:
        """FIX-9: Write a traceability record to trades_audit for every critical event."""
        if not self.db_client:
            return
        original_event = event
        if len(event) > 40:
            event = event[:40]
            print(f"[Futures/Audit] event truncated ({symbol}): '{original_event}' -> '{event}'")
        try:
            self.db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                "VALUES (%s, %s, %s, %s, %s)",
                (symbol, 'futures', event, json.dumps(detail), binance_confirmed)
            )
        except Exception as e:
            # Do not interrupt trading, but never silence audit failures.
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR,
                         f"Futures audit write failed ({symbol}:{event}): {e}")
            print(f"[Futures/Audit] failed {symbol}:{event}: {e}")

    def _save_futures_closed(self, pos: 'FuturesPosition', reason: str,
                              close_group_id: Optional[str] = None,
                              esc_index: int = 1, total_esc: int = 1,
                              pnl_override: Optional[float] = None) -> None:
        """Persist a closed futures position to trades_closed and remove from trades_open.
        Supports proportional PnL across escalamiento entries (via pnl_override).
        """
        if not self.db_client:
            return
        try:
            exit_price = pos.exit_price or pos.entry_price
            if pos.side == 'SHORT':
                gross_pnl = (pos.entry_price - exit_price) * pos.qty
                pnl_pct   = round((pos.entry_price - exit_price) / pos.entry_price * 100, 4)
            else:
                gross_pnl = (exit_price - pos.entry_price) * pos.qty
                pnl_pct   = round((exit_price - pos.entry_price) / pos.entry_price * 100, 4)
            # Commission accounting — use centralized Binance commission module
            from binance_commission import entry_commission, exit_commission, total_commission
            _settings = getattr(self, '_cached_settings', None)
            _entry_comm = round(entry_commission(pos.qty, pos.entry_price, 'futures', _settings), 6)
            _exit_comm = round(exit_commission(pos.qty, exit_price, 'futures', _settings), 6)
            if pos._smart_meta.get('pnl_accounting_mode') == 'binance_user_trade_verified':
                _exit_comm = round(float(pos._smart_meta.get('binance_close_commission', _exit_comm) or 0.0), 6)
            _total_comm = _entry_comm + _exit_comm
            if pnl_override is not None:
                # Proportional PnL from multi-entry close — ensure net of commission
                pnl = pnl_override - _total_comm
            elif pos.pnl is not None:
                # Binance realizedPnl (SL/TP/trailing closes) — already net, do NOT deduct again
                pnl = pos.pnl
            else:
                # Local SSO market-order close — gross, must deduct both sides
                pnl = gross_pnl - _total_comm
            # Store commission breakdown in metadata for traceability
            pos._smart_meta['entry_commission'] = _entry_comm
            pos._smart_meta['exit_commission'] = _exit_comm
            pos._smart_meta['total_commission'] = _total_comm
            _cgid = close_group_id or str(uuid.uuid4())
            meta = json.dumps({
                'trade_type': 'futures',
                'order_id': pos.order_id,
                'sl_order_id': pos.sl_order_id,
                'tp_order_id': pos.tp_order_id,
                'smart_meta': pos._smart_meta,
                'close_group_id': _cgid,
                'escalonamiento_index': esc_index,
                'total_escalonamientos': total_esc,
                'es_escalonamiento': total_esc > 1,
                'bounce_entry': bool(pos._smart_meta.get('bounce_entry', False)),  # FIX-4: root for easy lookup
                'btc_at_entry': pos._smart_meta.get('btc_price_at_entry'),
            })
            _pnl_pred_json = json.dumps(pos.pnl_prediction) if pos.pnl_prediction else None
            _ind_json      = json.dumps(pos.indicators_at_entry) if pos.indicators_at_entry else None
            # FIX-P3: prediction_accuracy = (real_pnl - predicted_pnl) / |predicted_pnl|
            _pred_accuracy = None
            if pos.pnl_prediction:
                _pred_pnl = float(pos.pnl_prediction.get('expected_pnl', 0) or 0)
                if _pred_pnl != 0:
                    _pred_accuracy = round((pnl - _pred_pnl) / abs(_pred_pnl), 4)
            insert_query = """
                INSERT INTO trades_closed
                    (symbol, side, quantity, entry_price, exit_price, pnl, pnl_percentage,
                     close_reason, entry_time, exit_time, metadata, data_version,
                     pnl_prediction, indicators_at_entry, prediction_accuracy, learning_eligible,
                     entry_commission, exit_commission)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            insert_params = (
                pos.symbol,
                'SELL' if pos.side == 'SHORT' else 'BUY',
                pos.qty,
                pos.entry_price,
                exit_price,
                round(pnl, 6),
                pnl_pct,
                reason,
                pos.opened_at.isoformat(),
                (pos.closed_at or datetime.now(timezone.utc)).isoformat(),
                meta,
                'futures_v2',
                _pnl_pred_json,
                _ind_json,
                _pred_accuracy,
                True,
                _entry_comm,
                _exit_comm,
            )

            # Remove from trades_open — delete exactly one row (oldest matching) using ctid
            _db_side_del = 'SELL' if pos.side == 'SHORT' else 'BUY'
            delete_query = (
                "DELETE FROM trades_open WHERE ctid = ("
                "  SELECT ctid FROM trades_open WHERE symbol = %s AND price = %s AND side = %s "
                "  AND metadata->>'trade_type' = 'futures' ORDER BY entry_time ASC LIMIT 1"
                ")"
            )
            delete_params = (pos.symbol, pos.entry_price, _db_side_del)

            self.db_client.execute_transaction([
                (insert_query, insert_params),
                (delete_query, delete_params),
            ])
            print(f'[Futures/DB] {pos.symbol} {pos.side} CLOSED ({reason}) pnl=${pnl:.2f} '
                  f'(comm=${_entry_comm+_exit_comm:.4f} esc={esc_index}/{total_esc} grp={_cgid[:8]}) saved')
            # P1.1: Invalidate daily PnL cache so next SSO-4 evaluation sees the updated total
            try:
                from daily_pnl_tracker import invalidate_cache as _inv_dpnl
                _inv_dpnl()
            except Exception:
                pass
            # P3.2: Invalidate correlation cache — portfolio composition changed
            try:
                from futures_correlation import invalidate_cache as _corr_inv
                _corr_inv()
            except Exception:
                pass
            _binance_close = reason in ('trailing_sl_binance', 'stop_loss_binance', 'take_profit_binance',
                                         'stop_loss_binance_safety', 'binance_safety_close', 'binance_closed_untracked')
            self._audit(pos.symbol, 'close', {
                'side': pos.side, 'qty': pos.qty, 'entry_price': pos.entry_price,
                'exit_price': exit_price, 'pnl': round(pnl, 4), 'reason': reason,
                'bounce_entry': bool(pos._smart_meta.get('bounce_entry', False)),
                'esc': f"{esc_index}/{total_esc}", 'group': _cgid[:8],
                'signal_id': pos._smart_meta.get('signal_id', ''),
            }, binance_confirmed=_binance_close)
        except Exception as e:
            print(f'[Futures/DB] save_closed {pos.symbol} failed: {e}')

    # ── FIX-T4 helper: query Binance for realized PnL of closed position ────

    def _get_realized_pnl_from_trades(self, symbol: str, side: str, opened_at: Optional[datetime] = None) -> dict:
        """Query Binance userTrades for the most recent closing trade on this symbol/side.
        Used when Binance executes SL/TP independently and our close call fails with -2022."""
        try:
            trades = self._get("/fapi/v1/userTrades", {
                "symbol": symbol, "limit": "10"
            }, signed=True)
            relevant = []
            for t in trades:
                _t_side = t.get("positionSide", "BOTH")
                if _t_side == "BOTH":
                    _t_side = "SHORT" if not t.get("buyer", True) else "LONG"
                if _t_side != side.upper():
                    continue
                if opened_at and int(t.get("time", 0) or 0) < int(opened_at.timestamp() * 1000):
                    continue
                relevant.append(t)
            closing_trades = [t for t in relevant if float(t.get("realizedPnl", 0) or 0) != 0]
            if not closing_trades:
                return {"exit_price": 0, "pnl": 0, "reason": "binance_safety_close"}
            closing_time = max(int(t.get("time", 0) or 0) for t in closing_trades)
            closing_trades = [t for t in closing_trades if int(t.get("time", 0) or 0) == closing_time]
            realized_pnl = sum(float(t.get("realizedPnl", 0) or 0) for t in closing_trades)
            close_qty = sum(float(t.get("qty", 0) or 0) for t in closing_trades)
            exit_price = (
                sum(float(t.get("price", 0) or 0) * float(t.get("qty", 0) or 0) for t in closing_trades) / close_qty
                if close_qty > 0 else 0
            )
            commission = sum(abs(float(row.get("commission", 0) or 0)) for row in relevant)
            funding = 0.0
            try:
                params = {"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": "100"}
                if opened_at:
                    params["startTime"] = str(int(opened_at.timestamp() * 1000))
                income_rows = self._get("/fapi/v1/income", params, signed=True)
                funding = sum(float(row.get("income", 0) or 0) for row in income_rows or [])
            except Exception as funding_error:
                print(f"[Futures] funding reconciliation unavailable for {symbol}: {funding_error}")
            return {
                "exit_price": exit_price,
                "pnl": realized_pnl - commission + funding,
                "realized_pnl": realized_pnl,
                "commission": commission,
                "funding": funding,
                "pnl_accounting_mode": "binance_user_trade_verified",
                "reason": "trailing_sl_binance" if realized_pnl > 0 else "stop_loss_binance",
            }
        except Exception as e:
            print(f"[Futures] _get_realized_pnl_from_trades {symbol}: {e}")
        return {"exit_price": 0, "pnl": 0, "reason": "binance_safety_close"}

    def _get_position_qty_real(self, symbol: str, side: str) -> float:
        """Return absolute positionAmt for symbol/side from Binance positionRisk."""
        if self.dry_run:
            return 0.0
        try:
            positions = self._get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
            for p in positions:
                if p.get("positionSide") == side.upper():
                    return abs(float(p.get("positionAmt", 0)))
        except Exception as e:
            print(f"[Futures/Verify] {symbol} {side}: qty fetch failed: {e}")
        return 0.0

    def _verify_position_closed(self, symbol: str, side: str) -> bool:
        """Query Binance positionRisk to confirm position is actually closed (qty=0)."""
        if self.dry_run:
            return True
        qty = self._get_position_qty_real(symbol, side)
        if qty > 0:
            print(f"[Futures/Verify] {symbol} {side}: STILL OPEN on Binance (amt={qty})")
            return False
        print(f"[Futures/Verify] {symbol} {side}: confirmed closed on Binance ✓")
        return True

    def _verify_entry_filled(self, symbol: str, side: str) -> bool:
        """Query Binance positionRisk to confirm entry order was actually executed (qty>0).

        Mirrors _verify_position_closed() but checks for the opposite condition.
        Called after placing MARKET entry in open_short/open_long before persisting to DB.
        """
        if self.dry_run:
            return True
        try:
            qty = self._get_position_qty_real(symbol, side)
            if qty > 0:
                print(f"[Futures/Verify] {symbol} {side}: entry confirmed on Binance (amt={qty}) ✓")
                return True
            print(f"[Futures/Verify] {symbol} {side}: entry NOT confirmed on Binance — positionAmt=0")
            return False
        except Exception as e:
            print(f"[Futures/Verify] {symbol} {side}: entry verification failed: {e} — NOT assuming filled")
            return False

    # ── Position key helpers ──────────────────────────────────────────────────

    @staticmethod
    def _pos_key(symbol: str, side: str) -> str:
        """Canonical key for _open_positions dict: 'BTCUSDT_SHORT' or 'BTCUSDT_LONG'."""
        return f"{symbol}_{side.upper()}"

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _sign(self, params: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request_with_retry(self, req: urllib.request.Request, timeout: int, path: str = "") -> bytes:
        """Execute a urllib request with retry on transient network errors.

        Retries on: socket.timeout, urllib.error.URLError, TimeoutError.
        Retries on: HTTP 5xx / 502 / 503.
        Does NOT retry on: Binance business errors (4xxx codes), HTTP 4xx.
        Backoff: [0.5s, 1.5s] between attempts (max_retries=2 extra attempts).
        """
        import socket as _socket
        import urllib.error as _ue
        _backoff = [0.5, 1.5]
        _max_retries = len(_backoff)
        for _attempt in range(_max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except (_socket.timeout, TimeoutError) as e:
                if _attempt < _max_retries:
                    print(f"[FuturesClient] timeout retry {_attempt + 1}/{_max_retries} for {path}: {e}")
                    time.sleep(_backoff[_attempt])
                else:
                    raise
            except _ue.HTTPError as e:
                if e.code in (502, 503, 504) and _attempt < _max_retries:
                    print(f"[FuturesClient] HTTP {e.code} retry {_attempt + 1}/{_max_retries} for {path}")
                    time.sleep(_backoff[_attempt])
                else:
                    body = e.read().decode("utf-8", errors="replace")
                    raise Exception(f"HTTP {e.code} {e.reason} — {body}") from None
            except _ue.URLError as e:
                if _attempt < _max_retries:
                    print(f"[FuturesClient] URLError retry {_attempt + 1}/{_max_retries} for {path}: {e.reason}")
                    time.sleep(_backoff[_attempt])
                else:
                    raise
        raise RuntimeError(f"[FuturesClient] _request_with_retry exhausted for {path}")

    def _get(self, path: str, params: dict = None, signed: bool = False, timeout: int = 5) -> dict:
        p = params or {}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(p)
            query += f"&signature={self._sign(query)}"
        else:
            query = urllib.parse.urlencode(p)
        url = f"{FAPI_BASE}{path}?{query}" if query else f"{FAPI_BASE}{path}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": self.api_key})
        return json.loads(self._request_with_retry(req, timeout, path))

    def _evaluate_lifecycle_structure(self, symbol: str, current_price: float, side: str, settings) -> Dict[str, Any]:
        mode = str(get_param(settings, 'lifecycle_structure_mode', 'shadow') or 'shadow').lower()

        def _fetch(target_symbol: str, interval: str, limit: int) -> List[List]:
            return self._get('/fapi/v1/klines', {'symbol': target_symbol, 'interval': interval, 'limit': limit}) or []

        snapshot = _build_lifecycle_structure_snapshot(_fetch, symbol, current_price)
        decision = _evaluate_lifecycle_structure(snapshot, side).to_dict()
        return {
            'mode': mode,
            'snapshot': snapshot,
            'decision': decision,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }

    def _post(self, path: str, params: dict, timeout: int = 8) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        query += f"&signature={self._sign(query)}"
        url  = f"{FAPI_BASE}{path}"
        data = query.encode("utf-8")
        req  = urllib.request.Request(
            url, data=data, method="POST",
            headers={"X-MBX-APIKEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"}
        )
        return json.loads(self._request_with_retry(req, timeout, path))

    def _delete(self, path: str, params: dict, timeout: int = 8) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urllib.parse.urlencode(params)
        query += f"&signature={self._sign(query)}"
        url  = f"{FAPI_BASE}{path}?{query}"
        req  = urllib.request.Request(
            url, method="DELETE",
            headers={"X-MBX-APIKEY": self.api_key}
        )
        return json.loads(self._request_with_retry(req, timeout, path))

    # ── Market data ───────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        now = time.monotonic()
        if symbol in self._price_cache:
            val, ts = self._price_cache[symbol]
            if now - ts < _PRICE_CACHE_TTL:
                return val
        data  = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        price = float(data["price"])
        self._price_cache[symbol] = (price, now)
        return price

    def get_funding_rate(self, symbol: str) -> float:
        """Return latest funding rate for symbol. Returns 0.0 on error."""
        try:
            data = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
            return float(data[0]["fundingRate"]) if data else 0.0
        except Exception:
            return 0.0

    def get_open_interest(self, symbol: str) -> float:
        """Returns open interest in contracts (useful for liquidity check)."""
        try:
            data = self._get("/fapi/v1/openInterest", {"symbol": symbol})
            return float(data.get("openInterest", 0))
        except Exception:
            return 0.0

    def get_symbol_info(self, symbol: str) -> dict:
        """Get futures symbol info (step size, min qty, etc)."""
        try:
            info = self._get("/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    return s
        except Exception:
            pass
        return {}

    def _get_step_size(self, symbol: str) -> float:
        info = self.get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                return float(f.get("stepSize", 0.001))
        return 0.001

    def _get_tick_size(self, symbol: str) -> float:
        """Return PRICE_FILTER tickSize for triggerPrice alignment."""
        info = self.get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                return float(f.get("tickSize", "0.01"))
        # Fallback: derive from pricePrecision
        prec = int(info.get("pricePrecision", 2))
        return 10 ** (-prec)

    def _align_price(self, price: float, tick: float) -> float:
        """Align price to tickSize (same logic as _align_qty for stepSize)."""
        from decimal import Decimal
        if tick <= 0:
            return round(price, 2)
        p = Decimal(str(price))
        t = Decimal(str(tick))
        aligned = (p / t).to_integral_value() * t
        return float(aligned)

    # ── ATR (Average True Range) for volatility-aware trailing ─────────────────
    _atr_cache: Dict[str, Tuple[float, float]] = {}  # symbol → (atr_pct, timestamp)
    _ATR_CACHE_TTL = 300  # 5 minutes

    def get_atr_pct(self, symbol: str, interval: str = "5m", periods: int = 14) -> float:
        """Return ATR as a percentage of current price (cached 5 min).
        
        ATR measures average candle range — represents normal volatility noise.
        Trail distances must be > ATR to avoid triggering on normal wicks.
        """
        if self.dry_run:
            return 0.005  # 0.5% fallback for dry-run/tests — no network call

        now = time.time()
        cached = self._atr_cache.get(symbol)
        if cached and (now - cached[1]) < self._ATR_CACHE_TTL:
            return cached[0]

        try:
            # Fetch klines: [open_time, open, high, low, close, volume, ...]
            klines = self._get(f"/fapi/v1/klines", {
                "symbol": symbol, "interval": interval, "limit": periods + 1
            })
            if not klines or len(klines) < periods + 1:
                return 0.005  # fallback 0.5%

            # True Range = max(H-L, |H-prev_close|, |L-prev_close|)
            tr_values = []
            for i in range(1, len(klines)):
                h = float(klines[i][2])
                l = float(klines[i][3])
                prev_c = float(klines[i - 1][4])
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                tr_values.append(tr)

            atr = sum(tr_values[-periods:]) / periods
            # Convert to percentage of current price
            current_price = float(klines[-1][4])
            atr_pct = (atr / current_price) if current_price > 0 else 0.005

            self._atr_cache[symbol] = (atr_pct, now)
            return atr_pct
        except Exception as e:
            print(f"[Futures/ATR] {symbol} fetch failed: {e} — using fallback 0.5%")
            return 0.005  # safe fallback

    def compute_trail_distance(self, symbol: str, pnl_pct: float, settings=None) -> float:
        """Compute ATR-scaled trailing distance for a given symbol and PnL level.
        
        Returns trail distance as a decimal (e.g., 0.008 = 0.8%).
        Formula: trail_dist = max(floor, min(ceiling, ATR * multiplier))
        """
        _atr_enabled = getattr(settings, 'futures_trail_atr_enabled', True) if settings else True
        _floor = getattr(settings, 'futures_trail_floor_pct', 0.4) / 100.0 if settings else 0.004
        _ceiling = getattr(settings, 'futures_trail_ceiling_pct', 1.2) / 100.0 if settings else 0.012

        if not _atr_enabled:
            # Fallback to static schedule (legacy)
            return getattr(settings, 'futures_trail_atr_fallback_distance_pct', 1.5) / 100.0 if settings else 0.015

        # Select multiplier based on PnL level
        if pnl_pct >= 0.08:
            mult = getattr(settings, 'futures_trail_atr_multiplier_deep', 0.8) if settings else 0.8
        elif pnl_pct >= 0.05:
            mult = getattr(settings, 'futures_trail_atr_multiplier_high', 1.0) if settings else 1.0
        elif pnl_pct >= 0.03:
            mult = getattr(settings, 'futures_trail_atr_multiplier_mid', 1.2) if settings else 1.2
        else:
            mult = getattr(settings, 'futures_trail_atr_multiplier_low', 1.5) if settings else 1.5

        atr_pct = self.get_atr_pct(symbol)
        trail_dist = atr_pct * mult

        # Clamp between floor and ceiling
        trail_dist = max(_floor, min(_ceiling, trail_dist))
        return trail_dist

    def _align_qty(self, qty: float, step: float) -> float:
        from decimal import Decimal
        if step <= 0 or qty <= 0:
            return 0.0
        q    = Decimal(str(qty))
        s    = Decimal(str(step))
        aligned = (q // s) * s
        return float(aligned.normalize()) if aligned > 0 else 0.0

    # ── Account / position queries ────────────────────────────────────────────

    def get_account_info(self) -> dict:
        return self._get("/fapi/v2/account", signed=True)

    def get_commission_rate(self) -> float:
        """Return the live futures taker commission rate from Binance account info.

        Falls back to the documented default (0.0004) if the API response does not
        contain the field or if the call fails.
        """
        try:
            info = self.get_account_info()
            rate = info.get("commissionRate")
            if rate is not None:
                return float(rate)
        except Exception as exc:
            print(f"[FuturesClient] commission rate fetch failed, using default: {exc}")
        return 0.0004  # Binance default futures taker rate

    def get_positions(self) -> List[dict]:
        """All open futures positions (non-zero positionAmt)."""
        account = self.get_account_info()
        return [p for p in account.get("positions", []) if float(p.get("positionAmt", 0)) != 0]

    def get_recent_trades(self, days: int = 3, limit: int = 50,
                          extra_symbols: List[str] = None) -> List[dict]:
        """
        Return recent closing trades from Binance userTrades (realizedPnl != 0).
        Queries tracked symbols + extra_symbols (e.g. futures_symbols from config).
        Used to populate the closed positions card in the dashboard.
        """
        if self.dry_run:
            return []
        cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
        symbols_to_check = set(extra_symbols or [])
        for pos in self._open_positions.values():
            symbols_to_check.add(pos.symbol)

        # Build entry_price lookup from in-memory open positions (available after sync)
        _entry_lookup: dict = {}
        for pos in self._open_positions.values():
            _entry_lookup[(pos.symbol, pos.side)] = {
                "entry_price": pos.entry_price,
                "sl_price":    pos.sl_price,
                "tp_price":    pos.tp_price,
            }

        results = []
        for sym in symbols_to_check:
            try:
                trades = self._get("/fapi/v1/userTrades", {
                    "symbol": sym, "startTime": str(cutoff_ms), "limit": str(limit)
                }, signed=True)
                # Group consecutive trades into open/close pairs by positionSide
                for t in trades:
                    side_raw = t.get("positionSide", "BOTH")
                    is_buyer = t.get("buyer", False)
                    qty      = float(t.get("qty", 0))
                    price    = float(t.get("price", 0))
                    pnl      = float(t.get("realizedPnl", 0))
                    ts_ms    = int(t.get("time", 0))
                    # Only include trades that closed a position (realizedPnl != 0)
                    if pnl == 0.0:
                        continue
                    side = side_raw if side_raw in ("LONG", "SHORT") else ("LONG" if is_buyer else "SHORT")
                    closed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

                    # Always derive entry_price mathematically from the trade itself.
                    # Never use _open_positions lookup: if the same symbol reopened
                    # after closing (e.g. SOL), the lookup returns the new position's
                    # entry price which equals the old exit price, giving wrong data.
                    # For SHORT: pnl = (entry - exit) * qty  →  entry = exit + pnl/qty
                    # For LONG:  pnl = (exit - entry) * qty  →  entry = exit - pnl/qty
                    entry_price = None
                    if qty > 0:
                        try:
                            if side == "SHORT":
                                entry_price = round(price + pnl / qty, 6)
                            else:
                                entry_price = round(price - pnl / qty, 6)
                        except Exception:
                            entry_price = None

                    # sl/tp from memory are reference-only (may belong to new open pos)
                    mem = _entry_lookup.get((sym, side), {})
                    sl_price = mem.get("sl_price")
                    tp_price = mem.get("tp_price")

                    results.append({
                        "symbol":      sym,
                        "side":        side,
                        "qty":         qty,
                        "entry_price": entry_price,
                        "exit_price":  price,
                        "sl_price":    sl_price,
                        "tp_price":    tp_price,
                        "pnl":         round(pnl, 4),
                        "opened_at":   None,
                        "closed_at":   closed_at,
                        "trade_type":  "futures",
                    })
            except Exception:
                pass

        results.sort(key=lambda x: x.get("closed_at", ""), reverse=True)
        return results[:limit]

    def enable_hedge_mode(self) -> bool:
        """
        Activate Binance hedge mode (dualSidePosition=true).
        Must be called before opening LONG positions to avoid netting.
        Returns True if already in hedge mode or successfully activated.
        Safe to call multiple times (idempotent).
        """
        if self.dry_run:
            print("[Futures DRY] Hedge mode enabled (simulated)")
            self._hedge_mode = True
            return True
        try:
            status = self._get("/fapi/v1/positionSide/dual", signed=True)
            if status.get("dualSidePosition") is True:
                self._hedge_mode = True
                print("[Futures] Hedge mode already active")
                return True
        except Exception as e:
            print(f"[Futures] Hedge mode check failed: {e}")
        try:
            self._post("/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})
            self._hedge_mode = True
            print("[Futures] Hedge mode activated (dualSidePosition=true)")
            return True
        except Exception as e:
            err = str(e)
            if "-4067" in err or "open orders" in err.lower():
                # Binance rejects mode change when algo orders (SL/TP) exist on any symbol.
                # Cancel all open algo orders first, then retry.
                print(f"[Futures] Hedge mode blocked by open orders (-4067) — attempting algo order cleanup")
                try:
                    # Cancel regular open orders per symbol
                    for _sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]:
                        try:
                            self._delete("/fapi/v1/allOpenOrders", {"symbol": _sym})
                        except Exception:
                            pass
                    # Cancel ALL open algo orders (SL/TP) — correct endpoint per Binance docs
                    try:
                        _algo_resp = self._get("/fapi/v1/openAlgoOrders", signed=True)
                        _algo_list = _algo_resp if isinstance(_algo_resp, list) else _algo_resp.get("orders", _algo_resp.get("algoOrders", []))
                        for _ao in _algo_list:
                            try:
                                self._delete("/fapi/v1/algoOrder", {"symbol": _ao.get("symbol"), "algoId": str(_ao.get("algoId"))})
                            except Exception:
                                pass
                        if _algo_list:
                            print(f"[Futures] Cleaned {len(_algo_list)} open algo orders")
                    except Exception as _ae:
                        print(f"[Futures] Algo order cleanup error: {_ae}")
                    self._post("/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})
                    self._hedge_mode = True
                    print("[Futures] Hedge mode activated after cleanup")
                    return True
                except Exception as e2:
                    print(f"[Futures] Hedge mode activation failed after cleanup: {e2} — running in one-way mode (LONG disabled)")
                    self._hedge_mode = False
                    return False
            print(f"[Futures] Hedge mode activation failed: {e}")
            self._hedge_mode = False
            return False

    def _consolidate_futures_open_records(self) -> None:
        """FIX-S7: Ensure trades_open exactly matches Binance positions.
        
        For each live Binance position, collapse any multiple DB rows
        (escalonamiento / stale partial fills) into a single row whose
        quantity and entry_price match Binance.  This keeps the in-memory
        position and the database in sync so PnL, SL/TP and dashboard
        entries are consistent with the exchange.
        """
        print(f"[Futures/Sync] _consolidate_futures_open_records called, db_client={self.db_client is not None}")
        if not self.db_client:
            return
        # Fetch all existing futures DB rows
        _all_db_rows = self.db_client.execute_query(
            """SELECT id, symbol, side, quantity, price, stop_loss, take_profit,
                      metadata, pnl_prediction, indicators_at_entry, entry_time
               FROM trades_open
               WHERE metadata->>'trade_type' = 'futures'
               ORDER BY entry_time ASC"""
        )
        # Group by symbol+side
        _db_groups: dict = {}
        for _r in _all_db_rows:
            _s = 'SHORT' if _r['side'] == 'SELL' else 'LONG'
            _k = self._pos_key(_r['symbol'], _s)
            _db_groups.setdefault(_k, []).append(_r)

        for _key, _pos in self._open_positions.items():
            if _pos.closed:
                continue
            _db_rows = _db_groups.get(_key, [])
            if not _db_rows:
                continue
            _db_total_qty = sum(float(_r['quantity']) for _r in _db_rows)
            _qty_match = abs(_db_total_qty - _pos.qty) < 1e-9
            if len(_db_rows) == 1 and _qty_match:
                continue  # already in sync

            # Use Binance-reported entry price as the source of truth for the consolidated row
            _avg_price = _pos.entry_price

            # Preserve newest metadata/prediction but keep oldest entry_time for real age tracking.
            _last = _db_rows[-1]
            _first = _db_rows[0]
            _meta = _last.get('metadata') or {}
            if isinstance(_meta, str):
                try:
                    _meta = json.loads(_meta)
                except Exception:
                    _meta = {}
            _meta['consolidated_at'] = datetime.now(timezone.utc).isoformat()
            _meta['consolidated_from'] = len(_db_rows)
            _meta['trade_type'] = 'futures'
            _first_entry_raw = _first.get('entry_time')
            _first_entry_iso = None
            try:
                _first_dt = _parse_utc_datetime(_first_entry_raw)
                if _first_dt:
                    _first_entry_iso = _first_dt.isoformat()
            except Exception:
                _first_entry_iso = None
            if _first_entry_iso:
                _meta['first_entry_time'] = _first_entry_iso

            # Insert consolidated row
            _pnl_pred = _last.get('pnl_prediction')
            _indicators = _last.get('indicators_at_entry')
            if isinstance(_pnl_pred, dict):
                _pnl_pred = json.dumps(_pnl_pred)
            if isinstance(_indicators, dict):
                _indicators = json.dumps(_indicators)
            self.db_client.execute_update(
                """INSERT INTO trades_open
                    (symbol, side, quantity, price, stop_loss, take_profit, metadata,
                     pnl_prediction, indicators_at_entry, entry_time)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    _pos.symbol,
                    'SELL' if _pos.side == 'SHORT' else 'BUY',
                    _pos.qty,
                    _avg_price,
                    _pos.sl_price,
                    _pos.tp_price,
                    json.dumps(_meta),
                    _pnl_pred,
                    _indicators,
                    _first_entry_raw or _last.get('entry_time'),
                )
            )
            # Delete old rows
            _old_ids = tuple(_r['id'] for _r in _db_rows)
            if len(_old_ids) == 1:
                self.db_client.execute_update("DELETE FROM trades_open WHERE id = %s", (_old_ids[0],))
            else:
                _placeholders = ','.join(['%s'] * len(_old_ids))
                self.db_client.execute_update(
                    f"DELETE FROM trades_open WHERE id IN ({_placeholders})", _old_ids
                )
            print(f"[Futures/Sync] Consolidated {_pos.symbol} {_pos.side}: "
                  f"{len(_db_rows)} rows → 1, qty={_pos.qty}, avg_price={_avg_price:.4f}")

    def _sync_open_positions(self) -> None:
        """Rebuild _open_positions from Binance on startup (survives restarts)."""
        try:
            positions = self.get_positions()
            # Fix 4: fetch all active algo orders once — used to restore real SL/TP per position
            _all_algo_orders: list = []
            try:
                _algo_resp = self._get("/fapi/v1/openAlgoOrders", signed=True)
                _all_algo_orders = _algo_resp if isinstance(_algo_resp, list) else _algo_resp.get("orders", _algo_resp.get("algoOrders", []))
            except Exception as _ae:
                print(f"[Futures/Sync] openAlgoOrders fetch failed ({_ae}) — SL/TP will use entry-based defaults")
            for p in positions:
                sym = p.get("symbol", "")
                amt = float(p.get("positionAmt", 0))
                entry = float(p.get("entryPrice", 0))
                if amt == 0 or entry <= 0:
                    continue
                # Determine side from positionAmt sign AND positionSide field
                ps = p.get("positionSide", "BOTH")
                if ps == "BOTH":
                    side = "SHORT" if amt < 0 else "LONG"
                else:
                    side = ps  # LONG or SHORT from hedge mode
                qty = abs(amt)
                tick = self._get_tick_size(sym)
                if side == "SHORT":
                    sl_price = self._align_price(entry * 1.03, tick)
                    tp_price = self._align_price(entry * 0.94, tick)
                else:  # LONG
                    sl_price = self._align_price(entry * 0.97, tick)
                    tp_price = self._align_price(entry * 1.06, tick)
                # Fix 4: restore real SL/TP from _all_algo_orders (fetched once above the loop)
                _sl_order_id = None
                _tp_order_id = None
                for _o in _all_algo_orders:
                    if _o.get("symbol") != sym:
                        continue
                    _o_pos_side = _o.get("positionSide", "BOTH")
                    _o_matches = (_o_pos_side == side) or (_o_pos_side == "BOTH")
                    if not _o_matches:
                        continue
                    _o_type = _o.get("type") or _o.get("orderType", "")
                    _trigger = float(_o.get("triggerPrice", 0))
                    if _trigger <= 0:
                        continue
                    if _o_type == "STOP_MARKET":
                        sl_price = _trigger
                        _sl_order_id = str(_o.get("algoId", _o.get("orderId", "")))
                    elif _o_type == "TAKE_PROFIT_MARKET":
                        tp_price = _trigger
                        _tp_order_id = str(_o.get("algoId", _o.get("orderId", "")))
                print(f"[Futures/Sync] {sym} {side}: SL={sl_price:.4f} TP={tp_price:.4f}"
                      f"{' (from algoOrders)' if _sl_order_id or _tp_order_id else ' (entry defaults)'}")
                pos = FuturesPosition(
                    symbol=sym, side=side, qty=qty,
                    entry_price=entry, sl_price=sl_price, tp_price=tp_price,
                    order_id=f"SYNC_{sym}_{side}", sl_order_id=_sl_order_id, tp_order_id=_tp_order_id,
                )
                key = self._pos_key(sym, side)
                self._open_positions[key] = pos
                print(f"[Futures] Synced position: {side} {sym} qty={qty} entry={entry:.4f} (SL={sl_price:.4f}, TP={tp_price:.4f})")

            # FIX-S7: Consolidate stale/escalamiento DB rows so trades_open matches Binance exactly
            if self.db_client:
                try:
                    self._consolidate_futures_open_records()
                except Exception as _cons_err:
                    print(f"[Futures/Sync] Consolidation error: {_cons_err}")

            # FIX-S2+S6: Restore _smart_meta from trades_open DB for each synced position
            # Uses entry_price matching to pick correct row when duplicates exist
            if self.db_client and self._open_positions:
                try:
                    _db_rows = self.db_client.execute_query(
                        """SELECT id, symbol, side, price, stop_loss, take_profit,
                                  metadata, pnl_prediction, indicators_at_entry, entry_time
                           FROM trades_open
                           WHERE metadata->>'trade_type' = 'futures'
                           ORDER BY entry_time DESC"""
                    )
                    # Build map: key → list of rows (handles duplicates)
                    _db_multi = {}
                    for _r in _db_rows:
                        _s = 'SHORT' if _r['side'] == 'SELL' else 'LONG'
                        _k = self._pos_key(_r['symbol'], _s)
                        _db_multi.setdefault(_k, []).append(_r)
                    # Pick best row per key: match by entry_price, fallback newest
                    _db_map = {}
                    for _k, _rows in _db_multi.items():
                        _pos = self._open_positions.get(_k)
                        if _pos:
                            _best = None
                            for _r in _rows:
                                _db_price = float(_r.get('price', 0))
                                if abs(_db_price - _pos.entry_price) < 0.01:
                                    _best = _r
                                    break
                            _db_map[_k] = _best or _rows[0]  # fallback: newest (ORDER BY DESC)
                        else:
                            _db_map[_k] = _rows[0]
                    for _k, _p in self._open_positions.items():
                        if _p.closed:
                            continue
                        _dbr = _db_map.get(_k)
                        if _dbr:
                            # Restore _smart_meta from DB metadata
                            _meta_raw = _normalize_metadata(_dbr.get('metadata'), default_trade_type='futures')
                            _saved_sm = _meta_raw.get('smart_meta')
                            if _saved_sm and isinstance(_saved_sm, dict):
                                _p._smart_meta = _saved_sm
                                print(f"[Futures/Sync] {_p.symbol} {_p.side}: _smart_meta restored from DB "
                                      f"(BE={_saved_sm.get('breakeven_done')}, trail={_saved_sm.get('trail_best_price')})")
                            # Restore pnl_prediction + indicators_at_entry
                            if _p.pnl_prediction is None and _dbr.get('pnl_prediction'):
                                _pp = _dbr['pnl_prediction']
                                if isinstance(_pp, str):
                                    try: _pp = json.loads(_pp)
                                    except Exception: _pp = None
                                _p.pnl_prediction = _pp
                            if _p.indicators_at_entry is None and _dbr.get('indicators_at_entry'):
                                _ie = _dbr['indicators_at_entry']
                                if isinstance(_ie, str):
                                    try: _ie = json.loads(_ie)
                                    except Exception: _ie = None
                                _p.indicators_at_entry = _ie
                            # Restore opened_at
                            _restored_opened_at = _parse_utc_datetime(_dbr.get('entry_time')) or _parse_utc_datetime(_meta_raw.get('opened_at'))
                            if _restored_opened_at is not None:
                                _p.opened_at = _restored_opened_at
                            elif _p.opened_at is None:
                                logger.warning("[Futures/Sync] %s %s: opened_at missing after DB restore", _p.symbol, _p.side)
                            # FIX-SL-RESTORE: Prefer software SL/TP from DB over Binance safety trigger
                            # Binance algo order triggerPrice is wider (safety margin), not software SL
                            _db_sl = _dbr.get('stop_loss')
                            _db_tp = _dbr.get('take_profit')
                            if _db_sl:
                                try:
                                    _db_sl_f = float(_db_sl)
                                    if _db_sl_f > 0:
                                        _p.sl_price = _db_sl_f
                                        print(f"[Futures/Sync] {_p.symbol} {_p.side}: SL restored from DB={_db_sl_f:.4f} (overrides Binance trigger)")
                                except Exception:
                                    pass
                            if _db_tp:
                                try:
                                    _db_tp_f = float(_db_tp)
                                    if _db_tp_f > 0:
                                        _p.tp_price = _db_tp_f
                                except Exception:
                                    pass
                except Exception as _dbe:
                    print(f"[Futures/Sync] _smart_meta DB restore failed: {_dbe}")

                # FIX-S2b: Infer trailing/breakeven state from SL position when DB has no smart_meta
                for _k, _p in self._open_positions.items():
                    if _p.closed or _p._smart_meta:
                        continue  # already restored or position closed
                    # Infer breakeven: if SL is at or beyond entry (protecting capital)
                    if _p.side == "SHORT" and _p.sl_price <= _p.entry_price:
                        _p._smart_meta['breakeven_done'] = True
                        print(f"[Futures/Sync] {_p.symbol} SHORT: breakeven inferred (SL={_p.sl_price:.4f} <= entry={_p.entry_price:.4f})")
                    elif _p.side == "LONG" and _p.sl_price >= _p.entry_price:
                        _p._smart_meta['breakeven_done'] = True
                        print(f"[Futures/Sync] {_p.symbol} LONG: breakeven inferred (SL={_p.sl_price:.4f} >= entry={_p.entry_price:.4f})")
                    # Infer trailing: if SL has moved significantly toward profit (beyond breakeven)
                    try:
                        _cur_price = self.get_price(_p.symbol)
                        _unr_sync_gross = _p.unrealized_pnl(_cur_price)
                        from binance_commission import net_pnl as _f_net_pnl_sync
                        _unr_sync = _f_net_pnl_sync(_unr_sync_gross, _p.qty, _p.entry_price, _cur_price, 'futures', self._cached_settings)
                        _pnl_pct_sync = _unr_sync / (_p.entry_price * _p.qty) if _p.entry_price > 0 and _p.qty > 0 else 0.0
                        if _pnl_pct_sync >= 0.015:  # 1.5% = trailing activation threshold (net)
                            # ATR-scaled trail distance for sync inference
                            _trail_dist = self.compute_trail_distance(_p.symbol, _pnl_pct_sync)
                            if _p.side == "SHORT":
                                # For SHORT, trailing best = SL / (1 + trail_dist)
                                _inferred_best = _p.sl_price / (1.0 + _trail_dist)
                                if _inferred_best < _p.entry_price:
                                    _p._smart_meta['trail_best_price'] = _inferred_best
                                    _p._smart_meta['breakeven_done'] = True
                                    print(f"[Futures/Sync] {_p.symbol} SHORT: trailing inferred best={_inferred_best:.4f} (PnL={_pnl_pct_sync*100:.2f}%, dist={_trail_dist*100:.2f}%)")
                                else:
                                    _p._smart_meta['trail_best_price'] = _cur_price
                                    print(f"[Futures/Sync] {_p.symbol} SHORT: trailing initialized at {_cur_price:.4f}")
                            elif _p.side == "LONG":
                                _denom = 1.0 - _trail_dist
                                _inferred_best = _p.sl_price / _denom if _denom > 0 else _cur_price
                                if _inferred_best > _p.entry_price:
                                    _p._smart_meta['trail_best_price'] = _inferred_best
                                    _p._smart_meta['breakeven_done'] = True
                                    print(f"[Futures/Sync] {_p.symbol} LONG: trailing inferred best={_inferred_best:.4f} (dist={_trail_dist*100:.2f}%)")
                                else:
                                    _p._smart_meta['trail_best_price'] = _cur_price
                                    print(f"[Futures/Sync] {_p.symbol} LONG: trailing initialized at {_cur_price:.4f}")
                    except Exception:
                        pass

            # FIX-S4: Ghost reconciliation — delete trades_open records with no matching Binance position
            if self.db_client:
                try:
                    _fut_db_rows = self.db_client.execute_query(
                        """SELECT id, symbol, side FROM trades_open
                           WHERE metadata->>'trade_type' = 'futures'"""
                    )
                    for _fdr in _fut_db_rows:
                        _fdr_side = 'SHORT' if _fdr['side'] == 'SELL' else 'LONG'
                        _fdr_key = self._pos_key(_fdr['symbol'], _fdr_side)
                        if _fdr_key not in self._open_positions:
                            self.db_client.execute_update(
                                "DELETE FROM trades_open WHERE id = %s", (_fdr['id'],)
                            )
                            print(f"[Futures/Sync] Ghost cleanup: deleted stale trades_open "
                                  f"id={_fdr['id']} {_fdr['symbol']} {_fdr['side']} (no Binance position)")
                except Exception as _gc_err:
                    print(f"[Futures/Sync] Ghost cleanup failed: {_gc_err}")

            # FIX-S5 (updated): Deduplicate trades_open — only delete truly identical rows
            # (price difference < 0.5%). Rows with >0.5% price difference are ALWAYS treated
            # as legitimate escalamientos even without the es_escalonamiento flag (legacy entries
            # opened before that field existed). NEVER delete rows with es_escalonamiento=True.
            if self.db_client and self._open_positions:
                try:
                    _dedup_rows = self.db_client.execute_query(
                        """SELECT id, symbol, side, price, metadata FROM trades_open
                           WHERE metadata->>'trade_type' = 'futures'
                           ORDER BY entry_time DESC"""
                    )
                    _dedup_groups = {}
                    for _dr in _dedup_rows:
                        _ds = 'SHORT' if _dr['side'] == 'SELL' else 'LONG'
                        _dk = self._pos_key(_dr['symbol'], _ds)
                        _dedup_groups.setdefault(_dk, []).append(_dr)
                    for _dk, _drows in _dedup_groups.items():
                        if len(_drows) <= 1:
                            continue
                        # Check if any rows are intentional escalamientos (flag present)
                        _has_escalamiento_flag = any(
                            (json.loads(_dr['metadata']) if isinstance(_dr.get('metadata'), str) else (_dr.get('metadata') or {})).get('es_escalonamiento')
                            or (json.loads(_dr['metadata']) if isinstance(_dr.get('metadata'), str) else (_dr.get('metadata') or {})).get('escalonamiento_index', 1) > 1
                            for _dr in _drows
                        )
                        if _has_escalamiento_flag:
                            print(f"[Futures/Sync] Dedup: SKIPPING {_drows[0]['symbol']} — has es_escalonamiento flag")
                            continue
                        # FIX-2: Also skip if all prices differ by >0.5% — legacy escalamientos without flag
                        _prices = [float(_dr['price']) for _dr in _drows if _dr.get('price')]
                        if len(_prices) >= 2:
                            _max_p, _min_p = max(_prices), min(_prices)
                            if _min_p > 0 and (_max_p - _min_p) / _min_p > 0.005:
                                print(f"[Futures/Sync] Dedup: SKIPPING {_drows[0]['symbol']} — price spread {(_max_p-_min_p)/_min_p*100:.2f}% > 0.5% (legacy escalamiento)")
                                continue
                        # Only deduplicate truly identical rows (price within 0.5%)
                        _pos = self._open_positions.get(_dk)
                        _keep_id = None
                        if _pos:
                            for _dr in _drows:
                                if abs(float(_dr['price']) - _pos.entry_price) < 0.01:
                                    _keep_id = _dr['id']
                                    break
                        if _keep_id is None:
                            _keep_id = _drows[0]['id']  # newest
                        for _dr in _drows:
                            if _dr['id'] != _keep_id:
                                self.db_client.execute_update(
                                    "DELETE FROM trades_open WHERE id = %s", (_dr['id'],)
                                )
                                print(f"[Futures/Sync] Dedup: deleted exact duplicate trades_open "
                                      f"id={_dr['id']} {_dr['symbol']} price={_dr['price']} "
                                      f"(keeping id={_keep_id})")
                except Exception as _dd_err:
                    print(f"[Futures/Sync] Dedup failed: {_dd_err}")

            # FIX-S4b: Ensure all synced positions have a trades_open DB record
            if self.db_client:
                try:
                    _existing_db = set()
                    _edb_rows = self.db_client.execute_query(
                        """SELECT symbol, side FROM trades_open
                           WHERE metadata->>'trade_type' = 'futures'"""
                    )
                    for _er in _edb_rows:
                        _er_side = 'SHORT' if _er['side'] == 'SELL' else 'LONG'
                        _existing_db.add(self._pos_key(_er['symbol'], _er_side))
                    for _k, _p in self._open_positions.items():
                        if _p.closed:
                            continue
                        if _k not in _existing_db:
                            self._save_futures_open(_p)
                            print(f"[Futures/Sync] Created missing trades_open record for {_p.symbol} {_p.side}")
                except Exception as _cr_err:
                    print(f"[Futures/Sync] Create missing records failed: {_cr_err}")

            # FIX-5: Retro-tag bounce_entry on eligible positions at startup
            # Any position already >= min_hours old with peak < threshold gets tagged immediately
            # so SSO-6 can evaluate it on the very first cycle after restart
            try:
                _settings = getattr(self, '_cached_settings', None)  # set by check_positions if called first; None is fine
                _br_max_peak = getattr(_settings, 'futures_bounce_rescue_max_peak_pct', 0.5) / 100.0 if _settings else 0.005
                _br_min_h   = getattr(_settings, 'futures_bounce_rescue_min_hours', 3.0) if _settings else 3.0
                _br_enabled = getattr(_settings, 'futures_bounce_rescue_enabled', True) if _settings else True
                if _br_enabled:
                    _retag_count = 0
                    for _k, _p in self._open_positions.items():
                        if _p.closed or _p._smart_meta.get('bounce_entry'):
                            continue
                        if not _p.opened_at:
                            continue
                        try:
                            _age_h = (datetime.now(timezone.utc) - _p.opened_at).total_seconds() / 3600
                            _peak  = _p._smart_meta.get('peak_pnl_pct', 0.0) or 0.0
                            if _age_h >= _br_min_h and _peak < _br_max_peak:
                                _p._smart_meta['bounce_entry'] = True
                                self._persist_smart_meta(_p)
                                print(f"[SSO-6/Startup] {_p.symbol} {_p.side}: RETRO-TAGGED bounce_entry "
                                      f"(peak={_peak*100:.2f}%<{_br_max_peak*100:.1f}%, age={_age_h:.1f}h)")
                                _retag_count += 1
                        except Exception:
                            pass
                    if _retag_count:
                        print(f"[SSO-6/Startup] Retro-tagged {_retag_count} position(s) as bounce_entry")
            except Exception as _rtag_err:
                print(f"[SSO-6/Startup] Retro-tag failed: {_rtag_err}")

            if self._open_positions:
                print(f"[Futures] FuturesClient initialized — synced {len(self._open_positions)} open position(s)")
            else:
                print(f"[Futures] FuturesClient initialized")
        except Exception as e:
            print(f"[Futures] FuturesClient initialized (sync failed: {e})")

    def count_open_shorts(self) -> int:
        try:
            positions = self.get_positions()
            return sum(1 for p in positions if float(p.get("positionAmt", 0)) < 0)
        except Exception:
            return sum(1 for p in self._open_positions.values() if not p.closed and p.side == "SHORT")

    def count_open_longs(self) -> int:
        try:
            positions = self.get_positions()
            return sum(1 for p in positions
                       if float(p.get("positionAmt", 0)) > 0
                       and p.get("positionSide", "BOTH") in ("LONG", "BOTH"))
        except Exception:
            return sum(1 for p in self._open_positions.values() if not p.closed and p.side == "LONG")

    def get_available_usdt(self) -> float:
        try:
            account = self.get_account_info()
            for asset in account.get("assets", []):
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
        except Exception:
            pass
        return 0.0

    def get_total_wallet_usdt(self) -> float:
        """Return total USDT-equivalent value of all assets in the futures wallet.
        Converts non-USDT assets (e.g. BNB used as margin) to USDT using live price.
        Uses walletBalance which includes committed margin, unlike availableBalance."""
        try:
            account = self.get_account_info()
            total = 0.0
            for asset in account.get("assets", []):
                wallet_bal = float(asset.get("walletBalance", 0))
                if wallet_bal <= 0:
                    continue
                asset_name = asset.get("asset", "")
                if asset_name == "USDT":
                    total += wallet_bal
                else:
                    try:
                        price = self.get_price(f"{asset_name}USDT")
                        total += wallet_bal * price
                    except Exception:
                        pass
            return total
        except Exception:
            pass
        return 0.0

    def transfer_to_futures(self, amount: float, spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Transfer USDT from Spot wallet to Futures wallet.
        Uses /sapi/v1/futures/transfer (type=1 = SPOT→USDT-M Futures).
        Requires 'Universal Transfer' permission on the API key.
        Returns True on success.
        """
        _key    = spot_api_key    or self.api_key
        _secret = spot_api_secret or self.api_secret
        if self.dry_run:
            print(f"[Futures DRY] Transfer ${amount:.2f} USDT spot→futures")
            return True
        try:
            params = {
                "asset":     "USDT",
                "amount":    str(round(amount, 4)),
                "type":      "1",           # 1 = SPOT → USDT-M Futures
                "timestamp": int(time.time() * 1000),
            }
            query = urllib.parse.urlencode(params)
            sig   = hmac.new(_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url   = f"https://api.binance.com/sapi/v1/futures/transfer?{query}&signature={sig}"
            req   = urllib.request.Request(
                url, data=b"", method="POST",
                headers={"X-MBX-APIKEY": _key, "Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                result = json.loads(r.read())
            if result.get("tranId"):
                print(f"[Futures] Transferred ${amount:.2f} USDT spot→futures (tranId={result['tranId']})")
                return True
            print(f"[Futures] Transfer failed: {result}")
            return False
        except Exception as e:
            print(f"[Futures] transfer_to_futures error: {e}")
            return False

    def transfer_to_spot(self, amount: float, spot_api_key: str = None, spot_api_secret: str = None,
                          safety_buffer_usdt: float = 10.0) -> bool:
        """
        Transfer USDT from Futures wallet back to Spot wallet.
        Uses /sapi/v1/futures/transfer (type=2 = USDT-M Futures→SPOT).
        Only transfers if the futures wallet has enough available balance after keeping a safety buffer.
        Returns True on success.
        """
        _key    = spot_api_key    or self.api_key
        _secret = spot_api_secret or self.api_secret
        if self.dry_run:
            print(f"[Futures DRY] Transfer ${amount:.2f} USDT futures→spot")
            return True
        try:
            # Cap against available USDT balance (walletBalance includes non-USDT assets like BNB
            # which cannot be transferred directly as USDT via this endpoint).
            available = self.get_available_usdt()
            max_transfer = max(0.0, available - safety_buffer_usdt)
            if amount <= 0:
                print(f"[Futures] Nothing to transfer to spot (amount={amount:.2f})")
                return False
            if amount > max_transfer:
                print(f"[Futures] Transfer capped: requested ${amount:.2f} > max safe ${max_transfer:.2f} (available_usdt=${available:.2f}, buffer={safety_buffer_usdt:.2f})")
                amount = max_transfer
            if amount < 1.0:
                print(f"[Futures] Transfer amount too small (${amount:.2f}), skipping")
                return False
            params = {
                "asset":     "USDT",
                "amount":    str(round(amount, 4)),
                "type":      "2",           # 2 = USDT-M Futures → SPOT
                "timestamp": int(time.time() * 1000),
            }
            query = urllib.parse.urlencode(params)
            sig   = hmac.new(_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            url   = f"https://api.binance.com/sapi/v1/futures/transfer?{query}&signature={sig}"
            req   = urllib.request.Request(
                url, data=b"", method="POST",
                headers={"X-MBX-APIKEY": _key, "Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                result = json.loads(r.read())
            if result.get("tranId"):
                print(f"[Futures] Transferred ${amount:.2f} USDT futures→spot (tranId={result['tranId']})")
                return True
            print(f"[Futures] Transfer futures→spot failed: {result}")
            return False
        except Exception as e:
            print(f"[Futures] transfer_to_spot error: {e}")
            return False

    def return_idle_funds_to_spot(self, reserve_usdt: float = 5.0,
                                  spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Return excess futures balance to spot when no positions are open.
        Keeps `reserve_usdt` in futures as buffer; transfers the rest.
        Returns True if transfer executed or not needed.
        """
        if self._open_positions:
            print(f"[Futures] {len(self._open_positions)} open positions — not returning funds to spot")
            return False
        available = self.get_available_usdt()
        transferable = available - reserve_usdt
        if transferable < 1.0:
            print(f"[Futures] Balance ${available:.2f} too low to return (reserve=${reserve_usdt:.2f})")
            return False
        print(f"[Futures] Returning ${transferable:.2f} USDT futures→spot (keeping ${reserve_usdt:.2f} reserve)")
        return self.transfer_to_spot(transferable, spot_api_key, spot_api_secret)

    def check_and_fund(self, required_usdt: float, spot_api_key: str = None, spot_api_secret: str = None) -> bool:
        """
        Check futures balance and auto-transfer from spot if needed.
        Only transfers if futures balance < required_usdt.
        Transfers shortfall + 10% buffer (not 2×) to avoid over-draining spot.
        Returns True if futures wallet has sufficient funds after check.
        """
        available = self.get_available_usdt()
        if available >= required_usdt:
            print(f"[Futures] Wallet funded: ${available:.2f} USDT available (need ${required_usdt:.2f})")
            return True
        shortfall = required_usdt - available
        # Transfer shortfall + 10% buffer (enough for this position, not more)
        transfer_amount = round(shortfall * 1.1, 2)
        print(f"[Futures] Insufficient balance: ${available:.2f} < ${required_usdt:.2f} — transferring ${transfer_amount:.2f} from spot (shortfall=${shortfall:.2f})")
        ok = self.transfer_to_futures(transfer_amount, spot_api_key, spot_api_secret)
        if ok:
            # Small delay for balance to reflect
            time.sleep(1.5)
            new_balance = self.get_available_usdt()
            print(f"[Futures] Post-transfer balance: ${new_balance:.2f} USDT")
            return new_balance >= required_usdt
        return False

    def smart_rebalance_capital(
        self,
        spot_usdt: float,
        open_shorts: int,
        max_shorts: int,
        kelly_margin: float,
        open_spot_count: int,
        kelly_spot_amount: float,
        regime: str,
        spot_api_key: str = None,
        spot_api_secret: str = None,
        min_transfer: float = 10.0,
        futures_arbiter_active: bool = False,
    ) -> dict:
        """
        Bidirectional autonomous capital bridge: spot ↔ futures.
        Evaluates both wallets every cycle and transfers exactly what is needed
        to avoid missing any trading opportunity in either direction.

        Returns dict with keys: direction ('spot->futures'|'futures->spot'|'none'),
        amount, reason, success (bool), error (str|None).
        """
        result = {"direction": "none", "amount": 0.0, "reason": "", "success": True, "error": None}
        try:
            # s77b: distinguish available (free margin) from total wallet balance.
            # Case 1 (spot→futures shortfall) uses available; Cases 2/3 (futures→spot)
            # use total so idle capital is correctly detected and returned.
            futures_available = self.get_available_usdt()
            futures_total = self.get_total_wallet_usdt()
            # Fallback: if total unavailable, use available as conservative estimate
            if futures_total <= 0:
                futures_total = futures_available

            # ── Compute committed margin (margin locked in open shorts) ──────────
            committed_margin = 0.0
            for pos in self._open_positions.values():
                if not pos.closed and pos.side == "SHORT":
                    try:
                        committed_margin += pos.qty * pos.entry_price / self._active_leverage
                    except Exception:
                        pass
            futures_min_reserve = max(committed_margin * 1.1, 5.0)

            # ── Compute spot minimum reserve (protect existing spot positions) ──
            spot_min_reserve = max(100.0, open_spot_count * kelly_spot_amount * 0.5)

            # ── Case 1: Futures needs capital (FUTURES_SHORT, open slots exist) ─
            if regime == "FUTURES_SHORT":
                futures_usdt = futures_available  # use available for shortfall check
                open_slots = max(0, max_shorts - open_shorts)
                if open_slots > 0:
                    needed_futures = open_slots * kelly_margin
                    shortfall = needed_futures - futures_usdt
                    if shortfall >= min_transfer:
                        transferable_from_spot = max(0.0, spot_usdt - spot_min_reserve)
                        transfer_amount = round(min(shortfall, transferable_from_spot), 2)
                        if transfer_amount >= min_transfer:
                            reason = (f"FUTURES_SHORT: {open_slots} open slot(s) × ${kelly_margin:.0f}/slot "
                                      f"= need ${needed_futures:.0f}, have ${futures_usdt:.0f} "
                                      f"→ transfer ${transfer_amount:.2f} spot→futures")
                            print(f"[CapBridge] {reason}")
                            ok = self.transfer_to_futures(transfer_amount, spot_api_key, spot_api_secret)
                            result["direction"] = "spot->futures"
                            result["amount"] = transfer_amount
                            result["reason"] = reason
                            result["success"] = ok
                            if ok:
                                time.sleep(1.0)
                                new_bal = self.get_available_usdt()
                                print(f"[CapBridge] Post-transfer futures balance: ${new_bal:.2f} USDT")
                            else:
                                err_msg = "transfer_to_futures returned False"
                                result["error"] = err_msg
                                print(f"[CapBridge] ERROR: spot→futures transfer failed — {err_msg}")
                            return result
                        else:
                            print(f"[CapBridge] spot→futures: need ${shortfall:.2f} but only ${transferable_from_spot:.2f} "
                                  f"transferable (spot_min_reserve=${spot_min_reserve:.0f}) — skipping")
                    else:
                        if shortfall > 0:
                            print(f"[CapBridge] futures shortfall ${shortfall:.2f} < min_transfer ${min_transfer:.0f} — skipping")

            # ── Case 1b: Arbiter active in RANGE_SCALP — ensure futures has min 1 kelly_margin ─
            elif futures_arbiter_active and regime in ("RANGE_SCALP", "SPOT_BULL"):
                futures_usdt = futures_available
                # Ensure futures wallet can open at least one position
                needed_futures = kelly_margin
                shortfall = needed_futures - futures_usdt
                if shortfall >= min_transfer:
                    transferable_from_spot = max(0.0, spot_usdt - spot_min_reserve)
                    transfer_amount = round(min(shortfall, transferable_from_spot), 2)
                    if transfer_amount >= min_transfer:
                        reason = (f"ARBITER_ACTIVE ({regime}): futures need ${needed_futures:.0f} "
                                  f"for 1 position, have ${futures_usdt:.0f} "
                                  f"→ transfer ${transfer_amount:.2f} spot→futures")
                        print(f"[CapBridge] {reason}")
                        ok = self.transfer_to_futures(transfer_amount, spot_api_key, spot_api_secret)
                        result["direction"] = "spot->futures"
                        result["amount"] = transfer_amount
                        result["reason"] = reason
                        result["success"] = ok
                        if ok:
                            time.sleep(1.0)
                            new_bal = self.get_available_usdt()
                            print(f"[CapBridge] Post-transfer futures balance: ${new_bal:.2f} USDT")
                        else:
                            err_msg = "transfer_to_futures returned False (arbiter)"
                            result["error"] = err_msg
                            print(f"[CapBridge] ERROR: spot→futures transfer failed — {err_msg}")
                        return result

            # ── Case 2: Spot needs capital (non-bear regime, spot has open slots) ─
            elif regime in ("SPOT_BULL", "RANGE_SCALP"):
                from config import get_settings as _gs
                from config_helpers import get_param
                try:
                    _s = _gs()
                    _max_spot = get_param(_s, 'max_positions')
                except Exception:
                    _max_spot = 12
                spot_open_slots = max(0, _max_spot - open_spot_count)
                if spot_open_slots > 0:
                    needed_spot = spot_open_slots * kelly_spot_amount
                    spot_shortfall = needed_spot - spot_usdt
                    if spot_shortfall >= min_transfer:
                        # s77b: use available USDT (not total which includes BNB) for actual transfer cap
                        futures_excess = max(0.0, futures_available - futures_min_reserve)
                        transfer_amount = round(min(spot_shortfall, futures_excess), 2)
                        if transfer_amount >= min_transfer:
                            reason = (f"{regime}: {spot_open_slots} spot slot(s) × ${kelly_spot_amount:.0f}/slot "
                                      f"= need ${needed_spot:.0f}, have ${spot_usdt:.0f} "
                                      f"→ transfer ${transfer_amount:.2f} futures→spot")
                            print(f"[CapBridge] {reason}")
                            ok = self.transfer_to_spot(transfer_amount, spot_api_key, spot_api_secret)
                            result["direction"] = "futures->spot"
                            result["amount"] = transfer_amount
                            result["reason"] = reason
                            result["success"] = ok
                            if ok:
                                time.sleep(1.0)
                                print(f"[CapBridge] Post-transfer: spot USDT ~${spot_usdt + transfer_amount:.2f}")
                            else:
                                err_msg = "transfer_to_spot returned False"
                                result["error"] = err_msg
                                print(f"[CapBridge] ERROR: futures→spot transfer failed — {err_msg}")
                            return result

            # ── Case 3: Idle futures capital — no open shorts, return excess ────
            # Skip if arbiter is active — futures capital is needed for FUTURES_LONG/SHORT decisions
            if open_shorts == 0 and regime not in ("FUTURES_SHORT", "PRESERVATION") and not futures_arbiter_active:
                # s77b: only transfer available USDT — futures_total includes non-USDT (e.g. BNB) which
                # cannot be sent via /sapi/v1/futures/transfer. Non-USDT assets must be handled separately.
                excess = futures_available - futures_min_reserve
                if excess >= min_transfer:
                    reason = f"Idle futures capital ${excess:.2f} (no open shorts, regime={regime}) → returning to spot"
                    print(f"[CapBridge] {reason}")
                    ok = self.transfer_to_spot(round(excess, 2), spot_api_key, spot_api_secret)
                    result["direction"] = "futures->spot"
                    result["amount"] = round(excess, 2)
                    result["reason"] = reason
                    result["success"] = ok
                    if not ok:
                        err_msg = "transfer_to_spot returned False (idle return)"
                        result["error"] = err_msg
                        print(f"[CapBridge] ERROR: idle futures return failed — {err_msg}")
                    return result

            if result["direction"] == "none":
                print(f"[CapBridge] No transfer needed — futures_avail=${futures_available:.2f} total=${futures_total:.2f} "
                      f"(non-USDT assets={futures_total-futures_available:.2f}), "
                      f"spot=${spot_usdt:.2f}, regime={regime}, open_shorts={open_shorts}/{max_shorts}")

        except Exception as e:
            result["success"] = False
            result["error"] = str(e)
            print(f"[CapBridge] ERROR: unexpected exception in smart_rebalance_capital: {e}")

        return result

    # ── Leverage management ───────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int = None) -> bool:
        """Set leverage for symbol. Capped at the configured active leverage (settings.futures_leverage)."""
        leverage = min(leverage or self._active_leverage, self._active_leverage)
        if symbol in self._leverage_set:
            return True
        if self.dry_run:
            print(f"[Futures DRY] Set leverage {symbol} × {leverage}")
            self._leverage_set.add(symbol)
            return True
        try:
            self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
            self._leverage_set.add(symbol)
            print(f"[Futures] Leverage set: {symbol} × {leverage}")
            return True
        except Exception as e:
            print(f"[Futures] Set leverage failed {symbol}: {e}")
            return False

    def set_isolated_margin(self, symbol: str) -> bool:
        """Set margin type to ISOLATED for controlled risk."""
        if self.dry_run:
            return True
        try:
            self._post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
            return True
        except Exception as e:
            err = str(e)
            if ("No need to change margin type" in err or "-4046" in err
                    or "already" in err.lower() or "-4047" in err):
                return True  # already isolated, or open orders block change — not an error
            print(f"[Futures] Set isolated margin failed {symbol}: {e}")
            return False

    # ── Pre-entry safety checks ───────────────────────────────────────────────

    def _pre_entry_checks(self, symbol: str, usdt_amount: float, side: str = "SHORT") -> Tuple[bool, str]:
        """Run all safety checks before opening a position. Returns (ok, reason)."""
        side = side.upper()

        # 1. Max open positions per side
        if side == "SHORT":
            n = self.count_open_shorts()
            if n >= self.max_shorts:
                return False, f"MAX_SHORTS reached ({n}/{self.max_shorts})"
        else:  # LONG
            n = self.count_open_longs()
            if n >= self.max_longs:
                return False, f"MAX_LONGS reached ({n}/{self.max_longs})"

        # 2. Already have same-side position on this symbol
        key = self._pos_key(symbol, side)
        if key in self._open_positions and not self._open_positions[key].closed:
            return False, f"Already have open {side} on {symbol}"

        # 2b. LARGE_CAP category cap — prevents opening >max_largecap_shorts correlated LARGE_CAPs simultaneously
        if side == "SHORT" and symbol in self.largecap_symbols:
            n_lc = sum(1 for k, p in self._open_positions.items()
                       if not p.closed and p.side == "SHORT" and p.symbol in self.largecap_symbols)
            if n_lc >= self.max_largecap_shorts:
                return False, (f"LARGECAP_CAP: {n_lc}/{self.max_largecap_shorts} large-cap shorts open "
                               f"({', '.join(p.symbol for p in self._open_positions.values() if not p.closed and p.side=='SHORT' and p.symbol in self.largecap_symbols)}) "
                               f"— prefer MID-CAP slot for {symbol}")

        # 3. Funding rate check (inverted logic for LONG vs SHORT)
        funding = self.get_funding_rate(symbol)
        if side == "SHORT" and funding > FUNDING_LIMIT:
            return False, f"Funding rate {funding:.4%} > {FUNDING_LIMIT:.4%} — skip (overleveraged longs)"
        # For LONG: very negative funding = shorts very apalancados = dangerous for longs
        if side == "LONG" and funding < -FUNDING_LIMIT:
            return False, f"Funding rate {funding:.4%} < -{FUNDING_LIMIT:.4%} — skip (overleveraged shorts)"

        # 4. Available margin
        if not self.dry_run:
            available = self.get_available_usdt()
            if available < usdt_amount:
                return False, f"Insufficient futures margin: ${available:.2f} < ${usdt_amount:.2f}"

        return True, ""

    # ── Position management ───────────────────────────────────────────────────

    def open_short(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 3.0,   # % above entry — stop loss
        tp_pct: float = 7.0,   # % below entry — take profit
        leverage: int = None,
        pnl_prediction: Optional[Dict] = None,      # FIX-P2: predictor output at entry
        indicators_at_entry: Optional[Dict] = None, # FIX-P2: technical indicators at entry
        signal_id: Optional[str] = None,
        peak_quality_profile: Optional[str] = None,
        peak_quality_score: Optional[float] = None,
    ) -> Optional[FuturesPosition]:
        """
        Open a SHORT position with mandatory SL and TP orders.
        Returns FuturesPosition or None if safety checks fail.

        Risk math at 2x leverage:
          SL 3% move → 6% loss on margin
          TP 7% move → 14% gain on margin
        """
        ok, reason = self._pre_entry_checks(symbol, usdt_amount, side="SHORT")
        if not ok:
            print(f"[Futures] SHORT {symbol} blocked: {reason}")
            return None

        # Configure leverage + isolated margin
        self.set_isolated_margin(symbol)
        self.set_leverage(symbol, min(leverage, self._active_leverage))

        price    = self.get_price(symbol)
        notional = usdt_amount * min(leverage, self._active_leverage)  # leveraged notional
        step     = self._get_step_size(symbol)
        qty      = self._align_qty(notional / price, step)

        if qty <= 0:
            print(f"[Futures] SHORT {symbol}: qty=0 after alignment (usdt={usdt_amount}, price={price})")
            return None

        actual_notional = qty * price
        if actual_notional < 10.0:
            print(
                f"[Futures] SHORT {symbol}: notional=${actual_notional:.4f} < $10.0 minimum "
                f"(qty={qty}, price={price:.6f}) — rejected"
            )
            return None

        # USD loss guard: cap effective stop-loss based on nominal USD loss per trade
        _fut_settings = getattr(self, 'settings', None) or getattr(self, '_cached_settings', None)
        _max_loss_usd = float(getattr(_fut_settings, 'max_loss_usd_per_trade', 18.0) or 0.0) if _fut_settings else 18.0
        if _max_loss_usd > 0 and actual_notional > 0:
            _usd_sl_pct = _max_loss_usd / actual_notional * 100.0
            sl_pct = min(sl_pct, _usd_sl_pct)
            print(f"[Futures/USDGuard] SHORT {symbol}: SL% capped to {sl_pct:.3f}% (${_max_loss_usd:.2f} / ${actual_notional:.2f})")

        tick = self._get_tick_size(symbol)
        sl_price = self._align_price(price * (1 + sl_pct / 100), tick)
        tp_price = self._align_price(price * (1 - tp_pct / 100), tick)

        print(f"[Futures] Opening SHORT {symbol}: qty={qty}, entry~{price:.4f}, "
              f"SL={sl_price:.4f} (+{sl_pct}%), TP={tp_price:.4f} (-{tp_pct}%), "
              f"leverage={leverage}×, notional=${notional:.2f}")

        if self.dry_run:
            pos = FuturesPosition(
                symbol=symbol, side="SHORT", qty=qty,
                entry_price=price, sl_price=sl_price, tp_price=tp_price,
                order_id=f"DRY_{symbol}_{int(time.time())}",
                sl_order_id=None, tp_order_id=None,
            )
            self._open_positions[self._pos_key(symbol, "SHORT")] = pos
            return pos

        # ── Live order execution ──────────────────────────────────────────────
        try:
            # 1. Market SHORT order (hedge mode requires positionSide=SHORT)
            entry_order = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": str(qty), "positionSide": "SHORT",
            })
            if entry_order.get("status") not in ("FILLED", "NEW", None):
                print(f"[Futures/WARN] SHORT {symbol}: entry order status={entry_order.get('status')} — may not have filled")
            order_id    = str(entry_order.get("orderId", "?"))
            filled_price = float(entry_order.get("avgPrice", price)) or price

            # Recalculate SL/TP based on actual fill price
            # FIX-T2A: software SL = tight (3%), Binance safety SL = wide (4.5%)
            sl_price = round(filled_price * (1 + sl_pct / 100), 6)  # software SL (used by is_sl_hit)
            tp_price = round(filled_price * (1 - tp_pct / 100), 6)
            _binance_sl_pct = sl_pct * 1.5  # safety net: 50% wider than software SL

            # 2. Stop-loss order via Algo Orders API (required since Binance API change 2025-12-09)
            # POST /fapi/v1/algoOrder with algoType=CONDITIONAL + orderType=STOP_MARKET
            # For SHORT: BUY STOP_MARKET triggers when price rises to triggerPrice
            # FIX-T2A: Binance trigger uses wider _binance_sl_pct as safety net
            sl_order_id = None
            for _sl_attempt in range(3):
                try:
                    _raw_trigger = self.get_price(symbol) * (1 + _binance_sl_pct / 100) if _sl_attempt > 0 else filled_price * (1 + _binance_sl_pct / 100)
                    _sl_trigger = self._align_price(_raw_trigger, tick)
                    sl_order = self._post("/fapi/v1/algoOrder", {
                        "symbol": symbol, "side": "BUY",
                        "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                        "quantity": str(qty), "triggerPrice": str(_sl_trigger),
                        "positionSide": "SHORT", "timeInForce": "GTC",
                    })
                    sl_order_id = str(sl_order.get("algoId", sl_order.get("orderId", "")))
                    # FIX-T2A: pos.sl_price stays at software level, NOT Binance safety trigger
                    print(f"[Futures] SL algo order placed: triggerPrice={_sl_trigger} (safety), software SL={sl_price} (algoId={sl_order_id})")
                    break
                except Exception as se:
                    if _sl_attempt < 2:
                        print(f"[Futures] SL order attempt {_sl_attempt + 1} failed for {symbol}: {se} — retrying with refreshed price")
                        time.sleep(0.5)
                    else:
                        print(f"[Futures] SL order failed for {symbol}: {se} — position open WITHOUT SL, closing immediately")
                        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_CRITICAL,
                                     f"Futures SHORT {symbol} SL placement failed after 3 attempts: {se}")
                        self._post("/fapi/v1/order", {
                            "symbol": symbol, "side": "BUY", "type": "MARKET",
                            "quantity": str(qty), "positionSide": "SHORT",
                        })
                        return None

            # 3. Take-profit order via Algo Orders API
            # POST /fapi/v1/algoOrder with algoType=CONDITIONAL + orderType=TAKE_PROFIT_MARKET
            # For SHORT: BUY TAKE_PROFIT_MARKET triggers when price falls to triggerPrice
            tp_order_id = None
            try:
                tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "BUY",
                    "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(qty), "triggerPrice": str(self._align_price(tp_price, tick)),
                    "positionSide": "SHORT", "timeInForce": "GTC",
                })
                tp_order_id = str(tp_order.get("algoId", tp_order.get("orderId", "")))
                print(f"[Futures] TP algo order placed: triggerPrice={tp_price:.4f} (algoId={tp_order_id})")
            except Exception as te:
                print(f"[Futures] TP order failed for {symbol}: {te} — continuing with SL only")

            pos = FuturesPosition(
                symbol=symbol, side="SHORT", qty=qty,
                entry_price=filled_price, sl_price=sl_price, tp_price=tp_price,
                order_id=order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
                pnl_prediction=pnl_prediction,
                indicators_at_entry=indicators_at_entry,
            )
            pos._smart_meta['fut_total_at_open'] = self.get_total_wallet_usdt()  # SSO gate anchor
            try:
                pos._smart_meta['btc_price_at_entry'] = self.get_price('BTCUSDT')  # SSO-6: BTC ref for bounce detection
            except Exception:
                pass
            # FIX-6: trazabilidad — registrar datos del fill real de Binance
            pos._smart_meta['entry_order_id'] = order_id
            pos._smart_meta['entry_filled_qty'] = float(entry_order.get('executedQty', qty))
            pos._smart_meta['entry_avg_price'] = filled_price
            _entry_confirmed = self._verify_entry_filled(symbol, "SHORT")
            pos._smart_meta['binance_entry_confirmed'] = _entry_confirmed
            if not _entry_confirmed:
                self._audit(symbol, 'entry_not_confirmed_on_binance', {
                    'side': 'SHORT', 'order_id': order_id, 'status': entry_order.get('status'),
                    'qty': qty, 'filled_price': filled_price,
                })
                print(f"[Futures/WARN] SHORT {symbol}: entry not confirmed on Binance — aborting position record")
                try:
                    self._post("/fapi/v1/order", {
                        "symbol": symbol, "side": "BUY", "type": "MARKET",
                        "quantity": str(qty), "positionSide": "SHORT",
                    })
                except Exception as _ce:
                    print(f"[Futures/WARN] SHORT {symbol}: cleanup close also failed: {_ce}")
                return None
            pos._smart_meta['regime_at_entry'] = str(getattr(_fut_settings, 'current_regime_mode', '') or '').strip().upper() or None
            pos._smart_meta['profile_at_entry'] = str(peak_quality_profile or 'B').strip().upper() or None
            pos._smart_meta['peak_quality_profile'] = str(peak_quality_profile or '').strip().upper() or None
            if peak_quality_score is not None:
                try:
                    pos._smart_meta['peak_quality'] = float(peak_quality_score)
                except Exception:
                    pass
            if signal_id:
                pos._smart_meta['signal_id'] = signal_id
            # USD loss guard audit fields
            pos._smart_meta['max_loss_usd_per_trade'] = _max_loss_usd
            pos._smart_meta['effective_sl_usd'] = min(_max_loss_usd, actual_notional * 0.95) if _max_loss_usd > 0 else None
            pos._smart_meta['effective_sl_price'] = sl_price
            pos._smart_meta['sl_source'] = 'usd_cap' if (_max_loss_usd > 0 and actual_notional > 0 and sl_pct < (_max_loss_usd / actual_notional * 100.0) * (1 + 1e-12)) else 'category_pct'
            self._open_positions[self._pos_key(symbol, "SHORT")] = pos
            self._save_futures_open(pos)  # FIX-G+P2: persist to DB with prediction
            print(f"[Futures] SHORT {symbol} OPEN: entry={filled_price:.4f}, "
                  f"SL={sl_price:.4f}, TP={tp_price:.4f}")
            return pos

        except Exception as e:
            self._last_error = str(e)  # auto-learn: lets run_trades detect -1121
            print(f"[Futures] Failed to open SHORT {symbol}: {e}")
            return None

    def open_long(
        self,
        symbol: str,
        usdt_amount: float,
        sl_pct: float = 2.5,   # % below entry — stop loss
        tp_pct: float = 5.0,   # % above entry — take profit
        leverage: int = None,
        signal_id: Optional[str] = None,
        peak_quality_profile: Optional[str] = None,
        peak_quality_score: Optional[float] = None,
    ) -> Optional[FuturesPosition]:
        """
        Open a LONG position with mandatory SL and TP orders.
        Requires hedge mode enabled (enable_hedge_mode() called first).

        Risk math at 2x leverage:
          SL 2.5% move down → 5% loss on margin
          TP 5.0% move up   → 10% gain on margin
        """
        if not self._hedge_mode:
            if not self.enable_hedge_mode():
                print(f"[Futures] LONG {symbol} blocked: hedge mode not available")
                return None

        ok, reason = self._pre_entry_checks(symbol, usdt_amount, side="LONG")
        if not ok:
            print(f"[Futures] LONG {symbol} blocked: {reason}")
            return None

        self.set_isolated_margin(symbol)
        self.set_leverage(symbol, min(leverage, self._active_leverage))

        price    = self.get_price(symbol)
        notional = usdt_amount * min(leverage, self._active_leverage)
        step     = self._get_step_size(symbol)
        qty      = self._align_qty(notional / price, step)

        if qty <= 0:
            print(f"[Futures] LONG {symbol}: qty=0 after alignment")
            return None

        actual_notional = qty * price
        if actual_notional < 10.0:
            print(
                f"[Futures] LONG {symbol}: notional=${actual_notional:.4f} < $10.0 minimum "
                f"(qty={qty}, price={price:.6f}) — rejected"
            )
            return None

        # USD loss guard: cap effective stop-loss based on nominal USD loss per trade
        _fut_settings = getattr(self, 'settings', None) or getattr(self, '_cached_settings', None)
        _max_loss_usd = float(getattr(_fut_settings, 'max_loss_usd_per_trade', 18.0) or 0.0) if _fut_settings else 18.0
        if _max_loss_usd > 0 and actual_notional > 0:
            _usd_sl_pct = _max_loss_usd / actual_notional * 100.0
            sl_pct = min(sl_pct, _usd_sl_pct)
            print(f"[Futures/USDGuard] LONG {symbol}: SL% capped to {sl_pct:.3f}% (${_max_loss_usd:.2f} / ${actual_notional:.2f})")

        tick     = self._get_tick_size(symbol)
        sl_price = self._align_price(price * (1 - sl_pct / 100), tick)  # below entry
        tp_price = self._align_price(price * (1 + tp_pct / 100), tick)  # above entry

        print(f"[Futures] Opening LONG {symbol}: qty={qty}, entry~{price:.4f}, "
              f"SL={sl_price:.4f} (-{sl_pct}%), TP={tp_price:.4f} (+{tp_pct}%), "
              f"leverage={leverage}×, notional=${notional:.2f}")

        if self.dry_run:
            pos = FuturesPosition(
                symbol=symbol, side="LONG", qty=qty,
                entry_price=price, sl_price=sl_price, tp_price=tp_price,
                order_id=f"DRY_LONG_{symbol}_{int(time.time())}",
                sl_order_id=None, tp_order_id=None,
            )
            self._open_positions[self._pos_key(symbol, "LONG")] = pos
            return pos

        try:
            # 1. Market LONG (BUY) order with positionSide=LONG for hedge mode
            entry_order = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": str(qty), "positionSide": "LONG",
            })
            if entry_order.get("status") not in ("FILLED", "NEW", None):
                print(f"[Futures/WARN] LONG {symbol}: entry order status={entry_order.get('status')} — may not have filled")
            order_id    = str(entry_order.get("orderId", "?"))
            filled_price = float(entry_order.get("avgPrice", price)) or price

            # FIX-T2A: software SL = tight, Binance safety SL = wide (50% wider)
            sl_price = self._align_price(filled_price * (1 - sl_pct / 100), tick)  # software SL
            tp_price = self._align_price(filled_price * (1 + tp_pct / 100), tick)
            _binance_sl_pct = sl_pct * 1.5  # safety net: 50% wider
            _binance_sl_trigger = self._align_price(filled_price * (1 - _binance_sl_pct / 100), tick)

            # 2. Stop-loss: SELL STOP_MARKET with positionSide=LONG
            # FIX-T2A: Binance trigger uses wider safety SL
            sl_order_id = None
            for _sl_attempt in range(3):
                try:
                    sl_order = self._post("/fapi/v1/algoOrder", {
                        "symbol": symbol, "side": "SELL",
                        "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                        "quantity": str(qty), "triggerPrice": str(_binance_sl_trigger),
                        "positionSide": "LONG", "timeInForce": "GTC",
                    })
                    sl_order_id = str(sl_order.get("algoId", sl_order.get("orderId", "")))
                    print(f"[Futures] LONG SL placed: triggerPrice={_binance_sl_trigger:.4f} (safety), software SL={sl_price:.4f} (algoId={sl_order_id})")
                    break
                except Exception as se:
                    if _sl_attempt < 2:
                        print(f"[Futures] LONG SL attempt {_sl_attempt + 1} failed {symbol}: {se} — retrying")
                        time.sleep(0.5)
                    else:
                        print(f"[Futures] LONG SL order failed {symbol}: {se} — closing immediately")
                        ec.add_error(ec.SOURCE_BINANCE, ec.LEVEL_CRITICAL,
                                     f"Futures LONG {symbol} SL placement failed after 3 attempts: {se}")
                        self._post("/fapi/v1/order", {
                            "symbol": symbol, "side": "SELL", "type": "MARKET",
                            "quantity": str(qty), "positionSide": "LONG",
                        })
                        return None

            # 3. Take-profit: SELL TAKE_PROFIT_MARKET with positionSide=LONG
            tp_order_id = None
            try:
                tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": "SELL",
                    "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(qty), "triggerPrice": str(tp_price),
                    "positionSide": "LONG", "timeInForce": "GTC",
                })
                tp_order_id = str(tp_order.get("algoId", tp_order.get("orderId", "")))
                print(f"[Futures] LONG TP placed: triggerPrice={tp_price:.4f} (algoId={tp_order_id})")
            except Exception as te:
                print(f"[Futures] LONG TP order failed {symbol}: {te} — continuing with SL only")

            pos = FuturesPosition(
                symbol=symbol, side="LONG", qty=qty,
                entry_price=filled_price, sl_price=sl_price, tp_price=tp_price,
                order_id=order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
            )
            pos._smart_meta['fut_total_at_open'] = self.get_total_wallet_usdt()  # SSO gate anchor
            try:
                pos._smart_meta['btc_price_at_entry'] = self.get_price('BTCUSDT')  # SSO-6: BTC ref for bounce detection
            except Exception:
                pass
            # FIX-6: trazabilidad — registrar datos del fill real de Binance
            pos._smart_meta['entry_order_id'] = order_id
            pos._smart_meta['entry_filled_qty'] = float(entry_order.get('executedQty', qty))
            pos._smart_meta['entry_avg_price'] = filled_price
            _entry_confirmed = self._verify_entry_filled(symbol, "LONG")
            pos._smart_meta['binance_entry_confirmed'] = _entry_confirmed
            if not _entry_confirmed:
                self._audit(symbol, 'entry_not_confirmed_on_binance', {
                    'side': 'LONG', 'order_id': order_id, 'status': entry_order.get('status'),
                    'qty': qty, 'filled_price': filled_price,
                })
                print(f"[Futures/WARN] LONG {symbol}: entry not confirmed on Binance — aborting position record")
                try:
                    self._post("/fapi/v1/order", {
                        "symbol": symbol, "side": "SELL", "type": "MARKET",
                        "quantity": str(qty), "positionSide": "LONG",
                    })
                except Exception as _ce:
                    print(f"[Futures/WARN] LONG {symbol}: cleanup close also failed: {_ce}")
                return None
            pos._smart_meta['regime_at_entry'] = str(getattr(settings, 'current_regime_mode', '') or '').strip().upper() or None
            pos._smart_meta['profile_at_entry'] = str(peak_quality_profile or 'B').strip().upper() or None
            pos._smart_meta['peak_quality_profile'] = str(peak_quality_profile or '').strip().upper() or None
            if peak_quality_score is not None:
                try:
                    pos._smart_meta['peak_quality'] = float(peak_quality_score)
                except Exception:
                    pass
            if signal_id:
                pos._smart_meta['signal_id'] = signal_id
            # USD loss guard audit fields
            pos._smart_meta['max_loss_usd_per_trade'] = _max_loss_usd
            pos._smart_meta['effective_sl_usd'] = min(_max_loss_usd, actual_notional * 0.95) if _max_loss_usd > 0 else None
            pos._smart_meta['effective_sl_price'] = sl_price
            pos._smart_meta['sl_source'] = 'usd_cap' if (_max_loss_usd > 0 and actual_notional > 0 and sl_pct < (_max_loss_usd / actual_notional * 100.0) * (1 + 1e-12)) else 'category_pct'
            self._open_positions[self._pos_key(symbol, "LONG")] = pos
            self._save_futures_open(pos)  # FIX-G: persist to DB
            print(f"[Futures] LONG {symbol} OPEN: entry={filled_price:.4f}, "
                  f"SL={sl_price:.4f}, TP={tp_price:.4f}")
            return pos

        except Exception as e:
            print(f"[Futures] Failed to open LONG {symbol}: {e}")
            return None

    def close_long(self, symbol: str, qty: float = None, reason: str = "manual") -> Optional[dict]:
        """Close a LONG position with a SELL MARKET order."""
        key = self._pos_key(symbol, "LONG")
        pos = self._open_positions.get(key)
        close_qty = qty or (pos.qty if pos else 0)
        if close_qty <= 0:
            return None
        current_price = self.get_price(symbol)
        pnl = (current_price - pos.entry_price) * close_qty if pos else 0.0
        print(f"[Futures] Closing LONG {symbol}: entry={pos.entry_price if pos else '?':.4f}, "
              f"current={current_price:.4f}, PnL~${pnl:.2f}, reason={reason}")
        if self.dry_run:
            if pos:
                pos.closed = True; pos.closed_at = datetime.now(timezone.utc); pos.exit_price = current_price; pos.pnl = pnl
            return {"simulated": True, "symbol": symbol, "pnl": pnl}
        if pos:
            for oid in [pos.sl_order_id, pos.tp_order_id]:
                if oid:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": oid})
                    except Exception:
                        pass
        try:
            step = self._get_step_size(symbol)
            aligned_qty = self._align_qty(close_qty, step)
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": str(aligned_qty), "positionSide": "LONG",
            })
            # Verify real position size before persisting close; if still open, keep tracking residual.
            _real_qty = self._get_position_qty_real(symbol, "LONG")
            if _real_qty > 0:
                print(f"[Futures/WARN] LONG {symbol}: close order sent but position still open (amt={_real_qty}) — NOT marking closed")
                if pos:
                    pos.qty = _real_qty
                    self._persist_smart_meta(pos)
                return result
            if pos:
                _real = self._get_realized_pnl_from_trades(symbol, "LONG", pos.opened_at)
                pos.closed = True
                pos.closed_at = datetime.now(timezone.utc)
                pos.exit_price = _real.get('exit_price') or current_price
                pos.pnl = _real.get('pnl') if _real.get('pnl_accounting_mode') else pnl
                if _real.get('pnl_accounting_mode'):
                    pos._smart_meta['pnl_accounting_mode'] = _real['pnl_accounting_mode']
                    pos._smart_meta['binance_realized_pnl'] = _real.get('realized_pnl')
                    pos._smart_meta['binance_close_commission'] = _real.get('commission')
                    pos._smart_meta['binance_funding'] = _real.get('funding', 0.0)
                self._save_futures_closed(pos, reason)  # FIX-G: persist to DB
            print(f"[Futures] LONG {symbol} CLOSED: PnL~${pnl:.2f}")
            return result
        except Exception as e:
            _emsg = str(e)
            # FIX-T1: Binance already closed position via SL/TP algo order
            if pos and ("-2022" in _emsg or "ReduceOnly" in _emsg):
                print(f"[Futures] close_long {symbol}: position already closed by Binance — recording")
                _real = self._get_realized_pnl_from_trades(symbol, "LONG", pos.opened_at)
                pos.closed = True
                pos.closed_at = datetime.now(timezone.utc)
                pos.exit_price = _real.get('exit_price') or current_price
                pos.pnl = _real.get('pnl') or pnl
                _reason = _real.get('reason', 'stop_loss_binance_safety')
                self._save_futures_closed(pos, _reason)
                print(f"[Futures] LONG {symbol} CLOSED (by Binance): PnL=${pos.pnl:.2f}")
                return {"binance_closed": True, "symbol": symbol, "pnl": pos.pnl}
            print(f"[Futures] close_long {symbol} failed: {e}")
            return None

    def close_short(self, symbol: str, qty: float = None, reason: str = "manual") -> Optional[dict]:
        """
        Close a SHORT position with a BUY MARKET order.
        If qty is None, uses the tracked position qty.
        Cancels any open SL/TP orders before closing.
        """
        key = self._pos_key(symbol, "SHORT")
        pos = self._open_positions.get(key) or self._open_positions.get(symbol)  # legacy compat
        close_qty = qty or (pos.qty if pos else 0)

        if close_qty <= 0:
            print(f"[Futures] close_short {symbol}: no qty to close")
            return None

        current_price = self.get_price(symbol)
        pnl = (pos.entry_price - current_price) * close_qty if pos else 0.0

        print(f"[Futures] Closing SHORT {symbol}: qty={close_qty}, "
              f"entry={pos.entry_price if pos else '?':.4f}, current={current_price:.4f}, "
              f"PnL~${pnl:.2f}, reason={reason}")

        if self.dry_run:
            if pos:
                pos.closed     = True
                pos.closed_at  = datetime.now(timezone.utc)
                pos.exit_price = current_price
                pos.pnl        = pnl
            return {"simulated": True, "symbol": symbol, "pnl": pnl}

        # Cancel open SL/TP algo orders first
        if pos:
            for oid in [pos.sl_order_id, pos.tp_order_id]:
                if oid:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": oid})
                    except Exception:
                        pass

        try:
            step = self._get_step_size(symbol)
            from decimal import Decimal
            aligned_qty = self._align_qty(close_qty, step)
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": str(aligned_qty), "positionSide": "SHORT",
            })
            # Verify real position size before persisting close; if still open, keep tracking residual.
            _real_qty = self._get_position_qty_real(symbol, "SHORT")
            if _real_qty > 0:
                print(f"[Futures/WARN] SHORT {symbol}: close order sent but position still open (amt={_real_qty}) — NOT marking closed")
                if pos:
                    pos.qty = _real_qty
                    self._persist_smart_meta(pos)
                return result
            if pos:
                _real = self._get_realized_pnl_from_trades(symbol, "SHORT", pos.opened_at)
                pos.closed     = True
                pos.closed_at  = datetime.now(timezone.utc)
                pos.exit_price = _real.get('exit_price') or current_price
                pos.pnl        = _real.get('pnl') if _real.get('pnl_accounting_mode') else pnl
                if _real.get('pnl_accounting_mode'):
                    pos._smart_meta['pnl_accounting_mode'] = _real['pnl_accounting_mode']
                    pos._smart_meta['binance_realized_pnl'] = _real.get('realized_pnl')
                    pos._smart_meta['binance_close_commission'] = _real.get('commission')
                    pos._smart_meta['binance_funding'] = _real.get('funding', 0.0)
                self._save_futures_closed(pos, reason)  # FIX-G: persist to DB
            print(f"[Futures] SHORT {symbol} CLOSED: PnL~${pnl:.2f}")
            return result
        except Exception as e:
            _emsg = str(e)
            # FIX-T1: Binance already closed position via SL/TP algo order
            if pos and ("-2022" in _emsg or "ReduceOnly" in _emsg):
                print(f"[Futures] close_short {symbol}: position already closed by Binance — recording")
                _real = self._get_realized_pnl_from_trades(symbol, "SHORT", pos.opened_at)
                pos.closed     = True
                pos.closed_at  = datetime.now(timezone.utc)
                pos.exit_price = _real.get('exit_price') or current_price
                pos.pnl        = _real.get('pnl') or pnl
                _reason = _real.get('reason', 'stop_loss_binance_safety')
                self._save_futures_closed(pos, _reason)
                print(f"[Futures] SHORT {symbol} CLOSED (by Binance): PnL=${pos.pnl:.2f}")
                return {"binance_closed": True, "symbol": symbol, "pnl": pos.pnl}
            print(f"[Futures] close_short {symbol} failed: {e}")
            return None

    def update_sl_tp(
        self,
        symbol: str,
        side: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        """
        Move stop-loss and/or take-profit on a live futures position.

        Process:
          1. Cancel the existing algo orders for SL/TP.
          2. Place new algo orders at the updated levels.
          3. Update in-memory FuturesPosition state.

        Returns True on success, False if anything failed (position stays managed
        by the old levels in Binance — silent failure does NOT close the position).

        IMPORTANT: Binance may reject the cancel with -2011 (order already filled)
        if the SL/TP triggered between our cycles.  This is handled gracefully —
        we proceed to place the new order (it will fail with no-position error and
        check_positions() will detect the closure on the next cycle).
        """
        key = self._pos_key(symbol, side.upper())
        pos = self._open_positions.get(key)
        if not pos or pos.closed:
            return False

        # FIX-T3: REGLA DE ORO — SL nunca empeora (guardia universal)
        if new_sl is not None:
            if pos.side == "SHORT" and new_sl > pos.sl_price:
                print(f"[Futures/Guard] {symbol}: SL WORSEN blocked {pos.sl_price:.4f} → {new_sl:.4f}")
                new_sl = None
            elif pos.side == "LONG" and new_sl < pos.sl_price:
                print(f"[Futures/Guard] {symbol}: SL WORSEN blocked {pos.sl_price:.4f} → {new_sl:.4f}")
                new_sl = None
        if new_sl is None and new_tp is None:
            return True  # nothing to update

        if self.dry_run:
            if new_sl is not None:
                pos.sl_price = new_sl
            if new_tp is not None:
                pos.tp_price = new_tp
            print(f"[Futures/Trail] DRY update {symbol} {side}: SL={new_sl} TP={new_tp}")
            return True

        tick = self._get_tick_size(symbol)
        success = True

        # FIX-T5: Anti-cancel gap — place new order FIRST, then cancel old.
        # This ensures the position is NEVER without SL/TP protection.

        # ── Update SL: place new → cancel old ────────────────────────────────
        if new_sl is not None:
            _sl_aligned = self._align_price(new_sl, tick)
            _sl_side   = "BUY" if side.upper() == "SHORT" else "SELL"
            _pos_side  = side.upper()
            _old_sl_id = pos.sl_order_id
            try:
                # 1. FIRST: place new SL order
                _sl_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": _sl_side,
                    "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                    "quantity": str(pos.qty), "triggerPrice": str(_sl_aligned),
                    "positionSide": _pos_side, "timeInForce": "GTC",
                })
                _new_sl_id = str(_sl_order.get("algoId", _sl_order.get("orderId", "")))
                if not _new_sl_id or _new_sl_id == "None":
                    print(f"[Futures/WARN] {symbol} {side}: SL order response missing algoId — response={_sl_order}")
                    success = False
                else:
                    pos.sl_order_id = _new_sl_id
                    pos.sl_price    = _sl_aligned
                # 2. THEN: cancel old SL (position is already protected by new one)
                if _old_sl_id:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": _old_sl_id})
                    except Exception:
                        pass  # old order may already be gone — safe to ignore
                print(f"[Futures/Trail] {symbol} {side}: SL moved → {_sl_aligned:.4f} (algoId={pos.sl_order_id})")
            except Exception as _e:
                _emsg = str(_e)
                # If new SL placement fails, old one is still active (no gap!)
                if "-2022" in _emsg or "ReduceOnly" in _emsg:
                    print(f"[Futures/Trail] {symbol}: SL place failed (position closed by Binance)")
                    pos.closed = True
                    return False
                # Fallback: try cancel-first approach (Binance may reject duplicate)
                if _old_sl_id:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": _old_sl_id})
                        _sl_order = self._post("/fapi/v1/algoOrder", {
                            "symbol": symbol, "side": _sl_side,
                            "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                            "quantity": str(pos.qty), "triggerPrice": str(_sl_aligned),
                            "positionSide": _pos_side, "timeInForce": "GTC",
                        })
                        pos.sl_order_id = str(_sl_order.get("algoId", _sl_order.get("orderId", "")))
                        pos.sl_price = _sl_aligned
                        print(f"[Futures/Trail] {symbol} {side}: SL moved (fallback) → {_sl_aligned:.4f}")
                    except Exception as _e2:
                        print(f"[Futures/Trail] {symbol}: SL update failed (both paths): {_e2}")
                        success = False
                else:
                    print(f"[Futures/Trail] {symbol}: failed to place new SL order: {_e}")
                    success = False

        # ── Update TP: place new → cancel old ────────────────────────────────
        if new_tp is not None:
            _tp_aligned = self._align_price(new_tp, tick)
            _tp_side   = "BUY" if side.upper() == "SHORT" else "SELL"
            _pos_side  = side.upper()
            _old_tp_id = pos.tp_order_id
            try:
                # 1. FIRST: place new TP order
                _tp_order = self._post("/fapi/v1/algoOrder", {
                    "symbol": symbol, "side": _tp_side,
                    "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(pos.qty), "triggerPrice": str(_tp_aligned),
                    "positionSide": _pos_side, "timeInForce": "GTC",
                })
                pos.tp_order_id = str(_tp_order.get("algoId", _tp_order.get("orderId", "")))
                pos.tp_price    = _tp_aligned
                # 2. THEN: cancel old TP
                if _old_tp_id:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": _old_tp_id})
                    except Exception:
                        pass
                print(f"[Futures/Trail] {symbol} {side}: TP moved → {_tp_aligned:.4f} (algoId={pos.tp_order_id})")
            except Exception as _e:
                # Fallback: cancel-first
                if _old_tp_id:
                    try:
                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": _old_tp_id})
                        _tp_order = self._post("/fapi/v1/algoOrder", {
                            "symbol": symbol, "side": _tp_side,
                            "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                            "quantity": str(pos.qty), "triggerPrice": str(_tp_aligned),
                            "positionSide": _pos_side, "timeInForce": "GTC",
                        })
                        pos.tp_order_id = str(_tp_order.get("algoId", _tp_order.get("orderId", "")))
                        pos.tp_price = _tp_aligned
                        print(f"[Futures/Trail] {symbol} {side}: TP moved (fallback) → {_tp_aligned:.4f}")
                    except Exception as _e2:
                        print(f"[Futures/Trail] {symbol}: TP update failed (both paths): {_e2}")
                        success = False
                else:
                    print(f"[Futures/Trail] {symbol}: failed to place new TP order: {_e}")
                    success = False

        return success

    def check_positions(self, settings=None, monitor_only: bool = False) -> List[dict]:
        """
        Check all tracked positions (LONG and SHORT) for SL/TP hits.

        Per-cycle logic (executed in order):
          1. Breakeven SL — when unrealized PnL% reaches futures_breakeven_activation_pct,
             move SL to entry price (protects capital, never reduces realized gains).
          2. Trailing stop — when unrealized PnL% reaches futures_trailing_activation_pct,
             trail the SL (SHORT: move SL down toward price; LONG: move SL up toward price)
             by futures_trailing_distance_pct.  Also trails the TP proportionally for SHORTs
             (when price drops more than TP, lower TP to lock in more profit).
          3. Smart TP EXTEND — when TP is hit and futures_smart_tp_enabled, evaluate SHORT
             momentum (RSI, vol_spike, trend_4h, success_prob).  If score >= threshold:
             EXTEND (lower TP, raise SL to current price). Otherwise CLOSE.
          4. SL hit — close immediately.

        Returns list of closed positions dicts with keys: symbol, side, reason, pnl.
        """
        closed = []
        if settings is not None:
            self._cached_settings = settings  # FIX-5: persist for retro-tag on next startup

        _signal_cache: Dict[str, Dict[str, Optional[float]]] = {}
        _signal_generator = None
        _truth_status = {
            "ok": True,
            "severity": "unknown",
            "mismatch_count": 0,
            "binance_keys": [],
        }
        _binance_live_keys = set()
        _enforce_live_gate = not bool(self.dry_run)

        try:
            if self.dry_run:
                _binance_live_keys = set(self._open_positions.keys())
            else:
                _truth_status = evaluate_futures_parity(self)
                _binance_live_keys = set(_truth_status.get("binance_keys") or [])
                persist_truth_status(self.db_client, _truth_status, source="futures_check_positions")
                if (_truth_status.get("mismatch_count") or 0) > 0:
                    audit_truth_mismatches(self.db_client, _truth_status, source="futures_check_positions")
                    print(
                        f"[BinanceTruth/Futures] severity={_truth_status.get('severity')} "
                        f"mismatches={_truth_status.get('mismatch_count')}"
                    )
        except Exception as _truth_err:
            print(f"[BinanceTruth/Futures] parity check failed: {_truth_err}")

        def _get_signal_snapshot(_symbol: str) -> Dict[str, Optional[float]]:
            nonlocal _signal_generator
            if _symbol in _signal_cache:
                return _signal_cache[_symbol]
            _ctx: Dict[str, Optional[float]] = {
                "strength": None,
                "direction": None,
                "timestamp": None,
            }
            try:
                if self._binance_client_ref is not None:
                    from autonomous_signals import AutonomousSignalGenerator
                    if _signal_generator is None:
                        _signal_generator = AutonomousSignalGenerator(self._binance_client_ref)
                    _sig = _signal_generator.analyze_symbol(_symbol, interval='1h', use_multi_timeframe=True)
                    if isinstance(_sig, dict):
                        _ctx["strength"] = float(_sig.get("strength", 0.0) or 0.0)
                        _ctx["direction"] = str(_sig.get("signal") or "").strip().upper() or None
                        _ctx["timestamp"] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
            _signal_cache[_symbol] = _ctx
            return _ctx

        def _record_loss_focus(
            pos: Any,
            position_key: str,
            *,
            price: float,
            pnl_usdt_net: float,
            pnl_pct_net: float,
            pnl_usdt_gross: float,
            signal_strength: Optional[float],
            signal_direction: Optional[str],
            pnl_velocity_1h: Optional[float],
            fear_greed_value: Optional[float],
        ) -> None:
            if not bool(get_param(settings, 'loss_focus_enabled', True)):
                return
            metadata = pos._smart_meta if isinstance(pos._smart_meta, dict) else {}
            metadata['trade_type'] = 'futures'
            _pos_profile = str(
                metadata.get('peak_quality_profile') or metadata.get('profile_at_entry') or 'B'
            ).strip().upper()
            state, event = evaluate_loss_focus(
                metadata,
                pnl_usdt_net,
                float(get_param(settings, 'loss_focus_threshold_usdt', 9.0)),
                float(get_param(settings, 'loss_focus_reset_usdt', 6.0)),
            )
            metadata['loss_focus_state'] = state
            if event is None:
                return
            snapshot_position = SimpleNamespace(
                position_id=position_key,
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                entry_time=pos.opened_at,
                quantity=pos.qty,
                entry_commission=metadata.get('entry_commission', 0.0),
                stop_loss=pos.sl_price,
                take_profit=pos.tp_price,
            )
            snapshot = build_loss_focus_snapshot(
                position=snapshot_position,
                metadata=metadata,
                pnl_usdt_net=pnl_usdt_net,
                pnl_pct_net=pnl_pct_net,
                pnl_usdt_gross=pnl_usdt_gross,
                current_price=price,
                regime_mode=getattr(settings, 'current_regime_mode', None) if settings else None,
                runtime_profile=_pos_profile,
                signal_strength=signal_strength,
                signal_direction=signal_direction,
                pnl_velocity_1h=pnl_velocity_1h,
                peak_pnl_pct=float(metadata.get('peak_pnl_pct', 0.0) or 0.0),
                fear_greed_value=fear_greed_value,
            )
            snapshot['event'] = event
            snapshot['non_blocking'] = True
            try:
                self._persist_smart_meta(pos)
                self._audit(pos.symbol, event, snapshot)
                if self.db_client:
                    alert_id = f"loss_focus_{position_key.replace('-', '').lower()}"
                    if event == 'loss_focus_recovered':
                        self.db_client.execute_update(
                            "UPDATE trading_runtime_state SET value = jsonb_set(value::jsonb, '{acknowledged}', 'true'::jsonb), updated_at=NOW() WHERE key=%s",
                            (f'operator_alert_{alert_id}',),
                        )
                    elif event == 'loss_focus_triggered':
                        alert_payload = {
                            'alert_type': 'loss_focus',
                            'message': f"{pos.symbol} {pos.side} crossed net loss ${abs(pnl_usdt_net):.2f}",
                            'position_id': position_key,
                            'symbol': pos.symbol,
                            'event': event,
                            'pnl_usdt_net': round(float(pnl_usdt_net), 8),
                            'entry_context_status': 'canonical',
                            'snapshot': snapshot,
                            'acknowledged': False,
                            'ts': datetime.now(timezone.utc).isoformat(),
                        }
                        self.db_client.execute_update(
                            "INSERT INTO trading_runtime_state (key, value, updated_at) VALUES (%s, %s, NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                            (f'operator_alert_{alert_id}', json.dumps(alert_payload)),
                        )
            except Exception as error:
                print(f'[LossFocus/Futures] {pos.symbol}: telemetry persistence failed: {error}')

        # ── FIX-T4: Ghost detection — find positions closed by Binance SL/TP ──
        if not self.dry_run:
            try:
                _binance_keys = set()
                _binance_syms = set()
                for p in self.get_positions():
                    _amt = float(p.get("positionAmt", 0))
                    _sym = p.get("symbol", "")
                    if _amt == 0:
                        continue
                    _side = "SHORT" if _amt < 0 else "LONG"
                    _binance_keys.add(self._pos_key(_sym, _side))
                    _binance_syms.add(_sym)

                # FIX-T4-L1 (original): memory _open_positions vs Binance
                for _gk, _gp in list(self._open_positions.items()):
                    if _gp.closed:
                        continue
                    if _gk not in _binance_keys:
                        print(f"[Futures/Ghost-L1] {_gp.symbol} {_gp.side}: position gone from Binance — closed externally")
                        _real = self._get_realized_pnl_from_trades(_gp.symbol, _gp.side, _gp.opened_at)
                        _gp.closed = True
                        _gp.closed_at = datetime.now(timezone.utc)
                        _gp.exit_price = _real.get('exit_price') or _gp.sl_price
                        _gp.pnl = _real.get('pnl', 0)
                        _close_reason = _real.get('reason', 'binance_safety_close')
                        self._save_futures_closed(_gp, _close_reason)
                        closed.append({"symbol": _gp.symbol, "side": _gp.side,
                                       "reason": _close_reason, "pnl": _gp.pnl})

                # FIX-T4-L2 (NEW): DB trades_open vs Binance — catches ghosts never in memory
                # This handles records left after container restart when Binance closed externally
                if self.db_client:
                    try:
                        _db_open = self.db_client.execute_query(
                            "SELECT id, symbol, side, quantity, price, entry_time, stop_loss, "
                            "take_profit, pnl_prediction, indicators_at_entry, metadata "
                            "FROM trades_open WHERE metadata->>'trade_type' = 'futures'"
                        )
                        # Group by symbol to handle escalamientos
                        _db_by_sym: dict = {}
                        for _row in _db_open:
                            _row_side = 'SHORT' if _row['side'] == 'SELL' else 'LONG'
                            _db_by_sym.setdefault((_row['symbol'], _row_side), []).append(_row)

                        for (_sym, _side), _db_rows in _db_by_sym.items():
                            if _sym in _binance_syms:
                                continue  # still active in Binance, skip
                            if self._pos_key(_sym, _side) in self._open_positions:
                                continue  # already handled by L1 above
                            # Ghost: symbol not in Binance but has DB record(s)
                            print(f"[Futures/Ghost-L2] {_sym} {_side}: {len(_db_rows)} DB record(s) not in Binance — cleaning up")
                            _opened_at = min(
                                (_parse_utc_datetime(row.get('entry_time')) for row in _db_rows),
                                key=lambda value: value or datetime.max.replace(tzinfo=timezone.utc),
                            )
                            _real = self._get_realized_pnl_from_trades(_sym, _side, _opened_at)
                            _exit_p = _real.get('exit_price', 0)
                            _total_pnl = float(_real.get('pnl', 0))
                            _close_reason = _real.get('reason', 'binance_closed_untracked')
                            _close_now = datetime.now(timezone.utc)
                            _cgid = str(uuid.uuid4())
                            # Calculate total notional for proportional PnL distribution
                            _total_notional = sum(float(r['quantity']) * float(r['price']) for r in _db_rows)
                            for _i, _row in enumerate(_db_rows):
                                _qty = float(_row['quantity'])
                                _ep = float(_row['price'])
                                _notional = _qty * _ep
                                _weight = _notional / _total_notional if _total_notional > 0 else 1.0 / len(_db_rows)
                                _row_pnl = _total_pnl * _weight if _total_pnl else None
                                # Build a minimal FuturesPosition to reuse _save_futures_closed
                                _meta = _row.get('metadata') or {}
                                if isinstance(_meta, str):
                                    try: _meta = json.loads(_meta)
                                    except: _meta = {}
                                _ghost_pos = FuturesPosition(
                                    symbol=_sym, side=_side, qty=_qty,
                                    entry_price=_ep,
                                    sl_price=float(_row.get('stop_loss') or _ep),
                                    tp_price=float(_row.get('take_profit') or _ep),
                                    order_id=_meta.get('order_id'),
                                    sl_order_id=_meta.get('sl_order_id'),
                                    tp_order_id=_meta.get('tp_order_id'),
                                )
                                _ghost_pos.closed = True
                                _ghost_pos.closed_at = _close_now
                                _ghost_pos.exit_price = _exit_p if _exit_p else _ep
                                _ghost_pos.pnl = _row_pnl
                                _ghost_pos._smart_meta = _meta.get('smart_meta', {})
                                try:
                                    _et = _row.get('entry_time')
                                    if _et:
                                        _ghost_pos.opened_at = _et if hasattr(_et, 'tzinfo') else datetime.fromisoformat(str(_et))
                                        if _ghost_pos.opened_at.tzinfo is None:
                                            _ghost_pos.opened_at = _ghost_pos.opened_at.replace(tzinfo=timezone.utc)
                                except Exception:
                                    pass
                                # Load prediction/indicators from DB row
                                try:
                                    if _row.get('pnl_prediction'):
                                        _ghost_pos.pnl_prediction = json.loads(_row['pnl_prediction']) if isinstance(_row['pnl_prediction'], str) else _row['pnl_prediction']
                                    if _row.get('indicators_at_entry'):
                                        _ghost_pos.indicators_at_entry = json.loads(_row['indicators_at_entry']) if isinstance(_row['indicators_at_entry'], str) else _row['indicators_at_entry']
                                except Exception:
                                    pass
                                self._save_futures_closed(
                                    _ghost_pos, _close_reason,
                                    close_group_id=_cgid,
                                    esc_index=_i + 1,
                                    total_esc=len(_db_rows),
                                    pnl_override=_row_pnl,
                                )
                                print(f"[Futures/Ghost-L2] {_sym}: inserted trades_closed esc={_i+1}/{len(_db_rows)} pnl={round(_row_pnl or 0, 2)}")
                            if _total_pnl:
                                closed.append({"symbol": _sym, "side": _side,
                                               "reason": _close_reason, "pnl": _total_pnl})
                    except Exception as _l2_err:
                        print(f"[Futures/Ghost-L2] error: {_l2_err}")

            except Exception as _ge:
                print(f"[Futures/Ghost] detection error: {_ge}")

        # ── FIX-7: Throttle algo order sync to every N cycles to avoid rate limits ──
        self._check_cycle_count = getattr(self, '_check_cycle_count', 0) + 1
        _verify_interval = getattr(settings, 'futures_binance_verify_interval_cycles', 3) if settings else 3
        _do_algo_sync = (self._check_cycle_count % _verify_interval == 1) or self._check_cycle_count == 1

        # ── FIX-T7b: Periodic Binance SL/TP sync — detect and fix drifts ──
        if not self.dry_run and _do_algo_sync:
            try:
                _all_algo = self._get("/fapi/v1/openAlgoOrders", signed=True)
                _algo_list = _all_algo if isinstance(_all_algo, list) else _all_algo.get("orders", _all_algo.get("algoOrders", []))
                # Build map: symbol → {sl_trigger, tp_trigger, sl_id, tp_id}
                _binance_sl_tp = {}
                for _ao in _algo_list:
                    _ao_sym = _ao.get("symbol", "")
                    _ao_side = _ao.get("positionSide", "BOTH")
                    _ao_type = _ao.get("type") or _ao.get("orderType", "")
                    _ao_trigger = float(_ao.get("triggerPrice", 0))
                    _ao_id = str(_ao.get("algoId", _ao.get("orderId", "")))
                    if not _ao_sym or _ao_trigger <= 0:
                        continue
                    _bk = f"{_ao_sym}_{_ao_side}"
                    _binance_sl_tp.setdefault(_bk, {})
                    if _ao_type == "STOP_MARKET":
                        _binance_sl_tp[_bk]["sl_trigger"] = _ao_trigger
                        _binance_sl_tp[_bk]["sl_id"] = _ao_id
                    elif _ao_type == "TAKE_PROFIT_MARKET":
                        _binance_sl_tp[_bk]["tp_trigger"] = _ao_trigger
                        _binance_sl_tp[_bk]["tp_id"] = _ao_id
                for _pk, _pp in self._open_positions.items():
                    if _pp.closed:
                        continue
                    _bk = f"{_pp.symbol}_{_pp.side}"
                    _bdata = _binance_sl_tp.get(_bk, {})
                    _b_sl = _bdata.get("sl_trigger")
                    if _b_sl is not None and abs(_b_sl - _pp.sl_price) > 0.001:
                        # Binance SL differs from in-memory — check if it's WORSE
                        _is_worse = (_pp.side == "SHORT" and _b_sl > _pp.sl_price) or \
                                    (_pp.side == "LONG" and _b_sl < _pp.sl_price)
                        if _is_worse:
                            print(f"[Futures/SyncSL] {_pp.symbol} {_pp.side}: Binance SL={_b_sl:.4f} is WORSE than "
                                  f"expected {_pp.sl_price:.4f} — re-placing order")
                            self.update_sl_tp(_pp.symbol, _pp.side, new_sl=_pp.sl_price)
                        else:
                            # Binance SL is better (tighter) — adopt it
                            _pp.sl_price = _b_sl
                            if _bdata.get("sl_id"):
                                _pp.sl_order_id = _bdata["sl_id"]
                            print(f"[Futures/SyncSL] {_pp.symbol} {_pp.side}: adopted Binance SL={_b_sl:.4f} (tighter)")
            except Exception as _sync_err:
                print(f"[Futures/SyncSL] algo order sync failed: {_sync_err}")

        # Read settings — use safe defaults if not provided
        from config_helpers import get_param
        _trailing_enabled      = get_param(settings, 'futures_trailing_enabled')      if settings else True
        _trail_act_pct        = get_param(settings, 'futures_trailing_activation_pct') if settings else 2.0
        _trail_dist_pct       = get_param(settings, 'futures_trailing_distance_pct')   if settings else 1.5
        _breakeven_act_pct    = get_param(settings, 'futures_breakeven_activation_pct') if settings else 1.0
        _smart_tp             = get_param(settings, 'futures_smart_tp_enabled')        if settings else True
        _smart_tp_max_ext     = get_param(settings, 'futures_smart_tp_max_extensions')   if settings else 2
        _smart_tp_max_pnl     = get_param(settings, 'futures_smart_tp_max_pnl_pct')  if settings else 12.0
        _smart_tp_threshold   = get_param(settings, 'futures_smart_tp_extend_score_threshold') if settings else 2
        _smart_tp_ext_pct     = get_param(settings, 'futures_smart_tp_extension_pct')  if settings else 2.0

        # C2: Fetch F&G once per cycle — shared across all positions in loop
        _cycle_fng_value: float = 50.0
        try:
            from news_client import NewsClient as _NC2
            _fng_data2 = _NC2().get_fear_greed()
            _cycle_fng_value = float(_fng_data2.get('value', 50)) if isinstance(_fng_data2, dict) else 50.0
        except Exception:
            pass

        for key, pos in list(self._open_positions.items()):
            if pos.closed:
                continue
            symbol = pos.symbol
            _pos_profile = str(
                (pos._smart_meta or {}).get('peak_quality_profile')
                or (pos._smart_meta or {}).get('profile_at_entry')
                or 'B'
            ).strip().upper()
            try:
                price   = self.get_price(symbol)
                entry   = pos.entry_price
                unr_gross = pos.unrealized_pnl(price)
                # C0: all futures calculations use net PnL after Binance fees
                from binance_commission import net_pnl as _f_net_pnl
                unr = _f_net_pnl(unr_gross, pos.qty, entry, price, 'futures', settings)
                pnl_pct = unr / (entry * pos.qty) if entry > 0 and pos.qty > 0 else 0.0
                _ctx_signal_strength = None
                _ctx_signal_direction = None
                _ctx_signal_timestamp = None
                _ctx_pnl_velocity = None

                # Base velocity context for supervisors (can be refined by SSO path below).
                try:
                    from smart_scale_out import compute_pnl_velocity as _compute_pnl_velocity
                    _v_snap = pos._smart_meta.get('sso_pnl_snapshot')
                    _v_val, _v_new = _compute_pnl_velocity(pnl_pct, _v_snap)
                    _ctx_pnl_velocity = _v_val
                    if _v_new != _v_snap:
                        pos._smart_meta['sso_pnl_snapshot'] = _v_new
                        self._persist_smart_meta(pos)
                except Exception:
                    pass

                _base_sig = _get_signal_snapshot(symbol)
                _ctx_signal_strength = _base_sig.get("strength")
                _ctx_signal_direction = _base_sig.get("direction")
                _ctx_signal_timestamp = _base_sig.get("timestamp")

                _record_loss_focus(
                    pos,
                    key,
                    price=price,
                    pnl_usdt_net=unr,
                    pnl_pct_net=pnl_pct,
                    pnl_usdt_gross=unr_gross,
                    signal_strength=_ctx_signal_strength,
                    signal_direction=_ctx_signal_direction,
                    pnl_velocity_1h=_ctx_pnl_velocity,
                    fear_greed_value=_cycle_fng_value,
                )

                print(
                    f"[Futures] {symbol} {pos.side} check: price={price:.4f}, "
                    f"entry={entry:.4f}, unr_pnl=${unr:.2f} ({pnl_pct*100:.2f}%), "
                    f"SL={pos.sl_price:.4f}, TP={pos.tp_price:.4f}"
                )

                # ── Emergency USD gap stop ──────────────────────────────────────
                _fut_guard_settings = settings or getattr(self, '_cached_settings', None) or getattr(self, 'settings', None)
                _max_loss_usd_f = float(getattr(_fut_guard_settings, 'max_loss_usd_per_trade', 18.0) or 0.0) if _fut_guard_settings else 18.0
                _emergency_mult_f = float(getattr(_fut_guard_settings, 'emergency_gap_loss_multiplier', 1.5) or 0.0) if _fut_guard_settings else 1.5
                if _max_loss_usd_f > 0 and _emergency_mult_f > 0 and unr <= -(_max_loss_usd_f * _emergency_mult_f):
                    _gap_reason = f"emergency_gap_stop ({unr:.2f} USD)"
                    _gap_close = self.close_long(symbol, reason=_gap_reason) if pos.side == "LONG" else self.close_short(symbol, reason=_gap_reason)
                    if _gap_close:
                        closed.append({"symbol": symbol, "side": pos.side, "reason": _gap_reason, "pnl": pos.pnl or unr})
                        self._audit(symbol, _gap_reason, {'side': pos.side, 'pnl_usdt': round(unr, 4), 'pnl_pct': round(pnl_pct * 100, 4)})
                        print(f"[Futures/Emergency] {symbol} {pos.side}: closed by {_gap_reason}")
                    continue

                # ── 0a. Adverse signal early-exit (loss containment before hard SL) ──
                _adv_enabled = bool(get_param(settings, 'futures_adverse_exit_enabled')) if settings else True
                _adv_loss_pct = float(get_param(settings, 'futures_adverse_exit_loss_pct')) / 100.0 if settings else 0.008
                _adv_min_hours = float(get_param(settings, 'futures_adverse_exit_min_hours')) if settings else 1.5
                _adv_min_strength = float(get_param(settings, 'futures_adverse_exit_min_strength')) if settings else 0.55
                if _adv_enabled and pnl_pct <= -abs(_adv_loss_pct):
                    _age_h = 0.0
                    try:
                        if pos.opened_at:
                            _age_h = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600.0
                    except Exception:
                        _age_h = 0.0
                    if _age_h >= _adv_min_hours:
                        try:
                            _bc_ref = getattr(self, '_binance_client_ref', None)
                            if _bc_ref:
                                from autonomous_signals import AutonomousSignalGenerator
                                _adv_sg = AutonomousSignalGenerator(_bc_ref)
                                _adv_sig = _adv_sg.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
                                _adv_dir = str((_adv_sig or {}).get('signal') or '').strip().upper()
                                _adv_strength = float((_adv_sig or {}).get('strength', 0.0) or 0.0)
                                _ctx_signal_direction = _adv_dir or _ctx_signal_direction
                                _ctx_signal_strength = _adv_strength
                                _ctx_signal_timestamp = datetime.now(timezone.utc).isoformat()
                                _against_short = pos.side == "SHORT" and _adv_dir == "BUY" and _adv_strength >= _adv_min_strength
                                _against_long = pos.side == "LONG" and _adv_dir == "SELL" and _adv_strength >= _adv_min_strength
                                if _against_short or _against_long:
                                    _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                                    _adv_result = _close_fn(symbol, reason="adverse_signal_exit")
                                    if _adv_result:
                                        self._audit(symbol, 'adverse_signal_exit', {
                                            'side': pos.side,
                                            'pnl_pct': round(pnl_pct * 100.0, 4),
                                            'pnl_usdt': round(unr, 4),
                                            'signal': _adv_dir,
                                            'signal_strength': round(_adv_strength, 4),
                                            'min_strength': round(_adv_min_strength, 4),
                                            'age_hours': round(_age_h, 3),
                                            'loss_threshold_pct': round(_adv_loss_pct * 100.0, 4),
                                        })
                                        print(f"[Futures/AdverseExit] {symbol} {pos.side}: close on {_adv_dir} "
                                              f"(strength={_adv_strength:.2f}, loss={pnl_pct*100:.2f}%, age={_age_h:.2f}h)")
                                        closed.append({
                                            "symbol": symbol,
                                            "side": pos.side,
                                            "reason": "adverse_signal_exit",
                                            "pnl": pos.pnl or unr,
                                        })
                                        continue
                        except Exception as _adv_err:
                            print(f"[Futures/AdverseExit] {symbol}: evaluation error: {_adv_err}")

                # ── 0a. Track peak PnL for momentum reversal ───────────────────
                _prev_peak = pos._smart_meta.get('peak_pnl_pct', 0.0)
                if pnl_pct > _prev_peak:
                    pos._smart_meta['peak_pnl_pct'] = pnl_pct
                    self._persist_smart_meta(pos)

                # ── 0a2. SSO-6: Bounce entry auto-tagging ─────────────────────────
                # Tag position as bounce if peak stayed too low for too long
                if not pos._smart_meta.get('bounce_entry', False) and pos.opened_at:
                    try:
                        _br_age_h = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
                        _br_max_peak = getattr(settings, 'futures_bounce_rescue_max_peak_pct', 0.5) / 100.0 if settings else 0.005
                        _br_min_h = getattr(settings, 'futures_bounce_rescue_min_hours', 3.0) if settings else 3.0
                        if _br_age_h >= _br_min_h and _prev_peak < _br_max_peak:
                            pos._smart_meta['bounce_entry'] = True
                            self._persist_smart_meta(pos)
                            print(f"[SSO-6] {symbol} {pos.side}: TAGGED as bounce_entry "
                                  f"(peak={_prev_peak*100:.2f}%<{_br_max_peak*100:.1f}%, age={_br_age_h:.1f}h)")
                            self._audit(symbol, 'bounce_tag', {
                                'side': pos.side, 'age_hours': round(_br_age_h, 1),
                                'peak_pnl_pct': round(_prev_peak * 100, 3),
                                'threshold_pct': round(_br_max_peak * 100, 1),
                            })
                    except Exception:
                        pass

                # ── 0b. SSO: Smart Scale-Out — replaces fixed PV1 with multi-signal system ─
                _sso_enabled = getattr(settings, 'sso_enabled', True) if settings else True
                _bounce_entry_flag = pos._smart_meta.get('bounce_entry', False)
                _sso_action = None   # W2: initialize so UG can reference regardless of path
                _pr_decision = None  # W2: initialize so UG can reference regardless of path
                if _sso_enabled and (pnl_pct > 0 or _bounce_entry_flag):  # SSO-6 also runs on losses
                    try:
                        from smart_scale_out import SmartScaleOut, SSOPositionData, compute_pnl_velocity, extract_sso_level_from_reason
                        _sso_fut_total_at_open = pos._smart_meta.get('fut_total_at_open')
                        _sso_fut_total = _sso_fut_total_at_open or self.get_total_wallet_usdt()
                        if _sso_fut_total_at_open:
                            _sso_cur = self.get_total_wallet_usdt()
                            if abs(_sso_cur - _sso_fut_total_at_open) > 50:
                                print(f"[SSO] {symbol} using fut_total_at_open=${_sso_fut_total_at_open:.0f} (current=${_sso_cur:.0f}) — transfer occurred")
                        _sso = SmartScaleOut(settings, futures_total=_sso_fut_total)
                        # Compute velocity
                        _sso_snap = pos._smart_meta.get('sso_pnl_snapshot')
                        _sso_vel, _sso_new_snap = compute_pnl_velocity(pnl_pct, _sso_snap)
                        _ctx_pnl_velocity = _sso_vel
                        if _sso_new_snap != _sso_snap:
                            pos._smart_meta['sso_pnl_snapshot'] = _sso_new_snap
                            self._persist_smart_meta(pos)  # FIX: persist velocity snapshot to DB
                        # Get ATR for multilevel (4h)
                        _sso_atr = None
                        try:
                            _sso_atr = self.get_atr_pct(symbol, interval="4h", periods=14)
                        except Exception:
                            _sso_atr = self.get_atr_pct(symbol)  # fallback to 5m
                        # Get RSI/vol_spike (lightweight — reuse cached if possible)
                        _sso_rsi = None
                        _sso_vol = None
                        try:
                            _bc_ref = getattr(self, '_binance_client_ref', None)
                            if _bc_ref:
                                from autonomous_signals import AutonomousSignalGenerator
                                _sso_sg = AutonomousSignalGenerator(_bc_ref)
                                _sso_sig = _sso_sg.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
                                _sso_ind = _sso_sig.get("indicators", {}) if _sso_sig else {}
                                _sso_rsi = float(_sso_ind.get("rsi", 50))
                                _sso_vol = float(_sso_ind.get("vol_spike") or _sso_ind.get("volume_ratio") or 1.0)
                                if _sso_sig:
                                    _ctx_signal_strength = float(_sso_sig.get('strength', 0.0) or 0.0)
                                    _ctx_signal_direction = str(_sso_sig.get('signal') or '').strip().upper() or None
                                    _ctx_signal_timestamp = datetime.now(timezone.utc).isoformat()
                        except Exception:
                            pass
                        # P1.1: Get today's realized PnL from BD (thread-safe, restart-safe)
                        _sso_today_pnl = 0.0
                        try:
                            from daily_pnl_tracker import get_daily_realized_pnl as _get_dpnl
                            _sso_today_pnl = _get_dpnl(self.db_client, trade_type='futures')
                        except Exception as _dpnl_err:
                            logger.debug(f"[SSO] daily_pnl fallback 0.0: {_dpnl_err}")
                        # SSO-6 Smart Gate: compute rescue_prediction_ev + signal
                        # Only when bounce_entry flag is set and pnl is near target (avoid unnecessary API call)
                        _rescue_pred_ev = None
                        _rescue_signal = None
                        _rescue_signal_str = 0.0
                        _rescue_target_pnl = getattr(settings, 'futures_bounce_rescue_target_pnl', -0.50) if settings else -0.50
                        if _bounce_entry_flag and unr >= _rescue_target_pnl:
                            try:
                                import pnl_service as _pnl_svc_gate
                                _gate_pred, _ = _pnl_svc_gate.predict_for_symbol(
                                    symbol=symbol, client=getattr(self, '_binance_client_ref', None),
                                    settings=settings,
                                    position_size_usdt=round(entry * pos.qty, 2),
                                    precomputed_signal=(_sso_sig if '_sso_sig' in dir() else None),
                                    trade_type='futures',
                                    trade_side=('SHORT' if str(pos.side).upper() == 'SHORT' else 'LONG'),
                                )
                                if _gate_pred:
                                    _rescue_pred_ev = float(_gate_pred.get('expected_pnl', 0.0))
                            except Exception:
                                pass
                            if _sso_sig:
                                _rescue_signal = _sso_sig.get('signal')
                                _rescue_signal_str = float(_sso_sig.get('strength', 0.0))
                        _sso_pos = SSOPositionData(
                            symbol=symbol,
                            side=pos.side,
                            pnl_pct=pnl_pct,
                            pnl_usdt=unr,
                            position_size_usdt=entry * pos.qty,
                            entry_price=entry,
                            current_price=price,
                            opened_at=pos.opened_at,
                            peak_pnl_pct=pos._smart_meta.get('peak_pnl_pct', 0.0),
                            last_pnl_snapshot=_sso_snap,
                            pnl_velocity_1h=_sso_vel,
                            sso_levels_done=pos._smart_meta.get('sso_levels_done', []),
                            rsi_1h=_sso_rsi,
                            vol_spike=_sso_vol,
                            atr_4h_pct=_sso_atr,
                            today_realized_pnl=_sso_today_pnl,
                            daily_target=getattr(settings, 'sso_target_daily_usd', 40.0) if settings else 40.0,
                            trade_type="futures",
                            regime_mode=getattr(settings, 'current_regime_mode', None) if settings else None,
                            profile_label=_pos_profile,
                            bounce_entry=_bounce_entry_flag,
                            rescue_prediction_ev=_rescue_pred_ev,
                            rescue_signal=_rescue_signal,
                            rescue_signal_strength=_rescue_signal_str,
                            _gate_block_count=pos._smart_meta.get('bounce_gate_blocked_count') if _bounce_entry_flag else None,
                            _gate_last_pnl_pct=pos._smart_meta.get('bounce_gate_first_pnl_pct') if _bounce_entry_flag else None,
                        )
                        _sso_action = _sso.evaluate(_sso_pos)
                        # Full SSO visibility — always log evaluation result
                        if _sso_action.should_close:
                            print(f"[SSO/F] {symbol} {pos.side}: → CLOSE {_sso_action.close_pct*100:.0f}% "
                                  f"{_sso_action.reason} | {_sso_action.detail}")
                        else:
                            print(f"[SSO/F] {symbol} {pos.side}: NO ACTION | {_sso_action.diagnosis}")
                        # SSO-6 Smart Gate: persist bounce_gate_* state to smart_meta + audit
                        if _bounce_entry_flag:
                            _diag = _sso_action.diagnosis or ""
                            _gate_blocked_now = "GATE_BLOCKED" in _diag
                            _gate_expired_now = "gate_expired" in _diag and _sso_action.should_close
                            _now_iso = datetime.now(timezone.utc).isoformat()
                            _gate_changed = False
                            if _gate_blocked_now:
                                _block_count = pos._smart_meta.get('bounce_gate_blocked_count', 0) + 1
                                _prev_reason = pos._smart_meta.get('bounce_gate_blocked_reason', '')
                                _new_reason = _diag
                                pos._smart_meta['bounce_gate_blocked_count'] = _block_count
                                pos._smart_meta['bounce_gate_last_eval'] = _now_iso
                                pos._smart_meta['bounce_gate_blocked_reason'] = _new_reason
                                pos._smart_meta['bounce_gate_last_signal'] = _rescue_signal
                                pos._smart_meta['bounce_gate_last_signal_strength'] = _rescue_signal_str
                                pos._smart_meta['bounce_gate_last_ev'] = _rescue_pred_ev
                                pos._smart_meta['bounce_gate_last_velocity'] = _sso_vel
                                if 'bounce_gate_first_qualified_at' not in pos._smart_meta and unr >= _rescue_target_pnl:
                                    pos._smart_meta['bounce_gate_first_qualified_at'] = _now_iso
                                # Store pnl_pct at first block for expiry improvement check
                                if 'bounce_gate_first_pnl_pct' not in pos._smart_meta:
                                    pos._smart_meta['bounce_gate_first_pnl_pct'] = pnl_pct
                                _gate_changed = True
                                self._audit(symbol, 'bounce_gate_blocked', {
                                    'side': pos.side, 'pnl_usdt': round(unr, 4),
                                    'gate_blocked_by': _new_reason,
                                    'signal': _rescue_signal, 'signal_strength': _rescue_signal_str,
                                    'rescue_ev': _rescue_pred_ev, 'velocity': _sso_vel,
                                    'block_count_so_far': _block_count,
                                    'and_partial': True,
                                })
                                print(f"[SSO-6/Gate] {symbol}: BLOCKED (#{_block_count}) — {_new_reason}")
                            elif not _gate_blocked_now and pos._smart_meta.get('bounce_gate_blocked_count', 0) > 0:
                                # Gate cleared without close (pnl moved away from target) — reset expiry counter
                                pos._smart_meta['bounce_gate_blocked_count'] = 0
                                pos._smart_meta.pop('bounce_gate_first_pnl_pct', None)
                                _gate_changed = True
                            if _gate_expired_now:
                                _block_count = pos._smart_meta.get('bounce_gate_blocked_count', 0)
                                self._audit(symbol, 'bounce_gate_expired', {
                                    'side': pos.side, 'pnl_usdt': round(unr, 4),
                                    'total_blocks': _block_count, 'reason': _diag,
                                })
                                print(f"[SSO-6/Gate] {symbol}: EXPIRY — rescue forzado tras {_block_count} bloqueos")
                            if _sso_action.should_close and _sso_action.reason == 'sso_bounce_rescue':
                                _block_count = pos._smart_meta.get('bounce_gate_blocked_count', 0)
                                _first_q = pos._smart_meta.get('bounce_gate_first_qualified_at')
                                _held_min = 0
                                if _first_q:
                                    try:
                                        _fq_dt = datetime.fromisoformat(_first_q)
                                        _held_min = int((datetime.now(timezone.utc) - _fq_dt).total_seconds() / 60)
                                    except Exception:
                                        pass
                                self._audit(symbol, 'bounce_gate_fired', {
                                    'side': pos.side, 'pnl_usdt': round(unr, 4),
                                    'total_blocks_before': _block_count,
                                    'gate_held_minutes': _held_min,
                                    'final_signal': _rescue_signal, 'final_ev': _rescue_pred_ev,
                                    'rescue_reason': 'sso_bounce_rescue',
                                    'expired': _gate_expired_now,
                                })
                            if _gate_changed:
                                self._persist_smart_meta(pos)
                        if _sso_action.should_close:
                            _sso_close_pct = _sso_action.close_pct
                            _sso_qty = pos.qty * _sso_close_pct
                            _step = self._get_step_size(symbol)
                            _sso_qty_aligned = self._align_qty(_sso_qty, _step)
                            if _sso_qty_aligned > 0 and _sso_qty_aligned <= pos.qty:
                                _sso_ok = False
                                if self.dry_run:
                                    _sso_ok = True
                                else:
                                    try:
                                        _side = "BUY" if pos.side == "SHORT" else "SELL"
                                        self._post("/fapi/v1/order", {
                                            "symbol": symbol, "side": _side, "type": "MARKET",
                                            "quantity": str(_sso_qty_aligned),
                                            "positionSide": pos.side,
                                        })
                                        _sso_ok = True
                                    except Exception as _sso_err:
                                        print(f"[Futures/SSO] {symbol} {_sso_action.trigger} FAILED: {_sso_err}")
                                if _sso_ok:
                                    _sso_pnl = (entry - price) * _sso_qty_aligned if pos.side == "SHORT" else (price - entry) * _sso_qty_aligned
                                    pos.qty -= _sso_qty_aligned
                                    # Save values before clearing advisory reference
                                    _sso_done_trigger = _sso_action.trigger
                                    _sso_done_reason = _sso_action.reason
                                    _sso_action = None  # W2: clear so UG does not re-arbitrate this executed partial
                                    # Track cumulative scale-out PnL
                                    _prev_so_pnl = float(pos._smart_meta.get('scale_out_pnl', 0))
                                    pos._smart_meta['scale_out_pnl'] = _prev_so_pnl + _sso_pnl
                                    pos._smart_meta['scale_out_done'] = True
                                    pos._smart_meta['sso_last_trigger'] = _sso_done_trigger
                                    pos._smart_meta['sso_last_reason'] = _sso_done_reason
                                    # C3: reset peak baseline for residual position after partial
                                    if _sso_close_pct < 1.0:
                                        pos._smart_meta['peak_pnl_pct'] = pnl_pct
                                    # Track multilevel progress
                                    _sso_lvl = extract_sso_level_from_reason(_sso_done_reason)
                                    if _sso_lvl:
                                        _done = pos._smart_meta.get('sso_levels_done', [])
                                        if _sso_lvl not in _done:
                                            _done.append(_sso_lvl)
                                        pos._smart_meta['sso_levels_done'] = _done
                                    self._persist_smart_meta(pos)
                                    # Re-place SL/TP with reduced qty (or skip if fully closed)
                                    if pos.qty > 0:
                                        self.update_sl_tp(symbol, pos.side, new_sl=pos.sl_price, new_tp=pos.tp_price)
                                    print(f"[Futures/SSO] {symbol} {pos.side}: {_sso_done_trigger} "
                                          f"close {_sso_close_pct*100:.0f}% qty={_sso_qty_aligned:.6f} "
                                          f"PnL=${_sso_pnl:.2f}")
                                    closed.append({"symbol": symbol, "side": pos.side,
                                                   "reason": _sso_done_reason, "pnl": _sso_pnl,
                                                   "partial": _sso_close_pct < 1.0,
                                                   "sso_trigger": _sso_done_trigger})
                                    if pos.qty <= 0 or _sso_close_pct >= 1.0:
                                        # Fully closed via SSO
                                        if pos.qty > 0:
                                            _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                                            _close_fn(symbol, reason=_sso_done_reason)
                                        else:
                                            # Cancel orphaned SL/TP algo orders
                                            for _oid in [pos.sl_order_id, pos.tp_order_id]:
                                                if _oid:
                                                    try:
                                                        self._delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": _oid})
                                                    except Exception:
                                                        pass
                                            # Verify on Binance that position is actually closed
                                            self._verify_position_closed(symbol, pos.side)
                                            # Persist to DB — restore qty for correct PnL calc
                                            pos.closed = True
                                            pos.exit_price = price
                                            pos.closed_at = datetime.now(timezone.utc)
                                            pos.pnl = _sso_pnl
                                            _original_qty = _sso_qty_aligned
                                            pos.qty = _original_qty
                                            self._save_futures_closed(pos, _sso_done_reason)
                                            pos.qty = 0
                                            print(f"[Futures/SSO] {symbol} {pos.side}: DB saved ✓ (reason={_sso_done_reason}, PnL=${_sso_pnl:.2f})")
                                    if pos.qty <= 0 or pos.closed:
                                        continue  # fully closed via SSO — move to next position
                    except Exception as _sso_exc:
                        print(f"[Futures/SSO] {symbol} evaluation error: {_sso_exc}")

                # ── 0c. PV2: Momentum reversal close — close remaining after peak drawback ─
                _mr_enabled = getattr(settings, 'futures_momentum_reversal_enabled', True) if settings else True
                _mr_min_peak = getattr(settings, 'futures_momentum_reversal_min_peak', 2.0) / 100.0 if settings else 0.02
                _mr_drawback = getattr(settings, 'futures_momentum_reversal_drawback', 0.8) / 100.0 if settings else 0.008
                _current_peak = pos._smart_meta.get('peak_pnl_pct', 0.0)
                if (_mr_enabled
                        and pos._smart_meta.get('scale_out_done')
                        and _current_peak >= _mr_min_peak
                        and (_current_peak - pnl_pct) >= _mr_drawback
                        and pnl_pct > 0):  # only close if still in profit
                    _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                    _mr_result = _close_fn(symbol, reason="momentum_reversal")
                    if _mr_result:
                        print(f"[Futures/PV2] {symbol} {pos.side}: MOMENTUM REVERSAL CLOSE "
                              f"peak={_current_peak*100:.2f}% → current={pnl_pct*100:.2f}% "
                              f"(gave back {(_current_peak-pnl_pct)*100:.2f}% >= {_mr_drawback*100:.1f}%)")
                        closed.append({"symbol": symbol, "side": pos.side,
                                       "reason": "momentum_reversal", "pnl": pos.pnl or unr})
                    continue  # position closed, move to next

                # ── Profit Realization Supervisor ───────────────────────────
                _pos_live_in_binance = (not _enforce_live_gate) or (self._pos_key(symbol, pos.side) in _binance_live_keys)
                if pnl_pct > 0 and _pos_live_in_binance:
                    _pr_meta = pos._smart_meta
                    _pr_started_at = _parse_profit_dt(_pr_meta.get('profit_realization_started_at'))
                    _pr_ctx = ProfitRealizationContext(
                        symbol=symbol,
                        trade_type='futures',
                        side=pos.side,
                        pnl_pct=pnl_pct,
                        pnl_usdt=unr,
                        entry_price=entry,
                        current_price=price,
                        opened_at=pos.opened_at,
                        profit_started_at=_pr_started_at,
                        peak_pnl_pct=float(_pr_meta.get('peak_pnl_pct', 0.0) or 0.0),
                        pnl_velocity_1h=_ctx_pnl_velocity,
                        current_signal_strength=_ctx_signal_strength,
                        regime_mode=getattr(settings, 'current_regime_mode', None) if settings else None,
                        profile_label=_pos_profile,
                        breakeven_done=bool(_pr_meta.get('breakeven_done', False)),
                        trailing_active=bool(_pr_meta.get('trail_best_price')),
                        tp_extensions=int(_pr_meta.get('tp_extensions_count', 0) or 0),
                        take_profit_price=pos.tp_price,
                        metadata=_pr_meta,
                    )
                    if _mark_profit_started(_pr_meta, pnl_pct):
                        self._persist_smart_meta(pos)
                    _pr_decision = _evaluate_profit_realization(_pr_ctx, settings)
                    if _pr_decision.should_close:
                        # Always persist PRS metadata for audit/observability
                        _pr_meta.update(_pr_decision.state_patch or {})
                        _pr_meta['profit_realization_last_reason'] = _pr_decision.reason
                        _pr_meta['profit_realization_last_detail'] = _pr_decision.detail
                        _pr_meta['profit_realization_override_target'] = _pr_decision.override_target
                        self._persist_smart_meta(pos)
                        print(f"[PRS/F/advisory] {symbol} {pos.side}: {_pr_decision.reason} "
                              f"close={_pr_decision.close_pct*100:.0f}% priority={_pr_decision.priority} → passing to UG")
                        # W2: peak_protection (priority=95) bypasses UG — critical loss of significant gains
                        if _pr_decision.reason == "prs_peak_protection":
                            _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                            _pp_result = _close_fn(symbol, reason=_pr_decision.reason)
                            if _pp_result:
                                try:
                                    self._audit(symbol, 'prs_peak_protection_direct', {
                                        'side': pos.side,
                                        'reason': _pr_decision.reason,
                                        'detail': _pr_decision.detail,
                                        'diagnostics': _pr_decision.diagnostics,
                                    })
                                except Exception:
                                    pass
                                print(f"[PRS/F] {symbol} {pos.side}: PEAK PROTECTION direct close (priority=95)")
                                closed.append({"symbol": symbol, "side": pos.side,
                                               "reason": _pr_decision.reason, "pnl": pos.pnl or unr})
                            continue
                        # All other PRS decisions: advisory to UG (fall through to UG block below)

                # ── Unified Governance Supervisor (profit + loss arbitration) ─
                try:
                    if not _pos_live_in_binance:
                        self._audit(symbol, 'ug_skipped_binance_unconfirmed', {
                            'side': pos.side,
                            'reason': 'binance_not_confirmed',
                            'truth_severity': _truth_status.get('severity'),
                            'truth_mismatches': _truth_status.get('mismatch_count'),
                        })
                        continue
                    _ug_meta = pos._smart_meta or {}
                    _ug_distance_to_sl = None
                    if price > 0 and pos.sl_price:
                        if pos.side == "SHORT":
                            _ug_distance_to_sl = ((pos.sl_price - price) / price) * 100.0
                        else:
                            _ug_distance_to_sl = ((price - pos.sl_price) / price) * 100.0

                    try:
                        _lifecycle_structure = self._evaluate_lifecycle_structure(symbol, price, pos.side, settings)
                        _ug_meta['lifecycle_structure_last'] = _lifecycle_structure
                        self._persist_smart_meta(pos)
                        self._audit(symbol, 'lifecycle_structure_decision', {
                            'side': pos.side,
                            'trade_type': 'futures',
                            **_lifecycle_structure,
                        })
                    except Exception as _structure_error:
                        _lifecycle_structure = {
                            'mode': str(get_param(settings, 'lifecycle_structure_mode', 'shadow')),
                            'decision': {'state': 'unavailable', 'action': 'allow_exit', 'reason': str(_structure_error)},
                        }

                    # W2: SSO and PRS advisories — variables initialized as None before their blocks
                    _ug_sso_close = bool(_sso_action and _sso_action.should_close)
                    _ug_sso_pct = float(_sso_action.close_pct if _sso_action and _sso_action.should_close else 0.0)
                    _ug_sso_reason = str(_sso_action.reason if _sso_action and _sso_action.should_close else '')
                    _ug_sso_trigger = str(_sso_action.trigger if _sso_action and _sso_action.should_close else '')
                    _ug_prs_close = bool(_pr_decision and _pr_decision.should_close)
                    _ug_prs_pct = float(_pr_decision.close_pct if _pr_decision and _pr_decision.should_close else 0.0)
                    _ug_prs_reason = str(_pr_decision.reason if _pr_decision and _pr_decision.should_close else '')
                    _ug_prs_detail = str(_pr_decision.detail if _pr_decision and _pr_decision.should_close else '')
                    # W2/C2: Use pre-fetched F&G from cycle cache (fetched once before position loop)
                    _ug_fng_value = _cycle_fng_value
                    _ug_ctx = UnifiedGovernanceContext(
                        symbol=symbol,
                        trade_type='futures',
                        side=pos.side,
                        pnl_pct=pnl_pct,
                        pnl_usdt=unr,
                        entry_price=entry,
                        current_price=price,
                        opened_at=pos.opened_at,
                        metadata=_ug_meta,
                        regime_mode=getattr(settings, 'current_regime_mode', None) if settings else None,
                        profile_label=_pos_profile,
                        distance_to_sl_pct=_ug_distance_to_sl,
                        pnl_velocity_1h=_ctx_pnl_velocity,
                        current_signal_strength=_ctx_signal_strength,
                        current_signal_direction=_ctx_signal_direction,
                        tp_extensions=int(_ug_meta.get('tp_extensions_count', 0) or 0),
                        peak_pnl_pct=float(_ug_meta.get('peak_pnl_pct', 0.0) or 0.0),
                        profit_started_at=_parse_profit_dt(_ug_meta.get('profit_realization_started_at')),
                        breakeven_done=bool(_ug_meta.get('breakeven_done', False)),
                        trailing_active=bool(_ug_meta.get('trail_best_price')),
                        take_profit_price=pos.tp_price,
                        monitor_only=bool(monitor_only),
                        tier1_soft_reason=None,
                        sso_advisory_should_close=_ug_sso_close,
                        sso_advisory_close_pct=_ug_sso_pct,
                        sso_advisory_reason=_ug_sso_reason,
                        sso_advisory_trigger=_ug_sso_trigger,
                        prs_advisory_should_close=_ug_prs_close,
                        prs_advisory_close_pct=_ug_prs_pct,
                        prs_advisory_reason=_ug_prs_reason,
                        prs_advisory_detail=_ug_prs_detail,
                        lifecycle_structure_mode=str(_lifecycle_structure.get('mode', 'shadow')),
                        lifecycle_structure_decision=dict(_lifecycle_structure.get('decision') or {}),
                        fear_greed_value=_ug_fng_value,
                    )
                    _ug_decision = _evaluate_unified_governance(_ug_ctx, settings)
                    _ug_shadow = _evaluate_ug_loss_shadow(_ug_ctx, _ug_decision)
                    _ug_meta['unified_governance_shadow_last'] = _ug_shadow
                    self._audit(symbol, 'unified_governance_shadow_decision', {
                        'side': pos.side,
                        'trade_type': 'futures',
                        **_ug_shadow,
                    })
                    if _ug_decision.state_patch:
                        pos._smart_meta.update(_ug_decision.state_patch)
                        self._persist_smart_meta(pos)

                    self._audit(symbol, 'unified_governance_decision', {
                        'side': pos.side,
                        'action': _ug_decision.action,
                        'reason': _ug_decision.reason,
                        'detail': _ug_decision.detail,
                        'priority': _ug_decision.priority,
                        'diagnostics': _ug_decision.diagnostics,
                        'close_pct': float(_ug_decision.close_pct or 0.0),
                        'lifecycle_structure': _lifecycle_structure.get('decision') or {},
                    })

                    if _ug_decision.should_close:
                        self._audit(symbol, 'unified_governance_close_triggered', {
                            'side': pos.side,
                            'action': _ug_decision.action,
                            'reason': _ug_decision.reason,
                            'detail': _ug_decision.detail,
                            'priority': _ug_decision.priority,
                            'diagnostics': _ug_decision.diagnostics,
                            'close_pct': float(_ug_decision.close_pct or 1.0),
                        })
                        if (_ug_decision.close_pct or 1.0) < 1.0:
                            _step = self._get_step_size(symbol)
                            _ug_qty = self._align_qty(pos.qty * float(_ug_decision.close_pct), _step)
                            if _ug_qty > 0:
                                _side = "BUY" if pos.side == "SHORT" else "SELL"
                                _ug_ok = False
                                if self.dry_run:
                                    _ug_ok = True
                                else:
                                    try:
                                        self._post("/fapi/v1/order", {
                                            "symbol": symbol,
                                            "side": _side,
                                            "type": "MARKET",
                                            "quantity": str(_ug_qty),
                                            "positionSide": pos.side,
                                        })
                                        _ug_ok = True
                                    except Exception as _uge:
                                        print(f"[UnifiedGov/Futures] {symbol} partial FAILED: {_uge}")
                                if _ug_ok:
                                    _ug_pnl = (entry - price) * _ug_qty if pos.side == "SHORT" else (price - entry) * _ug_qty
                                    pos.qty -= _ug_qty
                                    self._persist_smart_meta(pos)
                                    if pos.qty > 0:
                                        self.update_sl_tp(symbol, pos.side, new_sl=pos.sl_price, new_tp=pos.tp_price)
                                    closed.append({
                                        "symbol": symbol,
                                        "side": pos.side,
                                        "reason": _ug_decision.reason,
                                        "pnl": _ug_pnl,
                                        "partial": True,
                                    })
                                    continue
                        else:
                            _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                            _ug_result = _close_fn(symbol, reason=_ug_decision.reason)
                            if _ug_result:
                                closed.append({
                                    "symbol": symbol,
                                    "side": pos.side,
                                    "reason": _ug_decision.reason,
                                    "pnl": pos.pnl or unr,
                                })
                                continue
                except Exception as _ug_err:
                    print(f"[UnifiedGov/Futures] {symbol}: evaluation error: {_ug_err}")

                # ── 1. Breakeven SL ─────────────────────────────────────────────
                if _trailing_enabled and pnl_pct >= _breakeven_act_pct / 100.0:
                    if not pos._smart_meta.get('breakeven_done'):
                        _be_ok = False
                        if pos.side == "SHORT" and entry < pos.sl_price:
                            # SHORT: SL is above entry. Move SL to entry (price must rise to entry to stop out)
                            _be_ok = self.update_sl_tp(symbol, pos.side, new_sl=entry)
                        elif pos.side == "LONG" and entry > pos.sl_price:
                            # LONG: SL is below entry. Move SL to entry
                            _be_ok = self.update_sl_tp(symbol, pos.side, new_sl=entry)
                        if _be_ok:
                            pos._smart_meta['breakeven_done'] = True
                            self._persist_smart_meta(pos)  # FIX-S1: persist to DB
                            print(f"[Futures/Breakeven] {symbol} {pos.side}: SL → entry={entry:.4f} "
                                  f"(PnL={pnl_pct*100:.2f}% >= {_breakeven_act_pct}%)")

                # ── 2. Trailing stop ────────────────────────────────────────────
                if _trailing_enabled and pnl_pct >= _trail_act_pct / 100.0:
                    # ATR-scaled trail: respects per-symbol volatility to avoid wick stop-outs
                    _trail_dist = self.compute_trail_distance(symbol, pnl_pct, settings)
                    tick = self._get_tick_size(symbol)

                    if pos.side == "SHORT":
                        # SHORT profits when price falls. "Best price" = lowest seen.
                        # Trailing SL = best_price * (1 + trail_dist) — moves down as price falls.
                        # Trailing TP = best_price * (1 - trail_dist/2) — trails TP downward.
                        _best_price = pos._smart_meta.get('trail_best_price', price)

                        # Fix 3: Initialize trail_best_price from current SL (not from price).
                        # After a restart, SL may already be trailing-moved (Fix 4 restores it).
                        # Inferring best_price from SL prevents resetting progress: best = SL / (1 + dist)
                        if 'trail_best_price' not in pos._smart_meta:
                            _inferred_best = pos.sl_price / (1.0 + _trail_dist)
                            # Only use inferred value if it's lower than current price (valid for SHORT)
                            # and lower than entry (confirms trailing already happened)
                            if _inferred_best < pos.entry_price and _inferred_best < price:
                                pos._smart_meta['trail_best_price'] = _inferred_best
                                _best_price = _inferred_best
                                self._persist_smart_meta(pos)  # FIX-S1
                                print(f"[Futures/Trail] {symbol} SHORT: trail_best_price inferred from SL={pos.sl_price:.4f} → best={_inferred_best:.4f} (PnL={pnl_pct*100:.2f}%)")
                            else:
                                pos._smart_meta['trail_best_price'] = price
                                _best_price = price
                                # FIX-S5: Immediately set trailing SL on activation (don't wait for new low)
                                _init_sl = self._align_price(_best_price * (1.0 + _trail_dist), tick)
                                if _init_sl < pos.sl_price:
                                    self.update_sl_tp(symbol, pos.side, new_sl=_init_sl)
                                    print(f"[Futures/Trail] {symbol} SHORT: Trailing activated → SL={_init_sl:.4f} "
                                          f"(best={_best_price:.4f}, PnL={pnl_pct*100:.2f}%)")
                                else:
                                    print(f"[Futures/Trail] {symbol} SHORT: Trailing activated at {price:.4f} (PnL={pnl_pct*100:.2f}%)")
                                self._persist_smart_meta(pos)  # FIX-S1

                        if price < _best_price:  # new low = new best for SHORT
                            _best_price = price
                            pos._smart_meta['trail_best_price'] = _best_price
                            # New SL = best_price * (1 + trail_dist) — stays above best price
                            _new_sl = self._align_price(_best_price * (1.0 + _trail_dist), tick)
                            # Only lower SL (SHORT: lower sl_price = tighter stop = more profit protected)
                            if _new_sl < pos.sl_price:
                                self.update_sl_tp(symbol, pos.side, new_sl=_new_sl)
                                print(f"[Futures/Trail] {symbol} SHORT: SL trailed down → {_new_sl:.4f} "
                                      f"(best={_best_price:.4f}, dist={_trail_dist_pct}%)")
                            # Also trail TP downward (lock in more profit on deep moves)
                            _new_tp = self._align_price(_best_price * (1.0 - _trail_dist / 2.0), tick)
                            if _new_tp < pos.tp_price:  # only move TP toward more profit
                                self.update_sl_tp(symbol, pos.side, new_tp=_new_tp)
                                print(f"[Futures/Trail] {symbol} SHORT: TP trailed → {_new_tp:.4f}")
                            self._persist_smart_meta(pos)  # FIX-S1: persist trail update

                    elif pos.side == "LONG":
                        # LONG profits when price rises. "Best price" = highest seen.
                        _best_price = pos._smart_meta.get('trail_best_price', price)
                        # Fix 3 (LONG): infer trail_best_price from SL on first activation after restart
                        # For LONG: SL = best_price * (1 - dist) → best = SL / (1 - dist)
                        if 'trail_best_price' not in pos._smart_meta:
                            _inferred_best = pos.sl_price / (1.0 - _trail_dist) if (1.0 - _trail_dist) > 0 else price
                            if _inferred_best > pos.entry_price and _inferred_best > price:
                                pos._smart_meta['trail_best_price'] = _inferred_best
                                _best_price = _inferred_best
                                self._persist_smart_meta(pos)  # FIX-S1
                                print(f"[Futures/Trail] {symbol} LONG: trail_best_price inferred from SL={pos.sl_price:.4f} → best={_inferred_best:.4f} (PnL={pnl_pct*100:.2f}%)")
                            else:
                                pos._smart_meta['trail_best_price'] = price
                                _best_price = price
                                # FIX-S5: Immediately set trailing SL on activation
                                _init_sl = self._align_price(_best_price * (1.0 - _trail_dist), tick)
                                if _init_sl > pos.sl_price:
                                    self.update_sl_tp(symbol, pos.side, new_sl=_init_sl)
                                    print(f"[Futures/Trail] {symbol} LONG: Trailing activated → SL={_init_sl:.4f} "
                                          f"(best={_best_price:.4f}, PnL={pnl_pct*100:.2f}%)")
                                else:
                                    print(f"[Futures/Trail] {symbol} LONG: Trailing activated at {price:.4f} (PnL={pnl_pct*100:.2f}%)")
                                self._persist_smart_meta(pos)  # FIX-S1
                        if price > _best_price:  # new high = new best for LONG
                            _best_price = price
                            pos._smart_meta['trail_best_price'] = _best_price
                            _new_sl = self._align_price(_best_price * (1.0 - _trail_dist), tick)
                            if _new_sl > pos.sl_price:  # only raise SL (LONG: protect more)
                                self.update_sl_tp(symbol, pos.side, new_sl=_new_sl)
                                print(f"[Futures/Trail] {symbol} LONG: SL trailed up → {_new_sl:.4f}")
                            self._persist_smart_meta(pos)  # FIX-S1: persist trail update

                # ── Re-read pos.sl_price / pos.tp_price (may have changed above) ──
                # Note: pos is a reference — updates in update_sl_tp() are reflected here.

                # ── 3. Check SL hit ─────────────────────────────────────────────
                if pos.is_sl_hit(price):
                    result = (self.close_long(symbol, reason="stop_loss")
                              if pos.side == "LONG"
                              else self.close_short(symbol, reason="stop_loss"))
                    if result:
                        closed.append({"symbol": symbol, "side": pos.side,
                                       "reason": "stop_loss", "pnl": pos.pnl or unr})
                    continue  # done with this position

                # ── 4. Check TP hit — Smart TP EXTEND or close ───────────────
                if pos.is_tp_hit(price):
                    _ext_count = pos._smart_meta.get('tp_extensions_count', 0)
                    _can_extend = (_smart_tp
                                   and _ext_count < _smart_tp_max_ext
                                   and pnl_pct < _smart_tp_max_pnl / 100.0)

                    if _can_extend:
                        try:
                            _pos_size_real = float(pos.qty) * float(pos.entry_price)
                            _score, _details = self._evaluate_short_momentum(symbol, settings, position_size_usdt=_pos_size_real)
                        except Exception as _me:
                            print(f"[Futures/SmartTP] {symbol}: momentum eval failed: {_me} — closing")
                            _score, _details = -1, {}

                        print(f"[Futures/SmartTP] {symbol} {pos.side}: PnL={pnl_pct*100:.2f}% "
                              f"score={_score}/4 ext={_ext_count}/{_smart_tp_max_ext} "
                              f"→ {'EXTEND' if _score >= _smart_tp_threshold else 'CLOSE'}")

                        if _score >= _smart_tp_threshold:
                            tick = self._get_tick_size(symbol)
                            # BUG-K5 fix: dynamic SL distance = 40% of configured SL (not hardcoded 0.5%)
                            # Prevents immediate SL hit after extension on volatile candles
                            _sl_pct_raw = get_param(settings, 'futures_sl_pct') if settings else 3.33
                            _smart_sl_buffer = _sl_pct_raw * 0.40 / 100.0  # 40% of configured SL %
                            if pos.side == "SHORT":
                                # Extend: lower TP (SHORT profits on price drop)
                                _new_tp = self._align_price(price * (1.0 - _smart_tp_ext_pct / 100.0), tick)
                                # Raise SL above current price by dynamic buffer (lock in gains)
                                _new_sl = self._align_price(price * (1.0 + _smart_sl_buffer), tick)
                                # Only move SL if new_sl is lower than current sl (SHORT: lower sl_price = tighter stop)
                                _update_sl = _new_sl < pos.sl_price
                            else:  # LONG
                                _new_tp = self._align_price(price * (1.0 + _smart_tp_ext_pct / 100.0), tick)
                                _new_sl = self._align_price(price * (1.0 - _smart_sl_buffer), tick)
                                _update_sl = _new_sl > pos.sl_price

                            pos._smart_meta['tp_extensions_count'] = _ext_count + 1

                            self.update_sl_tp(
                                symbol, pos.side,
                                new_sl=_new_sl if _update_sl else None,
                                new_tp=_new_tp
                            )
                            self._persist_smart_meta(pos)  # FIX-S1: persist SmartTP ext
                            print(f"[Futures/SmartTP] EXTEND {symbol} {pos.side}: "
                                  f"TP → {pos.tp_price:.4f}, SL → {pos.sl_price:.4f} "
                                  f"(ext {_ext_count+1}/{_smart_tp_max_ext})")
                            continue  # do NOT close — let it run

                    # Not extending — close the position
                    _close_fn = self.close_long if pos.side == "LONG" else self.close_short
                    result = _close_fn(symbol, reason="take_profit")
                    if result:
                        _reason = (f"smart_tp_take_profit (ext={_ext_count})" if _ext_count > 0
                                   else "take_profit")
                        closed.append({"symbol": symbol, "side": pos.side,
                                       "reason": _reason, "pnl": pos.pnl or unr})

            except Exception as e:
                print(f"[Futures] check_positions {key}: {e}")
        return closed

    def _evaluate_short_momentum(self, symbol: str, settings, position_size_usdt: float = None) -> tuple:
        """
        Evaluate whether the current SHORT momentum justifies extending the TP.

        For a SHORT to be extended:
          - vol_spike < 1.0 (low buying volume — sellers still in control)
          - RSI 4h > 60 (overbought / still elevated — shorts benefit)
          - trend_4h == DOWN (confirmed bear)
          - success_prob < 0.45 (low probability of reversal)

        Score: 1 point per bearish SHORT condition (max 4).
        Returns (score: int, details: dict). -1 score = evaluation failed.
        """
        details = {"vol_spike": 1.0, "rsi": 50.0, "trend_4h": 0.0, "success_prob": 0.5}
        try:
            from autonomous_signals import AutonomousSignalGenerator
            from binance_client import BinanceClient as _BC

            # Re-use an existing BinanceClient if possible (avoid creating a new one)
            _bc = getattr(self, '_binance_client_ref', None)
            if _bc is None:
                return -1, details  # no client available, safe fallback

            sig_gen = AutonomousSignalGenerator(_bc)
            sig = sig_gen.analyze_symbol(symbol, interval='1h', use_multi_timeframe=True)
            ind = sig.get("indicators", {})

            vol_spike  = float(ind.get("vol_spike") or ind.get("volume_ratio") or 1.0)
            rsi        = float(ind.get("rsi") or 50.0)
            trend_4h   = float(ind.get("trend_4h") or 0.0)  # 1.0=UP, 0.0=DOWN

            # Run predictor to get success probability
            import pnl_service as _pnl_service_module
            _default_size = float(getattr(settings, 'futures_usdt_per_trade', 150.0))
            _max_size = float(getattr(settings, 'max_per_trade_usdt', 500.0))
            _effective_size = min(position_size_usdt, _max_size) if position_size_usdt else _default_size
            prediction, _ = _pnl_service_module.predict_for_symbol(
                symbol=symbol, client=_bc, settings=settings,
                position_size_usdt=_effective_size,
                trade_type='futures',
                trade_side='SHORT',
            )
            success_prob = float((prediction or {}).get("success_probability", 0.5))

            details.update({
                "vol_spike": round(vol_spike, 3),
                "rsi": round(rsi, 2),
                "trend_4h": round(trend_4h, 2),
                "success_prob": round(success_prob, 4),
            })

            # Score for SHORT extension — each point = SHORT-friendly condition
            score = 0
            if vol_spike < 1.2:     score += 1  # low buying pressure
            if rsi > 55.0:          score += 1  # price still elevated (short is winning)
            if trend_4h < 0.5:      score += 1  # 4h trend is DOWN (0.0=DOWN, 1.0=UP)
            if success_prob < 0.48: score += 1  # predictor says low upside probability

            return score, details

        except Exception as _e:
            print(f"[Futures/SmartTP] Momentum eval error for {symbol}: {_e}")
            return -1, details

    def get_open_positions_summary(self) -> List[dict]:
        """Summary of all tracked open positions (LONG and SHORT) for dashboard.
        FIX-L1: enriches pnl_prediction and opened_at from trades_open DB when fields
        are missing in memory (e.g. after container restart where _sync_open_positions
        rebuilds from Binance without prediction data).
        """
        # FIX-L1: build a DB lookup map {(symbol, 'SELL'|'BUY'): row} for enrichment
        _db_map: dict = {}
        _db_first_entry_map: dict = {}
        if self.db_client:
            try:
                rows = self.db_client.execute_query(
                    """SELECT symbol, side, pnl_prediction, indicators_at_entry, entry_time, metadata
                       FROM trades_open
                       WHERE metadata->>'trade_type' = 'futures'
                       ORDER BY entry_time ASC"""
                )
                for r in rows:
                    _k = (r['symbol'], r['side'])
                    _db_map[_k] = r
                    if _k not in _db_first_entry_map:
                        _meta = _normalize_metadata(r.get('metadata'), default_trade_type='futures')
                        _first = _parse_utc_datetime(_meta.get('first_entry_time')) or _parse_utc_datetime(r.get('entry_time'))
                        _db_first_entry_map[_k] = _first
            except Exception as _dbe:
                print(f'[Futures/DB] get_open_positions_summary enrichment failed: {_dbe}')

        result = []
        for key, pos in self._open_positions.items():
            if pos.closed:
                continue
            try:
                price = self.get_price(pos.symbol)
                _unr_gross = pos.unrealized_pnl(price)
                # C0: dashboard shows net PnL after Binance fees
                from binance_commission import net_pnl as _f_net_pnl_summary
                unr = _f_net_pnl_summary(_unr_gross, pos.qty, pos.entry_price, price, 'futures', self._cached_settings)
            except Exception:
                price = pos.entry_price
                unr   = 0.0

            # FIX-L1: enrich from DB when in-memory fields are missing
            _pnl_pred = pos.pnl_prediction
            _opened_at = pos.opened_at.isoformat()
            _db_side = 'SELL' if pos.side == 'SHORT' else 'BUY'
            _db_row = _db_map.get((pos.symbol, _db_side))
            _first_dt = _db_first_entry_map.get((pos.symbol, _db_side))
            if _db_row:
                if _pnl_pred is None and _db_row.get('pnl_prediction'):
                    _pnl_pred = _db_row['pnl_prediction']
                    if isinstance(_pnl_pred, str):
                        try:
                            import json as _json
                            _pnl_pred = _json.loads(_pnl_pred)
                        except Exception:
                            _pnl_pred = None
                if _db_row.get('entry_time'):
                    try:
                        _et = _db_row['entry_time']
                        if hasattr(_et, 'isoformat'):
                            _opened_at = _et.isoformat()   # datetime obj → proper ISO with T
                        elif _et:
                            _opened_at = str(_et).replace(' ', 'T', 1)  # string fallback
                    except Exception:
                        pass

            _first_opened_at = _first_dt.isoformat() if _first_dt and hasattr(_first_dt, 'isoformat') else _opened_at

            # VIS-1: SL/TP lifecycle fields from _smart_meta
            _pnl_pct = unr / (pos.entry_price * pos.qty) if pos.entry_price > 0 and pos.qty > 0 else 0.0
            _trail_dist_now = 0.015  # default 1.5%
            if _pnl_pct >= 0.05:
                _trail_dist_now = 0.007
            elif _pnl_pct >= 0.03:
                _trail_dist_now = 0.010

            # FIX-S3b: Infer trailing/breakeven when _smart_meta is empty (same as app.py FIX-S3)
            _vis_be = pos._smart_meta.get('breakeven_done', False)
            _vis_trail_active = 'trail_best_price' in pos._smart_meta
            _vis_trail_best = pos._smart_meta.get('trail_best_price')
            if not _vis_be:
                if pos.side == "SHORT" and pos.sl_price <= pos.entry_price:
                    _vis_be = True
                elif pos.side == "LONG" and pos.sl_price >= pos.entry_price:
                    _vis_be = True
            if not _vis_trail_active and _pnl_pct >= 0.015:
                _vis_trail_active = True
                if not _vis_trail_best:
                    _vis_trail_best = price

            result.append({
                "symbol":           pos.symbol,
                "side":             pos.side,
                "qty":              pos.qty,
                "entry_price":      pos.entry_price,
                "current_price":    price,
                "sl_price":         pos.sl_price,
                "tp_price":         pos.tp_price,
                "unrealized_pnl":   round(unr, 4),
                "opened_at":        _opened_at,
                "first_opened_at":  _first_opened_at,
                "pnl_prediction":   _pnl_pred,
                "breakeven_done":   _vis_be,
                "trail_best_price": _vis_trail_best,
                "trailing_active":  _vis_trail_active,
                "tp_extensions":    pos._smart_meta.get('tp_extensions_count', 0),
                "dynamic_trail_dist_pct": round(_trail_dist_now * 100, 2),
                "scale_out_done":   bool(pos._smart_meta.get('scale_out_done', False)),
                "scale_out_pnl":    round(float(pos._smart_meta.get('scale_out_pnl', 0)), 4),
                "peak_pnl_pct":     round(float(pos._smart_meta.get('peak_pnl_pct', 0)) * 100, 2),
                "sso_levels_done":  pos._smart_meta.get('sso_levels_done', []),
                "sso_last_trigger": pos._smart_meta.get('sso_last_trigger', ''),
                "bounce_entry":     bool(pos._smart_meta.get('bounce_entry', False)),
                "bounce_gate_blocked_count": int(pos._smart_meta.get('bounce_gate_blocked_count', 0)),
                "bounce_gate_blocked_reason": pos._smart_meta.get('bounce_gate_blocked_reason'),
                "bounce_gate_last_signal": pos._smart_meta.get('bounce_gate_last_signal'),
                "bounce_gate_last_signal_strength": pos._smart_meta.get('bounce_gate_last_signal_strength'),
                "bounce_gate_last_ev": pos._smart_meta.get('bounce_gate_last_ev'),
                "bounce_gate_last_velocity": pos._smart_meta.get('bounce_gate_last_velocity'),
                "bounce_gate_first_qualified_at": pos._smart_meta.get('bounce_gate_first_qualified_at'),
                "btc_at_entry":     pos._smart_meta.get('btc_price_at_entry'),
                "btc_chg_since_entry_pct": round(
                    (self.get_price('BTCUSDT') - pos._smart_meta['btc_price_at_entry'])
                    / pos._smart_meta['btc_price_at_entry'] * 100, 2
                ) if pos._smart_meta.get('btc_price_at_entry') else None,
            })

        # Fase 2.2: advisory futures reconciliation report (dry-run by default)
        try:
            if not self.dry_run:
                _rec_report = auto_reconcile_futures(self, self.db_client, dry_run=True)
                if _rec_report.get("mismatch_count", 0) > 0:
                    print(f"[Futures/Reconcile] advisory report: {_rec_report['mismatch_count']} mismatches")
        except Exception as _rec_err:
            print(f"[Futures/Reconcile] advisory reconciliation failed: {_rec_err}")

        return result
