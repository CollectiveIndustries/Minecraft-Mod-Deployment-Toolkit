# src/minecraft/discord_bot/daemon_config.py

"""Discord bot daemon configuration (Slice 15 T3a).

The bot is a peer daemon to ``deploy_pack/daemon``. Its runtime
configuration lives in ``config.d/discord_bot.toml`` under a ``[daemon]``
section, mirroring the ``[daemon]`` section of
``config.d/deploy_pack.toml`` that the ``deploy_pack`` daemon reads.

The application-level configuration (guild, channel, allowlists, bot
token) lives in ``[discord]`` of ``config.d/upgrade.toml`` and is loaded
by :mod:`minecraft.discord_bot.config`. This module concerns only the
daemon's runtime: HTTP listener, state directory, lock file, CLI
subprocess parameters, and the notify-endpoint bearer token.

Unlike :func:`minecraft.discord_bot.config.load_discord_bot_config`,
which returns ``None`` when the bot is disabled, this loader always
returns a fully-populated config or raises. The daemon config file is
required for the daemon to start; there is no "daemon disabled by
absence of config" state. If an operator does not want a bot daemon,
they do not run one.

Path resolution
---------------
Relative paths in ``[daemon]`` resolve against the project root
(``config_dir.parent``), matching ``deploy_pack.toml``'s convention.
Absolute paths are used verbatim.

Notify bearer token
-------------------
``notify_bearer_token_key`` names a key in ``config.d/.env`` whose value
authenticates ``POST /notify`` calls from the ``deploy_pack`` process.
The key is optional: an absent ``notify_bearer_token_key`` leaves
:attr:`DiscordDaemonConfig.notify_bearer_token` as ``None``, which
disables the notify endpoint in T3c. A key that is set but whose value
is absent or blank from ``.env`` raises, so a partially-configured
notify path cannot silently do nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ConfigCore import ConfigError, ConfigManager
from ConfigCore.loader import load_env
from LoggingCore import get_logger

from .errors import DiscordBotConfigError

__all__ = [
    "DiscordDaemonConfig",
    "load_discord_daemon_config",
]

_log = get_logger(__name__)

_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8766
_DEFAULT_CLI_BINARY = "minecraft-upgrade"
_DEFAULT_CLI_TIMEOUT_SECONDS = 60
_DEFAULT_DAEMON_VERSION = "0.2.1"


@dataclass(frozen=True)
class DiscordDaemonConfig:
    """Runtime configuration for the Discord bot daemon.

    Fields:

    - ``state_dir``: Directory for daemon-owned durable state (lock
      file, journal, idempotency store when T3b lands).
    - ``lock_path``: Single-file deployment lock path. Mirrors
      ``deploy_pack`` daemon's lock semantics.
    - ``http_host`` / ``http_port``: The signed-HTTP interaction
      endpoint's bind address. Loopback by default.
    - ``notify_bearer_token``: Credential for ``POST /notify`` from
      the ``deploy_pack`` process. ``None`` disables the endpoint. The
      token is excluded from ``repr``.
    - ``cli_binary``: Name or path of the ``minecraft-upgrade``
      executable that handlers invoke via subprocess.
    - ``cli_timeout_seconds``: Subprocess timeout for CLI invocations.
      A timeout raises :class:`DiscordBotCliError`.
    - ``project_root``: Working directory for CLI subprocesses. Equal
      to ``config_dir.parent``.
    - ``daemon_version``: Recorded in the bot's health report;
      informational only.
    """

    state_dir: Path
    lock_path: Path
    http_host: str
    http_port: int
    notify_bearer_token: str | None = field(repr=False)
    cli_binary: str
    cli_timeout_seconds: int
    project_root: Path
    daemon_version: str


def load_discord_daemon_config(
    config_dir: Path,
    *,
    daemon_version: str = _DEFAULT_DAEMON_VERSION,
) -> DiscordDaemonConfig:
    """Load ``config.d/discord_bot.toml``.

    Raises :class:`DiscordBotConfigError` when:

    - The file does not exist.
    - The file is not valid TOML.
    - The ``[daemon]`` section is absent or is not a table.
    - A required field is missing or malformed.
    - ``notify_bearer_token_key`` is set but ``.env`` is missing or
      does not contain a non-empty value for that key.
    """
    section = _read_daemon_section(config_dir)
    project_root = config_dir.parent

    state_dir = _require_path(section, "state_dir", project_root)
    lock_path = _require_path(section, "lock_path", project_root)
    http_host = _optional_str(section, "http_host", _DEFAULT_HTTP_HOST)
    http_port = _optional_port(section, "http_port", _DEFAULT_HTTP_PORT)
    cli_binary = _optional_str(section, "cli_binary", _DEFAULT_CLI_BINARY)
    cli_timeout_seconds = _optional_positive_int(section, "cli_timeout_seconds", _DEFAULT_CLI_TIMEOUT_SECONDS)
    notify_bearer_token_key = _optional_str_or_none(section, "notify_bearer_token_key")
    notify_bearer_token = _read_notify_token(config_dir, notify_bearer_token_key)

    return DiscordDaemonConfig(
        state_dir=state_dir,
        lock_path=lock_path,
        http_host=http_host,
        http_port=http_port,
        notify_bearer_token=notify_bearer_token,
        cli_binary=cli_binary,
        cli_timeout_seconds=cli_timeout_seconds,
        project_root=project_root,
        daemon_version=daemon_version,
    )


def _read_daemon_section(config_dir: Path) -> dict[str, object]:
    """Return the ``[daemon]`` table from ``discord_bot.toml``.

    Raises :class:`DiscordBotConfigError` if the file is missing,
    malformed, or the section is absent or not a table.
    """
    toml_path = config_dir / "discord_bot.toml"
    if not toml_path.is_file():
        raise DiscordBotConfigError(f"discord bot daemon config not found: {toml_path}")
    try:
        config = ConfigManager().file(toml_path).load()
    except ConfigError as exc:
        raise DiscordBotConfigError(f"could not read {toml_path}: {exc}") from exc
    raw = config.get("daemon")
    if raw is None:
        raise DiscordBotConfigError(f"{toml_path}: missing [daemon] section")
    if not isinstance(raw, dict):
        raise DiscordBotConfigError(f"{toml_path}: [daemon] must be a table, got {type(raw).__name__}")
    return dict(raw)


def _resolve_path(raw: str, project_root: Path) -> Path:
    """Resolve a config path value against the project root.

    Absolute paths are returned verbatim; relative paths are resolved
    against ``project_root``. The resolved path is not normalised
    further (no ``..`` collapse) - operator intent wins.
    """
    path = Path(raw)
    return path if path.is_absolute() else (project_root / path).resolve()


def _require_path(section: dict[str, object], key: str, project_root: Path) -> Path:
    """Return ``section[key]`` as a resolved path, or raise."""
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise DiscordBotConfigError(f"[daemon].{key} must be a non-empty string, got {value!r}")
    return _resolve_path(value, project_root)


def _optional_str(section: dict[str, object], key: str, default: str) -> str:
    """Return ``section[key]`` as a non-empty string, or ``default``."""
    value = section.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise DiscordBotConfigError(f"[daemon].{key} must be a non-empty string, got {value!r}")
    return value


def _optional_str_or_none(section: dict[str, object], key: str) -> str | None:
    """Return ``section[key]`` as a non-empty string, ``None`` when absent, or raise."""
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DiscordBotConfigError(f"[daemon].{key} must be a non-empty string, got {value!r}")
    return value


def _optional_port(section: dict[str, object], key: str, default: int) -> int:
    """Return ``section[key]`` as a TCP port (1-65535), or ``default``."""
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise DiscordBotConfigError(f"[daemon].{key} must be an integer between 1 and 65535, got {value!r}")
    return value


def _optional_positive_int(section: dict[str, object], key: str, default: int) -> int:
    """Return ``section[key]`` as a positive integer, or ``default``."""
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DiscordBotConfigError(f"[daemon].{key} must be a positive integer, got {value!r}")
    return value


def _read_notify_token(config_dir: Path, key: str | None) -> str | None:
    """Resolve the notify bearer token from ``.env``.

    Returns ``None`` when ``key`` is ``None`` (notify endpoint disabled).
    Raises :class:`DiscordBotConfigError` when ``key`` is set but the
    ``.env`` file is missing, unreadable, or does not contain a
    non-empty value for that key.
    """
    if key is None:
        return None
    env_path = config_dir / ".env"
    if not env_path.is_file():
        raise DiscordBotConfigError(f"[daemon].notify_bearer_token_key={key!r} is set but {env_path} does not exist")
    try:
        env = load_env(env_path)
    except OSError as exc:
        raise DiscordBotConfigError(f"could not read {env_path}: {exc}") from exc
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DiscordBotConfigError(f"[daemon].notify_bearer_token_key={key!r} is set but {env_path} has no non-empty value for it")
    return value.strip()
