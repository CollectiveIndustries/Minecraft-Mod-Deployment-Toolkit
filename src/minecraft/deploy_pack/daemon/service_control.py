# src/minecraft/deploy_pack/daemon/service_control.py

"""Docker SDK ``ServiceControl`` implementation (spec v1.3 Section 12.4 stages 15, 17).

Wraps :class:`docker_runtime.DockerRuntime`'s ``stop``/``start`` methods
behind the :class:`deploy.ServiceControl` protocol. The wrapper exists
because the protocol's shape (``stop(container: str) -> None``) differs
from the SDK's (which returns a ``StopResult`` and needs a per-container
timeout), and because the orchestrator's error handling expects
exceptions rather than ``(result, error)`` tuples.

Per-container stop timeouts
---------------------------
``stop_grace_period`` comes from the compose service and is parsed by
``config_model`` into ``InstanceConfig.stop_grace_seconds``. The control
holds a ``{container: timeout_seconds}`` mapping. A container not in the
mapping uses the compose default (10 seconds, matching ``DockerRuntime``).

Idempotency
-----------
The ``ServiceControl`` protocol requires idempotent stop semantics. A
no-op stop (``EXITED_BEFORE_STOP``) is treated as success: the container
was already stopped, and the orchestrator's lifecycle accounting tracks
it as "not actually stopped by this deployment" but not as an error.
"""

from __future__ import annotations

from collections.abc import Mapping

from LoggingCore import get_logger

from minecraft.deploy_pack.docker_runtime import (
    DockerRuntime,
    StartResult,
    StopOutcome,
    StopResult,
)
from minecraft.deploy_pack.errors import DockerRuntimeError

__all__ = ["DockerServiceControl"]


_log = get_logger(__name__)


_DEFAULT_STOP_TIMEOUT = 10


class DockerServiceControl:
    """``ServiceControl`` backed by the Docker SDK.

    Constructed once per deployment. ``runtime`` is the shared
    ``DockerRuntime`` wrapper (also used by the health probe);
    ``stop_timeouts`` is the per-container grace period map.
    """

    def __init__(
        self,
        runtime: DockerRuntime,
        stop_timeouts: Mapping[str, int] | None = None,
    ) -> None:
        """Store the runtime and stop-timeout map."""
        self._runtime = runtime
        self._stop_timeouts = dict(stop_timeouts or {})

    def stop(self, container: str) -> None:
        """Stop ``container``; raise :class:`DockerRuntimeError` on failure.

        ``EXITED_BEFORE_STOP`` (already-stopped container) is treated as
        success: the protocol requires idempotent stop semantics, and
        spec Section 4.5 says a no-op stop is not a failure.
        """
        timeout = self._stop_timeouts.get(container, _DEFAULT_STOP_TIMEOUT)
        result: StopResult = self._runtime.stop(container, timeout)
        if result.outcome == StopOutcome.STOPPED:
            _log.debug(f"service_control: {container} stopped")
            return
        if result.outcome == StopOutcome.EXITED_BEFORE_STOP:
            _log.debug(f"service_control: {container} already exited; no-op")
            return
        raise DockerRuntimeError(
            f"stop {container}: {result.error or 'unknown error'}",
        )

    def start(self, container: str) -> None:
        """Start ``container``; raise :class:`DockerRuntimeError` on failure."""
        result: StartResult = self._runtime.start(container)
        if result.success:
            _log.debug(f"service_control: {container} started")
            return
        raise DockerRuntimeError(
            f"start {container}: {result.error or 'unknown error'}",
        )
