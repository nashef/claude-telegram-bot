"""Telegram message handlers."""
import asyncio
import io
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from src.claude.cli_executor import ClaudeProcessManager, StreamUpdate
from src.security.validator import security_validator
from src.config.settings import settings

logger = logging.getLogger(__name__)

# Global executor (using CLI subprocess like richardatct)
claude_executor = ClaudeProcessManager(settings)

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

                logger.info(f"Heartbeat: sending internal monologue: '{settings.heartbeat_message[:50]}...'")

                try:
                    # Send a status message to user
                    status_msg = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="💭 Internal monologue..."
                    )

                    # Start typing indicator for heartbeat
                    heartbeat_typing_task = asyncio.create_task(
                        _send_typing_periodically(context, update)
                    )

                    # Initialize streaming state
                    last_update_time = 0

                    # Streaming callback for heartbeat
                    async def heartbeat_stream_callback(update_obj: StreamUpdate):
                        """Update progress message with streaming updates during heartbeat."""
                        nonlocal last_update_time
                        current_time = asyncio.get_event_loop().time()

                        # Log all stream updates (not throttled)
                        if update_obj.type == "tool_use":
                            logger.info(f"💭 Heartbeat - Tool: {update_obj.content}")
                            if update_obj.tool_calls:
                                for tool in update_obj.tool_calls:
                                    logger.info(f"   - {tool.get('name')}: {tool.get('input')}")
                        elif update_obj.type == "assistant":
                            logger.info(f"💭 Heartbeat - Claude: {update_obj.content[:100]}...")
                        elif update_obj.type == "tool_result":
                            logger.info(f"💭 Heartbeat - {update_obj.content}")
                        elif update_obj.type == "result":
                            logger.info(f"💭 Heartbeat - {update_obj.content}")

                        # Throttle UI updates to max 1 per second
                        if current_time - last_update_time < 1.0:
                            return

                        last_update_time = current_time

                        # Format the progress message
                        progress_text = ""
                        if update_obj.type == "tool_use":
                            progress_text = f"💭 **Internal monologue**\n\n🔧 **{update_obj.content}**"
                        elif update_obj.type == "assistant":
                            content_preview = (
                                update_obj.content[:150] + "..."
                                if len(update_obj.content) > 150
                                else update_obj.content
                            )
                            progress_text = f"💭 **Internal monologue**\n\n🤖 **Working...**\n\n_{content_preview}_"
                        elif update_obj.type == "result":
                            progress_text = "💭 **Internal monologue**\n\n✅ **Completed!**"

                        if progress_text:
                            try:
                                await status_msg.edit_text(progress_text, parse_mode="Markdown")
                            except Exception as e:
                                logger.debug(f"Failed to update heartbeat progress: {e}")

                    # Get current session ID
                    session_id = context.user_data.get('claude_session_id')

                    # Execute Claude with the heartbeat prompt with streaming
                    logger.info("Heartbeat: calling Claude executor with streaming")
                    response_obj = await claude_executor.execute_command(
                        prompt=settings.heartbeat_message,
                        working_directory=claude_executor.config.approved_directory,
                        session_id=session_id,
                        continue_session=bool(session_id),
                        stream_callback=heartbeat_stream_callback
                    )

                    # Stop typing indicator
                    heartbeat_typing_task.cancel()
                    try:
                        await heartbeat_typing_task
                    except asyncio.CancelledError:
                        pass

                    # Update session ID
                    if response_obj.session_id:
                        context.user_data['claude_session_id'] = response_obj.session_id

                    # Send Claude's response
                    response = response_obj.content
                    if response:
                        if len(response) > 4096:
                            chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]
                            await status_msg.delete()
                            for chunk in chunks:
                                await context.bot.send_message(
                                    chat_id=update.effective_chat.id,
                                    text=chunk
                                )
                        else:
                            await status_msg.edit_text(response)
                    else:
                        await status_msg.edit_text("💭 (no response)")

                    logger.info("Heartbeat: completed successfully")

                except Exception as e:
                    logger.error(f"Heartbeat execution error: {e}", exc_info=True)
                    # Stop typing on error
                    if 'heartbeat_typing_task' in locals():
                        heartbeat_typing_task.cancel()
                        try:
                            await heartbeat_typing_task
                        except asyncio.CancelledError:
                            pass
                    try:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"❌ Heartbeat error: {str(e)[:100]}"
                        )
                    except:
                        pass

                finally:
                    # Reset activity tracker since we just sent a message
                    last_activity_tracker['time'] = asyncio.get_event_loop().time()
                    logger.info(f"Heartbeat: activity tracker reset to {last_activity_tracker['time']}")
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

    # Budget check removed (CLI doesn't have built-in budget tracking)

    try:
        # Get or initialize persistent activity tracker (shared across messages)
        if 'activity_tracker' not in context.user_data:
            context.user_data['activity_tracker'] = {'time': asyncio.get_event_loop().time()}
            logger.debug(f"Activity tracker initialized at {context.user_data['activity_tracker']['time']}")
        else:
            # Update activity time for new incoming message
            context.user_data['activity_tracker']['time'] = asyncio.get_event_loop().time()
            logger.debug(f"Activity tracker updated for new message at {context.user_data['activity_tracker']['time']}")

        # Start typing indicator (shows "..." animation in Telegram)
        typing_task = asyncio.create_task(_send_typing_periodically(context, update))

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
        else:
            logger.debug(f"Heartbeat disabled in settings")
            heartbeat_task = None

        # Send "thinking" message
        thinking_msg = await update.message.reply_text("🤔 Processing...")

        # Initialize last_update_time for THIS message (must be inside try block)
        last_update_time = 0  # Start at 0 to allow immediate first update

        # Streaming callback to show real-time progress
        async def stream_callback(update_obj: StreamUpdate):
            """Update progress message with streaming updates."""
            nonlocal last_update_time
            current_time = asyncio.get_event_loop().time()

            # Log all stream updates (not throttled)
            if update_obj.type == "tool_use":
                logger.info(f"🔧 Tool: {update_obj.content}")
                if update_obj.tool_calls:
                    for tool in update_obj.tool_calls:
                        logger.info(f"   - {tool.get('name')}: {tool.get('input')}")
            elif update_obj.type == "assistant":
                logger.info(f"🤖 Claude: {update_obj.content[:100]}...")
            elif update_obj.type == "tool_result":
                logger.info(f"📊 {update_obj.content}")
            elif update_obj.type == "result":
                logger.info(f"✅ {update_obj.content}")

            # Handle file edit diff preview (not throttled)
            if update_obj.type == "file_edit":
                try:
                    # Generate diff image
                    diff_image_bytes = generate_diff_image(
                        update_obj.old_content,
                        update_obj.new_content,
                        update_obj.file_path
                    )

                    # Send diff image
                    await update.message.reply_photo(
                        photo=io.BytesIO(diff_image_bytes),
                        caption=f"📝 Proposed changes to `{update_obj.file_path}`",
                        parse_mode="Markdown"
                    )
                    logger.info(f"Sent diff image for {update_obj.file_path}")
                except Exception as e:
                    logger.error(f"Failed to send diff image: {e}")
                return

            # Throttle UI updates to max 1 per second to avoid Telegram rate limits
            if current_time - last_update_time < 1.0:
                return

            last_update_time = current_time

            # Format the progress message
            progress_text = ""
            if update_obj.type == "tool_use":
                # Show tools being used
                progress_text = f"🔧 **{update_obj.content}**"
            elif update_obj.type == "assistant":
                # Show Claude's thinking/response preview
                content_preview = (
                    update_obj.content[:150] + "..."
                    if len(update_obj.content) > 150
                    else update_obj.content
                )
                progress_text = f"🤖 **Working...**\n\n_{content_preview}_"
            elif update_obj.type == "result":
                # Execution completed
                progress_text = "✅ **Completed!**"

            if progress_text:
                try:
                    await thinking_msg.edit_text(progress_text, parse_mode="Markdown")
                except Exception as e:
                    # Ignore rate limit errors on streaming updates
                    logger.debug(f"Failed to update progress: {e}")

        # Get current session ID (if any)
        session_id = context.user_data.get('claude_session_id')

        # Execute Claude CLI with streaming (subprocess approach)
        response_obj = await claude_executor.execute_command(
            prompt=message_text,
            working_directory=claude_executor.config.approved_directory,
            session_id=session_id,
            continue_session=bool(session_id),
            stream_callback=stream_callback
        )

        # Store session ID for next message
        if response_obj.session_id:
            context.user_data['claude_session_id'] = response_obj.session_id

        # Get response text
        response = response_obj.content

        # Stop typing indicator (but keep heartbeat running!)
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

        logger.debug("Message complete - typing stopped, heartbeat continues monitoring")

        # Check for file sending markers: [SEND_FILE:path] or [SEND_AUDIO:path]
        import re
        from pathlib import Path as PathLib

        file_pattern = r'\[SEND_(FILE|AUDIO):([^\]]+)\]'
        file_matches = list(re.finditer(file_pattern, response))

        # Remove file markers from response text
        clean_response = re.sub(file_pattern, '', response).strip()

        # Send files first
        for match in file_matches:
            file_type, file_path = match.groups()
            file_path = file_path.strip()

            try:
                abs_path = PathLib(file_path)
                if not abs_path.is_absolute():
                    abs_path = PathLib(claude_executor.config.approved_directory) / file_path

                if abs_path.exists() and abs_path.is_file():
                    logger.info(f"Sending {file_type.lower()}: {abs_path}")

                    with open(abs_path, 'rb') as f:
                        if file_type == "AUDIO":
                            await update.message.reply_audio(
                                audio=f,
                                filename=abs_path.name,
                                caption=f"🎵 {abs_path.name}"
                            )
                        else:
                            await update.message.reply_document(
                                document=f,
                                filename=abs_path.name
                            )
                else:
                    await update.message.reply_text(f"⚠️ File not found: {file_path}")
            except Exception as e:
                logger.error(f"Failed to send file {file_path}: {e}")
                await update.message.reply_text(f"❌ Error sending file: {str(e)[:100]}")

        # Send response text (split if too long) - no confirmation needed
        if clean_response:
            if len(clean_response) > 4096:
                chunks = [clean_response[i:i+4096] for i in range(0, len(clean_response), 4096)]
                await thinking_msg.delete()
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await thinking_msg.edit_text(clean_response)

        # Update activity tracker - we just completed sending a response
        context.user_data['activity_tracker']['time'] = asyncio.get_event_loop().time()
        logger.debug(f"Activity tracker updated: message complete at {context.user_data['activity_tracker']['time']}")

    except TimeoutError:
        # Stop typing on timeout (but keep heartbeat running)
        if 'typing_task' in locals():
            typing_task.cancel()
        await update.message.reply_text(
            "⏱️ Request timed out. Please try a simpler request."
        )
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        # Stop typing on error (but keep heartbeat running)
        if 'typing_task' in locals():
            typing_task.cancel()
        await update.message.reply_text(
            f"❌ Error: {str(e)[:200]}"
        )


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

    # Send acknowledgment
    await update.message.reply_text(f"✅ Photo saved to tmp/{filename}\nCaption: {caption}\n\nSend me a text message to tell me what to do with it!")
