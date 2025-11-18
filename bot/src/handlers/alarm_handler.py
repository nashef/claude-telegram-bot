"""
Alarm handler for scheduled and one-time alarms.
"""
import asyncio
import logging
from datetime import datetime
from croniter import croniter
from typing import Optional

from src.config.settings import settings
from src.database.manager import db_manager
from src.handlers.message_handler import claude_queue, ClaudeRequest, _last_request

logger = logging.getLogger(__name__)


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
                now = datetime.utcnow()

                for alarm in alarms:
                    alarm_time = None

                    # Check one-shot alarm
                    if alarm["one_shot_time"]:
                        if alarm["one_shot_time"] <= now:
                            # This alarm is due right now
                            alarm_time = now
                        else:
                            alarm_time = alarm["one_shot_time"]

                    # Check recurring alarm (cron)
                    elif alarm["cron_schedule"]:
                        try:
                            cron = croniter(alarm["cron_schedule"], now)
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
                now = datetime.utcnow()
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

    except Exception as e:
        logger.error(f"Failed to fire alarm {alarm['id']}: {e}", exc_info=True)
        # Consider disabling problematic alarms
        if "prompt" not in alarm or not alarm["prompt"]:
            logger.error(f"Alarm {alarm['id']} has no prompt, disabling")
            db_manager.update_alarm(alarm["id"], status="disabled")