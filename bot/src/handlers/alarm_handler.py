"""
Alarm handler for scheduled and one-time alarms.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from croniter import croniter
from typing import Optional
import pytz

from src.config.settings import settings
from src.database.manager import db_manager
from src.handlers.message_handler import claude_queue, ClaudeRequest, _last_request

logger = logging.getLogger(__name__)


def get_user_now():
    """Get current time in user's timezone."""
    tz = pytz.timezone(settings.user_timezone)
    return datetime.now(tz)


async def alarm_worker(shutdown_event=None):
    """
    Main alarm worker coroutine.
    Monitors active alarms and queues them when they're due.
    Uses dynamic timeout based on next alarm time.
    """
    logger.info("Alarm worker started")

    try:
        while True:
            # Check for shutdown
            if shutdown_event and shutdown_event.is_set():
                logger.info("Alarm worker: shutdown event detected, exiting")
                break

            try:
                # Get all active alarms
                alarms = db_manager.get_active_alarms()

                if not alarms:
                    # No active alarms, sleep for a bit (but interruptible)
                    for _ in range(int(settings.alarm_max_timeout)):
                        if shutdown_event and shutdown_event.is_set():
                            logger.info("Alarm worker: shutdown during sleep")
                            return
                        await asyncio.sleep(1)
                    continue

                # Find the next alarm to fire
                next_fire_time = None
                next_alarm = None
                now = get_user_now()

                for alarm in alarms:
                    alarm_time = None

                    # Check one-shot alarm
                    if alarm["one_shot_time"]:
                        # Make one_shot_time timezone-aware if it isn't
                        # One-shot times are stored as naive UTC datetimes
                        one_shot = alarm["one_shot_time"]
                        if one_shot.tzinfo is None:
                            # Treat as UTC, then convert to user timezone
                            one_shot = pytz.UTC.localize(one_shot)
                            tz = pytz.timezone(settings.user_timezone)
                            one_shot = one_shot.astimezone(tz)

                        if one_shot <= now:
                            # This alarm is due right now
                            alarm_time = now
                        else:
                            alarm_time = one_shot

                    # Check recurring alarm (cron)
                    elif alarm["cron_schedule"]:
                        try:
                            # Add 1 second to handle exact boundary condition
                            # (get_prev at exactly 6:00:00 returns yesterday's 6 AM, not today's)
                            cron = croniter(alarm["cron_schedule"], now + timedelta(seconds=1))
                            # Get the most recent cron slot (at or before now+1s)
                            prev_slot = cron.get_prev(datetime)
                            # Make timezone-aware if needed
                            if prev_slot.tzinfo is None:
                                tz = pytz.timezone(settings.user_timezone)
                                prev_slot = tz.localize(prev_slot)

                            # Check if we're within the cron slot (within 60 seconds of the scheduled time)
                            # This handles the case where we check at exactly 6:00:00 or shortly after
                            time_since_slot = (now - prev_slot).total_seconds()
                            if 0 <= time_since_slot < 60:
                                # We're in the current cron window - check if already fired
                                last_fired = alarm.get("last_fired")
                                if last_fired:
                                    # Make last_fired timezone-aware if needed
                                    if last_fired.tzinfo is None:
                                        tz = pytz.timezone(settings.user_timezone)
                                        last_fired = tz.localize(last_fired)
                                    # Check if we already fired within this minute
                                    time_since_fired = (now - last_fired).total_seconds()
                                    if time_since_fired < 60:
                                        # Already fired this slot, get next occurrence
                                        alarm_time = cron.get_next(datetime)
                                    else:
                                        # Haven't fired recently, fire now
                                        alarm_time = now
                                else:
                                    # Never fired, fire now
                                    alarm_time = now
                            else:
                                # Get next occurrence
                                alarm_time = cron.get_next(datetime)
                        except Exception as e:
                            logger.error(f"Invalid cron schedule for alarm {alarm['id']}: {e}")
                            # Disable this alarm
                            db_manager.update_alarm(alarm["id"], status="disabled")
                            continue

                    # Update next alarm if this one is sooner
                    if alarm_time:
                        if next_fire_time is None or alarm_time < next_fire_time:
                            next_fire_time = alarm_time
                            next_alarm = alarm

                if next_alarm is None:
                    # No valid alarms, sleep with interruption checks
                    for _ in range(int(settings.alarm_max_timeout)):
                        if shutdown_event and shutdown_event.is_set():
                            logger.info("Alarm worker: shutdown during sleep")
                            return
                        await asyncio.sleep(1)
                    continue

                # Calculate timeout until next alarm
                now = get_user_now()
                time_until_alarm = (next_fire_time - now).total_seconds()

                if time_until_alarm <= 0:
                    # Alarm is due now
                    await _fire_alarm(next_alarm)
                    continue

                # Sleep until next alarm, but cap at ALARM_MAX_TIMEOUT and make it interruptible
                sleep_time = min(time_until_alarm, settings.alarm_max_timeout)
                logger.debug(f"Alarm worker sleeping for {sleep_time:.1f}s until next alarm")

                # Sleep in 1-second intervals for shutdown responsiveness
                for _ in range(int(sleep_time)):
                    if shutdown_event and shutdown_event.is_set():
                        logger.info("Alarm worker: shutdown during sleep")
                        return
                    await asyncio.sleep(1)
                # Handle fractional seconds
                remaining = sleep_time - int(sleep_time)
                if remaining > 0:
                    await asyncio.sleep(remaining)

            except Exception as e:
                logger.error(f"Error in alarm worker: {e}", exc_info=True)
                # Sleep with interruption checks on error
                for _ in range(int(settings.alarm_max_timeout)):
                    if shutdown_event and shutdown_event.is_set():
                        logger.info("Alarm worker: shutdown during error sleep")
                        return
                    await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("Alarm worker: task cancelled")
        raise
    except Exception as e:
        logger.error(f"Fatal error in alarm worker: {e}", exc_info=True)
    finally:
        logger.info("Alarm worker: exited")


async def _fire_alarm(alarm: dict) -> None:
    """
    Fire an alarm by queueing it as a Claude request.
    Uses the last request's context to send results back to the user.
    Falls back to logging if no context is available.
    """
    logger.info(f"Firing alarm {alarm['id']} for user {alarm['user_id']}")

    try:
        # Use the last request's context if available (like heartbeats do)
        # This allows us to send results back to the user via Telegram
        # Note: _last_request is only updated with user/heartbeat requests, not alarm requests,
        # so this preserves the actual Telegram context even after multiple alarms fire
        update = None
        context = None
        has_context = False

        if _last_request and hasattr(_last_request, 'update') and hasattr(_last_request, 'context'):
            # Check that the request belongs to this user
            if (_last_request.update and
                _last_request.update.effective_user and
                _last_request.update.effective_user.id == alarm['user_id']):
                update = _last_request.update
                context = _last_request.context
                has_context = True
                logger.info(f"✅ Alarm {alarm['id']} using Telegram context from {_last_request.source} request")
            else:
                logger.info(f"⚠️ Last request is for different user, alarm will run without context")
        else:
            logger.info(f"⚠️ No last request context available, alarm will run without Telegram context")

        # Queue the alarm as a Claude request
        await claude_queue.put(ClaudeRequest(
            prompt=alarm["prompt"],
            update=update if has_context else None,
            context=context if has_context else None,
            source="alarm",
            alarm_id=alarm["id"],
            user_id=alarm["user_id"]
        ))

        logger.info(f"✅ Alarm {alarm['id']} queued successfully")

        # Handle one-shot alarms
        if alarm["one_shot_time"]:
            logger.info(f"Disabling one-shot alarm {alarm['id']}")
            db_manager.update_alarm(alarm["id"], status="completed")
        # Handle recurring alarms - update last_fired timestamp
        elif alarm["cron_schedule"]:
            logger.info(f"Updating last_fired for recurring alarm {alarm['id']}")
            db_manager.update_alarm(alarm["id"], last_fired=datetime.now())

    except Exception as e:
        logger.error(f"Failed to fire alarm {alarm['id']}: {e}", exc_info=True)
        # Consider disabling problematic alarms
        if "prompt" not in alarm or not alarm["prompt"]:
            logger.error(f"Alarm {alarm['id']} has no prompt, disabling")
            db_manager.update_alarm(alarm["id"], status="disabled")