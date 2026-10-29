# tests/installer/test_installer.py

"""Tests for minecraft.installer.installer."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minecraft.installer import installer as installer_mod
from minecraft.installer.errors import ComposeError, InstallerError
from minecraft.installer.installer import install, rotate
from minecraft.installer.secrets import read_secret_file

_LOG = logging.getLogger("test_installer")
_LOG.addHandler(logging.NullHandler())
_LOG.propagate = False


def _write_dev_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.dev.yml").write_text(
        "services:\n"
        "  mc-latest:\n"
        "    container_name: mc-latest\n"
        "    volumes:\n"
        "      - ./latest-data:/data\n"
        "      - ./dev-mods:/data/mods\n"
        "  mc-candidate:\n"
        "    container_name: mc-candidate\n"
        "    volumes:\n"
        "      - ./candidate-data:/data\n"
        "      - ./dev-mods:/data/mods\n",
        encoding="utf-8",
    )


def _write_prod_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  survival:\n"
        "    container_name: mc-survival\n"
        "    volumes:\n"
        "      - ./data/survival:/data\n"
        "      - ./shared/mods:/data/mods\n"
        "  creative:\n"
        "    container_name: mc-creative\n"
        "    volumes:\n"
        "      - ./data/creative:/data\n"
        "      - ./shared/mods:/data/mods\n",
        encoding="utf-8",
    )


def _secret_value_from(props_text: str, key: str) -> str:
    """Return the value of ``key`` in a server.properties body."""
    return next(line.split("=", 1)[1] for line in props_text.splitlines() if line.startswith(f"{key}="))


def test_install_dev_creates_secrets_and_props(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install-dev creates the RCON secret and writes server.properties."""
    _write_dev_compose(tmp_path)
    monkeypatch.setattr(installer_mod, "bring_up", lambda *a, **k: None)

    rc = install("dev", tmp_path, tmp_path / "config.d", up=False, log=_LOG)
    assert rc == 0

    secret_path = tmp_path / "secrets" / "rcon_password"
    assert secret_path.is_file()
    secret = read_secret_file(secret_path)
    assert secret is not None and len(secret) == 64

    assert (tmp_path / "latest-data" / "server.properties").is_file()
    assert (tmp_path / "candidate-data" / "server.properties").is_file()


def test_install_dev_calls_bring_up_when_up_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install(up=True) calls bring_up with the derived compose file."""
    _write_dev_compose(tmp_path)
    called: list[Path] = []
    monkeypatch.setattr(installer_mod, "bring_up", lambda path, *, log: called.append(path))

    install("dev", tmp_path, tmp_path / "config.d", up=True, log=_LOG)
    assert called == [tmp_path / "docker-compose.dev.yml"]


def test_install_dev_does_not_call_bring_up_when_up_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install(up=False) skips bring_up."""
    _write_dev_compose(tmp_path)
    called: list[Path] = []
    monkeypatch.setattr(installer_mod, "bring_up", lambda path, *, log: called.append(path))

    install("dev", tmp_path, tmp_path / "config.d", up=False, log=_LOG)
    assert called == []


def test_install_invalid_mode(tmp_path: Path) -> None:
    """An invalid mode is an InstallerError."""
    with pytest.raises(InstallerError, match="invalid mode"):
        install("staging", tmp_path, tmp_path / "config.d", up=False, log=_LOG)


def test_install_missing_compose(tmp_path: Path) -> None:
    """A missing compose file is a ComposeError."""
    with pytest.raises(ComposeError, match="not found"):
        install("dev", tmp_path, tmp_path / "config.d", up=False, log=_LOG)


def test_install_prod_writes_props(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install-prod writes server.properties at each derived prod root."""
    _write_prod_compose(tmp_path)
    monkeypatch.setattr(installer_mod, "bring_up", lambda *a, **k: None)

    install("prod", tmp_path, tmp_path / "config.d", up=False, log=_LOG)
    props = tmp_path / "data" / "survival" / "server.properties"
    assert props.is_file()
    assert "enable-rcon=true" in props.read_text(encoding="utf-8")
    creative = tmp_path / "data" / "creative" / "server.properties"
    assert creative.is_file()


def test_install_uses_existing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing valid secret is reused, not regenerated."""
    _write_dev_compose(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "rcon_password").write_text("deadbeef\n", encoding="utf-8")

    monkeypatch.setattr(installer_mod, "bring_up", lambda *a, **k: None)

    install("dev", tmp_path, tmp_path / "config.d", up=False, log=_LOG)
    assert read_secret_file(secrets_dir / "rcon_password") == "deadbeef"
    props = (tmp_path / "latest-data" / "server.properties").read_text(encoding="utf-8")
    assert "rcon.password=deadbeef" in props


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def test_rotate_regenerates_rcon(tmp_path: Path) -> None:
    """Rotate regenerates the RCON secret and updates server.properties."""
    _write_dev_compose(tmp_path)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "rcon_password").write_text("deadbeef\n", encoding="utf-8")

    rc = rotate(tmp_path, tmp_path / "config.d", log=_LOG)
    assert rc == 0
    new_secret = read_secret_file(secrets_dir / "rcon_password")
    assert new_secret is not None
    assert new_secret != "deadbeef"
    props = (tmp_path / "latest-data" / "server.properties").read_text(encoding="utf-8")
    assert f"rcon.password={new_secret}" in props


def test_rotate_writes_to_both_modes(tmp_path: Path) -> None:
    """Rotate updates server.properties for both prod and dev."""
    _write_dev_compose(tmp_path)
    _write_prod_compose(tmp_path)

    rc = rotate(tmp_path, tmp_path / "config.d", log=_LOG)
    assert rc == 0
    dev_props = (tmp_path / "latest-data" / "server.properties").read_text(encoding="utf-8")
    prod_props = (tmp_path / "data" / "survival" / "server.properties").read_text(encoding="utf-8")
    assert "enable-rcon=true" in dev_props
    assert "enable-rcon=true" in prod_props
    dev_secret = _secret_value_from(dev_props, "rcon.password")
    prod_secret = _secret_value_from(prod_props, "rcon.password")
    assert dev_secret == prod_secret
