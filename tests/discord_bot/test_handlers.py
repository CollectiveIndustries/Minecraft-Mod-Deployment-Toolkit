# tests/discord_bot/test_handlers.py

"""Slash-command handler and dispatcher tests (T3a)."""

from __future__ import annotations

from minecraft.discord_bot.cli_invoker import CliResult, FakeCliInvoker
from minecraft.discord_bot.client import FakeBotClient
from minecraft.discord_bot.config import DiscordBotConfig
from minecraft.discord_bot.errors import DiscordBotCliError
from minecraft.discord_bot.handlers import dispatch
from minecraft.discord_bot.interactions import (
    InteractionContext,
    InteractionRef,
    OutboundMessage,
)

_AUTHORIZED_USER = "400000000000000001"
_AUTHORIZED_ROLE = "600000000000000001"


def _make_config(**overrides: object) -> DiscordBotConfig:
    """Build a DiscordBotConfig with the authorized user allowlisted by default."""
    base: dict[str, object] = {
        "bot_token": "tok",
        "application_id": "100000000000000001",
        "public_key": "a" * 64,
        "guild_id": "200000000000000001",
        "channel_id": "300000000000000001",
        "allowlist_roles": (),
        "allowlist_users": (_AUTHORIZED_USER,),
    }
    base.update(overrides)
    return DiscordBotConfig(**base)  # type: ignore[arg-type]


def _make_ctx(**overrides: object) -> InteractionContext:
    """Build an InteractionContext for the authorized user with subcommand=status."""
    base: dict[str, object] = {
        "interaction_id": "500000000000000001",
        "application_id": "100000000000000001",
        "token": "interaction-token",
        "guild_id": "200000000000000001",
        "channel_id": "300000000000000001",
        "user_id": _AUTHORIZED_USER,
        "user_roles": (),
        "command_name": "upgrade",
        "options": {"subcommand": "status"},
    }
    base.update(overrides)
    return InteractionContext(**base)  # type: ignore[arg-type]


def _last_content(bot: FakeBotClient) -> str:
    """Return the content of the most recent sent message."""
    assert bot.sent, "bot client recorded no messages"
    return bot.sent[-1].message.content


def _approve_args(ctx: InteractionContext, run_id: str) -> list[str]:
    """Return the exact argv the approve handler is expected to pass."""
    return ["approve", run_id, "--interaction-id", ctx.interaction_id, "--operator", f"discord:{ctx.user_id}"]


def _reject_args(ctx: InteractionContext, run_id: str, reason: str | None = None) -> list[str]:
    """Return the exact argv the reject handler is expected to pass."""
    args = ["reject", run_id, "--interaction-id", ctx.interaction_id, "--operator", f"discord:{ctx.user_id}"]
    if reason:
        args.extend(["--reason", reason])
    return args


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_authorized_by_user_runs_handler() -> None:
    """A user in allowlist_users is authorized."""
    ctx = _make_ctx()
    config = _make_config()
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout="ok", stderr=""))
    await dispatch(ctx, bot_config=config, invoker=invoker, bot_client=bot)
    assert invoker.calls == [["status"]]


async def test_authorized_by_role_runs_handler() -> None:
    """A user with an allowlisted role is authorized even when not in allowlist_users."""
    ctx = _make_ctx(user_id="999999999999999999", user_roles=(_AUTHORIZED_ROLE,))
    config = _make_config(allowlist_users=(), allowlist_roles=(_AUTHORIZED_ROLE,))
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout="ok", stderr=""))
    await dispatch(ctx, bot_config=config, invoker=invoker, bot_client=bot)
    assert invoker.calls == [["status"]]


async def test_unauthorized_user_denied() -> None:
    """A user not in either allowlist gets a rejection reply."""
    ctx = _make_ctx(user_id="999999999999999999", user_roles=())
    config = _make_config()
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=config, invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "not authorized" in _last_content(bot).lower()


async def test_empty_allowlists_deny_everyone() -> None:
    """Empty allowlists deny every user."""
    ctx = _make_ctx()
    config = _make_config(allowlist_users=(), allowlist_roles=())
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=config, invoker=invoker, bot_client=bot)
    assert invoker.calls == []


# ---------------------------------------------------------------------------
# Command selection
# ---------------------------------------------------------------------------


async def test_unknown_command_name_replies() -> None:
    """A non-``upgrade`` command name is rejected."""
    ctx = _make_ctx(command_name="other")
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Only /upgrade" in _last_content(bot)


async def test_missing_subcommand_replies() -> None:
    """A missing subcommand is rejected."""
    ctx = _make_ctx(options={})
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Missing subcommand" in _last_content(bot)


async def test_unknown_subcommand_replies() -> None:
    """An unknown subcommand is rejected."""
    ctx = _make_ctx(options={"subcommand": "unknown"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Unknown subcommand" in _last_content(bot)


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------


async def test_handle_status_invokes_cli() -> None:
    """``/upgrade status`` invokes the status CLI command and replies."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout="state summary", stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [["status"]]
    assert "state summary" in _last_content(bot)


async def test_handle_approve_invokes_cli() -> None:
    """``/upgrade approve`` invokes ``approve <run-id>`` with identity flags."""
    ctx = _make_ctx(options={"subcommand": "approve", "run_id": "run-42"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(_approve_args(ctx, "run-42"), CliResult(exit_code=0, stdout="recorded", stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [_approve_args(ctx, "run-42")]
    assert "recorded" in _last_content(bot)


async def test_handle_approve_without_run_id_replies_usage() -> None:
    """``/upgrade approve`` without a run-id replies with usage and does not invoke the CLI."""
    ctx = _make_ctx(options={"subcommand": "approve"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Usage: /upgrade approve" in _last_content(bot)


async def test_handle_reject_invokes_cli() -> None:
    """``/upgrade reject`` invokes ``reject <run-id>`` with identity flags."""
    ctx = _make_ctx(options={"subcommand": "reject", "run_id": "run-42"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(_reject_args(ctx, "run-42"), CliResult(exit_code=0, stdout="recorded", stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [_reject_args(ctx, "run-42")]


async def test_handle_reject_with_reason() -> None:
    """``/upgrade reject`` with a reason appends ``--reason``."""
    ctx = _make_ctx(options={"subcommand": "reject", "run_id": "run-42", "reason": "not ready"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        _reject_args(ctx, "run-42", "not ready"),
        CliResult(exit_code=0, stdout="", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [_reject_args(ctx, "run-42", "not ready")]


async def test_handle_reject_without_run_id_replies_usage() -> None:
    """``/upgrade reject`` without a run-id replies with usage."""
    ctx = _make_ctx(options={"subcommand": "reject"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Usage: /upgrade reject" in _last_content(bot)


async def test_handle_build_inhouse_invokes_cli() -> None:
    """``/upgrade build-inhouse`` invokes ``build-inhouse <name>``."""
    ctx = _make_ctx(options={"subcommand": "build-inhouse", "name": "mymod"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["build-inhouse", "mymod"],
        CliResult(exit_code=0, stdout="build ok", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [["build-inhouse", "mymod"]]


async def test_handle_build_inhouse_without_name_replies_usage() -> None:
    """``/upgrade build-inhouse`` without a name replies with usage."""
    ctx = _make_ctx(options={"subcommand": "build-inhouse"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker()
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == []
    assert "Usage: /upgrade build-inhouse" in _last_content(bot)


async def test_handle_migrate_world_defaults_to_survival() -> None:
    """``/upgrade migrate-world`` without an argument invokes ``snapshot-world survival``."""
    ctx = _make_ctx(options={"subcommand": "migrate-world"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["snapshot-world", "survival"],
        CliResult(exit_code=0, stdout="", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [["snapshot-world", "survival"]]


async def test_handle_migrate_world_explicit_world() -> None:
    """``/upgrade migrate-world creative`` uses the supplied world."""
    ctx = _make_ctx(options={"subcommand": "migrate-world", "world": "creative"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["snapshot-world", "creative"],
        CliResult(exit_code=0, stdout="", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert invoker.calls == [["snapshot-world", "creative"]]


# ---------------------------------------------------------------------------
# T11: identity forwarding
# ---------------------------------------------------------------------------


async def test_handle_approve_forwards_interaction_id() -> None:
    """The interaction ID is forwarded so the CLI can enforce replay protection (Section 11.4).

    Without ``--interaction-id``, a replayed Discord interaction would
    reach the CLI indistinguishable from a fresh approval and would be
    recorded as such. ``FakeCliInvoker.expect()`` enforces an exact argv
    match, so this test fails if the flag is dropped from the invocation.
    """
    ctx = _make_ctx(options={"subcommand": "approve", "run_id": "run-42"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["approve", "run-42", "--interaction-id", ctx.interaction_id, "--operator", f"discord:{ctx.user_id}"],
        CliResult(exit_code=0, stdout="", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)


async def test_handle_reject_forwards_interaction_id() -> None:
    """The interaction ID is forwarded for reject as well (Section 11.4)."""
    ctx = _make_ctx(options={"subcommand": "reject", "run_id": "run-42"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["reject", "run-42", "--interaction-id", ctx.interaction_id, "--operator", f"discord:{ctx.user_id}"],
        CliResult(exit_code=0, stdout="", stderr=""),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_cli_failure_reported_to_user() -> None:
    """A non-zero CLI exit is shown as a formatted failure to the user."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(
        ["status"],
        CliResult(exit_code=1, stdout="", stderr="disk on fire"),
    )
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    content = _last_content(bot)
    assert "exit 1" in content
    assert "disk on fire" in content


async def test_cli_exception_surfaced_to_user() -> None:
    """A DiscordBotCliError from the invoker is shown as a command failure."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect_raise(["status"], DiscordBotCliError("CLI binary not found"))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    content = _last_content(bot)
    assert "Command failed" in content
    assert "CLI binary not found" in content


async def test_unexpected_exception_reported_as_internal_error() -> None:
    """A non-DiscordBotError exception produces a generic internal-error reply."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect_raise(["status"], RuntimeError("boom"))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    content = _last_content(bot)
    assert "Internal error" in content
    assert "RuntimeError" in content


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


async def test_long_cli_output_is_truncated() -> None:
    """Output exceeding 1800 inner characters is truncated with a marker."""
    long_stdout = "x" * 3000
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout=long_stdout, stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    content = _last_content(bot)
    assert "... (truncated)" in content
    assert len(content) < 2000


async def test_empty_output_shows_placeholder() -> None:
    """An empty stdout on success shows ``(no output)``."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout="", stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert "(no output)" in _last_content(bot)


async def test_response_target_is_interaction_ref() -> None:
    """The response is sent through the interaction ref, not a channel ref."""
    ctx = _make_ctx(options={"subcommand": "status"})
    bot = FakeBotClient()
    invoker = FakeCliInvoker().expect(["status"], CliResult(exit_code=0, stdout="ok", stderr=""))
    await dispatch(ctx, bot_config=_make_config(), invoker=invoker, bot_client=bot)
    assert isinstance(bot.sent[0].target, InteractionRef)
    assert bot.sent[0].target.interaction_id == ctx.interaction_id
    assert bot.sent[0].message == OutboundMessage(content=bot.sent[0].message.content)
