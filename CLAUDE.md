# Yunam — Personal AI Agent

## Project Overview

Personal AI assistant server running 24/7 on a Vultr Tokyo VPS, controlled via Telegram (and later Slack). This repo is the control plane.

**Current phase**: Phase 2 is well underway. The agent runs on a **dual-model orchestrator** (Sonnet 4.6 default + Opus 4.7 via `/think`) over a SkillRegistry of 11 in-process skills + 2 MCP adapters + 1 sub-agent. Shipped capabilities include: Obsidian vault read/write + graph analysis, binary attachments with Voyage multimodal embeddings, web search (Jina + DuckDuckGo), air quality, parcel tracking, reminders / nudge sweeper, long-term memory with semantic recall, per-principal privacy controls, multi-principal support (jaekeun + yoolim), per-turn preference primer, Google Calendar MCP, Stock-Agent MCP, and deep-think Opus path. Current focus is the Phase 2.x roadmap captured in [milestone.md](milestone.md) — curation pipeline, reflection/digital-twin memory, finance guardrail sub-agent, and API/cost usage tracking. New capabilities are added through the governance layer described below, not by extending the orchestrator directly.

## Owner & Environment

- **Owner**: jaekeun (GitHub: `agdal1125`)
- **Repo**: `git@github.com:agdal1125/yunam.git` (private)
- **VPS**: Vultr Tokyo, Ubuntu 24.04, vc2-2c-2gb
- **SSH alias**: `yunam` (user: `jaekeun`) — already configured in `~/.ssh/config`
- **Telegram bot**: `@AgentYunamBot`
- **Local dev machine**: macOS

## Progress

- [x] Phase 0-1 to 0-7: VPS provisioned, SSH hardened, Docker installed, Telegram echo bot deployed
- [x] **Phase 1**: Agent core — LangGraph + SQLite sessions + Obsidian vault (read + write) on Claude
- [x] **Phase 1.5**: Binary attachments (filevault + Voyage multimodal embeddings + semantic file search), daily retrospective scheduler, skill/scope governance layer with idempotent schema migrations
- [x] **Phase 2 (in progress)**: governance-driven capability buildout
  - [x] Dual-model split (Sonnet 4.6 main + Opus 4.7 `/think` sub-orchestrator at [subagents/deep_think.py](gateway/yunam/subagents/deep_think.py))
  - [x] Multi-principal support (jaekeun + yoolim, per-turn `visibility`, privacy skill, ACL-filtered history/recall)
  - [x] Group-chat support (mention/trigger-word gating in [auth.py](gateway/yunam/auth.py))
  - [x] Web skill (Jina Reader/Search + DuckDuckGo fallback)
  - [x] Korean skills bundle: air quality (Open-Meteo), parcel tracking (Sweet Tracker)
  - [x] Reminders + nudge sweeper (proactive Telegram dispatch loop)
  - [x] Long-term memory skill with semantic recall over message turns
  - [x] Obsidian graph skill (backlinks, outgoing-links, find-by-tag, graph queries)
  - [x] Per-turn preference primer ([context_primer.py](gateway/yunam/context_primer.py)) — loads `preferences/*.md` into the user message, not the system prompt, to preserve cache
  - [x] Google Calendar MCP adapter ([mcp/gcal.py](gateway/yunam/mcp/gcal.py)) over nspady streamable-http; tolerates "Server already initialized" on restart
  - [x] Stock-Agent MCP adapter ([mcp/stock.py](gateway/yunam/mcp/stock.py)) over FastMCP SSE; supply/demand analysis of Korean equities
  - [x] Manual diary command (`/diary`)
  - [x] Handler modularization (`gateway/handlers/`)
- [ ] **Phase 2.x (planned, see [milestone.md](milestone.md))**: API/cost usage tracking skill, curation pipeline (Naver + Toss Invest + RSS + optional X via RSSHub), reflection/digital-twin memory with draft-then-approve, Finance guardrail sub-agent, hardening/eval/CLAUDE.md sync

## Project Structure

```
yunam/
├── .gitignore
├── .env.example                     # Template — required env vars + docs
├── .env                             # NEVER committed; contains real secrets
├── README.md                        # User-facing setup + run guide
├── CLAUDE.md                        # This file — project instructions, auto-loaded by Claude Code
├── milestone.md                     # Phase 2.x execution plan (curation / digital-twin / finance / usage)
├── docker-compose.yml               # gateway + stock-mcp + (profile-gated) calendar-mcp
├── docker-compose.consent.yml       # One-time Google Calendar OAuth consent override
├── data/yunam/                      # Bind-mount target for SQLite DB (gitignored)
├── docs/
│   └── gcal-setup.md                # Google Calendar OAuth bootstrap
├── mcp-servers/
│   └── google-calendar-mcp/         # nspady (cloned alongside repo)
├── scripts/
│   ├── repl.py                      # Local dev REPL (fake or --real Claude)
│   ├── smoke_dual_model.py          # Sonnet + Opus paths
│   ├── smoke_gcal.py                # Google Calendar MCP
│   ├── smoke_korean.py              # Korean skills bundle
│   ├── smoke_multiuser.py           # Multi-principal flows
│   └── smoke_web.py                 # Web skill
└── gateway/
    ├── Dockerfile                   # python:3.12-slim + gosu for PUID/PGID
    ├── entrypoint.sh                # uid remap → chown data → drop to appuser via gosu
    ├── requirements.txt             # PTB + anthropic + langgraph + aiosqlite + voyageai + sqlite-vec + Pillow + httpx + mcp + obsidiantools
    ├── main.py                      # Composition root — builds deps, registers handlers, manual PTB lifecycle
    ├── handlers/                    # Telegram handler package
    │   ├── __init__.py              # register_handlers() — single entry point for main.py
    │   ├── _helpers.py              # Shared constants (TELEGRAM_MSG_LIMIT, send_reply, ...)
    │   ├── commands.py              # /start, /save, /think, /diary, /chatid
    │   ├── text.py                  # Free-text handler + group-chat engagement logic
    │   └── attachments.py           # Receive, batch (media-group), and process file uploads
    └── yunam/                       # Core package
        ├── config.py                # Env loading, Principal/Config dataclasses, logging setup
        ├── auth.py                  # Principal resolution, chat allowlists, group triggers
        ├── prompts.py               # Core SYSTEM_PROMPT only — skill guidance lives in each skill module
        ├── orchestrator.py          # LangGraph: load_history → agent_step → persist; dual-model capable
        ├── sessions.py              # aiosqlite store + PRAGMA user_version migrations (DB v6)
        ├── capabilities.py          # Scope enum — vocabulary for tool authorization
        ├── embeddings.py            # Voyage multimodal client
        ├── context_primer.py        # Per-turn `preferences/*.md` injection into user message
        ├── sender.py                # AttachmentSender Protocol + PTBSender
        ├── vision.py                # Image content block helpers for inline vision
        ├── files.py                 # Filevault path safety + name sanitization
        ├── scheduler.py             # Nudge sweeper coroutine for reminder delivery
        ├── skills/                  # Governance layer — where new capabilities are added
        │   ├── base.py              # Skill, ToolSpec, DispatchContext, SkillRegistry
        │   ├── __init__.py          # Public exports of every build_*_skill factory
        │   ├── obsidian.py          # vault_read / vault_write / vault_list / vault_search
        │   ├── obsidian_graph.py    # vault_backlinks / vault_outgoing_links / vault_find_by_tag / vault_graph_query
        │   ├── files.py             # save_attachment(s) / search_files / retrieve_attachment
        │   ├── web.py               # web_search / web_fetch
        │   ├── airquality.py        # air quality (Open-Meteo)
        │   ├── parcel.py            # parcel_track (Sweet Tracker)
        │   ├── reminders.py         # schedule_reminder / list_reminders / cancel_reminder
        │   ├── memory.py            # recall — semantic search over message turns
        │   └── privacy.py           # mark_turn_private
        ├── tools/                   # Low-level primitives — no model/schema awareness
        │   ├── vault.py             # safe_join, size caps, atomic write, VaultError
        │   ├── obsidian.py          # ObsidianTools
        │   ├── obsidian_graph.py    # ObsidianGraphTools (obsidiantools-backed)
        │   ├── attachments.py       # AttachmentTools
        │   ├── web.py               # WebTools
        │   ├── airquality.py        # AirQualityTools
        │   ├── parcel.py            # ParcelTools
        │   ├── reminders.py         # ReminderTools
        │   └── memory.py            # MemoryTools
        ├── mcp/                     # External MCP server adapters
        │   ├── __init__.py          # Exports GCalMCPClient, StockMCPClient, build_*_mcp_skill
        │   ├── gcal.py              # nspady streamable-http; raw-JSON-RPC client, "already initialized" recovery
        │   └── stock.py             # FastMCP SSE; build_stock_mcp_skill factory (Scope.STOCK_SUPPLY_READ)
        └── subagents/               # Separately-configured Claude calls
            └── deep_think.py        # Opus 4.7 + adaptive thinking, invoked only by /think
```

**Layering discipline:** `tools/` modules are pure primitives (no Claude schemas, no dispatch). Schemas, scopes, prompt fragments, and dispatch live in `skills/` and `mcp/` — those are the only layers the orchestrator talks to. `capabilities.py` is the scope vocabulary that sits between them. Keep the dependency direction `skills/ → tools/ → (stdlib/external)` one-way; `mcp/` adapters depend only on `skills/base.py` + `capabilities.py` + the external MCP transport.

## Design Decisions

### Long polling, not webhook
The bot uses `python-telegram-bot` long polling. The container makes outbound calls to Telegram's API and receives updates on the same connection — **no inbound ports need to be exposed** on the VPS. This is intentional for Phase 0: zero attack surface. Webhook + Caddy + Cloudflare Tunnel is deferred to a later phase.

### Principal allowlist + group gating
The bot filters incoming updates against the `YUNAM_PRINCIPALS` JSON allowlist (legacy single-user `TELEGRAM_ALLOWED_USER_ID` is still honored when `YUNAM_PRINCIPALS` is unset). Unknown user_ids are silently ignored and logged at WARNING. In group chats, even authorized principals are ignored unless the message either (a) mentions `@<bot_username>`, (b) is a reply to a Yunam message, or (c) starts with a configured trigger word (`yunam`, `유남아`, ...). Group rooms additionally must be listed in `YUNAM_ALLOWED_CHATS`. Auth lives in [gateway/yunam/auth.py](gateway/yunam/auth.py).

### Non-root container user
The Dockerfile creates and runs as `appuser` (uid 1000), not root. Standard hardening.

### `restart: unless-stopped`
Docker Compose restart policy. Survives VPS reboots and container crashes, but respects manual `docker compose stop`.

## Secrets Handling — IMPORTANT

- **Never commit `.env`.** `.gitignore` must include it before the first commit.
- **Never hardcode tokens in `main.py`, `docker-compose.yml`, or any file Claude Code writes.** Always reference env vars.
- **Never print secrets to logs.** Log the user_id for auth events but never the token.
- If a secret is ever accidentally committed or pasted into a shared location, treat it as compromised — revoke via `@BotFather` (`/revoke`) and issue a new one.

## Required Environment Variables

| Name | Purpose | Source |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Auth to Telegram Bot API | `@BotFather` on Telegram |
| `YUNAM_PRINCIPALS` (or `TELEGRAM_ALLOWED_USER_ID`) | JSON array of `{user_id, name, is_owner?}` — multi-principal allowlist. Legacy single-user var is honored when the JSON form is unset. | `@userinfobot` on Telegram |
| `ANTHROPIC_API_KEY` | Claude API auth (Sonnet 4.6 main, Opus 4.7 deep-think) | `console.anthropic.com` |
| `VOYAGE_API_KEY` | Voyage multimodal embeddings — saved-file search AND message-turn semantic recall (memory skill) | `dash.voyageai.com` |

Optional / feature-flagged:

| Name | Default | Purpose |
|---|---|---|
| `YUNAM_ALLOWED_CHATS` | unset (DMs only) | JSON array or CSV of allowed group chat_ids. Without this, group rooms are entirely off. |
| `YUNAM_GROUP_TRIGGERS` | `yunam,Yunam,유남,유남아,유남이` | Group-chat vocative aliases that act like `@<bot>` mentions. |
| `YUNAM_VAULT_PATH_HOST` | `~/obsidian` | Host path to Obsidian vault (bind-mounted into `/data/obsidian`) |
| `YUNAM_FILEVAULT_PATH_HOST` | `~/filevault` | Host path to binary-attachment store (bind-mounted into `/data/filevault`) |
| `YUNAM_TIMEZONE` | `Asia/Seoul` | IANA tz name used by reminders, daily-stamp in user messages, group hour logic |
| `YUNAM_NUDGE_SWEEPER_ENABLED` | unset (disabled) | Truthy enables the reminder sweeper loop |
| `YUNAM_NUDGE_SWEEP_INTERVAL_SECONDS` | `60` | Sweeper tick |
| `JINA_API_KEY` | unset | Enables Jina Search; without it `web_search` falls back to DuckDuckGo HTML |
| `SWEETTRACKER_API_KEY` | unset | Enables `parcel_track`; without it the tool returns a friendly onboarding error |
| `YUNAM_GCAL_MCP_URL` | unset | e.g. `http://calendar-mcp:3000/mcp` — gcal skill is auto-disabled when unset |
| `YUNAM_STOCK_MCP_URL` | unset | e.g. `http://stock-mcp:3001/sse` — stock skill is auto-disabled when unset |
| `KRX_ID` / `KRX_PW` | unset | KRX Data Portal creds, consumed by sibling `stock-mcp` container (not by gateway) |
| `PUID` | `1000` | Host UID files written by the container should be owned by (set to `id -u` on mac) |
| `PGID` | `1000` | Host GID (set to `id -g` on mac) |

If either MCP URL is set but the container is unreachable / mis-OAuth'd / stuck on a stale session, the gateway logs the failure and **skips that skill** for this run rather than crashing — verify which skills loaded with `docker logs yunam-gateway | grep "MCP connected"`. See "Operational notes" below for the `Server already initialized` case.

## Dependencies

Lock-of-truth is [gateway/requirements.txt](gateway/requirements.txt). Compatibility brackets (not exact pins) — Anthropic SDK + langgraph + sqlite-vec all evolve fast; Docker build pins resolved versions for reproducibility in practice.

Key additions beyond the Phase 1 baseline:
- `httpx` — gcal MCP raw JSON-RPC client (chosen over `mcp.ClientSession` because nspady's stateful mode mishandles SSE close)
- `mcp` — stock MCP SSE client + the underlying spec
- `obsidiantools` — backend for the obsidian_graph skill (backlinks, tags, graph queries)

## Development Workflow

1. Edit locally on macOS in this repo
2. Test locally with `docker compose up` (foreground, Ctrl+C to stop)
3. Verify: send "hello" to `@AgentYunamBot` from phone → receive "hello" back
4. `git add . && git commit -m "..." && git push`
5. On VPS: `ssh yunam` → `cd ~/yunam` (first time: clone) → `git pull` → `docker compose up -d --build`
6. Verify again from phone: should work identically from VPS

Stop the local container before starting the VPS one, otherwise two bots will race on the same long-poll connection and Telegram will deliver each message to only one of them.

## Coding Conventions

- Python 3.12, type hints where they help readability
- `async def` handlers (python-telegram-bot v21 is async-native)
- Structured logging via stdlib `logging`, not `print`
- Fail fast: `os.environ["VAR"]` (KeyError) rather than `.get()` with silent defaults for required config
- Log unauthorized access attempts at WARNING level with `user_id` + `username` only — **never log message content**, in dev or prod (see `_is_authorized` in [gateway/main.py](gateway/main.py))

## Architecture

### Orchestrator shape

LangGraph: `START → load_history → agent_step → persist → END`. Three nodes, linear, no conditional edges. The Claude + tool loop lives **inside** `agent_step` — splitting call/tool into separate LangGraph nodes buys nothing and complicates prompt-caching audits. The orchestrator consumes a `SkillRegistry` built once at startup and uses `registry.lookup(name)` on every tool_use block; it never references individual tool classes. See [gateway/yunam/orchestrator.py](gateway/yunam/orchestrator.py).

### Claude request shape (dual-model: Sonnet default, Opus on `/think`)

The main orchestrator runs on **Sonnet 4.6** with no extended thinking. A
second `Orchestrator` instance configured for **Opus 4.7** with adaptive
thinking at high effort is invoked only when the user sends `/think <query>`
in Telegram — the main Sonnet path never delegates to it autonomously. Both
share the same `SkillRegistry` so tool surface and vault state are identical;
Anthropic caches prefix per-model so each path stabilizes its own cache.

Main (Sonnet 4.6) request:
```python
response = await client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    system=[{"type": "text", "text": self._system_prompt,
             "cache_control": {"type": "ephemeral"}}],
    tools=self._tool_schemas,
    messages=messages,
    # no `thinking` / `output_config` — Sonnet path is cost-optimized
)
```

Deep-think (Opus 4.7) request — built by `subagents/deep_think.py`:
```python
response = await client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8000,
    system=[{"type": "text", "text": self._system_prompt,
             "cache_control": {"type": "ephemeral"}}],
    thinking={"type": "adaptive"},       # Opus-4.7-only
    output_config={"effort": "high"},    # Opus-4.7-only
    tools=self._tool_schemas,
    messages=messages,
)
```

`self._system_prompt` is built in `Orchestrator.__init__` as `SYSTEM_PROMPT + "\n\n" + "\n\n".join(registry.system_prompt_fragments)`. `self._tool_schemas` is `registry.tool_schemas` — computed eagerly, byte-stable across turns. Both are intentionally assembled once; never rebuild per turn.

Forbidden on Opus 4.7 (all 400 errors): `temperature`, `top_p`, `top_k`, `budget_tokens`, assistant prefilling. `thinking` + `output_config` are Opus-4.7-only; the Sonnet path omits them entirely.

### Prompt caching invariants

- `SYSTEM_PROMPT` in [gateway/yunam/prompts.py](gateway/yunam/prompts.py) is a plain module constant. So is each skill's `SYSTEM_PROMPT_FRAGMENT` (e.g. [gateway/yunam/skills/obsidian.py](gateway/yunam/skills/obsidian.py), [gateway/yunam/skills/files.py](gateway/yunam/skills/files.py)). **Never interpolate** dates, chat_id, or per-request data — a single byte change invalidates the entire prefix.
- The composed system prompt is assembled in the registry's declared skill order. Reordering skills in [gateway/main.py](gateway/main.py)'s `SkillRegistry([...])` list flushes the cache for every existing deployment.
- Tool schemas are tuples/lists built as literals inside each skill module (see `_SCHEMAS` + `ToolSpec` ordering in `skills/obsidian.py`). The registry flattens them in skill order; the resulting list is frozen after init.
- Per-turn context (e.g. "today is X") goes into the user message, never the system prompt. [orchestrator.py](gateway/yunam/orchestrator.py)'s `_agent_step_node` wraps the user text with `[meta: now is ...]` for exactly this reason.

### Tool surface

Tools are bundled into **skills**, each of which owns its schemas, scopes, prompt fragment, and handlers. The orchestrator only sees `SkillRegistry`; it never references individual skill classes. Authoritative registration order (cache-affecting — see invariants) is in [main.py](gateway/main.py):

| Skill | Source | Tools | Scopes |
|---|---|---|---|
| `obsidian` | [skills/obsidian.py](gateway/yunam/skills/obsidian.py) | `vault_read`, `vault_write`, `vault_list`, `vault_search` | `vault:read`, `vault:write` |
| `files` | [skills/files.py](gateway/yunam/skills/files.py) | `save_attachment`, `save_attachments`, `search_files`, `retrieve_attachment` | `filevault:read`, `filevault:write`, `filevault:send` |
| `web` | [skills/web.py](gateway/yunam/skills/web.py) | `web_search`, `web_fetch` | `web:search`, `web:fetch` |
| `airquality` | [skills/airquality.py](gateway/yunam/skills/airquality.py) | `air_quality` | `airquality:read` |
| `parcel` | [skills/parcel.py](gateway/yunam/skills/parcel.py) | `parcel_track` | `parcel:read` |
| `gcal` (MCP, optional) | [mcp/gcal.py](gateway/yunam/mcp/gcal.py) | dynamic — nspady's `list-/get-/create-/update-/delete-/search-/respond-/manage-*` | `calendar:read`, `calendar:write` |
| `stock` (MCP, optional) | [mcp/stock.py](gateway/yunam/mcp/stock.py) | dynamic — currently `analyze_supply`, `get_historical_supply` | `stock:supply_read` |
| `reminders` | [skills/reminders.py](gateway/yunam/skills/reminders.py) | `schedule_reminder`, `list_reminders`, `cancel_reminder` | `reminder:schedule` |
| `memory` | [skills/memory.py](gateway/yunam/skills/memory.py) | `recall` | `memory:read` |
| `obsidian_graph` | [skills/obsidian_graph.py](gateway/yunam/skills/obsidian_graph.py) | `vault_backlinks`, `vault_outgoing_links`, `vault_find_by_tag`, `vault_graph_query` | `vault:graph` |
| `privacy` | [skills/privacy.py](gateway/yunam/skills/privacy.py) | `mark_turn_private` | `privacy:write` |

Plus the `/think` sub-orchestrator path ([subagents/deep_think.py](gateway/yunam/subagents/deep_think.py)) — same `SkillRegistry`, different model + thinking config. Not a skill; not registered as a tool.

Path safety for the Obsidian vault is `Path.resolve().is_relative_to(VAULT_ROOT)` in [tools/vault.py](gateway/yunam/tools/vault.py) — the only barrier between the model and the host FS. Writes are atomic (tempfile + rename). Size caps: 1 MB read, 500 KB write. The filevault gets its own analogous path-safety module at [gateway/yunam/files.py](gateway/yunam/files.py).

### Storage

- SQLite at `/data/yunam/yunam.db` (bind-mounted to host `./data/yunam/yunam.db`). WAL mode, `foreign_keys = ON`. Current schema version: **`DB_USER_VERSION = 6`** in [sessions.py](gateway/yunam/sessions.py).
- Tables / virtual tables (authoritative list is `_SCHEMA` in [sessions.py](gateway/yunam/sessions.py)):
  - `sessions` — one row per chat_id (created_at, last_seen_at).
  - `messages` — plain-text history only (no thinking / tool_use blocks). v6 added `user_id` + `visibility` columns for multi-principal ACL filtering. Indexed on `(chat_id, created_at)`; visibility index added during the v6 migration step.
  - `tool_calls` — brief per-call audit: name, input_json, result_preview, is_error, elapsed_ms, **skill_id**, **scope**, **principal_user_id** (v6). Governance bookkeeping written by the orchestrator on every dispatch.
  - `pending_attachments` — Telegram file_ids received but not yet committed.
  - `saved_files` — filevault-committed attachments with relpath, metadata, caption, description.
  - `scheduled_nudges` — proactive reminders scheduled by the reminders skill; polled by `run_nudge_sweeper`.
  - `message_turns` — one row per user→assistant exchange (denormalized for one-JOIN recall). Source of truth for the memory skill.
  - `file_embeddings` (vec0) — 1024-dim Voyage multimodal embeddings for KNN file search.
  - `message_embeddings` (vec0) — 1024-dim Voyage text embeddings over `message_turns` for the memory `recall` tool.
- Schema evolves via **idempotent migrations** guarded by `PRAGMA user_version` in [sessions.py](gateway/yunam/sessions.py): `DB_USER_VERSION` constant, column-exists checks around `ALTER TABLE`, no destructive ops. Bump the constant and add a versioned step when introducing new columns.
- `load_history` returns the last 20 messages as `[{role, content}]` — exactly what Claude expects. Multi-principal v6 adds DB-side ACL filtering by the viewing principal (private turns are dropped before the model sees them).

### Non-root + PUID/PGID

Container starts as root, [gateway/entrypoint.sh](gateway/entrypoint.sh) remaps `appuser`'s uid/gid to match `PUID`/`PGID`, chowns `/data/yunam`, then drops to `appuser` via `gosu`. On Ubuntu VPS the default `PUID=1000` matches `jaekeun` — no action needed. On macOS, set `PUID=$(id -u)` in `.env`. The vault directory (`/data/obsidian`) and filevault (`/data/filevault`) are **never chowned** — that could trigger a full Obsidian Sync re-sync and would rewrite every attachment's mtime.

### Local testing without token burn

[scripts/repl.py](scripts/repl.py) runs the full orchestrator with a fake Claude that recognizes trigger words (`write`, `read`, `escape`) to exercise the tool loop. It wires up only the obsidian skill (no attachments) — enough to exercise path safety, the registry, and schema migration. Real `--real` mode hits Anthropic for end-to-end verification.

## Governance — how to extend Yunam

Any new capability (a new tool bundle, a third-party MCP server, a specialist sub-agent) is added through the **skill layer** — not by modifying the orchestrator, not by adding ad-hoc branches to `agent_step`. The orchestrator treats every skill the same way regardless of where the tool's implementation lives. This section is the operating manual for that layer.

### Three-layer capability model

| Layer | What it is | When to use | File shape |
|---|---|---|---|
| **Skill** | In-process bundle of tools sharing a theme and a prompt fragment | Adding a few related tools that run in Yunam's process (filesystem, local DB, in-repo logic) | One module under `gateway/yunam/skills/` + a class under `gateway/yunam/tools/` |
| **MCP server** | External process exposing tools over the Model Context Protocol | Third-party integrations (Notion, GitHub, a proper Obsidian adapter, MoneyFlow) — anything where the tool implementation isn't mine to own | Thin adapter under `gateway/yunam/mcp/<name>.py` that discovers tools and wraps each as a `ToolSpec` |
| **Sub-agent** | A Claude call wrapped as a single `ask_<name>` tool, with its own system prompt, tools, model, and budget | Specialist reasoning with a different risk/cost profile (e.g. Finance Agent, with its own auth and a cheaper model) | `gateway/yunam/subagents/<name>.py`; exposed as a one-tool skill from the outside |

All three converge at `SkillRegistry` — the orchestrator never distinguishes them.

### Hard invariants (don't violate without a plan)

These are the things that break silently — no error at startup, no crash at runtime, just degraded cache hit rate, audit gaps, or security regressions. Guard them in every change.

1. **Prompt-cache prefix stability.** The system prompt (core + concatenated skill fragments) and the flattened tool schemas list are both cache-keyed. Any byte change anywhere in them invalidates the cache for every existing deployment.
   - Skills register in a fixed order in [gateway/main.py](gateway/main.py). **Append new skills; never interleave or reorder.**
   - `SYSTEM_PROMPT` and each skill's `SYSTEM_PROMPT_FRAGMENT` are module-level string literals with **no interpolation**. Per-turn data goes in the user message.
   - Tool schemas are list/tuple literals inside each skill module. Never build them from a dict, set, or dynamic config.
2. **Scope discipline.** Every `ToolSpec` declares exactly one `Scope` from [capabilities.py](gateway/yunam/capabilities.py). Scope assignment is a **policy decision made by a human**, not inferred from tool names. If a tool needs a scope that doesn't exist, add it to the enum — don't overload an existing one.
3. **Path safety is one-way.** All filesystem writes go through `safe_join()` (Obsidian) or the filevault equivalent. No tool handler ever accepts an already-resolved `Path` from the model.
4. **Schema migrations are idempotent.** Every DB change bumps `DB_USER_VERSION` in [sessions.py](gateway/yunam/sessions.py) and is guarded by a version check **and** a column-exists check (so a fresh DB created at the new version doesn't double-ALTER). Never write destructive migrations.
5. **Unknown tool names raise `VaultError`, not `KeyError`.** The orchestrator catches `VaultError` and surfaces a clean tool-error message to the model. Anything else is a 500 from the user's perspective.
6. **Tool handlers are async and return `str`.** Not `bytes`, not `dict`, not tuples. The orchestrator stuffs the return value straight into a `tool_result` block.
7. **`data/obsidian` and `data/filevault` are never chowned.** Rewriting file ownership breaks Obsidian Sync (re-uploads everything) and changes mtimes on historical attachments. `entrypoint.sh` only chowns `/data/yunam` (the SQLite DB).

### Adding a skill — checklist

Use this when adding in-process tools. Don't deviate.

1. **Name the skill and its scopes first, before writing code.** Each tool needs exactly one scope. If a new scope is needed (e.g. `calendar:read`), add it to [capabilities.py](gateway/yunam/capabilities.py).
2. **Create the primitive class** under `gateway/yunam/tools/<skill>.py` if implementing in-process. Pure async methods, no Claude schemas, no dispatch logic.
3. **Create the skill module** at `gateway/yunam/skills/<skill>.py` following [skills/obsidian.py](gateway/yunam/skills/obsidian.py):
   - `SKILL_ID`, `SKILL_VERSION` constants.
   - `SYSTEM_PROMPT_FRAGMENT` — a plain string constant explaining when to use these tools.
   - `_SCHEMAS` dict mapping tool name → Claude tool schema.
   - `build_<skill>_skill(tools_instance) -> Skill` factory wiring schemas → handlers → scopes.
4. **Export the factory** from [skills/\_\_init\_\_.py](gateway/yunam/skills/__init__.py).
5. **Register in [main.py](gateway/main.py)** — append to the end of the `SkillRegistry([...])` list. Never interleave with existing skills.
6. **Construct the tools instance** in [main.py](gateway/main.py) alongside the existing `ObsidianTools` / `AttachmentTools`, pass it to the factory.
7. **Verify**: `python -m py_compile` on every touched file; run the fake-Claude smoke test pattern from [scripts/repl.py](scripts/repl.py) with scripted tool_use blocks for each new tool; assert `tool_calls` rows show the correct `skill_id` and `scope`.
8. **Do NOT** touch [prompts.py](gateway/yunam/prompts.py) (core stays stable) or reorder existing skills.

### Adding an MCP server — checklist

Use this when integrating a third-party tool provider over the Model Context Protocol. Reference implementations: [mcp/gcal.py](gateway/yunam/mcp/gcal.py) (raw HTTP JSON-RPC over nspady streamable-http) and [mcp/stock.py](gateway/yunam/mcp/stock.py) (`mcp.ClientSession` over SSE).

1. **Declare scopes up front** for every MCP tool the server exposes. Do not infer from tool names. If the set is large, declare a single coarse scope (e.g. `notion:*`) and refine later.
2. **Create `gateway/yunam/mcp/<name>.py`** that:
   - Defines a `<Name>MCPClient` with `connect()` / `close()` / `call_tool()` / `tools` (cached, sorted by name for prompt-cache stability).
   - Discovers tools at `connect()` time and wraps each as a `ToolSpec` with an explicit scope from step 1.
   - Exposes `build_<name>_mcp_skill(client) -> Skill` — pure function, returns a frozen `Skill` dataclass the registry treats identically to an in-process skill. Do NOT subclass `Skill`; the dataclass contract is `id, version, tools: tuple[ToolSpec, ...], system_prompt_fragment: str`.
3. **Surface MCP failures as `VaultError`** so they render to Claude as tool errors, not 500s. Wrap the underlying transport's exceptions inside `call_tool()`.
4. **Connect failures are non-fatal at boot.** [main.py](gateway/main.py) wraps each MCP `connect()` in `try/except`, logs the failure, and skips the skill for that run. The gateway must stay alive without optional integrations. (Stock-Agent is required by some user flows but its absence still doesn't crash the gateway — the user just sees the model fall back to web search.)
5. **Register after existing skills** in [main.py](gateway/main.py). Include a system-prompt fragment explaining when to reach for MCP tools vs in-process alternatives.
6. **Verify**: start the MCP server locally, run a smoke test (`scripts/smoke_gcal.py` is a template), confirm `tool_calls` rows have the MCP skill's id and the declared scope.

#### Known MCP server quirks

- **nspady google-calendar-mcp** runs a single in-memory session per server process. After a gateway restart, the server still believes the previous session is open and rejects a fresh `initialize` with HTTP 400 + `"Server already initialized"`. `mcp/gcal.py:_is_already_initialized_error` detects that response and proceeds without re-handshaking; if that recovery path also fails, the README workaround is `docker restart yunam-calendar-mcp` before the gateway restart.
- **FastMCP SSE servers** (stock-mcp) close the SSE stream when the gateway disconnects. The `mcp.ClientSession` handles reconnect transparently; if you see `BrokenResourceError` in logs, the SSE endpoint host header probably doesn't match (FastMCP's `TransportSecuritySettings` rejects unknown hosts by default — allowlist the docker service name).

### Adding a sub-agent — checklist

Use this for specialist reasoning with its own system prompt, tool set, and budget.

1. **Design the boundary first.** What system prompt, what tools, what scope (sub-agents usually get a *different* set of scopes from Yunam proper — that's the point), what model, what budget caps.
2. **Create `gateway/yunam/subagents/<name>.py`** containing:
   - A dedicated `SYSTEM_PROMPT` for the sub-agent (NOT concatenated into Yunam's core prompt — sub-agents have their own context).
   - A tool set disjoint from Yunam's. The outer orchestrator never sees the sub-agent's tool_use/tool_result blocks.
   - A `run(query: str, context: DispatchContext) -> str` coroutine that opens its own `anthropic.messages.create` call with `max_iterations` and `max_tokens` caps.
   - A `build_<name>_subagent_skill() -> Skill` factory that packages `run()` as a single `ToolSpec` named `ask_<name>` with a new `Scope.SUBAGENT_<NAME>`.
3. **Default to a cheaper model** — `claude-haiku-4-5` or `claude-sonnet-4-6` — unless the task specifically requires Opus. Sub-agents run inside every Yunam turn that invokes them; Opus-on-Opus recursion gets expensive.
4. **Catch everything in `run()`**. Sub-agent failures must not crash the outer orchestrator. Return `"Sub-agent error: ..."` as the tool result.
5. **Register after existing skills.** Add a prompt fragment explaining when to delegate (and when not to).
6. **Verify**: mock the inner `messages.create` with a scripted response, run the outer orchestrator, assert the outer tool_call row has `skill_id=<subagent>` and `scope=subagent:<name>`, and that no inner tool activity leaked into the outer session's `messages` table.

### Scope before code

When extending Yunam, always answer these in this order, **before writing any code**:

1. What scopes does this capability need? (Pick from [capabilities.py](gateway/yunam/capabilities.py) or add new ones.)
2. Is this a skill, an MCP server, or a sub-agent?
3. Does it need any new columns in `sessions.py`? If so, what's the migration?

If you can't answer all three, you're not ready to write code. Scope is policy; if you ask Claude Code to choose scopes for you, you're delegating a policy decision to the model. Don't.

## Operational notes

These are recurring sharp edges that have bitten the deploy more than once. Read before debugging a "the bot stopped responding" report.

- **MCP `Server already initialized`** — nspady google-calendar-mcp keeps its session in memory; a gateway restart while calendar-mcp stays up leaves the server believing the previous session is open. `mcp/gcal.py:connect()` now recognizes and recovers from this; if recovery also fails (e.g. nspady changed the error wording), the workaround is `docker restart yunam-calendar-mcp` *before* the gateway restart. Same idea for stock-mcp on SSE: restart the sibling container if reconnect loops.
- **Two-bot race on the same token** — running `docker compose up` locally while the VPS container is also live makes both instances long-poll the same token. Telegram delivers each update to exactly one of them, so users see ~50% message loss. Stop one before starting the other.
- **`sqlite-vec` extension missing** — the memory and file-search skills load `sqlite-vec` as a SQLite extension. On stock macOS Python the extension may not load; in that case the relevant vec0 tables are skipped and KNN searches return empty. The Docker image bundles the extension so this only affects dev REPL on bare macOS.
- **Obsidian Sync re-sync risk** — `data/obsidian` and `data/filevault` are never chowned by entrypoint.sh. If you ever `chown -R` either tree manually, Obsidian Sync will retransmit every file. Don't.
- **`/data/obsidian` permissions** — vault writes fail with `EACCES` when `PUID`/`PGID` in `.env` don't match the host user that owns `~/obsidian`. Symptom: `vault_write` returns `Tool error: Permission denied`. Fix in `.env`, not at the FS level (see chown warning above).
- **Cache-miss spike after a skill add** — the SkillRegistry order is part of the cache key. Adding a skill in the middle of the list (instead of appending) reorders every following skill's fragment in the system prompt and invalidates the cache for every existing chat session. Always append; verify the new order in [main.py](gateway/main.py) before deploying.

## Next phases (see [milestone.md](milestone.md))

The next phase boundary is roadmapped in [milestone.md](milestone.md). All arrive through the governance layer above:

- **Phase 2.0 — Usage/Cost tracking skill** (`Scope.USAGE_READ`, new `api_usage` table, wrapper around every Anthropic / Voyage / Jina / Sweet Tracker / MCP call). Selected as the first Phase to make subsequent phases measurable.
- **Phase 2.1 — Curation pipeline** (Naver OpenAPI + Toss Invest + RSS + optional X via RSSHub sidecar). Background runner + new `curation` skill for in-conversation retrieval. Routes items as URGENT (immediate push), DIGEST (21:00 newsletter), or DROP.
- **Phase 2.2 — Reflection / Digital Twin memory** (draft-then-approve only; nightly reflector writes to `obsidian/inbox/reflections/`, never directly to `profile/*.md` — see Q4 in milestone.md for why auto-apply is forbidden).
- **Phase 2.3 — Finance guardrail sub-agent** wrapping the existing Stock-Agent MCP. Forced-reasoning by system-prompt structure (Opus 4.7 forbids prefilling). Mistake-ledger skill exposed in-conversation too.
- **Phase 2.4 — Hardening / eval / CLAUDE.md sync** — DB backup cron, regression fixtures, cost alarms, and a final pass on this file.

Out-of-scope until milestone.md unblocks them: a proper Obsidian Local-REST-API MCP adapter (would replace obsidiantools-based skill, enabling dataview), Slack control plane, push notifications outside Telegram.
