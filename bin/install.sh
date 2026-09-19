#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$PROJECT_DIR"

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

echo "==> Minecraft Docker installation"
echo "    Project: $PROJECT_DIR"
echo "    UID:GID: ${HOST_UID}:${HOST_GID}"
echo

# ==============================================================
# REQUIREMENTS
# ==============================================================

if [[ "$HOST_UID" != "1001" || "$HOST_GID" != "1001" ]]; then
    echo "ERROR: expected UID:GID 1001:1001"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed."
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose is not available."
    exit 1
fi

# ==============================================================
# REMOVE ROOT-OWNED PATHS CREATED BY THE OLD BROKEN COMPOSE FILE
#
# Docker daemon performs this as container root.
# No host sudo is required.
# ==============================================================

echo "==> Repairing host-managed ownership"

docker run --rm \
    --user 0:0 \
    --volume "$PROJECT_DIR:/repair" \
    nginx:stable \
    sh -c '
        for path in \
            /repair/config.d \
            /repair/mods \
            /repair/velocity \
            /repair/survival \
            /repair/creative \
            /repair/bluemap \
            /repair/www \
            /repair/logs
        do
            if [ -e "$path" ]; then
                chown -R 1001:1001 "$path"
            fi
        done

        rm -rf \
            /repair/exports.conf \
            /repair/nginx.conf
    '

# ==============================================================
# HOST-MANAGED DIRECTORIES
#
# Only top-level application mount points are created here.
# Minecraft creates everything below world/.
# ==============================================================

echo "==> Creating required host directories"

mkdir -p \
    ./mods \
    ./velocity \
    ./survival/config \
    ./survival/kubejs \
    ./survival/world \
    ./creative/config \
    ./creative/kubejs \
    ./creative/world \
    ./bluemap/config \
    ./logs/survival \
    ./logs/creative \
    ./logs/velocity \
    ./logs/bluemap \
    ./logs/nginx \
    ./logs/pdm \
    ./www

# ==============================================================
# SERVER.PROPERTIES
#
# The server owns the contents.
# The installer only creates the bind-mount target.
# ==============================================================

echo "==> Creating server.properties placeholders"

touch \
    ./survival/server.properties \
    ./creative/server.properties

# ==============================================================
# REQUIRED INFRASTRUCTURE CONFIG
# ==============================================================

echo "==> Checking required configuration"

required_files=(
    "./docker-compose.yml"
    "./config.d/exports.conf"
    "./config.d/nginx.conf"
)

for file in "${required_files[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo "ERROR: required file is missing: $file"
        exit 1
    fi
done

# ==============================================================
# NFS HOST PORT
#
# Host rpcbind must already be stopped/disabled.
# We do not manipulate systemd from this installer.
# ==============================================================

if ss -ltnup 2>/dev/null | grep -Eq '(^|[[:space:]])(0\.0\.0\.0:|\[::\]:)?111([[:space:]]|$)'; then
    echo "ERROR: host port 111 is occupied."
    ss -ltnup 2>/dev/null | grep '111' || true
    exit 1
fi

echo "==> Port 111 is available"

# ==============================================================
# COMPOSE VALIDATION
# ==============================================================

echo "==> Validating Docker Compose"

docker compose config -q

# ==============================================================
# BLUEMAP DOCKER VOLUMES
#
# BlueMap runs as 1001:1001.
#
# The named volumes are initialized by a temporary root process
# inside the BlueMap container, rather than by host sudo/chown.
# ==============================================================

echo "==> Initializing BlueMap Docker volumes"

docker compose run \
    --rm \
    --no-deps \
    --user 0:0 \
    --entrypoint /bin/sh \
    bluemap \
    -c 'chown -R 1001:1001 /app/data /app/web'

# ==============================================================
# START
# ==============================================================

echo
echo "==> Starting Minecraft stack"

docker compose up -d

echo
echo "==> Installation complete"
echo

docker compose ps