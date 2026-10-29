# tests/installer/test_rcon.py

"""Tests for minecraft.installer.rcon."""

from __future__ import annotations

import logging
from pathlib import Path

from minecraft.installer.rcon import configure_rcon, configure_server_properties

_LOG = logging.getLogger("test_installer_rcon")


def test_configure_creates_missing_file(tmp_path: Path) -> None:
    """A missing server.properties is created with the three RCON keys."""
    p = tmp_path / "server.properties"
    changed = configure_server_properties(p, "secret123")
    assert changed is True
    assert p.is_file()
    body = p.read_text(encoding="utf-8")
    assert "enable-rcon=true\n" in body
    assert "rcon.password=secret123\n" in body
    assert "rcon.port=25575\n" in body


def test_configure_updates_existing_keys(tmp_path: Path) -> None:
    """Existing keys are overwritten in place."""
    p = tmp_path / "server.properties"
    p.write_text(
        "motd=hello\nenable-rcon=false\nrcon.password=oldpw\nrcon.port=25575\n",
        encoding="utf-8",
    )
    changed = configure_server_properties(p, "newpw")
    assert changed is True
    body = p.read_text(encoding="utf-8")
    assert "motd=hello\n" in body
    assert "enable-rcon=true\n" in body
    assert "rcon.password=newpw\n" in body


def test_configure_is_idempotent(tmp_path: Path) -> None:
    """A second call with the same secret is a no-op."""
    p = tmp_path / "server.properties"
    configure_server_properties(p, "secret")
    changed = configure_server_properties(p, "secret")
    assert changed is False


def test_configure_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent directories are created."""
    p = tmp_path / "data" / "survival" / "server.properties"
    configure_server_properties(p, "x")
    assert p.is_file()


def test_configure_rcon_multiple_instances(tmp_path: Path) -> None:
    """configure_rcon walks a list of (name, root) pairs."""
    roots = [
        ("mc-latest", tmp_path / "latest-data"),
        ("mc-candidate", tmp_path / "candidate-data"),
    ]
    touched = configure_rcon(roots, "the-secret", log=_LOG)
    assert len(touched) == 2
    for _, root in roots:
        body = (root / "server.properties").read_text(encoding="utf-8")
        assert "rcon.password=the-secret\n" in body


def test_configure_rcon_skips_unchanged(tmp_path: Path) -> None:
    """A second call to configure_rcon reports no changes."""
    roots = [("mc-latest", tmp_path / "latest-data")]
    configure_rcon(roots, "the-secret", log=_LOG)
    touched = configure_rcon(roots, "the-secret", log=_LOG)
    assert touched == []
