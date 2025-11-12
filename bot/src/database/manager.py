"""
Database manager for common operations.
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from contextlib import contextmanager

from .models import (
    get_session, Config, UserSession, ProcessTracker,
    ErrorLog, BotState
)

logger = logging.getLogger(__name__)


@contextmanager
def db_session():
    """Context manager for database sessions."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        session.close()


class DatabaseManager:
    """Manager for database operations."""

    # Config operations
    @staticmethod
    def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
        """Get configuration value."""
        with db_session() as session:
            config = session.query(Config).filter_by(key=key).first()
            return config.value if config else default

    @staticmethod
    def set_config(key: str, value: str) -> None:
        """Set configuration value."""
        with db_session() as session:
            config = session.query(Config).filter_by(key=key).first()
            if config:
                config.value = value
                config.updated_at = datetime.utcnow()
            else:
                config = Config(key=key, value=value)
                session.add(config)
            logger.info(f"Config set: {key} = {value[:50]}...")

    @staticmethod
    def delete_config(key: str) -> bool:
        """Delete configuration value."""
        with db_session() as session:
            config = session.query(Config).filter_by(key=key).first()
            if config:
                session.delete(config)
                logger.info(f"Config deleted: {key}")
                return True
            return False

    # Session operations
    @staticmethod
    def get_user_session(user_id: int) -> Optional[str]:
        """Get user's Claude session ID."""
        with db_session() as session:
            user_session = session.query(UserSession).filter_by(user_id=user_id).first()
            if user_session:
                # Update last used
                user_session.last_used = datetime.utcnow()
                return user_session.session_id
            return None

    @staticmethod
    def set_user_session(user_id: int, session_id: str, metadata: Optional[Dict] = None) -> None:
        """Set user's Claude session ID."""
        with db_session() as session:
            user_session = session.query(UserSession).filter_by(user_id=user_id).first()
            if user_session:
                user_session.session_id = session_id
                user_session.last_used = datetime.utcnow()
                if metadata:
                    user_session.metadata = metadata
            else:
                user_session = UserSession(
                    user_id=user_id,
                    session_id=session_id,
                    metadata=metadata
                )
                session.add(user_session)
            logger.info(f"Session set for user {user_id}: {session_id[:20]}...")

    @staticmethod
    def clear_user_session(user_id: int) -> bool:
        """Clear user's session."""
        with db_session() as session:
            user_session = session.query(UserSession).filter_by(user_id=user_id).first()
            if user_session:
                session.delete(user_session)
                logger.info(f"Session cleared for user {user_id}")
                return True
            return False

    @staticmethod
    def get_all_sessions() -> List[UserSession]:
        """Get all active sessions."""
        with db_session() as session:
            return session.query(UserSession).all()

    # Process tracking
    @staticmethod
    def track_process(process_id: str, user_id: int, command: str) -> None:
        """Track a new Claude process."""
        with db_session() as session:
            process = ProcessTracker(
                process_id=process_id,
                user_id=user_id,
                command=command[:500] if command else None,  # Truncate long commands
                status="running"
            )
            session.add(process)
            logger.info(f"Process tracked: {process_id} for user {user_id}")

    @staticmethod
    def update_process_status(process_id: str, status: str) -> None:
        """Update process status."""
        with db_session() as session:
            process = session.query(ProcessTracker).filter_by(process_id=process_id).first()
            if process:
                process.status = status
                if status in ["completed", "killed"]:
                    process.ended_at = datetime.utcnow()
                logger.info(f"Process {process_id} status: {status}")

    @staticmethod
    def get_active_processes() -> List[ProcessTracker]:
        """Get all running processes."""
        with db_session() as session:
            return session.query(ProcessTracker).filter_by(status="running").all()

    @staticmethod
    def get_user_processes(user_id: int, only_active: bool = True) -> List[ProcessTracker]:
        """Get user's processes."""
        with db_session() as session:
            query = session.query(ProcessTracker).filter_by(user_id=user_id)
            if only_active:
                query = query.filter_by(status="running")
            return query.all()

    # Bot state operations
    @staticmethod
    def get_bot_state(key: str, default: Optional[str] = None) -> Optional[str]:
        """Get bot state value."""
        with db_session() as session:
            state = session.query(BotState).filter_by(key=key).first()
            return state.value if state else default

    @staticmethod
    def set_bot_state(key: str, value: str) -> None:
        """Set bot state value."""
        with db_session() as session:
            state = session.query(BotState).filter_by(key=key).first()
            if state:
                state.value = value
                state.updated_at = datetime.utcnow()
            else:
                state = BotState(key=key, value=value)
                session.add(state)
            logger.info(f"Bot state set: {key} = {value}")

    @staticmethod
    def is_paused() -> bool:
        """Check if bot is paused."""
        return DatabaseManager.get_bot_state("paused", "false").lower() == "true"

    @staticmethod
    def is_debug_mode() -> bool:
        """Check if debug mode is enabled."""
        return DatabaseManager.get_bot_state("debug_mode", "false").lower() == "true"

    # Error logging
    @staticmethod
    def log_error(
        error_type: str,
        error_message: str,
        user_id: Optional[int] = None,
        handler: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """Log an error to database."""
        try:
            with db_session() as session:
                error_log = ErrorLog(
                    user_id=user_id,
                    error_type=error_type,
                    error_message=error_message[:1000],  # Truncate long messages
                    handler=handler,
                    metadata=metadata
                )
                session.add(error_log)
                logger.debug(f"Error logged: {error_type} for user {user_id}")
        except Exception as e:
            # Don't fail if error logging fails
            logger.error(f"Failed to log error to database: {e}")

    @staticmethod
    def get_recent_errors(limit: int = 10) -> List[ErrorLog]:
        """Get recent errors."""
        with db_session() as session:
            return session.query(ErrorLog).order_by(ErrorLog.timestamp.desc()).limit(limit).all()

    @staticmethod
    def clear_old_errors(days: int = 7) -> int:
        """Clear errors older than specified days."""
        from datetime import timedelta
        with db_session() as session:
            cutoff = datetime.utcnow() - timedelta(days=days)
            count = session.query(ErrorLog).filter(ErrorLog.timestamp < cutoff).delete()
            logger.info(f"Cleared {count} old error logs")
            return count


# Singleton instance
db_manager = DatabaseManager()