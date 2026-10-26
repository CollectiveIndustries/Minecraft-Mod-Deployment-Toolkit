# src/minecraft/discord_bot/discord_client.py

"""Concrete Discord REST delivery client (Slice 15 T3b).

:class:`DiscordRestClient` implements the :class:`BotClient` protocol
against Discord's REST API using :mod:`httpx`. It handles two delivery
shapes:

- **Interaction followup**: ``POST /webhooks/{application_id}/{token}``.
  No ``Authorization`` header: the interaction token *is* the
  credential.
- **Channel post**: ``POST /channels/{channel_id}/messages`` with
  ``Authorization: Bot {bot_token}``.

Both go through ``BotClient.send``, dispatched on the target type. The
unified ``send`` shape means handlers do not know (or care) which
delivery path is taken; embeds, components, and future message shapes
extend :class:`OutboundMessage` without touching this transport.

Failure semantics
-----------------
Any delivery failure raises :class:`DiscordBotDeliveryError`:

- A network failure (connect, timeout, DNS) raises with the underlying
  ``httpx`` exception chained.
- A non-2xx HTTP response raises with the status code and a truncated
  response body for diagnostics.

The caller (the dispatcher, in production) is responsible for deciding
whether a delivery failure warrants user-visible feedback. This module
only reports what happened.

Rate limiting
-------------
Discord's 429 responses (with ``Retry-After`` header) are *not* retried
here. A 429 raises :class:`DiscordBotDeliveryError` like any other
non-2xx. Rate-limit-aware backoff is deferred to a hardening pass.
"""

from __future__ import annotations

import httpx
from LoggingCore import get_logger

from .errors import DiscordBotDeliveryError
from .interactions import ChannelRef, InteractionRef, MessageTarget, OutboundMessage

__all__ = ["DiscordRestClient"]

_log = get_logger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DEFAULT_TIMEOUT_SECONDS = 15.0
_ERROR_BODY_PREVIEW_CHARS = 200


class DiscordRestClient:
    """Concrete :class:`BotClient` over Discord REST.

    The client is stateless per call. ``http_client`` may be supplied
    for testing (with :class:`httpx.MockTransport`) or for reusing a
    shared connection pool; when it is ``None``, the client lazily
    creates its own :class:`httpx.AsyncClient` and owns its lifecycle.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        http_client: httpx.AsyncClient | None = None,
        api_base: str = _DISCORD_API_BASE,
    ) -> None:
        """Store the bot token, optional HTTP client, and API base URL."""
        self._bot_token = bot_token
        self._api_base = api_base.rstrip("/")
        self._http_client = http_client
        self._owns_client = http_client is None

    async def send(self, target: MessageTarget, message: OutboundMessage) -> None:
        """Deliver ``message`` to ``target``, dispatching on target type.

        Interaction targets use the interaction webhook URL. Channel
        targets use the channel messages endpoint with bot auth. Any
        failure raises :class:`DiscordBotDeliveryError`.
        """
        if isinstance(target, InteractionRef):
            await self._send_interaction_followup(target, message)
        else:
            await self._send_channel_message(target, message)

    async def _send_interaction_followup(self, target: InteractionRef, message: OutboundMessage) -> None:
        """POST a followup to the interaction's webhook URL."""
        url = f"{self._api_base}/webhooks/{target.application_id}/{target.token}"
        await self._post(url, message, headers=None, kind="interaction followup")

    async def _send_channel_message(self, target: ChannelRef, message: OutboundMessage) -> None:
        """POST a message to a channel with bot authentication."""
        url = f"{self._api_base}/channels/{target.channel_id}/messages"
        headers = {"Authorization": f"Bot {self._bot_token}"}
        await self._post(url, message, headers=headers, kind="channel post")

    async def _post(
        self,
        url: str,
        message: OutboundMessage,
        *,
        headers: dict[str, str] | None,
        kind: str,
    ) -> None:
        """POST ``message`` and raise :class:`DiscordBotDeliveryError` on failure."""
        client = self._client()
        try:
            response = await client.post(url, json={"content": message.content}, headers=headers)
        except httpx.HTTPError as exc:
            raise DiscordBotDeliveryError(f"{kind} failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            body_preview = response.text[:_ERROR_BODY_PREVIEW_CHARS]
            raise DiscordBotDeliveryError(f"{kind} HTTP {response.status_code}: {body_preview}")

    def _client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one lazily when needed."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
        return self._http_client

    async def aclose(self) -> None:
        """Close the owned HTTP client. Safe to call multiple times."""
        if self._http_client is not None and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None
