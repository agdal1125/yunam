# Yunam

Personal AI assistant running 24/7 on a Vultr Tokyo VPS, controlled via Telegram. Dual-model architecture: Claude Sonnet 4.6 for everyday conversation, Opus 4.7 via `/think` for hard problems. Access to your Obsidian vault, a separate filevault for binary attachments (photos / docs / voice notes indexed for semantic search), web browsing, Google Calendar, air quality, parcel tracking, reminders, and long-term memory.

Multi-principal: jaekeun (owner) and yoolim share the same bot with per-person privacy boundaries and memory isolation.

For architecture decisions, the governance layer, secrets handling, and the phase roadmap, see [CLAUDE.md](CLAUDE.md).

## What it does (today)

From Telegram (DM or authorized group chats):
- `/start` → Yunam greets you.
- Free-form chat → Claude Sonnet 4.6 replies, reading/writing your Obsidian vault as needed.
- `/think <query>` → Routes to Opus 4.7 with adaptive thinking for deeper reasoning.
- `/diary [content]` → Manual daily reflection. Without content, sends a reflection prompt; with content, processes and saves to `daily/YYYY-MM-DD.md`.
- Send a photo / document / voice note → cached as "pending"; say "save this" (or `/save`) to commit to the filevault with a Voyage multimodal embedding.
- "Find that whiteboard photo from standup" → semantic search over saved files.
- "Send me that receipt I saved last week" → Yunam retrieves and Telegram-attaches it.
- "오늘 서울 미세먼지 어때?" → air quality via Open-Meteo.
- "택배 어디쯤이야?" → parcel tracking via Sweet Tracker.
- "내일 3시에 알려줘" → reminders via the nudge sweeper.
- "기억해: ..." → long-term memory with semantic recall.
- Web search and page reading via Jina / DuckDuckGo.
- Google Calendar integration (events, scheduling) via MCP sidecar.
- `/chatid` → Echo the chat_id (for adding group chats to the allowlist).
- Group-chat support with `@bot` mention, reply, or trigger-word gating (`유남아`, `yunam`, etc.).
- Unknown users → silently ignored (logged at WARNING).

## Structure

```
yunam/
├── .env.example                     # Template — copy to .env and fill in
├── docker-compose.yml               # gateway + optional calendar-mcp sidecar
├── docker-compose.consent.yml       # One-time Google Calendar OAuth consent flow
├── gcp-oauth.keys.json              # Google OAuth credentials (gitignored)
├── scripts/
│   ├── repl.py                      # Local dev REPL (fake or real Claude)
│   ├── smoke_dual_model.py          # Smoke test: Sonnet + Opus paths
│   ├── smoke_gcal.py                # Smoke test: Google Calendar MCP
│   ├── smoke_korean.py              # Smoke test: Korean skills bundle
│   ├── smoke_multiuser.py           # Smoke test: multi-principal flows
│   └── smoke_web.py                 # Smoke test: web skill
├── docs/
│   └── gcal-setup.md                # Google Calendar MCP OAuth setup guide
├── data/yunam/                      # SQLite DB lives here (bind-mounted, gitignored)
├── mcp-servers/
│   └── google-calendar-mcp/         # nspady/google-calendar-mcp (git submodule / clone)
└── gateway/
    ├── Dockerfile                   # python:3.12-slim + gosu for PUID/PGID
    ├── entrypoint.sh                # uid remap → chown data → drop to appuser
    ├── requirements.txt             # PTB + anthropic + langgraph + aiosqlite + voyageai + ...
    ├── main.py                      # Composition root — builds deps, registers handlers, lifecycle
    ├── handlers/                    # Telegram handler definitions
    │   ├── __init__.py              # register_handlers() — single entry point for main.py
    │   ├── _helpers.py              # Shared constants (TELEGRAM_MSG_LIMIT, send_reply, ...)
    │   ├── commands.py              # /start, /save, /think, /diary, /chatid
    │   ├── text.py                  # Free-text handler + group-chat engagement logic
    │   └── attachments.py           # Receive, batch (media-group), and process file uploads
    └── yunam/                       # Core package
        ├── config.py                # Env loading, Principal/Config dataclasses, logging setup
        ├── auth.py                  # Principal resolution, chat allowlists, group triggers
        ├── prompts.py               # Core SYSTEM_PROMPT + DAILY_PROMPT_TEMPLATE
        ├── orchestrator.py          # LangGraph + Claude tool loop (SkillRegistry consumer)
        ├── sessions.py              # aiosqlite store + schema migrations (7 tables)
        ├── capabilities.py          # Scope enum (vault:*, filevault:*, web:*, ...)
        ├── embeddings.py            # Voyage multimodal client
        ├── context_primer.py        # Per-turn preference injection from Obsidian vault
        ├── sender.py                # AttachmentSender Protocol + PTBSender
        ├── vision.py                # Image content block helpers for inline vision
        ├── files.py                 # Filevault path safety + name sanitization
        ├── scheduler.py             # Nudge sweeper coroutine (reminder delivery loop)
        ├── skills/                  # Governance layer — where new capabilities are added
        │   ├── base.py              # Skill, ToolSpec, DispatchContext, SkillRegistry
        │   ├── obsidian.py          # Obsidian vault skill (read/write/list/search)
        │   ├── obsidian_graph.py    # Obsidian graph analysis (backlinks, orphans, structure)
        │   ├── files.py             # Filevault skill (save/search/retrieve attachments)
        │   ├── web.py               # Web browsing skill (search + page reading)
        │   ├── airquality.py        # Air quality skill (Open-Meteo)
        │   ├── parcel.py            # Parcel tracking skill (Sweet Tracker)
        │   ├── reminders.py         # Reminders / nudges skill
        │   ├── memory.py            # Long-term memory skill (semantic store)
        │   └── privacy.py           # Privacy controls (mark_turn_private)
        ├── tools/                   # Low-level primitives (no model/schema awareness)
        │   ├── vault.py             # safe_join, size caps, atomic write
        │   ├── obsidian.py          # ObsidianTools class
        │   ├── obsidian_graph.py    # ObsidianGraphTools class
        │   ├── attachments.py       # AttachmentTools class
        │   ├── web.py               # WebTools class (Jina Reader + DuckDuckGo)
        │   ├── airquality.py        # AirQualityTools class (Open-Meteo)
        │   ├── parcel.py            # ParcelTools class (Sweet Tracker)
        │   ├── reminders.py         # ReminderTools class
        │   └── memory.py            # MemoryTools class
        ├── mcp/                     # MCP server adapters (external tools via MCP protocol)
        │   └── gcal.py              # Google Calendar adapter (nspady/google-calendar-mcp)
        └── subagents/               # Separately-configured Claude calls
            └── deep_think.py        # Opus 4.7 + adaptive thinking (invoked via /think)
```

For the full file-by-file tour and the governance layer, see [CLAUDE.md](CLAUDE.md).

## Prerequisites

- Docker & Docker Compose
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)
- Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- Voyage API key from [dash.voyageai.com](https://dash.voyageai.com) — for multimodal embeddings on saved files
- A directory to serve as your Obsidian vault (create `~/obsidian` if it doesn't exist)
- A directory for binary attachments, separate from the vault (defaults to `~/filevault`)

Optional:
- Jina API key from [jina.ai](https://jina.ai/reader) — enables Jina Search (web_search falls back to DuckDuckGo without it)
- Sweet Tracker API key from [sweettracker.co.kr](http://info.sweettracker.co.kr/apikey/add) — parcel tracking
- Google Calendar MCP setup — see [docs/gcal-setup.md](docs/gcal-setup.md)

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
#   TELEGRAM_BOT_TOKEN, YUNAM_PRINCIPALS (or TELEGRAM_ALLOWED_USER_ID),
#   ANTHROPIC_API_KEY, VOYAGE_API_KEY

# macOS only: set PUID/PGID to your host user so vault writes land with the right owner.
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env

mkdir -p ~/obsidian ~/filevault data/yunam

docker compose up --build       # foreground; Ctrl+C to stop

# With Google Calendar MCP (optional):
docker compose --profile gcal up --build
```

From your phone, DM [`@AgentYunamBot`](https://t.me/AgentYunamBot):

- `/start` → greeting
- any message → Yunam responds, with vault reads/writes as needed
- `/think <question>` → deep reasoning via Opus 4.7
- `/diary` → daily reflection prompt

## Deploy to the VPS

Stop the local container first — two bots on the same token race on long-poll and each message lands on only one of them.

```bash
# On VPS (first time):
ssh yunam
git clone git@github.com:agdal1125/yunam.git ~/yunam
cd ~/yunam
cp .env.example .env
# Edit .env:
#   - TELEGRAM_BOT_TOKEN, YUNAM_PRINCIPALS, ANTHROPIC_API_KEY, VOYAGE_API_KEY
#   - YUNAM_NUDGE_SWEEPER_ENABLED=true to enable reminder delivery
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

# Per-call audit with governance columns:
sqlite3 data/yunam/yunam.db \
  'SELECT name, skill_id, scope, is_error, elapsed_ms FROM tool_calls ORDER BY id DESC LIMIT 20;'

# Which skill does the most work?
sqlite3 data/yunam/yunam.db \
  'SELECT skill_id, scope, COUNT(*) c FROM tool_calls GROUP BY skill_id, scope ORDER BY c DESC;'

# Schema version:
sqlite3 data/yunam/yunam.db 'PRAGMA user_version;'

# Saved files (filevault index):
sqlite3 data/yunam/yunam.db 'SELECT relpath, kind, mime_type, file_size FROM saved_files ORDER BY id DESC LIMIT 10;'

# Pending reminders:
sqlite3 data/yunam/yunam.db 'SELECT id, chat_id, fire_at, substr(message,1,60) FROM nudges WHERE sent_at IS NULL;'

# See what Yunam has saved to the vault:
ls -la ~/obsidian/ ~/filevault/
```

## Roadmap

- **Phase 1** — Agent core with Claude + Obsidian. **Done.**
- **Phase 1.5** — Binary attachments + Voyage multimodal embeddings + daily retrospective + skill/scope governance layer. **Done.**
- **Phase 2** — Extended skills + integrations. **In progress.**
  - [x] Web browsing (Jina Reader + DuckDuckGo)
  - [x] Korean skills bundle (air quality, parcel tracking)
  - [x] Reminders / nudge sweeper
  - [x] Long-term memory with semantic recall
  - [x] Multi-principal support (jaekeun + yoolim)
  - [x] Group-chat support (mention/trigger-word gating)
  - [x] Obsidian graph analysis (backlinks, orphans)
  - [x] Deep-think path (`/think` → Opus 4.7)
  - [x] Google Calendar MCP integration
  - [x] Manual diary command (`/diary`)
  - [x] Per-turn preference injection (context primer)
  - [x] Handler modularization (handlers/ package)
  - [ ] Finance Agent (MoneyFlow MCP integration)
  - [ ] Evals and automated testing

## Pending — Stock Agent MCP wiring

In-flight integration with [`stock-agent`](../stock-agent) (institutional/pension supply analysis). Resume from here after the running backfill finishes (`docker exec yunam-stock-mcp tail -f /app/data/backfill.log`):

- [x] Add `supply_history` table to `canonical.db` (schema was missing; entrypoint skips bootstrap when DB exists)
- [x] Add `YUNAM_STOCK_MCP_URL=http://stock-mcp:3001/sse` to `.env`
- [x] Override stock-mcp healthcheck in `docker-compose.yml` (Dockerfile probes :8001 HTTP but mcp_run.py serves SSE on :3001)
- [x] Patch `../stock-agent/src/stock_agent/agent_int/mcp_run.py` to allowlist `stock-mcp` host in FastMCP `TransportSecuritySettings` (otherwise HTTP 421 "Invalid Host header" from DNS-rebinding protection)
- [ ] Run the 7-day backfill (`docker exec -d yunam-stock-mcp python /app/backfill.py --days 7`) — **in progress as of 2026-05-12**
- [ ] After backfill: `docker compose up -d --no-deps --build --force-recreate stock-mcp` to apply the host-allowlist fix
- [ ] Restart calendar-mcp before each gateway restart (`docker restart yunam-calendar-mcp`) — nspady's stateful MCP is single-session per server, so a leftover session blocks new `initialize` with "Server already initialized"
- [ ] `docker compose up -d --no-deps --force-recreate gateway` to pick up new `.env` and reconnect to both MCPs
- [ ] Verify: `docker logs yunam-gateway | grep -E "(gcal|stock) MCP connected"` shows both, and Telegram answers "어제 수급 좋았던 종목" via `get_historical_supply`/`analyze_supply` instead of falling back to web search
- [ ] Verify `supply_history` populated: 14 rows expected (7 dates × {KOSDAQ, KOSPI})
- [ ] Commit stock-agent uncommitted files: `backfill.py`, `src/stock_agent/supply/`, `src/stock_agent/agent_int/mcp_run.py`, `src/stock_agent/agent_int/mcp_server.py`, and schema/env diffs

## Security notes

- `.env` is gitignored. Never commit real tokens or API keys.
- Bot replies only to principals listed in `YUNAM_PRINCIPALS`. Unknown users are silently ignored — check the logs.
- Group chats require explicit opt-in via `YUNAM_ALLOWED_CHATS`.
- Vault paths are sandboxed; `..` escapes and absolute paths are rejected before any filesystem call.
- Size caps: 1 MB per read, 500 KB per write.
- Per-principal privacy boundaries — private turns are filtered out of other principals' history.
- If a token leaks, revoke via `/revoke` in `@BotFather` and update `.env`.
