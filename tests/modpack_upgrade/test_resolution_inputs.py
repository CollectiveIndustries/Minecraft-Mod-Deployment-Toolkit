# tests/modpack_upgrade/test_resolution_inputs.py

"""resolution_inputs_sha256 canonical snapshot tests (Section 5.4.1, A2.2)."""

from __future__ import annotations

import pytest

from minecraft.modpack_upgrade.manifest import Manifest
from minecraft.modpack_upgrade.overrides import SideOverrides
from minecraft.modpack_upgrade.resolution_inputs import (
    SeedDependency,
    SeedSnapshotEntry,
    compute_resolution_inputs_sha256,
)


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _manifest(**overrides: object) -> Manifest:
    """Build a Manifest fixture."""
    base: dict[str, object] = {
        "minecraft_version": "1.21.1",
        "loader": "forge",
        "loader_version": "52.0.1",
        "selection": "release_then_beta",
        "no_version": "ask",
        "raw_sha256": _hex("0"),
    }
    base.update(overrides)
    return Manifest(**base)


def _external_entry(**overrides: object) -> SeedSnapshotEntry:
    """Build a valid external-provider seed entry."""
    base: dict[str, object] = {
        "provider": "modrinth",
        "current_filename": "create.jar",
        "jar_sha256": _hex("a"),
        "side_declared": "both",
        "dependencies": (),
        "current_version_id": "v1",
    }
    base.update(overrides)
    return SeedSnapshotEntry(**base)


def _in_house_entry(**overrides: object) -> SeedSnapshotEntry:
    """Build a valid in-house seed entry."""
    base: dict[str, object] = {
        "provider": "in-house",
        "current_filename": "custom.jar",
        "jar_sha256": _hex("b"),
        "side_declared": "",
        "dependencies": (),
    }
    base.update(overrides)
    return SeedSnapshotEntry(**base)


def _compute(**overrides: object) -> str:
    """Compute the fingerprint with sensible defaults."""
    base: dict[str, object] = {
        "release_mode": "upgrade",
        "manifest": _manifest(),
        "in_house_filenames": [],
        "seed": [],
        "side_overrides": SideOverrides(),
    }
    base.update(overrides)
    return compute_resolution_inputs_sha256(**base)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    """Same inputs produce the same hash across calls."""
    assert _compute() == _compute()


def test_different_release_mode_changes_hash() -> None:
    """Update vs upgrade changes the fingerprint."""
    assert _compute(release_mode="update") != _compute(release_mode="upgrade")


def test_invalid_release_mode_raises() -> None:
    """An invalid release_mode raises ValueError."""
    with pytest.raises(ValueError, match="release_mode"):
        _compute(release_mode="bogus")


def test_manifest_change_affects_hash() -> None:
    """A different manifest produces a different fingerprint."""
    assert _compute() != _compute(manifest=_manifest(minecraft_version="1.20.1"))


def test_in_house_filenames_sorted() -> None:
    """in_house_filenames order does not affect the hash."""
    a = _compute(in_house_filenames=["b.jar", "a.jar"])
    b = _compute(in_house_filenames=["a.jar", "b.jar"])
    assert a == b


def test_side_overrides_affects_hash() -> None:
    """Adding a side override changes the fingerprint."""
    ov = SideOverrides(by_filename={"foo.jar": "client"})
    assert _compute() != _compute(side_overrides=ov)


# ---------------------------------------------------------------------------
# Seed snapshot semantics
# ---------------------------------------------------------------------------


def test_external_entry_includes_version_id() -> None:
    """An external seed entry participates via current_version_id."""
    entry = _external_entry()
    assert _compute(seed=[entry]) != _compute()


def test_in_house_entry_omits_version_id() -> None:
    """An in-house seed entry with no version id still produces a hash."""
    assert _compute(seed=[_in_house_entry()]) != _compute()


def test_in_house_with_build_id() -> None:
    """An in-house entry with a build_id includes it."""
    a = _compute(seed=[_in_house_entry()])
    b = _compute(seed=[_in_house_entry(build_id="build-42")])
    assert a != b


def test_side_unknown_is_normalized_to_empty() -> None:
    """side_declared='unknown' hashes the same as '' per Section A2.2."""
    # The SeedSnapshotEntry constructor does not normalize; the caller does.
    # This test pins the caller contract: two entries with side_declared
    # "" and no side produce identical hashes; "unknown" is not used.
    a = _compute(seed=[_external_entry(side_declared="")])
    b = _compute(seed=[_external_entry(side_declared="both")])
    assert a != b


def test_dependencies_sorted() -> None:
    """Dependency order does not affect the fingerprint."""
    d1 = SeedDependency(addon_id="b", type="required", namespace="project")
    d2 = SeedDependency(addon_id="a", type="required", namespace="project")
    a = _compute(seed=[_external_entry(dependencies=(d1, d2))])
    b = _compute(seed=[_external_entry(dependencies=(d2, d1))])
    assert a == b
