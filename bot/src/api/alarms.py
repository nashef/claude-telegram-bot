"""
FastAPI application for alarm management.
Provides HTTP endpoints for creating, listing, updating, and deleting alarms.
"""
import logging
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from jinja2 import Template

from src.config.settings import settings
from src.database.manager import db_manager

logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Alarm API",
    description="HTTP API for managing alarms",
    version="1.0.0"
)


# Pydantic models
class AlarmCreate(BaseModel):
    """Request model for creating an alarm."""
    user_id: int
    prompt: str
    one_shot_time: Optional[datetime] = None
    cron_schedule: Optional[str] = None
    alarm_name: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": 123456789,
                "alarm_name": "daily_log_check",
                "prompt": "Remind me to check the logs",
                "one_shot_time": "2024-12-31T23:59:00"
            }
        }


class AlarmUpdate(BaseModel):
    """Request model for updating an alarm."""
    alarm_name: Optional[str] = None
    prompt: Optional[str] = None
    status: Optional[str] = None
    one_shot_time: Optional[datetime] = None
    cron_schedule: Optional[str] = None


class AlarmResponse(BaseModel):
    """Response model for an alarm."""
    id: str
    user_id: int
    alarm_name: Optional[str] = None
    prompt: str
    one_shot_time: Optional[datetime] = None
    cron_schedule: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime


class VoiceMessage(BaseModel):
    """Request model for voice assistant messages."""
    message: str
    user_id: Optional[int] = None

    class Config:
        json_schema_extra = {
            "example": {
                "message": "What time is it?",
                "user_id": 123456789
            }
        }


class VoiceMessageResponse(BaseModel):
    """Response model for voice message submission."""
    status: str
    message: str


# Routes
@app.post("/alarms", response_model=AlarmResponse, status_code=201)
async def create_alarm(alarm: AlarmCreate) -> dict:
    """
    Create a new alarm.

    Either `one_shot_time` (for one-time alarms) or `cron_schedule` (for recurring alarms)
    must be provided, but not both.
    """
    try:
        # Validate timing mechanism
        if not alarm.one_shot_time and not alarm.cron_schedule:
            raise HTTPException(
                status_code=400,
                detail="Alarm must have either one_shot_time or cron_schedule"
            )

        if alarm.one_shot_time and alarm.cron_schedule:
            raise HTTPException(
                status_code=400,
                detail="Alarm cannot have both one_shot_time and cron_schedule"
            )
        # Validate that user_id is in ALLOWED_USERS
        if alarm.user_id not in settings.allowed_users:
            raise HTTPException(
                status_code=403,
                detail=f"User {alarm.user_id} is not authorized to create alarms"
            )

        # Generate a unique alarm ID
        alarm_id = f"api_{alarm.user_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        db_manager.create_alarm(
            alarm_id=alarm_id,
            user_id=alarm.user_id,
            prompt=alarm.prompt,
            one_shot_time=alarm.one_shot_time,
            cron_schedule=alarm.cron_schedule,
            alarm_name=alarm.alarm_name
        )

        # Return the created alarm
        created_alarm = db_manager.get_alarm(alarm_id)
        if created_alarm:
            return created_alarm
        else:
            raise HTTPException(status_code=500, detail="Failed to retrieve created alarm")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating alarm: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/alarms/{alarm_id}", response_model=AlarmResponse)
async def get_alarm(alarm_id: str) -> dict:
    """Get an alarm by ID."""
    alarm = db_manager.get_alarm(alarm_id)
    if alarm is None:
        raise HTTPException(status_code=404, detail="Alarm not found")
    return alarm


@app.get("/alarms", response_model=list[AlarmResponse])
async def list_alarms(user_id: Optional[int] = None, status: Optional[str] = None) -> list:
    """
    List alarms.

    If user_id is provided, only return alarms for that user.
    If status is provided, only return alarms with that status.
    """
    if user_id is not None:
        alarms = db_manager.get_user_alarms(user_id, status=status)
    else:
        alarms = db_manager.get_active_alarms() if status == "active" else []
        if status is None:
            # Get all alarms (both active and fired)
            alarms = db_manager.get_active_alarms()
            # TODO: Add method to get all alarms regardless of status if needed

    return alarms


@app.get("/alarms/by-name/{alarm_name}", response_model=AlarmResponse)
async def get_alarm_by_name(alarm_name: str, user_id: int) -> dict:
    """
    Get a specific alarm by exact name for a user.

    Requires user_id query parameter to identify which user's alarm to retrieve.
    """
    try:
        # Validate user is authorized
        if user_id not in settings.allowed_users:
            raise HTTPException(
                status_code=403,
                detail=f"User {user_id} is not authorized"
            )

        alarm = db_manager.get_alarm_by_name(user_id, alarm_name)
        if alarm is None:
            raise HTTPException(
                status_code=404,
                detail=f"Alarm '{alarm_name}' not found for user {user_id}"
            )
        return alarm

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving alarm by name: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/alarms/search/{alarm_name}", response_model=list[AlarmResponse])
async def search_alarms_by_name(alarm_name: str, user_id: int) -> list:
    """
    Search for alarms by name pattern (case-insensitive partial match).

    Requires user_id query parameter to identify which user's alarms to search.
    """
    try:
        # Validate user is authorized
        if user_id not in settings.allowed_users:
            raise HTTPException(
                status_code=403,
                detail=f"User {user_id} is not authorized"
            )

        alarms = db_manager.get_user_alarms_by_name(user_id, alarm_name)
        return alarms

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching alarms by name: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/alarms/named-only/{user_id}", response_model=list[AlarmResponse])
async def list_named_alarms(user_id: int, status: Optional[str] = None) -> list:
    """
    List all named alarms for a user (filters out alarms without names).

    Optionally filter by status (active, fired, disabled).
    """
    try:
        # Validate user is authorized
        if user_id not in settings.allowed_users:
            raise HTTPException(
                status_code=403,
                detail=f"User {user_id} is not authorized"
            )

        alarms = db_manager.get_user_named_alarms(user_id, status=status)
        return alarms

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing named alarms: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/alarms/{alarm_id}", response_model=AlarmResponse)
async def update_alarm(alarm_id: str, alarm_update: AlarmUpdate) -> dict:
    """Update an alarm."""
    # Check if alarm exists
    existing = db_manager.get_alarm(alarm_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Alarm not found")

    try:
        success = db_manager.update_alarm(
            alarm_id,
            alarm_name=alarm_update.alarm_name,
            prompt=alarm_update.prompt,
            status=alarm_update.status,
            one_shot_time=alarm_update.one_shot_time,
            cron_schedule=alarm_update.cron_schedule
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to update alarm")

        # Return updated alarm
        updated_alarm = db_manager.get_alarm(alarm_id)
        if updated_alarm:
            return updated_alarm
        else:
            raise HTTPException(status_code=500, detail="Failed to retrieve updated alarm")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating alarm {alarm_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/alarms/{alarm_id}", status_code=204)
async def delete_alarm(alarm_id: str) -> None:
    """Delete an alarm."""
    # Check if alarm exists
    existing = db_manager.get_alarm(alarm_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Alarm not found")

    try:
        db_manager.delete_alarm(alarm_id)
    except Exception as e:
        logger.error(f"Error deleting alarm {alarm_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/voice", response_model=VoiceMessageResponse, status_code=200)
async def receive_voice_message(voice_msg: VoiceMessage) -> dict:
    """
    Receive a voice assistant message from an external source.

    The message is queued for Claude processing and returns immediately.
    Response handling is asynchronous and out-of-band.
    """
    try:
        # Import here to avoid circular dependency
        from src.handlers.message_handler import claude_queue, ClaudeRequest

        # Determine which user to route to
        user_id = voice_msg.user_id if voice_msg.user_id is not None else settings.allowed_users[0]

        # Validate user is authorized
        if user_id not in settings.allowed_users:
            raise HTTPException(
                status_code=403,
                detail=f"User {user_id} is not authorized"
            )

        # Render the prompt template with the message
        template = Template(settings.voice_assist_prompt)
        rendered_prompt = template.render(message=voice_msg.message)

        # Create request and enqueue
        request = ClaudeRequest(
            prompt=rendered_prompt,
            update=None,
            context=None,
            source="voice_assistant",
            user_id=user_id,
            alarm_id=None,
            original_message=voice_msg.message  # Store original message for Telegram notification
        )

        await claude_queue.put(request)

        return {
            "status": "queued",
            "message": "Voice message received and queued for processing"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing voice message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
