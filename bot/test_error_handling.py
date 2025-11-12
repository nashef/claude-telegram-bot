#!/usr/bin/env python3
"""
Test script to verify error handling and categorization.
"""

import sys
import asyncio
from unittest.mock import Mock, AsyncMock
from telegram import Update, Message, User, Chat
from telegram.error import NetworkError, TimedOut, RetryAfter, TelegramError

# Add src to path
sys.path.insert(0, '/home/leaf/claude-code-telegram-gcp/bot')

from src.utils.error_handler import categorize_error, error_handler, ErrorCategory


def test_error_categorization():
    """Test that errors are categorized correctly."""
    print("Testing error categorization...\n")

    test_cases = [
        # (Exception, Expected Category, Description)
        (ConnectionError("Network is unreachable"), ErrorCategory.NETWORK, "Connection error"),
        (NetworkError("Connection failed"), ErrorCategory.NETWORK, "Network error"),
        (TimeoutError("Request timed out"), ErrorCategory.TIMEOUT, "Timeout error"),
        (asyncio.TimeoutError("Operation timed out"), ErrorCategory.TIMEOUT, "Asyncio timeout"),
        (TimedOut("Telegram timeout"), ErrorCategory.TIMEOUT, "Telegram timeout"),
        (RetryAfter(60), ErrorCategory.RATE_LIMIT, "Rate limit error"),
        (FileNotFoundError("File not found"), ErrorCategory.NOT_FOUND, "File not found"),
        (PermissionError("Access denied"), ErrorCategory.PERMISSION, "Permission error"),
        (ValueError("Invalid value"), ErrorCategory.INVALID_INPUT, "Value error"),
        (KeyError("Key not found"), ErrorCategory.INVALID_INPUT, "Key error"),
        (MemoryError("Out of memory"), ErrorCategory.GENERIC, "Memory error"),
        (RuntimeError("Claude API timeout"), ErrorCategory.TIMEOUT, "Claude timeout"),
        (RuntimeError("Claude rate limit exceeded"), ErrorCategory.RATE_LIMIT, "Claude rate limit"),
        (Exception("Generic error"), ErrorCategory.GENERIC, "Generic exception"),
    ]

    passed = 0
    failed = 0

    for error, expected_category, description in test_cases:
        category, message = categorize_error(error)

        if category == expected_category:
            print(f"✅ {description}: {category} - {message[:50]}...")
            passed += 1
        else:
            print(f"❌ {description}: Expected {expected_category}, got {category}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


async def test_error_handler_decorator():
    """Test that the error handler decorator works correctly."""
    print("\n" + "="*60)
    print("Testing error handler decorator...")
    print("="*60 + "\n")

    # Create mock objects
    mock_update = Mock(spec=Update)
    mock_message = Mock(spec=Message)
    mock_user = Mock(spec=User)
    mock_chat = Mock(spec=Chat)

    # Setup mock relationships
    mock_user.id = 12345
    mock_chat.id = 12345
    mock_message.text = "Test message"
    mock_message.reply_text = AsyncMock()
    mock_update.message = mock_message
    mock_update.effective_user = mock_user
    mock_update.effective_chat = mock_chat
    mock_update.callback_query = None

    mock_context = Mock()

    # Test function that raises an error
    @error_handler
    async def failing_handler(update, context):
        raise ConnectionError("Network is unreachable")

    # Test function that succeeds
    @error_handler
    async def working_handler(update, context):
        return "Success"

    # Test the failing handler
    print("Testing handler with network error...")
    result = await failing_handler(mock_update, mock_context)

    # Check that reply_text was called with error message
    mock_message.reply_text.assert_called_once()
    call_args = mock_message.reply_text.call_args
    error_msg = call_args[0][0] if call_args[0] else call_args[1].get('text', '')

    print(f"Error message sent: {error_msg}")
    assert "Network" in error_msg or "network" in error_msg
    assert result is None  # Handler returns None on error
    print("✅ Network error handled correctly\n")

    # Reset mock
    mock_message.reply_text.reset_mock()

    # Test the working handler
    print("Testing handler that succeeds...")
    result = await working_handler(mock_update, mock_context)

    # Check that reply_text was NOT called
    mock_message.reply_text.assert_not_called()
    assert result == "Success"
    print("✅ Successful handler works correctly\n")

    # Test with cancellation
    @error_handler
    async def cancelled_handler(update, context):
        raise asyncio.CancelledError()

    print("Testing handler with cancellation...")
    try:
        await cancelled_handler(mock_update, mock_context)
        print("❌ CancelledError should propagate")
        return False
    except asyncio.CancelledError:
        print("✅ CancelledError propagates correctly")

    return True


async def main():
    """Run all tests."""
    print("="*60)
    print("ERROR HANDLING TEST SUITE")
    print("="*60 + "\n")

    # Test error categorization
    categorization_ok = test_error_categorization()

    # Test decorator
    decorator_ok = await test_error_handler_decorator()

    print("\n" + "="*60)
    if categorization_ok and decorator_ok:
        print("✅ ALL TESTS PASSED!")
    else:
        print("❌ SOME TESTS FAILED")
    print("="*60)

    return 0 if (categorization_ok and decorator_ok) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)