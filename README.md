# Claude Code Telegram Bot

Control Claude Code CLI directly from Telegram. Send messages to your bot and Claude executes tasks with full tool access: reading and writing files, running shell commands, searching the web, and more.

**Architecture**: CLI subprocess with streaming JSON output, based on [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) and further developed by [stebou/claude-code-telegram-gcp](https://github.com/stebou/claude-code-telegram-gcp).

---

## Features

- Full Claude Code tool access: Read, Write, Edit, Bash, Glob, Grep, WebSearch, Task, and more
- Real-time streaming with typing indicator while Claude works
- Session persistence across messages (conversation history maintained)
- Multi-user support with allowlist
- Heartbeat / internal monologue for autonomous operation during downtime
- Alarm system with API
- File upload support (photos, audio, documents)
- API key or OAuth authentication

---

## Quickstart

This bot is designed to run inside Docker as part of the [Clanker](../..) system. See the top-level README for the full setup flow.

### 1. Prerequisites

- Docker
- A Telegram bot token (from [@BotFather](https://t.me/botfather))
- Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
- An Anthropic API key or Claude account

### 2. Configure

Copy the example config and fill in your values:

```bash
cp config/.env.example /your/workspace/.env
nano /your/workspace/.env
```

Required settings:
```
TELEGRAM_BOT_TOKEN=      # From @BotFather
TELEGRAM_BOT_USERNAME=   # Bot username without @
ALLOWED_USERS=[12345678] # Your Telegram user ID
APPROVED_DIRECTORY=/workspace
DATABASE_URL=sqlite:////workspace/telegram_bot.db
```

### 3. Authenticate Claude

**Option A — API key (recommended):**

Add to your `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

**Option B — OAuth login:**

After starting the container, run:
```bash
docker exec -it <container-name> claude login
```

### 4. Run

```bash
docker run -d \
  --name my-agent \
  -v /path/to/workspace:/workspace \
  -e HOME=/workspace \
  --env-file /path/to/workspace/.env \
  --restart unless-stopped \
  clanker
```

Check logs:
```bash
docker logs my-agent
```

---

## Configuration Reference

See [`config/.env.example`](config/.env.example) for all options with descriptions. Key settings:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Bot token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | required | Bot username without @ |
| `ALLOWED_USERS` | required | JSON array of allowed Telegram user IDs |
| `APPROVED_DIRECTORY` | required | Directory Claude can access (use `/workspace`) |
| `ANTHROPIC_API_KEY` | optional | API key for auth (alternative to `claude login`) |
| `CLAUDE_MODEL` | `sonnet` | Model: `sonnet`, `opus`, or `haiku` |
| `CLAUDE_MAX_TURNS` | `10` | Max back-and-forth iterations per request |
| `CLAUDE_TIMEOUT_SECONDS` | `900` | Timeout for Claude commands |
| `HEARTBEAT_ENABLED` | `true` | Enable internal monologue during silence |
| `HEARTBEAT_INTERVAL_SECONDS` | `300` | Seconds of silence before heartbeat |
| `LOG_FILE` | empty | Log file path (e.g. `logs/bot.log`) |
| `DATABASE_URL` | `sqlite:///telegram_bot.db` | Use absolute path: `sqlite:////workspace/...` |
| `USER_TIMEZONE` | `UTC` | Timezone for alarm parsing (e.g. `America/Denver`) |

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Wake up the bot |
| `/status` | Show current status |
| `/help` | List available commands |
| `/clear` | Clear conversation history |
| `/pause` | Pause message processing |
| `/resume` | Resume message processing |
| `/ps` | Show active Claude processes |
| `/kill` | Kill current Claude process |
| `/killall` | Kill all active processes |
| `/model` | Switch Claude model |
| `/errors` | Show recent errors |
| `/debug` | Toggle debug output |

---

## Troubleshooting

**Bot starts but Claude commands fail:**
- Check that `ANTHROPIC_API_KEY` is set in `.env`, or run `docker exec -it <name> claude login`
- Verify `APPROVED_DIRECTORY=/workspace` matches the container mount point

**Pydantic validation error on startup:**
- `ALLOWED_USERS` and `CLAUDE_ALLOWED_TOOLS` must be JSON arrays: `[123456]`, `["Read","Write"]`

**Messages not being processed:**
- Check `/ps` to see if a previous process is stuck
- Use `/killall` then retry

**Database errors:**
- Use 4 slashes for absolute SQLite paths: `sqlite:////workspace/telegram_bot.db`

---

## Credits

Based on [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram), with significant additions from [stebou/claude-code-telegram-gcp](https://github.com/stebou/claude-code-telegram-gcp) including streaming tool output, typing indicator animation, session persistence, and Pydantic configuration.

Further developed as part of the [Clanker](https://github.com/nashef/clanker) project.

---

MIT License
