# src/minecraft/deploy_pack/daemon/main.py

"""Entry point for the Minecraft deploy daemon.

Wires the DaemonCore ``Daemon`` builder with Minecraft-specific config,
resources, and the HTTP admin plugin.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from DaemonCore import Daemon, DaemonError
from DaemonCore.resources import (
    EnvSecrets,
    FileJournal,
    FileLock,
    FileStateStore,
)

from .auth import BearerAuth, CombinedAuth, TailscaleAuth
from .config import MinecraftDaemonConfig, load_config
from .idempotency import SQLiteIdempotency
from .plugins.http import HttpAdminPlugin

__all__ = ["build_daemon", "main"]


def build_daemon(config: MinecraftDaemonConfig) -> Daemon:
    """Construct a fully wired ``Daemon`` from ``config``.

    Does not start anything. Call ``await daemon.run()`` to serve.
    Raises ``ValueError`` if the bearer token env var is unset.
    """
    auth = CombinedAuth(
        (
            TailscaleAuth(cidrs=config.tailscale_cidrs),
            BearerAuth(token_env=config.bearer_token_env),
        )
    )
    idempotency = SQLiteIdempotency(config.idempotency_path)
    plugin = HttpAdminPlugin(
        host=config.http_host,
        port=config.http_port,
        auth=auth,
        idempotency=idempotency,
        slip_dir=config.slip_dir,
        release_store=config.release_store,
    )
    return (
        Daemon("minecraft-deploy", config.daemon_version)
        .state(FileStateStore(config.state_dir))
        .audit(FileJournal(config.audit_path))
        .lock(FileLock(config.lock_path))
        .secrets(EnvSecrets())
        .plugin(plugin)
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="minecraft-deploy-daemon",
        description="Minecraft deploy daemon",
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default="config.d",
        help="Directory containing deploy_pack.toml (default: config.d)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Process entry point."""
    args = _parse_args(argv)
    config_dir = Path(args.config_dir).resolve()

    try:
        config = load_config(config_dir)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 3

    try:
        daemon = build_daemon(config)
    except ValueError as exc:
        print(f"daemon config error: {exc}", file=sys.stderr)
        return 3

    try:
        asyncio.run(daemon.run())
    except DaemonError as exc:
        print(f"daemon error: {exc}", file=sys.stderr)
        return 1
    return 0
