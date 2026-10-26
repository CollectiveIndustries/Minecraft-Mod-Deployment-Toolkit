# tests/deploy_pack/test_config_model_notify_dispatch.py

"""Tests for ``[discord.notify_dispatch]`` in deploy_pack.toml (Slice 15 T3c)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w

from minecraft.deploy_pack.config_model import load_deployment_config
from minecraft.deploy_pack.errors import ConfigError


def _minimal_discord_section() -> dict:
    """Return the minimum ``[discord]`` section needed for config load."""
    return {
        "tags": {"player_roles": [], "operator_roles": []},
        "messages": {
            "live": {"template": "live {tool_version}"},
            "online": {"template": "online {tool_version}"},
            "failure": {"template": "fail {failure_stage} {error}"},
            "diagnostic": {"template": "diag {tool_version}"},
        },
    }


def _minimal_deploy_pack_body() -> dict:
    """Return the minimum deploy_pack body (paths + instances + discord)."""
    return {
        "sync_root": "./sync",
        "modpack_dir": "./sync/downloads",
        "output_filename": "pack_{date}.zip",
        "download_base_url": "http://example.com",
        "sync_mapping": {"config": "config"},
        "instances": {},
        "discord": _minimal_discord_section(),
    }


def _write_config(config_dir: Path, body: dict) -> None:
    """Write ``deploy_pack.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "deploy_pack.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))


def _write_env(config_dir: Path, body: str) -> None:
    """Write ``.env`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(body, encoding="utf-8")


def _setup(tmp_path: Path, *, body: dict, env_body: str | None = None) -> Path:
    """Write the config + optional .env; return config_dir."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, body)
    if env_body is not None:
        _write_env(config_dir, env_body)
    return config_dir


# ---------------------------------------------------------------------------
# Absent section
# ---------------------------------------------------------------------------


def test_absent_notify_dispatch_yields_none_url_and_token(tmp_path: Path) -> None:
    """Without ``[discord.notify_dispatch]``, both fields are None (webhook fallback)."""
    config_dir = _setup(tmp_path, body=_minimal_deploy_pack_body())
    config = load_deployment_config(config_dir)
    assert config.discord.bot_daemon_url is None
    assert config.discord.bot_daemon_token is None


# ---------------------------------------------------------------------------
# Missing required pieces
# ---------------------------------------------------------------------------


def test_missing_url_raises(tmp_path: Path) -> None:
    """Section present without ``bot_daemon_url`` raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {"bot_daemon_token_env": "NOTIFY_TOKEN"}
    config_dir = _setup(tmp_path, body=body, env_body="NOTIFY_TOKEN=secret\n")
    with pytest.raises(ConfigError, match="bot_daemon_url"):
        load_deployment_config(config_dir)


def test_missing_token_env_raises(tmp_path: Path) -> None:
    """Section present without ``bot_daemon_token_env`` raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {"bot_daemon_url": "http://127.0.0.1:8766"}
    config_dir = _setup(tmp_path, body=body, env_body="NOTIFY_TOKEN=secret\n")
    with pytest.raises(ConfigError, match="bot_daemon_token_env"):
        load_deployment_config(config_dir)


def test_missing_env_file_raises(tmp_path: Path) -> None:
    """Section present but no ``.env`` file raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {
        "bot_daemon_url": "http://127.0.0.1:8766",
        "bot_daemon_token_env": "NOTIFY_TOKEN",
    }
    config_dir = _setup(tmp_path, body=body)
    with pytest.raises(ConfigError, match="does not exist"):
        load_deployment_config(config_dir)


def test_missing_token_in_env_raises(tmp_path: Path) -> None:
    """Section present but the named key is absent from ``.env`` raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {
        "bot_daemon_url": "http://127.0.0.1:8766",
        "bot_daemon_token_env": "NOTIFY_TOKEN",
    }
    config_dir = _setup(tmp_path, body=body, env_body="OTHER=value\n")
    with pytest.raises(ConfigError, match="has no value"):
        load_deployment_config(config_dir)


def test_blank_token_in_env_raises(tmp_path: Path) -> None:
    """Section present but the named key has an empty value raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {
        "bot_daemon_url": "http://127.0.0.1:8766",
        "bot_daemon_token_env": "NOTIFY_TOKEN",
    }
    config_dir = _setup(tmp_path, body=body, env_body="NOTIFY_TOKEN=\n")
    with pytest.raises(ConfigError, match="has no value"):
        load_deployment_config(config_dir)


def test_notify_dispatch_section_not_a_table_raises(tmp_path: Path) -> None:
    """A non-table ``[discord.notify_dispatch]`` value raises ConfigError."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = "not-a-table"
    config_dir = _setup(tmp_path, body=body)
    with pytest.raises(ConfigError, match="must be a table"):
        load_deployment_config(config_dir)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_config_resolves_url_and_token(tmp_path: Path) -> None:
    """A complete section resolves both fields correctly."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {
        "bot_daemon_url": "http://127.0.0.1:8766",
        "bot_daemon_token_env": "NOTIFY_TOKEN",
    }
    config_dir = _setup(tmp_path, body=body, env_body="NOTIFY_TOKEN=secret-abc\n")
    config = load_deployment_config(config_dir)
    assert config.discord.bot_daemon_url == "http://127.0.0.1:8766"
    assert config.discord.bot_daemon_token == "secret-abc"


def test_token_repr_hidden(tmp_path: Path) -> None:
    """The token is excluded from ``repr(DiscordConfig)``."""
    body = _minimal_deploy_pack_body()
    body["discord"]["notify_dispatch"] = {
        "bot_daemon_url": "http://127.0.0.1:8766",
        "bot_daemon_token_env": "NOTIFY_TOKEN",
    }
    config_dir = _setup(tmp_path, body=body, env_body="NOTIFY_TOKEN=very-secret-value\n")
    config = load_deployment_config(config_dir)
    assert "very-secret-value" not in repr(config.discord)
