"""
Database models for persistent storage.
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

Base = declarative_base()


class Config(Base):
    """Configuration key-value store."""
    __tablename__ = "config"

    key = Column(String(255), primary_key=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Config(key={self.key}, value={self.value[:50] if self.value else None})>"


class UserSession(Base):
    """Store Claude session IDs per user."""
    __tablename__ = "sessions"

    user_id = Column(Integer, primary_key=True, nullable=False)
    session_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    extra_data = Column(JSON, nullable=True)  # For future use

    def __repr__(self):
        return f"<UserSession(user_id={self.user_id}, session_id={self.session_id})>"


class ProcessTracker(Base):
    """Track active Claude processes."""
    __tablename__ = "processes"

    process_id = Column(String(255), primary_key=True, nullable=False)
    user_id = Column(Integer, nullable=False)
    command = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default="running")  # running, completed, killed
    ended_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ProcessTracker(process_id={self.process_id}, user_id={self.user_id}, status={self.status})>"


class ErrorLog(Base):
    """Log errors for analysis."""
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    error_type = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    handler = Column(String(100), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    extra_data = Column(JSON, nullable=True)

    def __repr__(self):
        return f"<ErrorLog(id={self.id}, user_id={self.user_id}, error_type={self.error_type})>"


class BotState(Base):
    """Global bot state (paused, debug mode, etc)."""
    __tablename__ = "bot_state"

    key = Column(String(100), primary_key=True, nullable=False)
    value = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BotState(key={self.key}, value={self.value})>"


# Database connection management
_engine = None
_SessionLocal = None


def init_database(database_url: str) -> None:
    """Initialize database connection and create tables."""
    global _engine, _SessionLocal

    _engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {}
    )

    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

    # Create all tables
    Base.metadata.create_all(bind=_engine)


def get_session() -> Session:
    """Get a database session."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _SessionLocal()


def close_database() -> None:
    """Close database connection."""
    global _engine
    if _engine:
        _engine.dispose()
        _engine = None