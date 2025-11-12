#!/usr/bin/env python3
"""
Test script to verify clean shutdown behavior.
Run this and press Ctrl+C to test graceful shutdown.
"""

import subprocess
import time
import signal

def main():
    print("Starting bot for shutdown test...")
    print("Press Ctrl+C to test graceful shutdown\n")

    # Start the bot
    proc = subprocess.Popen(
        ["python", "src/main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1
    )

    try:
        # Read output line by line
        for line in proc.stdout:
            print(line, end='')

            # Look for shutdown messages
            if "Graceful shutdown complete" in line:
                print("\n✅ CLEAN SHUTDOWN DETECTED!")
            elif "Task was destroyed but it is pending" in line:
                print("\n❌ UNCLEAN SHUTDOWN - asyncio task leak detected")
            elif "Event loop is closed" in line:
                print("\n⚠️ Event loop closed error detected")
    except KeyboardInterrupt:
        print("\n\nSending SIGINT to bot...")
        proc.send_signal(signal.SIGINT)

        # Wait for graceful shutdown
        try:
            proc.wait(timeout=10)
            print(f"Bot exited with code: {proc.returncode}")
        except subprocess.TimeoutExpired:
            print("⚠️ Bot didn't exit gracefully in 10 seconds, killing...")
            proc.kill()
            proc.wait()
            print("Bot forcefully terminated")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

if __name__ == "__main__":
    main()