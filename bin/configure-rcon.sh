#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_DIR="$ROOT_DIR/secrets"
SECRET_FILE="$SECRET_DIR/rcon_password"

SERVERS=(
    "survival"
    "creative"
)

ROTATE=false

usage() {
    cat <<'EOF'
Usage:
  configure-rcon.sh
  configure-rcon.sh --rotate
  configure-rcon.sh --help

Options:
  --rotate    Generate a new RCON password before configuring the servers.
  --help      Show this help message.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --rotate)
            ROTATE=true
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $arg" >&2
            echo >&2
            usage >&2
            exit 1
            ;;
    esac
done

mkdir -p "$SECRET_DIR"

if [[ "$ROTATE" == true ]]; then
    echo "Rotating RCON password..."

    temporary_secret="$(mktemp "$SECRET_DIR/.rcon_password.XXXXXX")"
    chmod 600 "$temporary_secret"

    openssl rand -hex 32 > "$temporary_secret"

    mv -f "$temporary_secret" "$SECRET_FILE"

    echo "RCON password rotated."
elif [[ ! -f "$SECRET_FILE" ]]; then
    echo "Creating RCON password..."

    temporary_secret="$(mktemp "$SECRET_DIR/.rcon_password.XXXXXX")"
    chmod 600 "$temporary_secret"

    openssl rand -hex 32 > "$temporary_secret"

    mv -f "$temporary_secret" "$SECRET_FILE"

    echo "RCON password created."
else
    echo "Using existing RCON password."
fi

RCON_PASSWORD="$(<"$SECRET_FILE")"

if [[ -z "$RCON_PASSWORD" ]]; then
    echo "ERROR: RCON password file is empty: $SECRET_FILE" >&2
    exit 1
fi

if [[ ! "$RCON_PASSWORD" =~ ^[0-9a-fA-F]+$ ]]; then
    echo "ERROR: RCON password contains unexpected characters." >&2
    exit 1
fi

for mode in "${SERVERS[@]}"; do
    PROPERTIES_FILE="$ROOT_DIR/$mode/server.properties"

    if [[ ! -f "$PROPERTIES_FILE" ]]; then
        echo "ERROR: Missing $PROPERTIES_FILE" >&2
        exit 1
    fi

    echo "Configuring RCON for $mode..."

    if grep -q '^enable-rcon=' "$PROPERTIES_FILE"; then
        sed -i 's/^enable-rcon=.*/enable-rcon=true/' "$PROPERTIES_FILE"
    else
        printf '\nenable-rcon=true\n' >> "$PROPERTIES_FILE"
    fi

    if grep -q '^rcon.password=' "$PROPERTIES_FILE"; then
        sed -i "s|^rcon.password=.*$|rcon.password=$RCON_PASSWORD|" "$PROPERTIES_FILE"
    else
        printf 'rcon.password=%s\n' "$RCON_PASSWORD" >> "$PROPERTIES_FILE"
    fi

    if grep -q '^rcon.port=' "$PROPERTIES_FILE"; then
        sed -i 's/^rcon.port=.*/rcon.port=25575/' "$PROPERTIES_FILE"
    else
        printf 'rcon.port=25575\n' >> "$PROPERTIES_FILE"
    fi
done

unset RCON_PASSWORD

echo
echo "RCON configuration:"
for mode in "${SERVERS[@]}"; do
    PROPERTIES_FILE="$ROOT_DIR/$mode/server.properties"

    echo "===== $mode ====="
    grep -E '^(enable-rcon|rcon.password|rcon.port)=' "$PROPERTIES_FILE" |
        sed 's/^rcon.password=.*/rcon.password=<configured>/'
done

echo
echo "RCON configuration complete."