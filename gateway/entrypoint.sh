#!/bin/sh
# Yunam gateway entrypoint.
#
# Runs briefly as root:
#   1. Remaps appuser's uid/gid to PUID/PGID if different (matches host ownership
#      on bind-mounted volumes — needed when the container's default uid 1000
#      doesn't match the host user, e.g. on macOS where the user is uid 501).
#   2. Ensures /data/yunam exists and is owned by appuser so SQLite can write.
#   3. DOES NOT chown /data/obsidian — that's the user's vault and chowning it
#      could trigger a full Obsidian Sync re-sync.
#   4. Drops to appuser via gosu before executing the app command.

set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

current_uid=$(id -u appuser)
current_gid=$(id -g appuser)

if [ "$PGID" != "$current_gid" ]; then
    groupmod -o -g "$PGID" appuser
fi
if [ "$PUID" != "$current_uid" ]; then
    usermod -o -u "$PUID" appuser
fi

# SQLite lives here; we own this dir, safe to chown.
mkdir -p /data/yunam
chown -R appuser:appuser /data/yunam

# /app is where the code lives; chown is cheap and keeps things consistent across
# uid remaps.
chown -R appuser:appuser /app

exec gosu appuser "$@"
