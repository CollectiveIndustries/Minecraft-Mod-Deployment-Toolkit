#!/bin/bash
set -euo pipefail

ROOT="/home/admiral/minecraft"
SYNC="$ROOT/sync"
LIVE="$ROOT/server"


EXCLUDE="$SYNC/.rsync_exclude"

echo "[1/4] Ensuring server is stopped..."
docker compose stop minecraft

echo "[2/4] Promoting mods..."
rsync -av --delete --exclude-from="$EXCLUDE" "$SYNC/mods/" "$LIVE/mods/"

echo "[3/4] Promoting configs..."
rsync -av --delete --exclude-from="$EXCLUDE" "$SYNC/config/" "$LIVE/config/"

echo "[4/4] Syncing KubeJS Scripts..."
rsync -av --delete --exclude-from="$EXCLUDE" "$SYNC/kubejs/" "$LIVE/kubejs/"

echo "Starting server..."
docker compose start minecraft
