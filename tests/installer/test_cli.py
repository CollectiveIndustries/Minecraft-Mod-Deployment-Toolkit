# tests/installer/test_cli.py

"""Tests for minecraft.installer.cli."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.installer import cli
from minecraft.installer import installer as installer_mod


def _write_dev_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.dev.yml").write_text(
        "services:\n"
        "  mc-latest:\n"
        "    container_name: mc-latest\n"
        "    volumes:\n"
        "      - ./latest-data:/data\n"
        "  mc-candidate:\n"
        "    container_name: mc-candidate\n"
        "    volumes:\n"
        "      - ./candidate-data:/data\n",
        encoding="utf-8",
    )


def test_install_dev_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """install-dev returns 0 on success."""
    _write_dev_compose(tmp_path)
    rc = cli.install_dev(
        [
            "--project-root",
            str(tmp_path),
            "--config-dir",
            "config.d",
            "--no-up",
        ]
    )
    assert rc == 0


def test_install_dev_returns_error_on_failure(tmp_path: Path) -> None:
    """A missing compose file yields exit 1."""
    rc = cli.install_dev(["--project-root", str(tmp_path), "--no-up"])
    assert rc == 1


def test_install_prod_returns_error_on_missing_compose(tmp_path: Path) -> None:
    """install-prod with no compose file yields exit 1."""
    rc = cli.install_prod(["--project-root", str(tmp_path), "--no-up"])
    assert rc == 1


def test_rotate_secrets_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rotate-secrets returns 0 on success."""
    _write_dev_compose(tmp_path)
    rc = cli.rotate_secrets(["--project-root", str(tmp_path)])
    assert rc == 0


def test_install_dev_honors_no_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-up suppresses the docker compose call."""
    _write_dev_compose(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(installer_mod, "bring_up", lambda path, *, log: calls.append(path))
    rc = cli.install_dev(["--project-root", str(tmp_path), "--no-up"])
    assert rc == 0
    assert calls == []


def test_help_exits_zero() -> None:
    """--help prints usage and exits 0."""
    with pytest.raises(SystemExit) as ei:
        cli.install_dev(["--help"])
    assert ei.value.code == 0
