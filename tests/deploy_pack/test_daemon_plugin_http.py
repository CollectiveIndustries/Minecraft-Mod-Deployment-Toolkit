# tests/deploy_pack/test_daemon_plugin_http.py

"""HTTP admin plugin endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w
from DaemonCore import DaemonContext, HealthReport
from DaemonCore.resources import FileJournal, FileStateStore
from fastapi.testclient import TestClient

from minecraft.deploy_pack.daemon.auth import BearerAuth
from minecraft.deploy_pack.daemon.idempotency import SQLiteIdempotency
from minecraft.deploy_pack.daemon.plugins.http import HttpAdminPlugin
from minecraft.modpack_upgrade import slip as slip_mod

_TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _slip_body(release_id: str) -> dict:
    """Build a valid slip body."""
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
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


def _write_slip(slip_dir: Path, release_id: str) -> None:
    """Write a valid slip file."""
    slip_dir.mkdir(parents=True, exist_ok=True)
    (slip_dir / f"{release_id}.toml").write_text(tomli_w.dumps(_slip_body(release_id)), encoding="utf-8")


def _write_bundle(release_store: Path, release_id: str) -> None:
    """Create a minimal release bundle."""
    (release_store / release_id / "artifacts").mkdir(parents=True)


async def _make_ctx(tmp_path: Path) -> DaemonContext:
    """Build a DaemonContext suitable for the plugin."""

    async def _health() -> HealthReport:
        return HealthReport(status="healthy")

    return DaemonContext(
        name="minecraft-deploy",
        version="1.3.0",
        config=None,  # type: ignore[arg-type]
        logger=_NullLogger(),  # type: ignore[arg-type]
        state=FileStateStore(tmp_path / "state"),
        audit=FileJournal(tmp_path / "audit.jsonl"),
        lock=None,
        secrets=None,
        health=_health,
    )


class _NullLogger:
    """Logger stub that discards everything."""

    def debug(self, msg: str, fields: dict | None = None) -> None:
        """Discard."""

    def info(self, msg: str, fields: dict | None = None) -> None:
        """Discard."""

    def warning(self, msg: str, fields: dict | None = None) -> None:
        """Discard."""

    def error(self, msg: str, fields: dict | None = None) -> None:
        """Discard."""


@pytest.fixture
async def plugin_client(tmp_path: Path) -> TestClient:
    """Build a plugin with real resources and return a TestClient."""
    slip_dir = tmp_path / "slips"
    release_store = tmp_path / "releases"
    _write_slip(slip_dir, "v1.0.2")
    _write_bundle(release_store, "v1.0.2")

    plugin = HttpAdminPlugin(
        host="127.0.0.1",
        port=0,
        auth=BearerAuth(token=_TOKEN),
        idempotency=SQLiteIdempotency(tmp_path / "idem.sqlite", ttl_hours=24),
        slip_dir=slip_dir,
        release_store=release_store,
    )
    ctx = await _make_ctx(tmp_path)
    app = plugin.build_app(ctx)
    return TestClient(app)


def _auth_headers(idempotency_key: str | None = "idem-1") -> dict[str, str]:
    """Return standard headers for a POST."""
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _get_headers() -> dict[str, str]:
    """Return auth headers for a GET."""
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


def test_status_fresh(plugin_client: TestClient):
    """A fresh daemon returns version and empty state."""
    resp = plugin_client.get("/status", headers=_get_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["daemon_version"] == "1.3.0"
    assert body["daemon_name"] == "minecraft-deploy"
    assert body["active_release"] is None
    assert body["last_registry_head"] is None
    assert body["last_verification"] is None
    assert body["health"]["status"] == "healthy"


def test_status_requires_auth(plugin_client: TestClient):
    """GET /status without auth returns 401."""
    resp = plugin_client.get("/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == 28


# ---------------------------------------------------------------------------
# POST /verify
# ---------------------------------------------------------------------------


def test_verify_ok(plugin_client: TestClient):
    """A valid release returns status=ok."""
    resp = plugin_client.post(
        "/verify",
        headers=_auth_headers(),
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["release_id"] == "v1.0.2"
    assert body["checks"]["slip_sha256"] is True


def test_verify_no_auth(plugin_client: TestClient):
    """POST /verify without auth returns 401."""
    resp = plugin_client.post(
        "/verify",
        headers={"Idempotency-Key": "k1"},
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 401


def test_verify_missing_idempotency_key(plugin_client: TestClient):
    """POST /verify without Idempotency-Key returns 422."""
    resp = plugin_client.post(
        "/verify",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 422


def test_verify_bad_body(plugin_client: TestClient):
    """POST /verify with neither release_id nor pointer returns 422."""
    resp = plugin_client.post(
        "/verify",
        headers=_auth_headers(),
        json={},
    )
    assert resp.status_code == 422


def test_verify_idempotent_same_body(plugin_client: TestClient):
    """Two POSTs with same key and body return identical responses."""
    body = {"release_id": "v1.0.2"}
    headers = _auth_headers(idempotency_key="idem-same")
    first = plugin_client.post("/verify", headers=headers, json=body)
    second = plugin_client.post("/verify", headers=headers, json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


def test_verify_idempotency_conflict(plugin_client: TestClient):
    """Same key with different body returns 409."""
    headers = _auth_headers(idempotency_key="idem-conflict")
    plugin_client.post("/verify", headers=headers, json={"release_id": "v1.0.2"})
    resp = plugin_client.post(
        "/verify",
        headers=headers,
        json={"release_id": "v1.0.2", "expected_slip_sha256": _hex("a")},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 27


# ---------------------------------------------------------------------------
# POST /publish
# ---------------------------------------------------------------------------


def test_publish_ok(plugin_client: TestClient):
    """POST /publish returns a receipt."""
    resp = plugin_client.post(
        "/publish",
        headers=_auth_headers(),
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["receipt_id"] is not None
    assert body["recorded_at_utc"] is not None


def test_publish_requires_auth(plugin_client: TestClient):
    """POST /publish without auth returns 401."""
    resp = plugin_client.post(
        "/publish",
        headers={"Idempotency-Key": "k1"},
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /deploy
# ---------------------------------------------------------------------------


def test_deploy_returns_501(plugin_client: TestClient):
    """POST /deploy returns 501 Not Implemented."""
    resp = plugin_client.post(
        "/deploy",
        headers=_auth_headers(),
        json={"release_id": "v1.0.2"},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body["code"] == 30


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------


async def test_plugin_health_before_start():
    """health() reports unhealthy before start()."""
    plugin = HttpAdminPlugin(
        host="127.0.0.1",
        port=18080,
        auth=BearerAuth(token=_TOKEN),
        idempotency=SQLiteIdempotency(Path("/tmp/never.sqlite"), ttl_hours=24),
        slip_dir=Path("/tmp"),
        release_store=Path("/tmp"),
    )
    report = await plugin.health()
    assert report.status == "unhealthy"


async def test_plugin_start_and_stop(tmp_path: Path):
    """The plugin starts a real uvicorn server and stops cleanly."""
    plugin = HttpAdminPlugin(
        host="127.0.0.1",
        port=18080,
        auth=BearerAuth(token=_TOKEN),
        idempotency=SQLiteIdempotency(tmp_path / "idem.sqlite", ttl_hours=24),
        slip_dir=tmp_path / "slips",
        release_store=tmp_path / "releases",
    )
    ctx = await _make_ctx(tmp_path)
    await plugin.start(ctx)
    try:
        report = await plugin.health()
        assert report.status == "healthy"
    finally:
        await plugin.stop()
    report = await plugin.health()
    assert report.status == "unhealthy"
