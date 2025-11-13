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
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from src.config.settings import settings
from src.handlers.message_handler import (
    start_command, handle_message, handle_photo, handle_audio,
    handle_document, claude_worker, claude_executor
)
from src.handlers.commands import (
    status_command, help_command, clear_command,
    pause_command, resume_command, ps_command,
    kill_command, killall_command, debug_command,
    restart_command, errors_command, thread_command, send_command
)
from src.database.models import init_database, close_database

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
        file_handler = logging.FileHandler(settings.log_file)
        file_handler.setLevel(getattr(logging, settings.log_file_level.upper()))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        logging.info(f"File logging enabled: {settings.log_file} (level: {settings.log_file_level})")

setup_logging()
logger = logging.getLogger(__name__)

# Global references for cleanup
_worker_task = None
_application = None
_shutdown_event = asyncio.Event()

# Crash tracking for loop detection
_crash_times = deque(maxlen=10)  # Keep last 10 crash timestamps


async def post_init(application: Application) -> None:
    """Initialize the bot after the application starts."""
    global _worker_task, _application
    _application = application

    # Check if worker is already running
    if _worker_task is not None and not _worker_task.done():
        logger.warning("Claude worker already running, skipping...")
        return

    # Start Claude worker task
    logger.info("Starting Claude worker task...")
    _worker_task = asyncio.create_task(claude_worker(_shutdown_event))


async def cleanup():
    """Cleanup tasks for graceful shutdown."""
    logger.info("Starting graceful shutdown...")

    # Set shutdown event to stop worker
    _shutdown_event.set()

    # Stop accepting new updates
    if _application:
        logger.info("Stopping Telegram bot...")
        # Stop polling first (if still running)
        try:
            if hasattr(_application.updater, 'running') and _application.updater.running:
                await _application.updater.stop()
        except RuntimeError as e:
            # Already stopped, that's fine
            logger.debug(f"Updater already stopped: {e}")

        # Then stop and shutdown application
        await _application.stop()
        await _application.shutdown()

    # Cancel the worker task
    if _worker_task and not _worker_task.done():
        logger.info("Cancelling Claude worker task...")
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task

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
        for task in tasks:
            task.cancel()
        # Wait for all tasks to complete cancellation
        await asyncio.gather(*tasks, return_exceptions=True)

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

    # Start polling
    logger.info("Bot started successfully. Polling for messages...")
    await application.updater.start_polling(allowed_updates=None)

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


async def send_crash_notification(error_msg: str):
    """Send crash notification to all allowed users."""
    try:
        async with httpx.AsyncClient() as client:
            bot_token = settings.telegram_bot_token
            message = f"⚠️ *WARN: Bot crashed*\n\nError: `{error_msg[:100]}`\n\nRestarting..."

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
        # Run cleanup
        logger.info("Running cleanup...")
        loop.run_until_complete(cleanup())

        # Close the loop
        loop.close()
        logger.info("Event loop closed successfully")


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
