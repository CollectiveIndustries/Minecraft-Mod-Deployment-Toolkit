# tests/discord_bot/test_config.py

"""Configuration loading tests for the Discord bot daemon (T2)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import tomli_w

from minecraft.discord_bot.config import (
    load_discord_bot_config,
)
from minecraft.discord_bot.errors import DiscordBotConfigError, DiscordBotError


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _full_discord_section() -> dict:
    """Return a fully-populated, valid ``[discord]`` section body."""
    return {
        "application_id": "1234567890123456789",
        "public_key": _hex("a"),
        "guild_id": "9876543210987654321",
        "channel_id": "1122334455667788990",
        "allowlist_roles": ["1111111111111111111", "2222222222222222222"],
        "allowlist_users": ["3333333333333333333"],
    }


def _write_upgrade_toml(config_dir: Path, body: dict) -> None:
    """Write ``upgrade.toml`` under ``config_dir`` as TOML."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))


def _write_env(config_dir: Path, body: str) -> None:
    """Write ``.env`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(body, encoding="utf-8")


def _setup(tmp_path: Path, *, section: dict | None = None, env_body: str | None = None) -> Path:
    """Create ``config.d`` with optional upgrade.toml and .env. Return config_dir."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    if section is not None:
        _write_upgrade_toml(config_dir, {"discord": section})
    if env_body is not None:
        _write_env(config_dir, env_body)
    return config_dir


# ---------------------------------------------------------------------------
# Disabled paths -- the function returns None
# ---------------------------------------------------------------------------


def test_missing_upgrade_toml_returns_none(tmp_path: Path) -> None:
    """No upgrade.toml means no [discord] section; bot disabled."""
    config_dir = _setup(tmp_path)
    assert load_discord_bot_config(config_dir) is None


def test_missing_discord_section_returns_none(tmp_path: Path) -> None:
    """An upgrade.toml without [discord] disables the bot."""
    config_dir = _setup(tmp_path)
    _write_upgrade_toml(config_dir, {"other": {"key": "value"}})
    _write_env(config_dir, "DISCORD_BOT_TOKEN=abc\n")
    assert load_discord_bot_config(config_dir) is None


def test_empty_discord_section_without_token_returns_none(tmp_path: Path) -> None:
    """An empty [discord] section without a token still disables the bot."""
    config_dir = _setup(tmp_path, section={})
    assert load_discord_bot_config(config_dir) is None


def test_missing_env_file_returns_none(tmp_path: Path) -> None:
    """A [discord] section alone is not enough; a token is required."""
    config_dir = _setup(tmp_path, section=_full_discord_section())
    assert load_discord_bot_config(config_dir) is None


def test_env_without_token_key_returns_none(tmp_path: Path) -> None:
    """A .env without DISCORD_BOT_TOKEN disables the bot."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="OTHER=value\n")
    assert load_discord_bot_config(config_dir) is None


def test_blank_token_returns_none(tmp_path: Path) -> None:
    """An empty DISCORD_BOT_TOKEN disables the bot."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=\n")
    assert load_discord_bot_config(config_dir) is None


def test_whitespace_only_token_returns_none(tmp_path: Path) -> None:
    """A whitespace-only DISCORD_BOT_TOKEN disables the bot after stripping."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=   \n")
    assert load_discord_bot_config(config_dir) is None


# ---------------------------------------------------------------------------
# Malformed config -- the function raises DiscordBotConfigError
# ---------------------------------------------------------------------------


def test_malformed_upgrade_toml_raises(tmp_path: Path) -> None:
    """A malformed upgrade.toml surfaces as DiscordBotConfigError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_text("not = valid = toml\n", encoding="utf-8")
    with pytest.raises(DiscordBotConfigError, match="could not read"):
        load_discord_bot_config(config_dir)


def test_discord_section_not_a_table_raises(tmp_path: Path) -> None:
    """A scalar [discord] value is a configuration error."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_text('discord = "scalar"\n', encoding="utf-8")
    with pytest.raises(DiscordBotConfigError, match="must be a table"):
        load_discord_bot_config(config_dir)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_config_loads(tmp_path: Path) -> None:
    """A complete [discord] section plus token produces a valid config."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=real-token\n")
    cfg = load_discord_bot_config(config_dir)
    assert cfg is not None
    assert cfg.bot_token == "real-token"
    assert cfg.application_id == "1234567890123456789"
    assert cfg.public_key == _hex("a")
    assert cfg.guild_id == "9876543210987654321"
    assert cfg.channel_id == "1122334455667788990"
    assert cfg.allowlist_roles == ("1111111111111111111", "2222222222222222222")
    assert cfg.allowlist_users == ("3333333333333333333",)


def test_token_is_stripped_of_surrounding_whitespace(tmp_path: Path) -> None:
    """A token with leading/trailing spaces is stripped to the bare value."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=  abc  \n")
    cfg = load_discord_bot_config(config_dir)
    assert cfg is not None
    assert cfg.bot_token == "abc"


# ---------------------------------------------------------------------------
# Validation -- missing or malformed required keys
# ---------------------------------------------------------------------------


def _config_with_section_and_env(tmp_path: Path, section: dict) -> Path:
    """Write a section + token .env and return config_dir."""
    return _setup(tmp_path, section=section, env_body="DISCORD_BOT_TOKEN=abc\n")


def test_missing_application_id_raises(tmp_path: Path) -> None:
    """A missing application_id raises DiscordBotConfigError."""
    section = _full_discord_section()
    del section["application_id"]
    with pytest.raises(DiscordBotConfigError, match="application_id"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_missing_public_key_raises(tmp_path: Path) -> None:
    """A missing public_key raises DiscordBotConfigError."""
    section = _full_discord_section()
    del section["public_key"]
    with pytest.raises(DiscordBotConfigError, match="public_key"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_public_key_not_hex_raises(tmp_path: Path) -> None:
    """A public_key that is not 64 lowercase hex chars raises."""
    section = _full_discord_section()
    section["public_key"] = "not-a-key"
    with pytest.raises(DiscordBotConfigError, match="public_key"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_public_key_uppercase_raises(tmp_path: Path) -> None:
    """A public_key in uppercase hex raises (lowercase is normative)."""
    section = _full_discord_section()
    section["public_key"] = _hex("A")
    with pytest.raises(DiscordBotConfigError, match="public_key"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_missing_guild_id_raises(tmp_path: Path) -> None:
    """A missing guild_id raises DiscordBotConfigError."""
    section = _full_discord_section()
    del section["guild_id"]
    with pytest.raises(DiscordBotConfigError, match="guild_id"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_missing_channel_id_raises(tmp_path: Path) -> None:
    """A missing channel_id raises DiscordBotConfigError."""
    section = _full_discord_section()
    del section["channel_id"]
    with pytest.raises(DiscordBotConfigError, match="channel_id"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_application_id_not_a_snowflake_raises(tmp_path: Path) -> None:
    """A non-numeric application_id raises."""
    section = _full_discord_section()
    section["application_id"] = "abc"
    with pytest.raises(DiscordBotConfigError, match="application_id"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_guild_id_too_short_raises(tmp_path: Path) -> None:
    """A guild_id shorter than 17 digits raises."""
    section = _full_discord_section()
    section["guild_id"] = "123"
    with pytest.raises(DiscordBotConfigError, match="guild_id"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


# ---------------------------------------------------------------------------
# Allowlists -- optional
# ---------------------------------------------------------------------------


def test_missing_allowlist_roles_is_empty_tuple(tmp_path: Path) -> None:
    """Absent allowlist_roles is a valid config with an empty tuple."""
    section = _full_discord_section()
    del section["allowlist_roles"]
    cfg = load_discord_bot_config(_config_with_section_and_env(tmp_path, section))
    assert cfg is not None
    assert cfg.allowlist_roles == ()


def test_missing_allowlist_users_is_empty_tuple(tmp_path: Path) -> None:
    """Absent allowlist_users is a valid config with an empty tuple."""
    section = _full_discord_section()
    del section["allowlist_users"]
    cfg = load_discord_bot_config(_config_with_section_and_env(tmp_path, section))
    assert cfg is not None
    assert cfg.allowlist_users == ()


def test_allowlist_roles_not_a_list_raises(tmp_path: Path) -> None:
    """A non-list allowlist_roles raises."""
    section = _full_discord_section()
    section["allowlist_roles"] = "not-a-list"
    with pytest.raises(DiscordBotConfigError, match="allowlist_roles"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_allowlist_roles_with_non_string_raises(tmp_path: Path) -> None:
    """A list with non-string elements raises."""
    section = _full_discord_section()
    section["allowlist_roles"] = ["1111111111111111111", 42]
    with pytest.raises(DiscordBotConfigError, match="allowlist_roles"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_allowlist_roles_with_non_snowflake_raises(tmp_path: Path) -> None:
    """A string that is not a snowflake raises."""
    section = _full_discord_section()
    section["allowlist_roles"] = ["role-name"]
    with pytest.raises(DiscordBotConfigError, match="allowlist_roles"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


def test_allowlist_users_not_a_list_raises(tmp_path: Path) -> None:
    """A non-list allowlist_users raises."""
    section = _full_discord_section()
    section["allowlist_users"] = {"a": "b"}
    with pytest.raises(DiscordBotConfigError, match="allowlist_users"):
        load_discord_bot_config(_config_with_section_and_env(tmp_path, section))


# ---------------------------------------------------------------------------
# Credential non-disclosure
# ---------------------------------------------------------------------------


def test_repr_does_not_contain_token(tmp_path: Path) -> None:
    """repr(DiscordBotConfig) must not include the token."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=secret-token-value\n")
    cfg = load_discord_bot_config(config_dir)
    assert cfg is not None
    assert "secret-token-value" not in repr(cfg)
    assert "secret-token-value" not in str(cfg)


def test_dataclass_is_frozen(tmp_path: Path) -> None:
    """The config dataclass rejects field assignment."""
    config_dir = _setup(tmp_path, section=_full_discord_section(), env_body="DISCORD_BOT_TOKEN=abc\n")
    cfg = load_discord_bot_config(config_dir)
    assert cfg is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.bot_token = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_config_error_is_a_bot_error() -> None:
    """DiscordBotConfigError inherits from DiscordBotError."""
    assert issubclass(DiscordBotConfigError, DiscordBotError)
