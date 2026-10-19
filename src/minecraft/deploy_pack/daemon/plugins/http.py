# src/minecraft/deploy_pack/daemon/plugins/http.py

"""HTTP admin plugin for the Minecraft deploy daemon.

Implements ``DaemonCore.Plugin``. Wraps FastAPI + uvicorn. Endpoints:

  GET  /status    -- daemon status (health + Minecraft-specific fields)
  POST /verify    -- verify a release without side effects
  POST /publish   -- verify and record a publication receipt
  POST /deploy    -- verify then dispatch to the deployment orchestrator

All POSTs require an ``Idempotency-Key`` header. All routes require auth.
DaemonCore provides state, audit, and the daemon's aggregated health
through the ``DaemonContext``; the plugin reads them at request time.

Slice 13 changes
----------------
The plugin accepts an optional ``deploy_callable`` parameter: a
``Callable[[str], DeployOutcome]`` that runs the deployment for a
release_id. When supplied (production wiring, slice 14), a verified
``/deploy`` invokes it and dispatches on the outcome. When absent
(the default for tests and pre-slice-14 production), the endpoint
falls through to the 501 that pre-slice-13 builds returned.

The verification gate is unchanged from slice 12: chain failures yield
``RegistryChainFailedError`` (409/26); bundle/slip verification failures
yield ``VerificationBlockedError`` (409/20). The deployment dispatch
follows only after verification passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from DaemonCore import DaemonContext, HealthReport, Plugin  # noqa: F401
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from minecraft.deploy_pack.daemon.auth import Authenticator
from minecraft.deploy_pack.daemon.deploy import DeployOutcome
from minecraft.deploy_pack.daemon.errors import (
    ActivationFailedError,
    DeployCommandFailedError,
    DeploymentLockHeldError,
    MinecraftDaemonError,
    NotImplementedError,
    PostDeployVerifyFailedError,
    RecoveryFailedError,
    RegistryChainFailedError,
    VerificationBlockedError,
)
from minecraft.deploy_pack.daemon.idempotency import SQLiteIdempotency
from minecraft.deploy_pack.daemon.schemas import (
    DeployRequest,
    DeployResponse,
    ErrorResponse,
    PublishRequest,
    PublishResponse,
    StatusResponse,
    VerifyRequest,
    VerifyResponse,
)
from minecraft.deploy_pack.daemon.verify import (
    VerificationResult,
    verify_release,
)

__all__ = ["HttpAdminPlugin"]


def _request_hash(body: dict[str, Any]) -> str:
    """Return the SHA-256 of a stable JSON serialization of ``body``."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _now_utc() -> str:
    """Return the current UTC time as an ISO-8601 Z string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _outcome_to_exception(outcome: DeployOutcome) -> MinecraftDaemonError:
    """Translate a failed ``DeployOutcome`` into a typed exception.

    The mapping is fixed by :mod:`errors`:

      * 21 -> ActivationFailedError
      * 22 -> DeployCommandFailedError
      * 23 -> PostDeployVerifyFailedError
      * 24 -> RecoveryFailedError
      * 25 -> DeploymentLockHeldError

    An unknown code yields a bare ``MinecraftDaemonError`` (500 / code 1)
    so a future code addition cannot silently break the endpoint.
    """
    message = outcome.message or "deployment failed"
    stage = outcome.stage or "unknown"
    detail = f"stage={stage}"
    code = outcome.error_code
    if code == 21:
        return ActivationFailedError(message, detail=detail)
    if code == 22:
        return DeployCommandFailedError(message, detail=detail)
    if code == 23:
        return PostDeployVerifyFailedError(message, detail=detail)
    if code == 24:
        return RecoveryFailedError(message, detail=detail)
    if code == 25:
        return DeploymentLockHeldError(message, detail=detail)
    return MinecraftDaemonError(message, detail=detail)


class HttpAdminPlugin:
    """Exposes Minecraft's admin API over HTTP, on DaemonCore's lifecycle."""

    name = "http-admin"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        auth: Authenticator,
        idempotency: SQLiteIdempotency,
        slip_dir: Path,
        release_store: Path,
        registry_path: Path | None = None,
        trusted_keys_path: Path | None = None,
        deploy_callable: Callable[[str], DeployOutcome] | None = None,
    ) -> None:
        """Configure the plugin. No network activity until ``start``.

        ``registry_path`` and ``trusted_keys_path`` are both optional. When
        both are supplied, chain-walk and bundle-signature verification
        run on every POST route. When either is omitted, the plugin falls
        back to slice-11 local-only behavior.

        ``deploy_callable`` is the deployment orchestrator's entry point.
        When supplied, a verified ``/deploy`` invokes it. When omitted,
        ``/deploy`` returns 501 (matches pre-slice-13 behavior for any
        embedding that has not yet wired the orchestrator).
        """
        self._host = host
        self._port = port
        self._auth = auth
        self._idempotency = idempotency
        self._slip_dir = slip_dir
        self._release_store = release_store
        self._registry_path = registry_path
        self._trusted_keys_path = trusted_keys_path
        self._deploy_callable = deploy_callable
        self._ctx: DaemonContext | None = None
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Plugin protocol
    # ------------------------------------------------------------------

    async def start(self, ctx: DaemonContext) -> None:
        """Build the FastAPI app, spawn uvicorn, wait for ready."""
        self._ctx = ctx
        app = self._build_app()
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

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._server.started:
                self._running = True
                return
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"http-admin plugin failed to start: {exc}")
            await asyncio.sleep(0.05)
        raise RuntimeError("http-admin plugin failed to start within 5s")

    async def stop(self) -> None:
        """Signal uvicorn to shut down and await its task."""
        self._running = False
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(Exception):
            await self._task
        self._server = None
        self._task = None

    async def health(self) -> HealthReport:
        """Return unhealthy when the server is not running."""
        if not self._running or self._server is None:
            return HealthReport(status="unhealthy", detail="http-admin not serving")
        return HealthReport(status="healthy")

    # ------------------------------------------------------------------
    # Test surface
    # ------------------------------------------------------------------

    def build_app(self, ctx: DaemonContext) -> FastAPI:
        """Build the FastAPI app around ``ctx``. Called from ``start``.

        Exposed publicly so tests can drive the routes without binding a
        real socket. Production code calls it only from ``start``.
        """
        self._ctx = ctx
        return self._build_app()

    # ------------------------------------------------------------------
    # App builder
    # ------------------------------------------------------------------

    def _require_ctx(self) -> DaemonContext:
        """Return the attached context or raise if the plugin is not started."""
        if self._ctx is None:
            raise RuntimeError("http-admin plugin not started")
        return self._ctx

    def _build_app(self) -> FastAPI:
        """Construct the FastAPI application with all routes and handlers."""
        ctx = self._require_ctx()
        app = FastAPI(
            title=f"{ctx.name} daemon",
            version=ctx.version,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @app.exception_handler(MinecraftDaemonError)
        async def _error_handler(_request: Request, exc: MinecraftDaemonError) -> JSONResponse:
            """Translate MinecraftDaemonError subclasses into responses.

            Only catches our own error hierarchy; FastAPI's default 500
            handling applies to any other exception (bugs in the plugin
            code, unexpected errors from helpers). That distinction is
            intentional -- framework errors should surface in monitoring
            with their real messages and tracebacks, not be silently
            reformatted as 500/code-1 responses.
            """
            return JSONResponse(
                status_code=exc.http_status,
                content=ErrorResponse(
                    code=exc.code,
                    message=exc.message,
                    detail=exc.detail,
                ).model_dump(),
            )

        def _require_auth(request: Request) -> None:
            """Authenticate the request; raise on failure."""
            self._auth.authenticate(request)

        def _shape_verify(result: VerificationResult) -> dict[str, Any]:
            """Convert a VerificationResult into a wire-shaped dict."""
            return VerifyResponse(
                status=result.status,
                release_id=result.release_id,
                content_sha256=result.content_sha256,
                checks=result.checks,
                reasons=result.reasons,
            ).model_dump()

        def _shape_publish(result: VerificationResult, receipt_id: str | None = None) -> dict[str, Any]:
            """Convert a VerificationResult into a publish-response dict."""
            recorded_at = _now_utc() if result.status == "ok" else None
            return PublishResponse(
                status=result.status,
                release_id=result.release_id,
                content_sha256=result.content_sha256,
                receipt_id=receipt_id,
                recorded_at_utc=recorded_at,
                checks=result.checks,
                reasons=result.reasons,
            ).model_dump()

        async def _run_idempotent(
            endpoint: str,
            idempotency_key: str,
            body_dict: dict[str, Any],
            execute: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
        ) -> JSONResponse:
            """Look up ``idempotency_key``; execute and store if absent."""
            request_hash = _request_hash(body_dict)
            cached = self._idempotency.lookup(idempotency_key, endpoint, request_hash)
            if cached is not None:
                status, cached_body = cached
                return JSONResponse(content=cached_body, status_code=status)
            status, payload = await execute()
            self._idempotency.store(idempotency_key, endpoint, request_hash, status, payload)
            return JSONResponse(content=payload, status_code=status)

        async def _record_audit(record: dict[str, Any]) -> None:
            """Append to ctx.audit if attached."""
            if ctx.audit is not None:
                await ctx.audit.append(record)

        async def _record_state(key: str, value: str | None, *, json_value: dict | None = None) -> None:
            """Write to ctx.state if attached."""
            if ctx.state is None:
                return
            if json_value is not None:
                await ctx.state.set_json(key, json_value)
            elif value is not None:
                await ctx.state.set(key, value)

        def _run_verify(body: VerifyRequest) -> VerificationResult:
            """Run verification with the plugin's configured paths."""
            return verify_release(
                release_id=body.release_id,
                release_pointer=body.release_pointer,
                expected_commit_sha=body.expected_commit_sha,
                expected_slip_sha256=body.expected_slip_sha256,
                slip_dir=self._slip_dir,
                release_store=self._release_store,
                registry_path=self._registry_path,
                trusted_keys_path=self._trusted_keys_path,
            )

        # --------------------------------------------------------------
        # GET /status
        # --------------------------------------------------------------

        @app.get("/status", response_model=StatusResponse)
        async def get_status(request: Request) -> JSONResponse:
            """Return daemon status."""
            _require_auth(request)
            active_release = None
            last_registry_head = None
            last_verification = None
            if ctx.state is not None:
                active_release = await ctx.state.get("active-release")
                last_registry_head = await ctx.state.get("last-registry-head")
                last_verification = await ctx.state.get_json("last-verification")
            health = await ctx.health()
            body = StatusResponse(
                daemon_name=ctx.name,
                daemon_version=ctx.version,
                active_release=active_release,
                last_registry_head=last_registry_head,
                last_verification=last_verification,
                health={
                    "status": health.status,
                    "checks": health.checks,
                    "detail": health.detail,
                },
            ).model_dump()
            return JSONResponse(content=body, status_code=200)

        # --------------------------------------------------------------
        # POST /verify
        # --------------------------------------------------------------

        @app.post("/verify", response_model=VerifyResponse)
        async def post_verify(
            request: Request,
            body: VerifyRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
        ) -> JSONResponse:
            """Verify a release without side effects."""
            _require_auth(request)
            body_dict = body.model_dump()

            async def _execute() -> tuple[int, dict[str, Any]]:
                result = _run_verify(body)
                await _record_audit(
                    {
                        "event": "verify",
                        "release_id": result.release_id,
                        "status": result.status,
                        "reasons": result.reasons,
                    }
                )
                await _record_state(
                    "last-verification",
                    None,
                    json_value={
                        "release_id": result.release_id,
                        "content_sha256": result.content_sha256,
                        "status": result.status,
                        "reasons": result.reasons,
                        "at_utc": _now_utc(),
                    },
                )
                return (200, _shape_verify(result))

            return await _run_idempotent("verify", idempotency_key, body_dict, _execute)

        # --------------------------------------------------------------
        # POST /publish
        # --------------------------------------------------------------

        @app.post("/publish", response_model=PublishResponse)
        async def post_publish(
            request: Request,
            body: PublishRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
        ) -> JSONResponse:
            """Verify a release and record a publication receipt."""
            _require_auth(request)
            body_dict = body.model_dump()

            async def _execute() -> tuple[int, dict[str, Any]]:
                result = _run_verify(body)
                receipt = str(uuid.uuid4()) if result.status == "ok" else None
                await _record_audit(
                    {
                        "event": "publish",
                        "release_id": result.release_id,
                        "status": result.status,
                        "receipt_id": receipt,
                        "reasons": result.reasons,
                    }
                )
                await _record_state(
                    "last-verification",
                    None,
                    json_value={
                        "release_id": result.release_id,
                        "content_sha256": result.content_sha256,
                        "status": result.status,
                        "reasons": result.reasons,
                        "at_utc": _now_utc(),
                    },
                )
                return (200, _shape_publish(result, receipt))

            return await _run_idempotent("publish", idempotency_key, body_dict, _execute)

        # --------------------------------------------------------------
        # POST /deploy
        # --------------------------------------------------------------

        @app.post("/deploy", response_model=DeployResponse)
        async def post_deploy(
            request: Request,
            body: DeployRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
        ) -> JSONResponse:
            """Verify the release, then dispatch to the deployment orchestrator.

            Behavior depends on how the plugin was constructed:

            * **Verification paths absent**: 501 without running anything
              (preserves pre-slice-12 semantics).

            * **Verification paths present, deploy_callable absent**:
              verification runs, then 501 (preserves slice-12 semantics
              for callers that have wired verification but not the
              orchestrator).

            * **Both present**: verification runs; on success, the
              callable is invoked with the release_id and the outcome is
              translated into an HTTP response. Failures dispatch on the
              outcome's ``error_code`` (21/22/23/24/25).
            """
            _require_auth(request)
            del idempotency_key  # deployment orchestration is single-shot

            if self._registry_path is None or self._trusted_keys_path is None:
                raise NotImplementedError(
                    "deployment orchestration is not implemented in this daemon build",
                    detail=(f"requested release: {body.release_id or body.release_pointer}"),
                )

            result = _run_verify(body)

            if not result.chain_ok:
                raise RegistryChainFailedError(
                    f"registry chain verification failed for {result.release_id!r}",
                    detail=f"chain_failures={sorted(set(result.chain_failures))}",
                )

            if result.status != "ok":
                raise VerificationBlockedError(
                    f"release {result.release_id!r} failed verification",
                    detail="reasons=" + "; ".join(result.reasons),
                )

            await _record_audit(
                {
                    "event": "deploy_requested",
                    "release_id": result.release_id,
                    "status": "verified",
                }
            )

            if self._deploy_callable is None:
                raise NotImplementedError(
                    "deployment orchestrator is not wired in this daemon build",
                    detail=f"verified release: {result.release_id!r}",
                )

            assert result.release_id is not None
            outcome = self._deploy_callable(result.release_id)

            if not outcome.success:
                raise _outcome_to_exception(outcome)

            await _record_audit(
                {
                    "event": "deploy_completed",
                    "release_id": result.release_id,
                    "status": "ok",
                }
            )
            await _record_state("active-release", result.release_id)
            return JSONResponse(
                content=DeployResponse(
                    release_id=result.release_id,
                    completed_at_utc=_now_utc(),
                ).model_dump(),
                status_code=200,
            )

        return app
