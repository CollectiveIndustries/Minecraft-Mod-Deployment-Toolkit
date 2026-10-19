# tests/deploy_pack/test_daemon_deploy_endpoint.py

"""End-to-end tests for the ``/deploy`` endpoint (Slice 13 Turn 4).

Verifies that :class:`HttpAdminPlugin` dispatches verified releases to
the deploy callable, translates outcomes to typed errors, and preserves
backward-compatible behavior (501) when no callable is wired.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from minecraft.deploy_pack.daemon.auth import BearerAuth
from minecraft.deploy_pack.daemon.deploy import DeployOutcome
from minecraft.deploy_pack.daemon.idempotency import SQLiteIdempotency
from minecraft.deploy_pack.daemon.plugins.http import HttpAdminPlugin
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.registry_verify import (
    GENESIS_HASH,
    compute_entry_hash,
)
from minecraft.modpack_upgrade.signing import SigningKey, sign_json


@dataclass
class _FakeHealth:
    """Minimal HealthReport stand-in."""

    status: str = "healthy"
    checks: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class _FakeContext:
    """Minimal DaemonContext stand-in."""

    name: str = "minecraft-deploy"
    version: str = "1.3.0"
    state: Any = None
    audit: Any = None

    async def health(self) -> _FakeHealth:
        """Return a healthy fake."""
        return _FakeHealth()


@pytest.fixture
def key() -> SigningKey:
    """Return a fresh Ed25519 signing key."""
    return SigningKey.from_private_key(Ed25519PrivateKey.generate())


def _slip_body(release_id: str) -> dict:
    """Build a minimal valid slip body."""
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
    deploy_callable,
    release_id: str = "v1.0.0",
) -> tuple[TestClient, str, list[str]]:
    """Build a plugin wired for ``/deploy`` and return ``(client, token, called)``."""
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
    called: list[str] = []

    if deploy_callable is not None:

        def _wrapper(rid: str) -> DeployOutcome:
            called.append(rid)
            return deploy_callable(rid)

        effective_callable = _wrapper
    else:
        effective_callable = None

    plugin = HttpAdminPlugin(
        host="127.0.0.1",
        port=0,
        auth=BearerAuth(token=token),
        idempotency=SQLiteIdempotency(tmp_path / "idem.sqlite"),
        slip_dir=slip_dir,
        release_store=release_store,
        registry_path=registry_path,
        trusted_keys_path=trusted_keys_path,
        deploy_callable=effective_callable,
    )
    client = TestClient(plugin.build_app(_FakeContext()))
    return (client, token, called)


def _headers(token: str) -> dict[str, str]:
    """Return Authorization and Idempotency-Key headers."""
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": "test-key"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_deploy_endpoint_invokes_callable(tmp_path: Path, key: SigningKey) -> None:
    """A verified release calls the deploy callable and returns 200."""
    client, token, called = _setup(
        tmp_path,
        key,
        deploy_callable=lambda rid: DeployOutcome(success=True, release_id=rid),
    )
    resp = client.post("/deploy", json={"release_id": "v1.0.0"}, headers=_headers(token))
    assert resp.status_code == 200
    assert called == ["v1.0.0"]
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["release_id"] == "v1.0.0"
    assert body["completed_at_utc"].endswith("Z")


# ---------------------------------------------------------------------------
# Failure translation
# ---------------------------------------------------------------------------


def test_deploy_endpoint_translates_post_deploy_failure(tmp_path: Path, key: SigningKey) -> None:
    """A health-failure outcome yields 500 / code 23."""
    client, token, _ = _setup(
        tmp_path,
        key,
        deploy_callable=lambda rid: DeployOutcome(
            success=False,
            release_id=rid,
            stage="health",
            message="boom",
            error_code=23,
        ),
    )
    resp = client.post("/deploy", json={"release_id": "v1.0.0"}, headers=_headers(token))
    assert resp.status_code == 500
    assert resp.json()["code"] == 23
    assert "boom" in resp.json()["message"]


def test_deploy_endpoint_translates_lock_held(tmp_path: Path, key: SigningKey) -> None:
    """A lock-held outcome yields 409 / code 25."""
    client, token, _ = _setup(
        tmp_path,
        key,
        deploy_callable=lambda rid: DeployOutcome(
            success=False,
            release_id=rid,
            stage="lock",
            message="held by other-run",
            error_code=25,
        ),
    )
    resp = client.post("/deploy", json={"release_id": "v1.0.0"}, headers=_headers(token))
    assert resp.status_code == 409
    assert resp.json()["code"] == 25


def test_deploy_endpoint_translates_recovery_failure(tmp_path: Path, key: SigningKey) -> None:
    """A recovery-failure outcome yields 500 / code 24."""
    client, token, _ = _setup(
        tmp_path,
        key,
        deploy_callable=lambda rid: DeployOutcome(
            success=False,
            release_id=rid,
            stage="recovery",
            message="could not restart",
            error_code=24,
        ),
    )
    resp = client.post("/deploy", json={"release_id": "v1.0.0"}, headers=_headers(token))
    assert resp.status_code == 500
    assert resp.json()["code"] == 24


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_deploy_endpoint_still_returns_501_without_callable(tmp_path: Path, key: SigningKey) -> None:
    """A verified release with no deploy callable still returns 501."""
    client, token, called = _setup(tmp_path, key, deploy_callable=None)
    resp = client.post("/deploy", json={"release_id": "v1.0.0"}, headers=_headers(token))
    assert resp.status_code == 501
    assert called == []
