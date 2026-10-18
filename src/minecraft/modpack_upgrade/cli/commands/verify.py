# src/minecraft/modpack_upgrade/cli/commands/verify.py

"""``minecraft-upgrade verify --tag <tag>`` -- read-only slip verification.

Loads the slip at ``<slip_dir>/<tag>.toml``, verifies schema and content
hash, and emits the parsed slip fields. Does not touch the release
bundle store; that is a deployment-time concern owned by the daemon.
"""

from __future__ import annotations

from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import (
    HashMismatchError,
    SchemaError,
    SlipError,
)

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.verify")


def _load_slip_or_raise(slip_path: Path) -> slip_mod.PackingSlip:
    """Load a slip, translating the three exception types to CLI errors."""
    try:
        return slip_mod.load_slip(slip_path)
    except HashMismatchError as exc:
        raise UpgradeCliError(3, f"slip hash mismatch at {slip_path}: {exc}") from exc
    except SlipError as exc:
        raise UpgradeCliError(3, f"slip unreadable at {slip_path}: {exc}") from exc
    except SchemaError as exc:
        raise UpgradeCliError(3, f"slip schema error at {slip_path}: {exc}") from exc


def _emit_slip(slip: slip_mod.PackingSlip, slip_path: Path) -> None:
    """Emit slip summary fields to the CLI output channel."""
    _log_cli.info(f"slip: {slip_path}")
    _log_cli.info(f"release_id: {slip.release_id}")
    _log_cli.info(f"release_mode: {slip.release_mode}")
    _log_cli.info(f"content_sha256: {slip.content_sha256}")
    _log_cli.info(f"jars: {len(slip.jars)}")
    _log_cli.info(f"resource_packs: {len(slip.resource_packs)}")
    _log_cli.info(f"velocity_plugins: {len(slip.velocity_plugins)}")
    instances = ", ".join(slip.instances.keys()) or "(none)"
    _log_cli.info(f"instances: {instances}")
    _log_cli.info("status: ok")


def run(*, config_dir: Path, project_root: Path, tag: str) -> int:
    """Verify a slip by tag. Returns 0 on success, 3 on any slip error."""
    del project_root
    daemon_config = load_daemon_config(config_dir)
    slip_path = daemon_config.slip_dir / f"{tag}.toml"
    slip = _load_slip_or_raise(slip_path)
    _emit_slip(slip, slip_path)
    return 0
