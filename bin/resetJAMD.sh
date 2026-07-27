#!/bin/bash
set -euo pipefail

ROOT="/srv/minecraft"
SYNC="$ROOT/sync"
LIVE="$ROOT/server"

EXCLUDE="$SYNC/.rsync_exclude"

MC_CONTAINER="mc"
RESTART_DELAY=30

#echo "[0/5] Sending restart warning to in-game chat..."
#docker exec "$MC_CONTAINER" rcon-cli \
#  'say §c[SERVER] Restarting in 30 seconds for maintenance.'
#docker exec "$MC_CONTAINER" rcon-cli \
#  'say §c[SERVER] Please allow up to 2-5 minutes for the world to start.'

#echo "Waiting ${RESTART_DELAY} seconds..."
#sleep "$RESTART_DELAY"

echo "[1/5] Ensuring server is stopped..."
docker compose down

echo "[3/5] Removing JAMD mining dimensions..."
rm -rfv "$LIVE/world/dimensions/jamd/"

echo "[4/5] Promoting mods..."
rsync -av --delete --exclude-from="$EXCLUDE" "$SYNC/mods/" "$LIVE/mods/"

echo "[5/5] Promoting configs..."
rsync -av --delete --exclude-from="$EXCLUDE" "$SYNC/config/" "$LIVE/config/"

echo "Starting server..."
docker compose up -d
