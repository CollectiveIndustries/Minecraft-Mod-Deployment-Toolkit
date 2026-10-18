# tests/modpack_upgrade/test_acknowledge.py

"""Acknowledge command unit tests (Resolution_Report Section 6.4, Section 15.1.2)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands.acknowledge import (
    _compute_compose_sha256,
    _get_confirmation,
    _now_utc,
    _present_losses,
    _resolve_compose_path,
    _write_acknowledgment,
    run,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.report import (
    Diagnostics,
    LostEntry,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    TargetRuntime,
)
from minecraft.modpack_upgrade.state import (
    STATUS_AWAITING_ACKNOWLEDGMENT,
    STATUS_RESOLVED,
    RunState,
    save_state,
)


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _hex_of(path: Path) -> str:
    """Return the SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_report() -> ResolutionReport:
    """Return a minimal valid ResolutionReport."""
    return ResolutionReport(
        run_id="abc",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="paused_awaiting_acknowledgment",
        inputs=ReportInputs(
            manifest_sha256=_hex("1"),
            resolution_inputs_sha256=_hex("2"),
            side_overrides_sha256=_hex("3"),
            release_mode="upgrade",
        ),
        target_runtime=TargetRuntime(minecraft_version="1.21.1", loader="forge", loader_version="52.0.1"),
        policy=ReportPolicy(selection="release_then_beta", no_version="ask"),
        seed=(),
        resolved=(),
        lost=(
            LostEntry(
                filename="gone.jar",
                provider="modrinth",
                project_id="gone",
                reason="no_compatible_version",
                detail="no versions",
            ),
        ),
        cascade=(),
        conflict=(),
        diagnostics=Diagnostics(),
    )


def _minimal_state(**overrides: object) -> RunState:
    """Return a minimal valid RunState."""
    base: dict[str, object] = {
        "run_id": "abc",
        "status": STATUS_AWAITING_ACKNOWLEDGMENT,
        "release_mode": "upgrade",
        "paused_at_utc": "2026-01-01T00:00:00Z",
        "report_sha256": _hex("a"),
        "resolution_inputs_sha256": _hex("b"),
        "manifest_path": "/m",
    }
    base.update(overrides)
    return RunState(**base)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_now_utc_format() -> None:
    """_now_utc returns ISO-8601 with Z suffix."""
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", _now_utc())


def test_resolve_compose_path_no_deploy_toml(tmp_path: Path) -> None:
    """No deploy_pack.toml means the default docker-compose.yml path."""
    assert _resolve_compose_path(tmp_path, tmp_path) == tmp_path / "docker-compose.yml"


def test_resolve_compose_path_custom(tmp_path: Path) -> None:
    """A custom compose_file in deploy_pack.toml is honored."""
    custom = tmp_path / "custom.yml"
    (tmp_path / "deploy_pack.toml").write_text(f'[docker]\ncompose_file = "{custom}"\n')
    assert _resolve_compose_path(tmp_path, tmp_path) == custom


def test_resolve_compose_path_malformed_toml_falls_back(tmp_path: Path) -> None:
    """A malformed deploy_pack.toml falls back to the default."""
    (tmp_path / "deploy_pack.toml").write_text("not = valid = toml")
    assert _resolve_compose_path(tmp_path, tmp_path) == tmp_path / "docker-compose.yml"


def test_compute_compose_sha256_missing_raises(tmp_path: Path) -> None:
    """A missing compose file raises UpgradeCliError(3)."""
    with pytest.raises(UpgradeCliError) as ei:
        _compute_compose_sha256(tmp_path, tmp_path)
    assert ei.value.exit_code == 3


def test_compute_compose_sha256_unreadable_raises(tmp_path: Path) -> None:
    """A compose file that does not parse raises UpgradeCliError(3)."""
    (tmp_path / "docker-compose.yml").write_text(":::not yaml")
    with pytest.raises(UpgradeCliError) as ei:
        _compute_compose_sha256(tmp_path, tmp_path)
    assert ei.value.exit_code == 3


def test_compute_compose_sha256_valid(tmp_path: Path) -> None:
    """A valid compose file yields its SHA-256."""
    (tmp_path / "docker-compose.yml").write_text("services:\n  a:\n    image: x\n")
    assert _compute_compose_sha256(tmp_path, tmp_path) == _hex_of(tmp_path / "docker-compose.yml")


def test_present_losses_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty report prints the no-losses message."""
    report = _minimal_report()
    report = ResolutionReport(
        run_id=report.run_id,
        generated_at_utc=report.generated_at_utc,
        status=report.status,
        inputs=report.inputs,
        target_runtime=report.target_runtime,
        policy=report.policy,
        seed=(),
        resolved=(),
        lost=(),
        cascade=(),
        conflict=(),
        diagnostics=report.diagnostics,
    )
    _present_losses(report)
    assert "No losses to acknowledge" in capsys.readouterr().out


def test_present_losses_renders_entries(capsys: pytest.CaptureFixture[str]) -> None:
    """Losses are rendered with project_id and reason."""
    _present_losses(_minimal_report())
    out = capsys.readouterr().out
    assert "gone" in out
    assert "no_compatible_version" in out


def test_get_confirmation_accept_losses_true() -> None:
    """accept_losses=True short-circuits the prompt."""
    assert _get_confirmation(True) is True


def test_get_confirmation_non_tty_no_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TTY and without accept_losses, confirmation returns False."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _get_confirmation(False) is False


def test_get_confirmation_tty_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'y' answer on a TTY returns True."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _get_confirmation(False) is True


def test_get_confirmation_tty_no(monkeypatch: pytest.MonkeyPatch) -> None:
    """An 'n' answer returns False."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _get_confirmation(False) is False


def test_get_confirmation_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EOF on input returns False."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _raise(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert _get_confirmation(False) is False


def test_write_acknowledgment(tmp_path: Path) -> None:
    """_write_acknowledgment produces the documented JSON shape."""
    path = tmp_path / "acknowledgment.json"
    _write_acknowledgment(path, _minimal_state(), _minimal_report(), "test-op")
    body = json.loads(path.read_text())
    assert body["schema_version"] == 1
    assert body["run_id"] == "abc"
    assert body["acknowledged_by"] == "test-op"
    assert len(body["acknowledged_losses"]) == 1


# ---------------------------------------------------------------------------
# run() error paths
# ---------------------------------------------------------------------------


def test_run_missing_state_raises(tmp_path: Path) -> None:
    """A missing state.toml raises UpgradeCliError(3)."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n')
    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=tmp_path, run_id="missing", accept_losses=True, operator="t")
    assert ei.value.exit_code == 3


def test_run_wrong_status_raises(tmp_path: Path) -> None:
    """A state not in awaiting_acknowledgment raises UpgradeCliError(3)."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state" / "runs" / "abc"
    state_dir.mkdir(parents=True)
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n')
    save_state(state_dir / "state.toml", _minimal_state(status=STATUS_RESOLVED))
    with pytest.raises(UpgradeCliError, match="acknowledge only accepts"):
        run(config_dir=config_dir, project_root=tmp_path, run_id="abc", accept_losses=True, operator="t")
