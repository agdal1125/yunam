# Yunam — Personal AI Agent

## Project Overview

Personal AI assistant server running 24/7 on a Vultr Tokyo VPS, controlled via Telegram (and later Slack). This repo is the control plane.

**Current phase**: Phase 0-7 — Telegram Echo Bot (Hello World). Success criterion: send "hello" to the Telegram bot from phone, receive "hello" back from the VPS.

## Owner & Environment

- **Owner**: jaekeun (GitHub: `agdal1125`)
- **Repo**: `git@github.com:agdal1125/yunam.git` (private)
- **VPS**: Vultr Tokyo, Ubuntu 24.04, vc2-2c-2gb
- **SSH alias**: `yunam` (user: `jaekeun`) — already configured in `~/.ssh/config`
- **Telegram bot**: `@AgentYunamBot`
- **Local dev machine**: macOS

## Phase 0 Progress

- [x] 0-1 to 0-6 complete: VPS provisioned, SSH hardened, Docker installed, VSCode Remote-SSH connected
- [ ] **0-7 (current)**: Telegram Echo Bot running locally → deployed to VPS

## Target Project Structure

```
yunam/
├── .gitignore
├── .env.example          # Committed; shows required env vars with placeholders
├── .env                  # NEVER committed; contains real secrets
├── README.md
├── CLAUDE.md             # This file
├── docker-compose.yml
└── gateway/
    ├── Dockerfile
    ├── requirements.txt
    └── main.py           # Telegram long-polling echo bot
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

## Dependencies (pinned)

```
python-telegram-bot==21.6
python-dotenv==1.0.1
```

Pinned versions are intentional — reproducibility matters more than latest for this infra.

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

## Next Phases (context, not current work)

- **Phase 1**: LangGraph orchestrator + Obsidian vault + SQLite sessions + Claude API
- **Phase 2+**: Specialist agents (investment, lifehack), skills system, evals
  - **Finance Agent**: wraps the MoneyFlow batch pipeline (separate repo at `~/Desktop/MoneyFlow`). Integration pattern: MoneyFlow runs as its own Docker service exposing `moneyflow-api`; Yunam's Finance Agent calls it as a sibling container over HTTP. Keeps batch pipeline state isolated from the control plane.
