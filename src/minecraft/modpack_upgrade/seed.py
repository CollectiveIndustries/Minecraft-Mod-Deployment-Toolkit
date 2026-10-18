# src/minecraft/modpack_upgrade/seed.py

"""Seed loader: `.index/*.pw.toml` -> resolver input (Section 6, Section 6.1, Section 3 of Resolution_Report).

Responsibilities:
  * Enumerate `.index/*.pw.toml`.
  * Reject a `.pw.toml` whose `side` value is malformed (Section 3.1 of the schema:
    valid values are `client`/`server`/`both`/`unknown`/absent; anything
    else is a schema error, exit 3).
  * Verify each referenced jar exists in the warehouse directory.
  * Hash each jar as `jar_sha256`.
  * Classify provider as `in-house` (via `[in-house].filenames`), `modrinth`,
    `curseforge`, or `unknown`.
  * Produce a `SeedSnapshotEntry` for `resolution_inputs_sha256` and a
    `SeedEntry` for the resolution report.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import SchemaError
from .report import SeedEntry
from .resolution_inputs import SeedDependency, SeedSnapshotEntry

__all__ = ["LoadedSeed", "load_seed"]

_VALID_PW_SIDES = frozenset({"client", "server", "both", "unknown"})


@dataclass(frozen=True)
class LoadedSeed:
    """Both the report-facing and snapshot-facing views of the seed."""

    report_entries: tuple[SeedEntry, ...]
    snapshot_entries: tuple[SeedSnapshotEntry, ...]


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _extract_update_block(data: dict) -> tuple[str, str | None, str | None]:
    """Return (provider, project_id, version_id) from the update block.

    A seed that carries both blocks is malformed (schema Section 5.7 rule 5 applies
    to writer output; for seed reading we pick CurseForge since the writer
    never produces both - but this ordering matches `parse_prism_toml`).
    """
    cf = data.get("update", {}).get("curseforge")
    if isinstance(cf, dict):
        pid = cf.get("project-id")
        fid = cf.get("file-id")
        if pid is None:
            raise SchemaError("update.curseforge present but project-id missing")
        return ("curseforge", str(pid), (str(fid) if fid is not None else None))
    mr = data.get("update", {}).get("modrinth")
    if isinstance(mr, dict):
        pid = mr.get("mod-id")
        ver = mr.get("version")
        if pid is None:
            raise SchemaError("update.modrinth present but mod-id missing")
        return ("modrinth", str(pid), (str(ver) if ver is not None else None))
    return ("unknown", None, None)


def _read_side(data: dict, source: Path) -> str:
    """Return the side field, validating its type/shape.

    Valid: absent (returns "unknown"), "client", "server", "both", "unknown".
    Malformed (int, list, other string) -> SchemaError.
    """
    raw = data.get("side")
    if raw is None:
        return "unknown"
    if not isinstance(raw, str):
        raise SchemaError(f"{source}: side must be a string or absent, got {type(raw).__name__}")
    if raw not in _VALID_PW_SIDES:
        raise SchemaError(f"{source}: side={raw!r} is not one of {sorted(_VALID_PW_SIDES)}")
    return raw


def _read_dependencies(data: dict) -> tuple[SeedDependency, ...]:
    blocks = data.get("x-prismlauncher-dependencies") or []
    if not isinstance(blocks, list):
        raise SchemaError("x-prismlauncher-dependencies must be an array")
    deps: list[SeedDependency] = []
    for b in blocks:
        if not isinstance(b, dict) or "addonId" not in b:
            continue
        deps.append(
            SeedDependency(
                addon_id=str(b["addonId"]),
                type=str(b.get("type", "")).lower(),
                namespace="project",  # .pw.toml emits project-ID edges only (Section 5.7 rule 6)
            )
        )
    return tuple(sorted(deps, key=lambda d: (d.addon_id, d.type, d.namespace)))


def _build_entries(
    index_dir: Path,
    modpack_dir: Path,
    in_house_filenames: set[str],
) -> tuple[list[SeedEntry], list[SeedSnapshotEntry]]:
    report_entries: list[SeedEntry] = []
    snapshot_entries: list[SeedSnapshotEntry] = []

    for toml_path in sorted(index_dir.glob("*.pw.toml")):
        try:
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SchemaError(f"{toml_path}: could not parse: {exc}") from exc

        filename = data.get("filename")
        if not isinstance(filename, str) or not filename:
            raise SchemaError(f"{toml_path}: filename is required")

        jar_path = modpack_dir / filename
        if not jar_path.is_file():
            raise SchemaError(f"{toml_path}: seed jar missing: {jar_path}")
        jar_sha256 = _hash_file(jar_path)

        side_declared = _read_side(data, toml_path)
        deps = _read_dependencies(data)

        if filename in in_house_filenames:
            provider = "in-house"
            project_id = None
            current_version_id = None
        else:
            provider, project_id, current_version_id = _extract_update_block(data)

        # side_effective at seed level equals side_declared (Section 3.1: "provisional
        # sides" are computed later; the seed entry reports what the .pw.toml
        # declares). The report field is `side_effective` and by convention
        # the seed-side view is what the raw file says.
        report_entries.append(
            SeedEntry(
                filename=filename,
                provider=provider,
                jar_sha256=jar_sha256,
                side_declared=side_declared,
                side_effective=side_declared,
                project_id=project_id,
                current_version_id=current_version_id,
            )
        )

        snapshot_entries.append(
            SeedSnapshotEntry(
                provider=provider,
                current_filename=filename,
                jar_sha256=jar_sha256,
                # Section A2.2: "" when absent OR "unknown"
                side_declared="" if side_declared == "unknown" else side_declared,
                dependencies=deps,
                current_version_id=current_version_id,
            )
        )

    return (report_entries, snapshot_entries)


def load_seed(
    index_dir: Path,
    modpack_dir: Path,
    in_house_filenames: list[str],
) -> LoadedSeed:
    """Load every `.pw.toml` in `index_dir` as seed (Section 6).

    `modpack_dir` is where jars live (usually the parent of `index_dir`).
    `in_house_filenames` is the sorted list from `[in-house].filenames`.
    """
    if not index_dir.is_dir():
        raise SchemaError(f"seed index directory not found: {index_dir}")
    in_house = set(in_house_filenames)
    report_entries, snapshot_entries = _build_entries(index_dir, modpack_dir, in_house)
    return LoadedSeed(report_entries=tuple(report_entries), snapshot_entries=tuple(snapshot_entries))
