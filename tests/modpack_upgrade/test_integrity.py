# tests/modpack_upgrade/test_integrity.py

"""Shared integrity check tests (Section 6.3 of Resolution_Report)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from minecraft.modpack_upgrade.inhouse import load_in_house_filenames
from minecraft.modpack_upgrade.integrity import IntegrityContext, check_integrity
from minecraft.modpack_upgrade.manifest import load_manifest
from minecraft.modpack_upgrade.overrides import load_side_overrides
from minecraft.modpack_upgrade.report import (
    Diagnostics,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    TargetRuntime,
    write_report,
)
from minecraft.modpack_upgrade.resolution_inputs import compute_resolution_inputs_sha256
from minecraft.modpack_upgrade.seed import load_seed
from minecraft.modpack_upgrade.state import RunState


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _build_project(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create a minimal project tree with manifest, index, and overrides.

    Returns (config_dir, modpack_dir, manifest_path, side_overrides_path).
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_text("[in-house]\nfilenames = []\n")

    manifest_path = tmp_path / "sync" / "modpack.manifest.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        '[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "52.0.1"\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n'
    )

    modpack_dir = tmp_path / "sync" / "downloads"
    (modpack_dir / ".index").mkdir(parents=True)

    side_overrides_path = config_dir / "side_overrides.toml"
    side_overrides_path.write_text("")

    return (config_dir, modpack_dir, manifest_path, side_overrides_path)


def _build_report() -> ResolutionReport:
    """Return a minimal valid ResolutionReport."""
    return ResolutionReport(
        run_id="abc",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="complete",
        inputs=ReportInputs(
            manifest_sha256=_hex("0"),
            resolution_inputs_sha256=_hex("1"),
            side_overrides_sha256=_hex("2"),
            release_mode="upgrade",
        ),
        target_runtime=TargetRuntime(
            minecraft_version="1.21.1",
            loader="forge",
            loader_version="52.0.1",
        ),
        policy=ReportPolicy(selection="release_then_beta", no_version="ask"),
        seed=(),
        resolved=(),
        lost=(),
        cascade=(),
        conflict=(),
        diagnostics=Diagnostics(),
    )


def _build_state(tmp_path: Path, report_path: Path, resolution_inputs_sha: str, **overrides: object) -> RunState:
    """Return a RunState whose hashes match the given report and inputs."""
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    base: dict[str, object] = {
        "run_id": "abc",
        "status": "resolved",
        "release_mode": "upgrade",
        "paused_at_utc": "2026-01-01T00:00:00Z",
        "report_sha256": report_sha,
        "resolution_inputs_sha256": resolution_inputs_sha,
        "manifest_path": str(tmp_path / "sync" / "modpack.manifest.toml"),
    }
    base.update(overrides)
    return RunState(**base)


def _compute_actual_inputs_sha(config_dir: Path, modpack_dir: Path, manifest_path: Path, release_mode: str) -> str:
    """Recompute the resolution_inputs_sha256 for the given project."""
    manifest = load_manifest(manifest_path)
    in_house = load_in_house_filenames(config_dir)
    overrides = load_side_overrides(config_dir / "side_overrides.toml")
    seed = load_seed(modpack_dir / ".index", modpack_dir, in_house)
    return compute_resolution_inputs_sha256(
        release_mode=release_mode,
        manifest=manifest,
        in_house_filenames=in_house,
        seed=seed.snapshot_entries,
        side_overrides=overrides,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_all_checks_pass(tmp_path: Path) -> None:
    """A correctly-set-up run passes every Section 6.3 check."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _build_report())

    actual_inputs_sha = _compute_actual_inputs_sha(config_dir, modpack_dir, manifest_path, "upgrade")
    state = _build_state(tmp_path, report_path, actual_inputs_sha)
    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=None,
    )
    assert check_integrity(state, ctx) == []


# ---------------------------------------------------------------------------
# Report integrity
# ---------------------------------------------------------------------------


def test_missing_report_fails(tmp_path: Path) -> None:
    """A missing report file yields a failure."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"

    state = RunState(
        run_id="abc",
        status="resolved",
        release_mode="upgrade",
        paused_at_utc="t",
        report_sha256=_hex("0"),
        resolution_inputs_sha256=_hex("0"),
        manifest_path=str(manifest_path),
    )
    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=None,
    )
    failures = check_integrity(state, ctx)
    assert any("report not found" in f for f in failures)


def test_report_hash_mismatch_fails(tmp_path: Path) -> None:
    """A report whose bytes do not match state.report_sha256 fails."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _build_report())

    actual_inputs_sha = _compute_actual_inputs_sha(config_dir, modpack_dir, manifest_path, "upgrade")
    state = _build_state(tmp_path, report_path, actual_inputs_sha, report_sha256=_hex("f"))

    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=None,
    )
    failures = check_integrity(state, ctx)
    assert any("report hash mismatch" in f for f in failures)


# ---------------------------------------------------------------------------
# Inputs integrity
# ---------------------------------------------------------------------------


def test_resolution_inputs_mismatch_fails(tmp_path: Path) -> None:
    """A state whose resolution_inputs_sha256 does not match current inputs fails."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _build_report())

    state = _build_state(tmp_path, report_path, _hex("0"))  # wrong inputs hash
    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=None,
    )
    failures = check_integrity(state, ctx)
    assert any("resolution_inputs hash mismatch" in f for f in failures)


# ---------------------------------------------------------------------------
# Compose integrity (update mode only)
# ---------------------------------------------------------------------------


def test_update_mode_missing_compose_hash_fails(tmp_path: Path) -> None:
    """release_mode='update' requires effective_compose_sha256 to be supplied."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _build_report())

    state = _build_state(
        tmp_path,
        report_path,
        _hex("0"),
        release_mode="update",
        effective_compose_sha256=_hex("a"),
    )
    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=None,  # caller did not supply
    )
    failures = check_integrity(state, ctx)
    assert any("requires effective_compose_sha256" in f for f in failures)


def test_update_mode_compose_hash_mismatch_fails(tmp_path: Path) -> None:
    """A drifted compose file fails the update-mode integrity check."""
    config_dir, modpack_dir, manifest_path, _ = _build_project(tmp_path)
    report_path = tmp_path / "www" / "runs" / "abc" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _build_report())

    state = _build_state(
        tmp_path,
        report_path,
        _hex("0"),
        release_mode="update",
        effective_compose_sha256=_hex("a"),
    )
    ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=modpack_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        effective_compose_sha256=_hex("b"),  # different from state
    )
    failures = check_integrity(state, ctx)
    assert any("compose hash mismatch" in f for f in failures)
