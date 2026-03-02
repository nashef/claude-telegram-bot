"""
Main entry point for the Telegram bot.
"""
import asyncio
import logging
import signal
import sys
import time
from collections import deque
from contextlib import suppress
import httpx
import uvicorn
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from src.config.settings import settings
from src.handlers.message_handler import (
    start_command, handle_message, handle_photo, handle_audio,
    handle_document, claude_worker, claude_executor, set_application
)
from src.handlers.commands import (
    status_command, help_command, clear_command,
    pause_command, resume_command, ps_command,
    kill_command, killall_command, debug_command,
    restart_command, errors_command, thread_command, send_command,
    alarm_command, model_command
)
from src.handlers.alarm_handler import alarm_worker
from src.database.models import init_database, close_database
from src.api.alarms import app as alarm_api

# Filter to suppress noisy polling logs
class TelegramPollingFilter(logging.Filter):
    """Filter out successful getUpdates and sendChatAction requests from httpx logs."""
    def filter(self, record):
        msg = record.getMessage()
        if '200 OK' in msg:
            if 'getUpdates' in msg or 'sendChatAction' in msg:
                return False
        return True


# Configure logging
def setup_logging():
    """Configure logging with console and optional file output."""
    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture everything, handlers will filter

    # Remove any existing handlers
    root_logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Console handler with configured level
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.log_level.upper()))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler if configured
    if settings.log_file:
        import os
        log_dir = os.path.dirname(settings.log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(settings.log_file)
        file_handler.setLevel(getattr(logging, settings.log_file_level.upper()))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        logging.info(f"File logging enabled: {settings.log_file} (level: {settings.log_file_level})")

    # Add filter to httpx logger to suppress noisy polling logs
    logging.getLogger("httpx").addFilter(TelegramPollingFilter())

setup_logging()
logger = logging.getLogger(__name__)

# Global references for cleanup
_worker_task = None
_alarm_worker_task = None
_api_executor_task = None
_application = None
_shutdown_event = asyncio.Event()

# Crash tracking for loop detection
_crash_times = deque(maxlen=10)  # Keep last 10 crash timestamps


async def post_init(application: Application) -> None:
    """Initialize the bot after the application starts."""
    global _worker_task, _alarm_worker_task, _api_server, _application
    _application = application

    # Share application reference with message handler for alarm messaging
    set_application(application)

    # Check if worker is already running
    if _worker_task is not None and not _worker_task.done():
        logger.warning("Claude worker already running, skipping...")
    else:
        # Start Claude worker task
        logger.info("Starting Claude worker task...")
        _worker_task = asyncio.create_task(claude_worker(_shutdown_event))
        if hasattr(_worker_task, 'set_name'):
            _worker_task.set_name("claude_worker")

    # Check if alarm worker is already running
    if _alarm_worker_task is not None and not _alarm_worker_task.done():
        logger.warning("Alarm worker already running, skipping...")
    else:
        # Start alarm worker task with shutdown event
        logger.info("Starting alarm worker task...")
        _alarm_worker_task = asyncio.create_task(alarm_worker(_shutdown_event))
        if hasattr(_alarm_worker_task, 'set_name'):
            _alarm_worker_task.set_name("alarm_worker")

    # Start API server if not already running
    global _api_executor_task
    if _api_executor_task is None or _api_executor_task.done():
        logger.info(f"Starting Alarm API server on 0.0.0.0:{settings.alarm_api_port}...")
        # Note: Uvicorn in thread mode doesn't shut down cleanly
        # We'll just let it die when the process exits
        # TODO: Consider using hypercorn or a pure async server for cleaner shutdown
        def run_api_server():
            try:
                uvicorn.run(
                    alarm_api,
                    host="0.0.0.0",
                    port=settings.alarm_api_port,
                    log_level="info" if settings.debug else "warning"
                )
            except Exception as e:
                logger.error(f"Error running API server: {e}")

        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        _api_executor_task = loop.run_in_executor(None, run_api_server)


async def cleanup():
    """Cleanup tasks for graceful shutdown."""
    logger.info("Starting graceful shutdown...")

    # Set shutdown event to stop worker
    _shutdown_event.set()

    # Stop accepting new updates
    if _application:
        logger.info("Stopping Telegram bot...")
        # Stop polling first (if still running) with timeout
        try:
            if hasattr(_application.updater, 'running') and _application.updater.running:
                await asyncio.wait_for(_application.updater.stop(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Updater stop timed out")
        except RuntimeError as e:
            # Already stopped, that's fine
            logger.debug(f"Updater already stopped: {e}")

        # Then stop and shutdown application with timeouts
        try:
            await asyncio.wait_for(_application.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Application stop timed out")
        except RuntimeError:
            logger.debug("Application already stopped")

        try:
            await asyncio.wait_for(_application.shutdown(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Application shutdown timed out")
        except RuntimeError:
            logger.debug("Application already shutdown")

    # Cancel the Claude worker task
    if _worker_task and not _worker_task.done():
        logger.info("Cancelling Claude worker task...")
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task

    # Cancel the alarm worker task
    if _alarm_worker_task and not _alarm_worker_task.done():
        logger.info("Cancelling alarm worker task...")
        _alarm_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _alarm_worker_task

    # API server runs in thread - we can't cleanly stop uvicorn
    # It will terminate when the process exits
    if _api_executor_task and not _api_executor_task.done():
        logger.info("API server running in thread will terminate with process")

    # Kill any active Claude processes
    if claude_executor and claude_executor.active_processes:
        logger.info(f"Killing {len(claude_executor.active_processes)} active Claude processes...")
        for process_id, process in list(claude_executor.active_processes.items()):
            try:
                logger.info(f"Terminating Claude process {process_id}")
                process.terminate()
                # Give it 5 seconds to terminate gracefully
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                # Force kill if still running
                if process.returncode is None:
                    logger.warning(f"Force killing Claude process {process_id}")
                    process.kill()
                    await process.wait()
            except Exception as e:
                logger.error(f"Error killing process {process_id}: {e}")

    # Cancel all remaining tasks
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        logger.info(f"Cancelling {len(tasks)} remaining tasks...")
        # Log task details for debugging
        for task in tasks:
            task_name = task.get_name() if hasattr(task, 'get_name') else str(task)
            logger.debug(f"  - Task: {task_name}")
            task.cancel()

        # Wait for all tasks to complete cancellation WITH TIMEOUT
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=5.0
            )
            logger.info("All tasks cancelled successfully")
        except asyncio.TimeoutError:
            logger.warning(f"Task cancellation timed out after 5 seconds, {len([t for t in tasks if not t.done()])} tasks still running")
            # Force exit if tasks won't cancel
            for task in tasks:
                if not task.done():
                    task_name = task.get_name() if hasattr(task, 'get_name') else str(task)
                    logger.error(f"Task still running: {task_name}")

    # Close database
    logger.info("Closing database...")
    close_database()

    logger.info("Graceful shutdown complete")


async def async_main():
    """Async main function for better control over lifecycle."""
    global _application

    # Initialize database
    logger.info("Initializing database...")
    init_database(settings.database_url)

    logger.info("Starting Telegram Bot...")
    logger.info(f"Approved directory: {settings.approved_directory}")
    logger.info(f"Allowed users: {settings.allowed_users}")
    logger.info(f"Claude model: {settings.claude_model}")
    logger.info(f"Allowed tools: {settings.claude_allowed_tools}")

    # Create application
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    _application = application

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("ps", ps_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("killall", killall_command))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("errors", errors_command))
    application.add_handler(CommandHandler("thread", thread_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("alarm", alarm_command))
    application.add_handler(CommandHandler("model", model_command))

    # Add message handlers
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Initialize application
    await application.initialize()
    await application.start()

    # Call post_init manually since we're not using run_polling
    await post_init(application)

    # Start polling with drop_pending_updates to ignore old messages
    logger.info("Bot started successfully. Dropping old updates and starting polling...")
    await application.updater.start_polling(allowed_updates=None, drop_pending_updates=True)

    # Wait for shutdown signal
    await _shutdown_event.wait()

    # Stop polling before exiting
    logger.info("Stopping updater...")
    await application.updater.stop()

    # Cleanup is handled by signal handler
    logger.info("Main loop exiting...")


def signal_handler(sig):
    """Handle shutdown signals."""
    logger.info(f"Received signal {signal.Signals(sig).name}")
    _shutdown_event.set()
    # Don't call sys.exit() - let the main loop handle shutdown gracefully


async def send_crash_notification(error_msg: str):
    """Send crash notification to all allowed users."""
    try:
        async with httpx.AsyncClient() as client:
            bot_token = settings.telegram_bot_token
            message = f"⚠️ *WARN: Bot crashed*\n\nError: `{error_msg}`\n\nRestarting..."

            for user_id in settings.allowed_users:
                try:
                    await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": user_id,
                            "text": message,
                            "parse_mode": "Markdown"
                        },
                        timeout=5.0
                    )
                    logger.info(f"Sent crash notification to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send crash notification to {user_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to send crash notifications: {e}")


def detect_crash_loop() -> bool:
    """Check if we're in a crash loop (5 crashes in 60 seconds)."""
    if len(_crash_times) < 5:
        return False

    # Check if the last 5 crashes were within 60 seconds
    now = time.time()
    recent_crashes = [t for t in _crash_times if now - t < 60]

    if len(recent_crashes) >= 5:
        logger.critical(f"CRASH LOOP DETECTED: {len(recent_crashes)} crashes in last 60 seconds")
        return True

    return False


def main():
    """Entry point with proper signal handling."""
    # Set up event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Install signal handlers
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, f: signal_handler(s))

    try:
        # Run the async main
        loop.run_until_complete(async_main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        # Run cleanup with timeout
        logger.info("Running cleanup...")
        try:
            # Give cleanup 10 seconds max
            loop.run_until_complete(asyncio.wait_for(cleanup(), timeout=10.0))
        except asyncio.TimeoutError:
            logger.warning("Cleanup timed out after 10 seconds, forcing exit")
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

        # Close the loop
        try:
            loop.close()
            logger.info("Event loop closed successfully")
        except Exception as e:
            logger.error(f"Failed to close event loop: {e}")


def resilient_main():
    """Main with retry logic and crash loop detection."""
    global _shutdown_event

    while True:
        try:
            # Reset shutdown event for restart
            _shutdown_event = asyncio.Event()

            logger.info("Starting bot...")
            main()

            # If main() exits normally, we're done
            logger.info("Bot exited normally")
            break

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt - shutting down")
            break

        except Exception as e:
            # Record crash time
            _crash_times.append(time.time())
            logger.error(f"Bot crashed: {e}", exc_info=True)

            # Check for crash loop
            if detect_crash_loop():
                logger.critical("Crash loop detected - exiting to allow Docker restart")
                sys.exit(1)

            # Send notification to users (create new event loop for this)
            try:
                notify_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(notify_loop)
                notify_loop.run_until_complete(send_crash_notification(str(e)))
                notify_loop.close()
            except Exception as notify_error:
                logger.error(f"Failed to send crash notification: {notify_error}")

            # Brief pause before restart
            logger.info("Restarting bot in 2 seconds...")
            time.sleep(2)


if __name__ == "__main__":
    try:
        resilient_main()
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal error in resilient_main: {e}", exc_info=True)
        sys.exit(1)
