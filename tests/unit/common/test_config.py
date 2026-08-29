# tests/unit/common/test_config.py
from pathlib import Path

from src.minecraft.common import config


def test_find_config_file_found(temp_dir):
    (temp_dir / "deploy_pack.toml").touch()
    (temp_dir / "deploy_pack.yaml").touch()
    result = config.find_config_file(temp_dir, "deploy_pack")
    assert result == temp_dir / "deploy_pack.toml"


def test_find_config_file_not_found(temp_dir):
    result = config.find_config_file(temp_dir, "deploy_pack")
    assert result is None


def test_load_config_with_file(temp_dir):
    config_file = temp_dir / "deploy_pack.toml"
    config_file.write_text("key = 'value'\nother = 42")
    cfg = config.load_config(
        config_dir=temp_dir,
        base_name="deploy_pack",
        env_prefix="TEST",
        cli_args=["--foo=bar"],
        env_file=temp_dir / ".env",
    )
    assert cfg.get("key") == "value"
    assert cfg.get("other") == 42
    assert cfg.get("foo") == "bar"
    # env_prefix with no vars does nothing
    assert cfg.get("something") is None


def test_load_config_with_env_override(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_HOST", "localhost")
    monkeypatch.setenv("TEST_DEBUG", "true")
    cfg = config.load_config(
        config_dir=Path("."),
        base_name="deploy_pack",
        env_prefix="TEST",
        cli_args=[],
    )
    assert cfg.get("database.host") == "localhost"
    assert cfg.get("debug") is True


def test_load_config_with_cli():
    cfg = config.load_config(
        config_dir=Path("."),
        base_name="deploy_pack",
        env_prefix="",
        cli_args=["--database.host", "127.0.0.1", "--debug"],
    )
    assert cfg.get("database.host") == "127.0.0.1"
    assert cfg.get("debug") is True
