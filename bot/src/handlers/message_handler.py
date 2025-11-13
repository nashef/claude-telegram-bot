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
from src.utils.error_handler import error_handler, categorize_error
from src.database.manager import db_manager

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
    source: str  # "user_text", "photo", "audio", "document", "heartbeat"

# Global queue - single worker processes all requests sequentially
claude_queue: Queue[ClaudeRequest] = Queue()

# Store last context for heartbeat messages
_last_request: ClaudeRequest | None = None

# Message threading state per user (Twitter-style)
@dataclass
class ThreadState:
    """Track threading state for a user."""
    messages: list[str]
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    timer_task: asyncio.Task | None
    start_time: float
    reminder_sent: bool

_thread_states: dict[int, ThreadState] = {}

# Thread markers
THREAD_START_MARKERS = ["1/", "🧵"]
THREAD_END_MARKERS = ["x/", "X/", "🏁", "✅", "✔️"]

# Note: No confirmation system - Claude executes actions directly (like richardatct)


def _is_thread_start(text: str) -> bool:
    """Check if message starts a thread."""
    return any(text.startswith(marker) for marker in THREAD_START_MARKERS)


def _is_thread_end(text: str) -> bool:
    """Check if message ends a thread."""
    # Check if any end marker is at the start or end of the message
    for marker in THREAD_END_MARKERS:
        if text.startswith(marker) or text.endswith(marker):
            return True
    return False


async def _submit_thread(user_id: int):
    """Submit threaded messages for a user."""
    global _thread_states

    if user_id not in _thread_states:
        return

    thread = _thread_states[user_id]

    # Cancel timer if still running
    if thread.timer_task and not thread.timer_task.done():
        thread.timer_task.cancel()

    # Combine all messages
    combined_prompt = "\n".join(thread.messages)

    logger.info(f"Submitting thread of {len(thread.messages)} messages for user {user_id}")

    # Enqueue the combined request
    await claude_queue.put(ClaudeRequest(
        prompt=combined_prompt,
        update=thread.update,
        context=thread.context,
        source="user_text"
    ))

    # Clear thread state
    del _thread_states[user_id]


async def _thread_timer(user_id: int):
    """Timer that sends reminder after 20s if thread not completed."""
    global _thread_states

    if user_id not in _thread_states:
        return

    thread = _thread_states[user_id]

    try:
        # Wait 20 seconds
        await asyncio.sleep(20.0)

        # Check if thread still exists and reminder not sent
        if user_id in _thread_states and not thread.reminder_sent:
            thread.reminder_sent = True
            await thread.context.bot.send_message(
                chat_id=thread.update.effective_chat.id,
                text="💬 Still there? (Send **X/** to send what I have)",
                parse_mode="Markdown",
                disable_notification=True
            )
            logger.info(f"Sent thread reminder to user {user_id}")

    except asyncio.CancelledError:
        # Timer was cancelled, thread was submitted or new message arrived
        pass


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


async def claude_worker(shutdown_event=None):
    """Single worker that processes all Claude requests sequentially from the queue."""
    global _last_request
    logger.info("Claude worker: started")

    while True:
        # Check for shutdown signal
        if shutdown_event and shutdown_event.is_set():
            logger.info("Claude worker: shutdown signal received, exiting...")
            break

        dequeued = False  # Track if we actually dequeued an item
        try:
            # Wait for next request with timeout for heartbeat
            try:
                # Use a shorter timeout if shutdown is requested
                timeout = 1.0 if (shutdown_event and shutdown_event.is_set()) else settings.heartbeat_interval_seconds
                request = await asyncio.wait_for(
                    claude_queue.get(),
                    timeout=timeout
                )
                dequeued = True  # We successfully dequeued an item
            except asyncio.TimeoutError:
                # Check for shutdown again
                if shutdown_event and shutdown_event.is_set():
                    break

                # No messages for timeout period - send heartbeat if enabled
                if settings.heartbeat_enabled and _last_request is not None:
                    logger.info(f"🔔 Heartbeat triggered after {settings.heartbeat_interval_seconds}s of inactivity")
                    request = ClaudeRequest(
                        prompt=settings.heartbeat_message,
                        update=_last_request.update,
                        context=_last_request.context,
                        source="heartbeat"
                    )
                    # Note: dequeued remains False - synthetic request
                else:
                    # No last request yet or heartbeat disabled, just continue waiting
                    continue

            # Store this request for future heartbeats
            _last_request = request

            logger.info(f"Claude worker: processing {request.source} request")

            # Get source emoji for logging
            source_emoji = {
                "user_text": "🔧",
                "photo": "📷",
                "audio": "🎵",
                "document": "📄",
                "heartbeat": "💭",
                "wake_up": "👁️"
            }.get(request.source, "❓")

            # Status prefix for non-text messages
            status_prefix = {
                "user_text": "",
                "photo": "📷 Photo notification\n\n",
                "audio": "🎵 Audio notification\n\n",
                "document": "📄 File received\n\n",
                "heartbeat": "💭 Internal monologue\n\n",
                "wake_up": "👁️ Waking up\n\n"
            }.get(request.source, "")

            # No "Processing..." message - will create message only if thinking output appears
            thinking_msg = None
            typing_task = None

            # Initialize streaming state
            last_update_time = 0

            # Streaming callback for this request
            async def stream_callback(update_obj: StreamUpdate):
                """Update progress message with streaming updates (silent notifications)."""
                nonlocal last_update_time, thinking_msg
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
                    # Don't show "Completed!" - final result will be shown with notification
                    return

                if progress_text:
                    try:
                        if thinking_msg is None:
                            # Create message silently on first update
                            thinking_msg = await request.context.bot.send_message(
                                chat_id=request.update.effective_chat.id,
                                text=progress_text,
                                parse_mode="Markdown",
                                disable_notification=True  # Silent thinking updates
                            )
                        else:
                            # Update existing message (also silent)
                            await thinking_msg.edit_text(progress_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.debug(f"Failed to update progress: {e}")

            # Get current session ID from database (fallback to context)
            user_id = request.update.effective_user.id if request.update.effective_user else None
            session_id = None
            if user_id:
                session_id = db_manager.get_user_session(user_id)
            if not session_id:
                session_id = request.context.user_data.get('claude_session_id')

            # Start typing indicator when Claude subprocess starts
            typing_task = asyncio.create_task(
                _send_typing_periodically(request.context, request.update)
            )

            # Execute Claude with the request
            logger.info(f"Claude worker: calling executor with prompt: {request.prompt[:100]}...")
            response_obj = await claude_executor.execute_command(
                prompt=request.prompt,
                working_directory=claude_executor.config.approved_directory,
                session_id=session_id,
                continue_session=bool(session_id),
                stream_callback=stream_callback
            )

            # Stop typing indicator when Claude subprocess ends
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

            # Track process in database
            if user_id and response_obj.session_id:
                db_manager.track_process(response_obj.session_id, user_id, request.prompt[:500])

            # Update session ID in both database and context
            if response_obj.session_id:
                request.context.user_data['claude_session_id'] = response_obj.session_id
                if user_id:
                    db_manager.set_user_session(user_id, response_obj.session_id)

            # Update activity tracker
            if 'activity_tracker' in request.context.user_data:
                request.context.user_data['activity_tracker']['time'] = asyncio.get_event_loop().time()

            # Send Claude's response with notification enabled
            response = response_obj.content
            if response:
                if len(response) > 4096:
                    # Response too long - delete thinking msg and send in chunks
                    if thinking_msg:
                        await thinking_msg.delete()

                    chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]
                    for i, chunk in enumerate(chunks):
                        # Only notify on first chunk
                        await request.context.bot.send_message(
                            chat_id=request.update.effective_chat.id,
                            text=chunk,
                            disable_notification=(i > 0)  # Only first chunk notifies
                        )
                else:
                    # Response fits in one message
                    if thinking_msg:
                        # Edit existing thinking message
                        # Note: Telegram doesn't support changing disable_notification on edits
                        # So we delete and resend to get notification
                        await thinking_msg.delete()
                        await request.context.bot.send_message(
                            chat_id=request.update.effective_chat.id,
                            text=response,
                            disable_notification=False  # Final result notifies
                        )
                    else:
                        # No thinking message, send directly with notification
                        await request.context.bot.send_message(
                            chat_id=request.update.effective_chat.id,
                            text=response,
                            disable_notification=False
                        )
            else:
                # No response - send error message
                await request.context.bot.send_message(
                    chat_id=request.update.effective_chat.id,
                    text=f"{status_prefix}(no response)",
                    disable_notification=False
                )

            logger.info(f"Claude worker: completed {request.source} request")

        except asyncio.CancelledError:
            # Let cancellation propagate for clean shutdown
            logger.info("Claude worker: task cancelled")
            raise
        except Exception as e:
            logger.error(f"Claude worker: error processing request: {e}", exc_info=True)
            # Stop typing on error
            if 'typing_task' in locals():
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

            # Categorize error and send appropriate message
            category, user_message = categorize_error(e)
            logger.info(f"Error category: {category}")

            try:
                await request.context.bot.send_message(
                    chat_id=request.update.effective_chat.id,
                    text=user_message,
                    parse_mode="Markdown"
                )
            except Exception as send_error:
                logger.error(f"Failed to send error message: {send_error}")
                # Try simple message without markdown
                try:
                    await request.context.bot.send_message(
                        chat_id=request.update.effective_chat.id,
                        text="❌ An error occurred. Please try again."
                    )
                except:
                    pass
        finally:
            # Only mark task as done if we actually dequeued an item
            if dequeued:
                claude_queue.task_done()

    logger.info("Claude worker: exited gracefully")




@error_handler
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - send wake-up prompt to Claude."""
    user_id = update.effective_user.id

    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Send wake-up prompt to Claude
    logger.info(f"User {user_id} started bot, sending wake-up prompt")
    await claude_queue.put(ClaudeRequest(
        prompt=settings.wake_up_prompt,
        update=update,
        context=context,
        source="wake_up"
    ))


# Note: Removed detect_action_in_response - Claude executes actions directly now


@error_handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user messages with Twitter-style threading."""
    global _thread_states

    user_id = update.effective_user.id
    message_text = update.message.text

    # Security checks
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    if not security_validator.check_rate_limit(user_id):
        await update.message.reply_text("⏱️ Rate limit exceeded. Please wait.")
        return

    # Check if bot is paused
    if db_manager.is_paused():
        await update.message.reply_text("⏸️ Bot is paused. An admin needs to /resume it.")
        return

    logger.info(f"Received user message: {message_text[:50]}...")

    # Check if this is a thread marker
    is_start = _is_thread_start(message_text)
    is_end = _is_thread_end(message_text)

    # Check if we're already in a thread for this user
    if user_id in _thread_states:
        thread = _thread_states[user_id]

        # Add message to thread
        thread.messages.append(message_text)
        thread.update = update
        thread.context = context

        logger.debug(f"Added to thread (now {len(thread.messages)} messages) for user {user_id}")

        # Check if this message ends the thread
        if is_end:
            logger.info(f"Thread end marker detected for user {user_id}")
            await _submit_thread(user_id)
        # If thread is restarted (new 1/ while already in thread), submit current and start new
        elif is_start and len(thread.messages) > 1:
            logger.info(f"Thread restart detected for user {user_id}, submitting current thread")
            # Remove the new start message from current thread
            thread.messages.pop()
            await _submit_thread(user_id)
            # Start new thread with the start message
            current_time = asyncio.get_event_loop().time()
            timer_task = asyncio.create_task(_thread_timer(user_id))
            _thread_states[user_id] = ThreadState(
                messages=[message_text],
                update=update,
                context=context,
                timer_task=timer_task,
                start_time=current_time,
                reminder_sent=False
            )
            logger.info(f"Started new thread for user {user_id}")

    elif is_start:
        # Start a new thread
        current_time = asyncio.get_event_loop().time()
        timer_task = asyncio.create_task(_thread_timer(user_id))

        _thread_states[user_id] = ThreadState(
            messages=[message_text],
            update=update,
            context=context,
            timer_task=timer_task,
            start_time=current_time,
            reminder_sent=False
        )

        logger.info(f"Started new thread for user {user_id}")

    else:
        # Not in a thread and no thread marker - process immediately
        logger.info(f"Immediate processing (no thread) for user {user_id}")
        await claude_queue.put(ClaudeRequest(
            prompt=message_text,
            update=update,
            context=context,
            source="user_text"
        ))


@error_handler
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages - save to Jarvis/tmp and notify Claude."""
    logger.info("handle_photo called!")
    user_id = update.effective_user.id

    # Security check
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Check if bot is paused
    if db_manager.is_paused():
        await update.message.reply_text("⏸️ Bot is paused. An admin needs to /resume it.")
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


@error_handler
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle audio messages - save to Jarvis/tmp and notify Claude."""
    logger.info("handle_audio called!")
    user_id = update.effective_user.id

    # Security check
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Check if bot is paused
    if db_manager.is_paused():
        await update.message.reply_text("⏸️ Bot is paused. An admin needs to /resume it.")
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


@error_handler
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document/file uploads - save to Jarvis/tmp and notify Claude."""
    logger.info("handle_document called!")
    user_id = update.effective_user.id

    # Security check
    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Check if bot is paused
    if db_manager.is_paused():
        await update.message.reply_text("⏸️ Bot is paused. An admin needs to /resume it.")
        return

    # Get the document
    document = update.message.document

    # Download the document
    doc_file = await document.get_file()

    # Generate filename with original name if available
    import time
    timestamp = int(time.time())
    if document.file_name:
        filename = f"telegram_doc_{timestamp}_{document.file_name}"
    else:
        filename = f"telegram_doc_{timestamp}"

    filepath = f"{settings.approved_directory}/tmp/{filename}"

    # Ensure tmp directory exists
    import os
    os.makedirs(f"{settings.approved_directory}/tmp", exist_ok=True)

    # Download and save
    await doc_file.download_to_drive(filepath)

    logger.info(f"Saved document to {filepath}")

    # Get caption and mime type
    caption = update.message.caption or "no caption"
    mime_type = document.mime_type or "unknown type"

    # Create notification message for Claude
    notification = f"leaf sent you a file: {filepath} (Type: {mime_type}) Caption: {caption}"

    # Enqueue document notification for worker to process
    logger.info(f"Enqueuing document notification: {filename}")
    await claude_queue.put(ClaudeRequest(
        prompt=notification,
        update=update,
        context=context,
        source="document"
    ))
