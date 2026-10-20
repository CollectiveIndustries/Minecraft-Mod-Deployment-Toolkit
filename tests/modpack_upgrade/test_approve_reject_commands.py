# tests/modpack_upgrade/test_approve_reject_commands.py

"""CLI tests for the ``approve`` and ``reject`` commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.commands import approve as approve_mod
from minecraft.modpack_upgrade.cli.commands import reject as reject_mod
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.cli.main import main
from minecraft.modpack_upgrade.state import STATUS_MATERIALIZED, RunState, save_state


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _slip_body(run_id: str) -> dict:
    """Build a valid slip body for ``run_id``."""
    body = {
        "packing_slip_version": 1,
        "release_id": run_id,
        "release_mode": "upgrade",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.4.0",
            "author": "materialize",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + "0" * 64,
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": "a" * 40,
                "kubejs_tree": "a" * 40,
                "compose_tree": "a" * 40,
                "deploy_toml_tree": "a" * 40,
            },
        },
        "fingerprints": {
            "manifest_sha256": "1" * 64,
            "resolution_inputs_sha256": "2" * 64,
            "runtime_sha256": "3" * 64,
            "server_inputs_sha256": "4" * 64,
            "client_inputs_sha256": "5" * 64,
        },
        "jar": [],
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _setup_run(
    tmp_path: Path,
    *,
    write_state: bool = True,
    write_slip: bool = True,
) -> tuple[Path, Path]:
    """Create the run's state and slip under ``tmp_path/state``.

    Returns ``(state_dir, run_dir)``.
    """
    state_dir = tmp_path / "state"
    run_dir = state_dir / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    candidate_dir = state_dir / "candidates" / "run-1"
    candidate_dir.mkdir(parents=True)

    if write_state:
        state = RunState(
            run_id="run-1",
            status=STATUS_MATERIALIZED,
            release_mode="upgrade",
            paused_at_utc="2026-01-01T00:00:00Z",
            report_sha256=_hex("a"),
            resolution_inputs_sha256=_hex("b"),
            manifest_path=str(tmp_path / "sync" / "modpack.manifest.toml"),
        )
        save_state(run_dir / "state.toml", state)

    if write_slip:
        body = _slip_body("run-1")
        (candidate_dir / "packing_slip.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))

    return (state_dir, run_dir)


def _config_dir(tmp_path: Path) -> Path:
    """Create ``config.d`` with a minimal deploy_pack.toml."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n')
    return config_dir


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_approve_happy_path(tmp_path: Path) -> None:
    """A successful approve records the decision."""
    state_dir, run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    code = approve_mod.run(
        config_dir=config_dir,
        project_root=tmp_path,
        run_id="run-1",
        operator="alice",
        state_dir=state_dir,
        now="2026-01-01T00:00:00Z",
    )
    assert code == 0
    data = json.loads((run_dir / "approval.mod_set.json").read_text())
    assert data["decision"] == "approved"
    assert data["signed_by"] == "alice"


def test_reject_happy_path(tmp_path: Path) -> None:
    """A successful reject records the decision with a reason."""
    state_dir, run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    code = reject_mod.run(
        config_dir=config_dir,
        project_root=tmp_path,
        run_id="run-1",
        operator="bob",
        reason="Create 7 broke KubeJS",
        state_dir=state_dir,
        now="2026-01-01T00:00:00Z",
    )
    assert code == 0
    data = json.loads((run_dir / "approval.mod_set.json").read_text())
    assert data["decision"] == "rejected"
    assert data["reason"] == "Create 7 broke KubeJS"


def test_approve_writes_audit_log(tmp_path: Path) -> None:
    """A successful approve appends to the audit log."""
    state_dir, _run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    approve_mod.run(
        config_dir=config_dir,
        project_root=tmp_path,
        run_id="run-1",
        state_dir=state_dir,
    )
    assert (state_dir / "approvals.jsonl").is_file()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_approve_replay_idempotent(tmp_path: Path) -> None:
    """Two approve calls with the same interaction ID produce one record."""
    state_dir, _run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    approve_mod.run(
        config_dir=config_dir,
        project_root=tmp_path,
        run_id="run-1",
        interaction_id="interaction-1",
        state_dir=state_dir,
    )
    approve_mod.run(
        config_dir=config_dir,
        project_root=tmp_path,
        run_id="run-1",
        interaction_id="interaction-1",
        state_dir=state_dir,
    )
    log_lines = [line for line in (state_dir / "approvals.jsonl").read_text().splitlines() if line.strip()]
    assert len(log_lines) == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_approve_missing_state_exits_3(tmp_path: Path) -> None:
    """A missing state.toml raises UpgradeCliError(3)."""
    state_dir, _run_dir = _setup_run(tmp_path, write_state=False)
    config_dir = _config_dir(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        approve_mod.run(
            config_dir=config_dir,
            project_root=tmp_path,
            run_id="run-1",
            state_dir=state_dir,
        )
    assert ei.value.exit_code == 3
    assert "state.toml not found" in str(ei.value)


def test_approve_missing_slip_exits_3(tmp_path: Path) -> None:
    """A missing candidate slip raises UpgradeCliError(3)."""
    state_dir, _run_dir = _setup_run(tmp_path, write_slip=False)
    config_dir = _config_dir(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        approve_mod.run(
            config_dir=config_dir,
            project_root=tmp_path,
            run_id="run-1",
            state_dir=state_dir,
        )
    assert ei.value.exit_code == 3
    assert "candidate slip not found" in str(ei.value)


def test_approve_invalid_scope_exits_2(tmp_path: Path) -> None:
    """An invalid scope raises UpgradeCliError(2)."""
    state_dir, _run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        approve_mod.run(
            config_dir=config_dir,
            project_root=tmp_path,
            run_id="run-1",
            scope="bogus",
            state_dir=state_dir,
        )
    assert ei.value.exit_code == 2


def test_approve_empty_interaction_id_exits_2(tmp_path: Path) -> None:
    """An empty interaction ID raises UpgradeCliError(2)."""
    state_dir, _run_dir = _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        approve_mod.run(
            config_dir=config_dir,
            project_root=tmp_path,
            run_id="run-1",
            interaction_id="   ",
            state_dir=state_dir,
        )
    assert ei.value.exit_code == 2


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_approve(tmp_path: Path) -> None:
    """``main(['approve', 'run-1'])`` reaches the approve command."""
    _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    code = main(["--config-dir", str(config_dir), "approve", "run-1"])
    assert code == 0
    data = json.loads((tmp_path / "state" / "runs" / "run-1" / "approval.mod_set.json").read_text())
    assert data["decision"] == "approved"


def test_main_dispatches_reject(tmp_path: Path) -> None:
    """``main(['reject', 'run-1', '--reason', '...'])`` reaches reject."""
    _setup_run(tmp_path)
    config_dir = _config_dir(tmp_path)
    code = main(
        [
            "--config-dir",
            str(config_dir),
            "reject",
            "run-1",
            "--reason",
            "not ready",
        ]
    )
    assert code == 0
    data = json.loads((tmp_path / "state" / "runs" / "run-1" / "approval.mod_set.json").read_text())
    assert data["decision"] == "rejected"
    assert data["reason"] == "not ready"


def test_main_approve_requires_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``approve`` without a run ID exits 2 (argparse)."""
    code = main(["approve"])
    assert code == 2


def test_main_reject_requires_run_id(tmp_path: Path) -> None:
    """``reject`` without a run ID exits 2 (argparse)."""
    code = main(["reject"])
    assert code == 2


def test_main_approve_missing_state_exits_3(tmp_path: Path) -> None:
    """A missing state.toml through the CLI exits 3."""
    _setup_run(tmp_path, write_state=False)
    config_dir = _config_dir(tmp_path)
    code = main(["--config-dir", str(config_dir), "approve", "run-1"])
    assert code == 3
