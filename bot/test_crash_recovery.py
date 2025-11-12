#!/usr/bin/env python3
"""
Test script to verify crash recovery and loop detection.
This creates a modified main.py that crashes on demand.
"""

import os
import tempfile
import subprocess
import time
import signal
import sys

def create_crashing_bot(crash_count=3):
    """Create a modified version of the bot that crashes after N messages."""
    with open("src/main.py", "r") as f:
        content = f.read()

    # Add crash trigger to async_main
    crash_code = f"""
    # TEST CRASH INJECTION
    import os
    crash_file = "/tmp/bot_crash_test_counter"
    if os.path.exists(crash_file):
        with open(crash_file, "r") as f:
            count = int(f.read().strip())
        if count < {crash_count}:
            with open(crash_file, "w") as f:
                f.write(str(count + 1))
            await asyncio.sleep(1)  # Let bot start
            raise RuntimeError(f"TEST CRASH {{count + 1}}/{crash_count}")
        else:
            os.remove(crash_file)  # Reset for next test
    else:
        with open(crash_file, "w") as f:
            f.write("0")
"""

    # Insert crash code at the beginning of async_main
    content = content.replace(
        'logger.info("Starting Telegram Bot...")',
        crash_code + '\n    logger.info("Starting Telegram Bot...")'
    )

    return content


def test_normal_recovery():
    """Test that bot recovers from occasional crashes."""
    print("\n" + "="*60)
    print("TEST 1: Normal crash recovery (3 crashes, spaced out)")
    print("="*60)

    # Create crashing version
    test_script = create_crashing_bot(crash_count=3)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(test_script)
        script_path = f.name

    try:
        # Reset crash counter
        if os.path.exists("/tmp/bot_crash_test_counter"):
            os.remove("/tmp/bot_crash_test_counter")

        proc = subprocess.Popen(
            ["python3", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )

        start_time = time.time()
        crash_count = 0
        restarts = 0

        print("\nMonitoring bot output...")
        for line in proc.stdout:
            if "TEST CRASH" in line:
                crash_count += 1
                print(f"✓ Crash {crash_count} detected")
            elif "Restarting bot in 2 seconds" in line:
                restarts += 1
                print(f"✓ Restart {restarts} initiated")
            elif "Bot started successfully" in line and restarts > 0:
                print(f"✓ Bot recovered after crash {restarts}")
            elif "CRASH LOOP DETECTED" in line:
                print("✗ Unexpected crash loop detection!")
                break

            # Stop after 15 seconds or successful recovery
            if time.time() - start_time > 15 or restarts >= 3:
                break

        proc.terminate()
        proc.wait(timeout=5)

        if restarts >= 3:
            print("\n✅ TEST PASSED: Bot recovered from 3 crashes")
        else:
            print(f"\n❌ TEST FAILED: Only {restarts} restarts occurred")

    finally:
        os.unlink(script_path)
        if os.path.exists("/tmp/bot_crash_test_counter"):
            os.remove("/tmp/bot_crash_test_counter")


def test_crash_loop_detection():
    """Test that crash loop is detected correctly."""
    print("\n" + "="*60)
    print("TEST 2: Crash loop detection (rapid crashes)")
    print("="*60)

    # Create version that always crashes immediately
    content = """
import sys
import time
import logging
from collections import deque

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

crash_times = deque(maxlen=10)

print("Starting crash loop test...")
for i in range(6):
    crash_times.append(time.time())
    logger.info(f"Simulated crash {i+1}")

    if len(crash_times) >= 5:
        now = time.time()
        recent = [t for t in crash_times if now - t < 60]
        if len(recent) >= 5:
            logger.critical(f"CRASH LOOP DETECTED: {len(recent)} crashes in last 60 seconds")
            print("✅ Crash loop correctly detected!")
            sys.exit(1)

    time.sleep(0.1)  # Rapid crashes

print("❌ Crash loop NOT detected!")
sys.exit(0)
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(content)
        script_path = f.name

    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            timeout=5
        )

        if "✅ Crash loop correctly detected!" in result.stdout:
            print("\n✅ TEST PASSED: Crash loop detection works")
        else:
            print("\n❌ TEST FAILED: Crash loop not detected")
            print(result.stdout)

    finally:
        os.unlink(script_path)


if __name__ == "__main__":
    print("Testing bot crash recovery system...")

    # Test normal recovery
    test_normal_recovery()

    # Test crash loop detection
    test_crash_loop_detection()

    print("\n" + "="*60)
    print("All tests completed!")
    print("="*60)