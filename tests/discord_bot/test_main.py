# tests/discord_bot/test_main.py

"""Bot daemon entry point tests (T3b)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w
from DaemonCore import Daemon

from minecraft.discord_bot.errors import DiscordBotConfigError
from minecraft.discord_bot.main import build_daemon, main


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _setup_full_config(tmp_path: Path) -> Path:
    """Write a valid [discord] section, [daemon] section, and .env. Return config_dir."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_bytes(
        tomli_w.dumps(
            {
                "discord": {
                    "application_id": "111111111111111111",
                    "public_key": _hex("a"),
                    "guild_id": "222222222222222222",
                    "channel_id": "333333333333333333",
                    "allowlist_users": ["444444444444444444"],
                }
            }
        ).encode()
    )
    (config_dir / "discord_bot.toml").write_bytes(
        tomli_w.dumps(
            {
                "daemon": {
                    "state_dir": "var/discord-bot",
                    "lock_path": "var/discord-bot/bot.lock",
                }
            }
        ).encode()
    )
    (config_dir / ".env").write_text("DISCORD_BOT_TOKEN=real-token\n", encoding="utf-8")
    return config_dir


# ---------------------------------------------------------------------------
# build_daemon
# ---------------------------------------------------------------------------


def test_build_daemon_without_bot_config_raises(tmp_path: Path) -> None:
    """A missing [discord] section raises RuntimeError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    with pytest.raises(RuntimeError, match="not configured"):
        build_daemon(config_dir=config_dir)


def test_build_daemon_with_invalid_daemon_config_raises(tmp_path: Path) -> None:
    """A [discord] section + token but no discord_bot.toml raises DiscordBotConfigError."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_bytes(
        tomli_w.dumps(
            {
                "discord": {
                    "application_id": "111111111111111111",
                    "public_key": _hex("a"),
                    "guild_id": "222222222222222222",
                    "channel_id": "333333333333333333",
                }
            }
        ).encode()
    )
    (config_dir / ".env").write_text("DISCORD_BOT_TOKEN=tok\n", encoding="utf-8")
    with pytest.raises(DiscordBotConfigError):
        build_daemon(config_dir=config_dir)


def test_build_daemon_returns_daemon_instance(tmp_path: Path) -> None:
    """A fully-configured daemon builds as a DaemonCore Daemon."""
    config_dir = _setup_full_config(tmp_path)
    daemon = build_daemon(config_dir=config_dir)
    assert isinstance(daemon, Daemon)


def test_build_daemon_daemon_version_threaded_through(tmp_path: Path) -> None:
    """The daemon_version kwarg is propagated to the built daemon."""
    config_dir = _setup_full_config(tmp_path)
    daemon = build_daemon(config_dir=config_dir, daemon_version="9.9.9")
    assert isinstance(daemon, Daemon)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_help_returns_zero() -> None:
    """``--help`` prints argparse help and returns 0."""
    assert main(["--help"]) == 0


def test_main_version_returns_zero() -> None:
    """``--version`` prints the version and returns 0."""
    assert main(["--version"]) == 0


def test_main_missing_config_dir_returns_3(tmp_path: Path) -> None:
    """A nonexistent config directory yields exit code 3."""
    nonexistent = tmp_path / "does-not-exist"
    assert main(["--config-dir", str(nonexistent)]) == 3
