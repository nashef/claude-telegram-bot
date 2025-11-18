"""
FastAPI application for alarm management.
Provides HTTP endpoints for creating, listing, updating, and deleting alarms.
"""
import logging
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.config.settings import settings
from src.database.manager import db_manager
from src.handlers.alarm_handler import create_alarm as create_alarm_handler

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

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": 123456789,
                "prompt": "Remind me to check the logs",
                "one_shot_time": "2024-12-31T23:59:00"
            }
        }


class AlarmUpdate(BaseModel):
    """Request model for updating an alarm."""
    prompt: Optional[str] = None
    status: Optional[str] = None
    one_shot_time: Optional[datetime] = None
    cron_schedule: Optional[str] = None


class AlarmResponse(BaseModel):
    """Response model for an alarm."""
    id: str
    user_id: int
    prompt: str
    one_shot_time: Optional[datetime] = None
    cron_schedule: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime


# Routes
@app.post("/alarms", response_model=AlarmResponse, status_code=201)
async def create_alarm(alarm: AlarmCreate) -> dict:
    """
    Create a new alarm.

    Either `one_shot_time` (for one-time alarms) or `cron_schedule` (for recurring alarms)
    must be provided, but not both.
    """
    try:
        alarm_id = create_alarm_handler(
            user_id=alarm.user_id,
            prompt=alarm.prompt,
            one_shot_time=alarm.one_shot_time,
            cron_schedule=alarm.cron_schedule
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


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
