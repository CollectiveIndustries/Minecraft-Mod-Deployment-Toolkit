# src/minecraft/discord_bot/config.py

"""Discord bot configuration loading (spec v1.4.0 Section 11.1, Section 17.1).

The bot is enabled by two things together:

1. A ``[discord]`` section in ``config.d/upgrade.toml``.
2. A non-empty ``DISCORD_BOT_TOKEN`` entry in ``config.d/.env``.

If either is absent, :func:`load_discord_bot_config` returns ``None``
and the bot is treated as disabled. This lets a host that never intends
to run a bot exist without a token, and lets an operator disable the bot
by removing its token without touching the TOML section.

**If ``[discord]`` and the token are both present**, every other
required key is validated strictly. A partial ``[discord]`` section with
a valid token means the operator intends to enable the bot; a missing
``guild_id`` is a bug, not an opt-out.

``.env`` handling
-----------------
``.env`` is parsed with :func:`ConfigCore.loader.load_env`, a flat
``KEY=VALUE`` reader that does **not** populate ``os.environ``. The
token never enters the process environment and is excluded from every
``repr`` and log line this module emits.

``config.d/.env`` is shared across the toolchain. The ``deploy_pack``
daemon reads ``webhook_url`` from it; the bot daemon reads
``DISCORD_BOT_TOKEN`` from it. Distinct keys, no collision.

Values in ``.env`` must **not** be quoted. ``ConfigCore.load_env`` does
not strip surrounding quotes; ``DISCORD_BOT_TOKEN="abc"`` stores the
literal string ``"abc"`` including the quote characters. Operators must
write ``DISCORD_BOT_TOKEN=abc`` on its own.

Fields recorded for T3
----------------------
``application_id`` and ``public_key`` are loaded here even though T2
does not consume them. T3's signed-HTTP interaction endpoint needs both:
the application ID identifies which application an inbound interaction
targets, and the Ed25519 public key verifies Discord's request
signature. Loading them now avoids a config-schema change when T3 lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ConfigCore import ConfigError, ConfigManager
from ConfigCore.loader import load_env
from LoggingCore import get_logger

from .errors import DiscordBotConfigError

__all__ = [
    "DiscordBotConfig",
    "load_discord_bot_config",
]

_log = get_logger(__name__)

_HEX_64 = re.compile(r"[0-9a-f]{64}")
# Discord snowflakes are numeric strings. As of the current API they are
# 17-20 digits; the range is future-proofed to accept any plausible
# growth while rejecting non-numeric typos.
_SNOWFLAKE = re.compile(r"[0-9]{17,20}")
_TOKEN_ENV_KEY = "DISCORD_BOT_TOKEN"


@dataclass(frozen=True)
class DiscordBotConfig:
    """Fully-resolved Discord bot application configuration.

    ``bot_token`` is a credential and is excluded from ``repr`` via
    :func:`dataclasses.field`. Use the printed field list for diagnostics;
    never log the token.

    Fields:

    - ``bot_token``: Discord bot token. Non-empty when the config exists.
    - ``application_id``: The bot's Discord application snowflake. Used
      by the T3 HTTP interaction endpoint to confirm an inbound
      interaction targets this application.
    - ``public_key``: 64-character lowercase hex Ed25519 public key
      Discord uses to sign HTTP interaction requests. Consumed by T3's
      signature-verification step.
    - ``guild_id``: The guild the bot serves. Interactions from other
      guilds are rejected at dispatch.
    - ``channel_id``: The operator-facing channel notifications post to.
    - ``allowlist_roles``: Role snowflakes authorized to invoke commands.
      Empty tuple means no role grants authorization.
    - ``allowlist_users``: User snowflakes authorized to invoke commands.
      Empty tuple means no user grants authorization. Union with
      ``allowlist_roles``: either grants access.
    """

    bot_token: str = field(repr=False)
    application_id: str
    public_key: str
    guild_id: str
    channel_id: str
    allowlist_roles: tuple[str, ...]
    allowlist_users: tuple[str, ...]


def load_discord_bot_config(config_dir: Path) -> DiscordBotConfig | None:
    """Load the Discord bot configuration, or ``None`` when the bot is disabled.

    Returns ``None`` when any of the following hold:

    - ``config.d/upgrade.toml`` does not exist or has no ``[discord]``
      section.
    - ``config.d/.env`` does not exist, is unreadable, or has no
      ``DISCORD_BOT_TOKEN`` entry.
    - ``DISCORD_BOT_TOKEN`` is present but blank after stripping.

    Raises :class:`DiscordBotConfigError` when the TOML file is
    malformed, when ``[discord]`` is present but not a table, or when
    the token is present and any other required key is missing,
    malformed, or fails validation. A section plus a token is an
    affirmative "enable the bot" signal; missing required keys are a
    configuration bug.

    The token is read from ``.env`` via
    :func:`ConfigCore.loader.load_env`, which is a flat ``KEY=VALUE``
    reader. ``os.environ`` is never touched.
    """
    section = _read_discord_section(config_dir)
    if section is None:
        _log.debug("discord bot: no [discord] section; bot disabled")
        return None

    token = _read_token(config_dir)
    if token is None:
        _log.debug("discord bot: no DISCORD_BOT_TOKEN in .env; bot disabled")
        return None

    return _validate_section(section, token)


def _read_discord_section(config_dir: Path) -> dict[str, object] | None:
    """Return the ``[discord]`` table from ``upgrade.toml``, or ``None``.

    ``None`` means the section is absent (bot disabled). An empty table
    (``[discord]`` present with no keys) is returned as ``{}`` and will
    fail validation later only if a token is also present.

    Raises :class:`DiscordBotConfigError` if the TOML file is malformed
    or if ``[discord]`` exists but is not a table.
    """
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return None
    try:
        config = ConfigManager().file(toml_path).load()
    except ConfigError as exc:
        raise DiscordBotConfigError(f"could not read {toml_path}: {exc}") from exc
    raw = config.get("discord")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DiscordBotConfigError(f"{toml_path}: [discord] must be a table, got {type(raw).__name__}")
    return dict(raw)


def _read_token(config_dir: Path) -> str | None:
    """Return the bot token from ``.env``, or ``None`` when absent or blank.

    Reads the file with :func:`ConfigCore.loader.load_env`, which does
    **not** modify ``os.environ``. A missing file, unreadable file, or
    blank value all yield ``None``. Malformed lines are skipped by the
    parser (same behaviour as the legacy ``deploy_pack`` ``.env``
    handling).
    """
    env_path = config_dir / ".env"
    if not env_path.is_file():
        return None
    try:
        env = load_env(env_path)
    except OSError as exc:
        _log.warning(f"discord bot: could not read {env_path}: {exc}")
        return None
    value = env.get(_TOKEN_ENV_KEY)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _validate_section(section: dict[str, object], token: str) -> DiscordBotConfig:
    """Build a :class:`DiscordBotConfig` from a present section and token.

    Raises :class:`DiscordBotConfigError` on any missing or malformed
    required key. The allowlists are optional and default to empty
    tuples.
    """
    application_id = _require_snowflake(section, "application_id")
    public_key = _require_str(section, "public_key")
    if _HEX_64.fullmatch(public_key) is None:
        raise DiscordBotConfigError(f"[discord].public_key must be a 64-char lowercase hex string, got {public_key!r}")
    guild_id = _require_snowflake(section, "guild_id")
    channel_id = _require_snowflake(section, "channel_id")
    allowlist_roles = _optional_snowflake_list(section, "allowlist_roles")
    allowlist_users = _optional_snowflake_list(section, "allowlist_users")
    return DiscordBotConfig(
        bot_token=token,
        application_id=application_id,
        public_key=public_key,
        guild_id=guild_id,
        channel_id=channel_id,
        allowlist_roles=allowlist_roles,
        allowlist_users=allowlist_users,
    )


def _require_str(section: dict[str, object], key: str) -> str:
    """Return ``section[key]`` as a non-empty string, or raise :class:`DiscordBotConfigError`."""
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise DiscordBotConfigError(f"[discord].{key} must be a non-empty string, got {value!r}")
    return value


def _require_snowflake(section: dict[str, object], key: str) -> str:
    """Return ``section[key]`` as a Discord snowflake string, or raise.

    A snowflake is a 17-20 digit numeric string. ``int`` values are
    rejected: Discord's JSON API uses string snowflakes, and
    ``tomllib`` accepts large integers as Python ints, so a user who
    writes ``guild_id = 123456789012345678`` unquoted would otherwise
    silently produce a non-string config value.
    """
    value = _require_str(section, key)
    if _SNOWFLAKE.fullmatch(value) is None:
        raise DiscordBotConfigError(f"[discord].{key} must be a Discord snowflake (17-20 digits), got {value!r}")
    return value


def _optional_snowflake_list(section: dict[str, object], key: str) -> tuple[str, ...]:
    """Return ``section[key]`` as a tuple of snowflake strings, or raise.

    An absent key yields an empty tuple. A present key must be a list of
    snowflake strings; non-list values, non-string elements, and elements
    that fail snowflake validation all raise
    :class:`DiscordBotConfigError`.
    """
    value = section.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise DiscordBotConfigError(f"[discord].{key} must be an array of strings, got {value!r}")
    for item in value:
        if _SNOWFLAKE.fullmatch(item) is None:
            raise DiscordBotConfigError(f"[discord].{key} entry must be a Discord snowflake (17-20 digits), got {item!r}")
    return tuple(value)
