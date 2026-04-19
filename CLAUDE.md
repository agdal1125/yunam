# Yunam — Personal AI Agent

## Project Overview

Personal AI assistant server running 24/7 on a Vultr Tokyo VPS, controlled via Telegram (and later Slack). This repo is the control plane.

**Current phase**: Phase 1 — Agent core (LangGraph orchestrator + Claude Opus 4.7 + SQLite sessions + Obsidian vault tools). Success criterion: from Telegram, `/start` → Yunam responds; "write a note called test.md with hello" → `~/obsidian/test.md` appears; "read test.md" → Yunam replies with "hello".

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
- [ ] **Phase 1 (current)**: Agent core — Claude Opus 4.7 + LangGraph + SQLite sessions + Obsidian vault (read + write)

## Target Project Structure

```
yunam/
├── .gitignore
├── .env.example          # Committed; shows required env vars with placeholders
├── .env                  # NEVER committed; contains real secrets
├── README.md
├── CLAUDE.md             # This file
├── docker-compose.yml
├── data/                 # Bind-mount target for SQLite DB (gitignored)
│   └── yunam/yunam.db
├── scripts/
│   └── repl.py           # Local dev REPL w/ fake Claude (no token burn)
└── gateway/
    ├── Dockerfile        # + gosu for PUID/PGID drop-privileges pattern
    ├── entrypoint.sh     # Remaps appuser uid/gid at runtime, then `exec gosu appuser`
    ├── requirements.txt  # PTB + anthropic + langgraph + aiosqlite
    ├── main.py           # Telegram gateway, manual PTB lifecycle
    └── yunam/            # Package: orchestrator + tools + sessions
        ├── config.py     # env loading, logging setup
        ├── prompts.py    # SYSTEM_PROMPT (cached prefix — never interpolate)
        ├── orchestrator.py  # LangGraph: load_history → agent_step → persist
        ├── sessions.py   # aiosqlite: sessions, messages, tool_calls
        └── tools/
            ├── vault.py     # safe_join, size caps, atomic write
            └── obsidian.py  # 4 async tool fns + TOOL_SCHEMAS + dispatch
```

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

Optional (have working defaults):

| Name | Default | Purpose |
|---|---|---|
| `YUNAM_VAULT_PATH_HOST` | `~/obsidian` | Host path to Obsidian vault (bind-mounted into `/data/obsidian`) |
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
```

Version compatibility brackets (not exact pins) for Phase 1 — `anthropic` and `langgraph` evolve fast, and Opus 4.7 needs recent SDK support. Docker build pins the resolved versions for reproducibility in practice.

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
- Log unauthorized access attempts at WARNING level (user_id only, never message content in prod — but for Phase 0 it's fine to log content for debugging)

## Phase 1 Architecture

### Orchestrator shape

LangGraph: `START → load_history → agent_step → persist → END`. Three nodes, linear, no conditional edges. The Claude + tool loop lives **inside** `agent_step` — splitting call/tool into separate LangGraph nodes buys nothing for Phase 1 and complicates prompt-caching audits. See [gateway/yunam/orchestrator.py](gateway/yunam/orchestrator.py).

### Claude request shape (Opus 4.7)

```python
response = await client.messages.create(
    model="claude-opus-4-7",
    max_tokens=16000,
    system=[{"type": "text", "text": SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}],
    thinking={"type": "adaptive"},       # only valid mode on 4.7
    output_config={"effort": "high"},
    tools=TOOL_SCHEMAS,
    messages=messages,
)
```

Forbidden on 4.7 (all 400 errors): `temperature`, `top_p`, `top_k`, `budget_tokens`, assistant prefilling.

### Prompt caching invariants

- `SYSTEM_PROMPT` in [gateway/yunam/prompts.py](gateway/yunam/prompts.py) is a plain module constant. **Never interpolate** dates, chat_id, or per-request data into it — a single byte change invalidates the cache.
- `TOOL_SCHEMAS` in [gateway/yunam/tools/obsidian.py](gateway/yunam/tools/obsidian.py) is a list literal with stable order. Never build from a dict/set.
- Per-turn context (e.g. "today is X") goes into user messages, not system.

### Tool surface

Four Obsidian tools with a raw-schema + dispatch pattern (not `@beta_tool`). Path safety via `Path.resolve().is_relative_to(VAULT_ROOT)` — the only barrier between the model and the host FS. Writes are atomic (tempfile + rename). Size caps: 1 MB read, 500 KB write. See [gateway/yunam/tools/vault.py](gateway/yunam/tools/vault.py).

### Storage

- SQLite at `/data/yunam/yunam.db` (bind-mounted to host `./data/yunam/yunam.db`).
- Three tables: `sessions`, `messages` (plain text only — no thinking/tool_use blocks), `tool_calls` (brief log).
- `load_history` returns last 20 messages as `[{role, content}]` — exactly what Claude expects.
- Schema in [gateway/yunam/sessions.py](gateway/yunam/sessions.py); created idempotently on startup with `IF NOT EXISTS`.

### Non-root + PUID/PGID

Container starts as root, [gateway/entrypoint.sh](gateway/entrypoint.sh) remaps `appuser`'s uid/gid to match `PUID`/`PGID`, chowns `/data/yunam`, then drops to `appuser` via `gosu`. On Ubuntu VPS the default `PUID=1000` matches `jaekeun` — no action needed. On macOS, set `PUID=$(id -u)` in `.env`. The vault directory (`/data/obsidian`) is **never chowned** — could trigger a full Obsidian Sync re-sync.

### Local testing without token burn

[scripts/repl.py](scripts/repl.py) runs the full orchestrator with a fake Claude that recognizes trigger words (`write`, `read`, `escape`) to exercise the tool loop. Real `--real` mode hits Anthropic for end-to-end verification.

## Next Phases (context, not current work)

- **Phase 2+**: Specialist agents (investment, lifehack), skills system, evals
  - **Finance Agent**: wraps the MoneyFlow batch pipeline (separate repo at `~/Desktop/MoneyFlow`). Integration pattern: MoneyFlow runs as its own Docker service exposing `moneyflow-api`; Yunam's Finance Agent calls it as a sibling container over HTTP. Keeps batch pipeline state isolated from the control plane.
