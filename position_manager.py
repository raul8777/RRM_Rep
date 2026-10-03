"""
Position Manager Module

Manages open and closed trades, tracks PnL, and handles automatic position closures.
"""

import json
import logging
import os
import threading
import time
import uuid
from decimal import Decimal
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field


import error_collector as ec
from config_helpers import get_param
from close_contract import apply_close_contract
from side_utils import is_long_side
from trade_utils import has_recent_binance_sell_proof


def _parse_exit_time(t) -> datetime:
    """Normalize exit_time to a timezone-aware UTC datetime regardless of input type."""
    if isinstance(t, datetime):
        return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)
    if isinstance(t, str):
        return datetime.fromisoformat(t.replace('Z', '+00:00'))
    return datetime.min.replace(tzinfo=timezone.utc)

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _stable_position_id(row: Dict[str, Any]) -> str:
    """Resolve the persisted position identity when hydrating an open row."""
    position_id = row.get("position_id")
    if position_id:
        return str(position_id)
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if isinstance(metadata, dict) and metadata.get("position_id"):
        return str(metadata["position_id"])
    return str(uuid.uuid4())


# Treat residual quantities below this threshold as zero / full close.
_QTY_EPS = 1e-9

# Assets that should never be imported as SPOT positions from Binance balance sync.
# Includes fiat/stablecoin tokens and testnet/demo "dust" assets that can appear
# in a demo wallet but are not tradeable candidates.
_DEFAULT_EXCLUDED_BALANCE_ASSETS: set = {
    # Stablecoins / fiat-like tokens
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDD", "GUSD", "UUSD", "USD1",
    "PAXG", "XAUT", "RLUSD", "SPCXB",
    # Fiat testnet placeholders / non-tradeable symbols
    "TRY", "ZAR", "UAH", "BRL", "ARS", "JPY", "MXN", "COP", "IDR", "KZT", "AED", "USD",
    # Common demo-account "dust" assets with no real liquidity
    "BTTC", "WIN", "JST", "IQ", "SPELL", "FLOKI", "PEPE", "DOGS", "HMSTR",
    "1000SATS", "BONK", "XEC", "LUNC", "SHIB",
    # Illiquid/low-cap tokens observed in staging demo wallet
    "PYR", "HFT", "PIVX", "VIC", "VANRY", "ACX",
    # Known invalid/non-tradeable assets
    "456", "这是测试币",
}


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    side: str
    quantity: float
    entry_price: float
    entry_time: str
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_id: Optional[int] = None
    metadata: Optional[Dict] = None
    entry_commission: Optional[float] = None  # Commission paid on entry
    pnl_prediction: Optional[Dict] = None  # PnL prediction at entry
    indicators_at_entry: Optional[Dict] = None  # Technical indicators at entry
    db_id: Optional[int] = None  # Primary key in trades_open table


@dataclass
class ClosedPosition:
    """Represents a closed trading position."""
    symbol: str
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    pnl: float
    pnl_percentage: float
    close_reason: str
    position_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    close_group_id: Optional[str] = None
    strategy_id: Optional[int] = None
    metadata: Optional[Dict] = None
    entry_commission: Optional[float] = None  # Commission paid on entry
    exit_commission: Optional[float] = None  # Commission paid on exit
    pnl_prediction: Optional[Dict] = None  # PnL prediction at entry
    indicators_at_entry: Optional[Dict] = None  # Technical indicators at entry
    prediction_accuracy: Optional[float] = None  # (PnL real - PnL predicted) / PnL predicted


class PositionManager:
    """Manages trading positions and lifecycle."""
    
    def __init__(self, db_client=None):
        """
        Initialize Position Manager.
        
        Args:
            db_client: Database client for persistent storage (optional)
        """
        self.db_client = db_client
        self._open_positions: Dict[str, List[Position]] = {}
        self._closed_positions: List[ClosedPosition] = []
        # Performance metrics cache: (result, monotonic_ts)
        self._perf_cache: Optional[tuple] = None
        self._perf_cache_ttl = 60.0  # seconds
        self._missing_balance_streak: Dict[str, int] = {}
        # s113: SPOT never had the equivalent of futures_client.py's `_close_lock`
        # (added in s105h) serializing close-persistence across concurrent callers —
        # the main trade cycle (SSO/PRS/UG, every 3-45min) and
        # `hard_risk_monitor.monitor_spot_hard_stops_once` (its own thread, every 2s)
        # can both call `close_position` for the same position with no mutual
        # exclusion, risking the same double-close-in-`trades_closed` bug already
        # found and fixed on the futures side (HYPERUSDT/ONDOUSDT/WIFUSDT/DOGEUSDT,
        # s110-s112). One lock across symbols (not per-symbol) mirrors the futures
        # design: closes are rare enough that a single lock costs negligible latency.
        self._close_lock = threading.RLock()
        logger.info("Position Manager initialized")
        
        # Load positions from database if available
        if self.db_client:
            self._load_positions_from_db()
    
    @staticmethod
    def _position_age_minutes(position: Position) -> float:
        """Return position age in minutes. Missing entry_time is treated as very old."""
        try:
            entry = getattr(position, 'entry_time', None)
            if not entry:
                return 1_000_000.0
            entry_dt = datetime.fromisoformat(str(entry).replace('Z', '+00:00'))
            return (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
        except Exception:
            return 1_000_000.0

    def _audit(self, symbol: str, event: str, detail: dict, binance_confirmed: bool = False) -> None:
        """FIX-9: Write a traceability record to trades_audit for SPOT events."""
        if not self.db_client:
            return
        original_event = event
        if len(event) > 40:
            event = event[:40]
            logger.warning(
                f"SPOT audit event truncated ({symbol}): '{original_event}' -> '{event}'"
            )
        try:
            self.db_client.execute_update(
                "INSERT INTO trades_audit (symbol, trade_type, event, detail, binance_confirmed) "
                "VALUES (%s, %s, %s, %s, %s)",
                (symbol, 'spot', event, json.dumps(_json_safe(detail)), binance_confirmed)
            )
        except Exception as e:
            # Keep trading flow alive, but surface traceability persistence failures.
            logger.error(f"SPOT audit write failed ({symbol}:{event}): {e}")
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR,
                         f"SPOT audit write failed ({symbol}:{event}): {e}")

    def _failed_entry_context(self, symbol: str, quantity: float) -> Optional[Dict[str, Any]]:
        if not self.db_client:
            return None
        try:
            rows = self.db_client.execute_query(
                "SELECT detail FROM trades_audit WHERE symbol=%s AND event='entry_persistence_failed' "
                "AND ts >= NOW() - INTERVAL '2 hours' ORDER BY ts DESC LIMIT 5",
                (symbol,),
            )
            for row in rows or []:
                detail = row.get("detail") or {}
                if isinstance(detail, str):
                    detail = json.loads(detail)
                if not isinstance(detail, dict):
                    continue
                failed_qty = float(detail.get("quantity") or 0.0)
                tolerance = max(1e-8, abs(quantity) * 0.01)
                metadata = detail.get("entry_metadata") or {}
                if (
                    abs(failed_qty - float(quantity)) <= tolerance
                    and isinstance(metadata, dict)
                    and bool(metadata.get("binance_entry_confirmed"))
                    and metadata.get("entry_order_id")
                ):
                    return _json_safe(metadata)
        except Exception as exc:
            logger.warning("Could not recover failed entry context for %s: %s", symbol, exc)
        return None

    def _load_positions_from_db(self) -> None:
        """Load open positions from database."""
        try:
            # Load ALL open positions (multiple per symbol supported)
            query = """
                SELECT id, position_id, symbol, side, quantity, price as entry_price,
                       strategy_id, entry_time, stop_loss, take_profit, metadata,
                       entry_commission, pnl_prediction, indicators_at_entry
                FROM trades_open
                WHERE metadata->>'trade_type' = 'spot'
                ORDER BY symbol, entry_time ASC
            """
            results = self.db_client.execute_query(query)
            self._open_positions = {}
            for row in results:
                position = Position(
                    symbol=row['symbol'],
                    side=row['side'],
                    quantity=row['quantity'],
                    entry_price=row['entry_price'],
                    entry_time=row['entry_time'],
                    position_id=_stable_position_id(row),
                    stop_loss=row['stop_loss'],
                    take_profit=row['take_profit'],
                    strategy_id=row['strategy_id'],
                    metadata=row['metadata'],
                    entry_commission=row.get('entry_commission'),
                    pnl_prediction=row.get('pnl_prediction'),
                    indicators_at_entry=row.get('indicators_at_entry'),
                    db_id=row.get('id')
                )
                # Ignore and clean up stale rows left by partial closes with zero residual qty.
                if float(position.quantity or 0) <= _QTY_EPS:
                    logger.warning(
                        f"Removing stale zero-quantity open position {position.symbol} (id={row.get('id')})"
                    )
                    if self.db_client and row.get('id'):
                        try:
                            self.db_client.execute_update(
                                "DELETE FROM trades_open WHERE id = %s", (row['id'],)
                            )
                        except Exception as _del_err:
                            logger.warning(f"Could not delete stale row {row.get('id')}: {_del_err}")
                    continue
                if position.symbol not in self._open_positions:
                    self._open_positions[position.symbol] = []
                self._open_positions[position.symbol].append(position)
            total = sum(len(v) for v in self._open_positions.values())
            logger.info(f"Loaded {total} open positions ({len(self._open_positions)} symbols) from database")
            try:
                _unknown_rows = self.db_client.execute_query(
                    "SELECT COUNT(*) AS c FROM trades_open WHERE metadata->>'trade_type' IS NULL"
                )
                _unknown = int((_unknown_rows or [{}])[0].get('c') or 0)
                if _unknown > 0:
                    logger.warning("Loaded positions with strict trade_type='spot'; found %s open rows with missing trade_type", _unknown)
                    ec.add_error(
                        ec.SOURCE_DB,
                        ec.LEVEL_WARNING,
                        f"trades_open rows with missing trade_type: {_unknown}",
                    )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to load positions from database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to load open positions: {e}")

        try:
            
            # Load closed positions (last 100)
            query = """
                SELECT symbol, side, quantity, entry_price, exit_price,
                       entry_time, exit_time, pnl, pnl_percentage, close_reason,
                       strategy_id, metadata, entry_commission, exit_commission,
                       pnl_prediction, indicators_at_entry, prediction_accuracy
                FROM trades_closed
                WHERE COALESCE(is_corrupted, FALSE) = FALSE
                ORDER BY exit_time DESC
                LIMIT 100
            """
            results = self.db_client.execute_query(query)
            for row in results:
                closed_position = ClosedPosition(
                    symbol=row['symbol'],
                    side=row['side'],
                    quantity=row['quantity'],
                    entry_price=row['entry_price'],
                    exit_price=row['exit_price'],
                    entry_time=row['entry_time'],
                    exit_time=row['exit_time'],
                    pnl=row['pnl'],
                    pnl_percentage=row['pnl_percentage'],
                    close_reason=row['close_reason'],
                    strategy_id=row['strategy_id'],
                    metadata=row['metadata'],
                    entry_commission=row.get('entry_commission'),
                    exit_commission=row.get('exit_commission'),
                    pnl_prediction=row.get('pnl_prediction'),
                    indicators_at_entry=row.get('indicators_at_entry'),
                    prediction_accuracy=row.get('prediction_accuracy')
                )
                self._closed_positions.append(closed_position)
            logger.info(f"Loaded {len(self._closed_positions)} closed positions from database")
        except Exception as e:
            logger.error(f"Failed to load closed positions from database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to load closed positions: {e}")

    def add_position(self, position: Position) -> bool:
        """
        Add a new open position.
        
        Args:
            position: Position to add
        """
        if position.symbol not in self._open_positions:
            self._open_positions[position.symbol] = []
        # ── SPOT Escalamiento tracking ──────────────────────────────────────
        _esc_idx = len(self._open_positions[position.symbol]) + 1
        if position.metadata is None:
            position.metadata = {}
        # Ensure trade_type is always persisted for parity/reporting.
        if isinstance(position.metadata, dict):
            position.metadata.setdefault('trade_type', 'spot')
            position.metadata.setdefault(
                'regime_at_entry',
                position.metadata.get('entry_regime')
                or position.metadata.get('regime_mode')
                or position.metadata.get('current_regime_mode'),
            )
            position.metadata.setdefault(
                'profile_at_entry',
                position.metadata.get('peak_quality_profile')
                or position.metadata.get('entry_profile_label')
                or position.metadata.get('profile_label')
                or position.metadata.get('current_profile_label'),
            )
            if not position.metadata.get('config_snapshot_id') and self.db_client:
                try:
                    _snap_rows = self.db_client.execute_query(
                        "SELECT value FROM trading_runtime_state WHERE key='config_snapshot_latest' LIMIT 1"
                    )
                    if _snap_rows:
                        _snap_val = _snap_rows[0].get('value')
                        if isinstance(_snap_val, str):
                            try:
                                _snap_val = json.loads(_snap_val)
                            except Exception:
                                _snap_val = {'snapshot_id': _snap_val}
                        if isinstance(_snap_val, dict):
                            _snapshot_id = str(_snap_val.get('snapshot_id') or _snap_val.get('id') or '').strip()
                        else:
                            _snapshot_id = str(_snap_val or '').strip()
                        if _snapshot_id:
                            position.metadata['config_snapshot_id'] = _snapshot_id
                except Exception:
                    pass
        position.metadata['escalonamiento_index'] = _esc_idx
        position.metadata['es_escalonamiento'] = (_esc_idx > 1)
        # Update total_escalonamientos on all existing entries for this symbol
        if _esc_idx > 1:
            for _prev in self._open_positions[position.symbol]:
                if isinstance(_prev.metadata, dict):
                    _prev.metadata['total_escalonamientos'] = _esc_idx
                    if self.db_client and getattr(_prev, 'db_id', None):
                        try:
                            import json as _j
                            self.db_client.execute_update(
                                "UPDATE trades_open SET metadata = metadata || %s::jsonb WHERE id = %s",
                                (_j.dumps({'total_escalonamientos': _esc_idx}), _prev.db_id)
                            )
                        except Exception:
                            pass
        position.metadata['total_escalonamientos'] = _esc_idx
        position.metadata = _json_safe(position.metadata)
        position.pnl_prediction = _json_safe(position.pnl_prediction)
        position.indicators_at_entry = _json_safe(position.indicators_at_entry)
        from position_thesis import build_thesis_snapshot
        position.metadata = dict(position.metadata or {})
        position.metadata.setdefault('position_id', position.position_id)
        position.metadata.setdefault('thesis_id', str(uuid.uuid4()))
        position.metadata.setdefault('close_group_id', str(uuid.uuid4()))
        thesis = build_thesis_snapshot(
            metadata=position.metadata,
            pnl_prediction=position.pnl_prediction,
            indicators_at_entry=position.indicators_at_entry,
            side=position.side,
            regime_mode=position.metadata.get('regime_at_entry') or position.metadata.get('regime_mode'),
        )
        if thesis:
            position.metadata['thesis_snapshot_v1'] = thesis

        if self.db_client:
            try:
                query = """
                    INSERT INTO trades_open
                    (symbol, side, quantity, price, strategy_id, entry_time, stop_loss, take_profit,
                     metadata, entry_commission, pnl_prediction, indicators_at_entry,
                     position_id, thesis_id, close_group_id, entry_order_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                self.db_client.execute_update(
                    query,
                    (
                        position.symbol,
                        position.side,
                        position.quantity,
                        position.entry_price,
                        position.strategy_id,
                        position.entry_time,
                        position.stop_loss,
                        position.take_profit,
                        json.dumps(position.metadata),
                        position.entry_commission,
                        json.dumps(position.pnl_prediction) if position.pnl_prediction is not None else None,
                        json.dumps(position.indicators_at_entry) if position.indicators_at_entry is not None else None,
                        position.position_id,
                        position.metadata['thesis_id'],
                        position.metadata['close_group_id'],
                        position.metadata.get('entry_order_id') or position.metadata.get('order_id'),
                    )
                )
                inserted = self.db_client.execute_query(
                    "SELECT id FROM trades_open WHERE position_id = %s ORDER BY id DESC LIMIT 1",
                    (position.position_id,),
                )
                if inserted:
                    position.db_id = int(inserted[0]["id"])
                logger.info(f"Position added to database: {position.symbol}")
            except Exception as exc:
                logger.error(f"Failed to save position to database: {exc}")
                ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to save position {position.symbol}: {exc}")
                _failed_metadata = position.metadata if isinstance(position.metadata, dict) else {}
                self._audit(position.symbol, 'entry_persistence_failed', {
                    'reason': str(exc),
                    'quantity': float(position.quantity),
                    'entry_price': float(position.entry_price),
                    'entry_time': position.entry_time,
                    'entry_order_id': str(_failed_metadata.get('entry_order_id', _failed_metadata.get('order_id', ''))),
                    'entry_metadata': _failed_metadata,
                }, binance_confirmed=bool(_failed_metadata.get('binance_entry_confirmed', False)))
                return False

        self._open_positions[position.symbol].append(position)
        _pos_meta = position.metadata if isinstance(position.metadata, dict) else {}
        self._audit(position.symbol, 'open', {
            'side': position.side, 'qty': float(position.quantity), 'price': float(position.entry_price),
            'sl': float(position.stop_loss or 0), 'tp': float(position.take_profit or 0),
            'esc_index': _esc_idx,
            'order_id': str(_pos_meta.get('entry_order_id', _pos_meta.get('order_id', ''))),
            'signal_id': str(_pos_meta.get('signal_id', '')),
            'binance_confirmed': bool(_pos_meta.get('binance_entry_confirmed', False)),
        }, binance_confirmed=bool(_pos_meta.get('binance_entry_confirmed', False)))
        logger.info(f"Position opened: {position.symbol} {position.side} {position.quantity} @ {position.entry_price}")
        return True
    
    def get_position(self, symbol: str) -> Optional[List[Position]]:
        """
        Get all open positions for a symbol.

        Args:
            symbol: Trading pair symbol

        Returns:
            List of Position objects if any open, None otherwise
        """
        positions = self._open_positions.get(symbol)
        return positions if positions else None

    def get_positions_count(self, symbol: str) -> int:
        """
        Return the number of open positions for a symbol.
        """
        return len(self._open_positions.get(symbol, []))

    def get_all_positions(self) -> Dict[str, List[Position]]:
        """
        Get all open positions, always refreshing from DB when available.

        Returns:
            Dictionary of symbol -> List[Position]
        """
        if self.db_client:
            try:
                query = """
                    SELECT id, position_id, symbol, side, quantity, price as entry_price,
                           strategy_id, entry_time, stop_loss, take_profit, metadata,
                           entry_commission, pnl_prediction, indicators_at_entry
                    FROM trades_open
                    WHERE metadata->>'trade_type' = 'spot'
                    ORDER BY symbol, entry_time ASC
                """
                results = self.db_client.execute_query(query)
                self._open_positions = {}
                for row in results:
                    position = Position(
                        symbol=row['symbol'],
                        side=row['side'],
                        quantity=row['quantity'],
                        entry_price=row['entry_price'],
                        entry_time=row['entry_time'],
                        position_id=_stable_position_id(row),
                        stop_loss=row['stop_loss'],
                        take_profit=row['take_profit'],
                        strategy_id=row['strategy_id'],
                        metadata=row['metadata'],
                        entry_commission=row.get('entry_commission'),
                        pnl_prediction=row.get('pnl_prediction'),
                        indicators_at_entry=row.get('indicators_at_entry'),
                        db_id=row.get('id')
                    )
                    # Ignore and clean up stale rows where a partial close left
                    # an effectively zero quantity still marked as OPEN.
                    if float(position.quantity or 0) <= _QTY_EPS:
                        logger.warning(
                            f"Removing stale zero-quantity open position {position.symbol} (id={row.get('id')})"
                        )
                        if self.db_client and row.get('id'):
                            try:
                                self.db_client.execute_update(
                                    "DELETE FROM trades_open WHERE id = %s", (row['id'],)
                                )
                            except Exception as _del_err:
                                logger.warning(f"Could not delete stale row {row.get('id')}: {_del_err}")
                        continue
                    if position.symbol not in self._open_positions:
                        self._open_positions[position.symbol] = []
                    self._open_positions[position.symbol].append(position)
            except Exception as e:
                logger.warning(f"Could not refresh positions from DB: {e}")
        return dict(self._open_positions)

    def _has_recent_binance_sell_trade(
        self,
        binance_client,
        symbol: str,
        hours: int = 24,
        minimum_qty: float = 0.0,
        since_time: Optional[datetime] = None,
    ) -> bool:
        """Confirm aggregate SELL fills since entry and within the recent window.

        A spot-wallet zero without a matching sell can mean the asset moved to another
        Binance wallet. Missing trade data and API errors therefore defer closure.
        """
        return has_recent_binance_sell_proof(
            binance_client,
            symbol,
            hours=hours,
            minimum_qty=minimum_qty,
            since_time=since_time,
        )

    def sync_with_exchange_balance(self, binance_client, settings, min_value_usdt: float = 5.0) -> List[str]:
        """
        Reconcile SPOT state against Binance balances.

        - Imports orphan assets that exist on Binance but not in DB.
        - Closes DB positions whose Binance balance is missing/zero.
        - Audits any mismatch so restart recovery stays fully traceable.

        Args:
            binance_client: BinanceClient instance
            settings: Settings instance (for SL/TP config)
            min_value_usdt: Minimum asset value in USDT to import (default $5)

        Returns:
            List of symbols imported as new positions
        """
        # Default $5 aligns with user-defined dust threshold; anything below is not imported.
        min_value_usdt = float(min_value_usdt or 5.0)
        # In dry-run / spot-margin-simulation mode we do NOT import real balances as
        # positions.  Simulation uses its own capital envelope and synthetic positions.
        # Check both settings and the Binance client / env because staging may have
        # the environment variable set while the Settings object still reflects .env defaults.
        _env_dry_run = str(os.environ.get("DRY_RUN", "false")).lower() in ("1", "true", "yes")
        _client_dry_run = bool(getattr(binance_client, 'dry_run', False))
        if (
            getattr(settings, 'dry_run', False)
            or getattr(settings, 'spot_margin_simulation_enabled', False)
            or _client_dry_run
            or _env_dry_run
        ):
            logger.info("sync_with_exchange_balance skipped: dry_run/spot_margin_simulation enabled")
            return []
        _excluded = set(_DEFAULT_EXCLUDED_BALANCE_ASSETS)
        _cfg_excluded = get_param(settings, 'spot_balance_sync_excluded_assets')
        if isinstance(_cfg_excluded, (list, tuple, set)):
            _excluded.update(str(a).upper() for a in _cfg_excluded)
        _keep_bnb_for_commission = bool(get_param(settings, 'use_bnb_for_commission')) or bool(
            get_param(settings, 'auto_bnb_for_commission')
        )
        imported = []
        balance_details = {}
        try:
            if hasattr(binance_client, 'get_account_balance_details'):
                balance_details = binance_client.get_account_balance_details()
                balances = {
                    asset: float(row['total'])
                    for asset, row in balance_details.items()
                }
            else:
                balances = binance_client.get_account_balance()
                balance_details = {
                    asset: {'free': float(amount), 'locked': 0.0, 'total': float(amount)}
                    for asset, amount in balances.items()
                }
            existing_positions = self.get_all_positions()
            existing = set(existing_positions.keys())

            for asset, amount in balances.items():
                if asset == "BNB" and _keep_bnb_for_commission:
                    continue
                if asset in _excluded or amount <= 0:
                    continue
                symbol = f"{asset}USDT"
                if symbol in existing:
                    continue
                try:
                    price = binance_client.get_symbol_price(symbol)
                except Exception:
                    continue
                value_usdt = amount * price
                if value_usdt < min_value_usdt:
                    continue

                _tok_cat = getattr(settings, 'token_categories', {}).get(symbol, {})
                _cat_beh = _tok_cat.get('market_behavior', 'MIDCAP_ALT')
                _cat_th  = getattr(settings, 'category_thresholds', {}).get(_cat_beh, {})
                _sl_pct  = _cat_th.get('stop_loss_pct',  get_param(settings, 'stop_loss_pct'))
                _tp_pct  = _cat_th.get('take_profit_pct', get_param(settings, 'take_profit_pct'))
                sl_price = price * (1 - _sl_pct / 100)
                tp_price = price * (1 + _tp_pct / 100)

                recovered_metadata = self._failed_entry_context(symbol, float(amount))
                metadata = {
                    "source": "balance_sync",
                    "sync_time": datetime.utcnow().isoformat() + "Z",
                    "value_usdt_at_sync": round(value_usdt, 4),
                    "base_asset": asset,
                    "base_quantity_at_import": float(amount),
                    "balance_free_qty": float(balance_details.get(asset, {}).get('free', amount)),
                    "balance_locked_qty": float(balance_details.get(asset, {}).get('locked', 0.0)),
                    "balance_total_qty": float(amount),
                    "mark_value_usdt_at_import": round(value_usdt, 8),
                }
                if recovered_metadata:
                    metadata.update(recovered_metadata)
                    metadata["source"] = "reconcile_canonical_restored"
                    metadata["entry_context_status"] = "restored_from_failed_persistence"
                    metadata.setdefault("cost_basis_status", "fill_verified")
                    metadata.setdefault("entry_price_source", "verified_entry_fill")
                    entry_commission = float(recovered_metadata.get('entry_commission_usdt') or 0.0)
                else:
                    metadata["entry_context_status"] = "unknown_reconciled"
                    metadata["cost_basis_status"] = "unknown"
                    metadata["entry_price_source"] = "balance_sync_mark_not_acquisition_cost"
                    metadata["commission_basis_status"] = "unknown"
                    entry_commission = 0.0
                pos = Position(
                    symbol=symbol,
                    side="BUY",
                    quantity=amount,
                    entry_price=price,
                    entry_time=datetime.utcnow().isoformat() + "Z",
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    entry_commission=entry_commission,
                    metadata=metadata,
                )
                metadata.setdefault('lineage_id', str(pos.position_id))
                if not self.add_position(pos):
                    self._audit(symbol, 'reconcile_entry_persist_failed', {
                        'quantity': float(amount),
                        'price': float(price),
                        'entry_context_status': metadata['entry_context_status'],
                    })
                    continue
                imported.append(symbol)
                reconcile_event = 'reconcile_canonical_restored' if recovered_metadata else 'reconcile_unknown_context'
                self._audit(symbol, reconcile_event, {
                    'position_id': pos.position_id,
                    'base_asset': asset,
                    'quantity': float(amount),
                    'base_quantity': float(amount),
                    'mark_price': float(price),
                    'mark_value_usdt': round(value_usdt, 8),
                    'cost_basis_status': metadata.get('cost_basis_status'),
                    'entry_price_source': metadata.get('entry_price_source'),
                    'entry_context_status': metadata['entry_context_status'],
                    'entry_order_id': metadata.get('entry_order_id'),
                })
                ec.add_error(
                    ec.SOURCE_SYSTEM, ec.LEVEL_INFO,
                    f"Balance sync: imported {symbol} {amount:.6f} @ {price:.4f} "
                    f"(${value_usdt:.2f}) — SL={sl_price:.4f} TP={tp_price:.4f}"
                )
                logger.info(f"Balance sync imported: {symbol} qty={amount} price={price}")

            # Reconcile DB positions that no longer have an on-exchange balance.
            _validation_ts = datetime.now(timezone.utc).isoformat()
            _min_missing_confirm = int(getattr(settings, 'spot_reconcile_missing_confirm_cycles', 2) or 2)

            for symbol, pos_list in existing_positions.items():
                if not pos_list:
                    continue
                asset = symbol.replace('USDT', '')
                balance_qty = float(balances.get(asset, 0.0))
                db_qty = sum(float(p.quantity or 0) for p in pos_list)
                if db_qty <= 0:
                    continue
                if balance_qty > 0:
                    self._missing_balance_streak[symbol] = 0
                    # Any remaining exchange quantity is still a position, including
                    # tiny residuals. Reconcile only the confirmed excess; never erase
                    # the whole position based on a ratio threshold or recent trade alone.
                    _qty_drift = db_qty - balance_qty
                    if _qty_drift > 1e-8:
                        try:
                            exit_price = binance_client.get_symbol_price(symbol)
                        except Exception:
                            exit_price = float(pos_list[0].entry_price or 0)
                        self._audit(symbol, 'reconcile_qty_drift', {
                            'db_qty': db_qty,
                            'exchange_qty': balance_qty,
                            'drift_qty': round(_qty_drift, 8),
                            'exit_price': exit_price,
                            'reason': 'binance_balance_smaller_than_db',
                            'confirmed_total_qty': balance_qty,
                        }, binance_confirmed=True)
                        self.close_all_positions_for_symbol(
                            symbol,
                            exit_price,
                            'reconcile_qty_drift',
                            get_param(settings, 'binance_commission_rate') if settings else 0.001,
                            qty_closed=min(_qty_drift, db_qty),
                        )
                        continue

                    _untracked_qty = balance_qty - db_qty
                    if _untracked_qty > 1e-8:
                        try:
                            mark_price = float(binance_client.get_symbol_price(symbol))
                        except Exception:
                            mark_price = 0.0
                        if mark_price <= 0:
                            self._audit(symbol, 'reconcile_surplus_deferred', {
                                'db_qty': db_qty,
                                'exchange_total_qty': balance_qty,
                                'untracked_qty': _untracked_qty,
                                'reason': 'mark_price_unavailable',
                            }, binance_confirmed=False)
                            continue
                        _surplus_metadata = {
                            'trade_type': 'spot',
                            'source': 'balance_sync_delta',
                            'entry_context_status': 'unknown_reconciled',
                            'cost_basis_status': 'unknown',
                            'entry_price_source': 'balance_sync_mark_not_acquisition_cost',
                            'base_asset': asset,
                            'base_quantity_at_import': _untracked_qty,
                            'balance_free_qty': float(balance_details.get(asset, {}).get('free', balance_qty)),
                            'balance_locked_qty': float(balance_details.get(asset, {}).get('locked', 0.0)),
                            'balance_total_qty': balance_qty,
                            'mark_value_usdt_at_import': round(_untracked_qty * mark_price, 8),
                            'sync_time': datetime.now(timezone.utc).isoformat(),
                        }
                        surplus_position = Position(
                            symbol=symbol,
                            side='BUY',
                            quantity=_untracked_qty,
                            entry_price=mark_price,
                            entry_time=datetime.now(timezone.utc).isoformat(),
                            stop_loss=mark_price * (1 - get_param(settings, 'stop_loss_pct') / 100),
                            take_profit=mark_price * (1 + get_param(settings, 'take_profit_pct') / 100),
                            entry_commission=0.0,
                            metadata=_surplus_metadata,
                        )
                        _surplus_metadata['lineage_id'] = str(surplus_position.position_id)
                        if self.add_position(surplus_position):
                            self._audit(symbol, 'reconcile_unknown_context', {
                                'position_id': surplus_position.position_id,
                                'base_asset': asset,
                                'db_qty_before': db_qty,
                                'exchange_total_qty': balance_qty,
                                'imported_base_qty': _untracked_qty,
                                'mark_price': mark_price,
                                'mark_value_usdt': round(_untracked_qty * mark_price, 8),
                                'cost_basis_status': 'unknown',
                                'source': 'balance_sync_delta',
                            }, binance_confirmed=False)
                        continue

                    for _pos in pos_list:
                        try:
                            _meta = dict(getattr(_pos, 'metadata', {}) or {})
                            _meta['binance_validated_at'] = _validation_ts
                            _meta['binance_validated_qty'] = round(balance_qty, 8)
                            self.update_position_metadata(_pos.position_id, _meta)
                        except Exception:
                            pass
                    continue
                # If Binance no longer shows the asset, treat the position as externally closed.
                if balance_qty <= 0:
                    # Staging/testnet safety: never close missing-balance positions that are too recent.
                    _min_age_min = float(getattr(settings, 'spot_ghost_min_age_minutes', 0.0) or 0.0)
                    _youngest_age_min = min(
                        self._position_age_minutes(p) for p in pos_list
                    ) if pos_list else 1_000_000.0
                    if _min_age_min > 0 and _youngest_age_min < _min_age_min:
                        self._audit(symbol, 'reconcile_missing_balance_deferred', {
                            'db_qty': db_qty,
                            'exchange_qty': balance_qty,
                            'youngest_age_min': round(_youngest_age_min, 2),
                            'required_age_min': _min_age_min,
                            'reason': 'position_too_recent_for_missing_balance',
                        }, binance_confirmed=True)
                        continue
                    _streak = int(self._missing_balance_streak.get(symbol, 0) or 0) + 1
                    self._missing_balance_streak[symbol] = _streak
                    if _streak < _min_missing_confirm:
                        self._audit(symbol, 'reconcile_missing_balance_deferred', {
                            'db_qty': db_qty,
                            'exchange_qty': balance_qty,
                            'streak': _streak,
                            'required': _min_missing_confirm,
                            'reason': 'waiting_confirmation_cycles',
                        }, binance_confirmed=True)
                        continue
                    _ghost_hours = int(getattr(settings, 'spot_ghost_sell_confirmation_hours', 24) or 24)
                    _since_entry = min((_parse_exit_time(pos.entry_time) for pos in pos_list), default=None)
                    _recent_sell = self._has_recent_binance_sell_trade(
                        binance_client,
                        symbol,
                        hours=_ghost_hours,
                        minimum_qty=db_qty,
                        since_time=_since_entry,
                    )
                    if not _recent_sell:
                        self._audit(symbol, 'reconcile_missing_balance_deferred', {
                            'db_qty': db_qty,
                            'exchange_total_qty': balance_qty,
                            'streak': _streak,
                            'required': _min_missing_confirm,
                            'confirmation_hours': _ghost_hours,
                            'reason': 'no_recent_spot_sell_proof_asset_may_be_in_other_wallet',
                        }, binance_confirmed=False)
                        continue
                    try:
                        exit_price = binance_client.get_symbol_price(symbol)
                    except Exception:
                        exit_price = float(pos_list[0].entry_price or 0)
                    self._audit(symbol, 'reconcile_missing_balance', {
                        'db_qty': db_qty,
                        'exchange_qty': balance_qty,
                        'exit_price': exit_price,
                        'reason': 'balance_missing_on_exchange',
                        'confirmation_streak': _streak,
                        'required_confirmations': _min_missing_confirm,
                        'balance_source': 'valid_spot_total_snapshot',
                        'recent_spot_sell_proof': _recent_sell,
                    }, binance_confirmed=False)
                    closed = self.close_all_positions_for_symbol(
                        symbol,
                        exit_price,
                        'reconcile_missing_balance',
                        get_param(settings, 'binance_commission_rate') if settings else 0.001,
                    )
                    if closed:
                        self._missing_balance_streak[symbol] = 0
                        logger.info(f"Balance sync reconciled missing balance: {symbol} closed {len(closed)} DB position(s)")
        except Exception as e:
            logger.error(f"Balance sync failed: {e}")
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR, f"Balance sync error: {e}")
        return imported

    def update_position_metadata(self, position_id: str, metadata: dict) -> None:
        """Update metadata dict of an open position (e.g. trailing_peak for trailing stop)."""
        for pos_list in self._open_positions.values():
            for pos in pos_list:
                if str(pos.position_id) == str(position_id):
                    if not isinstance(metadata, dict):
                        metadata = {
                            '_metadata_corrupt_raw': repr(metadata),
                            '_meta_recovered': True,
                        }
                    metadata['_meta_version'] = int(metadata.get('_meta_version', 0) or 0) + 1
                    metadata['_meta_updated_at'] = datetime.now(timezone.utc).isoformat()
                    pos.metadata = metadata
                    if self.db_client and pos.db_id:
                        try:
                            self.db_client.execute_update(
                                "UPDATE trades_open SET metadata = %s WHERE id = %s",
                                (json.dumps(metadata), pos.db_id)
                            )
                        except Exception as e:
                            logger.warning(f"Could not persist metadata update for {position_id}: {e}")
                    return

    def set_stop_loss_by_usd_loss(self, symbol: str, max_loss_usd: float) -> Dict[str, Any]:
        positions = list(self._open_positions.get(symbol) or [])
        target_loss = float(max_loss_usd or 0.0)
        if not positions or target_loss <= 0:
            return {"ok": False, "reason": "INVALID_POSITION_OR_LOSS"}
        sides = {"LONG" if is_long_side(pos.side) else "SHORT" for pos in positions}
        if len(sides) != 1:
            return {"ok": False, "reason": "MIXED_POSITION_SIDES"}
        total_qty = sum(float(pos.quantity or 0.0) for pos in positions)
        total_cost = sum(float(pos.quantity or 0.0) * float(pos.entry_price or 0.0) for pos in positions)
        if total_qty <= 0:
            return {"ok": False, "reason": "INVALID_POSITION_QUANTITY"}
        average_entry = total_cost / total_qty
        stop_price = average_entry - target_loss / total_qty if "LONG" in sides else average_entry + target_loss / total_qty
        if stop_price <= 0:
            return {"ok": False, "reason": "INVALID_STOP_PRICE"}
        for pos in positions:
            pos.stop_loss = stop_price
            if self.db_client and pos.db_id:
                self.db_client.execute_update(
                    "UPDATE trades_open SET stop_loss = %s WHERE id = %s",
                    (stop_price, pos.db_id),
                )
        return {
            "ok": True,
            "symbol": symbol,
            "target_loss_usd": target_loss,
            "average_entry_price": average_entry,
            "stop_loss_price": stop_price,
            "position_count": len(positions),
        }

    def update_position_sl(self, position_id: str, new_sl_price: float) -> None:
        """Move stop-loss price of an open position (e.g. to breakeven after scale-out)."""
        for pos_list in self._open_positions.values():
            for pos in pos_list:
                if str(pos.position_id) == str(position_id):
                    # FIX-T3: REGLA DE ORO — SL nunca empeora
                    _old_sl = float(pos.stop_loss or 0)
                    if _old_sl > 0:
                        _is_buy = is_long_side(pos.side)
                        if _is_buy and new_sl_price < _old_sl:
                            logger.info(f"[Guard] SL WORSEN blocked {pos.symbol}: {_old_sl:.6f} → {new_sl_price:.6f}")
                            return
                        if not _is_buy and new_sl_price > _old_sl:
                            logger.info(f"[Guard] SL WORSEN blocked {pos.symbol}: {_old_sl:.6f} → {new_sl_price:.6f}")
                            return
                    pos.stop_loss = new_sl_price
                    if self.db_client and pos.db_id:
                        try:
                            self.db_client.execute_update(
                                "UPDATE trades_open SET stop_loss = %s WHERE id = %s",
                                (new_sl_price, pos.db_id)
                            )
                        except Exception as e:
                            logger.warning(f"Could not persist SL update for {position_id}: {e}")
                    return

    def update_position_quantity(self, position_id: str, new_qty: float) -> None:
        """Update quantity of an open position after a partial sell (e.g. scale-out)."""
        for pos_list in self._open_positions.values():
            for pos in pos_list:
                if str(pos.position_id) == str(position_id):
                    pos.quantity = new_qty
                    if self.db_client and pos.db_id:
                        try:
                            self.db_client.execute_update(
                                "UPDATE trades_open SET quantity = %s WHERE id = %s",
                                (new_qty, pos.db_id)
                            )
                        except Exception as e:
                            logger.warning(f"Could not persist qty update for {position_id}: {e}")
                    return

    def update_position_tp(self, position_id: str, new_tp_price: float) -> None:
        """Move take-profit price of an open position (e.g. Smart TP extension)."""
        for pos_list in self._open_positions.values():
            for pos in pos_list:
                if str(pos.position_id) == str(position_id):
                    pos.take_profit = new_tp_price
                    if self.db_client and pos.db_id:
                        try:
                            self.db_client.execute_update(
                                "UPDATE trades_open SET take_profit = %s WHERE id = %s",
                                (new_tp_price, pos.db_id)
                            )
                        except Exception as e:
                            logger.warning(f"Could not persist TP update for {position_id}: {e}")
                    return

    def _calc_close(self, position: Position, exit_price: float, commission_rate: float = None, qty_closed: Optional[float] = None, entry_comm_for_closed: Optional[float] = None, exit_commission_override: Optional[float] = None):
        """Shared PnL calculation for closing a position. Returns (qty, entry_p, exit_price, pnl, pnl_pct, exit_comm, pred_accuracy)."""
        from binance_commission import get_config, exit_commission, net_pnl
        qty        = float(qty_closed) if qty_closed is not None else float(position.quantity)
        entry_p    = float(position.entry_price)
        exit_price = float(exit_price)
        # Use centralized commission module if commission_rate is not explicitly provided
        if exit_commission_override is not None:
            _exit_comm = float(exit_commission_override)
        elif commission_rate is None:
            _exit_comm = exit_commission(qty, exit_price, 'spot')
        else:
            _exit_comm = qty * exit_price * commission_rate
        # Preserve stored entry commission if available; for partial closes caller provides proportional share
        entry_comm = entry_comm_for_closed if entry_comm_for_closed is not None else float(position.entry_commission or 0)
        if entry_comm <= 0 and qty > 0:
            from binance_commission import entry_commission
            entry_comm = entry_commission(qty, entry_p, 'spot')
        if is_long_side(position.side):
            gross_pnl = (exit_price - entry_p) * qty
        else:
            gross_pnl = (entry_p - exit_price) * qty
        pnl = gross_pnl - entry_comm - _exit_comm
        pnl_percentage = (pnl / (qty * entry_p)) * 100 if (qty * entry_p) > 0 else 0.0
        prediction_accuracy = None
        if position.pnl_prediction:
            expected_pnl = float(position.pnl_prediction.get('expected_pnl', 0))
            if expected_pnl != 0:
                prediction_accuracy = float(((pnl - expected_pnl) / abs(expected_pnl)) * 100)
        return qty, entry_p, exit_price, pnl, pnl_percentage, _exit_comm, prediction_accuracy

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: Optional[float] = None,
        position_id: Optional[str] = None,
        exit_context: Optional[Dict[str, Any]] = None,
        qty_closed: Optional[float] = None,
        exit_fill: Optional[Dict[str, Any]] = None,
    ) -> Optional[ClosedPosition]:
        """
        Close a specific open position for a symbol.

        s113: serialized by `self._close_lock` — the main trade cycle and
        `hard_risk_monitor.monitor_spot_hard_stops_once` (separate thread, 2s
        cadence) can both reach this method for the same position; without a
        lock, both could read the same in-memory `Position` before either
        persists its close, producing the same double-close-in-`trades_closed`
        bug already found and fixed on the futures side (see
        `calibrar-ciclo-vida-52fe5d.md` s105h/s112).

        Args:
            symbol: Trading pair symbol
            exit_price: Exit price
            close_reason: Reason for closing
            commission_rate: Commission rate for exit trade
            position_id: If provided, closes the entry with this position_id;
                         otherwise closes the oldest entry (FIFO).
            qty_closed: Optional real quantity sold on Binance. If less than position.quantity
                        the position is kept open with the residual quantity.

        Returns:
            ClosedPosition if position was found, None otherwise
        """
        with self._close_lock:
            return self._close_position_impl(
                symbol, exit_price, close_reason, commission_rate,
                position_id, exit_context, qty_closed, exit_fill,
            )

    def _close_position_impl(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: Optional[float] = None,
        position_id: Optional[str] = None,
        exit_context: Optional[Dict[str, Any]] = None,
        qty_closed: Optional[float] = None,
        exit_fill: Optional[Dict[str, Any]] = None,
    ) -> Optional[ClosedPosition]:
        positions = self._open_positions.get(symbol)
        if not positions:
            logger.warning(f"No open position found for {symbol}")
            return None

        # Select which entry to close
        if position_id:
            target = next((p for p in positions if p.position_id == position_id), None)
            if not target:
                logger.warning(f"position_id {position_id} not found for {symbol}")
                return None
        else:
            target = positions[0]  # FIFO: oldest entry first

        full_qty = float(target.quantity)
        _closed_qty = float(qty_closed) if qty_closed is not None else full_qty
        _closed_qty = min(_closed_qty, full_qty)
        _is_partial = _closed_qty < full_qty

        # Floating-point residuals that round to zero must not remain as OPEN rows.
        if _is_partial and (full_qty - _closed_qty) <= max(_QTY_EPS, full_qty * _QTY_EPS):
            _closed_qty = full_qty
            _is_partial = False

        # Allocate entry commission proportionally for partial closes
        _entry_comm_total = float(target.entry_commission or 0)
        # C0: if entry commission was not stored, estimate it from the full notional
        if _entry_comm_total <= 0 and full_qty > 0:
            from binance_commission import entry_commission
            _entry_comm_total = entry_commission(full_qty, target.entry_price, 'spot')
        _entry_comm_for_closed = _entry_comm_total * (_closed_qty / full_qty) if full_qty > 0 else _entry_comm_total
        _entry_comm_remaining = _entry_comm_total - _entry_comm_for_closed

        if exit_fill and exit_fill.get('binance_confirmed'):
            _fill_price = float(exit_fill.get('avg_price', 0.0) or 0.0)
            if _fill_price > 0:
                exit_price = _fill_price
        _, entry_p, exit_p, pnl, pnl_pct, exit_comm, pred_acc = self._calc_close(
            target, exit_price, commission_rate, qty_closed=_closed_qty,
            entry_comm_for_closed=_entry_comm_for_closed,
            exit_commission_override=(exit_fill or {}).get('commission_usdt') if exit_fill and exit_fill.get('binance_confirmed') else None,
        )

        _meta = dict(target.metadata or {}) if isinstance(target.metadata, dict) else {}
        if exit_context:
            if exit_context.get('regime_mode'):
                _meta['regime_at_exit'] = exit_context.get('regime_mode')
            if exit_context.get('profile_label'):
                _meta['profile_at_exit'] = exit_context.get('profile_label')
            if exit_context.get('switch_id'):
                _meta['switch_id'] = exit_context.get('switch_id')
        if exit_fill and exit_fill.get('binance_confirmed'):
            _meta['exit_fill'] = dict(exit_fill)
            _meta['pnl_accounting_mode'] = 'binance_fill_verified'
        else:
            _meta['pnl_accounting_mode'] = 'estimated'
        _position_mode = 'SPOT_LONG'
        _position_id = str(
            target.position_id
            or _meta.get('position_id')
            or f"{target.symbol}:{target.entry_time}:{datetime.now(timezone.utc).isoformat()}:{_position_mode}"
        )
        _entry_order_id = str(_meta.get('entry_order_id') or _meta.get('order_id') or '') or None
        _exit_order_id = str(
            _meta.get('exit_order_id')
            or ((exit_fill or {}).get('order_id') if isinstance(exit_fill, dict) else '')
            or ''
        ) or None
        _close_group_id = str(_meta.get('close_group_id') or '') or None
        # Canonical close attribution fields (required for entry->close lineage and net-cost analysis).
        _meta.setdefault('ready_candidate_id', _meta.get('ready_candidate_id'))
        _meta.setdefault('staged_direction', _meta.get('staged_direction') or {})
        _meta.setdefault('profile_at_entry', _meta.get('profile_at_entry') or _meta.get('peak_quality_profile'))
        _meta.setdefault('regime_at_entry', _meta.get('regime_at_entry') or _meta.get('entry_regime') or _meta.get('regime_mode'))
        _meta['position_id'] = _position_id
        if _entry_order_id:
            _meta['entry_order_id'] = _entry_order_id
        if _exit_order_id:
            _meta['exit_order_id'] = _exit_order_id
        _meta['commission_bps_total'] = round(((_entry_comm_for_closed + (exit_comm or 0.0)) / max(1e-12, _closed_qty * entry_p)) * 10000.0, 6)
        _meta.setdefault('spread_bps_estimate', _meta.get('spread_bps_estimate'))
        _meta.setdefault('slippage_bps_estimate', _meta.get('slippage_bps_estimate'))
        _meta.setdefault('funding_bps', 0.0)
        _meta = apply_close_contract(
            _meta,
            trade_type=str(_meta.get('trade_type') or 'spot'),
            position_side=target.side,
            close_reason=close_reason,
            exchange_confirmed=bool(exit_fill and exit_fill.get('binance_confirmed')),
            synthetic=bool(_meta.get('synthetic')),
            learning_eligible=_meta.get('learning_eligible'),
            partial=_is_partial,
            close_group_id=_meta.get('close_group_id'),
        )
        if _is_partial:
            _meta['partial_close'] = True
            _meta['partial_qty_closed'] = _closed_qty
            _meta['partial_qty_remaining'] = full_qty - _closed_qty

        closed_position = ClosedPosition(
            symbol=target.symbol,
            side=target.side,
            quantity=_closed_qty,
            entry_price=entry_p,
            exit_price=exit_p,
            entry_time=target.entry_time,
            exit_time=datetime.now(timezone.utc).isoformat(),
            pnl=pnl,
            pnl_percentage=pnl_pct,
            close_reason=close_reason,
            position_id=_position_id,
            entry_order_id=_entry_order_id,
            exit_order_id=_exit_order_id,
            close_group_id=_close_group_id,
            strategy_id=target.strategy_id,
            metadata=_meta,
            entry_commission=_entry_comm_for_closed,
            exit_commission=exit_comm,
            pnl_prediction=target.pnl_prediction,
            indicators_at_entry=target.indicators_at_entry,
            prediction_accuracy=pred_acc
        )

        # Try to persist to DB BEFORE mutating in-memory state.
        # If the DB transaction fails, the position remains tracked and will be retried/reconciled.
        _db_ok = True
        if self.db_client:
            _db_ok = self._close_position_in_db(
                target, closed_position, is_partial=_is_partial,
                residual_qty=full_qty - _closed_qty if _is_partial else None,
                residual_entry_commission=_entry_comm_remaining if _is_partial else None,
            )

        if not _db_ok:
            logger.error(f"Position close NOT applied in memory because DB persist failed: {symbol}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Position close DB persist failed: {symbol}")
            self._audit(symbol, 'close_persist_failed', {
                'side': closed_position.side, 'qty': float(closed_position.quantity or 0),
                'entry_price': float(closed_position.entry_price or 0),
                'exit_price': float(closed_position.exit_price or 0),
                'pnl': round(float(pnl), 4), 'reason': close_reason, 'partial': _is_partial,
            }, binance_confirmed=bool(exit_fill and exit_fill.get('binance_confirmed')))
            return None

        # Update or remove the open entry depending on whether this was a full or partial close
        if _is_partial:
            target.quantity = full_qty - _closed_qty
            target.entry_commission = _entry_comm_remaining
            if not isinstance(target.metadata, dict):
                target.metadata = {}
            target.metadata['partial_close'] = True
            target.metadata['partial_qty_closed'] = _closed_qty
            target.metadata['partial_qty_remaining'] = full_qty - _closed_qty
            target.metadata['partial_close_time'] = datetime.now(timezone.utc).isoformat()
        else:
            positions.remove(target)
            if not positions:
                del self._open_positions[symbol]

        self._closed_positions.append(closed_position)

        _cp_meta = closed_position.metadata if isinstance(closed_position.metadata, dict) else {}
        self._audit(symbol, 'close', {
            'side': closed_position.side, 'qty': float(closed_position.quantity or 0),
            'entry_price': float(closed_position.entry_price or 0),
            'exit_price': float(closed_position.exit_price or 0),
            'pnl': round(float(pnl), 4), 'reason': close_reason, 'partial': _is_partial,
            'bounce_entry': bool(_cp_meta.get('bounce_entry', False)),
            'pnl_accounting_mode': _cp_meta.get('pnl_accounting_mode'),
        }, binance_confirmed=bool(exit_fill and exit_fill.get('binance_confirmed')))
        logger.info(f"Position closed: {symbol} qty={_closed_qty:.4f} PnL={pnl:.2f} ({pnl_pct:.2f}%) reason={close_reason}")
        return closed_position

    def close_all_positions_for_symbol(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: float = 0.001,
        exit_context: Optional[Dict[str, Any]] = None,
        qty_closed: Optional[float] = None,
        exit_fill: Optional[Dict[str, Any]] = None,
    ) -> List[ClosedPosition]:
        """
        Close open positions for a symbol. If qty_closed is provided, it closes up to that
        total quantity across entries (FIFO). Otherwise, closes all entries.

        s113: serialized by `self._close_lock` (RLock — safe to nest with the
        per-entry `close_position` calls below). See `close_position` docstring.

        Returns:
            List of ClosedPosition objects
        """
        with self._close_lock:
            return self._close_all_positions_for_symbol_impl(
                symbol, exit_price, close_reason, commission_rate,
                exit_context, qty_closed, exit_fill,
            )

    def _close_all_positions_for_symbol_impl(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: float = 0.001,
        exit_context: Optional[Dict[str, Any]] = None,
        qty_closed: Optional[float] = None,
        exit_fill: Optional[Dict[str, Any]] = None,
    ) -> List[ClosedPosition]:
        positions = list(self._open_positions.get(symbol, []))
        # ── SPOT Escalamiento: assign shared close_group_id when closing N entries ──
        _close_group = str(uuid.uuid4()) if len(positions) > 1 else None
        _remaining_to_close = float(qty_closed) if qty_closed is not None else None
        results = []
        for _i, pos in enumerate(positions):
            if _remaining_to_close is not None and _remaining_to_close <= 0:
                break
            # Inject close_group_id + escalonamiento metadata into the position before closing
            if _close_group:
                if not isinstance(pos.metadata, dict):
                    pos.metadata = {
                        '_metadata_corrupt_raw': repr(pos.metadata),
                        '_meta_recovered': True,
                    }
                pos.metadata['close_group_id'] = _close_group
                pos.metadata['escalonamiento_index'] = pos.metadata.get('escalonamiento_index', _i + 1)
                pos.metadata['total_escalonamientos'] = len(positions)
            _entry_close_qty = None
            if _remaining_to_close is not None:
                _entry_close_qty = min(_remaining_to_close, float(pos.quantity))
            _exit_ctx = dict(exit_context) if exit_context else {}
            if _entry_close_qty is not None and _entry_close_qty < float(pos.quantity):
                _exit_ctx['partial_qty_closed'] = _entry_close_qty
            closed = self.close_position(
                symbol, exit_price, close_reason, commission_rate,
                position_id=pos.position_id, exit_context=_exit_ctx,
                qty_closed=_entry_close_qty, exit_fill=exit_fill,
            )
            if closed:
                results.append(closed)
                if _remaining_to_close is not None:
                    _remaining_to_close -= float(closed.quantity)
        return results
    
    def _close_position_in_db(
        self,
        position: Position,
        closed_position: ClosedPosition,
        is_partial: bool = False,
        residual_qty: Optional[float] = None,
        residual_entry_commission: Optional[float] = None,
    ) -> bool:
        """Persist a close in trades_closed. For partial closes, update trades_open quantity instead of deleting.
        Returns True on success, False on failure. Caller must not update in-memory state unless this returns True.
        """
        def _to_json(v):
            return json.dumps(v) if isinstance(v, (dict, list)) else v

        def _safe_float(v):
            return float(v) if v is not None else None

        # FIX-4: Ensure bounce_entry is in root of metadata for easy lookup in closed endpoints
        _meta = closed_position.metadata
        if isinstance(_meta, str):
            try:
                _meta = json.loads(_meta)
            except Exception:
                _meta = {}
        if isinstance(_meta, dict):
            # Keep trade_type/close_reason available in closed rows for parity and audits.
            _meta.setdefault('trade_type', 'spot')
            if closed_position.close_reason and not _meta.get('close_reason'):
                _meta['close_reason'] = closed_position.close_reason
            if _meta.get('bounce_entry') is None:
                # Propagate from position metadata if available
                _pos_meta = position.metadata if isinstance(position.metadata, dict) else {}
                if _pos_meta.get('bounce_entry'):
                    _meta['bounce_entry'] = True
            if is_partial:
                _meta['partial_close'] = True
            _meta = apply_close_contract(
                _meta,
                trade_type=str(_meta.get('trade_type') or 'spot'),
                position_side=closed_position.side,
                close_reason=closed_position.close_reason,
                exchange_confirmed=bool(_meta.get('binance_confirmed')),
                synthetic=bool(_meta.get('synthetic')),
                learning_eligible=_meta.get('learning_eligible'),
                partial=is_partial,
                close_group_id=_meta.get('close_group_id'),
            )
        _meta_json = json.dumps(_meta) if isinstance(_meta, dict) else _to_json(_meta)

        insert_query = """
            INSERT INTO trades_closed 
            (symbol, side, quantity, entry_price, exit_price, strategy_id, entry_time, exit_time, 
             pnl, pnl_percentage, close_reason, position_id, entry_order_id, exit_order_id, close_group_id,
             metadata, entry_commission, exit_commission, 
             pnl_prediction, indicators_at_entry, prediction_accuracy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        insert_params = (
            closed_position.symbol,
            closed_position.side,
            _safe_float(closed_position.quantity),
            _safe_float(closed_position.entry_price),
            _safe_float(closed_position.exit_price),
            closed_position.strategy_id,
            closed_position.entry_time,
            closed_position.exit_time,
            _safe_float(closed_position.pnl),
            _safe_float(closed_position.pnl_percentage),
            closed_position.close_reason,
            closed_position.position_id,
            closed_position.entry_order_id,
            closed_position.exit_order_id,
            closed_position.close_group_id,
            _meta_json,
            _safe_float(closed_position.entry_commission),
            _safe_float(closed_position.exit_commission),
            _to_json(closed_position.pnl_prediction),
            _to_json(closed_position.indicators_at_entry),
            _safe_float(closed_position.prediction_accuracy)
        )

        _residual_meta = json.dumps(position.metadata) if isinstance(position.metadata, dict) else '{}'
        try:
            if is_partial:
                _res_qty = residual_qty if residual_qty is not None else position.quantity
                _res_comm = residual_entry_commission if residual_entry_commission is not None else position.entry_commission
                # Update residual quantity, entry commission and partial-close metadata in the open record
                if position.db_id:
                    update_query = "UPDATE trades_open SET quantity = %s, entry_commission = %s, metadata = metadata || %s::jsonb WHERE id = %s"
                    update_params = (
                        _safe_float(_res_qty),
                        _safe_float(_res_comm),
                        _residual_meta,
                        position.db_id,
                    )
                else:
                    # No db_id fallback: update by symbol+entry_time
                    update_query = "UPDATE trades_open SET quantity = %s, entry_commission = %s, metadata = metadata || %s::jsonb WHERE symbol = %s AND entry_time = %s"
                    update_params = (
                        _safe_float(_res_qty),
                        _safe_float(_res_comm),
                        _residual_meta,
                        position.symbol,
                        position.entry_time,
                    )
                self.db_client.execute_transaction([
                    (insert_query, insert_params),
                    (update_query, update_params),
                ])
            else:
                # Delete by primary key when available, otherwise fallback to symbol+entry_time.
                if position.db_id:
                    delete_query = "DELETE FROM trades_open WHERE id = %s"
                    delete_params = (position.db_id,)
                else:
                    delete_query = "DELETE FROM trades_open WHERE symbol = %s AND entry_time = %s"
                    delete_params = (position.symbol, position.entry_time)

                self.db_client.execute_transaction([
                    (insert_query, insert_params),
                    (delete_query, delete_params),
                ])
            # P1.1: Invalidate daily PnL cache — next SSO-4 evaluation will read updated total from BD
            try:
                from daily_pnl_tracker import invalidate_cache as _inv_dpnl
                _inv_dpnl()
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"Failed to close position in database: {e}")
            ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to close position {position.symbol} in DB: {e}")
            return False
    
    def check_stop_loss_take_profit(self, current_prices: Dict[str, float]) -> List[ClosedPosition]:
        """
        Check if any positions hit stop loss or take profit.

        Args:
            current_prices: Dictionary of symbol -> current price

        Returns:
            List of positions that were closed
        """
        closed_positions = []
        for symbol, pos_list in list(self._open_positions.items()):
            current_price = current_prices.get(symbol)
            if not current_price:
                continue
            for position in list(pos_list):
                close_reason = None
                if position.stop_loss:
                    if position.side.upper() == "BUY" and current_price <= float(position.stop_loss):
                        close_reason = "stop_loss"
                    elif position.side.upper() == "SELL" and current_price >= float(position.stop_loss):
                        close_reason = "stop_loss"
                if position.take_profit:
                    if position.side.upper() == "BUY" and current_price >= float(position.take_profit):
                        close_reason = "take_profit"
                    elif position.side.upper() == "SELL" and current_price <= float(position.take_profit):
                        close_reason = "take_profit"
                if close_reason:
                    closed = self.close_position(symbol, current_price, close_reason, position_id=position.position_id)
                    if closed:
                        closed_positions.append(closed)
        return closed_positions
    
    def get_position_summary(self) -> Dict[str, Any]:
        """
        Get summary of all open positions.

        Returns:
            Dictionary with position summary
        """
        total_value = 0.0
        all_entries = []
        for symbol, pos_list in self._open_positions.items():
            for pos in pos_list:
                position_value = float(pos.quantity) * float(pos.entry_price)
                total_value += position_value
                all_entries.append({
                    "symbol": symbol,
                    "position_id": pos.position_id,
                    "side": pos.side,
                    "quantity": float(pos.quantity),
                    "entry_price": float(pos.entry_price),
                    "stop_loss": float(pos.stop_loss) if pos.stop_loss else None,
                    "take_profit": float(pos.take_profit) if pos.take_profit else None,
                    "entry_time": pos.entry_time,
                    "entry_commission": float(pos.entry_commission) if pos.entry_commission else 0.0
                })
        total_entries = sum(len(v) for v in self._open_positions.values())
        return {
            "open_positions_count": total_entries,
            "symbols_count": len(self._open_positions),
            "total_value_usdt": total_value,
            "total_pnl_unrealized": 0.0,
            "positions": all_entries,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def get_closed_positions(self, limit: int = 20) -> List[ClosedPosition]:
        """
        Get the last N closed positions.
        
        Args:
            limit: Maximum number of closed positions to return
            
        Returns:
            List of ClosedPosition objects, most recent first
        """
        # Always query DB when available to avoid stale cache for low limits.
        # Futures reconciliation writes directly to trades_closed, so relying on
        # in-memory cache can hide recent reconciled closures from API consumers.
        if self.db_client:
            try:
                query = """
                      SELECT symbol, side, quantity, entry_price, exit_price,
                          entry_time, exit_time, pnl, pnl_percentage, close_reason,
                          position_id, entry_order_id, exit_order_id, close_group_id,
                          strategy_id, metadata, entry_commission, exit_commission,
                          pnl_prediction, indicators_at_entry, prediction_accuracy
                    FROM trades_closed
                    WHERE COALESCE(is_corrupted, FALSE) = FALSE
                    ORDER BY exit_time DESC
                    LIMIT %s
                """
                results = self.db_client.execute_query(query, (limit,))
                db_positions = []
                for row in results:
                    db_positions.append(ClosedPosition(
                        symbol=row['symbol'], side=row['side'],
                        quantity=row['quantity'], entry_price=row['entry_price'],
                        exit_price=row['exit_price'], entry_time=row['entry_time'],
                        exit_time=row['exit_time'], pnl=row['pnl'],
                        pnl_percentage=row['pnl_percentage'], close_reason=row['close_reason'],
                        position_id=row.get('position_id'),
                        entry_order_id=row.get('entry_order_id'),
                        exit_order_id=row.get('exit_order_id'),
                        close_group_id=row.get('close_group_id'),
                        strategy_id=row.get('strategy_id'), metadata=row.get('metadata'),
                        entry_commission=row.get('entry_commission'),
                        exit_commission=row.get('exit_commission'),
                        pnl_prediction=row.get('pnl_prediction'),
                        indicators_at_entry=row.get('indicators_at_entry'),
                        prediction_accuracy=row.get('prediction_accuracy'),
                    ))
                return db_positions
            except Exception as e:
                logger.warning(f"DB query for closed positions failed, using cache: {e}")
        # Fallback: in-memory sorted slice
        sorted_positions = sorted(
            self._closed_positions,
            key=lambda x: _parse_exit_time(x.exit_time),
            reverse=True
        )
        return sorted_positions[:limit]

    def get_closed_positions_since(self, since: datetime, limit: int = 50) -> List[ClosedPosition]:
        """
        Get closed positions since a specific datetime.

        Args:
            since: UTC datetime to filter positions (inclusive)
            limit: Maximum number of closed positions to return

        Returns:
            List of ClosedPosition objects with exit_time >= since, most recent first
        """
        since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)

        if self.db_client:
            try:
                query = """
                      SELECT symbol, side, quantity, entry_price, exit_price,
                          entry_time, exit_time, pnl, pnl_percentage, close_reason,
                          position_id, entry_order_id, exit_order_id, close_group_id,
                          strategy_id, metadata, entry_commission, exit_commission,
                          pnl_prediction, indicators_at_entry, prediction_accuracy
                    FROM trades_closed
                    WHERE exit_time >= %s
                      AND COALESCE(is_corrupted, FALSE) = FALSE
                    ORDER BY exit_time DESC
                    LIMIT %s
                """
                results = self.db_client.execute_query(query, (since_aware.isoformat(), limit))
                db_positions = []
                for row in results:
                    db_positions.append(ClosedPosition(
                        symbol=row['symbol'], side=row['side'],
                        quantity=row['quantity'], entry_price=row['entry_price'],
                        exit_price=row['exit_price'], entry_time=row['entry_time'],
                        exit_time=row['exit_time'], pnl=row['pnl'],
                        pnl_percentage=row['pnl_percentage'], close_reason=row['close_reason'],
                        position_id=row.get('position_id'),
                        entry_order_id=row.get('entry_order_id'),
                        exit_order_id=row.get('exit_order_id'),
                        close_group_id=row.get('close_group_id'),
                        strategy_id=row.get('strategy_id'), metadata=row.get('metadata'),
                        entry_commission=row.get('entry_commission'),
                        exit_commission=row.get('exit_commission'),
                        pnl_prediction=row.get('pnl_prediction'),
                        indicators_at_entry=row.get('indicators_at_entry'),
                        prediction_accuracy=row.get('prediction_accuracy'),
                    ))
                return db_positions
            except Exception as e:
                logger.warning(f"DB query for closed positions since failed, using cache: {e}")

        # Fallback: filter and sort in-memory
        filtered_positions = [
            pos for pos in self._closed_positions
            if _parse_exit_time(pos.exit_time) >= since_aware
        ]
        sorted_positions = sorted(
            filtered_positions,
            key=lambda x: _parse_exit_time(x.exit_time),
            reverse=True
        )
        return sorted_positions[:limit]

    def calculate_pnl_history(self, period: str = "day") -> Dict[str, Any]:
        """
        Calculate PnL for a specific period based on closed trades.
        
        Args:
            period: "day", "week", or "year"
            
        Returns:
            Dictionary with PnL summary for the period
        """
        now = datetime.utcnow()
        
        if period == "day":
            cutoff = now - timedelta(days=1)
        elif period == "week":
            cutoff = now - timedelta(weeks=1)
        elif period == "year":
            cutoff = now - timedelta(days=365)
        else:
            cutoff = now - timedelta(days=1)  # Default to day
        
        # Filter closed positions within the period
        _cutoff_aware = cutoff.replace(tzinfo=timezone.utc)
        period_positions = [
            pos for pos in self._closed_positions
            if _parse_exit_time(pos.exit_time) >= _cutoff_aware
        ]
        
        total_pnl = sum(pos.pnl for pos in period_positions)
        total_trades = len(period_positions)
        
        # Calculate percentage based on total invested
        total_invested = sum(
            pos.quantity * pos.entry_price 
            for pos in period_positions
        )
        pnl_percentage = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
        
        return {
            "period": period,
            "total_pnl": total_pnl,
            "pnl_percentage": pnl_percentage,
            "total_trades": total_trades,
            "cutoff_time": cutoff.isoformat(),
            "timestamp": now.isoformat()
        }
    
    def calculate_performance_metrics(self) -> Dict[str, Any]:
        """
        Calculate performance metrics from closed positions (cached 60 s).
        
        Returns:
            Dictionary with win rate, profit factor, max drawdown, etc.
        """
        now_mono = time.monotonic()
        if self._perf_cache is not None:
            cached_result, cached_ts = self._perf_cache
            if now_mono - cached_ts < self._perf_cache_ttl:
                return cached_result

        _EXCLUDED_REASONS = {
            'portfolio_rebalancing', 'manual_rebalance',
            'manual_liquidation_idle_capital', 'excluded_symbol_cleanup',
            'manual_b2_stablecoin_cleanup', 'regime_futures_short_spot_cleanup',
            'balance_sync', 'manual_close', 'manual_close_ghost',
        }
        real_trades = [
            pos for pos in self._closed_positions
            if getattr(pos, 'close_reason', '') not in _EXCLUDED_REASONS
        ]

        if not real_trades:
            return {
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_profit": 0.0,
                "total_loss": 0.0
            }
        
        winning_trades = [pos for pos in real_trades if float(pos.pnl) > 0]
        losing_trades = [pos for pos in real_trades if float(pos.pnl) < 0]

        total_profit = sum(float(pos.pnl) for pos in winning_trades)
        total_loss = abs(sum(float(pos.pnl) for pos in losing_trades))
        
        win_rate = (len(winning_trades) / len(real_trades) * 100) if real_trades else 0.0
        profit_factor = (total_profit / total_loss) if total_loss > 0 else 0.0
        
        # Calculate max drawdown
        cumulative_pnl = 0.0
        peak = 0.0
        max_drawdown = 0.0
        
        # Sort by exit_time to calculate drawdown chronologically
        sorted_positions = sorted(
            real_trades,
            key=lambda x: _parse_exit_time(x.exit_time)
        )
        
        for pos in sorted_positions:
            cumulative_pnl += float(pos.pnl)
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            drawdown = (peak - cumulative_pnl) / peak if peak > 0 else 0.0
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        result = {
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown": max_drawdown * 100,  # Convert to percentage
            "total_trades": len(real_trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "total_profit": total_profit,
            "total_loss": total_loss,
            "timestamp": datetime.utcnow().isoformat()
        }
        self._perf_cache = (result, time.monotonic())
        return result
