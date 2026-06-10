"""
Message Bus Module

Handles inter-agent communication using in-memory message bus (can be extended to Redis).
"""

import logging
import json
from typing import Dict, List, Callable, Optional, Any
from datetime import datetime
from collections import defaultdict
from threading import Lock

logger = logging.getLogger(__name__)


class MessageBus:
    """In-memory message bus for inter-agent communication."""
    
    def __init__(self):
        """Initialize Message Bus."""
        self.subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self.lock = Lock()
        self.message_history: List[Dict] = []
        self.max_history = 1000
        
        logger.info("Message Bus initialized")
    
    def publish(self, event_type: str, data: Dict, source: str = "unknown") -> None:
        """
        Publish a message to all subscribers of the event type.
        
        Args:
            event_type: Type of event (e.g., "SIGNAL", "ORDER", "POSITION_CLOSED")
            data: Message data
            source: Source of the message
        """
        message = {
            "event_type": event_type,
            "data": data,
            "source": source,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        with self.lock:
            # Add to history
            self.message_history.append(message)
            if len(self.message_history) > self.max_history:
                self.message_history.pop(0)
            
            # Notify subscribers
            for callback in self.subscribers[event_type]:
                try:
                    callback(message)
                except Exception as e:
                    logger.error(f"Error in subscriber callback for {event_type}: {e}")
        
        logger.debug(f"Published event {event_type} from {source}")
    
    def subscribe(self, event_type: str, callback: Callable) -> None:
        """
        Subscribe to an event type.
        
        Args:
            event_type: Type of event to subscribe to
            callback: Callback function to call when event is published
        """
        with self.lock:
            self.subscribers[event_type].append(callback)
        
        logger.info(f"Subscribed to event type: {event_type}")
    
    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """
        Unsubscribe from an event type.
        
        Args:
            event_type: Type of event to unsubscribe from
            callback: Callback function to remove
        """
        with self.lock:
            if callback in self.subscribers[event_type]:
                self.subscribers[event_type].remove(callback)
        
        logger.info(f"Unsubscribed from event type: {event_type}")
    
    def get_history(self, event_type: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        Get message history.
        
        Args:
            event_type: Filter by event type (optional)
            limit: Maximum number of messages to return
            
        Returns:
            List of messages
        """
        with self.lock:
            if event_type:
                filtered = [m for m in self.message_history if m["event_type"] == event_type]
                return filtered[-limit:]
            return self.message_history[-limit:]
    
    def clear_history(self) -> None:
        """Clear message history."""
        with self.lock:
            self.message_history.clear()
            logger.info("Message history cleared")


# Global singleton instance
message_bus = MessageBus()


# Event type constants
EVENT_SIGNAL = "SIGNAL"
EVENT_ORDER = "ORDER"
EVENT_POSITION_OPENED = "POSITION_OPENED"
EVENT_POSITION_CLOSED = "POSITION_CLOSED"
EVENT_CAPITAL_REALLOCATION = "CAPITAL_REALLOCATION"
EVENT_POSITION_CLOSURE_REQUEST = "POSITION_CLOSURE_REQUEST"
EVENT_SYSTEM_STATE_CHANGE = "SYSTEM_STATE_CHANGE"
EVENT_ERROR = "ERROR"
EVENT_NOTIFICATION = "NOTIFICATION"
