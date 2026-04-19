# Yunam

Personal AI assistant running 24/7 on a Vultr Tokyo VPS, controlled via Telegram. Claude Opus 4.7 with access to your Obsidian vault.

For architecture decisions, secrets handling, and phase roadmap, see [CLAUDE.md](CLAUDE.md).

## Current phase: 1 — Agent core

Success criteria (from Telegram):
- `/start` → Yunam greets you.
- "Write a note called test.md with the word hello" → `~/obsidian/test.md` is created.
- "What's in test.md?" → Yunam replies with "hello".
- A friend DMs the bot → silently ignored (logged at WARNING).

## Structure

```
yunam/
├── .env.example                   # Template — copy to .env and fill in
├── docker-compose.yml
├── scripts/repl.py                # Local dev REPL (fake or real Claude)
├── data/yunam/                    # SQLite DB lives here (bind-mounted, gitignored)
└── gateway/
    ├── Dockerfile                 # python:3.12-slim + gosu for PUID/PGID
    ├── entrypoint.sh              # uid remap → chown data → drop to appuser
    ├── requirements.txt           # PTB + anthropic + langgraph + aiosqlite
    ├── main.py                    # Telegram gateway
    └── yunam/                     # Orchestrator + tools + sessions
```

## Prerequisites

- Docker & Docker Compose
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)
- Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- A directory to serve as your Obsidian vault (create `~/obsidian` if it doesn't exist)

## Local testing (no token burn)

The fake Claude REPL exercises the SQLite + vault wiring without hitting the API:

```bash
cd gateway && pip install -r requirements.txt && cd ..
PYTHONPATH=gateway python scripts/repl.py
# Try: "hello"  (plain text)
#      "write hello"  (triggers vault_write)
#      "read test"    (triggers vault_read)
#      "escape"       (tries to read outside vault — should is_error)
```

Check that files appeared: `ls dev-vault/` and `sqlite3 dev-yunam.db 'SELECT * FROM tool_calls;'`.

Once wired up, run one turn against real Anthropic to verify caching:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
PYTHONPATH=gateway python scripts/repl.py --real
# Send two turns. On the second, the log should show cache_read>0.
```

## Run it locally via Docker

```bash
cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, ANTHROPIC_API_KEY.

# macOS only: set PUID/PGID to your host user so vault writes work.
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env

mkdir -p ~/obsidian data/yunam

docker compose up --build       # foreground; Ctrl+C to stop
```

From your phone, DM [`@AgentYunamBot`](https://t.me/AgentYunamBot):

- `/start` → greeting
- any message → Yunam responds, with vault reads/writes as needed

## Deploy to the VPS

Stop the local container first — two bots on the same token race on long-poll and each message lands on only one of them.

```bash
# On VPS (first time):
ssh yunam
git clone git@github.com:agdal1125/yunam.git ~/yunam
cd ~/yunam
cp .env.example .env
# Edit .env:
#   - TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID (same as local)
#   - ANTHROPIC_API_KEY
#   - Leave PUID=1000, PGID=1000 (VPS user is uid 1000)

mkdir -p ~/obsidian data/yunam
docker compose up -d --build

docker compose logs -f gateway  # expect: "gateway starting"  then  "gateway running"
```

No inbound ports are exposed — long polling only.

### Updates

```bash
ssh yunam
cd ~/yunam
git pull
docker compose up -d --build
```

## Inspecting state

```bash
# From VPS host:
sqlite3 data/yunam/yunam.db 'SELECT chat_id, role, substr(content,1,80) FROM messages ORDER BY id DESC LIMIT 10;'
sqlite3 data/yunam/yunam.db 'SELECT name, is_error, elapsed_ms FROM tool_calls ORDER BY id DESC LIMIT 20;'

# See what Yunam has saved to the vault:
ls -la ~/obsidian/
```

## Roadmap

- **Phase 1** (this) — Agent core with Claude + Obsidian
- **Phase 2+** — Specialist agents. Finance Agent wraps the [MoneyFlow](../MoneyFlow) batch pipeline as a sibling Docker service.

## Security notes

- `.env` is gitignored. Never commit real tokens or API keys.
- Bot replies only to the user ID in `TELEGRAM_ALLOWED_USER_ID`. If that's wrong or empty, the bot ignores you — check the logs.
- Vault paths are sandboxed; `..` escapes and absolute paths are rejected before any filesystem call.
- Size caps: 1 MB per read, 500 KB per write.
- If a token leaks, revoke via `/revoke` in `@BotFather` and update `.env`.
