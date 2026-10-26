# tests/deploy_pack/test_notify_dispatch.py

"""Tests for bot-daemon notification dispatch routing (Slice 15 T3c).

Covers :class:`minecraft.deploy_pack.notifications.BotDaemonNotifier` and
the ``_dispatch`` selection logic that routes notifications through the
bot daemon when ``[discord.notify_dispatch]`` is configured, and through
the legacy webhook otherwise.

The Q5 invariant this file pins: a **configured** bot daemon whose
delivery fails MUST NOT fall back to the webhook. Silent fallback on an
ambiguous delivery failure risks duplicate notifications once the bot
daemon recovers.
"""

from __future__ import annotations

import httpx

from minecraft.deploy_pack.config_model import DiscordConfig
from minecraft.deploy_pack.notifications import (
    BotDaemonDeliveryResult,
    BotDaemonNotifier,
    DiagnosticContext,
    LiveContext,
    NotifyKind,
    notify_diagnostic,
    notify_live,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingPoster:
    """Fake webhook poster that records calls."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        """Configure the return shape and initialize the call log."""
        self.ok = ok
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, url: str, content: str, allowed_mentions: dict, timeout: float) -> tuple[bool, str | None]:
        """Record the call and return the configured result."""
        del timeout
        self.calls.append({"url": url, "content": content, "mentions": allowed_mentions})
        return (self.ok, self.error)


class _FakeNotifier(BotDaemonNotifier):
    """Duck-typed fake notifier: overrides ``notify`` without I/O."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        """Skip parent init; set the return shape."""
        self.ok = ok
        self.error = error
        self.calls: list[dict] = []

    def notify(self, content: str, kind: str) -> BotDaemonDeliveryResult:  # type: ignore[override]
        """Record the call and return the configured result."""
        self.calls.append({"content": content, "kind": kind})
        return BotDaemonDeliveryResult(success=self.ok, error=self.error)


def _live_ctx() -> LiveContext:
    """Return a minimal LiveContext for notify_live tests."""
    return LiveContext(
        tool_version="2.0.0",
        timestamp="2026-10-26T00:00:00Z",
        requested_scopes=["server"],
        instance_list=["survival"],
    )


def _diag_ctx() -> DiagnosticContext:
    """Return a minimal DiagnosticContext for notify_diagnostic tests."""
    return DiagnosticContext(tool_version="2.0.0", timestamp="2026-10-26T00:00:00Z")


def _bot_daemon_config(*, with_token: bool = True) -> DiscordConfig:
    """Return a DiscordConfig with bot daemon dispatch configured."""
    return DiscordConfig(
        bot_daemon_url="http://127.0.0.1:8766",
        bot_daemon_token=("notify-secret" if with_token else None),
    )


# ---------------------------------------------------------------------------
# BotDaemonNotifier: direct unit tests
# ---------------------------------------------------------------------------


def _mock_notifier(handler) -> BotDaemonNotifier:
    """Return a notifier bound to an httpx.MockTransport."""
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return BotDaemonNotifier(base_url="http://bot.example:8766", token="tok", http_client=client)


def test_notifier_success_returns_true() -> None:
    """A 2xx response yields success=True."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"status": "ok"})

    notifier = _mock_notifier(handler)
    result = notifier.notify("hello", "live")
    assert result.success is True
    assert result.error is None
    assert len(recorded) == 1
    assert str(recorded[0].url) == "http://bot.example:8766/notify"
    assert recorded[0].headers["authorization"] == "Bearer tok"
    body = recorded[0].content.decode()
    assert '"content": "hello"' in body or '"content":"hello"' in body
    assert '"kind": "live"' in body or '"kind":"live"' in body


def test_notifier_http_error_returns_false() -> None:
    """A non-2xx response yields success=False with the status in the error."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="server error")

    notifier = _mock_notifier(handler)
    result = notifier.notify("hi", "failure")
    assert result.success is False
    assert result.error is not None
    assert "HTTP 500" in result.error


def test_notifier_network_error_returns_false() -> None:
    """A network error yields success=False with the exception name in the error."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("simulated connection failure")

    notifier = _mock_notifier(handler)
    result = notifier.notify("hi", "live")
    assert result.success is False
    assert result.error is not None
    assert "ConnectError" in result.error


# ---------------------------------------------------------------------------
# _dispatch via notify_live: transport selection
# ---------------------------------------------------------------------------


def test_bot_daemon_configured_dispatches_via_bot_daemon() -> None:
    """A configured bot daemon routes the notification through BotDaemonNotifier."""
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    result = notify_live(
        "hello {tool_version}",
        _live_ctx(),
        webhook_url="https://hooks.example/wh",
        all_role_ids=[],
        discord_config=_bot_daemon_config(),
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is True
    assert result.transport == "bot_daemon"
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["kind"] == NotifyKind.LIVE
    assert poster.calls == [], "webhook poster must not be called when bot daemon is configured"


def test_bot_daemon_delivery_failure_does_not_fall_back_to_webhook() -> None:
    """A bot-daemon delivery failure MUST NOT invoke the webhook poster."""
    notifier = _FakeNotifier(ok=False, error="HTTP 502: bad gateway")
    poster = _RecordingPoster(ok=True)
    result = notify_live(
        "hello",
        _live_ctx(),
        webhook_url="https://hooks.example/wh",
        all_role_ids=[],
        discord_config=_bot_daemon_config(),
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is False
    assert result.transport == "bot_daemon"
    assert "502" in (result.error or "")
    assert poster.calls == [], "no silent webhook fallback allowed (Q5)"


def test_webhook_used_when_bot_daemon_not_configured() -> None:
    """Without bot daemon config, dispatch goes through the webhook poster."""
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    result = notify_live(
        "hello",
        _live_ctx(),
        webhook_url="https://hooks.example/wh",
        all_role_ids=["111"],
        discord_config=None,
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is True
    assert result.transport == "webhook"
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"] == "https://hooks.example/wh"
    assert notifier.calls == [], "bot daemon notifier must not be called"


def test_skipped_when_neither_transport_configured() -> None:
    """With neither bot daemon nor webhook URL, the notification is skipped."""
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    result = notify_live(
        "hello",
        _live_ctx(),
        webhook_url=None,
        all_role_ids=[],
        discord_config=None,
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is False
    assert result.skipped is True
    assert notifier.calls == []
    assert poster.calls == []


def test_over_2000_chars_rejected_before_transport_selection() -> None:
    """The 2000-char guard runs before transport selection."""
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    huge = "x" * 2500
    result = notify_live(
        "{tool_version}",
        LiveContext(
            tool_version=huge,
            timestamp="t",
            requested_scopes=["server"],
            instance_list=["survival"],
        ),
        webhook_url="https://hooks.example/wh",
        all_role_ids=[],
        discord_config=_bot_daemon_config(),
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is False
    assert "2000" in (result.error or "")
    assert notifier.calls == []
    assert poster.calls == []


def test_url_set_but_token_none_dispatches_via_webhook_with_warning() -> None:
    """URL without token means the operator partially configured; webhook is used.

    This is a belt-and-braces test: ``config_model`` rejects this state at
    load time, so it can only appear via direct DiscordConfig construction
    in code. The dispatch layer must not silently drop the notification.
    """
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    result = notify_live(
        "hello",
        _live_ctx(),
        webhook_url="https://hooks.example/wh",
        all_role_ids=[],
        discord_config=_bot_daemon_config(with_token=False),
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.transport == "webhook"
    assert poster.calls != []


def test_diagnostic_uses_bot_daemon_when_configured() -> None:
    """notify_diagnostic also honors bot daemon dispatch."""
    notifier = _FakeNotifier(ok=True)
    poster = _RecordingPoster(ok=True)
    result = notify_diagnostic(
        "diagnostic {tool_version}",
        _diag_ctx(),
        webhook_url="https://hooks.example/wh",
        all_role_ids=[],
        discord_config=_bot_daemon_config(),
        poster=poster,
        bot_daemon_notifier=notifier,
    )
    assert result.success is True
    assert result.transport == "bot_daemon"
    assert notifier.calls[0]["kind"] == NotifyKind.DIAGNOSTIC
