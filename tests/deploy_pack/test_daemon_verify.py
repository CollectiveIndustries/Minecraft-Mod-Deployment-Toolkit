# tests/deploy_pack/test_daemon_verify.py

"""Release verification tests (Minecraft daemon)."""

from __future__ import annotations

from pathlib import Path

import tomli_w

from minecraft.deploy_pack.daemon.verify import verify_release
from minecraft.modpack_upgrade import slip as slip_mod


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _slip_body(release_id: str, meta_commit: str | None = None) -> dict:
    """Build a valid slip body."""
    meta = {
        "created_at_utc": "2026-10-12T00:00:00Z",
        "resolver_version": "minecraft-upgrade 1.3.0",
        "author": "test",
    }
    if meta_commit is not None:
        meta["commit_sha"] = meta_commit
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": "update",
        "meta": meta,
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
        "jar": [],
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
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_slip(slip_dir: Path, release_id: str, meta_commit: str | None = None) -> dict:
    """Write a valid slip file to ``slip_dir``; return the slip dict."""
    slip_dir.mkdir(parents=True, exist_ok=True)
    body = _slip_body(release_id, meta_commit)
    (slip_dir / f"{release_id}.toml").write_text(tomli_w.dumps(body), encoding="utf-8")
    return body


def _write_bundle(release_store: Path, release_id: str) -> None:
    """Create a minimal valid release bundle."""
    bundle = release_store / release_id
    (bundle / "artifacts").mkdir(parents=True)


def _write_pointer(slip_dir: Path, name: str, target: str) -> None:
    """Write a pointer file."""
    (slip_dir / name).write_text(target + "\n", encoding="utf-8")


def test_verify_ok(tmp_path: Path):
    """A valid slip and bundle yield status=ok."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "ok"
    assert result.release_id == "v1.0.2"
    assert result.content_sha256 is not None
    assert result.checks["slip_sha256"] is True
    assert result.checks["bundle_exists"] is True
    assert result.reasons == []


def test_verify_pointer_stable(tmp_path: Path):
    """A pointer resolves to the referenced release."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")
    _write_pointer(slip_dir, "stable", "v1.0.2.toml")

    result = verify_release(
        release_id=None,
        release_pointer="stable",
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "ok"
    assert result.release_id == "v1.0.2"


def test_verify_expected_slip_sha_matches(tmp_path: Path):
    """An expected_slip_sha256 that matches is accepted."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    body = _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=body["content_sha256"],
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "ok"
    assert result.checks["expected_slip_sha256"] is True


def test_verify_expected_commit_sha_matches(tmp_path: Path):
    """An expected_commit_sha that matches the slip meta is accepted."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    commit = "a" * 40
    _write_slip(slip_dir, "v1.0.2", meta_commit=commit)
    _write_bundle(release_store, "v1.0.2")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=commit,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "ok"
    assert result.checks["expected_commit_sha"] is True


def test_verify_pointer_missing(tmp_path: Path):
    """A missing pointer file yields pointer_missing."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    slip_dir.mkdir()

    result = verify_release(
        release_id=None,
        release_pointer="stable",
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "pointer_missing" in result.reasons


def test_verify_pointer_malformed(tmp_path: Path):
    """An empty pointer file yields pointer_malformed."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    slip_dir.mkdir()
    (slip_dir / "stable").write_text("", encoding="utf-8")

    result = verify_release(
        release_id=None,
        release_pointer="stable",
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "pointer_malformed" in result.reasons


def test_verify_slip_not_found(tmp_path: Path):
    """A missing slip file yields slip_not_found."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    slip_dir.mkdir()

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "slip_not_found" in result.reasons


def test_verify_slip_schema_error(tmp_path: Path):
    """A malformed slip yields slip_schema_error."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    slip_dir.mkdir()
    (slip_dir / "v1.0.2.toml").write_text("packing_slip_version = 999\n", encoding="utf-8")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "slip_schema_error" in result.reasons


def test_verify_slip_hash_mismatch(tmp_path: Path):
    """A slip whose content_sha256 is wrong yields slip_hash_mismatch."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    slip_dir.mkdir()
    body = _slip_body("v1.0.2")
    body["content_sha256"] = _hex("0")
    (slip_dir / "v1.0.2.toml").write_text(tomli_w.dumps(body), encoding="utf-8")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "slip_hash_mismatch" in result.reasons


def test_verify_expected_slip_sha_mismatch(tmp_path: Path):
    """A mismatched expected_slip_sha256 yields a block."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=_hex("f"),
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert result.checks["expected_slip_sha256"] is False
    assert "expected_slip_sha256_mismatch" in result.reasons


def test_verify_expected_commit_sha_missing_from_meta(tmp_path: Path):
    """An expected_commit_sha with no slip meta commit yields a block."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha="a" * 40,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert result.checks["expected_commit_sha"] is False
    assert "expected_commit_sha_unverifiable" in result.reasons


def test_verify_bundle_missing(tmp_path: Path):
    """A missing bundle directory yields bundle_missing."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    release_store.mkdir()

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.status == "blocked"
    assert "bundle_missing" in result.reasons
    assert result.checks["bundle_exists"] is False


def test_verify_bundle_manifest_present_flag(tmp_path: Path):
    """When the bundle has a manifest, bundle_manifest_present is True."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")
    (release_store / "v1.0.2" / "bundle-manifest.json").write_text("{}", encoding="utf-8")

    result = verify_release(
        release_id="v1.0.2",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
    )

    assert result.checks["bundle_manifest_present"] is True
