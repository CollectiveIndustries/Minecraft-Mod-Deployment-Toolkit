# tests/discord_bot/test_http_endpoint.py

"""Signed-HTTP interaction endpoint tests (T3b)."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from minecraft.discord_bot.cli_invoker import FakeCliInvoker
from minecraft.discord_bot.client import FakeBotClient
from minecraft.discord_bot.config import DiscordBotConfig
from minecraft.discord_bot.http_endpoint import (
    APPLICATION_COMMAND_TYPE,
    PING_TYPE,
    DiscordBotPlugin,
    normalize_interaction_context,
)
from minecraft.discord_bot.interactions import InteractionContext


def _keypair() -> tuple[str, Ed25519PrivateKey]:
    """Return ``(public_key_hex, private_key)`` for a fresh Ed25519 key."""
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (public_bytes.hex(), private)


def _bot_config(public_hex: str) -> DiscordBotConfig:
    """Return a DiscordBotConfig with the given public key."""
    return DiscordBotConfig(
        bot_token="tok",
        application_id="111111111111111111",
        public_key=public_hex,
        guild_id="222222222222222222",
        channel_id="333333333333333333",
        allowlist_roles=(),
        allowlist_users=("444444444444444444",),
    )


def _signed_headers(private: Ed25519PrivateKey, body: bytes, timestamp: str = "1700000000") -> dict[str, str]:
    """Return headers with a valid signature for ``body``."""
    signature = private.sign(timestamp.encode("utf-8") + body).hex()
    return {
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
        "Content-Type": "application/json",
    }


class _RecordingScheduler:
    """Records every dispatched InteractionContext."""

    def __init__(self) -> None:
        self.calls: list[InteractionContext] = []

    def __call__(self, ctx: InteractionContext) -> None:
        self.calls.append(ctx)


def _plugin(
    *,
    public_hex: str,
    scheduler: _RecordingScheduler | None = None,
    port: int = 8766,
) -> DiscordBotPlugin:
    """Return a plugin wired to fakes with an optional scheduler override.

    ``port`` defaults to the production 8766 for the route tests that
    never bind. Health tests pass ``port=0`` to bind an ephemeral port;
    this guarantees no test can collide with another, and lets the
    leaked-socket class of failure (a test that forgets to stop) surface
    as a harmless port reassignment rather than a bind error in an
    unrelated test.
    """
    return DiscordBotPlugin(
        host="127.0.0.1",
        port=port,
        bot_config=_bot_config(public_hex),
        invoker=FakeCliInvoker(),
        bot_client=FakeBotClient(),
        dispatch_scheduler=scheduler or _RecordingScheduler(),
    )


def _client(plugin: DiscordBotPlugin) -> TestClient:
    """Return a TestClient bound to the plugin's app."""
    return TestClient(plugin.build_app())


# ---------------------------------------------------------------------------
# Signature failures
# ---------------------------------------------------------------------------


def test_missing_signature_headers_returns_401() -> None:
    """A request without signature headers returns HTTP 401."""
    public_hex, _private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    resp = client.post("/interactions", content=b'{"type": 1}')
    assert resp.status_code == 401


def test_invalid_signature_returns_401() -> None:
    """A request with an invalid signature returns HTTP 401."""
    public_hex, _private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    body = b'{"type": 1}'
    resp = client.post(
        "/interactions",
        content=body,
        headers={"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "1700000000"},
    )
    assert resp.status_code == 401


def test_tampered_body_returns_401() -> None:
    """A body modified after signing returns HTTP 401."""
    public_hex, private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    original = b'{"type": 1}'
    headers = _signed_headers(private, original)
    resp = client.post("/interactions", content=b'{"type": 2}', headers=headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PING
# ---------------------------------------------------------------------------


def test_ping_returns_pong() -> None:
    """A signed PING returns type 1 (PONG)."""
    public_hex, private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    body = json.dumps({"type": PING_TYPE}).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


# ---------------------------------------------------------------------------
# Application command
# ---------------------------------------------------------------------------


def _application_command_body(name: str = "upgrade", options: list[dict] | None = None) -> bytes:
    """Return a signed-ready APPLICATION_COMMAND payload."""
    payload = {
        "type": APPLICATION_COMMAND_TYPE,
        "id": "555555555555555555",
        "application_id": "111111111111111111",
        "token": "interaction-token",
        "guild_id": "222222222222222222",
        "channel_id": "333333333333333333",
        "member": {
            "user": {"id": "444444444444444444"},
            "roles": [],
        },
        "data": {
            "name": name,
            "options": options or [],
        },
    }
    return json.dumps(payload).encode()


def test_application_command_returns_deferred() -> None:
    """A signed APPLICATION_COMMAND returns type 5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE)."""
    public_hex, private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    body = _application_command_body()
    resp = client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert resp.status_code == 200
    assert resp.json() == {"type": 5}


def test_application_command_schedules_dispatch() -> None:
    """The scheduler receives a normalized InteractionContext."""
    public_hex, private = _keypair()
    scheduler = _RecordingScheduler()
    client = _client(_plugin(public_hex=public_hex, scheduler=scheduler))
    body = _application_command_body()
    client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert len(scheduler.calls) == 1
    ctx = scheduler.calls[0]
    assert ctx.command_name == "upgrade"
    assert ctx.user_id == "444444444444444444"


def test_application_command_with_subcommand() -> None:
    """Subcommand is flattened into options["subcommand"]."""
    public_hex, private = _keypair()
    scheduler = _RecordingScheduler()
    client = _client(_plugin(public_hex=public_hex, scheduler=scheduler))
    body = _application_command_body(options=[{"name": "approve", "type": 1, "options": [{"name": "run_id", "type": 3, "value": "run-42"}]}])
    client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert scheduler.calls[0].options == {"subcommand": "approve", "run_id": "run-42"}


# ---------------------------------------------------------------------------
# Malformed requests
# ---------------------------------------------------------------------------


def test_malformed_json_returns_400() -> None:
    """A signed but non-JSON body returns HTTP 400."""
    public_hex, private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    body = b"not json"
    resp = client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert resp.status_code == 400


def test_unknown_interaction_type_returns_400() -> None:
    """An unsupported interaction type returns HTTP 400."""
    public_hex, private = _keypair()
    client = _client(_plugin(public_hex=public_hex))
    body = json.dumps({"type": 99}).encode()
    resp = client.post("/interactions", content=body, headers=_signed_headers(private, body))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
#
# These three tests exercise :meth:`DiscordBotPlugin.start` and
# :meth:`DiscordBotPlugin.stop`, which bind and release a real TCP
# socket. ``port=0`` requests an ephemeral port from the OS, so no test
# can collide with any other -- and a test that leaks a socket (say, by
# failing before its ``finally`` runs) cannot poison an unrelated test
# later in the file. Every test that calls :meth:`start` is wrapped in
# ``try/finally`` so :meth:`stop` always runs even when an assertion
# fails.
# ---------------------------------------------------------------------------


async def test_health_unhealthy_before_start() -> None:
    """Health is unhealthy before start()."""
    public_hex, _private = _keypair()
    plugin = _plugin(public_hex=public_hex, port=0)
    report = await plugin.health()
    assert report.status == "unhealthy"


async def test_health_healthy_after_start() -> None:
    """Health is healthy after start()."""
    public_hex, _private = _keypair()
    plugin = _plugin(public_hex=public_hex, port=0)
    await plugin.start()
    try:
        report = await plugin.health()
        assert report.status == "healthy"
    finally:
        await plugin.stop()


async def test_health_unhealthy_after_stop() -> None:
    """Health returns to unhealthy after stop()."""
    public_hex, _private = _keypair()
    plugin = _plugin(public_hex=public_hex, port=0)
    await plugin.start()
    await plugin.stop()
    report = await plugin.health()
    assert report.status == "unhealthy"


# ---------------------------------------------------------------------------
# normalize_interaction_context
# ---------------------------------------------------------------------------


def _raw_payload() -> dict:
    """Return a raw guild APPLICATION_COMMAND payload."""
    return {
        "type": 2,
        "id": "555555555555555555",
        "application_id": "111111111111111111",
        "token": "interaction-token",
        "guild_id": "222222222222222222",
        "channel_id": "333333333333333333",
        "member": {
            "user": {"id": "444444444444444444"},
            "roles": ["666666666666666666", "777777777777777777"],
        },
        "data": {"name": "upgrade", "options": [{"name": "status", "type": 1}]},
    }


def test_normalize_guild_interaction() -> None:
    """A guild payload normalizes user and roles from member."""
    ctx = normalize_interaction_context(_raw_payload())
    assert ctx.user_id == "444444444444444444"
    assert ctx.user_roles == ("666666666666666666", "777777777777777777")
    assert ctx.options["subcommand"] == "status"


def test_normalize_dm_interaction_has_no_roles() -> None:
    """A DM payload (user field, no member) normalizes to empty roles."""
    payload = _raw_payload()
    del payload["member"]
    payload["user"] = {"id": "444444444444444444"}
    ctx = normalize_interaction_context(payload)
    assert ctx.user_roles == ()


def test_normalize_missing_token_raises() -> None:
    """A payload without token raises ValueError."""
    payload = _raw_payload()
    del payload["token"]
    with pytest.raises(ValueError, match="token"):
        normalize_interaction_context(payload)


def test_normalize_missing_data_raises() -> None:
    """A payload without data raises ValueError."""
    payload = _raw_payload()
    del payload["data"]
    with pytest.raises(ValueError, match="data"):
        normalize_interaction_context(payload)


def test_normalize_top_level_options_no_subcommand() -> None:
    """Direct options (no subcommand wrapper) contribute under their own keys."""
    payload = _raw_payload()
    payload["data"] = {"name": "upgrade", "options": [{"name": "run_id", "type": 3, "value": "run-99"}]}
    ctx = normalize_interaction_context(payload)
    assert ctx.options == {"run_id": "run-99"}
