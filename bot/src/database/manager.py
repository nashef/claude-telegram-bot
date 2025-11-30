"""
Database manager for common operations.
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from contextlib import contextmanager

from .models import (
    get_session, Config, UserSession, ProcessTracker,
    ErrorLog, BotState, Alarm
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
            logger.info(f"Config set: {key} = {value}")

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
                    user_session.extra_data = metadata
            else:
                user_session = UserSession(
                    user_id=user_id,
                    session_id=session_id,
                    extra_data=metadata
                )
                session.add(user_session)
            logger.info(f"Session set for user {user_id}: {session_id}")

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
        """Track a new Claude process (upsert: update if exists, insert if new)."""
        with db_session() as session:
            process = session.query(ProcessTracker).filter_by(process_id=process_id).first()
            if process:
                # Update existing process
                process.command = command if command else None
                process.status = "running"
                process.started_at = datetime.utcnow()
                process.ended_at = None
                logger.info(f"Process updated: {process_id} for user {user_id}")
            else:
                # Create new process
                process = ProcessTracker(
                    process_id=process_id,
                    user_id=user_id,
                    command=command if command else None,
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
    def get_active_processes() -> List[dict]:
        """Get all running processes as dictionaries (to avoid detached instance errors)."""
        with db_session() as session:
            processes = session.query(ProcessTracker).filter_by(status="running").all()
            # Convert to dicts while still in session context
            return [
                {
                    "process_id": p.process_id,
                    "user_id": p.user_id,
                    "command": p.command,
                    "started_at": p.started_at,
                    "status": p.status,
                    "ended_at": p.ended_at,
                }
                for p in processes
            ]

    @staticmethod
    def get_user_processes(user_id: int, only_active: bool = True) -> List[dict]:
        """Get user's processes as dictionaries (to avoid detached instance errors)."""
        with db_session() as session:
            query = session.query(ProcessTracker).filter_by(user_id=user_id)
            if only_active:
                query = query.filter_by(status="running")
            processes = query.all()
            # Convert to dicts while still in session context
            return [
                {
                    "process_id": p.process_id,
                    "user_id": p.user_id,
                    "command": p.command,
                    "started_at": p.started_at,
                    "status": p.status,
                    "ended_at": p.ended_at,
                }
                for p in processes
            ]

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

    # Alarm operations
    @staticmethod
    def create_alarm(
        alarm_id: str,
        user_id: int,
        prompt: str,
        one_shot_time: Optional[datetime] = None,
        cron_schedule: Optional[str] = None,
        alarm_name: Optional[str] = None
    ) -> None:
        """Create a new alarm."""
        # Validate that at least one timing mechanism is provided
        if not one_shot_time and not cron_schedule:
            raise ValueError("Alarm must have either one_shot_time or cron_schedule")

        # Validate that both aren't provided (they're mutually exclusive)
        if one_shot_time and cron_schedule:
            raise ValueError("Alarm cannot have both one_shot_time and cron_schedule")

        with db_session() as session:
            alarm = Alarm(
                id=alarm_id,
                user_id=user_id,
                alarm_name=alarm_name,
                prompt=prompt,
                one_shot_time=one_shot_time,
                cron_schedule=cron_schedule,
                status="active"
            )
            session.add(alarm)
            logger.info(f"Alarm created: {alarm_id} ({alarm_name}) for user {user_id}")

    @staticmethod
    def get_alarm(alarm_id: str) -> Optional[dict]:
        """Get an alarm by ID."""
        with db_session() as session:
            alarm = session.query(Alarm).filter_by(id=alarm_id).first()
            if alarm:
                return {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
            return None

    @staticmethod
    def get_user_alarms(user_id: int, status: Optional[str] = None) -> List[dict]:
        """Get user's alarms."""
        with db_session() as session:
            query = session.query(Alarm).filter_by(user_id=user_id)
            if status:
                query = query.filter_by(status=status)
            alarms = query.all()
            return [
                {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
                for alarm in alarms
            ]

    @staticmethod
    def get_active_alarms() -> List[dict]:
        """Get all active alarms."""
        with db_session() as session:
            alarms = session.query(Alarm).filter_by(status="active").all()
            return [
                {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
                for alarm in alarms
            ]

    @staticmethod
    def update_alarm(
        alarm_id: str,
        prompt: Optional[str] = None,
        status: Optional[str] = None,
        one_shot_time: Optional[datetime] = None,
        cron_schedule: Optional[str] = None,
        alarm_name: Optional[str] = None
    ) -> bool:
        """Update an alarm."""
        with db_session() as session:
            alarm = session.query(Alarm).filter_by(id=alarm_id).first()
            if alarm:
                if prompt is not None:
                    alarm.prompt = prompt
                if status is not None:
                    alarm.status = status
                if one_shot_time is not None:
                    alarm.one_shot_time = one_shot_time
                if cron_schedule is not None:
                    alarm.cron_schedule = cron_schedule
                if alarm_name is not None:
                    alarm.alarm_name = alarm_name
                alarm.updated_at = datetime.utcnow()
                logger.info(f"Alarm updated: {alarm_id} ({alarm_name})")
                return True
            return False

    @staticmethod
    def delete_alarm(alarm_id: str) -> bool:
        """Delete an alarm."""
        with db_session() as session:
            alarm = session.query(Alarm).filter_by(id=alarm_id).first()
            if alarm:
                session.delete(alarm)
                logger.info(f"Alarm deleted: {alarm_id}")
                return True
            return False

    @staticmethod
    def get_alarm_by_name(user_id: int, alarm_name: str) -> Optional[dict]:
        """Get a specific alarm by name for a user."""
        with db_session() as session:
            alarm = session.query(Alarm).filter_by(user_id=user_id, alarm_name=alarm_name).first()
            if alarm:
                return {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
            return None

    @staticmethod
    def get_user_alarms_by_name(user_id: int, alarm_name: str) -> List[dict]:
        """Get all alarms for a user that match a name pattern (case-insensitive partial match)."""
        with db_session() as session:
            alarms = session.query(Alarm).filter(
                Alarm.user_id == user_id,
                Alarm.alarm_name.ilike(f"%{alarm_name}%")
            ).all()
            return [
                {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
                for alarm in alarms
            ]

    @staticmethod
    def get_user_named_alarms(user_id: int, status: Optional[str] = None) -> List[dict]:
        """Get all named alarms for a user (filters out alarms without names)."""
        with db_session() as session:
            query = session.query(Alarm).filter(
                Alarm.user_id == user_id,
                Alarm.alarm_name.isnot(None)
            )
            if status:
                query = query.filter_by(status=status)
            alarms = query.all()
            return [
                {
                    "id": alarm.id,
                    "user_id": alarm.user_id,
                    "alarm_name": alarm.alarm_name,
                    "prompt": alarm.prompt,
                    "one_shot_time": alarm.one_shot_time,
                    "cron_schedule": alarm.cron_schedule,
                    "status": alarm.status,
                    "created_at": alarm.created_at,
                    "updated_at": alarm.updated_at,
                }
                for alarm in alarms
            ]


# Singleton instance
db_manager = DatabaseManager()