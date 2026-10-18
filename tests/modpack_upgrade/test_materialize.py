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
from minecraft.modpack_upgrade.cli.commands import _compose
from minecraft.modpack_upgrade.cli.commands import materialize as materialize_mod
from minecraft.modpack_upgrade.cli.commands.materialize import _refuse_bad_status, run
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.integrity import IntegrityContext
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

# ---------------------------------------------------------------------------
# Test-wide determinism: force docker-inspect offline unless a test opts in.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force docker-inspect disabled unless a test opts in.

    Prevents CI/local-environment Docker state from influencing slip
    contents. Tests that exercise docker-inspect paths monkeypatch
    ``materialize_mod._run_docker_inspect`` directly (which replaces the
    whole function including the env check) or delete this env var.
    """
    monkeypatch.setenv("MINECRAFT_UPGRADE_NO_DOCKER", "1")


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


def _write_instance_toml(config_dir: Path, body: str) -> None:
    """Write a single ``[instances.survival]`` table with the given body."""
    config_dir.mkdir(exist_ok=True)
    (config_dir / "deploy_pack.toml").write_text(
        f'[instances.survival]\ncontainer = "mc-survival"\n{body}\n',
        encoding="utf-8",
    )


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

    Per Resolution_Report Section 3.3 / A5.2 step 6a: seed ``jar_sha256`` is the
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


# ---------------------------------------------------------------------------
# Slice 8: _run_docker_inspect shim
# ---------------------------------------------------------------------------


def test_run_docker_inspect_skips_when_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MINECRAFT_UPGRADE_NO_DOCKER short-circuits before subprocess.run."""
    monkeypatch.setenv("MINECRAFT_UPGRADE_NO_DOCKER", "1")
    called: list[bool] = []

    def _fake_run(*args: object, **kwargs: object) -> object:
        called.append(True)
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(materialize_mod.subprocess, "run", _fake_run)
    assert materialize_mod._run_docker_inspect("any/image", "{{...}}") is None
    assert called == []


def test_run_docker_inspect_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.run's stdout is returned when the call succeeds."""
    monkeypatch.delenv("MINECRAFT_UPGRADE_NO_DOCKER", raising=False)

    class _FakeResult:
        returncode = 0
        stdout = "sha256:abc\n"
        stderr = ""

    monkeypatch.setattr(materialize_mod.subprocess, "run", lambda *a, **kw: _FakeResult())
    assert materialize_mod._run_docker_inspect("x", "{{.Foo}}") == "sha256:abc"


def test_run_docker_inspect_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero docker exit yields None."""
    monkeypatch.delenv("MINECRAFT_UPGRADE_NO_DOCKER", raising=False)

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "no such image"

    monkeypatch.setattr(materialize_mod.subprocess, "run", lambda *a, **kw: _FakeResult())
    assert materialize_mod._run_docker_inspect("missing/image", "{{.Foo}}") is None


def test_run_docker_inspect_returns_none_on_file_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing docker binary yields None."""
    monkeypatch.delenv("MINECRAFT_UPGRADE_NO_DOCKER", raising=False)

    def _raise(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("docker not on PATH")

    monkeypatch.setattr(materialize_mod.subprocess, "run", _raise)
    assert materialize_mod._run_docker_inspect("x", "{{.Foo}}") is None


# ---------------------------------------------------------------------------
# Slice 8: _resolve_image_digest parser
# ---------------------------------------------------------------------------


def test_resolve_image_digest_from_repo_at_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``<repo>@sha256:<hex>`` RepoDigest is parsed to ``sha256:<hex>``."""
    hex64 = "a" * 64
    monkeypatch.setattr(
        materialize_mod,
        "_run_docker_inspect",
        lambda *a, **kw: f"itzg/minecraft-server@sha256:{hex64}",
    )
    assert materialize_mod._resolve_image_digest("itzg/minecraft-server:java25") == f"sha256:{hex64}"


def test_resolve_image_digest_from_bare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``sha256:<hex>`` value is accepted as-is."""
    hex64 = "b" * 64
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: f"sha256:{hex64}")
    assert materialize_mod._resolve_image_digest("x") == f"sha256:{hex64}"


def test_resolve_image_digest_rejects_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-digest output (e.g. ``<no value>``) is rejected."""
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: "<no value>")
    assert materialize_mod._resolve_image_digest("x") is None


def test_resolve_image_digest_none_when_docker_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """None in -> None out (binary missing, offline, etc.)."""
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: None)
    assert materialize_mod._resolve_image_digest("x") is None


# ---------------------------------------------------------------------------
# Slice 8: _resolve_java_version parser
# ---------------------------------------------------------------------------


def test_resolve_java_version_extracts_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """JAVA_VERSION=<value> in the env output is extracted."""
    env_output = "PATH=/usr/bin\nJAVA_VERSION=21.0.5\nHOME=/root\n"
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: env_output)
    assert materialize_mod._resolve_java_version("x") == "21.0.5"


def test_resolve_java_version_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent JAVA_VERSION yields None."""
    env_output = "PATH=/usr/bin\nHOME=/root\n"
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: env_output)
    assert materialize_mod._resolve_java_version("x") is None


def test_resolve_java_version_none_when_docker_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """None in -> None out."""
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: None)
    assert materialize_mod._resolve_java_version("x") is None


# ---------------------------------------------------------------------------
# Slice 8: _resolve_runtime end-to-end
# ---------------------------------------------------------------------------


def test_resolve_runtime_uses_docker_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real digest and java_version flow into the runtime dict."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (tmp_path / "docker-compose.yml").write_text("services:\n  mc-survival:\n    image: itzg/minecraft-server:java25\n    platform: linux/amd64\n")
    hex64 = "c" * 64

    def _fake_inspect(image: str, template: str) -> str | None:
        if "RepoDigests" in template:
            return f"itzg/minecraft-server@sha256:{hex64}"
        return "JAVA_VERSION=21.0.5\n"

    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", _fake_inspect)
    runtime = materialize_mod._resolve_runtime(config_dir, tmp_path, _minimal_report())
    assert runtime["image_reference"] == "itzg/minecraft-server:java25"
    assert runtime["platform"] == "linux/amd64"
    assert runtime["image_digest"] == f"sha256:{hex64}"
    assert runtime["java_version"] == "21.0.5"


def test_resolve_runtime_falls_back_to_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When docker cannot resolve, the placeholder digest and ``unknown`` are used."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (tmp_path / "docker-compose.yml").write_text("services:\n  mc-survival:\n    image: itzg/minecraft-server:java25\n")
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: None)
    runtime = materialize_mod._resolve_runtime(config_dir, tmp_path, _minimal_report())
    assert runtime["image_digest"] == materialize_mod._PLACEHOLDER_DIGEST
    assert runtime["java_version"] == "unknown"
    assert runtime["image_reference"] == "itzg/minecraft-server:java25"


# ---------------------------------------------------------------------------
# Slice 8: slip records real digest end-to-end
# ---------------------------------------------------------------------------


def test_happy_path_slip_records_resolved_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sealed slip records the docker-resolved image digest and java version."""
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

    hex64 = "d" * 64

    def _fake_inspect(image: str, template: str) -> str | None:
        if "RepoDigests" in template:
            return f"itzg/minecraft-server@sha256:{hex64}"
        return "JAVA_VERSION=21\n"

    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", _fake_inspect)

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
    assert slip.runtime.image_digest == f"sha256:{hex64}"
    assert slip.runtime.java_version == "21"


# ---------------------------------------------------------------------------
# Slice 8 pair 2: _load_instances reads and validates per-instance fields
# ---------------------------------------------------------------------------


def test_load_instances_defaults_when_fields_absent(tmp_path: Path) -> None:
    """Absent required_sides / config_deletions / protect_patterns use defaults."""
    config_dir = tmp_path / "config.d"
    _write_instance_toml(config_dir, 'config_mode = "merge"\nkubejs_mode = "delete"')
    instances = materialize_mod._load_instances(config_dir)
    assert instances["survival"]["required_sides"] == ["both", "server"]
    assert instances["survival"]["config_deletions"] == []
    assert instances["survival"]["protect_patterns"] == []
    assert instances["survival"]["config_mode"] == "merge"
    assert instances["survival"]["kubejs_mode"] == "delete"
    assert instances["survival"]["targeted"] is True


def test_load_instances_reads_required_sides(tmp_path: Path) -> None:
    """Explicit required_sides from config are read verbatim."""
    config_dir = tmp_path / "config.d"
    _write_instance_toml(
        config_dir,
        'config_mode = "merge"\nkubejs_mode = "delete"\nrequired_sides = ["both", "client"]',
    )
    instances = materialize_mod._load_instances(config_dir)
    assert instances["survival"]["required_sides"] == ["both", "client"]


def test_load_instances_reads_config_deletions_and_protect_patterns(tmp_path: Path) -> None:
    """config_deletions and protect_patterns from config are read verbatim."""
    config_dir = tmp_path / "config.d"
    _write_instance_toml(
        config_dir,
        'config_mode = "merge"\nkubejs_mode = "delete"\nconfig_deletions = ["tokens.json", "config/*.key"]\nprotect_patterns = ["*.pem"]',
    )
    instances = materialize_mod._load_instances(config_dir)
    assert instances["survival"]["config_deletions"] == ["tokens.json", "config/*.key"]
    assert instances["survival"]["protect_patterns"] == ["*.pem"]


def test_load_instances_accepts_empty_required_sides(tmp_path: Path) -> None:
    """An explicitly empty required_sides list is valid (no sides required)."""
    config_dir = tmp_path / "config.d"
    _write_instance_toml(
        config_dir,
        'config_mode = "merge"\nkubejs_mode = "delete"\nrequired_sides = []',
    )
    instances = materialize_mod._load_instances(config_dir)
    assert instances["survival"]["required_sides"] == []


@pytest.mark.parametrize(
    "instance_body,expected_substring",
    [
        ('required_sides = "both"', "required_sides must be an array"),
        ('required_sides = ["bogus"]', "required_sides contains invalid value"),
        ("required_sides = [42]", "required_sides must be an array"),
        ('config_deletions = "tokens.json"', "config_deletions must be an array"),
        ("config_deletions = [42]", "config_deletions must be an array"),
        ('protect_patterns = "*.key"', "protect_patterns must be an array"),
        ('config_mode = "replace"', "config_mode must be one of"),
        ('kubejs_mode = "merge"', "kubejs_mode must be one of"),
    ],
)
def test_load_instances_rejects_invalid_fields(tmp_path: Path, instance_body: str, expected_substring: str) -> None:
    """Each malformed per-instance field surfaces as UpgradeCliError(3)."""
    config_dir = tmp_path / "config.d"
    _write_instance_toml(config_dir, instance_body)
    with pytest.raises(UpgradeCliError) as ei:
        materialize_mod._load_instances(config_dir)
    assert ei.value.exit_code == 3
    assert expected_substring in str(ei.value)


# ---------------------------------------------------------------------------
# Slice 8 pair 2: end-to-end config -> slip integration
# ---------------------------------------------------------------------------


def test_required_sides_from_config_appear_in_slip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-instance config values flow into the sealed slip's [instances.*] table."""
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
    # Overwrite deploy_pack.toml with explicit per-instance fields.
    (config_dir / "deploy_pack.toml").write_text(
        "[instances.survival]\n"
        'container = "mc-survival"\n'
        'config_mode = "merge"\n'
        'kubejs_mode = "delete"\n'
        'required_sides = ["both"]\n'
        'config_deletions = ["tokens.json"]\n'
        'protect_patterns = ["*.key"]\n',
        encoding="utf-8",
    )
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
    slip = slip_mod.load_slip(project_root / "state" / "candidates" / "run-1" / "packing_slip.toml")
    instance = slip.instances["survival"]
    assert instance.required_sides == ("both",)
    assert instance.config_deletions == ("tokens.json",)
    assert instance.protect_patterns == ("*.key",)


def test_invalid_instance_config_rejected_before_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid per-instance field surfaces before any artifact is downloaded."""
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
    # Invalid config_mode; should be rejected before fetch.
    (config_dir / "deploy_pack.toml").write_text(
        '[instances.survival]\ncontainer = "mc-survival"\nconfig_mode = "replace"\nkubejs_mode = "delete"\n',
        encoding="utf-8",
    )
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
    assert "config_mode" in str(ei.value)
    # No artifact was downloaded.
    assert not (project_root / "sync" / "downloads" / "mod-v2.jar").exists()


# ---------------------------------------------------------------------------
# Slice 8 pair 5: materialize delegates to the shared _compose module
# ---------------------------------------------------------------------------
#
# ``_compose`` is exhaustively tested in ``test_compose.py``. These tests
# pin only the wiring: a future refactor that re-introduces a local copy
# of ``resolve_compose_path`` or ``compute_compose_sha256`` would fail these,
# even if the local copy happened to behave identically today.


def test_resolve_runtime_delegates_compose_path_to_shared_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_runtime calls _compose.resolve_compose_path, not a local copy."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    calls: list[tuple[Path, Path]] = []
    sentinel = tmp_path / "custom-compose.yml"
    sentinel.write_text("services:\n  mc-survival:\n    image: sentinel/image:tag\n")

    def _spy(cfg: Path, root: Path) -> Path:
        calls.append((cfg, root))
        return sentinel

    monkeypatch.setattr(_compose, "resolve_compose_path", _spy)
    # Force docker-offline so _resolve_runtime doesn't try to inspect sentinel/image.
    monkeypatch.setattr(materialize_mod, "_run_docker_inspect", lambda *a, **kw: None)

    runtime = materialize_mod._resolve_runtime(config_dir, tmp_path, _minimal_report())

    assert calls == [(config_dir, tmp_path)]
    assert runtime["image_reference"] == "sentinel/image:tag"


def test_build_integrity_ctx_delegates_compose_sha_to_shared_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_integrity_ctx calls _compose.compute_compose_sha256 in update mode."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    calls: list[tuple[Path, Path]] = []
    sentinel_sha = "f" * 64

    def _spy(cfg: Path, root: Path) -> str:
        calls.append((cfg, root))
        return sentinel_sha

    monkeypatch.setattr(_compose, "compute_compose_sha256", _spy)

    state = _minimal_state(
        status=STATUS_ACKNOWLEDGED,
        release_mode="update",
        effective_compose_sha256=sentinel_sha,
    )
    ctx = materialize_mod._build_integrity_ctx(config_dir=config_dir, project_root=tmp_path, run_id="run-1", state=state)

    assert calls == [(config_dir, tmp_path)]
    assert isinstance(ctx, IntegrityContext)
    assert ctx.effective_compose_sha256 == sentinel_sha
