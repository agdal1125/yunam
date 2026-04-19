# Yunam

Personal AI assistant server running 24/7 on a Vultr Tokyo VPS, controlled via Telegram. This repo is the control plane.

For architecture decisions, secrets handling, and phase roadmap, see [CLAUDE.md](CLAUDE.md).

## Current phase: 0-7 — Telegram Echo Bot

Success criterion: send "hello" to `@AgentYunamBot` from phone → receive "hello" back from the VPS-hosted container.

## Structure

```
yunam/
├── .env.example          # Template — copy to .env and fill in
├── docker-compose.yml    # Runs the gateway service
└── gateway/
    ├── Dockerfile        # python:3.12-slim, non-root appuser
    ├── requirements.txt  # Pinned deps
    └── main.py           # Long-polling echo bot with user-ID allowlist
```

## Prerequisites

- Docker & Docker Compose
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot)

## Run it locally

```bash
cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID
docker compose up --build    # foreground; Ctrl+C to stop
```

Then from your phone, DM `@AgentYunamBot`:

- `/start` → replies `Yunam online`
- any text → echoes it back

Messages from any other Telegram user are silently ignored (logged at WARNING).

## Deploy to the VPS

Stop the local container first — two bots on the same token race on long-poll and each message lands on only one of them.

```bash
# On VPS (first time):
ssh yunam
git clone git@github.com:agdal1125/yunam.git ~/yunam
cd ~/yunam
cp .env.example .env
# Edit .env with the same values

# Start (detached):
docker compose up -d --build

# Check logs:
docker compose logs -f gateway

# Update later:
git pull && docker compose up -d --build
```

No inbound ports are exposed — the container makes outbound calls to Telegram's API only (long polling).

## Phase 0 checklist

- [x] 0-1 to 0-6: VPS provisioned, SSH hardened, Docker installed, VSCode Remote-SSH
- [ ] 0-7: echo bot working local + VPS (this phase)

## Roadmap

- **Phase 1**: LangGraph orchestrator, Obsidian vault, SQLite sessions, Claude API
- **Phase 2+**: Specialist agents (Finance Agent wraps the [MoneyFlow](../MoneyFlow) batch pipeline as a sibling Docker service)

## Security notes

- `.env` is gitignored. Never commit real tokens.
- Bot replies only to the user ID in `TELEGRAM_ALLOWED_USER_ID`. If that value is wrong or empty, the bot will ignore you — check the logs.
- If a token leaks, revoke it with `/revoke` via `@BotFather` and update `.env`.
