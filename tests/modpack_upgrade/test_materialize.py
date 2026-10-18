# tests/modpack_upgrade/test_materialize.py

"""Materialize command end-to-end tests (Section 15.1.4)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.commands import materialize as materialize_mod
from minecraft.modpack_upgrade.cli.commands.materialize import _refuse_bad_status, run
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.providers.base import (
    HttpResult,
    Provider,
    ProviderDependency,
    ProviderVersion,
)
from minecraft.modpack_upgrade.providers.curseforge import CurseForgeProvider
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry
from minecraft.modpack_upgrade.report import (
    Diagnostics,
    ExpectedHash,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    ResolvedEntry,
    SeedEntry,
    TargetRuntime,
    compute_report_sha256,
    write_report,
)
from minecraft.modpack_upgrade.state import (
    STATUS_ACKNOWLEDGED,
    STATUS_AWAITING_ACKNOWLEDGMENT,
    STATUS_FAILED,
    STATUS_MATERIALIZED,
    STATUS_RESOLVED,
    RunState,
    save_state,
)


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _sha1(data: bytes) -> str:
    """Return the SHA-1 hex digest of ``data``."""
    return hashlib.sha1(data).hexdigest()


def _sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _make_fabric_jar(tmp_path: Path, filename: str = "mod.jar", content: bytes = b"jar-content") -> Path:
    """Create a minimal fabric jar."""
    jar = tmp_path / filename
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps({"environment": "client"}))
    return jar


def _seed_entry(
    *,
    filename: str = "mod.jar",
    provider: str = "modrinth",
    project_id: str | None = "mod",
    current_version_id: str | None = "v0",
    side_declared: str = "both",
    jar_sha256: str | None = None,
) -> SeedEntry:
    """Build a SeedEntry fixture."""
    return SeedEntry(
        filename=filename,
        provider=provider,
        jar_sha256=jar_sha256 or _hex("a"),
        side_declared=side_declared,
        side_effective=side_declared,
        project_id=project_id,
        current_version_id=current_version_id,
    )


def _resolved_entry(
    *,
    filename: str = "mod-v2.jar",
    provider: str = "modrinth",
    project_id: str | None = "mod",
    version_id: str | None = "v2",
    is_in_house: bool = False,
    side_provisional: str = "both",
    side_source_provisional: str = "provider",
    expected_hashes: tuple[ExpectedHash, ...] = (),
) -> ResolvedEntry:
    """Build a ResolvedEntry fixture."""
    return ResolvedEntry(
        filename=filename,
        provider=provider,
        is_in_house=is_in_house,
        is_dependency_only=False,
        side_provisional=side_provisional,
        side_source_provisional=side_source_provisional,
        project_id=project_id,
        version_id=version_id,
        expected_hashes=expected_hashes,
    )


def _minimal_report(
    *,
    resolved: tuple[ResolvedEntry, ...] = (),
    seed: tuple[SeedEntry, ...] = (),
    lost: tuple = (),
    cascade: tuple = (),
    no_version: str = "ask",
) -> ResolutionReport:
    """Build a minimal ResolutionReport fixture."""
    return ResolutionReport(
        run_id="run-1",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="complete",
        inputs=ReportInputs(
            manifest_sha256=_hex("1"),
            resolution_inputs_sha256=_hex("2"),
            side_overrides_sha256=_hex("3"),
            release_mode="upgrade",
        ),
        target_runtime=TargetRuntime(
            minecraft_version="1.21.1",
            loader="forge",
            loader_version="52.0.1",
        ),
        policy=ReportPolicy(selection="release_then_beta", no_version=no_version),
        seed=seed,
        resolved=resolved,
        lost=lost,
        cascade=cascade,
        conflict=(),
        diagnostics=Diagnostics(),
    )


def _minimal_state(**overrides: object) -> RunState:
    """Build a minimal RunState fixture."""
    base: dict[str, object] = {
        "run_id": "run-1",
        "status": STATUS_RESOLVED,
        "release_mode": "upgrade",
        "paused_at_utc": "2026-01-01T00:00:00Z",
        "report_sha256": _hex("a"),
        "resolution_inputs_sha256": _hex("b"),
        "manifest_path": "/m",
    }
    base.update(overrides)
    return RunState(**base)


def _setup_project(
    tmp_path: Path,
    *,
    report: ResolutionReport,
    state: RunState,
    instances: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Create a minimal project tree with config, report, and state."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    instances = instances or {"survival": "mc-survival"}
    toml_lines: list[str] = []
    for name, container in instances.items():
        toml_lines = [
            *toml_lines,
            f"[instances.{name}]",
            f'container = "{container}"',
            'config_mode = "merge"',
            'kubejs_mode = "delete"',
        ]
    (config_dir / "deploy_pack.toml").write_text("\n".join(toml_lines) + "\n")

    (tmp_path / "docker-compose.yml").write_text("services:\n  mc-survival:\n    image: itzg/minecraft-server:java25\n    platform: linux/amd64\n")

    state_dir = tmp_path / "state"
    state_run_dir = state_dir / "runs" / "run-1"
    state_run_dir.mkdir(parents=True)
    report_path = tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, report)

    actual_sha = compute_report_sha256(report_path)
    final_state = replace(state, report_sha256=actual_sha)
    save_state(state_run_dir / "state.toml", final_state)

    return (config_dir, tmp_path)


class _FakeProvider(Provider):
    """A Provider fixture that serves scripted versions and artifacts."""

    def __init__(self, versions: dict[str, list[ProviderVersion]], payload: bytes = b"artifact") -> None:
        self.name = "modrinth"
        self._versions = versions
        self._payload = payload

    def query_versions(self, project_id: str, game_version: str, loader: str) -> list[ProviderVersion]:
        return list(self._versions.get(project_id, []))

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        return self._payload


class _NullTransport:
    """A transport that raises on any call."""

    def get(self, url: str, **kwargs: object) -> HttpResult:
        raise AssertionError(f"unexpected transport use: {url}")


def _registry(versions: dict[str, list[ProviderVersion]] | None = None, payload: bytes = b"artifact") -> ProviderRegistry:
    """Build a registry with a fake Modrinth + inert CurseForge."""
    r = ProviderRegistry()
    r.register(_FakeProvider(versions or {}, payload))
    r.register(CurseForgeProvider(transport=_NullTransport(), api_key=None))
    return r


def _pv(
    version_id: str = "v2",
    *,
    filename: str = "mod-v2.jar",
    hashes: dict[str, str] | None = None,
    dependencies: tuple[ProviderDependency, ...] = (),
) -> ProviderVersion:
    """Build a ProviderVersion fixture."""
    return ProviderVersion(
        version_id=version_id,
        filename=filename,
        download_url=f"https://example/{filename}",
        hashes=hashes or {"sha1": _sha1(b"artifact")},
        game_versions=("1.21.1",),
        loaders=("forge",),
        release_channel="release",
        dependencies=dependencies,
        client_side="required",
        server_side="required",
    )


# ---------------------------------------------------------------------------
# Status gate (unit)
# ---------------------------------------------------------------------------


def test_refuse_awaiting_acknowledgment_exits_6() -> None:
    """awaiting_acknowledgment refuses with exit 6."""
    with pytest.raises(UpgradeCliError) as ei:
        _refuse_bad_status(STATUS_AWAITING_ACKNOWLEDGMENT, "run-1")
    assert ei.value.exit_code == 6


def test_refuse_failed_exits_3() -> None:
    """Failed refuses with exit 3."""
    with pytest.raises(UpgradeCliError) as ei:
        _refuse_bad_status(STATUS_FAILED, "run-1")
    assert ei.value.exit_code == 3


def test_refuse_materialized_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    """Materialized returns 0 with next-command hint."""
    assert _refuse_bad_status(STATUS_MATERIALIZED, "run-1") == 0
    out = capsys.readouterr().out
    assert "already materialized" in out
    assert "next: iterate" in out


def test_refuse_resolved_returns_none() -> None:
    """Resolved is accepted."""
    assert _refuse_bad_status(STATUS_RESOLVED, "run-1") is None


def test_refuse_acknowledged_returns_none() -> None:
    """Acknowledged is accepted."""
    assert _refuse_bad_status(STATUS_ACKNOWLEDGED, "run-1") is None


def test_refuse_unknown_raises() -> None:
    """An unexpected status raises."""
    with pytest.raises(UpgradeCliError, match="unexpected state status"):
        _refuse_bad_status("bogus", "run-1")


# ---------------------------------------------------------------------------
# run() error paths
# ---------------------------------------------------------------------------


def test_run_missing_state_raises(tmp_path: Path) -> None:
    """A missing state.toml raises exit 3."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text("")
    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=tmp_path, run_id="missing", state_dir=tmp_path / "state")
    assert ei.value.exit_code == 3


def test_run_already_materialized_returns_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An already-materialized run exits 0."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state" / "runs" / "run-1"
    state_dir.mkdir(parents=True)
    save_state(state_dir / "state.toml", _minimal_state(status=STATUS_MATERIALIZED))
    code = run(config_dir=config_dir, project_root=tmp_path, run_id="run-1", state_dir=tmp_path / "state")
    assert code == 0
    assert "already materialized" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# BUG-02 regression: SchemaError from check_integrity -> exit 3
# ---------------------------------------------------------------------------


def test_integrity_schema_error_exits_3(tmp_path: Path) -> None:
    """A SchemaError from check_integrity is wrapped to UpgradeCliError(3)."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n')

    # Valid-looking state; the manifest is what will fail.
    state_dir = tmp_path / "state" / "runs" / "run-1"
    state_dir.mkdir(parents=True)
    save_state(
        state_dir / "state.toml",
        _minimal_state(manifest_path=str(tmp_path / "sync" / "modpack.manifest.toml")),
    )

    # Report exists.
    report_path = tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    write_report(report_path, _minimal_report())

    # Malformed manifest -> load_manifest raises SchemaError inside check_integrity.
    manifest_path = tmp_path / "sync" / "modpack.manifest.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("not = valid = toml\n")

    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            run_id="run-1",
            registry=_registry(),
            state_dir=tmp_path / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 3
    assert "integrity" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Happy paths (integration)
# ---------------------------------------------------------------------------


def _wire_integrity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit the integrity check so we can focus on materialize logic."""
    monkeypatch.setattr(materialize_mod, "check_integrity", lambda state, ctx: [])


def test_happy_path_external_modrinth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single external Modrinth artifact flows end-to-end."""
    _wire_integrity(monkeypatch)
    payload = b"artifact-bytes"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)

    code = run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )

    assert code == 0
    assert (project_root / "sync" / "downloads" / "mod-v2.jar").is_file()
    assert (project_root / "state" / "candidates" / "run-1" / "metadata" / "mod-v2.pw.toml").is_file()
    assert (project_root / "state" / "candidates" / "run-1" / "packing_slip.toml").is_file()


def test_happy_path_writes_valid_slip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sealed slip parses cleanly and content_sha256 verifies."""
    _wire_integrity(monkeypatch)
    payload = b"artifact-bytes"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)
    run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )
    slip = slip_mod.load_slip(project_root / "state" / "candidates" / "run-1" / "packing_slip.toml")
    assert slip.release_mode == "upgrade"
    assert len(slip.jars) == 1
    assert slip.jars[0].filename == "mod-v2.jar"


def test_happy_path_updates_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State is updated to 'materialized'."""
    _wire_integrity(monkeypatch)
    payload = b"artifact-bytes"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)
    run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )
    text = (project_root / "state" / "runs" / "run-1" / "state.toml").read_text()
    assert 'status = "materialized"' in text


def test_happy_path_in_house_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-house-only run verifies the warehouse jar against the seed hash."""
    _wire_integrity(monkeypatch)
    jar_content = b"in-house-jar-bytes"
    jar_sha = _sha256(jar_content)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                filename="custom.jar",
                provider="in-house",
                project_id=None,
                version_id=None,
                is_in_house=True,
                side_provisional="both",
                side_source_provisional="pw_toml",
            ),
        ),
        seed=(
            _seed_entry(
                filename="custom.jar",
                provider="in-house",
                project_id=None,
                current_version_id=None,
                jar_sha256=jar_sha,
            ),
        ),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    warehouse = project_root / "sync" / "downloads"
    warehouse.mkdir(parents=True, exist_ok=True)
    (warehouse / "custom.jar").write_bytes(jar_content)
    registry = _registry()

    code = run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )

    assert code == 0
    slip = slip_mod.load_slip(project_root / "state" / "candidates" / "run-1" / "packing_slip.toml")
    assert len(slip.jars) == 1
    assert slip.jars[0].provider == "in-house"
    assert slip.jars[0].sha256 == jar_sha


def test_empty_resolved_produces_empty_slip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A report with zero resolved entries produces a slip with zero jars.

    Skipped mods (override ``"skipped"``) never appear in ``resolved``
    - the resolver excludes them. This test verifies the empty case
    flows cleanly through materialize.
    """
    _wire_integrity(monkeypatch)
    report = _minimal_report(resolved=(), seed=())
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry()
    code = run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )
    assert code == 0
    slip = slip_mod.load_slip(project_root / "state" / "candidates" / "run-1" / "packing_slip.toml")
    assert len(slip.jars) == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_fetch_error_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider fetch failure exits 1."""
    _wire_integrity(monkeypatch)
    report = _minimal_report(
        resolved=(_resolved_entry(),),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({})
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 1


def test_version_gone_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A version that disappeared from the provider exits 1."""
    _wire_integrity(monkeypatch)
    payload = b"artifact"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                version_id="v-gone",
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    # Provider returns only v2, not v-gone.
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 1
    assert "no longer available" in str(ei.value)


def test_fetch_provider_error_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ProviderError raised during fetch_artifact exits 1."""
    _wire_integrity(monkeypatch)
    payload = b"artifact"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())

    class _RaisingProvider(_FakeProvider):
        def fetch_artifact(self, version: ProviderVersion) -> bytes:
            raise ProviderError("simulated fetch failure")

    r = ProviderRegistry()
    r.register(_RaisingProvider({"mod": [_pv("v2", hashes={"sha1": sha1})]}))
    r.register(CurseForgeProvider(transport=_NullTransport(), api_key=None))

    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=r,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 1


def test_hash_mismatch_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expected_hashes mismatch exits 1."""
    _wire_integrity(monkeypatch)
    payload = b"actual-bytes"
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value="0" * 40),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": _sha1(payload)})]}, payload=payload)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 1
    assert "hash mismatch" in str(ei.value)


def test_filename_collision_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A collision with different bytes exits 3."""
    _wire_integrity(monkeypatch)
    payload = b"new-bytes"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    warehouse = project_root / "sync" / "downloads"
    warehouse.mkdir(parents=True, exist_ok=True)
    (warehouse / "mod-v2.jar").write_bytes(b"different-bytes")
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 3
    assert "collision" in str(ei.value)


def test_side_unresolved_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable side exits 3 with the full list of unknowns."""
    _wire_integrity(monkeypatch)
    payload = b"artifact"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                filename="mod.jar",
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="unknown",
                side_source_provisional="unknown",
            ),
        ),
        seed=(_seed_entry(side_declared="unknown"),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    pv = ProviderVersion(
        version_id="v2",
        filename="mod.jar",
        download_url="https://example/mod.jar",
        hashes={"sha1": sha1},
        game_versions=("1.21.1",),
        loaders=("forge",),
        release_channel="release",
        dependencies=(),
        client_side=None,
        server_side=None,
    )
    registry = _registry({"mod": [pv]}, payload=payload)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 3
    assert "side unresolved" in str(ei.value)


def test_staging_cleaned_up_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staging directory is removed after a successful run."""
    _wire_integrity(monkeypatch)
    payload = b"artifact"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    registry = _registry({"mod": [_pv("v2", hashes={"sha1": sha1})]}, payload=payload)
    run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )
    staging = project_root / "state" / "runs" / "run-1" / "staging"
    assert not staging.is_dir() or list(staging.iterdir()) == []


# ---------------------------------------------------------------------------
# Coverage: _slip_deps exercises provider-declared dependencies
# ---------------------------------------------------------------------------


def test_provider_dependencies_flow_to_slip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider-declared deps (project_id + mod_id) appear in the sealed slip."""
    _wire_integrity(monkeypatch)
    payload = b"artifact"
    sha1 = _sha1(payload)
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                expected_hashes=(ExpectedHash(algorithm="sha1", value=sha1),),
                side_provisional="client",
                side_source_provisional="override",
            ),
        ),
        seed=(_seed_entry(),),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())

    required_dep = ProviderDependency(project_id="lib", mod_id=None, version_id="v-lib", dependency_type="required")
    optional_dep = ProviderDependency(project_id="opt", mod_id=None, version_id=None, dependency_type="optional")
    modid_dep = ProviderDependency(project_id=None, mod_id="12345", version_id=None, dependency_type="required")

    pv = _pv(
        "v2",
        hashes={"sha1": sha1},
        dependencies=(required_dep, optional_dep, modid_dep),
    )
    registry = _registry({"mod": [pv]}, payload=payload)

    code = run(
        config_dir=config_dir,
        project_root=project_root,
        run_id="run-1",
        registry=registry,
        state_dir=project_root / "state",
        now="2026-01-01T00:00:00Z",
    )
    assert code == 0

    slip = slip_mod.load_slip(project_root / "state" / "candidates" / "run-1" / "packing_slip.toml")
    deps = slip.jars[0].dependencies
    assert len(deps) == 3

    lib = next(d for d in deps if d.project_id == "lib")
    assert lib.namespace == "project"
    assert lib.type == "required"
    assert lib.version_id == "v-lib"
    assert lib.source == "pw_toml"

    opt = next(d for d in deps if d.project_id == "opt")
    assert opt.namespace == "project"
    assert opt.type == "optional"
    assert opt.version_id is None

    modid = next(d for d in deps if d.namespace == "modid")
    assert modid.source == "provider_metadata"
    assert modid.type == "required"


# ---------------------------------------------------------------------------
# In-house expected_hashes verification (Section A5.2 step 6a)
# ---------------------------------------------------------------------------


def test_in_house_expected_hashes_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In-house jars with expected_hashes have each listed digest checked.

    Per Resolution_Report Section 3.3 / Section A5.2 step 6a: seed ``jar_sha256`` is the
    primary identity, and any ``expected_hashes[]`` present must also
    verify. A mismatch in the in-house ``expected_hashes`` exits 1.
    """
    _wire_integrity(monkeypatch)
    jar_content = b"in-house-bytes"
    jar_sha256 = _sha256(jar_content)
    wrong_sha1 = "0" * 40
    report = _minimal_report(
        resolved=(
            _resolved_entry(
                filename="custom.jar",
                provider="in-house",
                project_id=None,
                version_id=None,
                is_in_house=True,
                side_provisional="both",
                side_source_provisional="pw_toml",
                expected_hashes=(ExpectedHash(algorithm="sha1", value=wrong_sha1),),
            ),
        ),
        seed=(
            _seed_entry(
                filename="custom.jar",
                provider="in-house",
                project_id=None,
                current_version_id=None,
                jar_sha256=jar_sha256,
            ),
        ),
    )
    config_dir, project_root = _setup_project(tmp_path, report=report, state=_minimal_state())
    warehouse = project_root / "sync" / "downloads"
    warehouse.mkdir(parents=True, exist_ok=True)
    (warehouse / "custom.jar").write_bytes(jar_content)
    registry = _registry()

    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=project_root,
            run_id="run-1",
            registry=registry,
            state_dir=project_root / "state",
            now="2026-01-01T00:00:00Z",
        )
    assert ei.value.exit_code == 1
    assert "hash mismatch" in str(ei.value).lower()
