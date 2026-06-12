"""
Position Manager Module

Manages open and closed trades, tracks PnL, and handles automatic position closures.
"""

import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import error_collector as ec

logger = logging.getLogger(__name__)


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
        logger.info("Position Manager initialized")
        
        # Load positions from database if available
        if self.db_client:
            self._load_positions_from_db()
    
    def _load_positions_from_db(self) -> None:
        """Load open positions from database."""
        try:
            # Load ALL open positions (multiple per symbol supported)
            query = """
                SELECT id, symbol, side, quantity, price as entry_price,
                       strategy_id, entry_time, stop_loss, take_profit, metadata,
                       entry_commission, pnl_prediction, indicators_at_entry
                FROM trades_open
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
                    stop_loss=row['stop_loss'],
                    take_profit=row['take_profit'],
                    strategy_id=row['strategy_id'],
                    metadata=row['metadata'],
                    entry_commission=row.get('entry_commission'),
                    pnl_prediction=row.get('pnl_prediction'),
                    indicators_at_entry=row.get('indicators_at_entry'),
                    db_id=row.get('id')
                )
                if position.symbol not in self._open_positions:
                    self._open_positions[position.symbol] = []
                self._open_positions[position.symbol].append(position)
            total = sum(len(v) for v in self._open_positions.values())
            logger.info(f"Loaded {total} open positions ({len(self._open_positions)} symbols) from database")
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

    def add_position(self, position: Position) -> None:
        """
        Add a new open position.
        
        Args:
            position: Position to add
        """
        if position.symbol not in self._open_positions:
            self._open_positions[position.symbol] = []
        self._open_positions[position.symbol].append(position)
        
        if self.db_client:
            try:
                query = """
                    INSERT INTO trades_open 
                    (symbol, side, quantity, price, strategy_id, entry_time, stop_loss, take_profit, metadata, entry_commission, pnl_prediction, indicators_at_entry)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                def _to_json(v):
                    return json.dumps(v) if isinstance(v, (dict, list)) else v

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
                        _to_json(position.metadata),
                        position.entry_commission,
                        _to_json(position.pnl_prediction),
                        _to_json(position.indicators_at_entry)
                    )
                )
                logger.info(f"Position added to database: {position.symbol}")
            except Exception as e:
                logger.error(f"Failed to save position to database: {e}")
                ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to save position {position.symbol}: {e}")
        
        logger.info(f"Position opened: {position.symbol} {position.side} {position.quantity} @ {position.entry_price}")
    
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
                    SELECT id, symbol, side, quantity, price as entry_price,
                           strategy_id, entry_time, stop_loss, take_profit, metadata,
                           entry_commission, pnl_prediction, indicators_at_entry
                    FROM trades_open
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
                        stop_loss=row['stop_loss'],
                        take_profit=row['take_profit'],
                        strategy_id=row['strategy_id'],
                        metadata=row['metadata'],
                        entry_commission=row.get('entry_commission'),
                        pnl_prediction=row.get('pnl_prediction'),
                        indicators_at_entry=row.get('indicators_at_entry'),
                        db_id=row.get('id')
                    )
                    if position.symbol not in self._open_positions:
                        self._open_positions[position.symbol] = []
                    self._open_positions[position.symbol].append(position)
            except Exception as e:
                logger.warning(f"Could not refresh positions from DB: {e}")
        return dict(self._open_positions)

    def sync_with_exchange_balance(self, binance_client, settings, min_value_usdt: float = 5.0) -> List[str]:
        """
        Import orphan assets from Binance balance as managed positions.
        Runs at startup to ensure pre-existing or untracked holdings are managed with SL/TP.

        Args:
            binance_client: BinanceClient instance
            settings: Settings instance (for SL/TP config)
            min_value_usdt: Minimum asset value in USDT to import (default $5)

        Returns:
            List of symbols imported as new positions
        """
        EXCLUDED_ASSETS = {"USDT", "BNB", "BTTC", "WIN", "TRX", "JST", "IQ"}
        imported = []
        try:
            balances = binance_client.get_account_balance()
            existing = set(self.get_all_positions().keys())

            for asset, amount in balances.items():
                if asset in EXCLUDED_ASSETS or amount <= 0:
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

                sl_price = price * (1 - settings.stop_loss_pct / 100)
                tp_price = price * (1 + settings.take_profit_pct / 100)
                commission_rate = getattr(settings, 'binance_commission_rate', 0.001)
                entry_commission = amount * price * commission_rate

                pos = Position(
                    symbol=symbol,
                    side="BUY",
                    quantity=amount,
                    entry_price=price,
                    entry_time=datetime.utcnow().isoformat() + "Z",
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    entry_commission=entry_commission,
                    metadata={
                        "source": "balance_sync",
                        "sync_time": datetime.utcnow().isoformat() + "Z",
                        "value_usdt_at_sync": round(value_usdt, 4),
                    },
                )
                self.add_position(pos)
                imported.append(symbol)
                ec.add_error(
                    ec.SOURCE_SYSTEM, ec.LEVEL_INFO,
                    f"Balance sync: imported {symbol} {amount:.6f} @ {price:.4f} "
                    f"(${value_usdt:.2f}) — SL={sl_price:.4f} TP={tp_price:.4f}"
                )
                logger.info(f"Balance sync imported: {symbol} qty={amount} price={price}")
        except Exception as e:
            logger.error(f"Balance sync failed: {e}")
            ec.add_error(ec.SOURCE_SYSTEM, ec.LEVEL_ERROR, f"Balance sync error: {e}")
        return imported

    def update_position_metadata(self, position_id: str, metadata: dict) -> None:
        """Update metadata dict of an open position (e.g. trailing_peak for trailing stop)."""
        for pos_list in self._open_positions.values():
            for pos in pos_list:
                if str(pos.position_id) == str(position_id):
                    pos.metadata = metadata
                    if self.db_client:
                        try:
                            self.db_client.execute_update(
                                "UPDATE trades_open SET metadata = %s WHERE position_id = %s",
                                (json.dumps(metadata), str(position_id))
                            )
                        except Exception as e:
                            logger.warning(f"Could not persist metadata update for {position_id}: {e}")
                    return

    def _calc_close(self, position: Position, exit_price: float, commission_rate: float):
        """Shared PnL calculation for closing a position. Returns (qty, entry_p, exit_price, pnl, pnl_pct, exit_comm, pred_accuracy)."""
        qty        = float(position.quantity)
        entry_p    = float(position.entry_price)
        entry_comm = float(position.entry_commission or 0)
        exit_price = float(exit_price)
        exit_commission = qty * exit_price * commission_rate
        if position.side.upper() == "BUY":
            gross_pnl        = (exit_price - entry_p) * qty
            total_commission = entry_comm + exit_commission
            pnl              = gross_pnl - total_commission
            pnl_percentage   = ((exit_price - entry_p) / entry_p) * 100 - (total_commission / (qty * entry_p)) * 100
        else:
            gross_pnl        = (entry_p - exit_price) * qty
            total_commission = entry_comm + exit_commission
            pnl              = gross_pnl - total_commission
            pnl_percentage   = ((entry_p - exit_price) / entry_p) * 100 - (total_commission / (qty * entry_p)) * 100
        prediction_accuracy = None
        if position.pnl_prediction:
            expected_pnl = float(position.pnl_prediction.get('expected_pnl', 0))
            if expected_pnl != 0:
                prediction_accuracy = float(((pnl - expected_pnl) / abs(expected_pnl)) * 100)
        return qty, entry_p, exit_price, pnl, pnl_percentage, exit_commission, prediction_accuracy

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: float = 0.001,
        position_id: Optional[str] = None
    ) -> Optional[ClosedPosition]:
        """
        Close a specific open position for a symbol.

        Args:
            symbol: Trading pair symbol
            exit_price: Exit price
            close_reason: Reason for closing
            commission_rate: Commission rate for exit trade
            position_id: If provided, closes the entry with this position_id;
                         otherwise closes the oldest entry (FIFO).

        Returns:
            ClosedPosition if position was found, None otherwise
        """
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

        _, entry_p, exit_p, pnl, pnl_pct, exit_comm, pred_acc = self._calc_close(target, exit_price, commission_rate)

        closed_position = ClosedPosition(
            symbol=target.symbol,
            side=target.side,
            quantity=float(target.quantity),
            entry_price=entry_p,
            exit_price=exit_p,
            entry_time=target.entry_time,
            exit_time=datetime.utcnow().isoformat(),
            pnl=pnl,
            pnl_percentage=pnl_pct,
            close_reason=close_reason,
            strategy_id=target.strategy_id,
            metadata=target.metadata,
            entry_commission=float(target.entry_commission or 0),
            exit_commission=exit_comm,
            pnl_prediction=target.pnl_prediction,
            indicators_at_entry=target.indicators_at_entry,
            prediction_accuracy=pred_acc
        )

        # Remove this specific entry from in-memory list
        positions.remove(target)
        if not positions:
            del self._open_positions[symbol]

        self._closed_positions.append(closed_position)

        if self.db_client:
            try:
                self._close_position_in_db(target, closed_position)
            except Exception as e:
                logger.error(f"Failed to close position in database: {e}")
                ec.add_error(ec.SOURCE_DB, ec.LEVEL_ERROR, f"Failed to close position {symbol} in DB: {e}")

        logger.info(f"Position closed: {symbol} PnL={pnl:.2f} ({pnl_pct:.2f}%) reason={close_reason}")
        return closed_position

    def close_all_positions_for_symbol(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual",
        commission_rate: float = 0.001
    ) -> List[ClosedPosition]:
        """
        Close ALL open positions for a symbol (used on full liquidation).

        Returns:
            List of ClosedPosition objects
        """
        positions = list(self._open_positions.get(symbol, []))
        results = []
        for pos in positions:
            closed = self.close_position(symbol, exit_price, close_reason, commission_rate, position_id=pos.position_id)
            if closed:
                results.append(closed)
        return results
    
    def _close_position_in_db(self, position: Position, closed_position: ClosedPosition) -> None:
        """Move a single position entry from trades_open to trades_closed in database."""
        def _to_json(v):
            return json.dumps(v) if isinstance(v, (dict, list)) else v

        def _safe_float(v):
            return float(v) if v is not None else None

        insert_query = """
            INSERT INTO trades_closed 
            (symbol, side, quantity, entry_price, exit_price, strategy_id, entry_time, exit_time, 
             pnl, pnl_percentage, close_reason, metadata, entry_commission, exit_commission, 
             pnl_prediction, indicators_at_entry, prediction_accuracy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self.db_client.execute_update(
            insert_query,
            (
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
                _to_json(closed_position.metadata),
                _safe_float(closed_position.entry_commission),
                _safe_float(closed_position.exit_commission),
                _to_json(closed_position.pnl_prediction),
                _to_json(closed_position.indicators_at_entry),
                _safe_float(closed_position.prediction_accuracy)
            )
        )

        # Delete by primary key when available, otherwise fallback to symbol
        if position.db_id:
            self.db_client.execute_update("DELETE FROM trades_open WHERE id = %s", (position.db_id,))
        else:
            self.db_client.execute_update(
                "DELETE FROM trades_open WHERE symbol = %s AND entry_time = %s",
                (position.symbol, position.entry_time)
            )
    
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
        Get the last N closed positions, querying DB directly when limit > in-memory cache.
        
        Args:
            limit: Maximum number of closed positions to return
            
        Returns:
            List of ClosedPosition objects, most recent first
        """
        # If DB available and caller wants more than we have cached, hit DB directly
        if self.db_client and limit > len(self._closed_positions):
            try:
                query = """
                    SELECT symbol, side, quantity, entry_price, exit_price,
                           entry_time, exit_time, pnl, pnl_percentage, close_reason,
                           strategy_id, metadata, entry_commission, exit_commission,
                           pnl_prediction, indicators_at_entry, prediction_accuracy
                    FROM trades_closed
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
            key=lambda x: x.exit_time,
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
        period_positions = [
            pos for pos in self._closed_positions
            if datetime.fromisoformat(pos.exit_time) >= cutoff
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

        if not self._closed_positions:
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
        
        winning_trades = [pos for pos in self._closed_positions if float(pos.pnl) > 0]
        losing_trades = [pos for pos in self._closed_positions if float(pos.pnl) < 0]

        total_profit = sum(float(pos.pnl) for pos in winning_trades)
        total_loss = abs(sum(float(pos.pnl) for pos in losing_trades))
        
        win_rate = (len(winning_trades) / len(self._closed_positions) * 100) if self._closed_positions else 0.0
        profit_factor = (total_profit / total_loss) if total_loss > 0 else 0.0
        
        # Calculate max drawdown
        cumulative_pnl = 0.0
        peak = 0.0
        max_drawdown = 0.0
        
        # Sort by exit_time to calculate drawdown chronologically
        sorted_positions = sorted(
            self._closed_positions,
            key=lambda x: x.exit_time
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
            "total_trades": len(self._closed_positions),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "total_profit": total_profit,
            "total_loss": total_loss,
            "timestamp": datetime.utcnow().isoformat()
        }
        self._perf_cache = (result, time.monotonic())
        return result
