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

Config-source policy (Section 3.12, Section 9.2)
-----------------------------------------------
The bearer token and any other daemon secrets are resolved from
``config.d/.env`` via ConfigCore's flat-file loader. ``os.environ`` is
not consulted. ``config_dir`` is therefore required for any non-trivial
daemon build; the earlier ``config_dir=None`` path (used only by slice-11
tests) is gone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from ConfigCore.loader import load_env
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


def _resolve_bearer_token(config: MinecraftDaemonConfig, config_dir: Path | None) -> str:
    """Resolve the bearer token from ``config.d/.env``.

    ``config.bearer_token_env`` names the key. The value is loaded via
    ConfigCore's flat-file loader, matching Section 3.12. Any missing
    piece raises ``ValueError`` with a diagnostic naming the file and
    key, so an operator sees exactly what to fix at startup.
    """
    env_name = config.bearer_token_env
    if config_dir is None:
        raise ValueError(f"build_daemon requires config_dir to resolve bearer token {env_name!r}")
    env_path = config_dir / ".env"
    if not env_path.is_file():
        raise ValueError(f"bearer token {env_name!r} requires {env_path} (file not found)")
    try:
        env = load_env(env_path)
    except OSError as exc:
        raise ValueError(f"could not read {env_path}: {exc}") from exc
    value = env.get(env_name, "").strip()
    if not value:
        raise ValueError(f"bearer token {env_name!r} has no value in {env_path}")
    return value


def build_daemon(
    config: MinecraftDaemonConfig,
    *,
    config_dir: Path | None = None,
) -> Daemon:
    """Construct a fully wired ``Daemon`` from ``config``.

    Does not start anything. Call ``await daemon.run()`` to serve.
    Raises ``ValueError`` if the bearer token is not resolvable from
    ``config.d/.env``.

    ``config_dir`` is the directory containing ``.env``. It is required
    for production builds (bearer-token resolution) and for the deploy
    orchestrator wiring. Tests that only exercise verification /
    plugin-shape paths may still pass a temp config_dir with a written
    ``.env``.
    """
    bearer_token = _resolve_bearer_token(config, config_dir)
    auth = CombinedAuth(
        (
            TailscaleAuth(cidrs=config.tailscale_cidrs),
            BearerAuth(token=bearer_token),
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
