# src/minecraft/deploy_pack/daemon/main.py

"""Entry point for the Minecraft deploy daemon.

Wires the DaemonCore ``Daemon`` builder with Minecraft-specific config,
resources, and the HTTP admin plugin.

Slice 12: the plugin is constructed with ``registry_path`` and
``trusted_keys_path`` so ``/verify``, ``/publish``, and ``/deploy`` run
the real registry chain walk and bundle-signature verification
(Upgrade_System_specifications Section 7.1-7.5, Section 12.4).

Slice 13: when ``config_dir`` is supplied (production), the plugin is
also given a ``deploy_callable`` so ``/deploy`` performs real
deployments. Without ``config_dir`` (tests, or a daemon built for
verification only), ``/deploy`` falls back to 501.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from DaemonCore import Daemon, DaemonError
from DaemonCore.resources import (
    EnvSecrets,
    FileJournal,
    FileLock,
    FileStateStore,
)

from minecraft.deploy_pack.docker_runtime import DockerRuntime

from .auth import BearerAuth, CombinedAuth, TailscaleAuth
from .config import MinecraftDaemonConfig, load_config
from .deploy import DeployOutcome
from .deploy_runtime import build_deploy_callable
from .idempotency import SQLiteIdempotency
from .plugins.http import HttpAdminPlugin

__all__ = ["build_daemon", "main"]


def _startup_error(message: str) -> None:
    """Write a startup error to stderr before LoggingCore is available.

    DaemonCore installs its logging sinks inside ``daemon.run()``. Errors
    that occur before that call (config load, daemon construction, or the
    daemon itself raising) cannot route through LoggingCore because no
    core is configured yet; writing to stderr is the only correct option.
    This helper is the single allowed exception to the "no direct
    sys.stderr" rule for this file.
    """
    sys.stderr.write(message + "\n")  # noqa: TID251 -- see docstring
    sys.stderr.flush()  # noqa: TID251 -- see docstring


def _build_deploy_callable(
    config: MinecraftDaemonConfig,
    config_dir: Path | None,
) -> Callable[[str], DeployOutcome] | None:
    """Return the deploy callable, or None when config_dir is not supplied.

    Construction is wrapped in try/except because ``DockerRuntime()`` may
    raise ``DockerUnavailableError`` if the Docker daemon is not reachable
    at startup. A missing callable leaves ``/deploy`` at 501, which is the
    correct behavior for a daemon that cannot deploy.
    """
    if config_dir is None:
        return None
    try:
        runtime = DockerRuntime()
        return build_deploy_callable(config=config, config_dir=config_dir, runtime=runtime)
    except Exception as exc:
        _startup_error(f"deploy callable unavailable: {exc}")
        return None


def build_daemon(
    config: MinecraftDaemonConfig,
    *,
    config_dir: Path | None = None,
) -> Daemon:
    """Construct a fully wired ``Daemon`` from ``config``.

    Does not start anything. Call ``await daemon.run()`` to serve.
    Raises ``ValueError`` if the bearer token env var is unset.

    ``config_dir`` is optional. When supplied (production), the
    deployment orchestrator is wired up so ``/deploy`` performs real
    deployments. When omitted (default for tests), ``/deploy`` returns
    501 after verification.
    """
    auth = CombinedAuth(
        (
            TailscaleAuth(cidrs=config.tailscale_cidrs),
            BearerAuth(token_env=config.bearer_token_env),
        )
    )
    idempotency = SQLiteIdempotency(config.idempotency_path)
    deploy_callable = _build_deploy_callable(config, config_dir)
    plugin = HttpAdminPlugin(
        host=config.http_host,
        port=config.http_port,
        auth=auth,
        idempotency=idempotency,
        slip_dir=config.slip_dir,
        release_store=config.release_store,
        registry_path=config.registry_path,
        trusted_keys_path=config.trusted_keys_path,
        deploy_callable=deploy_callable,
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
        _startup_error(f"config error: {exc}")
        return 3

    try:
        daemon = build_daemon(config, config_dir=config_dir)
    except ValueError as exc:
        _startup_error(f"daemon config error: {exc}")
        return 3

    try:
        asyncio.run(daemon.run())
    except DaemonError as exc:
        _startup_error(f"daemon error: {exc}")
        return 1
    return 0
