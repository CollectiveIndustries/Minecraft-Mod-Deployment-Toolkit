# src/minecraft/modpack_upgrade/report.py

"""Resolution report schema and serialization (Section 3, Section 4, Section 6.2 of Resolution_Report).

Deterministic ordering is applied at write time (Section 4). Document order is
not preserved across a load/serialize round-trip; Section 4 explicitly states
sorting "is for diffs, not hash reproducibility," and the report's
reproducibility identity is ``resolution_inputs_sha256``, not a re-serialize.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomli_w
except ImportError as exc:  # pragma: no cover
    raise ImportError("tomli-w is required; install with `pdm add tomli-w`") from exc

from .errors import SchemaError

__all__ = [
    "CascadeEntry",
    "ConflictCandidate",
    "ConflictEntry",
    "Diagnostics",
    "ExpectedHash",
    "LostEntry",
    "ReportInputs",
    "ReportPolicy",
    "ResolutionReport",
    "ResolvedEntry",
    "SeedEntry",
    "TargetRuntime",
    "compute_report_sha256",
    "load_report",
    "write_report",
]

SCHEMA_VERSION = 1

# Enumerations (Section 3.1)
_REPORT_STATUSES = frozenset({"complete", "paused_awaiting_acknowledgment", "failed"})
_RELEASE_MODES = frozenset({"update", "upgrade"})
_SIDE_PROVISIONAL = frozenset({"client", "server", "both", "unknown"})
_SIDE_SOURCES = frozenset({"override", "pw_toml", "provider", "unknown"})
_LOST_REASONS = frozenset({"no_compatible_version", "dependency_unsatisfiable"})
_CASCADE_REASON = "dependency_unsatisfiable"
_CASCADE_REASONS = frozenset({_CASCADE_REASON})
_CONFLICT_RESOLUTIONS = frozenset({"seed_version_preferred", "blocked"})
_HASH_ALGORITHMS = frozenset({"sha512", "sha256", "sha1", "md5"})


@dataclass(frozen=True)
class ExpectedHash:
    """Represents an expected hash consisting of an algorithm and its corresponding value."""

    algorithm: str
    value: str


@dataclass(frozen=True)
class ReportInputs:
    """Holds SHA-256 digests and release metadata used as inputs for generating a report."""

    manifest_sha256: str
    resolution_inputs_sha256: str
    side_overrides_sha256: str
    release_mode: str


@dataclass(frozen=True)
class TargetRuntime:
    """Describes the target Minecraft runtime, including version, loader, and loader version."""

    minecraft_version: str
    loader: str
    loader_version: str


@dataclass(frozen=True)
class ReportPolicy:
    """Specifies policy options controlling report selection behavior and handling of missing versions."""

    selection: str
    no_version: str


@dataclass(frozen=True)
class SeedEntry:
    """Represents a seed entry with file metadata, provider information, hashes, side declarations, and optional project and build identifiers."""

    filename: str
    provider: str
    jar_sha256: str
    side_declared: str
    side_effective: str
    project_id: str | None = None
    current_version_id: str | None = None
    build_id: str | None = None


@dataclass(frozen=True)
class ResolvedEntry:
    """Represents a resolved entry with its file, provider, and in-house status.

    Attributes:
        filename (str): Name of the resolved file.
        provider (str): Provider associated with the entry.
        is_in_house (bool): Whether the entry originates in-house.
        is_dependency_only (bool): Whether the entry is a dependency only.
        side_provisional (str): Provisional side classification.
        side_source_provisional (str): Provisional source-side classification.
        project_id (str | None): Optional project identifier.
        version_id (str | None): Optional version identifier.
        build_id (str | None): Optional build identifier.
        upgraded_from_version_id (str | None): Optional version identifier this entry was upgraded from.
        expected_hashes (tuple[ExpectedHash, ...]): Expected hashes for the entry.
    """

    filename: str
    provider: str
    is_in_house: bool
    is_dependency_only: bool
    side_provisional: str
    side_source_provisional: str
    project_id: str | None = None
    version_id: str | None = None
    build_id: str | None = None
    upgraded_from_version_id: str | None = None
    expected_hashes: tuple[ExpectedHash, ...] = ()


@dataclass(frozen=True)
class LostEntry:
    """Represents an entry that could not be resolved.

    Attributes:
        filename (str): Name of the lost file.
        provider (str): Provider associated with the entry.
        project_id (str): Project identifier the entry belongs to.
        reason (str): Short reason the entry was lost.
        detail (str): Detailed explanation of the loss.
    """

    filename: str
    provider: str
    project_id: str
    reason: str
    detail: str


@dataclass(frozen=True)
class CascadeEntry:
    """Represents an entry lost as a cascading effect of a dependency failure.

    Attributes:
        filename (str): Name of the affected file.
        provider (str): Provider associated with the entry.
        project_id (str): Project identifier the entry belongs to.
        reason (str): Short reason for the cascade.
        dependency_chain (tuple[str, ...]): Chain of dependencies that led to the cascade.
    """

    filename: str
    provider: str
    project_id: str
    reason: str
    dependency_chain: tuple[str, ...]


@dataclass(frozen=True)
class ConflictCandidate:
    """Represents a candidate involved in a conflict.

    Attributes:
        version_id (str): Version identifier of the candidate.
        filename (str): Name of the candidate file.
        source (str): Source of the candidate.
    """

    version_id: str
    filename: str
    source: str


@dataclass(frozen=True)
class ConflictEntry:
    """Represents a conflict detected for a project.

    Attributes:
        project_id (str): Project identifier the conflict belongs to.
        candidates (tuple[ConflictCandidate, ...]): Candidates involved in the conflict.
        resolution (str): Description of how the conflict was resolved.
    """

    project_id: str
    candidates: tuple[ConflictCandidate, ...]
    resolution: str


@dataclass(frozen=True)
class Diagnostics:
    """Aggregates diagnostics produced during a resolution run, including accumulated error messages, warning messages, and the number of provider requests made."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_requests_made: int = 0


@dataclass(frozen=True)
class ResolutionReport:
    """Represents the complete report of a resolution run, capturing the run identifier, generation timestamp, overall status, input configuration, target runtime, applied policy, seed entries, and the resolved, lost, cascade, and conflict entries along with collected diagnostics."""

    run_id: str
    generated_at_utc: str
    status: str
    inputs: ReportInputs
    target_runtime: TargetRuntime
    policy: ReportPolicy
    seed: tuple[SeedEntry, ...]
    resolved: tuple[ResolvedEntry, ...]
    lost: tuple[LostEntry, ...]
    cascade: tuple[CascadeEntry, ...]
    conflict: tuple[ConflictEntry, ...]
    diagnostics: Diagnostics


# ---------------------------------------------------------------------------
# Sorting (Section 4)
# ---------------------------------------------------------------------------


def _seed_sort_key(e: SeedEntry) -> tuple[str, str]:
    return (e.project_id or e.filename, e.filename)


def _resolved_sort_key(e: ResolvedEntry) -> tuple[str, str]:
    return (e.project_id or e.filename, e.filename)


def _lost_sort_key(e: LostEntry) -> tuple[str, str]:
    return (e.project_id or e.filename, e.filename)


def _cascade_sort_key(e: CascadeEntry) -> tuple[str, str]:
    return (e.project_id or e.filename, e.filename)


def _conflict_sort_key(e: ConflictEntry) -> str:
    return e.project_id


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _seed_block(e: SeedEntry) -> dict[str, Any]:
    out: dict[str, Any] = {"filename": e.filename, "provider": e.provider}
    if e.provider != "in-house":
        out["project_id"] = e.project_id
        out["current_version_id"] = e.current_version_id
    elif e.build_id is not None:
        out["build_id"] = e.build_id
    out["jar_sha256"] = e.jar_sha256
    out["side_declared"] = e.side_declared
    out["side_effective"] = e.side_effective
    return {k: v for k, v in out.items() if v is not None}


def _resolved_block(e: ResolvedEntry) -> dict[str, Any]:
    out: dict[str, Any] = {"filename": e.filename, "provider": e.provider}
    if e.provider != "in-house":
        out["project_id"] = e.project_id
        out["version_id"] = e.version_id
    elif e.build_id is not None:
        out["build_id"] = e.build_id
    out["is_in_house"] = e.is_in_house
    if e.upgraded_from_version_id is not None:
        out["upgraded_from_version_id"] = e.upgraded_from_version_id
    out["is_dependency_only"] = e.is_dependency_only
    out["side_provisional"] = e.side_provisional
    out["side_source_provisional"] = e.side_source_provisional
    if e.expected_hashes:
        out["expected_hashes"] = [{"algorithm": h.algorithm, "value": h.value} for h in sorted(e.expected_hashes, key=lambda h: h.algorithm)]
    return {k: v for k, v in out.items() if v is not None}


def _lost_block(e: LostEntry) -> dict[str, Any]:
    return {
        "filename": e.filename,
        "provider": e.provider,
        "project_id": e.project_id,
        "reason": e.reason,
        "detail": e.detail,
    }


def _cascade_block(e: CascadeEntry) -> dict[str, Any]:
    return {
        "filename": e.filename,
        "provider": e.provider,
        "project_id": e.project_id,
        "reason": e.reason,
        "dependency_chain": list(e.dependency_chain),
    }


def _conflict_block(e: ConflictEntry) -> dict[str, Any]:
    return {
        "project_id": e.project_id,
        "candidates": [{"version_id": c.version_id, "filename": c.filename, "source": c.source} for c in sorted(e.candidates, key=lambda c: c.version_id)],
        "resolution": e.resolution,
    }


def _diagnostics_block(d: Diagnostics) -> dict[str, Any]:
    out: dict[str, Any] = {"provider_requests_made": d.provider_requests_made}
    if d.errors:
        out["errors"] = sorted(d.errors)
    if d.warnings:
        out["warnings"] = sorted(d.warnings)
    return out


def _to_toml_dict(report: ResolutionReport) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": report.run_id,
        "generated_at_utc": report.generated_at_utc,
        "status": report.status,
        "inputs": {
            "manifest_sha256": report.inputs.manifest_sha256,
            "resolution_inputs_sha256": report.inputs.resolution_inputs_sha256,
            "side_overrides_sha256": report.inputs.side_overrides_sha256,
            "release_mode": report.inputs.release_mode,
        },
        "target_runtime": {
            "minecraft_version": report.target_runtime.minecraft_version,
            "loader": report.target_runtime.loader,
            "loader_version": report.target_runtime.loader_version,
        },
        "policy": {
            "selection": report.policy.selection,
            "no_version": report.policy.no_version,
        },
    }
    if report.seed:
        out["seed"] = [_seed_block(e) for e in sorted(report.seed, key=_seed_sort_key)]
    if report.resolved:
        out["resolved"] = [_resolved_block(e) for e in sorted(report.resolved, key=_resolved_sort_key)]
    if report.lost:
        out["lost"] = [_lost_block(e) for e in sorted(report.lost, key=_lost_sort_key)]
    if report.cascade:
        out["cascade"] = [_cascade_block(e) for e in sorted(report.cascade, key=_cascade_sort_key)]
    if report.conflict:
        out["conflict"] = [_conflict_block(e) for e in sorted(report.conflict, key=_conflict_sort_key)]
    out["diagnostics"] = _diagnostics_block(report.diagnostics)
    return out


def write_report(path: Path, report: ResolutionReport) -> str:
    """Serialize ``report`` to ``path`` atomically. Return the SHA-256 of the bytes.

    The SHA-256 returned here is what goes in ``state.toml`` as
    ``report_sha256``. It is *not* a reproducible content hash (Section 5) - it is
    the identity of these specific bytes.
    """
    body = _to_toml_dict(report)
    text = tomli_w.dumps(body)
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest()


def compute_report_sha256(path: Path) -> str:
    """Return the SHA-256 of the report file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _require(d: dict, key: str, path: str) -> Any:
    if key not in d:
        raise SchemaError(f"report: missing required field {path}{key}")
    return d[key]


def _check_enum(value: Any, allowed: frozenset[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SchemaError(f"report: {path} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _seed_from_dict(d: dict) -> SeedEntry:
    return SeedEntry(
        filename=_require(d, "filename", "[[seed]]."),
        provider=_require(d, "provider", "[[seed]]."),
        jar_sha256=_require(d, "jar_sha256", "[[seed]]."),
        side_declared=_require(d, "side_declared", "[[seed]]."),
        side_effective=_require(d, "side_effective", "[[seed]]."),
        project_id=d.get("project_id"),
        current_version_id=d.get("current_version_id"),
        build_id=d.get("build_id"),
    )


def _resolved_from_dict(d: dict) -> ResolvedEntry:
    return ResolvedEntry(
        filename=_require(d, "filename", "[[resolved]]."),
        provider=_require(d, "provider", "[[resolved]]."),
        is_in_house=bool(_require(d, "is_in_house", "[[resolved]].")),
        is_dependency_only=bool(_require(d, "is_dependency_only", "[[resolved]].")),
        side_provisional=_check_enum(
            _require(d, "side_provisional", "[[resolved]]."),
            _SIDE_PROVISIONAL,
            "[[resolved]].side_provisional",
        ),
        side_source_provisional=_check_enum(
            _require(d, "side_source_provisional", "[[resolved]]."),
            _SIDE_SOURCES,
            "[[resolved]].side_source_provisional",
        ),
        project_id=d.get("project_id"),
        version_id=d.get("version_id"),
        build_id=d.get("build_id"),
        upgraded_from_version_id=d.get("upgraded_from_version_id"),
        expected_hashes=tuple(
            ExpectedHash(
                algorithm=_check_enum(
                    h["algorithm"],
                    _HASH_ALGORITHMS,
                    "[[resolved]].expected_hashes[].algorithm",
                ),
                value=h["value"],
            )
            for h in (d.get("expected_hashes") or [])
        ),
    )


def _lost_from_dict(d: dict) -> LostEntry:
    # BUG-03: all fields routed through _require so missing keys surface as
    # SchemaError instead of KeyError.
    return LostEntry(
        filename=_require(d, "filename", "[[lost]]."),
        provider=_require(d, "provider", "[[lost]]."),
        project_id=_require(d, "project_id", "[[lost]]."),
        reason=_check_enum(_require(d, "reason", "[[lost]]."), _LOST_REASONS, "[[lost]].reason"),
        detail=_require(d, "detail", "[[lost]]."),
    )


def _cascade_from_dict(d: dict) -> CascadeEntry:
    # BUG-03: same treatment as _lost_from_dict.
    return CascadeEntry(
        filename=_require(d, "filename", "[[cascade]]."),
        provider=_require(d, "provider", "[[cascade]]."),
        project_id=_require(d, "project_id", "[[cascade]]."),
        reason=_check_enum(_require(d, "reason", "[[cascade]]."), _CASCADE_REASONS, "[[cascade]].reason"),
        dependency_chain=tuple(_require(d, "dependency_chain", "[[cascade]].")),
    )


def _conflict_from_dict(d: dict) -> ConflictEntry:
    # BUG-03: same treatment; candidates also use _require so missing
    # version_id/filename/source surface as SchemaError.
    raw_candidates = _require(d, "candidates", "[[conflict]].")
    return ConflictEntry(
        project_id=_require(d, "project_id", "[[conflict]]."),
        candidates=tuple(
            ConflictCandidate(
                version_id=_require(c, "version_id", "[[conflict]].candidates[]."),
                filename=_require(c, "filename", "[[conflict]].candidates[]."),
                source=_require(c, "source", "[[conflict]].candidates[]."),
            )
            for c in raw_candidates
        ),
        resolution=_check_enum(
            _require(d, "resolution", "[[conflict]]."),
            _CONFLICT_RESOLUTIONS,
            "[[conflict]].resolution",
        ),
    )


def load_report(path: Path) -> ResolutionReport:
    """Parse a resolution report from disk. Raises ``SchemaError`` on any problem."""
    if not path.is_file():
        raise SchemaError(f"report not found: {path}")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SchemaError(f"could not read {path}: {exc}") from exc

    if _require(data, "schema_version", "") != SCHEMA_VERSION:
        raise SchemaError(f"report: unsupported schema_version {data['schema_version']!r}")
    status = _check_enum(_require(data, "status", ""), _REPORT_STATUSES, "status")
    inputs_raw = _require(data, "inputs", "")
    policy_raw = _require(data, "policy", "")
    runtime_raw = _require(data, "target_runtime", "")
    diagnostics_raw = data.get("diagnostics", {})

    return ResolutionReport(
        run_id=_require(data, "run_id", ""),
        generated_at_utc=_require(data, "generated_at_utc", ""),
        status=status,
        inputs=ReportInputs(
            manifest_sha256=_require(inputs_raw, "manifest_sha256", "[inputs]."),
            resolution_inputs_sha256=_require(inputs_raw, "resolution_inputs_sha256", "[inputs]."),
            side_overrides_sha256=_require(inputs_raw, "side_overrides_sha256", "[inputs]."),
            release_mode=_check_enum(
                _require(inputs_raw, "release_mode", "[inputs]."),
                _RELEASE_MODES,
                "[inputs].release_mode",
            ),
        ),
        target_runtime=TargetRuntime(
            minecraft_version=_require(runtime_raw, "minecraft_version", "[target_runtime]."),
            loader=_require(runtime_raw, "loader", "[target_runtime]."),
            loader_version=_require(runtime_raw, "loader_version", "[target_runtime]."),
        ),
        policy=ReportPolicy(
            selection=_require(policy_raw, "selection", "[policy]."),
            no_version=_require(policy_raw, "no_version", "[policy]."),
        ),
        seed=tuple(_seed_from_dict(e) for e in data.get("seed", [])),
        resolved=tuple(_resolved_from_dict(e) for e in data.get("resolved", [])),
        lost=tuple(_lost_from_dict(e) for e in data.get("lost", [])),
        cascade=tuple(_cascade_from_dict(e) for e in data.get("cascade", [])),
        conflict=tuple(_conflict_from_dict(e) for e in data.get("conflict", [])),
        diagnostics=Diagnostics(
            errors=list(diagnostics_raw.get("errors", [])),
            warnings=list(diagnostics_raw.get("warnings", [])),
            provider_requests_made=int(diagnostics_raw.get("provider_requests_made", 0)),
        ),
    )
