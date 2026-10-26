# tests/discord_bot/test_notify_endpoint.py

"""Tests for the ``POST /notify`` bot-daemon endpoint (Slice 15 T3c)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from minecraft.discord_bot.cli_invoker import FakeCliInvoker
from minecraft.discord_bot.client import FakeBotClient
from minecraft.discord_bot.config import DiscordBotConfig
from minecraft.discord_bot.http_endpoint import DiscordBotPlugin
from minecraft.discord_bot.interactions import ChannelRef

_NOTIFY_TOKEN = "test-notify-token"


def _bot_config() -> DiscordBotConfig:
    """Return a DiscordBotConfig with a valid channel_id for notify delivery."""
    return DiscordBotConfig(
        bot_token="tok",
        application_id="111111111111111111",
        public_key="a" * 64,
        guild_id="222222222222222222",
        channel_id="333333333333333333",
        allowlist_roles=(),
        allowlist_users=(),
    )


def _plugin(*, notify_bearer_token: str | None = _NOTIFY_TOKEN) -> tuple[DiscordBotPlugin, FakeBotClient]:
    """Return a plugin and its recorded bot client."""
    bot_client = FakeBotClient()
    plugin = DiscordBotPlugin(
        host="127.0.0.1",
        port=8766,
        bot_config=_bot_config(),
        invoker=FakeCliInvoker(),
        bot_client=bot_client,
        notify_bearer_token=notify_bearer_token,
    )
    return (plugin, bot_client)


def _client(plugin: DiscordBotPlugin) -> TestClient:
    """Return a TestClient bound to the plugin's app."""
    return TestClient(plugin.build_app())


def _auth_headers(token: str = _NOTIFY_TOKEN) -> dict[str, str]:
    """Return the Authorization header for /notify."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_notify_route_not_registered_when_token_is_none() -> None:
    """Without a notify token, /notify is not registered (404)."""
    plugin, _ = _plugin(notify_bearer_token=None)
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi", "kind": "live"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_notify_route_registered_when_token_is_set() -> None:
    """With a notify token, /notify is registered and reachable."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi", "kind": "live"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_notify_without_auth_header_returns_401() -> None:
    """Missing Authorization header yields 401."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post("/notify", json={"content": "hi", "kind": "live"})
    assert resp.status_code == 401
    assert "bearer" in resp.json()["error"].lower()


def test_notify_with_wrong_scheme_returns_401() -> None:
    """A non-Bearer scheme yields 401."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi", "kind": "live"},
        headers={"Authorization": f"Basic {_NOTIFY_TOKEN}"},
    )
    assert resp.status_code == 401


def test_notify_with_wrong_token_returns_401() -> None:
    """A wrong bearer token yields 401."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi", "kind": "live"},
        headers=_auth_headers(token="wrong-token"),
    )
    assert resp.status_code == 401
    assert "invalid" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_notify_malformed_json_returns_400() -> None:
    """A malformed JSON body yields 400."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        content=b"not json",
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_notify_non_object_body_returns_400() -> None:
    """A JSON array (not object) yields 400."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        content=json.dumps([1, 2, 3]).encode(),
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_notify_missing_content_returns_400() -> None:
    """A body without content yields 400."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"kind": "live"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "content" in resp.json()["error"].lower()


def test_notify_empty_content_returns_400() -> None:
    """An empty-string content yields 400."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "", "kind": "live"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_notify_missing_kind_returns_400() -> None:
    """A body without kind yields 400."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "kind" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_notify_delivers_to_operator_channel() -> None:
    """A successful notify sends OutboundMessage via the bot client to the operator channel."""
    plugin, bot_client = _plugin()
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "deployment starting", "kind": "live"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(bot_client.sent) == 1
    entry = bot_client.sent[0]
    assert isinstance(entry.target, ChannelRef)
    assert entry.target.channel_id == "333333333333333333"
    assert entry.message.content == "deployment starting"


def test_notify_delivery_failure_returns_502() -> None:
    """A delivery exception surfaces as 502 so the client can report failure."""
    plugin, bot_client = _plugin()
    bot_client.fail_next(RuntimeError("discord unreachable"))
    client = _client(plugin)
    resp = client.post(
        "/notify",
        json={"content": "hi", "kind": "failure"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 502
    assert "delivery failed" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Interaction route still works alongside /notify
# ---------------------------------------------------------------------------


def test_interactions_route_remains_registered_with_notify_token() -> None:
    """Adding /notify does not remove /interactions."""
    plugin, _ = _plugin()
    client = _client(plugin)
    # GET on a POST-only route yields 405, not 404 -- proves the route exists.
    resp = client.get("/interactions")
    assert resp.status_code in (405, 422)


def test_notify_route_does_not_break_interactions_route_missing_sig() -> None:
    """A POST to /interactions without signature still returns 401 (route intact)."""
    plugin, _ = _plugin()
    client = _client(plugin)
    resp = client.post("/interactions", content=b'{"type": 1}')
    assert resp.status_code == 401
