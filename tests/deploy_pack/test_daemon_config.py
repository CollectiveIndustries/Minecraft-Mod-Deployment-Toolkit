# tests/deploy_pack/test_daemon_config.py

"""Minecraft daemon config loading tests."""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack.daemon.config import load_config


def test_defaults_when_toml_missing(tmp_path: Path):
    """Without deploy_pack.toml, defaults are used."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    config = load_config(config_dir)
    assert config.daemon_version == "1.3.0"
    assert config.http_host == "127.0.0.1"
    assert config.http_port == 8765
    assert config.bearer_token_env == "MINECRAFT_DAEMON_TOKEN"
    assert config.state_dir.is_absolute()
    assert config.project_root == config_dir.parent


def test_toml_overrides(tmp_path: Path):
    """Values in [daemon] override defaults."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        '[daemon]\nhttp_host = "0.0.0.0"\nhttp_port = 9999\n',
        encoding="utf-8",
    )
    config = load_config(config_dir)
    assert config.http_host == "0.0.0.0"
    assert config.http_port == 9999


def test_relative_paths_resolve_against_project_root(tmp_path: Path):
    """Relative path values resolve against the project root."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text('[daemon]\nstate_dir = "var/state"\n', encoding="utf-8")
    config = load_config(config_dir)
    assert config.state_dir == tmp_path / "var" / "state"


def test_tailscale_cidrs_parsed(tmp_path: Path):
    """Comma-separated CIDRs become a tuple."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        '[daemon]\ntailscale_cidrs = "10.0.0.0/8,192.168.0.0/16"\n',
        encoding="utf-8",
    )
    config = load_config(config_dir)
    assert config.tailscale_cidrs == ("10.0.0.0/8", "192.168.0.0/16")


def test_malformed_port_falls_back(tmp_path: Path):
    """A non-integer http_port falls back to the default."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text('[daemon]\nhttp_port = "not-a-number"\n', encoding="utf-8")
    config = load_config(config_dir)
    assert config.http_port == 8765


def test_daemon_version_param(tmp_path: Path):
    """The daemon_version argument is threaded through."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    config = load_config(config_dir, daemon_version="9.9.9")
    assert config.daemon_version == "9.9.9"
