# tests/deploy_pack/test_deploy_gate.py

"""``/deploy`` verification gate (Slice 12).

Acceptance coverage:
  * T10 -- chain tampering produces 409 / code 26
  * T22 -- bundle tampering produces 409 / code 20
  * happy path produces 501 / code 30 (verification passed, orchestration deferred)
  * auth required produces 401
  * idempotency header required produces 422

The HTTP layer is exercised through FastAPI's ``TestClient`` against the
same plugin the daemon runs. The chain and bundle fixtures mirror
``test_verify_chain.py`` so the wire-format and error-code shape are
pinned by a real signed chain.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from minecraft.deploy_pack.daemon.auth import BearerAuth
from minecraft.deploy_pack.daemon.idempotency import SQLiteIdempotency
from minecraft.deploy_pack.daemon.plugins.http import HttpAdminPlugin
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.registry_verify import (
    GENESIS_HASH,
    compute_entry_hash,
)
from minecraft.modpack_upgrade.signing import SigningKey, sign_json

# ---------------------------------------------------------------------------
# Fake DaemonContext (only fields http.py touches)
# ---------------------------------------------------------------------------


@dataclass
class _FakeHealth:
    """Minimal HealthReport stand-in for the fake context."""

    status: str = "healthy"
    checks: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class _FakeContext:
    """Minimal DaemonContext stand-in covering only what the plugin reads."""

    name: str = "minecraft-deploy"
    version: str = "1.3.0"
    state: Any = None
    audit: Any = None

    async def health(self) -> _FakeHealth:
        """Return a healthy fake health report."""
        return _FakeHealth()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> SigningKey:
    """Return a fresh Ed25519 signing key."""
    return SigningKey.from_private_key(Ed25519PrivateKey.generate())


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


def _setup(
    tmp_path: Path,
    key: SigningKey,
    *,
    release_id: str = "v1.0.0",
) -> tuple[HttpAdminPlugin, str]:
    """Return ``(plugin, bearer_token)`` with a fully wired valid release."""
    slip_dir = tmp_path / "slips"
    slip_dir.mkdir()
    slip_body = _slip_body(release_id)
    (slip_dir / f"{release_id}.toml").write_bytes(tomli_w.dumps(slip_body).encode("utf-8"))

    release_store = tmp_path / "releases"
    bundle_dir = release_store / release_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "artifacts").mkdir()

    projection = {
        "schema_version": 1,
        "release_id": release_id,
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
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    chain_fields = {
        "schema_version": 1,
        "release_id": release_id,
        "tag_object_sha": "a" * 40,
        "tag_object_format": "sha1",
        "commit_sha": "b" * 40,
        "slip_content_sha256": slip_body["content_sha256"],
        "bundle_manifest_sha256": manifest_sha,
        "previous_entry_hash": GENESIS_HASH,
    }
    entry_hash = compute_entry_hash(chain_fields)
    entry_sig = sign_json(key, chain_fields)
    entry = {
        **chain_fields,
        "entry_hash": entry_hash,
        "signature": base64.b64encode(entry_sig).decode("ascii"),
        "key_id": key.key_id,
    }
    registry_path = tmp_path / "release-index.jsonl"
    registry_path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

    pem = key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trusted_keys_path = tmp_path / "trusted_keys.toml"
    trusted_keys_path.write_bytes(tomli_w.dumps({"keys": {key.key_id: pem}}).encode("utf-8"))

    token = "test-bearer"
    plugin = HttpAdminPlugin(
        host="127.0.0.1",
        port=0,
        auth=BearerAuth(token=token),
        idempotency=SQLiteIdempotency(tmp_path / "idem.sqlite"),
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry_path,
        trusted_keys_path=trusted_keys_path,
    )
    return plugin, token


def _client(plugin: HttpAdminPlugin) -> TestClient:
    """Return a FastAPI TestClient bound to the plugin's app."""
    return TestClient(plugin.build_app(_FakeContext()))


def _headers(token: str) -> dict[str, str]:
    """Return Authorization and Idempotency-Key headers for a POST."""
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "test-key",
    }


# ---------------------------------------------------------------------------
# Happy path: verification passes -> 501 NotImplementedError
# ---------------------------------------------------------------------------


def test_deploy_verified_release_returns_501(tmp_path: Path, key: SigningKey) -> None:
    """A verified release returns 501 (orchestration deferred)."""
    plugin, token = _setup(tmp_path, key)
    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body["code"] == 30  # NotImplementedError
    assert "verified release: 'v1.0.0'" in (body["detail"] or "")


# ---------------------------------------------------------------------------
# Chain tampering -> 409 / code 26 (T10)
# ---------------------------------------------------------------------------


def test_deploy_broken_chain_returns_409_code_26(tmp_path: Path, key: SigningKey) -> None:
    """A malformed chain file returns 409 with code 26."""
    plugin, token = _setup(tmp_path, key)
    # Tamper with the chain file.
    registry = tmp_path / "release-index.jsonl"
    registry.write_text("not json\n", encoding="utf-8")

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 26
    assert "registry chain verification failed" in body["message"]


def test_deploy_chain_entry_hash_mismatch_returns_409_code_26(tmp_path: Path, key: SigningKey) -> None:
    """A tampered entry hash returns 409 with code 26."""
    plugin, token = _setup(tmp_path, key)
    registry = tmp_path / "release-index.jsonl"
    entry = json.loads(registry.read_text().splitlines()[0])
    entry["commit_sha"] = "0" * 40
    registry.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 26


def test_deploy_unknown_key_returns_409_code_26(tmp_path: Path, key: SigningKey) -> None:
    """A chain signed by an unpinned key returns 409 with code 26."""
    plugin, token = _setup(tmp_path, key)
    # Replace trusted-keys with an unrelated key.
    other = SigningKey.from_private_key(Ed25519PrivateKey.generate())
    pem = other.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    (tmp_path / "trusted_keys.toml").write_bytes(tomli_w.dumps({"keys": {other.key_id: pem}}).encode("utf-8"))

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 26


# ---------------------------------------------------------------------------
# Bundle tampering -> 409 / code 20 (T22)
# ---------------------------------------------------------------------------


def test_deploy_tampered_bundle_returns_409_code_20(tmp_path: Path, key: SigningKey) -> None:
    """A tampered bundle manifest returns 409 with code 20."""
    plugin, token = _setup(tmp_path, key)
    manifest_path = tmp_path / "releases" / "v1.0.0" / "bundle-manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 20  # VerificationBlockedError
    assert "failed verification" in body["message"]


def test_deploy_missing_bundle_returns_409_code_20(tmp_path: Path, key: SigningKey) -> None:
    """A missing bundle directory returns 409 with code 20."""
    plugin, token = _setup(tmp_path, key)

    shutil.rmtree(tmp_path / "releases" / "v1.0.0")

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 20


def test_deploy_release_not_in_chain_returns_409_code_20(tmp_path: Path, key: SigningKey) -> None:
    """A valid chain missing the requested release returns 409 with code 20."""
    plugin, token = _setup(tmp_path, key)
    # Add a slip file for a different release but do NOT add a chain entry.
    other = _slip_body("v2.0.0")
    (tmp_path / "slips" / "v2.0.0.toml").write_bytes(tomli_w.dumps(other).encode("utf-8"))

    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v2.0.0"},
        headers=_headers(token),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 20


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


def test_deploy_requires_auth(tmp_path: Path, key: SigningKey) -> None:
    """A request without an Authorization header returns 401 code 28."""
    plugin, _ = _setup(tmp_path, key)
    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers={"Idempotency-Key": "x"},  # no Authorization
    )
    # Missing Authorization -> 401 (AuthFailedError, code 28)
    assert resp.status_code == 401
    assert resp.json()["code"] == 28


def test_deploy_requires_idempotency_header(tmp_path: Path, key: SigningKey) -> None:
    """A request without an Idempotency-Key header returns 422."""
    plugin, token = _setup(tmp_path, key)
    resp = _client(plugin).post(
        "/deploy",
        json={"release_id": "v1.0.0"},
        headers={"Authorization": f"Bearer {token}"},  # no Idempotency-Key
    )
    # FastAPI's Header(..., alias=...) -> 422 when absent.
    assert resp.status_code == 422


def test_deploy_missing_release_id_returns_422(tmp_path: Path, key: SigningKey) -> None:
    """A request body with neither release_id nor pointer returns 422."""
    plugin, token = _setup(tmp_path, key)
    resp = _client(plugin).post(
        "/deploy",
        json={},  # no release_id or release_pointer
        headers=_headers(token),
    )
    # Pydantic model_validator rejects with 422.
    assert resp.status_code == 422
