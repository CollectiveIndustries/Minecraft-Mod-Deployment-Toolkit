# tests/discord_bot/test_client.py

"""BotClient protocol and FakeBotClient tests (T2)."""

from __future__ import annotations

import asyncio

import pytest

from minecraft.discord_bot.client import (
    BotClient,
    FakeBotClient,
    SentMessage,
)
from minecraft.discord_bot.interactions import (
    ChannelRef,
    InteractionRef,
    OutboundMessage,
)


async def _send_channel(fake: FakeBotClient, channel_id: str, content: str) -> None:
    """Send a channel-targeted message through ``fake``."""
    await fake.send(ChannelRef(channel_id=channel_id), OutboundMessage(content=content))


async def _send_interaction(fake: FakeBotClient, interaction_id: str, content: str) -> None:
    """Send an interaction-targeted message through ``fake``."""
    await fake.send(
        InteractionRef(interaction_id=interaction_id, application_id="app", token="tok"),
        OutboundMessage(content=content),
    )


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_fake_bot_client_satisfies_protocol() -> None:
    """FakeBotClient is structurally a BotClient."""
    fake = FakeBotClient()
    assert isinstance(fake, BotClient)


def test_fake_bot_client_send_is_coroutine_function() -> None:
    """The send method is async so callers must await it."""
    assert asyncio.iscoroutinefunction(FakeBotClient().send)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_send_with_channel_ref_records_message() -> None:
    """Sending to a channel records a SentMessage with the exact arguments."""
    fake = FakeBotClient()
    await _send_channel(fake, "123", "hello")
    assert len(fake.sent) == 1
    entry = fake.sent[0]
    assert isinstance(entry, SentMessage)
    assert entry.target == ChannelRef(channel_id="123")
    assert entry.message == OutboundMessage(content="hello")


async def test_send_with_interaction_ref_records_message() -> None:
    """Sending to an interaction records a SentMessage with the InteractionRef target."""
    fake = FakeBotClient()
    await _send_interaction(fake, "999", "hi")
    assert len(fake.sent) == 1
    entry = fake.sent[0]
    assert isinstance(entry.target, InteractionRef)
    assert entry.target.interaction_id == "999"
    assert entry.message.content == "hi"


async def test_multiple_sends_accumulate_in_order() -> None:
    """Multiple sends append to the call log in order."""
    fake = FakeBotClient()
    await _send_channel(fake, "1", "first")
    await _send_channel(fake, "1", "second")
    await _send_channel(fake, "1", "third")
    assert [entry.message.content for entry in fake.sent] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------


async def test_fail_next_raises_queued_exception() -> None:
    """A queued failure is raised by the next send."""
    fake = FakeBotClient()
    fake.fail_next(RuntimeError("simulated"))
    with pytest.raises(RuntimeError, match="simulated"):
        await _send_channel(fake, "1", "hi")


async def test_fail_next_does_not_record() -> None:
    """A failed send leaves the call log empty."""
    fake = FakeBotClient()
    fake.fail_next(RuntimeError("simulated"))
    with pytest.raises(RuntimeError):
        await _send_channel(fake, "1", "hi")
    assert fake.sent == []


async def test_multiple_failures_raised_in_order() -> None:
    """Two queued failures are raised in FIFO order, then sends succeed."""
    fake = FakeBotClient()
    fake.fail_next(RuntimeError("first"))
    fake.fail_next(RuntimeError("second"))
    with pytest.raises(RuntimeError, match="first"):
        await _send_channel(fake, "1", "a")
    with pytest.raises(RuntimeError, match="second"):
        await _send_channel(fake, "1", "b")
    await _send_channel(fake, "1", "c")
    assert len(fake.sent) == 1
    assert fake.sent[0].message.content == "c"


async def test_failure_queue_does_not_leak_into_later_successes() -> None:
    """One queued failure raises once and does not prevent later sends."""
    fake = FakeBotClient()
    fake.fail_next(RuntimeError("transient"))
    with pytest.raises(RuntimeError):
        await _send_channel(fake, "1", "a")
    await _send_channel(fake, "1", "b")
    assert len(fake.sent) == 1
    assert fake.sent[0].message.content == "b"
