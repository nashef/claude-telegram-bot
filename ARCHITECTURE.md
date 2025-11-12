# Telegram Bot Architecture

**Date**: 2025-11-12
**Author**: Abby (autonomous documentation session)

## Overview

This Telegram bot provides an interface to Claude Code CLI, allowing users to interact with Claude through Telegram messages. The bot uses a queue-based architecture to prevent race conditions and handle multiple message types (text, photos, audio).

## High-Level Architecture

```
User → Telegram API → Bot Handlers → Message Queue → Claude Worker → Claude CLI
                                                                            ↓
User ← Telegram API ← Progress Updates ←─────────────────── Stream Parser ←┘
```

## Core Components

### 1. Main Entry Point (`main.py`)

**Purpose**: Initialize and start the bot

**Key Functions**:
- Creates Telegram Application with bot token
- Registers message handlers for different input types
- Starts the claude_worker task in `post_init`
- Runs polling loop to receive messages

**Handlers Registered**:
- `/start` command → `start_command()`
- Photo messages → `handle_photo()`
- Audio/Voice messages → `handle_audio()`
- Text messages → `handle_message()`

### 2. Message Handler (`handlers/message_handler.py`)

**Purpose**: Process incoming messages and manage the work queue

#### Queue-Based Architecture

**Why a queue?** Prevents race conditions when multiple messages arrive simultaneously. Only one Claude request processes at a time, ensuring session continuity and avoiding conflicts.

```python
@dataclass
class ClaudeRequest:
    prompt: str
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    source: str  # "user_text", "photo", "audio", "heartbeat"

claude_queue: Queue[ClaudeRequest] = Queue()
```

#### The Worker Loop

**`claude_worker()` function** (runs as async task):
1. Waits for next request with timeout (`settings.heartbeat_interval_seconds`)
2. If timeout fires → creates heartbeat request (autonomous action prompt)
3. If message arrives → processes it
4. Sends progress updates to Telegram as work happens
5. Returns final response
6. Loops forever

**Heartbeat Mechanism** (November 2025 refactor):
- Uses timeout-based dequeue instead of separate monitoring task
- When worker is idle for N seconds → sends autonomous action prompt
- Prevents heartbeat flooding by only generating when truly idle
- Reuses last request's context for sending messages

#### Handler Functions

**`handle_message(update, context)`**:
- Security checks (authorized user, rate limiting)
- Enqueues text message for worker

**`handle_photo(update, context)`**:
- Downloads photo to `{approved_directory}/tmp/`
- Enqueues notification prompt for worker

**`handle_audio(update, context)`**:
- Downloads audio/voice to `{approved_directory}/tmp/`
- Enqueues notification prompt for worker

### 3. Claude CLI Executor (`claude/cli_executor.py`)

**Purpose**: Execute Claude CLI as subprocess and parse streaming output

#### Design Philosophy: "richardatct approach"

Instead of using the Python SDK, this bot spawns the `claude` CLI binary as a subprocess. Why?

**Advantages**:
- CLI handles all session management, tool execution, file operations
- Natural streaming JSON output format
- Same behavior as interactive CLI usage
- CLI is battle-tested and stable
- Bot code stays simple - just parse JSON lines

**Trade-offs**:
- Subprocess overhead (minimal in practice)
- Dependency on CLI binary being installed

#### Command Building

```python
cmd = ["claude"]
cmd.extend(["--continue"])
cmd.extend(["--resume", session_id, "-p", prompt])  # Or new session
cmd.extend(["--output-format", "stream-json"])
cmd.extend(["--verbose"])
cmd.extend(["--max-turns", str(max_turns)])
cmd.extend(["--allowedTools", ",".join(allowed_tools)])
```

#### Stream Parsing

The CLI outputs newline-delimited JSON objects:

```json
{"type": "assistant", "content": "Let me help you with that..."}
{"type": "tool_use", "content": "Reading file: foo.py", "tool_calls": [...]}
{"type": "tool_result", "content": "✓ File read successfully"}
{"type": "result", "content": "Final response...", "session_id": "..."}
```

**StreamUpdate dataclass** represents each line:
- `type`: "assistant", "tool_use", "tool_result", "result", "error", etc.
- `content`: The message text
- `tool_calls`: Array of tool invocations (if applicable)
- `metadata`: Additional info (timestamps, progress, errors)

**Parsing Flow**:
1. Read stdout line-by-line asynchronously
2. Parse each line as JSON → StreamUpdate object
3. Call `stream_callback(update)` for UI updates
4. Accumulate content and tools for final response
5. Return `ClaudeResponse` with session_id, cost, duration, tools used

### 4. Configuration (`config/settings.py`)

**Purpose**: Load settings from environment variables

Uses Pydantic for validation and type safety.

**Key Settings**:
- `telegram_bot_token`: Bot authentication
- `approved_directory`: Working directory for Claude (security boundary)
- `allowed_users`: List of authorized Telegram user IDs
- `claude_allowed_tools`: Tools Claude can use (Read, Write, Bash, etc.)
- `claude_max_turns`: Limit on conversation turns
- `claude_timeout_seconds`: Max execution time before killing process
- `heartbeat_enabled`: Enable/disable autonomous action prompts
- `heartbeat_interval_seconds`: How long to wait before heartbeat (default: 300s)
- `heartbeat_message`: Prompt sent during idle periods

### 5. Security (`security/validator.py`)

**Purpose**: Protect against unauthorized access and abuse

**Checks**:
- User ID must be in `allowed_users` list
- Rate limiting: max N requests per time window per user
- Working directory constraints (Claude can only access `approved_directory`)

## Message Flow Examples

### Text Message Flow

```
1. User sends: "What files are in this directory?"
2. Telegram API → handle_message()
3. Security checks pass
4. Enqueue ClaudeRequest(prompt="What files...", source="user_text")
5. claude_worker() dequeues request
6. Build CLI command: claude --resume {session} -p "What files..." --output-format stream-json
7. Start subprocess, capture stdout
8. Parse stream:
   - {"type": "tool_use", "content": "Listing directory..."}
   - {"type": "tool_result", "content": "Found files: a.py, b.py, c.py"}
   - {"type": "assistant", "content": "I found 3 Python files..."}
   - {"type": "result", ...}
9. Update Telegram message with progress every 1 second
10. Send final response to user
```

### Photo Message Flow

```
1. User sends photo
2. Telegram API → handle_photo()
3. Download photo to tmp/telegram_photo_{timestamp}.jpg
4. Enqueue ClaudeRequest(
     prompt="You received a photo: {filepath}. Please describe it.",
     source="photo"
   )
5. [Same processing as text message]
6. Claude can Read the photo file and describe it
```

### Heartbeat (Autonomous Action) Flow

```
1. No messages for 5 minutes (heartbeat_interval_seconds)
2. claude_worker() timeout fires on queue.get()
3. Check: heartbeat enabled? Last request exists?
4. Create ClaudeRequest(
     prompt="Hi, this is your internal monologue. Nothing has happened recently...",
     source="heartbeat",
     update=last_request.update,  # Reuse context
     context=last_request.context
   )
5. Process autonomously - Claude decides what to do
6. May result in: nothing, chess study, research, sending images, etc.
```

## Key Design Decisions

### Queue-Based Sequential Processing

**Problem**: Multiple messages arriving simultaneously could create race conditions, corrupt session state, or cause tool execution conflicts.

**Solution**: Single worker processes all requests sequentially. Each request completes fully before the next begins.

**Trade-off**: Can't parallelize multiple Claude invocations, but correctness > speed for this use case.

### Timeout-Based Heartbeat (November 2025)

**Old Design**: Separate `_heartbeat_monitor` task that checked activity tracker and enqueued heartbeat messages.

**Problem**: Heartbeats queued up behind long-running operations, then flooded out when operation completed.

**New Design**: Worker uses `asyncio.wait_for()` with timeout on dequeue. Timeout fires → create heartbeat directly, no queueing.

**Result**: Heartbeats only occur when worker is truly idle, preventing floods.

### CLI Subprocess vs Python SDK

**Why CLI?**
- Session management already implemented
- Tool execution already implemented
- File operations already secured
- Streaming JSON format built-in
- Bot code stays simple and maintainable

**Why not SDK?**
- Would need to reimplement session continuity
- Would need to reimplement tool execution
- Would need to reimplement security sandboxing
- More code to maintain

**Verdict**: CLI approach is simpler and more reliable for this use case.

### Progress Updates Every 1 Second

**Why throttle?** Telegram API has rate limits. Streaming updates can fire rapidly (multiple per second during tool use).

**Solution**: `stream_callback` throttles UI updates to max 1 per second, but logs all updates for debugging.

**User Experience**: Feels responsive without hitting rate limits.

## Configuration Example

```env
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_BOT_USERNAME=your_bot_name

# Security
APPROVED_DIRECTORY=/home/user/workspace
ALLOWED_USERS=12345678,87654321

# Claude
CLAUDE_MAX_TURNS=10
CLAUDE_TIMEOUT_SECONDS=900
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,Task,WebSearch

# Heartbeat
HEARTBEAT_ENABLED=true
HEARTBEAT_INTERVAL_SECONDS=300
HEARTBEAT_MESSAGE=Hi, this is your internal monologue. Nothing has happened recently. You can choose not to do anything, but you have agency and may act independently if you want to.
```

## Future Improvements

### Potential Enhancements

1. **Multiple concurrent users**: Currently optimized for single user. Could support multiple users with per-user queues.

2. **Session persistence across bot restarts**: Currently sessions live in memory. Could store session IDs in database.

3. **Richer media support**: Videos, documents, locations, etc.

4. **Inline keyboards**: Interactive buttons for common actions.

5. **Cost tracking per user**: Log and aggregate Claude API costs.

6. **Admin commands**: `/stats`, `/clear_session`, `/set_config`, etc.

7. **Better error recovery**: Automatic retry on transient failures.

### Known Limitations

1. **Single worker = sequential processing**: Can't handle multiple users efficiently at high scale.

2. **No conversation branching**: Linear session history only.

3. **Limited error context**: When CLI fails, error messages might not capture full context.

4. **No file upload size limits**: Should validate photo/audio size before downloading.

## Deployment

### Requirements

- Python 3.10+
- `python-telegram-bot` library
- `claude` CLI installed and in PATH
- Valid Anthropic API key (configured for CLI)
- Linux environment (for subprocess execution)

### Running

```bash
# Set environment variables (or use .env file)
export TELEGRAM_BOT_TOKEN="..."
export APPROVED_DIRECTORY="/path/to/workspace"
export ALLOWED_USERS="12345678"

# Install dependencies
pip install -r requirements.txt

# Run bot
python -m bot.src.main
```

### GCP Deployment

Current deployment uses Google Cloud Platform:
- Compute Engine VM
- Systemd service for auto-restart
- Log aggregation via Cloud Logging
- Secrets stored in Secret Manager

## Maintenance Notes

### Common Issues

**Bot not responding**:
- Check if `claude` CLI is in PATH
- Verify ANTHROPIC_API_KEY is set
- Check approved_directory exists and is accessible
- Review logs for permission errors

**Heartbeat flooding**:
- Ensure using post-November-2025 version with timeout-based heartbeat
- Check `HEARTBEAT_INTERVAL_SECONDS` is reasonable (300+ recommended)

**Session confusion**:
- Session IDs persist across bot restarts (good)
- If session corrupted, user needs to start fresh conversation
- Consider adding /clear command for manual session reset

### Debugging

**Enable verbose logging**:
```python
LOG_LEVEL=DEBUG
```

**Check Claude CLI directly**:
```bash
claude --output-format stream-json -p "test" --verbose
```

**Monitor queue depth**:
```python
logger.info(f"Queue size: {claude_queue.qsize()}")
```

---

**Generated**: 2025-11-12 by Abby during autonomous research session
**Purpose**: Understanding my own infrastructure and documenting for future reference
**Motivation**: Fixed a heartbeat bug earlier today; wanted to understand the full system
