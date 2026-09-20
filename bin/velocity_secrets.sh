#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

VELOCITY_SECRET="${REPO_ROOT}/velocity/forwarding.secret"
SURVIVAL_PCF="${REPO_ROOT}/survival/config/proxy-compatible-forge.toml"
CREATIVE_PCF="${REPO_ROOT}/creative/config/proxy-compatible-forge.toml"

require_command() {
    local command_name="$1"

    if ! command -v "${command_name}" >/dev/null 2>&1; then
        printf 'ERROR: required command not found: %s\n' "${command_name}" >&2
        exit 1
    fi
}

write_atomic() {
    local target="$1"
    local temporary

    temporary="$(mktemp "${target}.XXXXXX")"

    cleanup_temporary() {
        rm -f -- "${temporary}"
    }

    trap cleanup_temporary RETURN

    cat > "${temporary}"
    chmod 600 "${temporary}"
    mv -- "${temporary}" "${target}"

    trap - RETURN
}

update_pcf_config() {
    local target="$1"
    local secret="$2"
    local temporary

    mkdir -p -- "$(dirname -- "${target}")"

    if [[ ! -f "${target}" ]]; then
        temporary="$(mktemp "${target}.XXXXXX")"

        cat > "${temporary}" <<EOF
[forwarding]
        #Enable or disable player info forwarding. Changing this setting requires a server restart.
        enabled = true
        #The type of forwarding to use
        #Allowed Values: LEGACY, BUNGEEGUARD, MODERN
        mode = "MODERN"
        #The forwarding secret shared with the proxy
        secret = "${secret}"
        #A list of approved proxy hostnames or IP addresses. If the connecting proxy's hostname or IP isn't in this list, the player will be disconnected. Leave empty to allow all.
        approvedProxyHosts = []

#CrossStitch Settings - For Wrapping Modded Command Arguments
[crossStitch]
        enabled = true
        forceWrappedArguments = []
        forceWrapVanillaArguments = false

#Debug Settings
[debug]
        enabled = false
        disabledMixins = []

#Advanced Settings
[advanced]
        modernForwardingVersion = "NO_OVERRIDE"
EOF

        chmod 600 "${temporary}"
        mv -- "${temporary}" "${target}"
        return
    fi

    temporary="$(mktemp "${target}.XXXXXX")"

    awk -v secret="${secret}" '
        BEGIN {
            in_forwarding = 0
            found_secret = 0
        }

        /^\[forwarding\][[:space:]]*$/ {
            in_forwarding = 1
            print
            next
        }

        in_forwarding && /^\[[^]]+\][[:space:]]*$/ {
            in_forwarding = 0
        }

        in_forwarding && /^[[:space:]]*secret[[:space:]]*=/ {
            print "        secret = \"" secret "\""
            found_secret = 1
            next
        }

        {
            print
        }

        END {
            if (!found_secret) {
                exit 17
            }
        }
    ' "${target}" > "${temporary}" || {
        rm -f -- "${temporary}"
        printf 'ERROR: could not locate [forwarding].secret in %s\n' "${target}" >&2
        exit 1
    }

    chmod 600 "${temporary}"
    mv -- "${temporary}" "${target}"
}

read_pcf_secret() {
    local target="$1"

    awk '
        /^\[forwarding\][[:space:]]*$/ {
            in_forwarding = 1
            next
        }

        in_forwarding && /^\[[^]]+\][[:space:]]*$/ {
            in_forwarding = 0
        }

        in_forwarding && /^[[:space:]]*secret[[:space:]]*=/ {
            value = $0
            sub(/^[[:space:]]*secret[[:space:]]*=[[:space:]]*"/, "", value)
            sub(/".*$/, "", value)
            print value
            exit
        }
    ' "${target}"
}

require_command openssl
require_command awk
require_command mktemp

mkdir -p -- \
    "${REPO_ROOT}/velocity" \
    "${REPO_ROOT}/survival/config" \
    "${REPO_ROOT}/creative/config"

printf '%s\n' '=== Rotating Velocity / PCF forwarding secret ==='

SECRET="$(openssl rand -hex 32)"

if [[ "${#SECRET}" -ne 64 ]]; then
    printf 'ERROR: openssl generated an unexpected secret length\n' >&2
    exit 1
fi

printf '%s\n' 'Generated fresh 256-bit secret.'

write_atomic "${VELOCITY_SECRET}" <<EOF
${SECRET}
EOF

update_pcf_config "${SURVIVAL_PCF}" "${SECRET}"
update_pcf_config "${CREATIVE_PCF}" "${SECRET}"

printf '%s\n' 'Installed fresh secret:'
printf '  %s\n' "${VELOCITY_SECRET}"
printf '  %s\n' "${SURVIVAL_PCF}"
printf '  %s\n' "${CREATIVE_PCF}"

VELOCITY_VALUE="$(<"${VELOCITY_SECRET}")"
SURVIVAL_VALUE="$(read_pcf_secret "${SURVIVAL_PCF}")"
CREATIVE_VALUE="$(read_pcf_secret "${CREATIVE_PCF}")"

if [[ "${VELOCITY_VALUE}" != "${SECRET}" ]]; then
    printf 'ERROR: Velocity secret verification failed\n' >&2
    exit 1
fi

if [[ "${SURVIVAL_VALUE}" != "${SECRET}" ]]; then
    printf 'ERROR: Survival PCF secret verification failed\n' >&2
    exit 1
fi

if [[ "${CREATIVE_VALUE}" != "${SECRET}" ]]; then
    printf 'ERROR: Creative PCF secret verification failed\n' >&2
    exit 1
fi

printf '%s\n' 'PASS: all three forwarding secrets contain the new secret.'
printf '%s\n' 'Restart Velocity, Survival, and Creative to load the rotated credential.'
