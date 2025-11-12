"""
Slash command handlers for bot management.
"""
import logging
import asyncio
from datetime import datetime
from typing import List
from telegram import Update
from telegram.ext import ContextTypes

from src.config.settings import settings
from src.security.validator import security_validator
from src.utils.error_handler import error_handler
from src.database.manager import db_manager
from src.handlers.message_handler import claude_executor, claude_queue

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Check if user is an admin."""
    # For now, all allowed users are admins
    # In future, could have separate ADMIN_USERS list
    return security_validator.is_authorized(user_id)


@error_handler
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - show bot health and user session."""
    user_id = update.effective_user.id

    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Get queue depth
    queue_depth = claude_queue.qsize()

    # Get active processes
    active_processes = len(claude_executor.active_processes) if claude_executor else 0

    # Get user session
    session_id = db_manager.get_user_session(user_id)

    # Get bot state
    is_paused = db_manager.is_paused()
    is_debug = db_manager.is_debug_mode()

    # Format status message
    status_msg = f"""📊 **Bot Status**

**System:**
• Queue depth: {queue_depth} messages
• Active processes: {active_processes}
• Bot state: {'⏸️ PAUSED' if is_paused else '✅ Running'}
• Debug mode: {'ON' if is_debug else 'OFF'}

**Your session:**
• Session ID: `{session_id[:20] if session_id else 'None'}...`
• User ID: {user_id}
"""

    await update.message.reply_text(status_msg, parse_mode="Markdown")


@error_handler
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - show available commands."""
    user_id = update.effective_user.id

    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    help_msg = """📖 **Available Commands**

**Basic Commands:**
/start - Welcome message
/status - Show bot status
/help - Show this help message
/clear - Clear your session

**Admin Commands:**
/pause - Pause message processing
/resume - Resume message processing
/ps - List active processes
/kill <process_id> - Kill a specific process
/killall - Kill all active processes
/debug <on/off> - Toggle debug mode
/restart - Restart the bot (sessions preserved)
/errors - Show recent errors

**Tips:**
• Send photos/audio/documents - they'll be saved to tmp/
• Messages are processed sequentially
• Sessions persist across restarts
"""

    if is_admin(user_id):
        await update.message.reply_text(help_msg, parse_mode="Markdown")
    else:
        # Non-admin users get limited help
        basic_help = help_msg.split("**Admin Commands:**")[0] + "\n*Admin commands hidden*"
        await update.message.reply_text(basic_help, parse_mode="Markdown")


@error_handler
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear command - clear user's session."""
    user_id = update.effective_user.id

    if not security_validator.is_authorized(user_id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    # Clear session from database
    cleared = db_manager.clear_user_session(user_id)

    # Clear from context
    if 'claude_session_id' in context.user_data:
        del context.user_data['claude_session_id']

    if cleared:
        await update.message.reply_text("🗑️ Your session has been cleared.")
    else:
        await update.message.reply_text("ℹ️ No session to clear.")


@error_handler
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pause command - pause message processing (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    db_manager.set_bot_state("paused", "true")
    await update.message.reply_text("⏸️ Bot paused. Use /resume to continue.")

    # Notify all admins
    for admin_id in settings.allowed_users:
        if admin_id != user_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"⏸️ Bot paused by user {user_id}"
                )
            except:
                pass


@error_handler
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resume command - resume message processing (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    db_manager.set_bot_state("paused", "false")
    await update.message.reply_text("▶️ Bot resumed.")

    # Notify all admins
    for admin_id in settings.allowed_users:
        if admin_id != user_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"▶️ Bot resumed by user {user_id}"
                )
            except:
                pass


@error_handler
async def ps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ps command - list active processes (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    # Get active processes from memory
    if not claude_executor or not claude_executor.active_processes:
        await update.message.reply_text("📋 No active processes.")
        return

    # Format process list
    process_list = "📋 **Active Processes:**\n\n"
    for proc_id, proc in claude_executor.active_processes.items():
        # Get process info from database if available
        db_processes = db_manager.get_active_processes()
        db_proc = next((p for p in db_processes if p.process_id == proc_id), None)

        if db_proc:
            elapsed = (datetime.utcnow() - db_proc.started_at).total_seconds()
            process_list += f"• `{proc_id[:8]}...`\n"
            process_list += f"  User: {db_proc.user_id}\n"
            process_list += f"  Time: {elapsed:.0f}s\n"
            process_list += f"  Cmd: {db_proc.command[:50] if db_proc.command else 'N/A'}...\n\n"
        else:
            process_list += f"• `{proc_id[:20]}...` (no details)\n"

    await update.message.reply_text(process_list, parse_mode="Markdown")


@error_handler
async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /kill command - kill specific process (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    # Parse process ID from command
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /kill <process_id>")
        return

    process_id = parts[1]

    # Find and kill process
    if not claude_executor or process_id not in claude_executor.active_processes:
        # Check if it's a partial ID
        matching = [pid for pid in claude_executor.active_processes.keys() if pid.startswith(process_id)]
        if len(matching) == 1:
            process_id = matching[0]
        elif len(matching) > 1:
            await update.message.reply_text(f"⚠️ Multiple processes match '{process_id}'")
            return
        else:
            await update.message.reply_text(f"❌ Process '{process_id}' not found.")
            return

    # Kill the process
    process = claude_executor.active_processes[process_id]
    try:
        process.terminate()
        await asyncio.sleep(2)  # Grace period
        if process.returncode is None:
            process.kill()

        # Remove from tracking
        del claude_executor.active_processes[process_id]

        # Update database
        db_manager.update_process_status(process_id, "killed")

        await update.message.reply_text(f"💀 Process '{process_id[:20]}...' killed.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to kill process: {e}")


@error_handler
async def killall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /killall command - kill all processes (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    if not claude_executor or not claude_executor.active_processes:
        await update.message.reply_text("📋 No active processes to kill.")
        return

    # Kill all processes
    killed_count = 0
    for proc_id, process in list(claude_executor.active_processes.items()):
        try:
            process.terminate()
            await asyncio.sleep(0.5)  # Brief grace period
            if process.returncode is None:
                process.kill()

            # Remove from tracking
            del claude_executor.active_processes[proc_id]

            # Update database
            db_manager.update_process_status(proc_id, "killed")

            killed_count += 1
        except Exception as e:
            logger.error(f"Failed to kill process {proc_id}: {e}")

    await update.message.reply_text(f"💀 Killed {killed_count} processes.")


@error_handler
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /debug command - toggle debug mode (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    # Parse on/off
    parts = update.message.text.split()
    if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
        current = db_manager.is_debug_mode()
        await update.message.reply_text(f"Debug mode is {'ON' if current else 'OFF'}. Use: /debug <on/off>")
        return

    enable = parts[1].lower() == "on"
    db_manager.set_bot_state("debug_mode", "true" if enable else "false")

    # Update logger level if needed
    if enable:
        logging.getLogger().setLevel(logging.DEBUG)
        await update.message.reply_text("🐛 Debug mode enabled.")
    else:
        logging.getLogger().setLevel(logging.INFO)
        await update.message.reply_text("🐛 Debug mode disabled.")


@error_handler
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /restart command - graceful restart (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    await update.message.reply_text("🔄 Restarting bot... Sessions will be preserved.")

    # Notify all admins
    for admin_id in settings.allowed_users:
        if admin_id != user_id:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔄 Bot restart initiated by user {user_id}"
                )
            except:
                pass

    # Exit gracefully - the resilient_main will restart it
    import sys
    sys.exit(0)


@error_handler
async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /errors command - show recent errors (admin only)."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin access required.")
        return

    # Get recent errors
    errors = db_manager.get_recent_errors(5)

    if not errors:
        await update.message.reply_text("✅ No recent errors.")
        return

    # Format error list
    error_msg = "❌ **Recent Errors:**\n\n"
    for error in errors:
        error_msg += f"• **{error.error_type}**\n"
        error_msg += f"  User: {error.user_id or 'N/A'}\n"
        error_msg += f"  Time: {error.timestamp.strftime('%H:%M:%S')}\n"
        error_msg += f"  Msg: {error.error_message[:50]}...\n\n"

    await update.message.reply_text(error_msg, parse_mode="Markdown")