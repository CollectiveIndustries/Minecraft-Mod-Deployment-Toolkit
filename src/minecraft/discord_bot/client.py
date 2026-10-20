# src/minecraft/discord_bot/client.py

"""Outbound bot client seam (spec v1.4.0 Section 11.3).

:class:`BotClient` is the injection point for the concrete outbound
Discord transport. T3 will implement it against Discord's REST API
using Discord's bot token for authentication. T2 ships only the
protocol and an in-memory test double.

**Outbound only.** Inbound interaction handling lives in T3's HTTP
transport, not here: Discord delivers interactions as signed HTTP POSTs
to the bot daemon's endpoint, and the daemon verifies signatures and
normalizes payloads before any handler runs. ``BotClient`` is the path
back out - sending responses to interactions, posting to channels.

Unified ``send`` shape
----------------------
Both inbound interaction responses and outbound channel posts travel
through one method. Embeds, buttons, and components are additions to
:class:`OutboundMessage` and do not require new protocol methods. This
keeps every concrete transport's implementation surface flat as the
feature set grows.

Transport neutrality
--------------------
Nothing in this module assumes HTTP, Gateway, or any other delivery
mechanism. The concrete transport is a T3 decision. The protocol is a
pure dispatch shape: ``send(target, message) -> None`` (awaitable). It
carries no notion of connection state, retries, rate-limit handling, or
authentication; those are the concrete implementation's concerns.

Failure semantics
-----------------
A ``send`` that raises is a **delivery failure**. A
:class:`DiscordBotConfig` that is ``None`` means the bot is **disabled**.
These two states MUST NOT be collapsed. A configured-but-unreachable
bot is a distinct condition from "no bot configured", and conflating
them risks a silent fallback to the legacy webhook that would produce
duplicate notifications once the bot recovers. Callers in
``notifications.py`` decide whether to fall back; this module only
reports what happened.

The bot daemon owns Discord delivery end-to-end. ``deploy_pack`` calls
into the daemon (via the notification facade) rather than posting to
Discord directly. A concrete ``BotClient`` implementation therefore sits
inside the bot daemon and is never instantiated by external callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .interactions import MessageTarget, OutboundMessage

__all__ = [
    "BotClient",
    "FakeBotClient",
    "SentMessage",
]


@runtime_checkable
class BotClient(Protocol):
    """Protocol every concrete outbound bot transport must satisfy.

    The method is asynchronous because Discord outbound delivery is
    I/O-bound and T3's concrete implementation is HTTP-based. Tests use
    :class:`FakeBotClient`, whose ``send`` is also async so
    ``await``-heavy handler code is exercised through the same call
    shape as production.

    A transport MUST deliver ``message`` to ``target`` exactly once, or
    raise. Silent drops (returning ``None`` without sending) violate the
    protocol. Rate-limit retries, signature verification, and connection
    management are implementation concerns and are not part of this
    contract.
    """

    async def send(self, target: MessageTarget, message: OutboundMessage) -> None:
        """Deliver ``message`` to ``target``.

        Raises on delivery failure. The exception type is
        implementation-defined; callers distinguish "disabled" (config
        is ``None``) from "failed delivery" (``send`` raised) at the
        call site, not by inspecting the exception.
        """
        ...


@dataclass(frozen=True)
class SentMessage:
    """One recorded delivery from :class:`FakeBotClient`.

    ``target`` and ``message`` are the exact arguments passed to
    ``send``. The record is captured only on successful delivery; a
    ``send`` that raises does not append to the fake's call log.
    """

    target: MessageTarget
    message: OutboundMessage


class FakeBotClient:
    """In-memory :class:`BotClient` for tests.

    Records every successful ``send`` in :attr:`sent`. Failures are
    pre-loaded with :meth:`fail_next`: the next ``send`` will raise the
    queued exception instead of recording. The queue is FIFO, so multiple
    queued failures are raised in order. A failed ``send`` does **not**
    consume a queue slot from a later, unrelated success.
    """

    def __init__(self) -> None:
        """Initialise an empty call log and failure queue."""
        self.sent: list[SentMessage] = []
        self._failures: list[Exception] = []

    def fail_next(self, exc: Exception) -> None:
        """Queue ``exc`` to be raised by the next ``send`` call.

        Calls queue in FIFO order: two calls to ``fail_next`` cause the
        next two ``send`` invocations to raise, in order.
        """
        self._failures.append(exc)

    async def send(self, target: MessageTarget, message: OutboundMessage) -> None:
        """Record the delivery, or raise the next queued failure.

        A queued failure is raised *before* the message is recorded, so
        ``sent`` contains only successful deliveries. This matches the
        protocol's "deliver exactly once or raise" contract: a
        transport that raises did not deliver.
        """
        if self._failures:
            raise self._failures.pop(0)
        self.sent.append(SentMessage(target=target, message=message))
