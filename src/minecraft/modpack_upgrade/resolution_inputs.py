# src/minecraft/modpack_upgrade/resolution_inputs.py

"""Canonical `resolution_inputs_sha256` (Section 5.4.1, Section A2.2).

A *semantic* fingerprint of every input the resolver consumed and acted
on. Raw-file identity is recorded separately (`manifest_sha256`,
`side_overrides_sha256`) and is not in this snapshot - formatting-only
edits do not change this fingerprint.

Determinism requirements (Section 5.4.1):

  * Every collection sorted as noted in the snapshot definition.
  * Raw file hashes NOT in the snapshot.
  * `generated_at_utc` NOT included.
  * `jar_sha256` requires the seed jar; missing jar = exit 3 before
    fingerprinting (the seed loader enforces this).
  * In-house entries omit `current_version_id`; `build_id` present only
    when known.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import canonical
from .manifest import Manifest
from .overrides import SideOverrides  # see note below

__all__ = ["SeedDependency", "SeedSnapshotEntry", "compute_resolution_inputs_sha256"]


@dataclass(frozen=True)
class SeedDependency:
    """One `[[x-prismlauncher-dependencies]]` edge, normalized for hashing."""

    addon_id: str
    type: str
    namespace: str  # "project" | "modid"


@dataclass(frozen=True)
class SeedSnapshotEntry:
    """One `.pw.toml` entry as seen by the resolver (Section A2.2 `seed[]`)."""

    provider: str  # "modrinth" | "curseforge" | "in-house"
    current_filename: str
    jar_sha256: str
    side_declared: str  # "" when absent OR "unknown"
    dependencies: tuple[SeedDependency, ...]

    current_version_id: str | None = None  # external only
    build_id: str | None = None  # in-house, when known

    def sort_key(self) -> tuple[str, str]:
        """Builds a sort key tuple from the current version ID, build ID, or current filename, paired with the current filename.

        Returns:
            tuple[str, str]: A tuple containing the identifier and current filename used for sorting.
        """
        ident = self.current_version_id or self.build_id or self.current_filename
        return (ident, self.current_filename)


def _build_pw_toml_block(entry: SeedSnapshotEntry) -> dict[str, Any]:
    deps_sorted = sorted(
        ({"addon_id": d.addon_id, "type": d.type, "namespace": d.namespace} for d in entry.dependencies),
        key=lambda d: (d["addon_id"], d["type"], d["namespace"]),
    )
    return {"side_declared": entry.side_declared, "dependencies": deps_sorted}


def _build_seed_entry(entry: SeedSnapshotEntry) -> dict[str, Any]:
    block: dict[str, Any] = {
        "provider": entry.provider,
        "current_filename": entry.current_filename,
        "jar_sha256": entry.jar_sha256,
        "pw_toml": _build_pw_toml_block(entry),
    }
    if entry.provider != "in-house" and entry.current_version_id:
        block["current_version_id"] = entry.current_version_id
    if entry.provider == "in-house" and entry.build_id:
        block["build_id"] = entry.build_id
    return block


def _build_side_overrides_block(overrides: SideOverrides) -> dict[str, Any]:
    return {
        "by_id": dict(sorted(overrides.by_id.items())),
        "by_filename": dict(sorted(overrides.by_filename.items())),
        "deployment_tool_review": dict(sorted(overrides.deployment_tool_review.items())),
    }


def compute_resolution_inputs_sha256(
    *,
    release_mode: str,
    manifest: Manifest,
    in_house_filenames: Iterable[str],
    seed: Iterable[SeedSnapshotEntry],
    side_overrides: SideOverrides,
) -> str:
    """Return the RFC 8785 canonical SHA-256 per Section 5.4.1."""
    if release_mode not in ("update", "upgrade"):
        raise ValueError(f"release_mode must be 'update' or 'upgrade', got {release_mode!r}")

    seed_sorted = sorted(seed, key=lambda e: e.sort_key())
    snapshot: dict[str, Any] = {
        "release_mode": release_mode,
        "manifest": {
            "minecraft": {
                "version": manifest.minecraft_version,
                "loader": manifest.loader,
                "loader_version": manifest.loader_version,
            },
            "policy": {
                "selection": manifest.selection,
                "no_version": manifest.no_version,
            },
        },
        "in_house_filenames": sorted(in_house_filenames),
        "seed": [_build_seed_entry(e) for e in seed_sorted],
        "side_overrides": _build_side_overrides_block(side_overrides),
    }
    return canonical.digest(snapshot)
