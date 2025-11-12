"""Telegram message handlers."""
import asyncio
import io
import logging
from dataclasses import dataclass
from asyncio import Queue
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from src.claude.cli_executor import ClaudeProcessManager, StreamUpdate
from src.security.validator import security_validator
from src.config.settings import settings

logger = logging.getLogger(__name__)

# Global executor (using CLI subprocess like richardatct)
claude_executor = ClaudeProcessManager(settings)

# Queue-based architecture to prevent race conditions
@dataclass
class ClaudeRequest:
    """Message to enqueue for Claude processing."""
    prompt: str
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    source: str  # "user_text", "photo", "audio", "heartbeat"

# Global queue - single worker processes all requests sequentially
claude_queue: Queue[ClaudeRequest] = Queue()

# Note: No confirmation system - Claude executes actions directly (like richardatct)


async def _send_typing_periodically(context: ContextTypes.DEFAULT_TYPE, update: Update):
    """Send typing indicator every 4 seconds to show bot is working."""
    try:
        while True:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING
            )
            await asyncio.sleep(4)  # Typing lasts 5s, renew every 4s
    except asyncio.CancelledError:
        # Task was cancelled, stop gracefully
        pass
    except Exception as e:
        logger.debug(f"Typing indicator error: {e}")


async def claude_worker():
    """Single worker that processes all Claude requests sequentially from the queue."""
    logger.info("Claude worker: started")

    while True:
        try:
            # Wait for next request
            request = await claude_queue.get()

            logger.info(f"Claude worker: processing {request.source} request")

            # Get source emoji for logging
            source_emoji = {
                "user_text": "🔧",
                "photo": "📷",
                "audio": "🎵",
                "heartbeat": "💭"
            }.get(request.source, "❓")

            # Send initial "processing" message
            status_prefix = {
                "user_text": "",
                "photo": "📷 Photo notification\n\n",
                "audio": "🎵 Audio notification\n\n",
                "heartbeat": "💭 Internal monologue\n\n"
            }.get(request.source, "")

            thinking_msg = await request.context.bot.send_message(
                chat_id=request.update.effective_chat.id,
                text=f"{status_prefix}🤔 Processing..."
            )

            # Start typing indicator
            typing_task = asyncio.create_task(
                _send_typing_periodically(request.context, request.update)
            )

            # Initialize streaming state
            last_update_time = 0

            # Streaming callback for this request
            async def stream_callback(update_obj: StreamUpdate):
                """Update progress message with streaming updates."""
                nonlocal last_update_time
                current_time = asyncio.get_event_loop().time()

                # Log all stream updates (not throttled)
                if update_obj.type == "tool_use":
                    logger.info(f"{source_emoji} Tool: {update_obj.content}")
                    if update_obj.tool_calls:
                        for tool in update_obj.tool_calls:
                            logger.info(f"   - {tool.get('name')}: {tool.get('input')}")
                elif update_obj.type == "assistant":
                    logger.info(f"{source_emoji} Claude: {update_obj.content[:100]}...")
                elif update_obj.type == "tool_result":
                    logger.info(f"{source_emoji} {update_obj.content}")
                elif update_obj.type == "result":
                    logger.info(f"{source_emoji} {update_obj.content}")

                # Throttle UI updates to max 1 per second
                if current_time - last_update_time < 1.0:
                    return

                last_update_time = current_time

                # Format the progress message
                progress_text = ""
                if update_obj.type == "tool_use":
                    progress_text = f"{status_prefix}🔧 **{update_obj.content}**"
                elif update_obj.type == "assistant":
                    content_preview = (
                        update_obj.content[:150] + "..."
                        if len(update_obj.content) > 150
                        else update_obj.content
                    )
                    progress_text = f"{status_prefix}🤖 **Working...**\n\n_{content_preview}_"
                elif update_obj.type == "result":
                    progress_text = f"{status_prefix}✅ **Completed!**"

                if progress_text:
                    try:
                        await thinking_msg.edit_text(progress_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.debug(f"Failed to update progress: {e}")

            # Get current session ID
            session_id = request.context.user_data.get('claude_session_id')

            # Execute Claude with the request
            logger.info(f"Claude worker: calling executor with prompt: {request.prompt[:100]}...")
            response_obj = await claude_executor.execute_command(
                prompt=request.prompt,
                working_directory=claude_executor.config.approved_directory,
                session_id=session_id,
                continue_session=bool(session_id),
                stream_callback=stream_callback
            )

            # Stop typing indicator
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

            # Update session ID
            if response_obj.session_id:
                request.context.user_data['claude_session_id'] = response_obj.session_id

            # Update activity tracker
            if 'activity_tracker' in request.context.user_data:
                request.context.user_data['activity_tracker']['time'] = asyncio.get_event_loop().time()

            # Send Claude's response
            response = response_obj.content
            if response:
                if len(response) > 4096:
                    chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]
                    await thinking_msg.delete()
                    for chunk in chunks:
                        await request.context.bot.send_message(
                            chat_id=request.update.effective_chat.id,
                            text=chunk
                        )
                else:
                    await thinking_msg.edit_text(response)
            else:
                await thinking_msg.edit_text(f"{status_prefix}(no response)")

            logger.info(f"Claude worker: completed {request.source} request")

        except Exception as e:
            logger.error(f"Claude worker: error processing request: {e}", exc_info=True)
            # Stop typing on error
            if 'typing_task' in locals():
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
            try:
                await request.context.bot.send_message(
                    chat_id=request.update.effective_chat.id,
                    text=f"❌ Error processing request: {str(e)[:100]}"
                )
            except:
                pass
        finally:
            # Mark task as done
            claude_queue.task_done()


async def _heartbeat_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    last_activity_tracker: dict
):
    """Monitor for silence and send internal monologue prompt to Claude."""
    if not settings.heartbeat_enabled:
        logger.info("Heartbeat monitor: disabled in settings")
        return

    check_interval = 10  # Check every 10 seconds
    heartbeat_triggered = False

    logger.info(f"Heartbeat monitor: started (threshold={settings.heartbeat_interval_seconds}s, check_interval={check_interval}s)")

    try:
        while True:
            await asyncio.sleep(check_interval)

            current_time = asyncio.get_event_loop().time()
            silence_duration = current_time - last_activity_tracker['time']

            logger.debug(f"Heartbeat monitor: silence={silence_duration:.1f}s, threshold={settings.heartbeat_interval_seconds}s")

            # Check if we've been silent for longer than the threshold
            if silence_duration >= settings.heartbeat_interval_seconds and not heartbeat_triggered:
                logger.info(f"🔔 Heartbeat triggered after {silence_duration:.0f}s of silence")
                heartbeat_triggered = True

                logger.info(f"Heartbeat: enqueuing internal monologue: '{settings.heartbeat_message[:50]}...'")

                # Enqueue heartbeat request to worker
                await claude_queue.put(ClaudeRequest(
                    prompt=settings.heartbeat_message,
                    update=update,
                    context=context,
                    source="heartbeat"
                ))

                # Reset flag so it can trigger again later
                heartbeat_triggered = False

    except asyncio.CancelledError:
        logger.info("Heartbeat monitor: stopped (task cancelled)")
        pass
    except Exception as e:
        logger.error(f"Heartbeat monitor error: {e}", exc_info=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id

    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    welcome_msg = (
        "🤖 **Claude Code Bot**\n\n"
        "I have access to the full Claude Code CLI.\n\n"
        "**Available tools:**\n"
        "📖 Read, ✍️ Write, ✏️ Edit\n"
        "🔧 Bash, 🔍 Glob, 🔎 Grep\n"
        "🌐 WebSearch, 📋 TodoWrite\n"
        "🎯 Task, ⚡ Skill, 🔨 SlashCommand\n\n"
        "Just send me a message!"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")


# Note: Removed detect_action_in_response - Claude executes actions directly now


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user messages."""
    user_id = update.effective_user.id
    message_text = update.message.text

    # Security checks
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    if not security_validator.check_rate_limit(user_id):
        await update.message.reply_text("⏱️ Rate limit exceeded. Please wait.")
        return

    # Get or initialize persistent activity tracker (shared across messages)
    if 'activity_tracker' not in context.user_data:
        context.user_data['activity_tracker'] = {'time': asyncio.get_event_loop().time()}
        logger.debug(f"Activity tracker initialized at {context.user_data['activity_tracker']['time']}")
    else:
        # Update activity time for new incoming message
        context.user_data['activity_tracker']['time'] = asyncio.get_event_loop().time()
        logger.debug(f"Activity tracker updated for new message at {context.user_data['activity_tracker']['time']}")

    # Cancel any existing heartbeat task and start a new one
    if settings.heartbeat_enabled:
        # Cancel old heartbeat if it exists
        old_heartbeat = context.user_data.get('heartbeat_task')
        if old_heartbeat and not old_heartbeat.done():
            logger.info("Cancelling existing heartbeat task")
            old_heartbeat.cancel()
            try:
                await old_heartbeat
            except asyncio.CancelledError:
                pass

        logger.info("Creating new heartbeat monitor task")
        heartbeat_task = asyncio.create_task(_heartbeat_monitor(update, context, context.user_data['activity_tracker']))
        context.user_data['heartbeat_task'] = heartbeat_task

    # Enqueue message for worker to process
    logger.info(f"Enqueuing user message: {message_text[:50]}...")
    await claude_queue.put(ClaudeRequest(
        prompt=message_text,
        update=update,
        context=context,
        source="user_text"
    ))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages - save to Jarvis/tmp and notify Claude."""
    user_id = update.effective_user.id

    # Security check
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Get the largest photo (best quality)
    photo = update.message.photo[-1]

    # Download the photo
    photo_file = await photo.get_file()

    # Generate filename
    import time
    timestamp = int(time.time())
    filename = f"telegram_photo_{timestamp}.jpg"
    filepath = f"{settings.approved_directory}/tmp/{filename}"

    # Ensure tmp directory exists
    import os
    os.makedirs(f"{settings.approved_directory}/tmp", exist_ok=True)

    # Download and save
    await photo_file.download_to_drive(filepath)

    logger.info(f"Saved photo to {filepath}")

    # Get caption if any
    caption = update.message.caption or "no caption"

    # Create notification message for Claude
    notification = f"leaf sent you a photo: {filepath} Caption: {caption}"

    # Enqueue photo notification for worker to process
    logger.info(f"Enqueuing photo notification: {filename}")
    await claude_queue.put(ClaudeRequest(
        prompt=notification,
        update=update,
        context=context,
        source="photo"
    ))


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle audio messages - save to Jarvis/tmp and notify Claude."""
    user_id = update.effective_user.id

    # Security check
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Get the audio file
    audio = update.message.audio or update.message.voice

    # Download the audio
    audio_file = await audio.get_file()

    # Generate filename with appropriate extension
    import time
    timestamp = int(time.time())
    # Try to get original filename or use generic name
    if update.message.audio and update.message.audio.file_name:
        original_name = update.message.audio.file_name
        extension = original_name.split('.')[-1] if '.' in original_name else 'mp3'
        filename = f"telegram_audio_{timestamp}.{extension}"
    else:
        # Voice messages are typically OGG format
        filename = f"telegram_voice_{timestamp}.ogg"

    filepath = f"{settings.approved_directory}/tmp/{filename}"

    # Ensure tmp directory exists
    import os
    os.makedirs(f"{settings.approved_directory}/tmp", exist_ok=True)

    # Download and save
    await audio_file.download_to_drive(filepath)

    logger.info(f"Saved audio to {filepath}")

    # Get caption if any
    caption = update.message.caption or "no caption"

    # Determine if it's a voice message or audio file
    audio_type = "voice message" if update.message.voice else "audio file"

    # Create notification message for Claude
    notification = f"leaf sent you a {audio_type}: {filepath} Caption: {caption}"

    # Enqueue audio notification for worker to process
    logger.info(f"Enqueuing audio notification: {filename}")
    await claude_queue.put(ClaudeRequest(
        prompt=notification,
        update=update,
        context=context,
        source="audio"
    ))
