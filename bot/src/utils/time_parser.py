"""
Natural language time parsing utilities for alarm system.
Handles conversion between user's local timezone and UTC storage format.
"""
import re
from datetime import datetime, timedelta
from typing import Optional, Union
import logging

import dateparser
import pytz

logger = logging.getLogger(__name__)


def parse_natural_time(
    time_str: str,
    user_tz: str = "UTC",
    base_time: Optional[datetime] = None
) -> datetime:
    """
    Parse natural language time string and convert to UTC.

    Args:
        time_str: Natural language time string like "tomorrow at 2pm" or "in 3 hours"
        user_tz: User's timezone string (e.g., "America/Denver")
        base_time: Base time for relative parsing (defaults to now)

    Returns:
        Naive UTC datetime for storage in database

    Raises:
        ValueError: If time string cannot be parsed

    Examples:
        >>> parse_natural_time("tomorrow at 2pm", "America/Denver")
        datetime(2024, 12, 1, 21, 0, 0)  # 2pm MT = 9pm UTC

        >>> parse_natural_time("in 3 hours", "America/New_York")
        datetime(2024, 11, 30, 18, 0, 0)  # 3 hours from now in UTC
    """
    # Validate timezone
    try:
        tz = pytz.timezone(user_tz)
    except pytz.exceptions.UnknownTimeZoneError:
        logger.warning(f"Unknown timezone '{user_tz}', falling back to UTC")
        tz = pytz.UTC
        user_tz = "UTC"

    # Use current time in user's timezone as base if not provided
    if base_time is None:
        base_time = datetime.now(tz)
    elif base_time.tzinfo is None:
        # Make base_time timezone-aware if it isn't already
        base_time = tz.localize(base_time)

    # Configure dateparser settings
    settings = {
        'TIMEZONE': user_tz,
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future',  # Prefer future dates for ambiguous input
        'RELATIVE_BASE': base_time,
    }

    # Try to parse the time string
    parsed = dateparser.parse(time_str, settings=settings)

    if not parsed:
        raise ValueError(f"Could not parse time: '{time_str}'")

    # Ensure the datetime is timezone-aware
    if parsed.tzinfo is None:
        parsed = tz.localize(parsed)

    # Convert to UTC and make naive for database storage
    utc_time = parsed.astimezone(pytz.UTC).replace(tzinfo=None)

    logger.debug(f"Parsed '{time_str}' in {user_tz} -> {utc_time} UTC")

    return utc_time


def parse_time_delta(delta_str: str) -> datetime:
    """
    Parse simple time delta format (backward compatibility).

    Args:
        delta_str: Time delta string like "5m", "1h", "2d"

    Returns:
        UTC datetime that is delta_str in the future

    Raises:
        ValueError: If format is invalid

    Examples:
        >>> parse_time_delta("5m")  # 5 minutes from now
        >>> parse_time_delta("2h")  # 2 hours from now
        >>> parse_time_delta("1d")  # 1 day from now
    """
    # Match format: number + unit (m/h/d)
    match = re.match(r'^(\d+)([mhd])$', delta_str.strip())
    if not match:
        raise ValueError(f"Invalid time delta format: '{delta_str}'. Use format like '5m', '1h', or '2d'")

    amount = int(match.group(1))
    unit = match.group(2)

    now = datetime.utcnow()

    if unit == 'm':
        return now + timedelta(minutes=amount)
    elif unit == 'h':
        return now + timedelta(hours=amount)
    elif unit == 'd':
        return now + timedelta(days=amount)
    else:
        raise ValueError(f"Unknown time unit: '{unit}'")


def format_datetime_local(
    dt: datetime,
    user_tz: str = "UTC",
    include_tz: bool = True
) -> str:
    """
    Format a UTC datetime for display in user's local timezone.

    Args:
        dt: Naive UTC datetime from database
        user_tz: User's timezone string
        include_tz: Whether to include timezone abbreviation in output

    Returns:
        Formatted datetime string in user's timezone

    Examples:
        >>> dt = datetime(2024, 12, 1, 21, 0, 0)  # 9pm UTC
        >>> format_datetime_local(dt, "America/Denver")
        "Dec 01, 2024 02:00 PM MST"
    """
    # Make the UTC datetime timezone-aware
    utc_dt = pytz.UTC.localize(dt)

    # Convert to user's timezone
    try:
        tz = pytz.timezone(user_tz)
        local_dt = utc_dt.astimezone(tz)
    except pytz.exceptions.UnknownTimeZoneError:
        logger.warning(f"Unknown timezone '{user_tz}', using UTC")
        local_dt = utc_dt
        tz = pytz.UTC

    # Format the datetime
    if include_tz:
        return local_dt.strftime("%b %d, %Y %I:%M %p %Z")
    else:
        return local_dt.strftime("%b %d, %Y %I:%M %p")


def parse_time_flexible(
    time_str: str,
    user_tz: str = "UTC"
) -> datetime:
    """
    Flexible time parser that tries multiple parsing strategies.

    Supports:
    - Natural language: "tomorrow at 2pm", "next Monday", "in 3 hours"
    - ISO format: "2024-12-31T23:59:00"
    - Simple delta: "5m", "1h", "2d"

    Args:
        time_str: Time string in any supported format
        user_tz: User's timezone for natural language parsing

    Returns:
        Naive UTC datetime for database storage

    Raises:
        ValueError: If no parsing strategy succeeds
    """
    # Strategy 1: Try ISO format first (fastest, most precise)
    try:
        # ISO format is assumed to be in user's local time unless it has timezone info
        if 'T' in time_str and ':' in time_str:
            # Check if it has timezone info
            if time_str.endswith('Z') or '+' in time_str[-6:] or '-' in time_str[-6:]:
                # Has timezone info, parse directly
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                if dt.tzinfo:
                    return dt.astimezone(pytz.UTC).replace(tzinfo=None)
                else:
                    return dt
            else:
                # No timezone info, assume user's local time
                dt = datetime.fromisoformat(time_str)
                tz = pytz.timezone(user_tz)
                local_dt = tz.localize(dt)
                return local_dt.astimezone(pytz.UTC).replace(tzinfo=None)
    except (ValueError, TypeError):
        pass

    # Strategy 2: Try simple delta format (backward compatibility)
    if re.match(r'^\d+[mhd]$', time_str.strip()):
        try:
            return parse_time_delta(time_str)
        except ValueError:
            pass

    # Strategy 3: Natural language parsing (most flexible, slowest)
    try:
        return parse_natural_time(time_str, user_tz)
    except ValueError:
        pass

    # No strategy worked
    raise ValueError(
        f"Could not parse time '{time_str}'. Supported formats:\n"
        f"  - Natural language: 'tomorrow at 2pm', 'in 3 hours'\n"
        f"  - ISO format: '2024-12-31T23:59:00'\n"
        f"  - Simple delta: '5m', '1h', '2d'"
    )