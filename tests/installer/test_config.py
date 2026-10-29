# tests/installer/test_config.py

"""Tests for minecraft.installer.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.installer.config import load_installer_config
from minecraft.installer.errors import ConfigError


def _write_toml(config_dir: Path, body: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "deploy_pack.toml").write_text(body, encoding="utf-8")


def test_defaults_when_toml_missing(tmp_path: Path) -> None:
    """No deploy_pack.toml at all -> all defaults."""
    cfg = load_installer_config(tmp_path, tmp_path / "config.d")
    assert cfg.secrets_dir == (tmp_path / "secrets").resolve()
    assert cfg.rcon_secret_name == "rcon_password"
    assert cfg.velocity is None


def test_empty_installer_section(tmp_path: Path) -> None:
    """An empty [installer] section uses defaults."""
    config_dir = tmp_path / "config.d"
    _write_toml(config_dir, "[installer]\n")
    cfg = load_installer_config(tmp_path, config_dir)
    assert cfg.secrets_dir == (tmp_path / "secrets").resolve()
    assert cfg.rcon_secret_name == "rcon_password"
    assert cfg.velocity is None


def test_custom_secrets_dir(tmp_path: Path) -> None:
    """A custom secrets_dir is resolved against the project root."""
    config_dir = tmp_path / "config.d"
    _write_toml(config_dir, '[installer]\nsecrets_dir = "./custom-secrets"\n')
    cfg = load_installer_config(tmp_path, config_dir)
    assert cfg.secrets_dir == (tmp_path / "custom-secrets").resolve()


def test_absolute_secrets_dir(tmp_path: Path) -> None:
    """An absolute secrets_dir is used verbatim."""
    config_dir = tmp_path / "config.d"
    absolute = (tmp_path / "abs").resolve()
    _write_toml(config_dir, f'[installer]\nsecrets_dir = "{absolute}"\n')
    cfg = load_installer_config(tmp_path, config_dir)
    assert cfg.secrets_dir == absolute


def test_custom_rcon_secret_name(tmp_path: Path) -> None:
    """A custom rcon_secret_name is used."""
    config_dir = tmp_path / "config.d"
    _write_toml(config_dir, '[installer]\nrcon_secret_name = "my_secret"\n')
    cfg = load_installer_config(tmp_path, config_dir)
    assert cfg.rcon_secret_name == "my_secret"


def test_velocity_section(tmp_path: Path) -> None:
    """A [installer.velocity] section is parsed."""
    config_dir = tmp_path / "config.d"
    _write_toml(
        config_dir,
        "[installer.velocity]\n"
        'forwarding_secret_path = "./velocity/forwarding.secret"\n'
        'pcf_paths = ["./survival/config/pcf.toml", "./creative/config/pcf.toml"]\n',
    )
    cfg = load_installer_config(tmp_path, config_dir)
    assert cfg.velocity is not None
    assert cfg.velocity.forwarding_secret_path == (tmp_path / "velocity/forwarding.secret").resolve()
    assert cfg.velocity.pcf_paths == (
        (tmp_path / "survival/config/pcf.toml").resolve(),
        (tmp_path / "creative/config/pcf.toml").resolve(),
    )


def test_velocity_section_missing_secret_path(tmp_path: Path) -> None:
    """A velocity section without forwarding_secret_path is a ConfigError."""
    config_dir = tmp_path / "config.d"
    _write_toml(config_dir, '[installer.velocity]\npcf_paths = ["./a.toml"]\n')
    with pytest.raises(ConfigError, match="forwarding_secret_path"):
        load_installer_config(tmp_path, config_dir)


def test_velocity_section_empty_pcf_paths(tmp_path: Path) -> None:
    """A velocity section with empty pcf_paths is a ConfigError."""
    config_dir = tmp_path / "config.d"
    _write_toml(
        config_dir,
        '[installer.velocity]\nforwarding_secret_path = "./v.secret"\npcf_paths = []\n',
    )
    with pytest.raises(ConfigError, match="pcf_paths"):
        load_installer_config(tmp_path, config_dir)


def test_velocity_section_non_string_pcf_entry(tmp_path: Path) -> None:
    """A non-string pcf_paths entry is a ConfigError."""
    config_dir = tmp_path / "config.d"
    _write_toml(
        config_dir,
        '[installer.velocity]\nforwarding_secret_path = "./v.secret"\npcf_paths = [42]\n',
    )
    with pytest.raises(ConfigError, match="pcf_paths"):
        load_installer_config(tmp_path, config_dir)
