# src/minecraft/deploy_pack/daemon/config.py

"""Daemon configuration for the Minecraft deploy daemon.

Reads the ``[daemon]`` table from ``config.d/deploy_pack.toml``. Missing
keys fall back to defaults. Secret material (the bearer token) is NOT
read here; the HTTP plugin reads it from an environment variable named
by ``bearer_token_env`` when it constructs the authenticator.

Slice 12 additions
------------------
``trusted_keys_path`` and ``registry_path`` are read by
:mod:`minecraft.deploy_pack.daemon.verify` to perform chain-walk and
bundle-signature verification (Upgrade_System_specifications Section 7.1-7.5,
Section 12.4 steps 3-4). ``journal_path`` and ``world_backup_root`` are
reserved for the deployment orchestration slice (Section 12.4 steps 11-19).

Defaults match the DEV-side ``publish`` command so a shared checkout
finds the same files: ``trusted_keys_path`` defaults to
``config.d/trusted_keys.toml`` (config_dir-relative),
``registry_path`` defaults to ``sync/packing_slips/release-index.jsonl``
(project-root-relative).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MinecraftDaemonConfig", "load_config"]


_DEFAULTS: dict[str, str] = {
    "state_dir": "/var/lib/minecraft-deploy",
    "slip_dir": "sync/packing_slips",
    "release_store": "/srv/minecraft/releases",
    "staging_root": "/srv/minecraft/staging",
    "http_host": "127.0.0.1",
    "http_port": "8765",
    "bearer_token_env": "MINECRAFT_DAEMON_TOKEN",
    "tailscale_cidrs": "100.64.0.0/10,fd7a:115c:a1e0::/48",
    "idempotency_path": "/var/lib/minecraft-deploy/idempotency.sqlite",
    "audit_path": "/var/lib/minecraft-deploy/audit.jsonl",
    "lock_path": "/var/lib/minecraft-deploy/daemon.lock",
    # Slice 12 additions
    "trusted_keys_path": "config.d/trusted_keys.toml",
    "registry_path": "sync/packing_slips/release-index.jsonl",
    "journal_path": "/var/lib/minecraft-deploy/operation.jsonl",
    "world_backup_root": "/srv/minecraft/backups/world",
}


@dataclass(frozen=True)
class MinecraftDaemonConfig:
    """Fully resolved daemon configuration."""

    state_dir: Path
    slip_dir: Path
    release_store: Path
    staging_root: Path
    project_root: Path
    daemon_version: str
    http_host: str
    http_port: int
    bearer_token_env: str
    tailscale_cidrs: tuple[str, ...]
    idempotency_path: Path
    audit_path: Path
    lock_path: Path
    # Slice 12 additions
    trusted_keys_path: Path
    registry_path: Path
    journal_path: Path
    world_backup_root: Path


def _read_daemon_section(toml_path: Path) -> dict:
    """Return the ``[daemon]`` table from ``toml_path``, or {} on any failure."""
    if not toml_path.is_file():
        return {}
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("daemon")
    return section if isinstance(section, dict) else {}


def _resolve_path(raw: str, project_root: Path) -> Path:
    """Resolve a TOML path value against the project root."""
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path


def load_config(config_dir: Path, daemon_version: str = "1.3.0") -> MinecraftDaemonConfig:
    """Load the daemon configuration from ``config_dir/deploy_pack.toml``.

    Missing keys use defaults. Missing config file uses defaults entirely.
    Environment variables (bearer token) are NOT read here; the plugin
    reads them at construction time so failures surface with a clear
    message during daemon startup, not during config load.
    """
    project_root = config_dir.parent
    toml_path = config_dir / "deploy_pack.toml"
    section = _read_daemon_section(toml_path)

    def _get(key: str) -> str:
        return str(section.get(key, _DEFAULTS[key]))

    cidrs_raw = _get("tailscale_cidrs")
    cidrs = tuple(part.strip() for part in cidrs_raw.split(",") if part.strip())

    try:
        port = int(_get("http_port"))
    except ValueError:
        port = int(_DEFAULTS["http_port"])

    return MinecraftDaemonConfig(
        state_dir=_resolve_path(_get("state_dir"), project_root),
        slip_dir=_resolve_path(_get("slip_dir"), project_root),
        release_store=_resolve_path(_get("release_store"), project_root),
        staging_root=_resolve_path(_get("staging_root"), project_root),
        project_root=project_root,
        daemon_version=daemon_version,
        http_host=_get("http_host"),
        http_port=port,
        bearer_token_env=_get("bearer_token_env"),
        tailscale_cidrs=cidrs,
        idempotency_path=_resolve_path(_get("idempotency_path"), project_root),
        audit_path=_resolve_path(_get("audit_path"), project_root),
        lock_path=_resolve_path(_get("lock_path"), project_root),
        trusted_keys_path=_resolve_path(_get("trusted_keys_path"), project_root),
        registry_path=_resolve_path(_get("registry_path"), project_root),
        journal_path=_resolve_path(_get("journal_path"), project_root),
        world_backup_root=_resolve_path(_get("world_backup_root"), project_root),
    )
