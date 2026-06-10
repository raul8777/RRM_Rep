"""
Position Manager Module

Manages open and closed trades, tracks PnL, and handles automatic position closures.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    side: str
    quantity: float
    entry_price: float
    entry_time: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_id: Optional[int] = None
    metadata: Optional[Dict] = None


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


class PositionManager:
    """Manages trading positions and lifecycle."""
    
    def __init__(self, db_client=None):
        """
        Initialize Position Manager.
        
        Args:
            db_client: Database client for persistent storage (optional)
        """
        self.db_client = db_client
        self._open_positions: Dict[str, Position] = {}
        logger.info("Position Manager initialized")
    
    def add_position(self, position: Position) -> None:
        """
        Add a new open position.
        
        Args:
            position: Position to add
        """
        self._open_positions[position.symbol] = position
        
        if self.db_client:
            try:
                query = """
                    INSERT INTO trades_open 
                    (symbol, side, quantity, price, strategy_id, entry_time, stop_loss, take_profit, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        position.metadata
                    )
                )
                logger.info(f"Position added to database: {position.symbol}")
            except Exception as e:
                logger.error(f"Failed to save position to database: {e}")
        
        logger.info(f"Position opened: {position.symbol} {position.side} {position.quantity} @ {position.entry_price}")
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """
        Get an open position by symbol.
        
        Args:
            symbol: Trading pair symbol
            
        Returns:
            Position if found, None otherwise
        """
        return self._open_positions.get(symbol)
    
    def get_all_positions(self) -> Dict[str, Position]:
        """
        Get all open positions.
        
        Returns:
            Dictionary of symbol -> Position
        """
        return dict(self._open_positions)
    
    def close_position(
        self,
        symbol: str,
        exit_price: float,
        close_reason: str = "manual"
    ) -> Optional[ClosedPosition]:
        """
        Close an open position.
        
        Args:
            symbol: Trading pair symbol
            exit_price: Exit price
            close_reason: Reason for closing
            
        Returns:
            ClosedPosition if position was open, None otherwise
        """
        position = self._open_positions.get(symbol)
        if not position:
            logger.warning(f"No open position found for {symbol}")
            return None
        
        # Calculate PnL
        if position.side.upper() == "BUY":
            pnl = (exit_price - position.entry_price) * position.quantity
            pnl_percentage = ((exit_price - position.entry_price) / position.entry_price) * 100
        else:  # SELL
            pnl = (position.entry_price - exit_price) * position.quantity
            pnl_percentage = ((position.entry_price - exit_price) / position.entry_price) * 100
        
        # Create closed position
        closed_position = ClosedPosition(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            entry_time=position.entry_time,
            exit_time=datetime.utcnow().isoformat(),
            pnl=pnl,
            pnl_percentage=pnl_percentage,
            close_reason=close_reason,
            strategy_id=position.strategy_id,
            metadata=position.metadata
        )
        
        # Remove from open positions
        del self._open_positions[symbol]
        
        # Save to database
        if self.db_client:
            try:
                # Move from trades_open to trades_closed
                self._close_position_in_db(position, closed_position)
            except Exception as e:
                logger.error(f"Failed to close position in database: {e}")
        
        logger.info(
            f"Position closed: {symbol} PnL={pnl:.2f} ({pnl_percentage:.2f}%) reason={close_reason}"
        )
        
        return closed_position
    
    def _close_position_in_db(self, position: Position, closed_position: ClosedPosition) -> None:
        """Move position from trades_open to trades_closed in database."""
        # Insert into trades_closed
        query = """
            INSERT INTO trades_closed 
            (symbol, side, quantity, entry_price, exit_price, strategy_id, entry_time, exit_time, 
             pnl, pnl_percentage, close_reason, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        self.db_client.execute_update(
            query,
            (
                closed_position.symbol,
                closed_position.side,
                closed_position.quantity,
                closed_position.entry_price,
                closed_position.exit_price,
                closed_position.strategy_id,
                closed_position.entry_time,
                closed_position.exit_time,
                closed_position.pnl,
                closed_position.pnl_percentage,
                closed_position.close_reason,
                closed_position.metadata
            )
        )
        
        # Delete from trades_open
        delete_query = "DELETE FROM trades_open WHERE symbol = %s"
        self.db_client.execute_update(delete_query, (position.symbol,))
    
    def check_stop_loss_take_profit(self, current_prices: Dict[str, float]) -> List[ClosedPosition]:
        """
        Check if any positions hit stop loss or take profit.
        
        Args:
            current_prices: Dictionary of symbol -> current price
            
        Returns:
            List of positions that were closed
        """
        closed_positions = []
        
        for symbol, position in list(self._open_positions.items()):
            current_price = current_prices.get(symbol)
            if not current_price:
                continue
            
            close_reason = None
            
            # Check stop loss
            if position.stop_loss:
                if position.side.upper() == "BUY" and current_price <= position.stop_loss:
                    close_reason = "stop_loss"
                elif position.side.upper() == "SELL" and current_price >= position.stop_loss:
                    close_reason = "stop_loss"
            
            # Check take profit
            if position.take_profit:
                if position.side.upper() == "BUY" and current_price >= position.take_profit:
                    close_reason = "take_profit"
                elif position.side.upper() == "SELL" and current_price <= position.take_profit:
                    close_reason = "take_profit"
            
            if close_reason:
                closed = self.close_position(symbol, current_price, close_reason)
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
        total_pnl_unrealized = 0.0
        
        for symbol, position in self._open_positions.items():
            # For simplicity, we'll use entry price as current price
            # In production, this should use current market price
            position_value = position.quantity * position.entry_price
            total_value += position_value
        
        return {
            "open_positions_count": len(self._open_positions),
            "total_value_usdt": total_value,
            "total_pnl_unrealized": total_pnl_unrealized,
            "positions": {
                symbol: {
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "stop_loss": pos.stop_loss,
                    "take_profit": pos.take_profit,
                    "entry_time": pos.entry_time
                }
                for symbol, pos in self._open_positions.items()
            },
            "timestamp": datetime.utcnow().isoformat()
        }
