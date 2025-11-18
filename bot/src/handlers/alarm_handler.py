"""
Alarm handler for scheduling one-shot and recurring alarms.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import uuid
from croniter import croniter

from src.config.settings import settings
from src.database.manager import db_manager
from src.handlers.message_handler import claude_queue, ClaudeRequest, _last_request

logger = logging.getLogger(__name__)


async def alarm_worker():
    """
    Main alarm worker coroutine.
    Monitors active alarms and queues them when they're due.
    Uses dynamic timeout based on next alarm time.
    """
    logger.info("Alarm worker started")

    while True:
        try:
            # Get all active alarms
            alarms = db_manager.get_active_alarms()

            if not alarms:
                # No active alarms, sleep for a bit
                await asyncio.sleep(settings.alarm_max_timeout)
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
                # No valid alarms
                await asyncio.sleep(settings.alarm_max_timeout)
                continue

            # Calculate timeout until next alarm
            now = datetime.utcnow()
            time_until_alarm = (next_fire_time - now).total_seconds()

            if time_until_alarm <= 0:
                # Alarm is due now
                await _fire_alarm(next_alarm)
                continue

            # Sleep until next alarm, but cap at ALARM_MAX_TIMEOUT
            sleep_time = min(time_until_alarm, settings.alarm_max_timeout)
            logger.debug(f"Alarm worker sleeping for {sleep_time:.1f}s until next alarm")
            await asyncio.sleep(sleep_time)

        except Exception as e:
            logger.error(f"Error in alarm worker: {e}", exc_info=True)
            await asyncio.sleep(settings.alarm_max_timeout)


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
        update = None
        context = None
        has_context = False

        if _last_request:
            update = _last_request.update
            context = _last_request.context
            if update and context:
                has_context = True
                logger.info(f"Alarm {alarm['id']} has user context, will send to Telegram")
            else:
                logger.warning(f"Alarm {alarm['id']} found last_request but missing update/context")
        else:
            logger.warning(f"Alarm {alarm['id']} has no last_request context, result will be logged only")

        # Create a request with the alarm prompt
        request = ClaudeRequest(
            prompt=alarm["prompt"],
            update=update,
            context=context,
            source="alarm",
            user_id=alarm["user_id"],
            alarm_id=alarm["id"]
        )

        # Queue the request
        await claude_queue.put(request)
        logger.info(f"Alarm {alarm['id']} queued for execution")

        # Update alarm status if it's a one-shot
        if alarm["one_shot_time"]:
            db_manager.update_alarm(alarm["id"], status="fired")
            logger.info(f"One-shot alarm {alarm['id']} marked as fired")

    except Exception as e:
        logger.error(f"Error firing alarm {alarm['id']}: {e}", exc_info=True)


def create_alarm(
    user_id: int,
    prompt: str,
    one_shot_time: Optional[datetime] = None,
    cron_schedule: Optional[str] = None
) -> str:
    """
    Create a new alarm.

    Args:
        user_id: Telegram user ID
        prompt: Prompt to send to Claude
        one_shot_time: UTC datetime for one-shot alarm
        cron_schedule: Cron schedule string for recurring alarm

    Returns:
        Alarm ID
    """
    # Validate that either one_shot_time or cron_schedule is provided
    if not one_shot_time and not cron_schedule:
        raise ValueError("Either one_shot_time or cron_schedule must be provided")

    # Validate cron if provided
    if cron_schedule:
        try:
            croniter(cron_schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron schedule: {e}")

    # Generate alarm ID
    alarm_id = str(uuid.uuid4())

    # Create alarm in database
    db_manager.create_alarm(
        alarm_id=alarm_id,
        user_id=user_id,
        prompt=prompt,
        one_shot_time=one_shot_time,
        cron_schedule=cron_schedule
    )

    logger.info(f"Alarm created: {alarm_id}")
    return alarm_id
