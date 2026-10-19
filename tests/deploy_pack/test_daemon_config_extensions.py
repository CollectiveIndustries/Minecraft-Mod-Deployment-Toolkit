# tests/deploy_pack/test_daemon_config_extensions.py

"""Additional tests for the Slice 13 config fields (pull source).

Verifies that ``source_host``, ``source_user``, ``source_path``, and
``ssh_key`` are read from ``[daemon]``, default to empty/default values
when absent, and that ``ssh_key`` expands a leading ``~`` so the operator
can write ``~/.ssh/id_...`` without worrying about CWD.
"""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack.daemon.config import load_config


def _write_config(config_dir: Path, body: str) -> None:
    """Write ``deploy_pack.toml`` under ``config_dir``."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "deploy_pack.toml").write_text(body, encoding="utf-8")


def test_pull_source_defaults(tmp_path: Path) -> None:
    """An empty config yields empty source_host and sensible other defaults."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    cfg = load_config(config_dir)
    assert cfg.source_host == ""
    assert cfg.source_user == "deploy"
    assert cfg.source_path == Path("/srv/minecraft-dev/www/releases")
    assert cfg.ssh_key == Path("~/.ssh/id_deploy_warehouse_ro").expanduser()


def test_pull_source_overrides(tmp_path: Path) -> None:
    """Explicit [daemon] values override the defaults."""
    config_dir = tmp_path / "config.d"
    _write_config(
        config_dir,
        '[daemon]\nsource_host = "dev.internal"\nsource_user = "ops"\nsource_path = "/srv/releases"\nssh_key = "~/.ssh/custom_key"\n',
    )
    cfg = load_config(config_dir)
    assert cfg.source_host == "dev.internal"
    assert cfg.source_user == "ops"
    assert cfg.source_path == Path("/srv/releases")
    assert cfg.ssh_key == Path("~/.ssh/custom_key").expanduser()


def test_pull_source_absolute_ssh_key_preserved(tmp_path: Path) -> None:
    """An absolute ssh_key path is returned verbatim."""
    config_dir = tmp_path / "config.d"
    _write_config(config_dir, '[daemon]\nssh_key = "/opt/keys/deploy.pem"\n')
    cfg = load_config(config_dir)
    assert cfg.ssh_key == Path("/opt/keys/deploy.pem")
