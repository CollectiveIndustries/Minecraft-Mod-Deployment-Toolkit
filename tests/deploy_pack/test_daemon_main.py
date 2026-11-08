# tests/deploy_pack/test_daemon_main.py

"""Minecraft daemon build_daemon sanity tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from DaemonCore import Daemon

from minecraft.deploy_pack.daemon.config import load_config
from minecraft.deploy_pack.daemon.main import build_daemon


def _make_config_dir(tmp_path: Path) -> Path:
    """Create a config.d directory with a valid deploy_pack.toml."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n'
        f'audit_path = "{tmp_path / "audit.jsonl"}"\n'
        f'lock_path = "{tmp_path / "daemon.lock"}"\n'
        f'idempotency_path = "{tmp_path / "idem.sqlite"}"\n',
        encoding="utf-8",
    )
    return config_dir


def test_build_daemon_returns_daemon(tmp_path: Path):
    """build_daemon returns a Daemon instance with a plugin attached."""
    config_dir = _make_config_dir(tmp_path)
    (config_dir / ".env").write_text("MINECRAFT_DAEMON_TOKEN=test-token\n", encoding="utf-8")
    config = load_config(config_dir)
    daemon = build_daemon(config, config_dir=config_dir)
    assert isinstance(daemon, Daemon)


def test_build_daemon_without_token_raises(tmp_path: Path):
    """An empty bearer token in .env raises at build time."""
    config_dir = _make_config_dir(tmp_path)
    (config_dir / ".env").write_text("", encoding="utf-8")
    config = load_config(config_dir)
    with pytest.raises(ValueError, match="has no value"):
        build_daemon(config, config_dir=config_dir)
