# src/minecraft/discord_bot/interactions.py

"""Normalized interaction types (spec v1.4.0 Section 11.3, Section 11.4).

This module defines the shapes the bot passes between its transport
layer (T3) and its command handlers (T3). Nothing here imports
:mod:`discord.py`; every type is a plain frozen dataclass that can be
constructed from raw Discord JSON and tested in isolation. This keeps
the T3 signed-HTTP transport swappable and lets handlers be exercised
without a live bot.

Two axes of types
-----------------
**Message targets** identify *where* a response goes. An inbound
interaction is answered through its one-shot interaction token; an
outbound notification is posted to a persistent channel. Both are
first-class so :meth:`BotClient.send` can dispatch on target type without
a token-keyed registry.

**Message content** is *what* is being sent. T2 carries plain text only.
Embeds, buttons, and select menus are future additions to
:class:`OutboundMessage`; the target types and the protocol shape do not
change when they arrive.

Credential handling
-------------------
The interaction token is a credential: an attacker with it can post to
the interaction's followup channel for 15 minutes. Both
:class:`InteractionRef` and :class:`InteractionContext` carry the token
via :func:`dataclasses.field` with ``repr=False`` so a stray ``print()``
or log call cannot leak it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ChannelRef",
    "InteractionContext",
    "InteractionRef",
    "MessageTarget",
    "OutboundMessage",
]


@dataclass(frozen=True)
class OutboundMessage:
    """Content to deliver to a channel or interaction.

    Only ``content`` (plain text) is present in T2. Future fields for
    embeds, action rows, components, and attachments can be added here
    without changing :class:`BotClient.send` or any target type.
    """

    content: str


@dataclass(frozen=True)
class InteractionRef:
    """One-shot target referring to a pending interaction response.

    An :class:`InteractionRef` can deliver exactly one initial response
    (Discord requires it within three seconds of receiving the
    interaction) and followup messages for up to fifteen minutes after
    the initial response. The ``token`` field is a credential and is
    excluded from ``repr``.

    ``application_id`` is carried so the transport can confirm the
    interaction targets this application; it is not needed in the HTTP
    response URL but is needed by the signature-verification step.
    """

    interaction_id: str
    application_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ChannelRef:
    """Persistent target referring to a Discord channel."""

    channel_id: str


MessageTarget = InteractionRef | ChannelRef


@dataclass(frozen=True)
class InteractionContext:
    """Normalized inbound interaction passed to command handlers.

    Constructed at the HTTP boundary in T3 after Discord's Ed25519
    request signature has been validated. Handlers never see the raw
    payload; they see this context. The ``token`` field is excluded from
    ``repr`` for the same reason as :class:`InteractionRef`.

    ``command_name`` is populated for ``APPLICATION_COMMAND``
    interactions and is ``None`` for component and modal interactions.

    ``options`` is a mapping of slash-command option names to their
    string values. It is a plain ``dict`` for handler ergonomics. As a
    frozen dataclass field the dict is not itself frozen, so callers
    MUST treat it as read-only; nothing in this module mutates it.
    """

    interaction_id: str
    application_id: str
    token: str = field(repr=False)
    guild_id: str
    channel_id: str
    user_id: str
    user_roles: tuple[str, ...]
    command_name: str | None
    options: dict[str, str]

    def to_ref(self) -> InteractionRef:
        """Return the :class:`InteractionRef` for responding to this interaction.

        A handler that has finished processing constructs an
        :class:`InteractionRef` from its context and passes it to
        :meth:`BotClient.send`. This method exists so handlers do not
        have to reconstruct the ref inline, and so the credential field
        has exactly one canonical construction site.
        """
        return InteractionRef(
            interaction_id=self.interaction_id,
            application_id=self.application_id,
            token=self.token,
        )
