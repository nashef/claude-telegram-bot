"""
Error handling utilities for graceful exception recovery.
"""
import asyncio
import functools
import logging
from typing import Callable, Any
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError, NetworkError, TimedOut, RetryAfter

logger = logging.getLogger(__name__)


class ErrorCategory:
    """Categorize errors for appropriate user messaging."""
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    CLAUDE_ERROR = "claude_error"
    GENERIC = "generic"


def categorize_error(error: Exception) -> tuple[ErrorCategory, str]:
    """
    Categorize an exception and return appropriate user message.

    Returns:
        (category, user_friendly_message)
    """
    error_name = type(error).__name__
    error_str = str(error)[:200]  # Truncate long error messages

    # Network and connectivity errors
    if isinstance(error, (NetworkError, ConnectionError, OSError)):
        if "Connection" in error_str or "Network" in error_str:
            return (ErrorCategory.NETWORK,
                   "⚠️ Network connection issue. Please try again in a moment.")

    # Timeout errors
    if isinstance(error, (TimedOut, asyncio.TimeoutError)):
        return (ErrorCategory.TIMEOUT,
               "⏱️ Request timed out. Please try again with a simpler request.")

    # Rate limiting
    if isinstance(error, RetryAfter):
        retry_after = getattr(error, 'retry_after', 60)
        return (ErrorCategory.RATE_LIMIT,
               f"⏸️ Rate limit reached. Please wait {retry_after} seconds.")

    # Telegram API errors
    if isinstance(error, TelegramError):
        if "forbidden" in error_str.lower() or "unauthorized" in error_str.lower():
            return (ErrorCategory.PERMISSION,
                   "🔒 Permission denied. Please check bot permissions.")
        elif "not found" in error_str.lower():
            return (ErrorCategory.NOT_FOUND,
                   "❓ Resource not found. Please try again.")

    # Permission and access errors
    if isinstance(error, PermissionError):
        return (ErrorCategory.PERMISSION,
               "🔒 Access denied. Insufficient permissions.")

    # File and IO errors
    if isinstance(error, (FileNotFoundError, IOError)):
        if isinstance(error, FileNotFoundError):
            return (ErrorCategory.NOT_FOUND,
                   "📁 File not found. Please check the file path.")
        else:
            return (ErrorCategory.GENERIC,
                   "💾 File operation failed. Please try again.")

    # Value and input errors
    if isinstance(error, (ValueError, KeyError, IndexError, TypeError)):
        return (ErrorCategory.INVALID_INPUT,
               "❌ Invalid input or data format. Please check your message.")

    # Claude-specific errors (check message content)
    if "claude" in error_str.lower() or "api" in error_str.lower():
        if "timeout" in error_str.lower():
            return (ErrorCategory.TIMEOUT,
                   "⏱️ Claude is taking too long. Please try a simpler request.")
        elif "rate" in error_str.lower() or "limit" in error_str.lower():
            return (ErrorCategory.RATE_LIMIT,
                   "⏸️ Claude API limit reached. Please wait a moment.")
        else:
            return (ErrorCategory.CLAUDE_ERROR,
                   "🤖 Claude encountered an error. Please try again.")

    # Memory errors
    if isinstance(error, MemoryError):
        return (ErrorCategory.GENERIC,
               "💭 Out of memory. Please try a smaller request.")

    # Default fallback
    return (ErrorCategory.GENERIC,
           "❌ An error occurred. Please try again.\n"
           f"Error: `{error_name}`")


def error_handler(func: Callable) -> Callable:
    """
    Decorator to handle errors in message handlers gracefully.
    Sends user-friendly error messages and continues processing.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except asyncio.CancelledError:
            # Let cancellation propagate for clean shutdown
            raise
        except Exception as e:
            # Log the full error
            logger.error(
                f"Error in {func.__name__} for user {update.effective_user.id if update.effective_user else 'unknown'}: {e}",
                exc_info=True,
                extra={
                    'handler': func.__name__,
                    'user_id': update.effective_user.id if update.effective_user else None,
                    'user_message': update.message.text[:100] if update.message and update.message.text else None,
                }
            )

            # Categorize and get user-friendly message
            category, user_message = categorize_error(e)

            # Log category for metrics
            logger.info(f"Error category: {category} for {func.__name__}")

            # Try to send error message to user
            try:
                if update.message:
                    await update.message.reply_text(
                        user_message,
                        parse_mode="Markdown"
                    )
                elif update.callback_query:
                    await update.callback_query.answer(
                        text=user_message[:200],  # Callback answers have length limit
                        show_alert=True
                    )
            except Exception as send_error:
                logger.error(f"Failed to send error message to user: {send_error}")

            # Don't re-raise - let the bot continue
            return None

    return wrapper


def resilient_task(func: Callable) -> Callable:
    """
    Decorator for background tasks that should log errors but not crash.
    Different from error_handler as it doesn't send user messages.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            # Let cancellation propagate for clean shutdown
            logger.info(f"{func.__name__} cancelled")
            raise
        except Exception as e:
            logger.error(
                f"Error in background task {func.__name__}: {e}",
                exc_info=True
            )
            # Don't re-raise - let the task exit gracefully
            return None

    return wrapper