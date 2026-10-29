# tests/installer/test_compose.py

"""Tests for minecraft.installer.compose."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minecraft.installer import compose as compose_mod
from minecraft.installer.compose import bring_up, derive_instance_roots
from minecraft.installer.errors import ComposeError

_LOG = logging.getLogger("test_installer_compose")


def _write_dev_compose(tmp_path: Path) -> Path:
    compose = tmp_path / "docker-compose.dev.yml"
    compose.write_text(
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
        "      - ./dev-mods:/data/mods\n"
        "  mc-artifact-store:\n"
        "    container_name: mc-artifact-store\n"
        "    volumes:\n"
        "      - ./sync/downloads:/usr/share/nginx/html:ro\n",
        encoding="utf-8",
    )
    return compose


def test_derive_instance_roots_dev(tmp_path: Path) -> None:
    """Both /data services are returned; the artifact-store is skipped."""
    compose = _write_dev_compose(tmp_path)
    roots = derive_instance_roots(compose, log=_LOG)
    names = [name for name, _ in roots]
    assert "mc-latest" in names
    assert "mc-candidate" in names
    assert "mc-artifact-store" not in names
    by_name = dict(roots)
    assert by_name["mc-latest"] == (tmp_path / "latest-data").resolve()


def test_derive_instance_roots_missing_compose(tmp_path: Path) -> None:
    """A missing compose file is a ComposeError."""
    with pytest.raises(ComposeError, match="could not load"):
        derive_instance_roots(tmp_path / "nope.yml", log=_LOG)


def test_derive_instance_roots_no_data_bind(tmp_path: Path) -> None:
    """A service without a /data bind is skipped."""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  nginx:\n    container_name: nginx\n    volumes:\n      - ./www:/usr/share/nginx/html\n",
        encoding="utf-8",
    )
    roots = derive_instance_roots(compose, log=_LOG)
    assert roots == []


def test_bring_up_runs_docker_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bring_up invokes docker compose with the expected args."""
    compose = tmp_path / "docker-compose.dev.yml"
    compose.write_text("services: {}\n", encoding="utf-8")

    calls: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _FakeResult()

    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    bring_up(compose, log=_LOG)
    assert len(calls) == 1
    assert calls[0][:5] == [
        "docker",
        "compose",
        "-f",
        str(compose),
        "up",
    ]
    assert "-d" in calls[0]


def test_bring_up_missing_compose(tmp_path: Path) -> None:
    """A missing compose file is a ComposeError before docker is invoked."""
    with pytest.raises(ComposeError, match="not found"):
        bring_up(tmp_path / "nope.yml", log=_LOG)


def test_bring_up_docker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit is a ComposeError with stderr in the message."""
    compose = tmp_path / "docker-compose.dev.yml"
    compose.write_text("services: {}\n", encoding="utf-8")

    class _FakeResult:
        returncode = 1
        stderr = "permission denied"
        stdout = ""

    def fake_run(args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    with pytest.raises(ComposeError, match="permission denied"):
        bring_up(compose, log=_LOG)


def test_bring_up_docker_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError from subprocess is a ComposeError."""
    compose = tmp_path / "docker-compose.dev.yml"
    compose.write_text("services: {}\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        raise FileNotFoundError("docker: command not found")

    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    with pytest.raises(ComposeError, match="not installed"):
        bring_up(compose, log=_LOG)
