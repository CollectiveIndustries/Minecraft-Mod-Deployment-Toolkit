# tests/modpack_upgrade/test_slip_context.py

"""SlipContext construction and validation tests (spec Section 5.8)."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from minecraft.modpack_upgrade import slip
from minecraft.modpack_upgrade.errors import UpgradeError
from minecraft.modpack_upgrade.slip_context import (
    build_integration_context,
    build_release_context,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _hex(ch: str) -> str:
    """Return a 64-character hex string composed of the repeated character ``ch``."""
    return ch * 64


def _jar_entry(filename: str, content: bytes) -> dict:
    """Build a valid [[jar]] entry for ``filename`` whose sha256 matches ``content``."""
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "side": "both",
        "provider": "modrinth",
        "pw_toml": f"{filename}.pw.toml",
        "pw_toml_sha256": _hex("0"),
        "dependencies": [],
    }


def _valid_slip_dict(jar_entries: list[dict] | None = None) -> dict:
    """Build a valid slip dict with the given [[jar]] entries; content_sha256 computed."""
    body = {
        "packing_slip_version": 1,
        "release_id": "v1.0.0",
        "release_mode": "update",
        "meta": {
            "created_at_utc": "2026-10-12T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.0",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.20.1",
            "loader_type": "forge",
            "loader_version": "47.4.10",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + _hex("1"),
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": _hex("2"),
                "kubejs_tree": _hex("3"),
                "compose_tree": _hex("4"),
                "deploy_toml_tree": _hex("5"),
            },
        },
        "fingerprints": {
            "manifest_sha256": _hex("6"),
            "runtime_sha256": _hex("7"),
            "server_inputs_sha256": _hex("8"),
            "client_inputs_sha256": _hex("9"),
        },
        "jar": jar_entries or [],
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
    body["content_sha256"] = slip.compute_content_sha256(body)
    return body


def _build_slip(jar_entries: list[dict] | None = None) -> slip.PackingSlip:
    """Parse and return a valid PackingSlip."""
    return slip.parse_slip(_valid_slip_dict(jar_entries))


def _materialize_bundle(
    bundle_root: Path,
    jar_contents: dict[str, bytes],
) -> None:
    """Create a well-formed release bundle directory tree."""
    (bundle_root / "artifacts").mkdir(parents=True)
    (bundle_root / "config").mkdir()
    (bundle_root / "kubejs").mkdir()
    (bundle_root / "runtime").mkdir()
    for filename, content in jar_contents.items():
        (bundle_root / "artifacts" / filename).write_bytes(content)


# ===========================================================================
# build_release_context - happy path
# ===========================================================================


def test_build_release_context_valid(tmp_path: Path) -> None:
    """A well-formed bundle produces a SlipContext with the expected fields."""
    content = b"mod content"
    parsed = _build_slip([_jar_entry("mod.jar", content)])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {"mod.jar": content})

    ctx = build_release_context(parsed, bundle_root)

    assert ctx.mode == "release"
    assert ctx.slip is parsed
    assert ctx.bundle_root == bundle_root
    assert ctx.config_source == bundle_root / "config"
    assert ctx.kubejs_source == bundle_root / "kubejs"
    assert ctx.runtime_payload == bundle_root / "runtime"
    assert ctx.captured_head is None
    assert dict(ctx.jar_sources) == {"mod.jar": bundle_root / "artifacts" / "mod.jar"}


def test_build_release_context_multiple_jars(tmp_path: Path) -> None:
    """Multiple jars are all verified and mapped."""
    contents = {"a.jar": b"aaa", "b.jar": b"bbb", "c.jar": b"ccc"}
    entries = [_jar_entry(name, data) for name, data in contents.items()]
    parsed = _build_slip(entries)
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, contents)

    ctx = build_release_context(parsed, bundle_root)

    assert set(ctx.jar_sources.keys()) == {"a.jar", "b.jar", "c.jar"}


def test_build_release_context_empty_jar_list(tmp_path: Path) -> None:
    """A slip with no jars produces an empty jar_sources mapping."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {})

    ctx = build_release_context(parsed, bundle_root)

    assert dict(ctx.jar_sources) == {}


# ===========================================================================
# build_release_context - error cases
# ===========================================================================


def test_build_release_context_missing_bundle_root(tmp_path: Path) -> None:
    """A nonexistent bundle root raises UpgradeError."""
    parsed = _build_slip([])
    missing = tmp_path / "does-not-exist"

    with pytest.raises(UpgradeError, match="bundle root not found"):
        build_release_context(parsed, missing)


def test_build_release_context_missing_artifacts(tmp_path: Path) -> None:
    """A bundle missing its artifacts directory raises UpgradeError."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "config").mkdir()
    (bundle_root / "kubejs").mkdir()
    (bundle_root / "runtime").mkdir()

    with pytest.raises(UpgradeError, match="artifacts directory not found"):
        build_release_context(parsed, bundle_root)


def test_build_release_context_missing_config(tmp_path: Path) -> None:
    """A bundle missing its config directory raises UpgradeError."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "artifacts").mkdir()
    (bundle_root / "kubejs").mkdir()
    (bundle_root / "runtime").mkdir()

    with pytest.raises(UpgradeError, match="config directory not found"):
        build_release_context(parsed, bundle_root)


def test_build_release_context_missing_kubejs(tmp_path: Path) -> None:
    """A bundle missing its kubejs directory raises UpgradeError."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "artifacts").mkdir()
    (bundle_root / "config").mkdir()
    (bundle_root / "runtime").mkdir()

    with pytest.raises(UpgradeError, match="kubejs directory not found"):
        build_release_context(parsed, bundle_root)


def test_build_release_context_missing_runtime(tmp_path: Path) -> None:
    """A bundle missing its runtime directory raises UpgradeError."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "artifacts").mkdir()
    (bundle_root / "config").mkdir()
    (bundle_root / "kubejs").mkdir()

    with pytest.raises(UpgradeError, match="runtime directory not found"):
        build_release_context(parsed, bundle_root)


def test_build_release_context_missing_jar(tmp_path: Path) -> None:
    """A slip-declared jar absent from artifacts raises UpgradeError."""
    content = b"mod content"
    parsed = _build_slip([_jar_entry("mod.jar", content)])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {})  # no jars on disk

    with pytest.raises(UpgradeError, match="jar not found"):
        build_release_context(parsed, bundle_root)


def test_build_release_context_wrong_hash(tmp_path: Path) -> None:
    """A jar whose content does not match its slip hash raises UpgradeError."""
    content = b"expected content"
    parsed = _build_slip([_jar_entry("mod.jar", content)])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {"mod.jar": b"tampered content"})

    with pytest.raises(UpgradeError, match="jar hash mismatch"):
        build_release_context(parsed, bundle_root)


# ===========================================================================
# build_integration_context - happy path
# ===========================================================================


def test_build_integration_context_valid(tmp_path: Path) -> None:
    """A well-formed candidate with a captured HEAD produces a SlipContext."""
    content = b"mod content"
    parsed = _build_slip([_jar_entry("mod.jar", content)])
    candidate_root = tmp_path / "candidate"
    _materialize_bundle(candidate_root, {"mod.jar": content})
    config_source = tmp_path / "git" / "sync" / "config"
    kubejs_source = tmp_path / "git" / "sync" / "kubejs"
    config_source.mkdir(parents=True)
    kubejs_source.mkdir(parents=True)
    head = "abc123def456"

    ctx = build_integration_context(
        parsed,
        candidate_root,
        config_source=config_source,
        kubejs_source=kubejs_source,
        captured_head=head,
    )

    assert ctx.mode == "integration"
    assert ctx.slip is parsed
    assert ctx.bundle_root == candidate_root
    assert ctx.config_source == config_source
    assert ctx.kubejs_source == kubejs_source
    assert ctx.runtime_payload == candidate_root / "runtime"
    assert ctx.captured_head == head


# ===========================================================================
# build_integration_context - error cases
# ===========================================================================


def test_build_integration_context_empty_head(tmp_path: Path) -> None:
    """An empty captured_head raises UpgradeError."""
    parsed = _build_slip([])
    candidate_root = tmp_path / "candidate"
    _materialize_bundle(candidate_root, {})
    config_source = tmp_path / "config"
    kubejs_source = tmp_path / "kubejs"
    config_source.mkdir()
    kubejs_source.mkdir()

    with pytest.raises(UpgradeError, match="captured_head is required"):
        build_integration_context(
            parsed,
            candidate_root,
            config_source=config_source,
            kubejs_source=kubejs_source,
            captured_head="",
        )


def test_build_integration_context_missing_candidate_root(tmp_path: Path) -> None:
    """A nonexistent candidate root raises UpgradeError."""
    parsed = _build_slip([])
    missing = tmp_path / "does-not-exist"
    config_source = tmp_path / "config"
    kubejs_source = tmp_path / "kubejs"
    config_source.mkdir()
    kubejs_source.mkdir()

    with pytest.raises(UpgradeError, match="candidate root not found"):
        build_integration_context(
            parsed,
            missing,
            config_source=config_source,
            kubejs_source=kubejs_source,
            captured_head="abc",
        )


def test_build_integration_context_missing_config_source(tmp_path: Path) -> None:
    """A nonexistent config source raises UpgradeError."""
    parsed = _build_slip([])
    candidate_root = tmp_path / "candidate"
    _materialize_bundle(candidate_root, {})
    kubejs_source = tmp_path / "kubejs"
    kubejs_source.mkdir()

    with pytest.raises(UpgradeError, match="config directory not found"):
        build_integration_context(
            parsed,
            candidate_root,
            config_source=tmp_path / "missing-config",
            kubejs_source=kubejs_source,
            captured_head="abc",
        )


def test_build_integration_context_missing_kubejs_source(tmp_path: Path) -> None:
    """A nonexistent kubejs source raises UpgradeError."""
    parsed = _build_slip([])
    candidate_root = tmp_path / "candidate"
    _materialize_bundle(candidate_root, {})
    config_source = tmp_path / "config"
    config_source.mkdir()

    with pytest.raises(UpgradeError, match="kubejs directory not found"):
        build_integration_context(
            parsed,
            candidate_root,
            config_source=config_source,
            kubejs_source=tmp_path / "missing-kubejs",
            captured_head="abc",
        )


# ===========================================================================
# Immutability
# ===========================================================================


def test_jar_sources_is_immutable(tmp_path: Path) -> None:
    """jar_sources is a MappingProxyType and rejects assignment."""
    content = b"x"
    parsed = _build_slip([_jar_entry("mod.jar", content)])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {"mod.jar": content})

    ctx = build_release_context(parsed, bundle_root)

    with pytest.raises(TypeError):
        ctx.jar_sources["new.jar"] = Path("/nowhere")  # type: ignore[index]


def test_slip_context_is_frozen(tmp_path: Path) -> None:
    """SlipContext is a frozen dataclass and rejects attribute assignment."""
    parsed = _build_slip([])
    bundle_root = tmp_path / "bundle"
    _materialize_bundle(bundle_root, {})

    ctx = build_release_context(parsed, bundle_root)

    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.mode = "integration"  # type: ignore[misc]
