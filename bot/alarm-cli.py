#!/usr/bin/env python3
"""
CLI tool for managing alarms via HTTP API.
Wraps the Alarm API endpoints for easy command-line usage.
Uses only Python standard library (no external dependencies).
"""
import argparse
import json
import sys
import re
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import urllib.request
import urllib.error
import urllib.parse


# Default API base URL
DEFAULT_API_URL = "http://localhost:8000"


# HTTP helper functions using stdlib
def http_request(method: str, url: str, data: Optional[dict] = None) -> tuple[int, dict]:
    """
    Make an HTTP request using urllib.
    Returns (status_code, response_dict).
    """
    headers = {"Content-Type": "application/json"}

    if data:
        body = json.dumps(data).encode("utf-8")
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            body_text = response.read().decode("utf-8")
            try:
                return status, json.loads(body_text)
            except json.JSONDecodeError:
                return status, {"raw": body_text}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8")
        try:
            error_data = json.loads(body_text)
        except json.JSONDecodeError:
            error_data = {"raw": body_text}
        return e.code, error_data
    except urllib.error.URLError as e:
        raise Exception(f"Connection failed: {e.reason}")


# Try to read default user ID from .env file
def get_default_user_id() -> Optional[int]:
    """Load the first allowed user from .env for convenience."""
    env_path = Path(".env")
    if not env_path.exists():
        return None

    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("ALLOWED_USERS="):
                    # Parse JSON array like [123456789, 987654321]
                    users_str = line.split("=", 1)[1].strip()
                    users = json.loads(users_str)
                    if users and isinstance(users, list):
                        return users[0]
    except Exception:
        pass

    return None

DEFAULT_USER_ID = get_default_user_id()


def parse_time_delta(delta_str: str) -> datetime:
    """
    Parse a time delta string and return the future datetime.
    Supports formats like: 1m, 5m, 1h, 2h, 1d, 3d, etc.
    """
    match = re.match(r'^(\d+)([mhd])$', delta_str.strip())
    if not match:
        raise ValueError(
            f"Invalid time delta format: '{delta_str}'. "
            "Use format like '5m', '1h', '2d' (minutes, hours, days)"
        )

    amount = int(match.group(1))
    unit = match.group(2)

    now = datetime.utcnow()
    if unit == 'm':
        return now + timedelta(minutes=amount)
    elif unit == 'h':
        return now + timedelta(hours=amount)
    elif unit == 'd':
        return now + timedelta(days=amount)
    else:
        raise ValueError(f"Unknown time unit: {unit}")


def print_alarm(alarm: dict):
    """Pretty print an alarm."""
    print(f"  ID: {alarm['id']}")
    print(f"  User ID: {alarm['user_id']}")
    print(f"  Prompt: {alarm['prompt'][:80]}..." if len(alarm['prompt']) > 80 else f"  Prompt: {alarm['prompt']}")
    if alarm.get('one_shot_time'):
        print(f"  One-shot time: {alarm['one_shot_time']}")
    if alarm.get('cron_schedule'):
        print(f"  Cron schedule: {alarm['cron_schedule']}")
    print(f"  Status: {alarm['status']}")
    print(f"  Created: {alarm['created_at']}")
    print(f"  Updated: {alarm['updated_at']}")


def create_alarm(
    user_id: int,
    prompt: list,
    one_shot_time: Optional[str] = None,
    one_shot_in: Optional[str] = None,
    cron_schedule: Optional[str] = None,
    file: Optional[str] = None,
    api_url: str = DEFAULT_API_URL
) -> None:
    """Create a new alarm."""
    try:
        # Determine prompt source: file or arguments
        if file:
            try:
                with open(file, 'r') as f:
                    final_prompt = f.read()
                print(f"📖 Reading prompt from file: {file}")
            except FileNotFoundError:
                print(f"❌ Error: File not found: {file}")
                sys.exit(1)
            except Exception as e:
                print(f"❌ Error reading file: {e}")
                sys.exit(1)
        elif prompt:
            # Concatenate prompt arguments with newlines
            final_prompt = "\n".join(prompt)
        else:
            print("❌ Error: Either provide prompt arguments or use --file")
            sys.exit(1)

        # Handle one_shot_in (convert delta to absolute time)
        if one_shot_in:
            if one_shot_time:
                print("❌ Error: Cannot specify both --one-shot-time and --one-shot-in")
                sys.exit(1)
            try:
                future_time = parse_time_delta(one_shot_in)
                one_shot_time = future_time.isoformat()
                print(f"⏰ Alarm will fire in {one_shot_in} at {future_time.isoformat()} UTC")
            except ValueError as e:
                print(f"❌ Error parsing time delta: {e}")
                sys.exit(1)

        payload = {
            "user_id": user_id,
            "prompt": final_prompt
        }

        if one_shot_time:
            payload["one_shot_time"] = one_shot_time
        if cron_schedule:
            payload["cron_schedule"] = cron_schedule

        status, response = http_request("POST", f"{api_url}/alarms", payload)

        if status not in (200, 201):
            error_msg = response.get("detail", response.get("raw", str(response)))
            print(f"❌ Error: {error_msg}")
            sys.exit(1)

        alarm = response
        print("✅ Alarm created successfully:")
        print_alarm(alarm)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


def get_alarm(alarm_id: str, api_url: str = DEFAULT_API_URL) -> None:
    """Get an alarm by ID."""
    try:
        status, response = http_request("GET", f"{api_url}/alarms/{alarm_id}")

        if status == 404:
            print(f"❌ Alarm not found: {alarm_id}")
            sys.exit(1)
        elif status != 200:
            error_msg = response.get("detail", response.get("raw", str(response)))
            print(f"❌ Error: {error_msg}")
            sys.exit(1)

        alarm = response
        print("📋 Alarm details:")
        print_alarm(alarm)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


def list_alarms(user_id: Optional[int] = None, status: Optional[str] = None, api_url: str = DEFAULT_API_URL) -> None:
    """List alarms."""
    try:
        params = {}
        if user_id is not None:
            params["user_id"] = user_id
        if status:
            params["status"] = status

        query_string = urllib.parse.urlencode(params) if params else ""
        url = f"{api_url}/alarms" + (f"?{query_string}" if query_string else "")

        status_code, response = http_request("GET", url)

        if status_code != 200:
            error_msg = response.get("detail", response.get("raw", str(response)))
            print(f"❌ Error: {error_msg}")
            sys.exit(1)

        alarms = response
        if not alarms:
            print("📋 No alarms found")
            return

        print(f"📋 Found {len(alarms)} alarm(s):")
        for i, alarm in enumerate(alarms, 1):
            print(f"\n{i}. Alarm {alarm['id'][:8]}...")
            print_alarm(alarm)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


def update_alarm(
    alarm_id: str,
    prompt: Optional[str] = None,
    status: Optional[str] = None,
    api_url: str = DEFAULT_API_URL
) -> None:
    """Update an alarm."""
    try:
        payload = {}
        if prompt is not None:
            payload["prompt"] = prompt
        if status is not None:
            payload["status"] = status

        status_code, response = http_request("PUT", f"{api_url}/alarms/{alarm_id}", payload)

        if status_code == 404:
            print(f"❌ Alarm not found: {alarm_id}")
            sys.exit(1)
        elif status_code != 200:
            error_msg = response.get("detail", response.get("raw", str(response)))
            print(f"❌ Error: {error_msg}")
            sys.exit(1)

        alarm = response
        print("✅ Alarm updated successfully:")
        print_alarm(alarm)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


def delete_alarm(alarm_id: str, api_url: str = DEFAULT_API_URL) -> None:
    """Delete an alarm."""
    try:
        status_code, response = http_request("DELETE", f"{api_url}/alarms/{alarm_id}")

        if status_code == 404:
            print(f"❌ Alarm not found: {alarm_id}")
            sys.exit(1)
        elif status_code != 204:
            error_msg = response.get("detail", response.get("raw", str(response)))
            print(f"❌ Error: {error_msg}")
            sys.exit(1)

        print(f"✅ Alarm deleted successfully: {alarm_id}")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


def health_check(api_url: str = DEFAULT_API_URL) -> None:
    """Check API health."""
    try:
        status_code, response = http_request("GET", f"{api_url}/health")

        if status_code != 200:
            print(f"❌ API is not healthy (status {status_code})")
            sys.exit(1)

        print(f"✅ API is healthy: {response}")
    except Exception as e:
        print(f"❌ API is not responding: {e}")
        sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="CLI tool for managing alarms",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a one-shot alarm (fires in 5 minutes)
  alarm-cli create "Hello!" --one-shot-in 5m

  # Create a one-shot alarm with absolute time
  alarm-cli create "Hello!" --one-shot-time "2024-12-31T23:59:00"

  # Create a recurring alarm (daily at 9am)
  alarm-cli create "Daily reminder" --cron "0 9 * * *"

  # List all alarms
  alarm-cli list

  # Get a specific alarm
  alarm-cli get abc123def456

  # Update alarm status to disabled
  alarm-cli update abc123def456 --status disabled

  # Delete an alarm
  alarm-cli delete abc123def456

  # Check API health
  alarm-cli health
        """
    )

    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"API base URL (default: {DEFAULT_API_URL})"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new alarm")
    create_parser.add_argument("prompt", nargs="*", help="Prompt to send to Claude (concatenated with newlines)")
    create_parser.add_argument(
        "--user-id",
        type=int,
        default=DEFAULT_USER_ID,
        required=(DEFAULT_USER_ID is None),
        help=f"Telegram user ID (default: {DEFAULT_USER_ID})" if DEFAULT_USER_ID else "Telegram user ID (required)"
    )
    create_parser.add_argument(
        "--file", "-f",
        help="Read prompt from file instead of arguments"
    )
    create_parser.add_argument(
        "--one-shot-time",
        help="ISO datetime for one-shot alarm (e.g., 2024-12-31T23:59:00)"
    )
    create_parser.add_argument(
        "--one-shot-in",
        help="Time delta for one-shot alarm (e.g., '5m', '1h', '2d')"
    )
    create_parser.add_argument(
        "--cron",
        help="Cron schedule for recurring alarm (e.g., '0 9 * * *' for daily at 9am)"
    )

    # Get command
    get_parser = subparsers.add_parser("get", help="Get an alarm by ID")
    get_parser.add_argument("alarm_id", help="Alarm ID")

    # List command
    list_parser = subparsers.add_parser("list", help="List alarms")
    if DEFAULT_USER_ID:
        list_parser.add_argument(
            "--user-id",
            type=int,
            default=DEFAULT_USER_ID,
            help=f"Filter by user ID (default: {DEFAULT_USER_ID})"
        )
    else:
        list_parser.add_argument("--user-id", type=int, help="Filter by user ID")
    list_parser.add_argument("--status", help="Filter by status (active, fired, disabled)")

    # Update command
    update_parser = subparsers.add_parser("update", help="Update an alarm")
    update_parser.add_argument("alarm_id", help="Alarm ID")
    update_parser.add_argument("--prompt", help="New prompt")
    update_parser.add_argument("--status", help="New status (active, fired, disabled)")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete an alarm")
    delete_parser.add_argument("alarm_id", help="Alarm ID")

    # Health check command
    health_parser = subparsers.add_parser("health", help="Check API health")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "create":
        create_alarm(
            args.user_id,
            args.prompt,
            one_shot_time=args.one_shot_time,
            one_shot_in=args.one_shot_in,
            cron_schedule=args.cron,
            file=args.file,
            api_url=args.api_url
        )
    elif args.command == "get":
        get_alarm(args.alarm_id, api_url=args.api_url)
    elif args.command == "list":
        list_alarms(user_id=args.user_id, status=args.status, api_url=args.api_url)
    elif args.command == "update":
        update_alarm(
            args.alarm_id,
            prompt=args.prompt,
            status=args.status,
            api_url=args.api_url
        )
    elif args.command == "delete":
        delete_alarm(args.alarm_id, api_url=args.api_url)
    elif args.command == "health":
        health_check(api_url=args.api_url)


if __name__ == "__main__":
    main()
