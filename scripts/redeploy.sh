#!/usr/bin/env bash
# Clean redeploy that survives nspady's stateful-session bug.
#
# Why not just `docker compose up -d --build`?
#   - Compose rebuilds the gateway, starts a fresh container, but leaves
#     calendar-mcp running with its previous in-memory session.
#   - The new gateway can't `initialize` (nspady returns "already initialized")
#     and the recovery path also fails (no mcp-session-id in the error response).
#   - Result: gcal skill is silently disabled until you manually restart
#     calendar-mcp.
#
# `down` stops every service including calendar-mcp, clearing nspady's
# session. `up -d --build` brings everything back fresh. Adds ~10s vs
# `restart`, but always works.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "[redeploy] stopping all services..."
docker compose down

echo "[redeploy] rebuilding gateway + bringing services up..."
docker compose up -d --build

echo "[redeploy] waiting for gateway to be running..."
for i in $(seq 1 30); do
  if docker compose ps gateway --format '{{.Status}}' | grep -q "^Up"; then
    echo "[redeploy] gateway is up"
    break
  fi
  sleep 1
done

echo "[redeploy] tailing gateway logs for gcal/stock connect lines..."
sleep 5
docker logs yunam-gateway 2>&1 | grep -E "(gcal|stock) MCP (connected|connect failed)" | tail -5 || true

echo "[redeploy] done"
