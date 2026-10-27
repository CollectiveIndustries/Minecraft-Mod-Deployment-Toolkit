# tests/modpack_upgrade/test_validate_command.py

"""Functional tests for ``validate migration`` and ``validate velocity``
(spec Section 15.1, Section 8.4, Section 8.5).

Both subcommands have module-level imports that must be monkeypatched for
tests:

  * ``validate.run_migration`` calls ``_run_deploy_pack_integration``,
    which shells out to the ``deploy-pack`` binary.
  * ``validate.run_velocity`` constructs ``DockerRuntime()`` inline and
    calls ``load_compose``.

Monkeypatching the module attributes (not the originals) keeps the
production wiring untouched while letting tests script all four external
boundaries deterministically.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w
import yaml

from minecraft.modpack_upgrade.cli.commands import validate as val
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _write_upgrade_toml(
    config_dir: Path,
    *,
    dest_root: Path,
    compose_file: Path | None = None,
) -> None:
    """Write ``upgrade.toml`` with the fields both subcommands read."""
    config_dir.mkdir(parents=True, exist_ok=True)
    body: dict = {"world_snapshot": {"dest_root": str(dest_root)}}
    (config_dir / "upgrade.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))
    if compose_file is not None:
        (config_dir / "deploy_pack.toml").write_text(
            f'[docker]\ncompose_file = "{compose_file}"\n',
            encoding="utf-8",
        )


def _write_snapshot(
    dest_root: Path,
    *,
    name: str = "survival-20260101T000000Z",
    files: dict[str, bytes] | None = None,
    corrupt_manifest: bool = False,
    corrupt_file: bool = False,
) -> Path:
    """Write a snapshot dir with a manifest that matches its files."""
    contents = files if files is not None else {"level.dat": b"world"}
    snapshot_dir = dest_root / name
    world_dir = snapshot_dir / "world"
    world_dir.mkdir(parents=True)

    entries: list[dict] = []
    for rel, data in contents.items():
        target = world_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append(
            {
                "path": rel,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    manifest = {
        "schema_version": 1,
        "source": "/srv/minecraft/survival/world",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "files": entries,
    }
    manifest_path = snapshot_dir / "snapshot-manifest.json"
    if corrupt_manifest:
        manifest_path.write_bytes(b"not json")
    else:
        manifest_path.write_bytes((json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"))

    if corrupt_file:
        (world_dir / next(iter(contents))).write_bytes(b"tampered")

    return snapshot_dir


def _write_candidate(state_dir: Path, run_id: str = "run-1") -> None:
    """Write a minimal candidate slip directory."""
    candidate = state_dir / "candidates" / run_id
    candidate.mkdir(parents=True)
    (candidate / "packing_slip.toml").write_text("placeholder")


def _write_compose(
    project_root: Path,
    *,
    include_velocity: bool = True,
    container_name: str = "velocity",
) -> Path:
    """Write a compose file, optionally declaring a velocity service."""
    services: dict = {
        "mc-survival": {"image": "itzg/minecraft-server:latest"},
    }
    if include_velocity:
        services["velocity"] = {
            "image": "itzg/velocity:latest",
            "container_name": container_name,
        }
    body = {"services": services}
    compose_path = project_root / "docker-compose.yml"
    compose_path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return compose_path


def _combined_output(capsys: pytest.CaptureFixture[str]) -> str:
    """Return stdout + stderr as a single string.

    ``_log_cli.info`` routes to stdout but ``_log_cli.warning`` routes to
    stderr through the routing sink. Tests that assert on warning-level
    output must combine both streams; asserting on ``.out`` alone would
    miss every warning.
    """
    captured = capsys.readouterr()
    return captured.out + captured.err


# ---------------------------------------------------------------------------
# Migration subcommand
# ---------------------------------------------------------------------------


def test_migration_no_snapshot_dir_exits_3(tmp_path: Path) -> None:
    """Snapshot destination doesn't exist -> exit 3."""
    config_dir = tmp_path / "config.d"
    _write_upgrade_toml(config_dir, dest_root=tmp_path / "missing-snapshots")
    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "snapshot destination not found" in str(ei.value)


def test_migration_no_snapshots_exits_3(tmp_path: Path) -> None:
    """Destination exists but has no snapshots -> exit 3."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_upgrade_toml(config_dir, dest_root=dest)
    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "no snapshots found" in str(ei.value)


def test_migration_explicit_snapshot_id_not_found_exits_3(tmp_path: Path) -> None:
    """An explicit ``--snapshot-id`` that doesn't exist -> exit 3."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest, name="survival-20260101T000000Z")
    _write_upgrade_toml(config_dir, dest_root=dest)
    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(
            config_dir=config_dir,
            project_root=tmp_path,
            snapshot_id="nonexistent",
        )
    assert ei.value.exit_code == 3
    assert "snapshot not found" in str(ei.value)


def test_migration_corrupt_manifest_exits_1(tmp_path: Path) -> None:
    """Snapshot manifest is not JSON -> exit 1."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest, corrupt_manifest=True)
    _write_upgrade_toml(config_dir, dest_root=dest)
    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "manifest unreadable" in str(ei.value)


def test_migration_hash_mismatch_exits_1(tmp_path: Path) -> None:
    """A snapshot file whose content doesn't hash to its recorded value -> exit 1."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest, corrupt_file=True)
    _write_upgrade_toml(config_dir, dest_root=dest)
    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "snapshot hash mismatch" in str(ei.value)


def test_migration_no_candidate_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid snapshot but no materialized candidate -> exit 3."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest)
    _write_upgrade_toml(config_dir, dest_root=dest)

    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "no materialized candidate" in str(ei.value)


def test_migration_deploy_pack_failure_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``deploy-pack --integration`` failure -> exit 1."""
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest)
    _write_upgrade_toml(config_dir, dest_root=dest)

    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )
    _write_candidate(state_dir)

    def _fail(*_a, **_kw):
        raise UpgradeCliError(1, "simulated integration deploy failure")

    monkeypatch.setattr(val, "_run_deploy_pack_integration", _fail)

    with pytest.raises(UpgradeCliError) as ei:
        val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "simulated integration deploy failure" in str(ei.value)


def test_migration_happy_path_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Valid snapshot + candidate + integration boot -> exit 0.

    The PARTIAL-coverage warning goes to **stderr** (the routing sink
    sends WARNING+ to stderr); the informational lines go to stdout.
    Assert against combined output.
    """
    config_dir = tmp_path / "config.d"
    dest = tmp_path / "snapshots"
    dest.mkdir()
    _write_snapshot(dest)
    _write_upgrade_toml(config_dir, dest_root=dest)

    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )
    _write_candidate(state_dir)

    monkeypatch.setattr(val, "_run_deploy_pack_integration", lambda *_a, **_kw: None)

    code = val.run_migration(config_dir=config_dir, project_root=tmp_path)
    assert code == 0

    combined = _combined_output(capsys)
    assert "snapshot:" in combined
    assert "PARTIAL" in combined  # honest limitation warning
    assert "did NOT boot against the snapshot world" in combined


# ---------------------------------------------------------------------------
# Velocity subcommand
# ---------------------------------------------------------------------------


def _mock_runtime(monkeypatch: pytest.MonkeyPatch, *, exists: bool, running: bool, health: str | None) -> None:
    """Patch ``DockerRuntime`` in the ``validate`` namespace."""
    fake_state = SimpleNamespace(
        exists=exists,
        running=running,
        status="running" if running else "exited",
        health=health,
        raw=None,
    )

    class _FakeRuntime:
        def __init__(self, *_a, **_kw):
            pass

        def inspect(self, name: str) -> SimpleNamespace:
            del name
            return fake_state

    monkeypatch.setattr(val, "DockerRuntime", _FakeRuntime)


def test_velocity_missing_compose_exits_3(tmp_path: Path) -> None:
    """No compose file anywhere -> exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    with pytest.raises(UpgradeCliError) as ei:
        val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "compose file not found" in str(ei.value)


def test_velocity_no_velocity_service_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compose with no velocity service -> documented no-op, exit 0."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_compose(tmp_path, include_velocity=False)

    code = val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert code == 0
    combined = _combined_output(capsys)
    assert "no velocity service declared" in combined


def test_velocity_container_missing_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared velocity service whose container doesn't exist -> exit 1."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_compose(tmp_path)
    _mock_runtime(monkeypatch, exists=False, running=False, health=None)

    with pytest.raises(UpgradeCliError) as ei:
        val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "not found" in str(ei.value)


def test_velocity_container_not_running_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container exists but isn't running -> exit 1."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_compose(tmp_path)
    _mock_runtime(monkeypatch, exists=True, running=False, health=None)

    with pytest.raises(UpgradeCliError) as ei:
        val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "not running" in str(ei.value)


def test_velocity_container_unhealthy_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container running but not healthy -> exit 1."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_compose(tmp_path)
    _mock_runtime(monkeypatch, exists=True, running=True, health="starting")

    with pytest.raises(UpgradeCliError) as ei:
        val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 1
    assert "not healthy" in str(ei.value)


def test_velocity_happy_path_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Healthy velocity container -> exit 0, prints the partial-coverage warning.

    The PARTIAL-coverage warning goes to **stderr**; the informational
    lines go to stdout. Assert against combined output.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    _write_compose(tmp_path)
    _mock_runtime(monkeypatch, exists=True, running=True, health="healthy")

    code = val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert code == 0

    combined = _combined_output(capsys)
    assert "container healthy" in combined
    assert "PARTIAL" in combined
    assert "Backend handshake" in combined or "not verified" in combined


def test_velocity_service_missing_container_name_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A velocity service without ``container_name`` -> exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    # Write compose without container_name on the velocity service
    body = {
        "services": {
            "velocity": {"image": "itzg/velocity:latest"},
        }
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(body), encoding="utf-8")

    with pytest.raises(UpgradeCliError) as ei:
        val.run_velocity(config_dir=config_dir, project_root=tmp_path)
    assert ei.value.exit_code == 3
    assert "no container_name" in str(ei.value)
