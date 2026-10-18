# tests/modpack_upgrade/test_report.py

"""Resolution report schema and round-trip tests (Section 3, Section 4 of Resolution_Report)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.report import (
    CascadeEntry,
    ConflictCandidate,
    ConflictEntry,
    Diagnostics,
    ExpectedHash,
    LostEntry,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    ResolvedEntry,
    SeedEntry,
    TargetRuntime,
    compute_report_sha256,
    load_report,
    write_report,
)


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _minimal_report() -> ResolutionReport:
    """Return a report with only required sections."""
    return ResolutionReport(
        run_id="abc",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="complete",
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
        lost=(),
        cascade=(),
        conflict=(),
        diagnostics=Diagnostics(),
    )


def _full_report() -> ResolutionReport:
    """Return a report exercising every optional section."""
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
        seed=(
            SeedEntry(
                filename="create.jar",
                provider="modrinth",
                jar_sha256=_hex("a"),
                side_declared="both",
                side_effective="both",
                project_id="create",
                current_version_id="v1",
            ),
        ),
        resolved=(
            ResolvedEntry(
                filename="create-v2.jar",
                provider="modrinth",
                is_in_house=False,
                is_dependency_only=False,
                side_provisional="server",
                side_source_provisional="override",
                project_id="create",
                version_id="v2",
                upgraded_from_version_id="v1",
                expected_hashes=(
                    ExpectedHash(algorithm="sha512", value=_hex("f")),
                    ExpectedHash(algorithm="sha1", value=_hex("e")),
                ),
            ),
        ),
        lost=(
            LostEntry(
                filename="gone.jar",
                provider="modrinth",
                project_id="gone",
                reason="no_compatible_version",
                detail="no compatible versions",
            ),
        ),
        cascade=(
            CascadeEntry(
                filename="dependent.jar",
                provider="modrinth",
                project_id="dependent",
                reason="dependency_unsatisfiable",
                dependency_chain=("create", "dependent"),
            ),
        ),
        conflict=(
            ConflictEntry(
                project_id="ambig",
                candidates=(
                    ConflictCandidate(version_id="v1", filename="a.jar", source="seed"),
                    ConflictCandidate(version_id="v2", filename="b.jar", source="dependency"),
                ),
                resolution="seed_version_preferred",
            ),
        ),
        diagnostics=Diagnostics(
            errors=["e1", "e2"],
            warnings=["w1"],
            provider_requests_made=42,
        ),
    )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_minimal(tmp_path: Path) -> None:
    """A minimal report survives write/load cleanly."""
    path = tmp_path / "report.toml"
    write_report(path, _minimal_report())
    loaded = load_report(path)
    assert loaded.run_id == "abc"
    assert loaded.status == "complete"
    assert loaded.seed == ()
    assert loaded.resolved == ()


def test_round_trip_full(tmp_path: Path) -> None:
    """A full report with every optional section round-trips."""
    path = tmp_path / "report.toml"
    write_report(path, _full_report())
    loaded = load_report(path)
    assert len(loaded.seed) == 1
    assert len(loaded.resolved) == 1
    assert len(loaded.lost) == 1
    assert len(loaded.cascade) == 1
    assert len(loaded.conflict) == 1
    assert loaded.diagnostics.provider_requests_made == 42


def test_write_report_returns_sha256(tmp_path: Path) -> None:
    """write_report returns the SHA-256 of the bytes it wrote."""
    path = tmp_path / "report.toml"
    digest = write_report(path, _minimal_report())
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(digest) == 64


def test_compute_report_sha256(tmp_path: Path) -> None:
    """compute_report_sha256 returns the file's raw-byte SHA-256."""
    path = tmp_path / "report.toml"
    write_report(path, _minimal_report())
    assert compute_report_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Sorting (Section 4)
# ---------------------------------------------------------------------------


def test_resolved_entries_sorted_by_project_id(tmp_path: Path) -> None:
    """Resolved entries sort by project_id."""
    r1 = ResolvedEntry(
        filename="b.jar",
        provider="modrinth",
        is_in_house=False,
        is_dependency_only=False,
        side_provisional="both",
        side_source_provisional="unknown",
        project_id="z",
        version_id="v1",
        expected_hashes=(),
    )
    r2 = ResolvedEntry(
        filename="a.jar",
        provider="modrinth",
        is_in_house=False,
        is_dependency_only=False,
        side_provisional="both",
        side_source_provisional="unknown",
        project_id="a",
        version_id="v1",
        expected_hashes=(),
    )
    report = ResolutionReport(
        run_id="abc",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="complete",
        inputs=ReportInputs(manifest_sha256=_hex("1"), resolution_inputs_sha256=_hex("2"), side_overrides_sha256=_hex("3"), release_mode="upgrade"),
        target_runtime=TargetRuntime(minecraft_version="1.21.1", loader="forge", loader_version="52.0.1"),
        policy=ReportPolicy(selection="release_then_beta", no_version="ask"),
        seed=(),
        resolved=(r1, r2),
        lost=(),
        cascade=(),
        conflict=(),
        diagnostics=Diagnostics(),
    )
    path = tmp_path / "report.toml"
    write_report(path, report)
    text = path.read_text()
    # tomli_w emits string values with double quotes: project_id = "a".
    assert text.find('project_id = "a"') < text.find('project_id = "z"')


def test_expected_hashes_sorted_by_algorithm(tmp_path: Path) -> None:
    """expected_hashes sort by algorithm."""
    path = tmp_path / "report.toml"
    write_report(path, _full_report())
    text = path.read_text()
    # sha1 < sha512 lexicographically
    assert text.index("sha1") < text.index("sha512")


# ---------------------------------------------------------------------------
# Load validation
# ---------------------------------------------------------------------------


def _write_raw(tmp_path: Path, body: dict) -> Path:
    """Write a raw TOML body as report.toml."""
    path = tmp_path / "report.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    return path


def _minimal_raw() -> dict:
    """Return a minimal raw report dict."""
    return {
        "schema_version": 1,
        "run_id": "abc",
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "status": "complete",
        "inputs": {
            "manifest_sha256": _hex("1"),
            "resolution_inputs_sha256": _hex("2"),
            "side_overrides_sha256": _hex("3"),
            "release_mode": "upgrade",
        },
        "target_runtime": {
            "minecraft_version": "1.21.1",
            "loader": "forge",
            "loader_version": "52.0.1",
        },
        "policy": {"selection": "release_then_beta", "no_version": "ask"},
        "diagnostics": {"provider_requests_made": 0},
    }


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """A missing report file raises SchemaError."""
    with pytest.raises(SchemaError, match="not found"):
        load_report(tmp_path / "nope.toml")


def test_load_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML raises SchemaError."""
    p = tmp_path / "report.toml"
    p.write_text("not = valid = toml\n")
    with pytest.raises(SchemaError):
        load_report(p)


def test_load_bad_schema_version_raises(tmp_path: Path) -> None:
    """An unsupported schema_version raises SchemaError."""
    raw = _minimal_raw()
    raw["schema_version"] = 999
    p = _write_raw(tmp_path, raw)
    with pytest.raises(SchemaError, match="unsupported schema_version"):
        load_report(p)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("status", "bogus"),
        ("inputs.release_mode", "bogus"),
    ],
)
def test_load_enum_violation_raises(tmp_path: Path, field: str, bad_value: str) -> None:
    """Each documented enum field is validated on load."""
    raw = _minimal_raw()
    if field == "status":
        raw["status"] = bad_value
    elif field == "inputs.release_mode":
        raw["inputs"]["release_mode"] = bad_value
    p = _write_raw(tmp_path, raw)
    with pytest.raises(SchemaError):
        load_report(p)


def test_load_missing_required_field_raises(tmp_path: Path) -> None:
    """A missing required field raises SchemaError."""
    raw = _minimal_raw()
    del raw["run_id"]
    p = _write_raw(tmp_path, raw)
    with pytest.raises(SchemaError, match="missing required field"):
        load_report(p)
