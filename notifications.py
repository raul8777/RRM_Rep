"""
Notifications Module

Handles notifications for dashboard and mobile app.
"""

import logging
from typing import Dict, List, Optional, Callable
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Notification types."""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SUCCESS = "SUCCESS"


class Notification:
    """Represents a notification."""
    
    def __init__(
        self,
        notification_type: NotificationType,
        title: str,
        message: str,
        metadata: Optional[Dict] = None
    ):
        """
        Initialize notification.
        
        Args:
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            metadata: Additional metadata
        """
        self.notification_type = notification_type
        self.title = title
        self.message = message
        self.metadata = metadata or {}
        self.timestamp = datetime.utcnow()
    
    def to_dict(self) -> Dict:
        """Convert notification to dictionary."""
        return {
            "type": self.notification_type.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata
        }


class NotificationManager:
    """Manages notifications for the system."""
    
    def __init__(self, message_bus=None):
        """
        Initialize Notification Manager.
        
        Args:
            message_bus: Message bus for publishing notifications
        """
        self.message_bus = message_bus
        self.notifications: List[Notification] = []
        self.max_notifications = 100
        self.subscribers: List[Callable] = []
        
        logger.info("Notification Manager initialized")
    
    def create_notification(
        self,
        notification_type: NotificationType,
        title: str,
        message: str,
        metadata: Optional[Dict] = None
    ) -> Notification:
        """
        Create a notification.
        
        Args:
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            metadata: Additional metadata
            
        Returns:
            Created notification
        """
        notification = Notification(notification_type, title, message, metadata)
        
        # Add to history
        self.notifications.append(notification)
        if len(self.notifications) > self.max_notifications:
            self.notifications.pop(0)
        
        # Publish to message bus if available
        if self.message_bus:
            from message_bus import EVENT_NOTIFICATION
            self.message_bus.publish(
                EVENT_NOTIFICATION,
                notification.to_dict(),
                source="notification_manager"
            )
        
        # Notify subscribers
        for callback in self.subscribers:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Error in notification subscriber callback: {e}")
        
        logger.info(f"Notification created: {notification_type.value} - {title}")
        return notification
    
    def subscribe(self, callback: Callable) -> None:
        """
        Subscribe to notifications.
        
        Args:
            callback: Callback function to call when notification is created
        """
        self.subscribers.append(callback)
        logger.info("Notification subscriber added")
    
    def unsubscribe(self, callback: Callable) -> None:
        """
        Unsubscribe from notifications.
        
        Args:
            callback: Callback function to remove
        """
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            logger.info("Notification subscriber removed")
    
    def get_notifications(
        self,
        notification_type: Optional[NotificationType] = None,
        limit: int = 50
    ) -> List[Dict]:
        """
        Get notifications.
        
        Args:
            notification_type: Filter by notification type (optional)
            limit: Maximum number of notifications to return
            
        Returns:
            List of notification dictionaries
        """
        if notification_type:
            filtered = [n for n in self.notifications if n.notification_type == notification_type]
            return [n.to_dict() for n in filtered[-limit:]]
        return [n.to_dict() for n in self.notifications[-limit:]]
    
    def clear_notifications(self) -> None:
        """Clear all notifications."""
        self.notifications.clear()
        logger.info("Notifications cleared")


# Global singleton instance
notification_manager = NotificationManager()
