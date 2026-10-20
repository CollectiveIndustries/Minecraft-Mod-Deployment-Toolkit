# src/minecraft/discord_bot/handlers.py

"""Slash-command handlers and dispatcher (Slice 15 T3a).

Command routing
---------------
Discord invokes the bot with a top-level ``/upgrade`` command that takes
a subcommand: ``status``, ``approve``, ``reject``, ``build-inhouse``,
``migrate-world``. The transport layer (T3b) normalizes the payload into
an :class:`InteractionContext`; :func:`dispatch` maps the subcommand to
the handler and runs it.

Authorization
-------------
Every dispatch passes through :func:`_check_authorization` first. A user
is authorized if their ID is in ``allowlist_users`` OR any of their role
IDs is in ``allowlist_roles``. Empty allowlists deny everyone:
operators must explicitly authorize at least one user or role.

Handler contract
----------------
A handler is an ``async`` callable receiving ``(ctx, invoker, bot_client)``.
It sends zero or more messages via ``bot_client.send(ctx.to_ref(), ...)``
and returns ``None``. Raising :class:`DiscordBotError` (or any subclass)
is turned into a user-facing "command failed" reply; raising any other
exception is turned into a generic "internal error" reply.

CLI invocation
--------------
Handlers do not touch state files directly. They invoke the
``minecraft-upgrade`` CLI via the :class:`CliInvoker` seam. This
preserves the "bot is not the controller" property: the CLI owns the
state machine, the bot is a relay.

Discord message limit
---------------------
Discord caps individual messages at 2000 characters.
:func:`_format_cli_output` truncates inner text at 1800 characters,
leaving room for the surrounding code-block markers and any prefix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from LoggingCore import get_logger

from .cli_invoker import CliInvoker, CliResult
from .client import BotClient
from .config import DiscordBotConfig
from .errors import DiscordBotAuthorizationError, DiscordBotError, DiscordBotHandlerError
from .interactions import InteractionContext, OutboundMessage

__all__ = [
    "CommandHandler",
    "dispatch",
]

_log = get_logger(__name__)

_CLI_OUTPUT_INNER_LIMIT = 1800
_TRUNCATION_SUFFIX = "\n... (truncated)"

CommandHandler = Callable[[InteractionContext, CliInvoker, BotClient], Awaitable[None]]


async def dispatch(
    ctx: InteractionContext,
    *,
    bot_config: DiscordBotConfig,
    invoker: CliInvoker,
    bot_client: BotClient,
) -> None:
    """Authorize, dispatch, and send a response for one interaction.

    Authorization and handler selection are checked before any CLI
    invocation. A handler exception becomes a user-facing reply;
    exceptions that are not :class:`DiscordBotError` produce a generic
    "internal error" reply with the exception type name.
    """
    try:
        _check_authorization(ctx, bot_config)
    except DiscordBotAuthorizationError:
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content="You are not authorized to invoke this command."),
        )
        return

    try:
        handler = _select_handler(ctx)
    except DiscordBotHandlerError as exc:
        await bot_client.send(ctx.to_ref(), OutboundMessage(content=str(exc)))
        return

    try:
        await handler(ctx, invoker, bot_client)
    except DiscordBotError as exc:
        _log.warning(f"handler failed: {type(exc).__name__}: {exc}")
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content=f"Command failed: {exc}"),
        )
    except Exception as exc:
        _log.exception(f"handler raised unexpectedly for {ctx.command_name!r}/{ctx.options.get('subcommand')!r}")
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content=f"Internal error: {type(exc).__name__}"),
        )


def _check_authorization(ctx: InteractionContext, config: DiscordBotConfig) -> None:
    """Raise :class:`DiscordBotAuthorizationError` if the user is not authorized."""
    if ctx.user_id in config.allowlist_users:
        return
    if any(role in config.allowlist_roles for role in ctx.user_roles):
        return
    raise DiscordBotAuthorizationError(f"user {ctx.user_id} is not authorized")


def _select_handler(ctx: InteractionContext) -> CommandHandler:
    """Return the handler for ``ctx``'s command + subcommand.

    Raises :class:`DiscordBotHandlerError` for unknown command names,
    missing subcommands, or unknown subcommands. The error message is
    surfaced directly to the user, so it names the offending value.
    """
    if ctx.command_name != "upgrade":
        raise DiscordBotHandlerError(f"Unknown command /{ctx.command_name}. Only /upgrade is supported.")
    subcommand = ctx.options.get("subcommand")
    if not subcommand:
        raise DiscordBotHandlerError("Missing subcommand. Try /upgrade status.")
    handler = _HANDLERS.get(subcommand)
    if handler is None:
        raise DiscordBotHandlerError(f"Unknown subcommand {subcommand!r}. Try /upgrade status.")
    return handler


def _format_cli_output(result: CliResult, *, max_inner_length: int = _CLI_OUTPUT_INNER_LIMIT) -> str:
    """Wrap a :class:`CliResult` in a Discord code block.

    Success (``exit_code == 0``) displays stdout. Failure prepends the
    exit code and displays stderr, falling back to stdout when stderr is
    empty. Empty output yields ``"(no output)"``. Inner text longer than
    ``max_inner_length`` is truncated with a ``... (truncated)`` marker;
    the resulting message stays under Discord's 2000-character cap for
    any sane ``max_inner_length`` (default 1800).
    """
    if result.exit_code == 0:
        inner = result.stdout.strip() or "(no output)"
    else:
        body = result.stderr.strip() or result.stdout.strip() or "(no output)"
        inner = f"exit {result.exit_code}\n{body}"
    if len(inner) > max_inner_length:
        inner = inner[: max_inner_length - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    return f"```\n{inner}\n```"


async def _handle_status(ctx: InteractionContext, invoker: CliInvoker, bot_client: BotClient) -> None:
    """``/upgrade status`` - report the current pipeline state."""
    result = await invoker.invoke(["status"])
    await bot_client.send(ctx.to_ref(), OutboundMessage(content=_format_cli_output(result)))


async def _handle_approve(ctx: InteractionContext, invoker: CliInvoker, bot_client: BotClient) -> None:
    """``/upgrade approve <run-id>`` - record a mod-set approval."""
    run_id = ctx.options.get("run_id")
    if not run_id:
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content="Usage: /upgrade approve <run-id>"),
        )
        return
    result = await invoker.invoke(["approve", run_id])
    await bot_client.send(ctx.to_ref(), OutboundMessage(content=_format_cli_output(result)))


async def _handle_reject(ctx: InteractionContext, invoker: CliInvoker, bot_client: BotClient) -> None:
    """``/upgrade reject <run-id> [reason]`` - record a rejection."""
    run_id = ctx.options.get("run_id")
    if not run_id:
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content="Usage: /upgrade reject <run-id> [reason]"),
        )
        return
    args = ["reject", run_id]
    reason = ctx.options.get("reason")
    if reason:
        args.extend(["--reason", reason])
    result = await invoker.invoke(args)
    await bot_client.send(ctx.to_ref(), OutboundMessage(content=_format_cli_output(result)))


async def _handle_build_inhouse(ctx: InteractionContext, invoker: CliInvoker, bot_client: BotClient) -> None:
    """``/upgrade build-inhouse <name>`` - trigger an in-house mod build."""
    name = ctx.options.get("name")
    if not name:
        await bot_client.send(
            ctx.to_ref(),
            OutboundMessage(content="Usage: /upgrade build-inhouse <name>"),
        )
        return
    result = await invoker.invoke(["build-inhouse", name])
    await bot_client.send(ctx.to_ref(), OutboundMessage(content=_format_cli_output(result)))


async def _handle_migrate_world(ctx: InteractionContext, invoker: CliInvoker, bot_client: BotClient) -> None:
    """``/upgrade migrate-world [survival|creative]`` - trigger a world snapshot."""
    world = ctx.options.get("world") or "survival"
    result = await invoker.invoke(["snapshot-world", world])
    await bot_client.send(ctx.to_ref(), OutboundMessage(content=_format_cli_output(result)))


_HANDLERS: dict[str, CommandHandler] = {
    "status": _handle_status,
    "approve": _handle_approve,
    "reject": _handle_reject,
    "build-inhouse": _handle_build_inhouse,
    "migrate-world": _handle_migrate_world,
}
