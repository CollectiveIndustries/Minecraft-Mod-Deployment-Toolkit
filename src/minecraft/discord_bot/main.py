# src/minecraft/discord_bot/main.py

"""Bot daemon entry point (Slice 15 T3b/T3c).

:func:`build_daemon` wires the configuration, invoker, REST client,
and HTTP plugin into a :class:`DaemonCore.Daemon`. :func:`main` is the
process entry point: it parses ``--config-dir``, calls
:func:`build_daemon`, and hands control to the daemon's run loop.

DaemonCore boundary
-------------------
:class:`DaemonCore.Daemon` uses a **builder pattern**, matching the
existing ``deploy_pack`` daemon:

    Daemon(name, version)
        .state(...)
        .audit(...)
        .lock(...)
        .secrets(...)
        .plugin(...)

The name and version are positional. Each builder method returns the
daemon so the chain stays flat. The bot daemon wires:

- ``state`` - ``FileStateStore`` over ``daemon_config.state_dir``.
- ``audit`` - ``FileJournal`` at ``state_dir / "audit.jsonl"``. No
  separate config key; the audit log is colocated with the rest of
  the daemon-owned state.
- ``lock`` - ``FileLock`` at ``daemon_config.lock_path``.
- ``secrets`` - ``EnvSecrets()``; the bot does not consume DaemonCore-
  managed secrets today, but the builder requires a provider.
- ``plugin`` - :class:`DiscordBotPlugin` from
  :mod:`minecraft.discord_bot.http_endpoint`, wired with the notify
  bearer token (T3c) when ``[daemon].notify_bearer_token_key`` resolves.

If DaemonCore's ``Daemon`` builder later changes, only this module and
``tests/discord_bot/test_main.py`` need updating; every other module in
T3b/T3c is DaemonCore-independent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from DaemonCore import Daemon, DaemonError
from DaemonCore.resources import EnvSecrets, FileJournal, FileLock, FileStateStore
from LoggingCore import get_logger

from .cli_invoker import SubprocessCliInvoker
from .config import load_discord_bot_config
from .daemon_config import load_discord_daemon_config
from .discord_client import DiscordRestClient
from .http_endpoint import DiscordBotPlugin

__all__ = ["build_daemon", "main"]

_log = get_logger(__name__)

_DEFAULT_DAEMON_VERSION = "0.2.1"


def build_daemon(
    *,
    config_dir: Path,
    daemon_version: str = _DEFAULT_DAEMON_VERSION,
) -> Daemon:
    """Assemble the Discord bot daemon from its configuration files.

    Raises :class:`RuntimeError` when the bot is disabled (no
    ``[discord]`` section or no bot token). Raises
    :class:`DiscordBotConfigError` when the daemon config file is
    malformed or a required field is absent. Never returns ``None``:
    a daemon is either fully built or an exception is raised.
    """
    bot_config = load_discord_bot_config(config_dir)
    if bot_config is None:
        raise RuntimeError("Discord bot is not configured; [discord] section plus DISCORD_BOT_TOKEN required")
    daemon_config = load_discord_daemon_config(config_dir, daemon_version=daemon_version)

    invoker = SubprocessCliInvoker(
        binary=daemon_config.cli_binary,
        config_dir=config_dir,
        timeout_seconds=daemon_config.cli_timeout_seconds,
        project_root=daemon_config.project_root,
    )
    bot_client = DiscordRestClient(bot_token=bot_config.bot_token)
    plugin = DiscordBotPlugin(
        host=daemon_config.http_host,
        port=daemon_config.http_port,
        bot_config=bot_config,
        invoker=invoker,
        bot_client=bot_client,
        notify_bearer_token=daemon_config.notify_bearer_token,
    )

    audit_path = daemon_config.state_dir / "audit.jsonl"
    return (
        Daemon("discord-bot", daemon_version)
        .state(FileStateStore(daemon_config.state_dir))
        .audit(FileJournal(audit_path))
        .lock(FileLock(daemon_config.lock_path))
        .secrets(EnvSecrets())
        .plugin(plugin)
    )


def main(argv: list[str] | None = None) -> int:
    """Process entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="minecraft-discord-bot",
        description="Discord bot peer daemon for the minecraft toolchain",
    )
    parser.add_argument(
        "--config-dir",
        default="config.d",
        help="Config directory (default: config.d)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"minecraft-discord-bot {_DEFAULT_DAEMON_VERSION}",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0

    config_dir = Path(args.config_dir).resolve()
    if config_dir.is_file():
        config_dir = config_dir.parent

    try:
        daemon = build_daemon(config_dir=config_dir)
    except Exception as exc:
        _log.error(f"failed to build daemon: {exc}")
        return 3

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        return 0
    except DaemonError as exc:
        _log.error(f"daemon failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
