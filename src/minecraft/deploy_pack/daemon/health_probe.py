# src/minecraft/deploy_pack/daemon/health_probe.py

"""Docker SDK + RCON ``HealthProbe`` implementation (spec v1.3 Section 12.9).

Consumes the shared Docker SDK client for container state and logs, and
a pre-resolved ``{container: RconTransport}`` map for the RCON ``list``
checks. The RCON transports are constructed once by
:mod:`deploy_runtime` using
:func:`docker_runtime.select_rcon_transport`.

Missing containers
------------------
Every method treats a missing container as its "not applicable" value:
``is_running`` -> ``False``, ``restart_count`` -> ``0``, ``recent_logs``
-> ``""``. The health check's first step classifies a missing container
as a failure with a clear message; the probe itself never raises on
absence. This matches the ``HealthProbe`` protocol contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from docker.errors import DockerException, NotFound
from LoggingCore import get_logger

from minecraft.deploy_pack.docker_runtime import RconTransport

__all__ = ["DockerHealthProbe"]


_log = get_logger(__name__)


_LOG_TAIL_LINES = 500


class DockerHealthProbe:
    """``HealthProbe`` backed by a Docker SDK client + RCON transports."""

    def __init__(
        self,
        client: Any,
        rcon_transports: Mapping[str, RconTransport] | None = None,
        rcon_errors: Mapping[str, str] | None = None,
    ) -> None:
        """Store the SDK client, RCON transports, and RCON setup errors.

        ``rcon_transports`` maps container name to the selected transport.
        ``rcon_errors`` maps container name to the reason a transport
        could not be selected (used to construct a meaningful RCON
        failure when the health check reaches that step).
        """
        self._client = client
        self._transports = dict(rcon_transports or {})
        self._errors = dict(rcon_errors or {})

    def _get_container(self, container: str) -> Any | None:
        """Return the SDK container object, or None when absent."""
        try:
            return self._client.containers.get(container)
        except NotFound:
            return None
        except DockerException as exc:
            _log.debug(f"health_probe: get {container!r} failed: {exc}")
            return None

    def is_running(self, container: str) -> bool:
        """Return True iff ``container`` exists and is running."""
        obj = self._get_container(container)
        if obj is None:
            return False
        try:
            return bool(obj.attrs.get("State", {}).get("Running", False))
        except DockerException as exc:
            _log.debug(f"health_probe: read state for {container!r} failed: {exc}")
            return False

    def restart_count(self, container: str) -> int:
        """Return the container's cumulative restart count."""
        obj = self._get_container(container)
        if obj is None:
            return 0
        try:
            return int(obj.attrs.get("RestartCount", 0))
        except (DockerException, TypeError, ValueError) as exc:
            _log.debug(f"health_probe: read restart count for {container!r} failed: {exc}")
            return 0

    def recent_logs(self, container: str) -> str:
        """Return the tail of the container's logs as text."""
        obj = self._get_container(container)
        if obj is None:
            return ""
        try:
            raw = obj.logs(tail=_LOG_TAIL_LINES)
        except DockerException as exc:
            _log.debug(f"health_probe: read logs for {container!r} failed: {exc}")
            return ""
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", "replace")
        return str(raw)

    def rcon_list(self, container: str) -> tuple[bool, str]:
        """Run ``list`` over RCON; return ``(success, response_or_error)``."""
        transport = self._transports.get(container)
        if transport is None:
            reason = self._errors.get(container, "no RCON transport configured")
            return (False, f"no RCON transport for {container!r}: {reason}")
        try:
            return transport.execute("list")
        except Exception as exc:
            _log.debug(f"health_probe: rcon list for {container!r} raised: {exc}")
            return (False, str(exc))
