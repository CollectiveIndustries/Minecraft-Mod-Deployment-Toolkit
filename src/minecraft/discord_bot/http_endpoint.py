# src/minecraft/discord_bot/http_endpoint.py

"""Signed-HTTP interaction endpoint plus notify endpoint (Slice 15 T3b/T3c).

Discord delivers application command interactions as signed HTTP POSTs
to a configured endpoint URL. This plugin is that endpoint. It enforces
five properties in order:

1. **Signature verification**: every interaction request must carry
   ``X-Signature-Ed25519`` and ``X-Signature-Timestamp`` headers whose
   signature validates under the bot's configured Ed25519 public key.
   A missing or invalid signature returns HTTP 401 before any payload
   bytes are parsed.

2. **Parse**: the request body must be valid JSON. Malformed JSON
   returns HTTP 400.

3. **PING handshake**: Discord sends a PING (type 1) when the endpoint
   URL is first configured. Respond with PONG (type 1) immediately.

4. **Application commands**: type 2 payloads are normalized into an
   :class:`InteractionContext` and scheduled on a background task. The
   endpoint returns DEFERRED (type 5) immediately, satisfying Discord's
   three-second initial-response window even when the CLI subprocess
   takes fifteen seconds.

5. **Nothing else**: component, modal, and autocomplete interactions
   are rejected with HTTP 400 until those surfaces land in a later
   slice.

Payload normalization
---------------------
Discord nests slash-command arguments under ``data.options``, with
subcommands adding another level:

    {"name": "upgrade", "options": [
        {"name": "approve", "type": 1, "options": [
            {"name": "run_id", "type": 3, "value": "run-42"}
        ]}
    ]}

:func:`normalize_interaction_context` flattens this to::

    options = {"subcommand": "approve", "run_id": "run-42"}

The dispatcher in :mod:`handlers` reads ``options["subcommand"]`` plus
any named arguments. A top-level command without a subcommand (rare in
this bot's design) flattens to ``options`` with only the direct
arguments; there is no ``"subcommand"`` key.

Notify endpoint (Slice 15 T3c)
------------------------------
When :class:`DiscordBotPlugin` is constructed with a non-``None``
``notify_bearer_token``, it registers a second route at ``POST /notify``
for the ``deploy_pack`` process to dispatch notifications through the
bot daemon instead of the legacy webhook. The route enforces a bearer
token (constant-time compare) and delivers ``{"content": str, "kind":
str}`` to the bot's configured operator channel via
:meth:`BotClient.send`.

The auth model differs by design:

  * ``/interactions`` - Ed25519 signature headers, provided by Discord.
  * ``/notify`` - ``Authorization: Bearer <token>`` header, provided by
    ``deploy_pack``. This token is the same ``.env`` value the bot
    daemon's ``[daemon].notify_bearer_token_key`` names.

Both routes share the same :class:`BotClient` for outbound delivery, so
the delivery path is unified.

Plugin identification
---------------------
The class attribute :attr:`DiscordBotPlugin.name` follows the convention
established by ``deploy_pack.daemon.plugins.http.HttpAdminPlugin``
(``name = "http-admin"``). DaemonCore's ``Plugin`` protocol identifies a
plugin by this attribute when it registers and reports it in the health
rollup; omitting it yields an ``AttributeError`` during ``Daemon.plugin()``.

HTTP lifecycle
--------------
The plugin owns its own uvicorn server, following the pattern set by
:class:`deploy_pack.daemon.plugins.http.HttpAdminPlugin`. DaemonCore's
``Plugin`` protocol is transport-neutral: it does not assume or provide
an HTTP stack. A plugin that wants to serve HTTP constructs its server
inside :meth:`start`, awaits readiness, holds the server and its task
for :meth:`stop`, and reports :meth:`health` accordingly. A plugin that
wants a socket, an in-memory queue, or a UNIX domain socket owns that
decision; DaemonCore only orders the lifecycle.

:meth:`start` is idempotent: a second call while running returns
immediately. :meth:`stop` is safe on a not-yet-started plugin. Both
match :class:`HttpAdminPlugin`'s contract, so an operator running both
daemons sees identical startup and shutdown semantics.

Scheduling
----------
Handlers are scheduled, not awaited. The production scheduler calls
``asyncio.create_task`` and retains the returned task in
``_pending_tasks`` until it completes; without that reference the event
loop may garbage-collect a still-running task (RUF006). The scheduler
is injectable so tests can substitute a fake that records the
normalized context without spawning real tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import uvicorn
from DaemonCore import HealthReport
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from LoggingCore import get_logger

from .cli_invoker import CliInvoker
from .client import BotClient
from .config import DiscordBotConfig
from .handlers import dispatch
from .interactions import ChannelRef, InteractionContext, OutboundMessage
from .signature import verify_discord_signature

__all__ = [
    "APPLICATION_COMMAND_TYPE",
    "PING_TYPE",
    "DiscordBotPlugin",
    "normalize_interaction_context",
]

_log = get_logger(__name__)

# Discord interaction types
PING_TYPE = 1
APPLICATION_COMMAND_TYPE = 2

# Discord interaction response types
PONG_TYPE = 1
DEFERRED_CHANNEL_MESSAGE_TYPE = 5

# Discord option types we care about for flattening
SUB_COMMAND_OPTION_TYPE = 1

# HTTP statuses
_HTTP_401 = 401
_HTTP_400 = 400
_HTTP_502 = 502

# uvicorn readiness wait. Matches HttpAdminPlugin's 5s budget; identical
# diagnostic shape so an operator reading either daemon's startup failure
# sees the same message format.
_STARTUP_READY_TIMEOUT_SECONDS = 5.0


DispatchScheduler = Callable[[InteractionContext], None]


def normalize_interaction_context(payload: dict[str, Any]) -> InteractionContext:
    """Convert a raw Discord interaction payload into an :class:`InteractionContext`.

    Raises :class:`ValueError` when the payload does not contain the
    minimum fields required to construct the context. That is a
    malformed-request signal; callers translate it to HTTP 400.

    Guild interactions carry ``member.user.id`` and ``member.roles``.
    Direct-message interactions carry ``user.id`` and no roles; those
    are normalized to an empty tuple so downstream authorization sees
    a consistent shape.
    """
    interaction_id = _require_str(payload, "id")
    application_id = _require_str(payload, "application_id")
    token = _require_str(payload, "token")
    guild_id = _require_str(payload, "guild_id")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("missing or malformed channel_id")

    user_id, user_roles = _extract_actor(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("missing or malformed data")

    command_name = data.get("name")
    if not isinstance(command_name, str) or not command_name:
        raise ValueError("missing or malformed data.name")

    options = _flatten_options(data.get("options"))

    return InteractionContext(
        interaction_id=interaction_id,
        application_id=application_id,
        token=token,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        user_roles=user_roles,
        command_name=command_name,
        options=options,
    )


def _require_str(payload: dict[str, Any], key: str) -> str:
    """Return ``payload[key]`` as a non-empty string, or raise ``ValueError``."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing or malformed {key}")
    return value


def _extract_actor(payload: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return ``(user_id, user_roles)`` from a guild or DM interaction payload.

    Guild interactions expose ``member.user.id`` and ``member.roles``.
    DM interactions expose ``user.id`` and no roles. Both shapes appear
    under ``APPLICATION_COMMAND``; normalize them here so downstream
    code has one shape.
    """
    member = payload.get("member")
    if isinstance(member, dict):
        user = member.get("user")
        if isinstance(user, dict):
            user_id = user.get("id")
            if isinstance(user_id, str) and user_id:
                roles_raw = member.get("roles")
                roles = tuple(r for r in roles_raw if isinstance(r, str)) if isinstance(roles_raw, list) else ()
                return (user_id, roles)
    user = payload.get("user")
    if isinstance(user, dict):
        user_id = user.get("id")
        if isinstance(user_id, str) and user_id:
            return (user_id, ())
    raise ValueError("missing or malformed user identity")


def _flatten_options(raw_options: Any) -> dict[str, str]:
    """Flatten Discord's nested options into a ``{name: value}`` mapping.

    Subcommand options (type 1) contribute their name under the
    ``"subcommand"`` key plus each of their nested options directly.
    Other option types contribute their name and value directly. A
    top-level command with no subcommand contributes only direct
    options; there is no ``"subcommand"`` key in that case.
    """
    result: dict[str, str] = {}
    if not isinstance(raw_options, list):
        return result
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        name = opt.get("name")
        if not isinstance(name, str) or not name:
            continue
        opt_type = opt.get("type")
        if opt_type == SUB_COMMAND_OPTION_TYPE:
            result["subcommand"] = name
            nested = opt.get("options")
            if isinstance(nested, list):
                for sub in nested:
                    if not isinstance(sub, dict):
                        continue
                    sub_name = sub.get("name")
                    sub_value = sub.get("value")
                    if isinstance(sub_name, str) and sub_name and isinstance(sub_value, str):
                        result[sub_name] = sub_value
        else:
            value = opt.get("value")
            if isinstance(value, str):
                result[name] = value
    return result


class DiscordBotPlugin:
    """FastAPI plugin for Discord's signed-HTTP interaction endpoint.

    The plugin exposes ``POST /interactions`` plus an optional ``POST
    /notify`` route, and implements the DaemonCore plugin contract
    (``build_app``, ``start``, ``stop``, ``health``) plus the ``name``
    identifier DaemonCore uses when it registers and reports plugins.

    HTTP lifecycle is owned by the plugin, mirroring
    :class:`deploy_pack.daemon.plugins.http.HttpAdminPlugin`:
    :meth:`start` constructs a :class:`uvicorn.Server`, spawns
    :meth:`uvicorn.Server.serve` as an asyncio task, and awaits the
    server's readiness flag before returning. :meth:`stop` signals
    shutdown and awaits the task. DaemonCore orders plugin lifecycles
    but does not assume any plugin serves HTTP, so a plugin that needs a
    socket owns it.

    It is transport-neutral for scheduling: production retains task
    references in :attr:`_pending_tasks` so the event loop cannot
    garbage-collect a still-running dispatch task; tests inject a fake
    scheduler that records contexts without spawning real tasks.

    ``notify_bearer_token`` is the shared secret between the bot daemon
    and the ``deploy_pack`` process (Slice 15 T3c). When ``None``, the
    ``/notify`` route is not registered and the plugin behaves
    identically to its T3b-only form. When set, ``/notify`` requires
    ``Authorization: Bearer <token>`` and delivers the payload's
    ``content`` to the bot's configured operator channel.
    """

    name = "discord-bot"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        bot_config: DiscordBotConfig,
        invoker: CliInvoker,
        bot_client: BotClient,
        dispatch_scheduler: DispatchScheduler | None = None,
        notify_bearer_token: str | None = None,
    ) -> None:
        """Store collaborators and the scheduler."""
        self._host = host
        self._port = port
        self._bot_config = bot_config
        self._invoker = invoker
        self._bot_client = bot_client
        self._notify_bearer_token = notify_bearer_token
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._dispatch_scheduler: DispatchScheduler = dispatch_scheduler if dispatch_scheduler is not None else self._make_production_scheduler()
        self._started = False
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def _make_production_scheduler(self) -> DispatchScheduler:
        """Return a scheduler that fires and retains an asyncio task per dispatch.

        The task reference is retained in :attr:`_pending_tasks` and
        discarded on completion via :meth:`asyncio.Task.add_done_callback`.
        Without the retained reference, the event loop may
        garbage-collect the task while it is still running (RUF006).
        """

        def _schedule(ctx: InteractionContext) -> None:
            task = asyncio.create_task(
                dispatch(
                    ctx,
                    bot_config=self._bot_config,
                    invoker=self._invoker,
                    bot_client=self._bot_client,
                )
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        return _schedule

    def build_app(self, ctx: Any = None) -> FastAPI:
        """Build the FastAPI application.

        Registers ``POST /interactions`` unconditionally. Registers
        ``POST /notify`` only when :attr:`_notify_bearer_token` is set:
        a bot daemon without a configured notify token has no
        authenticated notify surface.

        Public so tests can drive the routes via ``TestClient`` without
        binding a real socket. :meth:`start` calls this once to construct
        the app it hands to uvicorn.
        """
        del ctx  # unused for now; DaemonCore may later provide shared resources
        app = FastAPI()
        app.add_api_route("/interactions", self._handle_interactions, methods=["POST"])
        if self._notify_bearer_token is not None:
            app.add_api_route("/notify", self._handle_notify, methods=["POST"])
        return app

    async def start(self, ctx: Any = None) -> None:
        """Build the app, spawn uvicorn, and await readiness.

        Idempotent: a second call while the server is running returns
        immediately without re-binding. Mirrors
        :meth:`deploy_pack.daemon.plugins.http.HttpAdminPlugin.start`'s
        contract so the two daemons have identical startup semantics.

        Raises :class:`RuntimeError` if uvicorn's task terminates before
        the server signals ready, or if readiness is not observed within
        :data:`_STARTUP_READY_TIMEOUT_SECONDS`.
        """
        del ctx  # unused; DaemonCore may later provide shared resources
        if self._started:
            return
        app = self.build_app()
        self._app = app
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        deadline = time.monotonic() + _STARTUP_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._server.started:
                self._started = True
                _log.info(f"discord-bot plugin serving on {self._host}:{self._port}")
                return
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"discord-bot plugin failed to start: {exc}")
            await asyncio.sleep(0.05)
        raise RuntimeError(f"discord-bot plugin failed to start within {_STARTUP_READY_TIMEOUT_SECONDS:g}s")

    async def stop(self) -> None:
        """Signal uvicorn to shut down and await its task.

        Safe on a plugin that was never started: a no-op if no server
        and task are held. Matches HttpAdminPlugin's shutdown contract.
        """
        self._started = False
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(Exception):
            await self._task
        self._server = None
        self._task = None
        _log.info("discord-bot plugin stopped")

    async def health(self) -> HealthReport:
        """Return the plugin's health report.

        Healthy only when the uvicorn server was observed ready at start
        and the server object is still held. Matches HttpAdminPlugin's
        "not serving" diagnostic shape so a health rollup distinguishes
        "process up, HTTP dead" from "process never started".
        """
        if not self._started or self._server is None:
            return HealthReport(status="unhealthy", detail="discord-bot not serving")
        return HealthReport(status="healthy")

    async def _handle_interactions(self, request: Request) -> JSONResponse:
        """Handle one inbound interaction POST.

        Order of checks: signature, body parse, PING, application
        command. Every failure returns a JSON response with a small
        explanatory body; success responses are bare Discord-shaped
        JSON (``{"type": N}``).
        """
        body = await request.body()
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not signature or not timestamp:
            return JSONResponse({"error": "missing signature headers"}, status_code=_HTTP_401)
        if not verify_discord_signature(
            public_key_hex=self._bot_config.public_key,
            signature_hex=signature,
            timestamp=timestamp,
            body=body,
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=_HTTP_401)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed JSON"}, status_code=_HTTP_400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "payload must be a JSON object"}, status_code=_HTTP_400)

        interaction_type = payload.get("type")
        if interaction_type == PING_TYPE:
            return JSONResponse({"type": PONG_TYPE})
        if interaction_type == APPLICATION_COMMAND_TYPE:
            return self._handle_application_command(payload)
        return JSONResponse(
            {"error": f"unsupported interaction type {interaction_type!r}"},
            status_code=_HTTP_400,
        )

    def _handle_application_command(self, payload: dict[str, Any]) -> JSONResponse:
        """Normalize, schedule, and acknowledge an application command."""
        try:
            ctx = normalize_interaction_context(payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=_HTTP_400)
        self._dispatch_scheduler(ctx)
        return JSONResponse({"type": DEFERRED_CHANNEL_MESSAGE_TYPE})

    async def _handle_notify(self, request: Request) -> JSONResponse:
        """Handle one inbound notification POST.

        Requires ``Authorization: Bearer <notify_bearer_token>``. The
        token compare is constant-time so a timing attack cannot leak
        the secret one character at a time. Body is ``{"content": str,
        "kind": str}``. Delivery to the bot's configured operator
        channel happens synchronously inside this request; a delivery
        failure returns 502 so the caller (:class:`BotDaemonNotifier`)
        can report it accurately without falling back to the webhook.
        """
        assert self._notify_bearer_token is not None  # route only registered when set
        authorization = request.headers.get("authorization", "")
        scheme, _, provided = authorization.partition(" ")
        if scheme.lower() != "bearer" or not provided:
            return JSONResponse({"error": "missing bearer token"}, status_code=_HTTP_401)
        if not hmac.compare_digest(provided, self._notify_bearer_token):
            return JSONResponse({"error": "invalid bearer token"}, status_code=_HTTP_401)

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed JSON"}, status_code=_HTTP_400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "payload must be a JSON object"}, status_code=_HTTP_400)
        content = payload.get("content")
        kind = payload.get("kind")
        if not isinstance(content, str) or not content:
            return JSONResponse({"error": "content must be a non-empty string"}, status_code=_HTTP_400)
        if not isinstance(kind, str) or not kind:
            return JSONResponse({"error": "kind must be a non-empty string"}, status_code=_HTTP_400)

        channel = ChannelRef(channel_id=self._bot_config.channel_id)
        try:
            await self._bot_client.send(channel, OutboundMessage(content=content))
        except Exception as exc:
            _log.warning(f"notify: delivery failed for kind={kind!r}: {exc}")
            return JSONResponse({"error": f"delivery failed: {exc}"}, status_code=_HTTP_502)
        _log.info(f"notify: delivered kind={kind!r} ({len(content)} chars)")
        return JSONResponse({"status": "ok"})
