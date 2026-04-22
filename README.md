# Yunam

Personal AI assistant running 24/7 on a Vultr Tokyo VPS, controlled via Telegram. Claude Opus 4.7 with access to your Obsidian vault, a separate filevault for binary attachments (photos / docs / voice notes indexed for semantic search), and an optional nightly retrospective prompt.

For architecture decisions, the governance layer, secrets handling, and the phase roadmap, see [CLAUDE.md](CLAUDE.md).

## What it does (today)

From Telegram:
- `/start` → Yunam greets you.
- Free-form chat → Claude Opus 4.7 replies, reading/writing your Obsidian vault as needed.
- Send a photo / document / voice note → cached as "pending"; say "save this" (or `/save`) to commit it to the filevault with a Voyage multimodal embedding.
- "Find that whiteboard photo from standup" → semantic search over saved files.
- "Send me that receipt I saved last week" → Yunam retrieves and Telegram-attaches it.
- Optional: nightly `22:30 KST` retrospective prompt; your reply lands in `daily/YYYY-MM-DD.md` in the vault.
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
    ├── requirements.txt           # PTB + anthropic + langgraph + aiosqlite + voyageai + sqlite-vec + Pillow
    ├── main.py                    # Telegram gateway, SkillRegistry wiring
    └── yunam/
        ├── orchestrator.py        # LangGraph + Claude tool loop
        ├── sessions.py            # SQLite store + schema migrations
        ├── capabilities.py        # Scope enum (vault:*, filevault:*)
        ├── skills/                # Governance layer — add new skills here
        ├── tools/                 # Low-level primitives (path safety, vault/filevault I/O)
        ├── embeddings.py          # Voyage multimodal client
        ├── scheduler.py           # Daily retrospective cron
        └── prompts.py             # Core system prompt (skill fragments live in each skill module)
```

For the full file-by-file tour and the governance layer, see [CLAUDE.md](CLAUDE.md).

## Prerequisites

- Docker & Docker Compose
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)
- Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- Voyage API key from [dash.voyageai.com](https://dash.voyageai.com) — for multimodal embeddings on saved files
- A directory to serve as your Obsidian vault (create `~/obsidian` if it doesn't exist)
- A directory for binary attachments, separate from the vault (defaults to `~/filevault`; created at first `/save`)

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
# Edit .env and fill in:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, ANTHROPIC_API_KEY, VOYAGE_API_KEY
# Optional: YUNAM_SCHEDULE_ENABLED=1 to turn on the nightly retrospective.

# macOS only: set PUID/PGID to your host user so vault writes land with the right owner.
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env

mkdir -p ~/obsidian ~/filevault data/yunam

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
#   - ANTHROPIC_API_KEY, VOYAGE_API_KEY
#   - YUNAM_SCHEDULE_ENABLED=1 to enable the nightly retrospective
#   - Leave PUID=1000, PGID=1000 (VPS user is uid 1000)

mkdir -p ~/obsidian ~/filevault data/yunam
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

# Per-call audit with governance columns (skill_id + scope populated from v2 onward):
sqlite3 data/yunam/yunam.db \
  'SELECT name, skill_id, scope, is_error, elapsed_ms FROM tool_calls ORDER BY id DESC LIMIT 20;'

# Which skill does the most work?
sqlite3 data/yunam/yunam.db \
  'SELECT skill_id, scope, COUNT(*) c FROM tool_calls GROUP BY skill_id, scope ORDER BY c DESC;'

# Schema version (should be 2 after the governance migration):
sqlite3 data/yunam/yunam.db 'PRAGMA user_version;'

# Saved files (filevault index):
sqlite3 data/yunam/yunam.db 'SELECT relpath, kind, mime_type, file_size FROM saved_files ORDER BY id DESC LIMIT 10;'

# See what Yunam has saved to the vault:
ls -la ~/obsidian/ ~/filevault/
```

## Roadmap

- **Phase 1** — Agent core with Claude + Obsidian. **Done.**
- **Phase 1.5** — Binary attachments + Voyage multimodal embeddings + nightly retrospective scheduler + skill/scope governance layer. **Done.**
- **Phase 2+** — Specialist agents and MCP integrations, all wired through the governance layer. Finance Agent wraps the [MoneyFlow](../MoneyFlow) batch pipeline as a sibling Docker service (likely as an MCP server). See [CLAUDE.md § Governance](CLAUDE.md) for the checklists on adding a skill, MCP server, or sub-agent.

## Security notes

- `.env` is gitignored. Never commit real tokens or API keys.
- Bot replies only to the user ID in `TELEGRAM_ALLOWED_USER_ID`. If that's wrong or empty, the bot ignores you — check the logs.
- Vault paths are sandboxed; `..` escapes and absolute paths are rejected before any filesystem call.
- Size caps: 1 MB per read, 500 KB per write.
- If a token leaks, revoke via `/revoke` in `@BotFather` and update `.env`.
