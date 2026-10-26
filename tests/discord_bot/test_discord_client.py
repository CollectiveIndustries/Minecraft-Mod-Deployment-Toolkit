# tests/discord_bot/test_discord_client.py

"""Discord REST client tests (T3b)."""

from __future__ import annotations

import httpx
import pytest

from minecraft.discord_bot.discord_client import DiscordRestClient
from minecraft.discord_bot.errors import DiscordBotDeliveryError
from minecraft.discord_bot.interactions import (
    ChannelRef,
    InteractionRef,
    OutboundMessage,
)


def _interaction_ref(app_id: str = "111111111111111111", token: str = "interaction-token-abc") -> InteractionRef:
    """Return a minimal InteractionRef for tests."""
    return InteractionRef(
        interaction_id="999999999999999999",
        application_id=app_id,
        token=token,
    )


def _mock_transport(handler) -> httpx.MockTransport:
    """Return an async :class:`httpx.MockTransport` wrapping ``handler``."""
    return httpx.MockTransport(handler)


def _async_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` bound to ``transport``."""
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# Interaction followup
# ---------------------------------------------------------------------------


async def test_interaction_followup_posts_to_webhook_url() -> None:
    """Interaction target POSTs to /webhooks/{application_id}/{token}."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"id": "msg-1"})

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    await rest.send(_interaction_ref(app_id="111", token="tok-abc"), OutboundMessage(content="hello"))
    await client.aclose()

    assert len(recorded) == 1
    assert recorded[0].method == "POST"
    assert str(recorded[0].url) == "https://discord.com/api/v10/webhooks/111/tok-abc"
    assert b'"content"' in recorded[0].content


async def test_interaction_followup_omits_auth_header() -> None:
    """The interaction token is the credential; no Authorization header is sent."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={})

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="bot-token", http_client=client)
    await rest.send(_interaction_ref(), OutboundMessage(content="x"))
    await client.aclose()

    assert "authorization" not in recorded[0].headers


# ---------------------------------------------------------------------------
# Channel posts
# ---------------------------------------------------------------------------


async def test_channel_post_uses_channel_messages_url() -> None:
    """Channel target POSTs to /channels/{channel_id}/messages."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"id": "msg-2"})

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    await rest.send(ChannelRef(channel_id="222222222222222222"), OutboundMessage(content="hi"))
    await client.aclose()

    assert str(recorded[0].url) == "https://discord.com/api/v10/channels/222222222222222222/messages"


async def test_channel_post_includes_bot_auth_header() -> None:
    """Channel posts include Authorization: Bot <token>."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={})

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="my-bot-token", http_client=client)
    await rest.send(ChannelRef(channel_id="333333333333333333"), OutboundMessage(content="x"))
    await client.aclose()

    assert recorded[0].headers["authorization"] == "Bot my-bot-token"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_interaction_followup_4xx_raises_delivery_error() -> None:
    """A 4xx response raises DiscordBotDeliveryError with status and body preview."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="unknown webhook")

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    with pytest.raises(DiscordBotDeliveryError, match="HTTP 404"):
        await rest.send(_interaction_ref(), OutboundMessage(content="x"))
    await client.aclose()


async def test_channel_post_5xx_raises_delivery_error() -> None:
    """A 5xx response raises DiscordBotDeliveryError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    with pytest.raises(DiscordBotDeliveryError, match="HTTP 500"):
        await rest.send(ChannelRef(channel_id="444444444444444444"), OutboundMessage(content="x"))
    await client.aclose()


async def test_network_error_raises_delivery_error() -> None:
    """A connection error raises DiscordBotDeliveryError with the exception chained."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    with pytest.raises(DiscordBotDeliveryError, match="ConnectError"):
        await rest.send(ChannelRef(channel_id="555555555555555555"), OutboundMessage(content="x"))
    await client.aclose()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_aclose_when_client_supplied_is_noop() -> None:
    """When the caller supplies the client, aclose does not shut it down."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _async_client(_mock_transport(handler))
    rest = DiscordRestClient(bot_token="tok", http_client=client)
    await rest.aclose()
    # The client is still usable; caller owns it.
    await rest.send(ChannelRef(channel_id="666666666666666666"), OutboundMessage(content="ok"))
    await client.aclose()


async def test_aclose_when_no_client_supplied_is_noop() -> None:
    """When no client has been lazily created, aclose does not raise."""
    rest = DiscordRestClient(bot_token="tok")
    await rest.aclose()
