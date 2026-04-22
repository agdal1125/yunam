# Yunam — Personal AI Agent

## Project Overview

Personal AI assistant server running 24/7 on a Vultr Tokyo VPS, controlled via Telegram (and later Slack). This repo is the control plane.

**Current phase**: Phase 1 agent core has shipped (LangGraph orchestrator + Claude Opus 4.7 + SQLite sessions + Obsidian vault tools), plus the Phase 1.5 extensions that followed: binary attachments with Voyage multimodal embeddings + semantic search, a daily-retrospective scheduler, and a **skill/scope governance layer** that formalizes how tools are bundled, authorized, and audited. Current focus is readiness for Phase 2 (specialist agents / MCP integration); new capabilities should be added through the governance layer described below, not by extending the orchestrator directly.

## Owner & Environment

- **Owner**: jaekeun (GitHub: `agdal1125`)
- **Repo**: `git@github.com:agdal1125/yunam.git` (private)
- **VPS**: Vultr Tokyo, Ubuntu 24.04, vc2-2c-2gb
- **SSH alias**: `yunam` (user: `jaekeun`) — already configured in `~/.ssh/config`
- **Telegram bot**: `@AgentYunamBot`
- **Local dev machine**: macOS

## Progress

- [x] Phase 0-1 to 0-6: VPS provisioned, SSH hardened, Docker installed, VSCode Remote-SSH connected
- [x] **Phase 0-7**: Telegram Echo Bot running locally → deployed to VPS
- [x] **Phase 1**: Agent core — Claude Opus 4.7 + LangGraph + SQLite sessions + Obsidian vault (read + write)
- [x] **Phase 1.5**: Binary attachments (filevault + Voyage multimodal embeddings + semantic file search), daily retrospective scheduler, skill/scope governance layer with idempotent schema migrations
- [ ] **Phase 2 (next)**: Specialist agents and/or MCP integrations wired through the governance layer. No in-process skills, sub-agents, or MCP servers shipped yet — the harness is ready for them.

## Project Structure

```
yunam/
├── .gitignore
├── .env.example              # Committed; shows required env vars with placeholders
├── .env                      # NEVER committed; contains real secrets
├── README.md
├── CLAUDE.md                 # This file — project instructions, auto-loaded by Claude Code
├── docker-compose.yml
├── data/                     # Bind-mount target for SQLite DB (gitignored)
│   └── yunam/yunam.db
├── scripts/
│   └── repl.py               # Local dev REPL w/ fake Claude (no token burn)
└── gateway/
    ├── Dockerfile            # + gosu for PUID/PGID drop-privileges pattern
    ├── entrypoint.sh         # Remaps appuser uid/gid at runtime, then `exec gosu appuser`
    ├── requirements.txt      # PTB + anthropic + langgraph + aiosqlite + voyageai + sqlite-vec + Pillow
    ├── main.py               # Telegram gateway, manual PTB lifecycle, SkillRegistry wiring
    └── yunam/                # Package
        ├── config.py         # env loading, logging setup
        ├── prompts.py        # Core SYSTEM_PROMPT only — skill guidance lives in each skill module
        ├── orchestrator.py   # LangGraph: load_history → agent_step → persist; consumes SkillRegistry
        ├── sessions.py       # aiosqlite: sessions/messages/tool_calls/pending_attachments/saved_files/file_embeddings; PRAGMA user_version migrations
        ├── capabilities.py   # Scope enum — vocabulary for tool authorization
        ├── embeddings.py     # Voyage multimodal client (embed_query / embed_document)
        ├── files.py          # Filevault path safety + name sanitization (analog of tools/vault.py for binaries)
        ├── scheduler.py      # Daily retrospective cron — proactive prompts at a fixed local time
        ├── sender.py         # AttachmentSender Protocol + PTBSender (lets AttachmentTools be unit-tested without Telegram)
        ├── skills/           # Governance layer — where new capabilities are added
        │   ├── base.py       # Skill, ToolSpec, DispatchContext, SkillRegistry dataclasses
        │   ├── __init__.py   # Public exports: build_obsidian_skill, build_files_skill, ...
        │   ├── obsidian.py   # Obsidian vault skill (schemas + scopes + prompt fragment + handlers → ObsidianTools)
        │   └── files.py      # Filevault skill (schemas + scopes + prompt fragment + handlers → AttachmentTools)
        └── tools/            # Low-level primitives — no model/schema awareness
            ├── vault.py      # safe_join, size caps, atomic write (used by Obsidian + breadcrumbs)
            ├── obsidian.py   # ObsidianTools class: async vault_read / vault_write / vault_list / vault_search
            └── attachments.py # AttachmentTools class: save_attachment / search_files / retrieve_attachment / commit_pending
```

**Layering discipline:** `tools/` modules are pure primitives (no Claude schemas, no dispatch). Schemas, scopes, prompt fragments, and dispatch live in `skills/` — that's the only layer the orchestrator talks to. `capabilities.py` is the scope vocabulary that sits between them. Keep the dependency direction `skills/ → tools/ → (stdlib/external)` one-way.

## Design Decisions

### Long polling, not webhook
The bot uses `python-telegram-bot` long polling. The container makes outbound calls to Telegram's API and receives updates on the same connection — **no inbound ports need to be exposed** on the VPS. This is intentional for Phase 0: zero attack surface. Webhook + Caddy + Cloudflare Tunnel is deferred to a later phase.

### User ID allowlist
The bot MUST filter by `TELEGRAM_ALLOWED_USER_ID` and ignore messages from any other user. Even though `@AgentYunamBot` is a new bot with no followers, the bot username is discoverable, so anyone could `/start` it. The allowlist is the only thing preventing strangers from interacting with the agent (which will later have API access, vault write access, etc.).

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
| `TELEGRAM_ALLOWED_USER_ID` | Numeric user ID of jaekeun; only this user's messages are processed | `@userinfobot` on Telegram |
| `ANTHROPIC_API_KEY` | Claude API auth | `console.anthropic.com` |
| `VOYAGE_API_KEY` | Voyage multimodal embeddings for `search_files` / saved-file indexing | `dash.voyageai.com` |

Optional (have working defaults):

| Name | Default | Purpose |
|---|---|---|
| `YUNAM_VAULT_PATH_HOST` | `~/obsidian` | Host path to Obsidian vault (bind-mounted into `/data/obsidian`) |
| `YUNAM_FILEVAULT_PATH_HOST` | `~/filevault` | Host path to binary-attachment store (bind-mounted into `/data/filevault`) |
| `YUNAM_SCHEDULE_ENABLED` | unset (disabled) | Set to any truthy value to enable the daily retrospective scheduler |
| `YUNAM_DAILY_REFLECTION_TIME` | `22:30` | Local time (HH:MM) when the retrospective prompt fires |
| `YUNAM_TIMEZONE` | `Asia/Seoul` | IANA tz name used by the scheduler and the per-turn `[meta: now is ...]` tag |
| `PUID` | `1000` | Host UID that owns files written by the container (set to `id -u` on mac) |
| `PGID` | `1000` | Host GID (set to `id -g` on mac) |

## Dependencies

```
python-telegram-bot==21.6
python-dotenv==1.0.1
anthropic>=0.45.0,<1.0.0
langgraph>=0.2.50,<0.3.0
langchain-core>=0.3.0,<0.4.0
aiosqlite>=0.20.0,<0.22.0
voyageai>=0.3.0,<1.0.0          # multimodal embeddings for saved-file search
sqlite-vec>=0.1.0,<0.2.0        # vec0 virtual table loaded into the session DB
Pillow>=10.0.0,<12.0.0           # image I/O for attachment embedding
```

Version compatibility brackets (not exact pins) — `anthropic` and `langgraph` evolve fast, Opus 4.7 needs recent SDK support, and `sqlite-vec` is young (monitor 0.x releases). Docker build pins the resolved versions for reproducibility in practice.

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

Tools are bundled into **skills**, each of which owns its schemas, scopes, prompt fragment, and handlers. Today two skills ship:

| Skill | Tools | Scopes |
|---|---|---|
| `obsidian` ([skills/obsidian.py](gateway/yunam/skills/obsidian.py)) | `vault_read`, `vault_write`, `vault_list`, `vault_search` | `vault:read`, `vault:write` |
| `files` ([skills/files.py](gateway/yunam/skills/files.py)) | `save_attachment`, `search_files`, `retrieve_attachment` | `filevault:read`, `filevault:write`, `filevault:send` |

Path safety for the Obsidian vault is `Path.resolve().is_relative_to(VAULT_ROOT)` in [tools/vault.py](gateway/yunam/tools/vault.py) — the only barrier between the model and the host FS. Writes are atomic (tempfile + rename). Size caps: 1 MB read, 500 KB write. The filevault gets its own analogous path-safety module at [gateway/yunam/files.py](gateway/yunam/files.py).

### Storage

- SQLite at `/data/yunam/yunam.db` (bind-mounted to host `./data/yunam/yunam.db`). WAL mode, `foreign_keys = ON`.
- Six tables + one virtual table:
  - `sessions` — one row per chat_id (created_at, last_seen_at).
  - `messages` — plain-text history only (no thinking/tool_use blocks). Indexed on `(chat_id, created_at)`.
  - `tool_calls` — brief per-call audit: name, input_json, result_preview, is_error, elapsed_ms, **skill_id**, **scope**. The last two are governance bookkeeping, populated by the orchestrator for every dispatch.
  - `pending_attachments` — Telegram file_ids received but not yet committed (download is deferred until `/save` or the `save_attachment` tool fires).
  - `saved_files` — filevault-committed attachments with relpath, metadata, caption, description.
  - `file_embeddings` (virtual, `vec0`) — co-located 1024-dim Voyage embeddings for KNN file search. Loaded via `sqlite-vec`; fails gracefully and disables semantic search if the extension can't load (e.g. stock macOS Python).
- Schema evolves via **idempotent migrations** guarded by `PRAGMA user_version` in [sessions.py](gateway/yunam/sessions.py): `DB_USER_VERSION` constant, column-exists checks around `ALTER TABLE`, no destructive ops. Bump the constant and add a versioned step when introducing new columns.
- `load_history` returns the last 20 messages as `[{role, content}]` — exactly what Claude expects.

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

Use this when integrating a third-party tool provider over the Model Context Protocol.

1. **Declare scopes up front** for every MCP tool the server exposes. Do not infer from tool names. If the set is large, declare a single coarse scope (`notion:*`) and refine later.
2. **Create `gateway/yunam/mcp/<name>.py`** that:
   - Connects to the server at orchestrator init (via `anthropic.AsyncAnthropic` MCP client or equivalent). Fail fast on unreachable servers at startup.
   - Discovers tools and wraps each as a `ToolSpec` with an explicit scope from step 1. Keep discovery deterministic (sort by name) so the flattened schema order is stable.
   - Exposes `build_<name>_mcp_skill() -> Skill` returning a `Skill` the registry treats identically to an in-process skill.
3. **Surface MCP failures as `VaultError`** so they render to Claude as tool errors, not 500s.
4. **Add a circuit-breaker wrapper** (deferred until the second MCP server arrives — ok to skip on the first integration).
5. **Register after existing skills** in [main.py](gateway/main.py). Include a system-prompt fragment explaining when to reach for MCP tools vs in-process alternatives.
6. **Verify**: start the MCP server locally, run the fake-Claude REPL with real dispatch, confirm tool_calls rows have the MCP skill's id and the declared scope.

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

## Next Phases (context, not current work)

- **Phase 2+**: Specialist agents (investment, lifehack), MCP integrations, evals. All arrive through the governance layer above.
  - **Finance Agent**: wraps the MoneyFlow batch pipeline (separate repo at `~/Desktop/MoneyFlow`). Integration pattern: MoneyFlow runs as its own Docker service exposing `moneyflow-api` (ideally as an MCP server, not custom REST); Yunam calls it as a sibling container. Keeps batch pipeline state isolated from the control plane. Implemented as a sub-agent skill per the checklist above.
  - **Obsidian proper integration**: optional — a future MCP adapter around Obsidian's Local REST API plugin would enable tag/backlink/dataview features that pure filesystem tools can't reach. Today the "Obsidian" in Yunam is just a directory of `.md` files; cross-device sync is the Obsidian desktop app + Obsidian Sync, not Yunam talking to Obsidian.
