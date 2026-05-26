# Yunam

Personal AI assistant running 24/7 on a Vultr Tokyo VPS, controlled via Telegram. Dual-model architecture: **Claude Sonnet 4.6** for everyday conversation, **Opus 4.7** via `/think` for hard problems. Access to your Obsidian vault, a separate filevault for binary attachments (photos / docs / voice notes indexed for semantic search), web browsing, Google Calendar, Korean stock supply analysis, a curated news/market feed with auto-newsletter, air quality, parcel tracking, reminders, long-term memory, and API/cost tracking.

Multi-principal: jaekeun (owner) and yoolim share the same bot with per-person privacy boundaries and memory isolation.

For architecture decisions, the governance layer, secrets handling, and the phase roadmap, see [CLAUDE.md](CLAUDE.md). For the active execution plan, see [milestone.md](milestone.md).

## What it does (today)

From Telegram (DM or authorized group chats):

- Free-form chat → Claude Sonnet 4.6 replies, reading/writing your Obsidian vault as needed.
- Send a photo / document / voice note → cached as "pending"; `/save [caption]` to commit to the filevault with a multimodal embedding for later semantic search.
- "Find that whiteboard photo from standup" → semantic search over saved files (Voyage multimodal embeddings).
- "Send me that receipt I saved last week" → Yunam retrieves and Telegram-attaches it.
- "오늘 서울 미세먼지 어때?" → air quality via Open-Meteo.
- "택배 어디쯤이야?" → parcel tracking via Sweet Tracker.
- "내일 3시에 알려줘" → reminders via the nudge sweeper (proactive Telegram push).
- "지난번에 X 얘기 뭐였지?" → long-term memory recall (semantic search over every prior turn).
- "내일 일정 알려줘" → Google Calendar via MCP sidecar.
- "어제 수급 좋았던 종목 보여줘" → Korean equity institutional flow analysis via Stock-Agent MCP.
- "오늘 뭐 들어왔어?" → list curated news items routed today (urgent / digest / drop).
- "비용 얼마 썼어?" → API spend / token usage / cache hit rate.
- Web search and page reading via Jina / DuckDuckGo.
- Group-chat support with `@bot` mention, reply, or trigger-word gating (`유남아`, `yunam`, etc.).
- **Automatic curation runner** — every 60–120 minutes fetches Naver / Toss Invest / tiered RSS / Stock-Agent moneyflow; high-score items push to Telegram immediately, mid-score items roll into a daily 21:00 KST newsletter, low-score items are stored for later analysis.
- Unknown users → silently ignored (logged at WARNING).

## Slash commands

Send `/help` in Telegram to see this live (zero-token static reply).

| Command | What it does |
|---|---|
| `/start` | Greeting + auth confirmation |
| `/help` | Static usage guide (this list + capabilities + tips) |
| `/save [caption]` | Commit the most recently sent attachment to the filevault + embed |
| `/think <query>` | Routes to Opus 4.7 with adaptive thinking (slow + expensive) |
| `/diary [content]` | Daily reflection. Without content: sends a prompt; with content: processes + saves to `daily/YYYY-MM-DD.md` |
| `/newsletter [hours]` | Build and send the curation digest now (default lookback 24h) — useful for testing without waiting until 21:00 |
| `/chatid` | Echo current chat info (for adding new groups to `YUNAM_ALLOWED_CHATS`) |

Capability tools (called automatically by the agent based on what you ask):

- **Obsidian vault** — read/write/list/search + graph (backlinks, outgoing links, find-by-tag, graph queries)
- **Files / attachments** — save with caption/description, semantic search over content+metadata, retrieve and send back
- **Web** — search (Jina, falls back to DuckDuckGo) + URL fetch (Jina Reader)
- **Korean** — air quality (Open-Meteo), parcel tracking (Sweet Tracker)
- **Reminders** — schedule / list / cancel proactive nudges
- **Memory** — semantic recall over every past conversation turn
- **Calendar** (optional, MCP) — events, scheduling, attendee management
- **Stock supply** (optional, MCP) — Korean equity institutional flow analysis
- **Curation** — read recent curated items, semantic search across the stream, edit the interest profile
- **API usage** — `usage_summary`, `usage_breakdown`, `cost_alert_status`
- **Privacy** — `mark_turn_private` keeps a turn out of other principals' history

## Structure

```
yunam/
├── .env.example                     # Template — copy to .env and fill in
├── docker-compose.yml               # gateway + stock-mcp + (profile-gated) calendar-mcp
├── docker-compose.consent.yml       # One-time Google Calendar OAuth consent flow
├── gcp-oauth.keys.json              # Google OAuth credentials (gitignored)
├── scripts/
│   ├── redeploy.sh                  # Clean redeploy (works around nspady stale-session)
│   ├── repl.py                      # Local dev REPL (fake or --real Claude)
│   ├── smoke_curation.py            # Smoke: curation pipeline end-to-end
│   ├── smoke_dual_model.py          # Smoke: Sonnet + Opus paths
│   ├── smoke_gcal.py                # Smoke: Google Calendar MCP
│   ├── smoke_korean.py              # Smoke: Korean skills bundle
│   ├── smoke_multiuser.py           # Smoke: multi-principal flows
│   ├── smoke_usage.py               # Smoke: usage tracking + audit
│   └── smoke_web.py                 # Smoke: web skill
├── docs/
│   └── gcal-setup.md                # Google Calendar MCP OAuth setup guide
├── data/yunam/                      # SQLite DB lives here (bind-mounted, gitignored)
├── mcp-servers/
│   └── google-calendar-mcp/         # nspady/google-calendar-mcp (cloned sibling)
└── gateway/
    ├── Dockerfile                   # python:3.12-slim + gosu for PUID/PGID
    ├── entrypoint.sh                # uid remap → chown data → drop to appuser
    ├── requirements.txt             # PTB + anthropic + langgraph + aiosqlite + voyageai + feedparser + ...
    ├── main.py                      # Composition root — builds deps, registers handlers, lifecycle
    ├── handlers/                    # Telegram handler package
    │   ├── __init__.py              # register_handlers() — single entry point
    │   ├── _helpers.py              # Shared constants (TELEGRAM_MSG_LIMIT, send_reply, ...)
    │   ├── commands.py              # /start, /help, /save, /think, /diary, /newsletter, /chatid
    │   ├── text.py                  # Free-text handler + group-chat engagement logic
    │   └── attachments.py           # Receive, batch (media-group), and process file uploads
    └── yunam/                       # Core package
        ├── config.py                # Env loading, Principal/Config dataclasses, logging setup
        ├── auth.py                  # Principal resolution, chat allowlists, group triggers
        ├── prompts.py               # Core SYSTEM_PROMPT + DAILY_PROMPT_TEMPLATE
        ├── orchestrator.py          # LangGraph + Claude tool loop (SkillRegistry consumer)
        ├── sessions.py              # aiosqlite store + schema migrations (DB v8)
        ├── capabilities.py          # Scope enum (vault:*, filevault:*, web:*, curation:*, ...)
        ├── embeddings.py            # Voyage multimodal client (image + text)
        ├── text_embedder.py         # Pluggable text embedder (Voyage or Jina v3)
        ├── context_primer.py        # Per-turn preference injection from Obsidian vault
        ├── sender.py                # AttachmentSender Protocol + PTBSender
        ├── vision.py                # Image content block helpers for inline vision
        ├── files.py                 # Filevault path safety + name sanitization
        ├── scheduler.py             # Nudge sweeper coroutine (reminder delivery loop)
        ├── skills/                  # Governance layer — where new capabilities are added
        │   ├── base.py              # Skill, ToolSpec, DispatchContext, SkillRegistry
        │   ├── obsidian.py          # Vault read/write/list/search
        │   ├── obsidian_graph.py    # Backlinks / tags / graph queries
        │   ├── files.py             # Filevault save/search/retrieve
        │   ├── web.py               # web_search + web_fetch
        │   ├── airquality.py        # air_quality (Open-Meteo)
        │   ├── parcel.py            # parcel_track (Sweet Tracker)
        │   ├── reminders.py         # schedule/list/cancel reminders
        │   ├── memory.py            # recall (semantic search over turns)
        │   ├── privacy.py           # mark_turn_private
        │   ├── usage.py             # usage_summary / usage_breakdown / cost_alert_status
        │   └── curation.py          # list_recent_curated / search_curated / interests
        ├── tools/                   # Low-level primitives (no model/schema awareness)
        │   ├── vault.py             # safe_join, size caps, atomic write, VaultError
        │   ├── obsidian.py          # ObsidianTools
        │   ├── obsidian_graph.py    # ObsidianGraphTools
        │   ├── attachments.py       # AttachmentTools
        │   ├── web.py               # WebTools (Jina Reader + DuckDuckGo)
        │   ├── airquality.py        # AirQualityTools
        │   ├── parcel.py            # ParcelTools
        │   ├── reminders.py         # ReminderTools
        │   ├── memory.py            # MemoryTools
        │   ├── usage.py             # UsageTools (queries over api_usage)
        │   └── curation.py          # CurationTools
        ├── mcp/                     # External MCP adapters
        │   ├── gcal.py              # Google Calendar — raw JSON-RPC over nspady streamable-http
        │   └── stock.py             # Stock-Agent — FastMCP SSE
        ├── runners/                 # Background workers (NOT in SkillRegistry)
        │   ├── curator.py           # Hourly tick loop + 21:00 newsletter cron
        │   ├── summarizer.py        # Haiku 4.5 summarizer
        │   ├── scorer.py            # Voyage embed + vec0 KNN vs interest_profile
        │   ├── router.py            # URGENT / DIGEST / DROP threshold split
        │   ├── digester.py          # Newsletter builder
        │   ├── pusher.py            # PTBSender wrapper + audit writes
        │   └── sources/
        │       ├── base.py          # CuratedCandidate + FeedSource Protocol
        │       ├── naver_news.py    # Naver Search OpenAPI (news)
        │       ├── rss_generic.py   # Tier-aware RSS/Atom (HIGH/MID/LOW divisors)
        │       ├── toss_invest.py   # Toss Invest internal JSON API (playwright fallback stub)
        │       ├── x_playwright.py  # X (Twitter) — stub, needs Chromium sidecar
        │       └── moneyflow_pull.py# Stock-Agent MCP pull for today's hot tickers
        ├── usage/                   # Usage tracking
        │   ├── recorder.py          # UsageRecorder (ContextVar-bound, async write)
        │   └── rates.py             # Per-provider per-1M pricing tables
        └── subagents/               # Separately-configured Claude calls
            └── deep_think.py        # Opus 4.7 + adaptive / high-effort (only via /think)
```

For the full file-by-file tour and the governance layer (how to add a skill / MCP / sub-agent), see [CLAUDE.md](CLAUDE.md).

## Prerequisites

- Docker & Docker Compose
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)
- Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- Voyage API key from [dash.voyageai.com](https://dash.voyageai.com) — for multimodal embeddings on saved files
- A directory to serve as your Obsidian vault (create `~/obsidian` if it doesn't exist)
- A directory for binary attachments, separate from the vault (defaults to `~/filevault`)

Optional:
- Jina API key from [jina.ai](https://jina.ai/reader) — enables Jina Search; also enables `YUNAM_TEXT_EMBEDDER=jina` (free-tier text embeddings as a Voyage alternative)
- Sweet Tracker API key from [sweettracker.co.kr](http://info.sweettracker.co.kr/apikey/add) — parcel tracking
- Google Calendar MCP setup — see [docs/gcal-setup.md](docs/gcal-setup.md)
- Naver OpenAPI credentials from [developers.naver.com](https://developers.naver.com/apps/) — Korean news curation
- Stock-Agent sibling repo cloned at `../stock-agent` — Korean equity supply/demand MCP

## Local testing (no token burn)

The fake-Claude REPL exercises the SQLite + vault wiring without hitting the API:

```bash
cd gateway && pip install -r requirements.txt && cd ..
PYTHONPATH=gateway python scripts/repl.py
# Try: "hello"  (plain text)
#      "write hello"  (triggers vault_write)
#      "read test"    (triggers vault_read)
#      "escape"       (tries to read outside vault — should is_error)
```

Smoke tests for individual subsystems (no Telegram, no network — temporary DB):

```bash
PYTHONPATH=gateway python scripts/smoke_usage.py       # Phase 2.0 — usage tracking + audit
PYTHONPATH=gateway python scripts/smoke_curation.py    # Phase 2.1 — curation pipeline end-to-end
PYTHONPATH=gateway python scripts/smoke_korean.py      # air quality + parcel
PYTHONPATH=gateway python scripts/smoke_web.py         # web skill
PYTHONPATH=gateway python scripts/smoke_multiuser.py   # multi-principal flows
PYTHONPATH=gateway python scripts/smoke_dual_model.py  # Sonnet + Opus paths (uses real Anthropic if ANTHROPIC_API_KEY is set)
PYTHONPATH=gateway python scripts/smoke_gcal.py        # Google Calendar MCP (requires sidecar running)
```

Once wired up, run one turn against real Anthropic to verify caching:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
PYTHONPATH=gateway python scripts/repl.py --real
# Send two turns. On the second, the log should show cache_read>0.
```

## Run it locally via Docker

```bash
cp .env.example .env
# Edit .env and fill in at minimum:
#   TELEGRAM_BOT_TOKEN, YUNAM_PRINCIPALS (or TELEGRAM_ALLOWED_USER_ID),
#   ANTHROPIC_API_KEY, VOYAGE_API_KEY

# macOS only: set PUID/PGID to your host user so vault writes land with the right owner.
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env

mkdir -p ~/obsidian ~/filevault data/yunam

docker compose up --build       # foreground; Ctrl+C to stop

# With Google Calendar MCP (profile-gated):
docker compose --profile gcal up --build
```

From your phone, DM [`@AgentYunamBot`](https://t.me/AgentYunamBot):

- `/help` → list of commands + capabilities
- `/start` → greeting
- any message → Yunam responds, with vault reads/writes as needed
- `/think <question>` → deep reasoning via Opus 4.7
- `/diary` → daily reflection prompt
- `/newsletter` → fire the curation digest immediately

## Deploy to the VPS

**Stop the local container first** — two bots on the same token race on long-poll and each message lands on only one of them.

```bash
# On VPS (first time):
ssh yunam
git clone git@github.com:agdal1125/yunam.git ~/yunam
cd ~/yunam
cp .env.example .env
# Edit .env:
#   - TELEGRAM_BOT_TOKEN, YUNAM_PRINCIPALS, ANTHROPIC_API_KEY, VOYAGE_API_KEY
#   - YUNAM_NUDGE_SWEEPER_ENABLED=true to enable reminder delivery
#   - YUNAM_CURATION_ENABLED=true + NAVER_* + tiered RSS feeds to enable curation
#   - YUNAM_TEXT_EMBEDDER=jina if you want free-tier text embeddings (Voyage stays for images)
#   - Leave PUID=1000, PGID=1000 (VPS user is uid 1000)

mkdir -p ~/obsidian ~/filevault data/yunam

# Initial bring-up (also pulls calendar-mcp if --profile gcal is set in your env)
docker compose up -d --build

docker compose logs -f gateway  # expect: "gateway starting" then "gateway running"
```

No inbound ports are exposed — long polling only.

## Docker operations

The single command you'll run most often after a code or `.env` change:

```bash
bash scripts/redeploy.sh
```

This does `docker compose down && docker compose up -d --build`. The `down` step is important: it stops calendar-mcp, which clears nspady's in-memory MCP session — without that, the gateway hits "Server already initialized" on the next start (see [Troubleshooting](#troubleshooting)).

Other common commands:

```bash
# What's running?
docker compose ps

# Logs (live tail, all services):
docker compose logs -f

# Logs (last 100 lines, gateway only):
docker logs yunam-gateway --tail 100

# Logs filtered for connect-status of optional integrations:
docker logs yunam-gateway 2>&1 | grep -E "(gcal|stock) MCP (connected|connect failed|configured|disabled)"

# Logs filtered for curation tick / newsletter:
docker logs yunam-gateway 2>&1 | grep -E "curation (tick|newsletter|runner)"

# Restart just the gateway (does NOT clear calendar-mcp's session — usually causes the "already initialized" error; prefer redeploy.sh):
docker compose restart gateway

# Restart calendar-mcp first, then gateway (manual variant of redeploy.sh):
docker restart yunam-calendar-mcp
sleep 15
docker compose restart gateway

# Stop everything:
docker compose down

# Stop everything + remove volumes (nuclear; loses calendar OAuth tokens):
docker compose down -v

# Pull latest code, then redeploy cleanly:
git pull && bash scripts/redeploy.sh

# Tail logs while watching curation ticks (useful right after enabling curation):
docker logs -f yunam-gateway 2>&1 | grep --line-buffered -E "(curation|claude|tool=)"
```

For dev iteration where you only changed `.env` (no code edits), `docker compose up -d` (no `--build`) is enough — Compose reuses the cached image and just restarts containers with the new env. Still safer to use `redeploy.sh` to also clear calendar-mcp.

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

# Today's API spend by provider (in µUSD — divide by 1e6 for USD):
sqlite3 data/yunam/yunam.db \
  "SELECT provider, COUNT(*) calls, SUM(cost_usd_micro) cost_micro FROM api_usage WHERE created_at >= date('now') GROUP BY provider ORDER BY cost_micro DESC;"

# Today's curation route distribution:
sqlite3 data/yunam/yunam.db \
  "SELECT routed_as, COUNT(*) FROM curated_items WHERE fetched_at >= date('now') GROUP BY routed_as;"

# What got pushed urgent today:
sqlite3 data/yunam/yunam.db \
  "SELECT title, source, score, pushed_at FROM curated_items WHERE routed_as='urgent' AND pushed_at IS NOT NULL ORDER BY pushed_at DESC LIMIT 10;"

# Current interest profile:
sqlite3 data/yunam/yunam.db \
  'SELECT label, weight, enabled FROM interest_profile ORDER BY label;'

# Schema version (should be 8 with curation shipped):
sqlite3 data/yunam/yunam.db 'PRAGMA user_version;'

# Saved files (filevault index):
sqlite3 data/yunam/yunam.db 'SELECT relpath, kind, mime_type, file_size FROM saved_files ORDER BY id DESC LIMIT 10;'

# Pending reminders:
sqlite3 data/yunam/yunam.db 'SELECT id, chat_id, fire_at, substr(message,1,60) FROM scheduled_nudges WHERE sent_at IS NULL AND cancelled_at IS NULL;'

# Which MCP skills loaded successfully this run:
docker logs yunam-gateway 2>&1 | grep -E "(gcal|stock) MCP connected"

# See what Yunam has saved to the vault:
ls -la ~/obsidian/ ~/filevault/
```

From inside Telegram (no SQL needed) — ask Yunam directly:

- "오늘 비용 얼마 썼어?" → `usage_summary today`
- "어느 skill이 제일 많이 썼어?" → `usage_breakdown today skill_id`
- "오늘 뭐 들어왔어?" → `list_recent_curated today`
- "관심사 보여줘" → `list_interests`

## Roadmap

- **Phase 1** — Agent core with Claude + Obsidian. **Done.**
- **Phase 1.5** — Binary attachments + Voyage multimodal embeddings + daily retrospective + skill/scope governance layer. **Done.**
- **Phase 2.0** — API/Cost usage tracking skill + UsageRecorder. **Done** (DB v7, `api_usage` table, `usage_summary` / `usage_breakdown` / `cost_alert_status` tools).
- **Phase 2.1** — Curation pipeline. **Done** (DB v8, `runners/` package with hourly tick + 21:00 newsletter, sources: Naver / RSS tiered / Toss / Moneyflow / X stub, `curation` skill for in-conversation read/admin).
- **Phase 2.x — in flight** (see [milestone.md](milestone.md)):
  - [ ] **Phase 2.2** — Reflection / digital-twin memory (draft-then-approve, no auto-apply to `profile/*.md`)
  - [ ] **Phase 2.3** — Finance guardrail sub-agent wrapping Stock-Agent + a mistake-ledger skill
  - [ ] **Phase 2.4** — Hardening / eval / DB backup cron / docs sync

Shipped earlier in Phase 2:

- Web browsing (Jina Reader + DuckDuckGo)
- Korean skills bundle (air quality, parcel tracking)
- Reminders / nudge sweeper
- Long-term memory with semantic recall
- Multi-principal support (jaekeun + yoolim) + per-turn privacy
- Group-chat support (mention/trigger-word gating)
- Obsidian graph analysis (backlinks, outgoing links, find-by-tag)
- Deep-think path (`/think` → Opus 4.7 with adaptive thinking)
- Google Calendar MCP integration
- Stock-Agent MCP integration (institutional supply/demand analysis)
- Manual diary command (`/diary`)
- `/help` static command (zero-Anthropic-token user guide)
- `/newsletter` on-demand digest command
- Per-turn preference injection (context primer)
- Pluggable text embedder (Voyage default, Jina v3 as free-tier alternative)
- Handler modularization (`handlers/` package)

## Troubleshooting

### Gateway can't reach calendar-mcp on restart — "Server already initialized"

**Symptom** in `docker logs yunam-gateway`:

```
WARNING yunam.mcp.gcal: gcal MCP: server reports 'already initialized'; reusing existing session ...
ERROR yunam.gateway: gcal MCP connect failed — skill disabled for this run
RuntimeError: MCP HTTP 400 on tools/list: {"error":{"message":"Bad Request: Mcp-Session-Id header is required"}}
```

**Why**: nspady google-calendar-mcp keeps a single in-memory MCP session. When you `docker compose restart gateway`, calendar-mcp stays up but the gateway has no session id. nspady refuses to issue a new one and the recovery path can't read a session id from its error response.

**Fix**: use `bash scripts/redeploy.sh` instead of `docker compose restart gateway`. The script runs `docker compose down && docker compose up -d --build`, which stops calendar-mcp and clears nspady's in-memory state. Manual variant:

```bash
docker restart yunam-calendar-mcp
sleep 15
docker compose restart gateway
docker logs yunam-gateway 2>&1 | grep "gcal MCP connected"  # expect a tools=N line
```

### Stock-mcp connect race on cold start

**Symptom**: `httpx.ConnectError: All connection attempts failed` during `stock MCP` connect, but `docker compose ps` shows stock-mcp `(healthy)`.

**Why**: gateway started before stock-mcp finished its `Application startup complete` phase. As of 2026-05-23, `docker-compose.yml` waits for `service_healthy` on stock-mcp, so this should no longer happen on cold starts — but if it does, the fix is:

```bash
docker compose restart gateway
```

Stock-mcp is already healthy by then, and the retry connects cleanly.

### A specific MCP skill isn't being used

MCP `connect()` failures are non-fatal — the gateway logs the failure and skips that skill for the run rather than crashing. Check which skills loaded:

```bash
docker logs yunam-gateway 2>&1 | grep -E "MCP connected"
```

To re-attempt: fix the upstream issue (OAuth, container down, host header) and `bash scripts/redeploy.sh`.

### Voyage quota exceeded

Switch text embeddings to Jina (free tier ~1M tokens/month) — only photo/file saves keep hitting Voyage:

```
YUNAM_TEXT_EMBEDDER=jina
```

Make sure `JINA_API_KEY` is set, then `bash scripts/redeploy.sh`. Verify:

```bash
docker logs yunam-gateway 2>&1 | grep "text embedder ="
```

Should show `text embedder = jina (multimodal path stays on voyage)`.

### Two messages, one reply

Long-poll race between local and VPS containers running the same `TELEGRAM_BOT_TOKEN`. Stop one (`docker compose stop` locally, or on the VPS) before bringing the other up.

### `vault_write` returns "Permission denied"

`PUID`/`PGID` in `.env` don't match the host user that owns `~/obsidian`. Fix the env vars (`PUID=$(id -u)`, `PGID=$(id -g)`) and restart. **Do not** `chown -R` the vault directly — Obsidian Sync will retransmit every file.

### Curation runner is enabled but nothing's being fetched

Check the per-source tally in the curator log:

```bash
docker logs yunam-gateway 2>&1 | grep "curation tick:"
```

You should see a line like `curation tick: fetched_per_source={'naver': 10, 'rss-high': 9, 'rss-mid': 0, ...}`. Common causes:

- All-zero fetches → credentials missing. Verify `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` / `YUNAM_CURATION_NAVER_QUERIES` and your tiered RSS env vars are set.
- Items fetched but all routed to `drop` → your interest profile is empty or doesn't match the content. Tell Yunam in chat: `add_interest('AI 인프라', 'AI 반도체와 클라우드 인프라 동향')`, etc.
- Newsletter says queue is empty → curator hasn't ticked yet (default interval 120 min on first deploy). Either wait, or temporarily lower `YUNAM_CURATION_INTERVAL_MINUTES=5` and redeploy.

## Security notes

- `.env` is gitignored. Never commit real tokens or API keys.
- Bot replies only to principals listed in `YUNAM_PRINCIPALS`. Unknown users are silently ignored — check the logs.
- Group chats require explicit opt-in via `YUNAM_ALLOWED_CHATS`.
- Vault paths are sandboxed; `..` escapes and absolute paths are rejected before any filesystem call.
- Size caps: 1 MB per read, 500 KB per write.
- Per-principal privacy boundaries — private turns are filtered out of other principals' history.
- External text (curated news items) enters as user messages only — never concatenated into the system prompt. Prompt-injection from third-party content can't change Yunam's behavior policy.
- The agent's tool surface does NOT include push-to-Telegram or curation-fetch — those are background-runner-only, so prompt injection can't make the model send proactive messages or trigger external API calls outside of normal tool use.
- If a token leaks, revoke via `/revoke` in `@BotFather` and update `.env`.
