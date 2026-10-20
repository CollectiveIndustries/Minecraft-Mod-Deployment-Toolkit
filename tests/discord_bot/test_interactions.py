# tests/discord_bot/test_interactions.py

"""Normalized interaction type tests (T2)."""

from __future__ import annotations

import dataclasses

import pytest

from minecraft.discord_bot.interactions import (
    ChannelRef,
    InteractionContext,
    InteractionRef,
    MessageTarget,
    OutboundMessage,
)


def _context() -> InteractionContext:
    """Build a fully-populated InteractionContext for tests."""
    return InteractionContext(
        interaction_id="100000000000000001",
        application_id="200000000000000001",
        token="very-secret-token",
        guild_id="300000000000000001",
        channel_id="400000000000000001",
        user_id="500000000000000001",
        user_roles=("600000000000000001",),
        command_name="upgrade",
        options={"subcommand": "status"},
    )


# ---------------------------------------------------------------------------
# OutboundMessage
# ---------------------------------------------------------------------------


def test_outbound_message_constructs_with_content() -> None:
    """OutboundMessage carries the plain text content."""
    msg = OutboundMessage(content="hello")
    assert msg.content == "hello"


def test_outbound_message_is_frozen() -> None:
    """OutboundMessage rejects field assignment."""
    msg = OutboundMessage(content="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "world"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ChannelRef
# ---------------------------------------------------------------------------


def test_channel_ref_constructs_with_id() -> None:
    """ChannelRef carries the channel id."""
    ref = ChannelRef(channel_id="123")
    assert ref.channel_id == "123"


def test_channel_ref_is_frozen() -> None:
    """ChannelRef rejects field assignment."""
    ref = ChannelRef(channel_id="123")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.channel_id = "456"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# InteractionRef
# ---------------------------------------------------------------------------


def test_interaction_ref_constructs_with_all_fields() -> None:
    """InteractionRef carries id, application id, and token."""
    ref = InteractionRef(
        interaction_id="100000000000000001",
        application_id="200000000000000001",
        token="secret",
    )
    assert ref.interaction_id == "100000000000000001"
    assert ref.application_id == "200000000000000001"
    assert ref.token == "secret"


def test_interaction_ref_is_frozen() -> None:
    """InteractionRef rejects field assignment."""
    ref = InteractionRef(interaction_id="1", application_id="2", token="t")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.token = "other"  # type: ignore[misc]


def test_interaction_ref_repr_does_not_contain_token() -> None:
    """The credential token is excluded from repr."""
    ref = InteractionRef(interaction_id="1", application_id="2", token="super-secret-value")
    assert "super-secret-value" not in repr(ref)
    assert "1" in repr(ref)
    assert "2" in repr(ref)


# ---------------------------------------------------------------------------
# InteractionContext
# ---------------------------------------------------------------------------


def test_interaction_context_constructs_with_all_fields() -> None:
    """InteractionContext carries every normalized field."""
    ctx = _context()
    assert ctx.interaction_id == "100000000000000001"
    assert ctx.application_id == "200000000000000001"
    assert ctx.token == "very-secret-token"
    assert ctx.guild_id == "300000000000000001"
    assert ctx.channel_id == "400000000000000001"
    assert ctx.user_id == "500000000000000001"
    assert ctx.user_roles == ("600000000000000001",)
    assert ctx.command_name == "upgrade"
    assert ctx.options == {"subcommand": "status"}


def test_interaction_context_is_frozen() -> None:
    """InteractionContext rejects field assignment."""
    ctx = _context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.token = "other"  # type: ignore[misc]


def test_interaction_context_repr_does_not_contain_token() -> None:
    """The credential token is excluded from repr."""
    ctx = _context()
    assert "very-secret-token" not in repr(ctx)
    assert "100000000000000001" in repr(ctx)
    assert "upgrade" in repr(ctx)


def test_to_ref_returns_matching_ref() -> None:
    """to_ref() produces an InteractionRef with the credential and identity fields."""
    ctx = _context()
    ref = ctx.to_ref()
    assert isinstance(ref, InteractionRef)
    assert ref.interaction_id == ctx.interaction_id
    assert ref.application_id == ctx.application_id
    assert ref.token == ctx.token


def test_to_ref_repr_also_hides_token() -> None:
    """The InteractionRef produced by to_ref() also excludes the token from repr."""
    ref = _context().to_ref()
    assert "very-secret-token" not in repr(ref)


def test_options_can_be_empty() -> None:
    """Options may be an empty mapping for interactions with no arguments."""
    ctx = InteractionContext(
        interaction_id="1",
        application_id="2",
        token="t",
        guild_id="3",
        channel_id="4",
        user_id="5",
        user_roles=(),
        command_name=None,
        options={},
    )
    assert ctx.options == {}
    assert ctx.command_name is None


# ---------------------------------------------------------------------------
# MessageTarget union
# ---------------------------------------------------------------------------


def test_message_target_accepts_interaction_ref() -> None:
    """InteractionRef satisfies the MessageTarget alias."""
    ref: MessageTarget = InteractionRef(interaction_id="1", application_id="2", token="t")
    assert isinstance(ref, InteractionRef)


def test_message_target_accepts_channel_ref() -> None:
    """ChannelRef satisfies the MessageTarget alias."""
    ref: MessageTarget = ChannelRef(channel_id="123")
    assert isinstance(ref, ChannelRef)
