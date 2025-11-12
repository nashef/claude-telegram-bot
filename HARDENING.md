# Production Hardening Plan for Telegram Bot

## Overview
This document outlines the production hardening improvements needed to make the Telegram-Claude bot more resilient, manageable, and user-friendly in production environments.

## 1. Crash-Resilient Main Loop ✅ COMPLETED

### Problem
- Bot dies on any unhandled exception
- No automatic recovery mechanism
- Loss of service until manual restart

### Implemented Solution
- ✅ Clean asyncio shutdown with proper task cancellation
- ✅ Retry wrapper around main() for automatic recovery
- ✅ Crash loop detection (5 crashes in 60 seconds → exit for Docker restart)
- ✅ Sends "⚠️ WARN: Bot crashed" message to all users on crash
- ✅ 2-second pause between restarts
- ✅ Graceful cleanup of Claude processes on shutdown

**Key implementation details:**
- `resilient_main()` wraps the main bot loop
- Deque tracks last 10 crash timestamps
- On crash: logs error, notifies users, checks for loop, restarts
- On crash loop: exits with code 1 to trigger Docker restart
- Clean shutdown on SIGTERM/SIGINT with full resource cleanup

### Implementation Notes
- **Crash loop threshold**: 5 crashes in 60 seconds triggers exit
- **User alerting**: All allowed users get crash notifications via Telegram API
- **State preservation**: Sessions are lost on crash (will be addressed in Section 3)

## 2. Graceful Handler Error Recovery ✅ COMPLETED

### Problem
- Exceptions in handlers can bubble up to application
- Users get no feedback when errors occur
- Errors in one handler can affect others

### Implemented Solution
- ✅ Created `@error_handler` decorator in `src/utils/error_handler.py`
- ✅ Applied to all message handlers (start, message, photo, audio, document)
- ✅ Error categorization with 8 categories
- ✅ User-friendly messages based on error type
- ✅ Full logging with context (user_id, handler name, message)
- ✅ Worker also uses categorized error messages
- ✅ CancelledError propagates for clean shutdown

**Error Categories & Messages:**
- **Network**: "⚠️ Network connection issue. Please try again in a moment."
- **Timeout**: "⏱️ Request timed out. Please try again with a simpler request."
- **Rate Limit**: "⏸️ Rate limit reached. Please wait X seconds."
- **Permission**: "🔒 Access denied. Insufficient permissions."
- **Not Found**: "📁 File not found. Please check the file path."
- **Invalid Input**: "❌ Invalid input or data format. Please check your message."
- **Claude Error**: "🤖 Claude encountered an error. Please try again."
- **Generic**: "❌ An error occurred. Please try again."

### Implementation Notes
- **Error detection**: Decorator catches all exceptions except CancelledError
- **User experience**: Users always get feedback instead of silence
- **Logging**: Full stack traces preserved for debugging
- **Resilience**: One user's error doesn't affect others (queue continues)
- **Future enhancements**: Could add retry buttons or error IDs for support

## 3. Slash Commands with Persistence

### Problem
- No persistent configuration
- No admin commands for management
- Settings lost on restart

### Proposed Solution

#### Database Schema
```sql
-- Configuration table
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Session storage
CREATE TABLE sessions (
    user_id INTEGER PRIMARY KEY,
    session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    metadata TEXT  -- JSON field for additional data
);

-- Process tracking (optional - could stay in memory)
CREATE TABLE processes (
    process_id TEXT PRIMARY KEY,
    user_id INTEGER,
    command TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT  -- running, completed, killed
);
```

#### Proposed Commands

**User Commands:**
- `/status` - Show bot health and your current session
- `/interrupt` - Stop your current Claude process
- `/clear` - Clear your session history
- `/help` - Show available commands

**Admin Commands:**
- `/pause` - Pause all message processing
- `/resume` - Resume message processing
- `/restart` - Graceful restart with session preservation
- `/kill [process_id]` - Kill specific process
- `/killall` - Kill all active processes
- `/ps` - List all active processes
- `/debug [on/off]` - Toggle debug logging
- `/config [key] [value]` - Set configuration value
- `/users` - List active users and sessions

### Design Questions
- **Authorization model**: How to identify admins?
  - Reuse ALLOWED_USERS for all commands?
  - Separate ADMIN_USERS list?
  - Role-based with levels?

- **Command syntax**: How to handle parameters?
  - `/config debug true` or `/config debug=true`?
  - `/kill 12345` or `/kill process_id=12345`?

- **Persistence strategy**: When to write to DB?
  - Every config change immediately?
  - Batch writes every N seconds?
  - Write-through cache?

## 4. Process Interruption from Telegram

### Problem
- Can't stop runaway Claude processes
- No visibility into what's running
- Orphaned processes possible

### Proposed Solution
```python
# Track active_processes: user_id → process_id mapping
# Add interrupt capability via:
#   1. /interrupt command
#   2. Inline keyboard "🛑 Stop" button on processing messages
#   3. Automatic timeout with user notification

# Process cleanup sequence:
#   1. Send SIGTERM
#   2. Wait 5 seconds
#   3. Send SIGKILL if still running
#   4. Clean up session state
```

### Design Questions
- **Interrupt UX**: How to present interruption option?
  - Inline keyboard on every "Processing..." message?
  - Only show after N seconds of processing?
  - React to any message during processing as interrupt signal?

- **Partial results**: What to do with partial Claude responses?
  - Show what was completed before interrupt?
  - Discard everything?
  - Mark as "Interrupted: [partial response]"?

- **Session continuity**: After interrupt, what happens to session?
  - Keep session, just kill process?
  - Reset session completely?
  - User choice?

## 5. Message Batching for Rapid Messages

### Problem
- Each message processed separately
- Users often send multiple messages quickly
- Wastes API calls and context

### Proposed Solution
```python
# Message collection algorithm:
# 1. Receive first message
# 2. Start 1-second timer
# 3. Collect additional messages while:
#    - User is typing (check indicator)
#    - OR new messages arrive within 4s
#    - Maximum collection time: 30s
# 4. Concatenate all messages with newlines
# 5. Send as single prompt to Claude
```

### Design Questions
- **Collection parameters**: What timing works best?
  - Initial delay: 0.5s, 1s, or 2s?
  - Idle timeout: 2s, 4s, or until typing stops?
  - Maximum collection time: 30s or 60s?

- **Message combination**: How to merge messages?
  - Simple newline concatenation?
  - Add timestamps?
  - Preserve as separate messages with markers?

- **User feedback**: How to show batching is happening?
  - "📝 Collecting messages..." status?
  - Show message count?
  - Allow manual "send now" trigger?

- **Edge cases**: How to handle:
  - Commands mixed with regular messages?
  - Media/files between messages?
  - Different message types?

## Implementation Priority

### Phase 1: Core Resilience (Critical)
1. Main loop crash recovery
2. Handler error decoration
3. Basic process interruption (/interrupt command)

### Phase 2: Management (High)
4. Database setup and models
5. Basic slash commands (/status, /ps, /kill)
6. Session persistence

### Phase 3: Enhanced UX (Medium)
7. Message batching
8. Inline stop buttons
9. Advanced admin commands

### Phase 4: Monitoring (Nice to have)
10. Metrics collection
11. Health endpoints
12. Alert system

## Technical Considerations

### Dependencies to Add
- `sqlalchemy` - ORM for database
- `alembic` - Database migrations
- `psutil` - Process monitoring (optional)

### Performance Impact
- Database writes: Minimal (few KB per session)
- Message batching: Reduces API calls, may increase latency
- Process tracking: Negligible overhead

### Security Considerations
- Admin commands need authentication
- SQL injection prevention (use parameterized queries)
- Process IDs shouldn't be guessable
- Rate limiting on expensive operations

## Testing Strategy
- Unit tests for error handlers
- Integration tests for database operations
- Chaos testing: Random exceptions, process kills
- Load testing: Multiple concurrent users
- Recovery testing: Crash and restart scenarios

## Rollback Plan
- Each feature behind feature flag
- Database migrations reversible
- Old message handler preserved as fallback
- Gradual rollout to subset of users first