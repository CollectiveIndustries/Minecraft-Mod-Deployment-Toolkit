# tests/deploy_pack/test_verify_chain.py

"""Chain-walk verification via ``daemon.verify`` (Slice 12).

Complements ``test_daemon_verify.py`` (slice 11 local-only checks).
Every test here passes ``registry_path`` and ``trusted_keys_path`` so
``verify_release`` dispatches to ``_verify_chain_and_bundle`` and uses
:mod:`minecraft.modpack_upgrade.registry_verify` under the hood.

Each test builds a real signed Ed25519 chain and a real signed bundle
manifest via the same primitives ``publish`` uses, so a wiring bug
between daemon and publisher cannot be masked by fakes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from minecraft.deploy_pack.daemon.verify import verify_release
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.registry_verify import (
    GENESIS_HASH,
    compute_entry_hash,
)
from minecraft.modpack_upgrade.signing import SigningKey, sign_json

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> SigningKey:
    """Return a fresh Ed25519 signing key."""
    return SigningKey.from_private_key(Ed25519PrivateKey.generate())


@pytest.fixture
def trusted_keys_path(tmp_path: Path, key: SigningKey) -> Path:
    """Write a trusted-keys file pinning ``key`` and return its path."""
    path = tmp_path / "trusted_keys.toml"
    pem = key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    path.write_bytes(tomli_w.dumps({"keys": {key.key_id: pem}}).encode("utf-8"))
    return path


def _slip_body(release_id: str) -> dict:
    """Build a minimal valid slip body with a fresh ``content_sha256``."""
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": "upgrade",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + "0" * 64,
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": "a" * 40,
                "kubejs_tree": "a" * 40,
                "compose_tree": "a" * 40,
                "deploy_toml_tree": "a" * 40,
            },
        },
        "fingerprints": {
            "manifest_sha256": "0" * 64,
            "resolution_inputs_sha256": "0" * 64,
            "runtime_sha256": "0" * 64,
            "server_inputs_sha256": "0" * 64,
            "client_inputs_sha256": "0" * 64,
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


def _write_signed_bundle(
    bundle_dir: Path,
    key: SigningKey,
    slip_body: dict,
) -> str:
    """Write a signed bundle manifest and return its SHA-256.

    Idempotent on the directory tree: ``bundle_dir`` and
    ``bundle_dir/artifacts`` may or may not already exist when the
    helper is called. The manifest file itself is overwritten if present
    -- the caller is responsible for ordering if that matters.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "artifacts").mkdir(exist_ok=True)
    projection = {
        "schema_version": 1,
        "release_id": slip_body["release_id"],
        "slip_content_sha256": slip_body["content_sha256"],
        "approval_sha256": "a" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": [],
    }
    sig_bytes = sign_json(key, projection)
    manifest = {
        **projection,
        "signature": base64.b64encode(sig_bytes).decode("ascii"),
        "key_id": key.key_id,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (bundle_dir / "bundle-manifest.json").write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def _write_chain_entry(
    registry_path: Path,
    key: SigningKey,
    *,
    release_id: str,
    slip_sha: str,
    bundle_manifest_sha: str,
    previous: str = GENESIS_HASH,
) -> dict:
    """Append one signed entry to the registry and return the entry dict."""
    fields = {
        "schema_version": 1,
        "release_id": release_id,
        "tag_object_sha": "a" * 40,
        "tag_object_format": "sha1",
        "commit_sha": "b" * 40,
        "slip_content_sha256": slip_sha,
        "bundle_manifest_sha256": bundle_manifest_sha,
        "previous_entry_hash": previous,
    }
    entry_hash = compute_entry_hash(fields)
    sig_bytes = sign_json(key, fields)
    entry = {
        **fields,
        "entry_hash": entry_hash,
        "signature": base64.b64encode(sig_bytes).decode("ascii"),
        "key_id": key.key_id,
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def _setup_full_release(
    tmp_path: Path,
    key: SigningKey,
    release_id: str = "v1.0.0",
) -> tuple[Path, Path, Path, Path, dict]:
    """Return ``(slip_dir, release_store, registry_path, trusted_keys_path, slip_body)``.

    Writes a valid slip, a signed bundle, a signed chain entry, and the
    trusted-keys file. A test can tamper with any of them to exercise
    failure branches.
    """
    slip_dir = tmp_path / "slips"
    slip_dir.mkdir()
    slip_body = _slip_body(release_id)
    (slip_dir / f"{release_id}.toml").write_bytes(tomli_w.dumps(slip_body).encode("utf-8"))

    release_store = tmp_path / "releases"
    bundle_dir = release_store / release_id
    manifest_sha = _write_signed_bundle(bundle_dir, key, slip_body)

    registry_path = tmp_path / "release-index.jsonl"
    _write_chain_entry(
        registry_path,
        key,
        release_id=release_id,
        slip_sha=slip_body["content_sha256"],
        bundle_manifest_sha=manifest_sha,
    )

    pem = key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trusted_keys_path = tmp_path / "trusted_keys.toml"
    trusted_keys_path.write_bytes(tomli_w.dumps({"keys": {key.key_id: pem}}).encode("utf-8"))

    return slip_dir, release_store, registry_path, trusted_keys_path, slip_body


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_chain_verify_ok(tmp_path: Path, key: SigningKey) -> None:
    """A valid chain and valid bundle yield status ok and chain_ok True."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "ok"
    assert result.chain_ok is True
    assert result.chain_failures == []
    assert result.checks["chain"] is True
    assert result.checks["release_present"] is True
    assert result.checks["bundle_present"] is True
    assert result.checks["bundle_manifest"] is True
    assert result.checks["trusted_keys"] is True


# ---------------------------------------------------------------------------
# Chain failure
# ---------------------------------------------------------------------------


def test_chain_broken_detected(tmp_path: Path, key: SigningKey) -> None:
    """A tampered chain entry blocks with chain_ok False and a reason code."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)

    # Tamper: rewrite the last line to flip one field.
    lines = registry.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["commit_sha"] = "0" * 40  # invalidates entry_hash
    registry.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is False
    assert "entry_hash_mismatch" in result.chain_failures
    assert any("chain_failure[entry_hash_mismatch]" in r for r in result.reasons)


def test_chain_unknown_key_detected(tmp_path: Path, key: SigningKey) -> None:
    """A chain signed by a key not in trusted-keys produces unknown_key failure."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)

    # Replace the trusted-keys file with a fresh, unrelated key.
    other = SigningKey.from_private_key(Ed25519PrivateKey.generate())
    pem = other.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trusted.write_bytes(tomli_w.dumps({"keys": {other.key_id: pem}}).encode("utf-8"))

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is False
    assert "unknown_key" in result.chain_failures


def test_trusted_keys_missing_blocks(tmp_path: Path, key: SigningKey) -> None:
    """A missing trusted-keys file blocks with a trusted_keys_error reason."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)
    trusted.unlink()

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.checks["trusted_keys"] is False
    assert any("trusted_keys_error" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Release not in chain
# ---------------------------------------------------------------------------


def test_release_not_in_chain_blocks(tmp_path: Path, key: SigningKey) -> None:
    """A valid chain that does not contain the requested release blocks."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)
    # Write a slip and a signed bundle for a different release.
    slip_b = _slip_body("v9.9.9")
    (slip_dir / "v9.9.9.toml").write_bytes(tomli_w.dumps(slip_b).encode("utf-8"))
    manifest_sha = _write_signed_bundle(release_store / "v9.9.9", key, slip_b)

    # Overwrite registry with just the v9.9.9 entry.
    registry.write_text("", encoding="utf-8")
    _write_chain_entry(
        registry,
        key,
        release_id="v9.9.9",
        slip_sha=slip_b["content_sha256"],
        bundle_manifest_sha=manifest_sha,
    )

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is True  # chain intact
    assert result.checks["release_present"] is False
    assert any("not in the registry chain" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Bundle failure
# ---------------------------------------------------------------------------


def test_bundle_manifest_tampered_blocks(tmp_path: Path, key: SigningKey) -> None:
    """A bundle manifest that no longer matches the chain entry blocks."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)

    # Append a byte to the manifest; hash no longer matches entry.
    manifest_path = release_store / "v1.0.0" / "bundle-manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is True  # chain intact; bundle drift
    assert result.checks["bundle_manifest"] is False
    assert any("hash mismatch" in r for r in result.reasons)


def test_bundle_signature_tampered_blocks(tmp_path: Path, key: SigningKey) -> None:
    """A bundle manifest whose signature does not validate blocks.

    The manifest is re-signed with random signature bytes and the chain
    entry is rewritten to match the new manifest hash, so the failure
    comes from the signature check itself, not the hash check.
    """
    slip_dir, release_store, registry, trusted, slip_body = _setup_full_release(tmp_path, key)

    # Build a manifest whose signature bytes are random but hash-stable.
    manifest_path = release_store / "v1.0.0" / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    tampered_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    manifest_path.write_bytes(tampered_bytes)
    new_manifest_sha = hashlib.sha256(tampered_bytes).hexdigest()

    # Rewrite the chain to record the new manifest hash.
    registry.write_text("", encoding="utf-8")
    _write_chain_entry(
        registry,
        key,
        release_id="v1.0.0",
        slip_sha=slip_body["content_sha256"],
        bundle_manifest_sha=new_manifest_sha,
    )

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is True
    assert result.checks["bundle_manifest"] is False
    assert any("signature does not validate" in r for r in result.reasons)


def test_bundle_directory_missing_blocks(tmp_path: Path, key: SigningKey) -> None:
    """A valid chain with a present entry but no bundle directory blocks."""
    slip_dir, release_store, registry, trusted, _ = _setup_full_release(tmp_path, key)

    shutil.rmtree(release_store / "v1.0.0")

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry,
        trusted_keys_path=trusted,
    )

    assert result.status == "blocked"
    assert result.chain_ok is True
    assert result.checks["bundle_present"] is False
    assert any("bundle directory missing" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Backward compat: no chain args -> slice-11 local checks only
# ---------------------------------------------------------------------------


def test_local_only_mode_when_chain_args_absent(tmp_path: Path, key: SigningKey) -> None:
    """Omitting registry_path and trusted_keys_path keeps slice-11 semantics."""
    slip_dir, release_store, registry, _trusted, _ = _setup_full_release(tmp_path, key)

    # Break the chain; local-only mode should not notice.
    registry.write_text("garbage\n", encoding="utf-8")

    result = verify_release(
        release_id="v1.0.0",
        release_pointer=None,
        expected_commit_sha=None,
        expected_slip_sha256=None,
        slip_dir=slip_dir,
        release_store=release_store,
        # registry_path / trusted_keys_path omitted
    )

    # Local checks pass (bundle dir + artifacts exist); chain not run.
    assert result.status == "ok"
    assert result.chain_ok is True
    assert "chain" not in result.checks
    assert "bundle_exists" in result.checks
