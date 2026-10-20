# tests/discord_bot/test_daemon_config.py

"""Discord bot daemon config loader tests (T3a)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from minecraft.discord_bot.daemon_config import (
    load_discord_daemon_config,
)
from minecraft.discord_bot.errors import DiscordBotConfigError


def _full_daemon_section() -> dict:
    """Return a fully-populated, valid ``[daemon]`` section body."""
    return {
        "state_dir": "var/discord-bot",
        "lock_path": "var/discord-bot/bot.lock",
        "http_host": "127.0.0.1",
        "http_port": 8766,
        "notify_bearer_token_key": "DISCORD_BOT_NOTIFY_TOKEN",
        "cli_binary": "minecraft-upgrade",
        "cli_timeout_seconds": 45,
    }


def _write_config(config_dir: Path, body: dict) -> None:
    """Write ``discord_bot.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "discord_bot.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))


def _write_env(config_dir: Path, body: str) -> None:
    """Write ``.env`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(body, encoding="utf-8")


def _setup(tmp_path: Path, *, section: dict | None = None, env_body: str | None = None) -> Path:
    """Create ``config.d`` with optional discord_bot.toml and .env. Return config_dir."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    if section is not None:
        _write_config(config_dir, {"daemon": section})
    if env_body is not None:
        _write_env(config_dir, env_body)
    return config_dir


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path) -> None:
    """No discord_bot.toml raises DiscordBotConfigError."""
    config_dir = _setup(tmp_path)
    with pytest.raises(DiscordBotConfigError, match="not found"):
        load_discord_daemon_config(config_dir)


def test_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML raises DiscordBotConfigError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "discord_bot.toml").write_text("not = valid = toml\n", encoding="utf-8")
    with pytest.raises(DiscordBotConfigError, match="could not read"):
        load_discord_daemon_config(config_dir)


def test_missing_daemon_section_raises(tmp_path: Path) -> None:
    """A file without [daemon] raises DiscordBotConfigError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_config(config_dir, {"other": {"key": "value"}})
    with pytest.raises(DiscordBotConfigError, match="missing \\[daemon\\] section"):
        load_discord_daemon_config(config_dir)


def test_daemon_section_not_table_raises(tmp_path: Path) -> None:
    """A scalar [daemon] value raises DiscordBotConfigError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "discord_bot.toml").write_text('daemon = "scalar"\n', encoding="utf-8")
    with pytest.raises(DiscordBotConfigError, match="must be a table"):
        load_discord_daemon_config(config_dir)


def test_missing_state_dir_raises(tmp_path: Path) -> None:
    """A missing state_dir raises DiscordBotConfigError."""
    section = _full_daemon_section()
    del section["state_dir"]
    config_dir = _setup(tmp_path, section=section)
    with pytest.raises(DiscordBotConfigError, match="state_dir"):
        load_discord_daemon_config(config_dir)


def test_missing_lock_path_raises(tmp_path: Path) -> None:
    """A missing lock_path raises DiscordBotConfigError."""
    section = _full_daemon_section()
    del section["lock_path"]
    config_dir = _setup(tmp_path, section=section)
    with pytest.raises(DiscordBotConfigError, match="lock_path"):
        load_discord_daemon_config(config_dir)


# ---------------------------------------------------------------------------
# Happy path and defaults
# ---------------------------------------------------------------------------


def test_full_section_loads(tmp_path: Path) -> None:
    """A complete section produces a fully-populated config."""
    config_dir = _setup(tmp_path, section=_full_daemon_section(), env_body="DISCORD_BOT_NOTIFY_TOKEN=notify-secret\n")
    cfg = load_discord_daemon_config(config_dir)
    assert cfg.state_dir == (tmp_path / "var" / "discord-bot").resolve()
    assert cfg.lock_path == (tmp_path / "var" / "discord-bot" / "bot.lock").resolve()
    assert cfg.http_host == "127.0.0.1"
    assert cfg.http_port == 8766
    assert cfg.notify_bearer_token == "notify-secret"
    assert cfg.cli_binary == "minecraft-upgrade"
    assert cfg.cli_timeout_seconds == 45
    assert cfg.project_root == tmp_path
    assert cfg.daemon_version == "0.2.1"


def test_optional_fields_use_defaults(tmp_path: Path) -> None:
    """Absent optional fields use documented defaults."""
    section = {"state_dir": "var/state", "lock_path": "var/state/bot.lock"}
    config_dir = _setup(tmp_path, section=section)
    cfg = load_discord_daemon_config(config_dir)
    assert cfg.http_host == "127.0.0.1"
    assert cfg.http_port == 8766
    assert cfg.notify_bearer_token is None
    assert cfg.cli_binary == "minecraft-upgrade"
    assert cfg.cli_timeout_seconds == 60


def test_absolute_paths_preserved(tmp_path: Path) -> None:
    """Absolute paths in the config are used verbatim."""
    abs_state = tmp_path / "elsewhere" / "state"
    abs_lock = tmp_path / "elsewhere" / "lock"
    section = {"state_dir": str(abs_state), "lock_path": str(abs_lock)}
    config_dir = _setup(tmp_path, section=section)
    cfg = load_discord_daemon_config(config_dir)
    assert cfg.state_dir == abs_state
    assert cfg.lock_path == abs_lock


def test_daemon_version_kwarg_threaded_through(tmp_path: Path) -> None:
    """The ``daemon_version`` kwarg overrides the built-in default."""
    config_dir = _setup(tmp_path, section=_full_daemon_section(), env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    cfg = load_discord_daemon_config(config_dir, daemon_version="9.9.9")
    assert cfg.daemon_version == "9.9.9"


# ---------------------------------------------------------------------------
# http_port validation
# ---------------------------------------------------------------------------


def test_http_port_non_int_raises(tmp_path: Path) -> None:
    """A non-integer http_port raises."""
    section = _full_daemon_section()
    section["http_port"] = "not-a-port"
    config_dir = _setup(tmp_path, section=section, env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    with pytest.raises(DiscordBotConfigError, match="http_port"):
        load_discord_daemon_config(config_dir)


def test_http_port_below_range_raises(tmp_path: Path) -> None:
    """http_port below 1 raises."""
    section = _full_daemon_section()
    section["http_port"] = 0
    config_dir = _setup(tmp_path, section=section, env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    with pytest.raises(DiscordBotConfigError, match="http_port"):
        load_discord_daemon_config(config_dir)


def test_http_port_above_range_raises(tmp_path: Path) -> None:
    """http_port above 65535 raises."""
    section = _full_daemon_section()
    section["http_port"] = 70000
    config_dir = _setup(tmp_path, section=section, env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    with pytest.raises(DiscordBotConfigError, match="http_port"):
        load_discord_daemon_config(config_dir)


# ---------------------------------------------------------------------------
# cli_timeout_seconds validation
# ---------------------------------------------------------------------------


def test_cli_timeout_zero_raises(tmp_path: Path) -> None:
    """A zero cli_timeout_seconds raises."""
    section = _full_daemon_section()
    section["cli_timeout_seconds"] = 0
    config_dir = _setup(tmp_path, section=section, env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    with pytest.raises(DiscordBotConfigError, match="cli_timeout_seconds"):
        load_discord_daemon_config(config_dir)


def test_cli_timeout_negative_raises(tmp_path: Path) -> None:
    """A negative cli_timeout_seconds raises."""
    section = _full_daemon_section()
    section["cli_timeout_seconds"] = -5
    config_dir = _setup(tmp_path, section=section, env_body="DISCORD_BOT_NOTIFY_TOKEN=tok\n")
    with pytest.raises(DiscordBotConfigError, match="cli_timeout_seconds"):
        load_discord_daemon_config(config_dir)


# ---------------------------------------------------------------------------
# Notify bearer token
# ---------------------------------------------------------------------------


def test_notify_key_absent_yields_none_token(tmp_path: Path) -> None:
    """No notify_bearer_token_key means notify endpoint is disabled."""
    section = {"state_dir": "var/state", "lock_path": "var/state/bot.lock"}
    config_dir = _setup(tmp_path, section=section)
    cfg = load_discord_daemon_config(config_dir)
    assert cfg.notify_bearer_token is None


def test_notify_key_set_env_missing_raises(tmp_path: Path) -> None:
    """A set notify_bearer_token_key with no .env raises."""
    config_dir = _setup(tmp_path, section=_full_daemon_section())
    with pytest.raises(DiscordBotConfigError, match="does not exist"):
        load_discord_daemon_config(config_dir)


def test_notify_key_set_env_lacks_key_raises(tmp_path: Path) -> None:
    """A set notify_bearer_token_key with an unrelated .env raises."""
    config_dir = _setup(tmp_path, section=_full_daemon_section(), env_body="OTHER=value\n")
    with pytest.raises(DiscordBotConfigError, match="no non-empty value"):
        load_discord_daemon_config(config_dir)


def test_notify_key_set_env_blank_raises(tmp_path: Path) -> None:
    """A set notify_bearer_token_key with a blank .env value raises."""
    config_dir = _setup(tmp_path, section=_full_daemon_section(), env_body="DISCORD_BOT_NOTIFY_TOKEN=\n")
    with pytest.raises(DiscordBotConfigError, match="no non-empty value"):
        load_discord_daemon_config(config_dir)


# ---------------------------------------------------------------------------
# Credential non-disclosure
# ---------------------------------------------------------------------------


def test_repr_does_not_contain_notify_token(tmp_path: Path) -> None:
    """repr(DiscordDaemonConfig) must not include the notify token."""
    config_dir = _setup(tmp_path, section=_full_daemon_section(), env_body="DISCORD_BOT_NOTIFY_TOKEN=secret-value\n")
    cfg = load_discord_daemon_config(config_dir)
    assert "secret-value" not in repr(cfg)
    assert "secret-value" not in str(cfg)
