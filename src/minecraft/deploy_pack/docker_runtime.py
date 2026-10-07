# src/minecraft/deploy_pack/docker_runtime.py

"""Docker SDK wrapper, health polling, and RCON transport (Project_Specs.md §8).

Responsibilities (§9.2):
  * wrap the official docker SDK (§8.1 - no subprocess)
  * capture container state per the §4.12 state table
  * inspect mounts for the §3.17 drift check
  * perform the §4.12 bounded wait on ``restarting`` containers
  * poll ``.State.Health.Status`` until healthy or timeout (§8.3)
  * stop / start containers (§8.10, §4.13)
  * resolve the RCON transport for an instance (§8.4) and execute
    commands over it

Non-responsibilities:
  * Deciding whether a state is fatal. §4.12 says ``paused``,
    ``removing``, ``dead``, ``missing``, and running-without-health
    are exit 3, but that decision is preflight's. This module reports
    state faithfully and raises only on daemon-unavailability, which
    is a different failure mode.
  * Retry policy, batching, and recovery sequencing. Those live in
    hooks.py (§4.7, §4.8, §4.13).
  * Rendering state for Discord. The §5.8 vocabulary is notifications.py.

Daemon-availability policy (§2.4)
----------------------------------

``DockerUnavailableError`` is raised whenever the daemon cannot be
reached. Preflight lets it propagate (exit 3). ``hooks.py`` catches it
and re-raises as ``DockerRuntimeError`` (exit 1) so a daemon that drops
mid-deployment is a runtime failure, not a configuration one.

The daemon is not pinged automatically. Preflight calls ``ping()``
explicitly; runtime code does not, and instead relies on the connection
error being raised by whatever operation hits it first.

Structure
---------

The module is organised into twelve sections with explicit banner
comments. Each section is self-contained.

    §1   Imports, dataclasses, Clock
    §2   Error classification            (§2.4)
    §3   State extraction                (§4.12)
    §4   DockerRuntime                   (§8.1-§8.3, §8.10)
    §5   Mount drift                     (§3.17)
    §6   env_file helper                 (§8.4)
    §7   RCON port resolution            (§8.4)
    §8   RCON password                   (§8.4)
    §9   RconTransport ABC + Option A
    §10  TcpRconTransport (Option B)     (§8.4)
    §11  Transport selection             (§8.4)
    §12  _RconConnection wire protocol

Aggressive decomposition: every public method is a thin orchestrator
delegating to small single-purpose helpers. Where the SDK exposes
multiple exception shapes for one operation (``stop`` has NotFound,
DockerException, not-running, and the 304 sentinel), each shape's
interpretation lives in its own helper so the error contract is
visible at a glance.

Logging
-------

Module logger is ``minecraft.deploy_pack.docker_runtime``. Every SDK
call is traced at DEBUG with the container name and, where applicable,
the state snapshot (``status``, ``running``, ``health``). State
transitions observed by ``start``/``stop`` are logged at INFO by
``hooks.py`` at the phase boundary; this module stays at DEBUG to avoid
double-reporting. ``wait_healthy`` logs one DEBUG line per poll so a
stuck container's health trajectory is reconstructable, and one INFO
when the poll settles. The single ERROR site is
:func:`_raise_if_connection`: it is the convergence point for every
"daemon unreachable" path, so logging there yields exactly one
diagnostic per connection failure regardless of which SDK call tripped
it. Configuration failures (missing secret, malformed env_file,
multiple published RCON mappings, mount drift) log at ERROR immediately
before the ``ConfigError`` raise, because callers aggregate rather than
log. The RCON password is never logged: ``load_rcon_password`` records
the path and the character length, and the transport classes record the
command text but never the credential.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from .config_model import ComposeFile, ComposeService
from .errors import ConfigError, DockerUnavailableError
from .logging_setup import get_logger

_log = get_logger(__name__)
__all__ = [
    "Clock",
    "ContainerState",
    "DockerRuntime",
    "ExecRconTransport",
    "HealthResult",
    "Mount",
    "RconTransport",
    "StartResult",
    "StopOutcome",
    "StopResult",
    "TcpRconTransport",
    "check_mount_drift",
    "load_rcon_password",
    "resolve_rcon_port",
    "select_rcon_transport",
]


# ===========================================================================
# §1  Imports, dataclasses, Clock
# ===========================================================================


class StopOutcome(Enum):
    """Result of a ``docker stop`` call (§8.10)."""

    STOPPED = "stopped"
    "A real stop was performed."
    EXITED_BEFORE_STOP = "exited_before_stop"
    'The container had already exited; the stop was a no-op.\n\n    Per §4.5 the container is not "actually stopped by this deployment",\n    is not lifecycle-touched, and is not an actual restart. It is only\n    reported in the CLI as ``exited before stop``.\n    '
    FAILED = "failed"
    "The stop raised a real error. Caller decides per §8.8."


@dataclass
class ContainerState:
    """Snapshot of a container's state (§4.12)."""

    name: str
    exists: bool
    status: str
    running: bool
    health: str | None
    raw: dict | None

    @property
    def is_running(self) -> bool:
        """Returns whether the container exists and is currently running."""
        return self.exists and self.running


@dataclass
class Mount:
    """Represents a mount with a source and destination path.

    Attributes:
        source (str): The source path of the mount.
        destination (str): The destination path where the source is mounted.
    """

    source: str
    destination: str


@dataclass
class StopResult:
    """Represents the result of stopping a container.

    Attributes:
        container (str): The name or identifier of the container.
        outcome (StopOutcome): The outcome of the stop operation.
        error (str | None): Optional error message if the operation failed.
    """

    container: str
    outcome: StopOutcome
    error: str | None = None


@dataclass
class StartResult:
    """Represents the result of starting a container.

    Attributes:
        container (str): The name or identifier of the container.
        success (bool): Whether the start operation succeeded.
        error (str | None): Optional error message if the operation failed.
    """

    container: str
    success: bool
    error: str | None = None


@dataclass
class HealthResult:
    """Represents the result of a container health check."""

    container: str
    healthy: bool
    final_health: str | None
    error: str | None = None


class Clock:
    """Wall clock plus sleep. Injected so polls are testable without sleeping."""

    def now(self) -> float:
        """Returns the current monotonic clock time in seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleeps for the given number of seconds if positive.

        Args:
            seconds: The duration to sleep, in seconds. Non-positive values are ignored.
        """
        if seconds > 0:
            time.sleep(seconds)


# ===========================================================================
# §2  Error classification (§2.4)
# ===========================================================================


_CONNECTION_NEEDLES = (
    "connection refused",
    "cannot connect to the docker daemon",
    "connection error",
    "connection aborted",
    "error while fetching server api version",
    "socket",
    "unix socket",
)


def _cause_indicates_connection_loss(cause: BaseException | None) -> bool:
    """Return True when the exception's ``__cause__`` is a requests-level connection error.

    The docker SDK wraps the underlying ``requests`` exception as
    ``__cause__`` when the transport cannot reach the daemon; the
    exception type alone (``DockerException``) is not enough to tell a
    connection failure from a 404 or a permission error.
    """
    if cause is None:
        return False
    mod = type(cause).__module__ or ""
    name = type(cause).__name__
    return mod.startswith("requests") and name in ("ConnectionError", "ConnectTimeout")


def _text_indicates_connection_loss(text: str) -> bool:
    """Return True if the lowercased text contains any connection-loss needle."""
    return any(needle in text for needle in _CONNECTION_NEEDLES)


def _is_connection_error(exc: BaseException) -> bool:
    """Best-effort detection of "daemon unreachable" from a docker error."""
    if _cause_indicates_connection_loss(exc.__cause__):
        return True
    return _text_indicates_connection_loss(str(exc).lower())


def _is_not_running_error(exc: BaseException) -> bool:
    """Detect a stop() call on a container that is no longer running."""
    if "ContainerNotRunning" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "not running" in text or "already stopped" in text


def _raise_if_connection(exc: BaseException) -> None:
    """Translate a connection error into DockerUnavailableError.

    This is the single convergence point for "daemon unreachable" across
    every SDK call. Logging at ERROR here gives one diagnostic per
    connection failure regardless of which operation tripped it, and
    guarantees the message lands in the sink even though some callers
    (hooks.py) translate the exception further before it reaches the
    operator.
    """
    if _is_connection_error(exc):
        _log.error(f"docker daemon unreachable: {exc}")
        raise DockerUnavailableError(f"Cannot connect to Docker daemon: {exc}") from exc


# ===========================================================================
# §3  State extraction (§4.12)
# ===========================================================================


def _state_from_attrs(name: str, attrs: dict) -> ContainerState:
    """Project a docker SDK attrs dict into a ContainerState."""
    state_block = attrs.get("State") or {}
    status = str(state_block.get("Status") or "unknown")
    running = bool(state_block.get("Running") or False)
    health_block = state_block.get("Health")
    health = str(health_block.get("Status")) if isinstance(health_block, dict) and health_block.get("Status") else None
    return ContainerState(name=name, exists=True, status=status, running=running, health=health, raw=attrs)


def _missing_state(name: str) -> ContainerState:
    """Return the ContainerState used when a container does not exist."""
    return ContainerState(name=name, exists=False, status="missing", running=False, health=None, raw=None)


# ===========================================================================
# §4  DockerRuntime (§8.1-§8.3, §8.10)
# ===========================================================================


def _build_default_client() -> Any:
    """Build a docker SDK client from the environment; raise DockerUnavailableError on failure."""
    _log.debug("DockerRuntime: building client from environment")
    try:
        return docker.from_env()
    except DockerException as exc:
        _log.error(f"DockerRuntime: docker.from_env failed: {exc}")
        raise DockerUnavailableError(f"Cannot connect to Docker daemon: {exc}") from exc


class DockerRuntime:
    """Thin wrapper around the Docker SDK.

    Constructed with an existing client in tests, or with ``None`` to
    build one from the environment. Constructing from the environment
    does not contact the daemon; call :meth:`ping` explicitly at
    preflight to distinguish "daemon unreachable at preflight" (exit 3)
    from "daemon lost at runtime" (exit 1).
    """

    def __init__(self, client: Any = None, clock: Clock | None = None) -> None:
        if client is None:
            client = _build_default_client()
        else:
            _log.debug("DockerRuntime: using injected client")
        self._client = client
        self._clock = clock if clock is not None else Clock()

    # ------------------------------------------------------------------
    # Ping
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Ping the daemon. Raises DockerUnavailableError on failure."""
        _log.debug("docker ping: sending")
        try:
            self._client.ping()
        except DockerException as exc:
            _raise_if_connection(exc)
            _log.error(f"docker ping failed: {exc}")
            raise DockerUnavailableError(f"Docker daemon ping failed: {exc}") from exc
        _log.debug("docker ping: OK")

    # ------------------------------------------------------------------
    # Inspect
    # ------------------------------------------------------------------

    def _inspect_get_container(self, name: str) -> Any | None:
        """Return the SDK container object, or None when it does not exist.

        Raises DockerUnavailableError on daemon loss.
        """
        try:
            return self._client.containers.get(name)
        except NotFound:
            _log.debug(f"docker inspect: {name} not found")
            return None
        except DockerException as exc:
            _raise_if_connection(exc)
            raise

    def inspect(self, name: str) -> ContainerState:
        """Return the current state of a container, or a "missing" snapshot.

        Raises DockerUnavailableError if the daemon cannot be reached.
        """
        _log.debug(f"docker inspect: {name}")
        container = self._inspect_get_container(name)
        if container is None:
            return _missing_state(name)
        try:
            attrs = container.attrs
        except NotFound:
            _log.debug(f"docker inspect: {name} disappeared between get and attrs")
            return _missing_state(name)
        except DockerException as exc:
            _raise_if_connection(exc)
            raise
        state = _state_from_attrs(name, attrs)
        _log.debug(f"docker inspect: {name} status={state.status!r} running={state.running} health={state.health!r}")
        return state

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the container's bind mounts (Type == "bind")."""
        state = self.inspect(name)
        if not state.exists or state.raw is None:
            _log.error(f"docker list_mounts: container {name!r} is not present")
            raise DockerUnavailableError(f"container {name!r} is not present")
        out: list[Mount] = []
        for m in state.raw.get("Mounts") or []:
            if not isinstance(m, dict):
                continue
            if m.get("Type") != "bind":
                continue
            src = m.get("Source")
            dst = m.get("Destination")
            if src and dst:
                out.append(Mount(source=str(src), destination=str(dst)))
        _log.debug(f"docker list_mounts: {name} has {len(out)} bind mount(s)")
        return out

    # ------------------------------------------------------------------
    # Published ports
    # ------------------------------------------------------------------

    def published_ports(self, name: str) -> dict[str, list[tuple[str, int]]]:
        """Return {container_port_proto: [(host_ip, host_port), ...]}.

        Only ports with at least one published mapping appear.
        """
        state = self.inspect(name)
        if not state.exists or state.raw is None:
            _log.error(f"docker published_ports: container {name!r} is not present")
            raise DockerUnavailableError(f"container {name!r} is not present")
        net = state.raw.get("NetworkSettings") or {}
        ports = net.get("Ports") or {}
        out: dict[str, list[tuple[str, int]]] = {}
        for key, mappings in ports.items():
            entries = _published_port_entries(mappings)
            if entries:
                out[str(key)] = entries
        _log.debug(f"docker published_ports: {name} has {len(out)} published port key(s)")
        return out

    # ------------------------------------------------------------------
    # Exec
    # ------------------------------------------------------------------

    def exec_run(self, name: str, args: list[str]) -> tuple[int, str]:
        """Run ``args`` in a container. Returns (exit_code, decoded output).

        Raises DockerUnavailableError if the daemon cannot be reached.
        """
        _log.debug(f"docker exec_run: {name} args={args}")
        container = _exec_get_container(self._client, name)
        result = _exec_invoke(container, args, name)
        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", b"")
        text = _decode_exec_output(output)
        code = int(exit_code) if exit_code is not None else -1
        _log.debug(f"docker exec_run: {name} exit={code} output_len={len(text)}")
        return (code, text)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def start(self, name: str) -> StartResult:
        """Start a container. Never raises on a start failure.

        A start failure is reported in the returned StartResult; the
        caller collects them all (§4.13).
        """
        _log.debug(f"docker start: {name}")
        container, err = _start_get_container(self._client, name)
        if err is not None:
            return err
        err = _start_invoke(container, name)
        if err is not None:
            return err
        _log.debug(f"docker start: {name} started")
        return StartResult(name, True)

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop(self, name: str, timeout: int) -> StopResult:
        """Stop a container. Distinguishes real stop from no-op (§8.10).

        Raises DockerUnavailableError on daemon loss; that is a runtime
        failure and the caller decides (hooks converts to exit 1).
        """
        _log.debug(f"docker stop: {name} timeout={timeout}s")
        container, err = _stop_get_container(self._client, name)
        if err is not None:
            return err
        attrs, err = _stop_read_state(container, name)
        if err is not None:
            return err
        state_block = attrs.get("State") or {}
        if not state_block.get("Running", False):
            _log.debug(f"docker stop: {name} already exited; no-op")
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        return _stop_invoke(container, name, timeout)

    # ------------------------------------------------------------------
    # Restarting settle
    # ------------------------------------------------------------------

    def wait_for_restarting_settle(self, names: list[str], total_timeout: float, poll_interval: float) -> dict[str, ContainerState]:
        """Bounded wait on a set of ``restarting`` containers (§4.12).

        All names are inspected once per iteration; the wait exits as
        soon as none are still ``restarting``, or the total timeout
        elapses. ``total_timeout <= 0`` disables the wait: states are
        captured once and returned.
        """
        if not names:
            return {}
        _log.debug(f"wait_for_restarting_settle: names={names} total_timeout={total_timeout}s poll_interval={poll_interval}s")
        if total_timeout <= 0:
            states = _snapshot_restarting(self, names)
            _log.debug(f"wait_for_restarting_settle: wait disabled; captured once: {[s.status for s in states.values()]}")
            return states
        deadline = self._clock.now() + total_timeout
        while True:
            states = _snapshot_restarting(self, names)
            still_restarting = _still_restarting(states)
            if not still_restarting:
                _log.debug(f"wait_for_restarting_settle: settled with statuses {[s.status for s in states.values()]}")
                return states
            now = self._clock.now()
            if now >= deadline:
                _log.debug(f"wait_for_restarting_settle: timeout reached; still restarting: {still_restarting}")
                return states
            self._clock.sleep(min(poll_interval, deadline - now))

    # ------------------------------------------------------------------
    # Health poll
    # ------------------------------------------------------------------

    def wait_healthy(self, name: str, timeout: float, poll_interval: float, preexisting_unhealthy: bool = False) -> HealthResult:
        """Poll ``.State.Health.Status`` until ``healthy`` or timeout (§8.3).

        A missing ``.State.Health`` block on a running container is a
        health failure with a distinct message. If the container was
        observed ``unhealthy`` at preflight and this poll times out,
        the returned error is prefixed with a note about the
        pre-existing state.
        """
        _log.debug(f"wait_healthy: {name} timeout={timeout:g}s poll_interval={poll_interval:g}s preexisting_unhealthy={preexisting_unhealthy}")
        deadline = self._clock.now() + timeout
        while True:
            state = self.inspect(name)
            early = _wait_healthy_early_failure(name, state)
            if early is not None:
                return early
            if state.health == "healthy":
                _log.debug(f"wait_healthy: {name} is healthy")
                return HealthResult(name, True, "healthy")
            now = self._clock.now()
            if now >= deadline:
                _log.debug(f"wait_healthy: {name} timed out (last status: {state.health!r})")
                return _wait_healthy_timeout_result(name, state, timeout, preexisting_unhealthy)
            _log.debug(f"wait_healthy: {name} poll status={state.health!r}; sleeping {min(poll_interval, deadline - now):g}s")
            self._clock.sleep(min(poll_interval, deadline - now))


# ---------------------------------------------------------------------------
# §4 helpers (module-level so they can be unit-tested without a runtime)
# ---------------------------------------------------------------------------


def _published_port_host_port(value: Any) -> int | None:
    """Coerce a ``HostPort`` value to int; return None on failure."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def _published_port_entries(mappings: Any) -> list[tuple[str, int]]:
    """Convert one port's mappings list into ``[(host_ip, host_port), ...]``."""
    if not mappings:
        return []
    entries: list[tuple[str, int]] = []
    for m in mappings:
        if not isinstance(m, dict):
            continue
        host_ip = str(m.get("HostIp") or "")
        host_port = _published_port_host_port(m.get("HostPort"))
        if host_port is None:
            continue
        entries.append((host_ip, host_port))
    return entries


def _exec_get_container(client: Any, name: str) -> Any:
    """Fetch a container for exec_run; translate not-found / daemon-loss."""
    try:
        return client.containers.get(name)
    except NotFound:
        _log.error(f"docker exec_run: container {name!r} not found")
        raise DockerUnavailableError(f"container {name!r} not found") from None
    except DockerException as exc:
        _raise_if_connection(exc)
        raise


def _exec_invoke(container: Any, args: list[str], name: str) -> Any:
    """Invoke ``container.exec_run``; translate not-found / daemon-loss."""
    try:
        return container.exec_run(args)
    except NotFound:
        _log.error(f"docker exec_run: container {name!r} not found mid-exec")
        raise DockerUnavailableError(f"container {name!r} not found") from None
    except DockerException as exc:
        _raise_if_connection(exc)
        raise


def _decode_exec_output(output: Any) -> str:
    """Decode exec output bytes to UTF-8; pass non-bytes through as str."""
    if isinstance(output, (bytes, bytearray)):
        return bytes(output).decode("utf-8", "replace")
    return str(output)


def _start_get_container(client: Any, name: str) -> tuple[Any | None, StartResult | None]:
    """Fetch a container for start; return (container, None) or (None, error_result)."""
    try:
        return client.containers.get(name), None
    except NotFound:
        _log.debug(f"docker start: container {name!r} not found")
        return None, StartResult(name, False, f"container {name!r} not found")
    except DockerException as exc:
        _raise_if_connection(exc)
        return None, StartResult(name, False, str(exc))


def _start_invoke(container: Any, name: str) -> StartResult | None:
    """Invoke ``container.start``; return None on success, error result on failure."""
    try:
        container.start()
        return None
    except NotFound:
        _log.debug(f"docker start: container {name!r} disappeared before start")
        return StartResult(name, False, f"container {name!r} not found")
    except DockerException as exc:
        _raise_if_connection(exc)
        _log.debug(f"docker start: container {name!r} start raised: {exc}")
        return StartResult(name, False, str(exc))


def _stop_get_container(client: Any, name: str) -> tuple[Any | None, StopResult | None]:
    """Fetch a container for stop; return (container, None) or (None, error_result)."""
    try:
        return client.containers.get(name), None
    except NotFound:
        _log.debug(f"docker stop: container {name!r} not found")
        return None, StopResult(name, StopOutcome.FAILED, error=f"container {name!r} not found")
    except DockerException as exc:
        _raise_if_connection(exc)
        return None, StopResult(name, StopOutcome.FAILED, error=str(exc))


def _stop_read_state(container: Any, name: str) -> tuple[dict | None, StopResult | None]:
    """Read container attrs for stop; return (attrs, None) or (None, error_result)."""
    try:
        return container.attrs, None
    except NotFound:
        _log.debug(f"docker stop: container {name!r} disappeared before state read")
        return None, StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
    except DockerException as exc:
        _raise_if_connection(exc)
        return None, StopResult(name, StopOutcome.FAILED, error=str(exc))


def _stop_invoke(container: Any, name: str, timeout: int) -> StopResult:
    """Invoke ``container.stop``; classify success, no-op, and real failure."""
    try:
        result = container.stop(timeout=timeout)
    except NotFound:
        _log.debug(f"docker stop: {name} disappeared during stop; treating as exited")
        return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
    except DockerException as exc:
        _raise_if_connection(exc)
        if _is_not_running_error(exc):
            _log.debug(f"docker stop: {name} already stopped (SDK reported not-running): {exc}")
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        _log.debug(f"docker stop: {name} stop raised: {exc}")
        return StopResult(name, StopOutcome.FAILED, error=str(exc))
    if result == 304:
        _log.debug(f"docker stop: {name} returned 304 (not modified); treating as exited")
        return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
    _log.debug(f"docker stop: {name} stopped")
    return StopResult(name, StopOutcome.STOPPED)


def _snapshot_restarting(runtime: DockerRuntime, names: list[str]) -> dict[str, ContainerState]:
    """Inspect each name and return {name: state}."""
    return {n: runtime.inspect(n) for n in names}


def _still_restarting(states: dict[str, ContainerState]) -> list[str]:
    """Return the names whose status is ``restarting``."""
    return [n for n, s in states.items() if s.status == "restarting"]


def _wait_healthy_early_failure(name: str, state: ContainerState) -> HealthResult | None:
    """Return a HealthResult for a definitively-failed state, else None.

    Covers the three "this will never become healthy" cases: container
    is missing, is not running, or is running without ``.State.Health``.
    """
    if not state.exists:
        _log.debug(f"wait_healthy: {name} is missing")
        return HealthResult(name, False, None, error=f"{name}: container is missing")
    if not state.running:
        _log.debug(f"wait_healthy: {name} is not running (status={state.status!r})")
        return HealthResult(name, False, state.health, error=f"{name}: container is not running")
    if state.health is None:
        _log.debug(f"wait_healthy: {name} is running without .State.Health")
        return HealthResult(
            name,
            False,
            None,
            error=f"{name}: running container does not expose .State.Health; the healthcheck may not have been created with the container",
        )
    return None


def _wait_healthy_timeout_result(
    name: str,
    state: ContainerState,
    timeout: float,
    preexisting_unhealthy: bool,
) -> HealthResult:
    """Build the HealthResult for a timed-out poll, with the pre-existing note."""
    msg = f"{name}: health poll timed out after {timeout:g}s (last status: {state.health!r})"
    if preexisting_unhealthy:
        msg = f"{name}: container was unhealthy before the deployment; {msg}"
    return HealthResult(name, False, state.health, error=msg)


# ===========================================================================
# §5  Mount drift (§3.17)
# ===========================================================================


def _realpath_pair(a: Path | str, b: Path | str, container_name: str, target: str, logger: Any) -> tuple[str, str]:
    """Return the pair of realpath'd, trailing-slash-stripped paths.

    Raises ConfigError when realpath fails on either side (broken
    symlink, permission denied) per §3.17.
    """
    try:
        ra = os.path.realpath(str(a), strict=True).rstrip("/")
        rb = os.path.realpath(str(b), strict=True).rstrip("/")
    except OSError as exc:
        logger.error(f"check_mount_drift: {container_name!r}: realpath failed for {target!r}: {exc}")
        raise ConfigError(f"container {container_name!r}: realpath failed for {target!r}: {exc}") from exc
    return ra, rb


def check_mount_drift(
    runtime: DockerRuntime,
    container_name: str,
    expected: list[tuple[Path, str]],
    logger: Any = None,
) -> None:
    """Raise ConfigError if a required mount is missing or drifted (§3.17).

    ``expected`` is a list of (resolved_host_source, container_target).
    Callers decide which targets to check: ``/data`` always, ``/data/mods``
    only when the server scope will touch ``mods_dir``. Extra mounts on
    the container are permitted and ignored - the check is one-directional.

    Realpath failures on either side raise ConfigError, per §3.17.
    """
    if logger is None:
        logger = _log
    logger.debug(f"check_mount_drift: {container_name} expecting {len(expected)} target(s): {[t for _, t in expected]}")
    try:
        mounts = runtime.list_mounts(container_name)
    except DockerUnavailableError:
        raise
    by_dest = {m.destination: m.source for m in mounts}
    for host_source, target in expected:
        actual = by_dest.get(target)
        if actual is None:
            logger.error(f"check_mount_drift: {container_name!r}: no bind mount at {target!r}")
            raise ConfigError(f"container {container_name!r}: no bind mount at {target!r} (compose declares it, the running container does not)")
        ra, rb = _realpath_pair(host_source, actual, container_name, target, logger)
        if ra != rb:
            logger.error(f"check_mount_drift: {container_name!r}: mount drift at {target!r}: compose={ra} container={rb}")
            raise ConfigError(f"container {container_name!r}: mount drift at {target!r}: compose={ra} container={rb}")
    logger.debug(f"check_mount_drift: {container_name} OK")


# ===========================================================================
# §6  env_file helper (§8.4)
# ===========================================================================


def _read_env_file_value(path: Path, key: str) -> str | None:
    """Return ``key``'s value from a dotenv-style file, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.error(f"_read_env_file_value: could not read env_file {path}: {exc}")
        raise ConfigError(f"Could not read env_file {path}: {exc}") from exc
    value: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            value = v.strip()
    _log.debug(f"_read_env_file_value: {path} key={key!r} found={value is not None}")
    return value


# ===========================================================================
# §7  RCON port resolution (§8.4)
# ===========================================================================


def _resolve_port_from_environment(service: ComposeService, logger: Any) -> int | None:
    """Return ``RCON_PORT`` from ``services.<svc>.environment``, or None.

    Raises ConfigError on a non-integer value.
    """
    env_value = service.environment.get("RCON_PORT")
    if env_value is None:
        return None
    try:
        port = int(env_value)
    except (TypeError, ValueError) as exc:
        logger.error(f"resolve_rcon_port: [{service.name}] environment.RCON_PORT={env_value!r} is not an integer")
        raise ConfigError(f"services.{service.name}.environment.RCON_PORT={env_value!r} is not an integer") from exc
    logger.debug(f"resolve_rcon_port: [{service.name}] from environment -> {port}")
    return port


def _find_port_in_env_files(service: ComposeService, logger: Any) -> str | None:
    """Scan ``env_file`` entries left-to-right for ``RCON_PORT``; last wins.

    A missing env_file is an error only when ``RCON_PORT`` has not yet
    been found (per §8.4).
    """
    found: str | None = None
    for env_file in service.env_files:
        if not env_file.is_file():
            if found is None:
                logger.error(f"resolve_rcon_port: [{service.name}] env_file is missing: {env_file}")
                raise ConfigError(f"services.{service.name}.env_file is missing: {env_file}")
            continue
        value = _read_env_file_value(env_file, "RCON_PORT")
        if value is not None:
            found = value
    return found


def _resolve_port_from_env_files_result(service: ComposeService, found: str, logger: Any) -> int:
    """Coerce a string env_file value to int, raising ConfigError on failure."""
    try:
        port = int(found)
    except (TypeError, ValueError) as exc:
        logger.error(f"resolve_rcon_port: [{service.name}] RCON_PORT={found!r} in env_file is not an integer")
        raise ConfigError(f"RCON_PORT={found!r} in env_file is not an integer") from exc
    logger.debug(f"resolve_rcon_port: [{service.name}] from env_file -> {port}")
    return port


def resolve_rcon_port(service: ComposeService, compose: ComposeFile, logger: Any = None) -> int:
    """Resolve ``RCON_PORT`` for a service (§8.4).

    Priority: ``services.<svc>.environment.RCON_PORT``, then the
    service's ``env_file`` entries left-to-right (last wins), then 25575.

    A missing ``env_file`` is an error only when the file would be needed
    to resolve ``RCON_PORT`` (i.e. it hasn't been found yet).
    """
    if logger is None:
        logger = _log
    from_environment = _resolve_port_from_environment(service, logger)
    if from_environment is not None:
        return from_environment
    found = _find_port_in_env_files(service, logger)
    if found is not None:
        return _resolve_port_from_env_files_result(service, found, logger)
    logger.debug(f"resolve_rcon_port: [{service.name}] default -> 25575")
    return 25575


# ===========================================================================
# §8  RCON password (§8.4)
# ===========================================================================


def _require_rcon_secret_declared(service: ComposeService, logger: Any) -> None:
    """Raise ConfigError if the service does not reference ``rcon_password``."""
    if "rcon_password" not in service.secrets:
        logger.error(f"load_rcon_password: service {service.name!r} does not declare the rcon_password secret")
        raise ConfigError(f"services.{service.name}: RCON is required but the service does not declare the rcon_password secret")


def _require_secret_file_entry(compose: ComposeFile, service: ComposeService, logger: Any) -> Path:
    """Return the ``secrets.rcon_password.file`` path or raise ConfigError."""
    path = compose.secret_files.get("rcon_password")
    if path is None:
        logger.error(f"load_rcon_password: service {service.name!r} references rcon_password but secrets.rcon_password.file is not declared")
        raise ConfigError(f"services.{service.name}: rcon_password is referenced but secrets.rcon_password.file is not declared")
    return path


def _require_secret_file_exists(path: Path, logger: Any) -> None:
    """Raise ConfigError if the password file is not on disk."""
    if not path.is_file():
        logger.error(f"load_rcon_password: rcon_password secret file not found: {path}")
        raise ConfigError(f"rcon_password secret file not found: {path}")


def _read_secret_file(path: Path, logger: Any) -> str:
    """Read and rstrip the password file; raise ConfigError on OSError."""
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        logger.error(f"load_rcon_password: could not read {path}: {exc}")
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def load_rcon_password(compose: ComposeFile, service: ComposeService, logger: Any = None) -> str:
    """Read the RCON password from ``secrets.rcon_password.file`` (§8.4).

    Raises ConfigError if the service doesn't reference ``rcon_password``,
    if the compose file doesn't declare ``secrets.rcon_password.file``,
    or if the file cannot be read.

    The password content is never logged; only the resolved path and the
    character count.
    """
    if logger is None:
        logger = _log
    _require_rcon_secret_declared(service, logger)
    path = _require_secret_file_entry(compose, service, logger)
    _require_secret_file_exists(path, logger)
    password = _read_secret_file(path, logger)
    logger.debug(f"load_rcon_password: read {len(password)}-char password from {path}")
    return password


# ===========================================================================
# §9  RconTransport ABC + ExecRconTransport (Option A)
# ===========================================================================


class RconTransport(ABC):
    """Executes RCON commands against an instance.

    ``command`` is a single space-separated string: ``"say hello world"``,
    ``"list"``, ``"reload"``. The transport is responsible for turning
    that into whatever the underlying mechanism needs.

    ``execute`` returns ``(success, response_text)``. It never raises on
    an RCON-level failure; only DockerUnavailableError can escape, and
    only from the exec-backed implementation.
    """

    @abstractmethod
    def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Executes a command and returns whether it succeeded and its output.

        Args:
            command: The command to execute.
            timeout: Maximum time in seconds to wait for the command.

        Returns:
            A tuple of (success, output), where success indicates whether the
            command completed successfully and output is the captured result.
        """
        ...

    @abstractmethod
    def describe(self) -> str:
        """Human-readable description, for logging."""


class ExecRconTransport(RconTransport):
    """Option A: ``container.exec_run(["rcon-cli", ...])`` (§8.4)."""

    def __init__(self, runtime: DockerRuntime, container_name: str) -> None:
        self._runtime = runtime
        self._container_name = container_name

    def describe(self) -> str:
        """Returns a description of the object."""
        return f"exec(rcon-cli) on {self._container_name}"

    def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Executes a command."""
        _log.debug(f"ExecRconTransport: {self._container_name} <- {command!r}")
        args = ["rcon-cli", *command.split(" ", 1)]
        try:
            exit_code, output = self._runtime.exec_run(self._container_name, args)
        except DockerUnavailableError:
            raise
        except Exception as exc:
            _log.debug(f"ExecRconTransport: {self._container_name} exec failed: {exc}")
            return (False, f"exec failed: {exc}")
        ok = exit_code == 0
        _log.debug(f"ExecRconTransport: {self._container_name} exit={exit_code} ok={ok}")
        return (ok, output.strip())


# ===========================================================================
# §10  TcpRconTransport (Option B)
# ===========================================================================


def _default_sock_factory(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def _tcp_connect(
    sock_factory: Callable[[str, int, float], Any],
    host: str,
    port: int,
    timeout: float,
) -> tuple[Any | None, str | None]:
    """Open a TCP connection; return (sock, None) or (None, error_message)."""
    try:
        return sock_factory(host, port, timeout), None
    except OSError as exc:
        _log.debug(f"TcpRconTransport: {host}:{port} connect failed: {exc}")
        return None, f"connect failed: {exc}"


def _tcp_login(conn: _RconConnection, password: str, host: str, port: int) -> str | None:
    """Perform the RCON login handshake; return None on success, error message on failure."""
    try:
        conn.login(password)
        return None
    except (OSError, ValueError) as exc:
        _log.debug(f"TcpRconTransport: {host}:{port} login failed: {exc}")
        return f"RCON login failed: {exc}"


def _tcp_command(conn: _RconConnection, command: str, host: str, port: int) -> tuple[str | None, str | None]:
    """Send a command; return (payload, None) or (None, error_message)."""
    try:
        return conn.command(command), None
    except (OSError, ValueError) as exc:
        _log.debug(f"TcpRconTransport: {host}:{port} command failed: {exc}")
        return None, f"RCON command failed: {exc}"


class TcpRconTransport(RconTransport):
    """Option B: direct TCP RCON (§8.4)."""

    def __init__(self, host: str, port: int, password: str, sock_factory: Callable[[str, int, float], Any] | None = None) -> None:
        self.host = host
        self.port = port
        self._password = password
        self._sock_factory = sock_factory or _default_sock_factory

    def describe(self) -> str:
        """Returns a description of the object."""
        return f"tcp://{self.host}:{self.port}"

    def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Executes a command."""
        _log.debug(f"TcpRconTransport: {self.host}:{self.port} <- {command!r}")
        sock, err = _tcp_connect(self._sock_factory, self.host, self.port, timeout)
        if err is not None:
            return (False, err)
        try:
            with contextlib.suppress(OSError):
                sock.settimeout(timeout)
            conn = _RconConnection(sock, timeout)
            err = _tcp_login(conn, self._password, self.host, self.port)
            if err is not None:
                return (False, err)
            payload, err = _tcp_command(conn, command, self.host, self.port)
            if err is not None:
                return (False, err)
            _log.debug(f"TcpRconTransport: {self.host}:{self.port} reply_len={len(payload)}")
            return (True, payload)
        finally:
            with contextlib.suppress(OSError):
                sock.close()


# ===========================================================================
# §11  Transport selection (§8.4)
# ===========================================================================


def _select_remote_transport(
    runtime: DockerRuntime,
    service: ComposeService,
    compose: ComposeFile,
    container_name: str,
    rcon_host: str,
    rcon_port: int,
    mappings: list[tuple[str, int]],
    logger: Any,
) -> RconTransport:
    """Remote rcon_host path: exactly one published mapping required."""
    if not mappings:
        logger.error(f"select_rcon_transport: {container_name}: rcon_host is set but port {rcon_port}/tcp is not published")
        raise ConfigError(f"container {container_name!r}: [docker].rcon_host is set but RCON port {rcon_port}/tcp is not published")
    if len(mappings) > 1:
        logger.error(f"select_rcon_transport: {container_name}: port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")
        raise ConfigError(f"container {container_name!r}: RCON port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")
    _ip, host_port = mappings[0]
    password = load_rcon_password(compose, service, logger)
    transport = TcpRconTransport(host=rcon_host, port=host_port, password=password)
    logger.info(f"select_rcon_transport: [{container_name}] chose {transport.describe()} (remote rcon_host)")
    return transport


def _select_local_transport(
    runtime: DockerRuntime,
    service: ComposeService,
    compose: ComposeFile,
    container_name: str,
    rcon_port: int,
    mappings: list[tuple[str, int]],
    logger: Any,
) -> RconTransport:
    """Same-host path: one published mapping → TCP; zero → exec; >1 → error."""
    if len(mappings) == 1:
        _ip, host_port = mappings[0]
        password = load_rcon_password(compose, service, logger)
        transport = TcpRconTransport(host="127.0.0.1", port=host_port, password=password)
        logger.info(f"select_rcon_transport: [{container_name}] chose {transport.describe()} (published port)")
        return transport
    if not mappings:
        transport = ExecRconTransport(runtime, container_name)
        logger.info(f"select_rcon_transport: [{container_name}] chose {transport.describe()} (no published port)")
        return transport
    logger.error(f"select_rcon_transport: {container_name}: port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")
    raise ConfigError(f"container {container_name!r}: RCON port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")


def select_rcon_transport(
    runtime: DockerRuntime,
    service: ComposeService,
    compose: ComposeFile,
    container_name: str,
    rcon_host: str | None,
    logger: Any = None,
) -> RconTransport:
    """Choose Option A or Option B per §8.4.

    Raises ConfigError on any configuration problem (missing secret file,
    multiple published mappings, remote rcon_host without a published
    mapping, etc.). No automatic fallback between transports.
    """
    if logger is None:
        logger = _log
    logger.debug(f"select_rcon_transport: {container_name} rcon_host={rcon_host!r}")
    rcon_port = resolve_rcon_port(service, compose, logger)
    published = runtime.published_ports(container_name)
    mappings = published.get(f"{rcon_port}/tcp") or []
    logger.debug(f"select_rcon_transport: {container_name} port={rcon_port}/tcp mappings={len(mappings)}")
    if rcon_host:
        return _select_remote_transport(runtime, service, compose, container_name, rcon_host, rcon_port, mappings, logger)
    return _select_local_transport(runtime, service, compose, container_name, rcon_port, mappings, logger)


# ===========================================================================
# §12  _RconConnection wire protocol
# ===========================================================================


_AUTH_TYPE = 3
_AUTH_RESPONSE_TYPE = 2
_COMMAND_TYPE = 2
_RESPONSE_TYPE = 0


class _RconConnection:
    """Minimal Minecraft RCON client over a stream socket.

    One instance per TCP connection. Packet format::

        int32 size       length of the body that follows
        int32 request_id
        int32 type
        bytes payload    UTF-8, NUL-terminated
        bytes 0x00 0x00
    """

    def __init__(self, sock: Any, timeout: float) -> None:
        self._sock = sock
        self._timeout = timeout
        self._next_id = 0

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, request_id: int, type_: int, payload: str) -> None:
        payload_b = payload.encode("utf-8")
        body = struct.pack("<ii", request_id, type_) + payload_b + b"\x00\x00"
        packet = struct.pack("<i", len(body)) + body
        self._sock.sendall(packet)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ValueError("RCON connection closed")
            buf += chunk
        return bytes(buf)

    def _recv(self) -> tuple[int, int, str]:
        size_bytes = self._read_exact(4)
        (size,) = struct.unpack("<i", size_bytes)
        if size < 10:
            raise ValueError(f"invalid RCON packet size: {size}")
        body = self._read_exact(size)
        request_id, type_ = struct.unpack("<ii", body[:8])
        payload_bytes = body[8:-2]
        payload = payload_bytes.decode("utf-8", "replace")
        return (request_id, type_, payload)

    def login(self, password: str) -> None:
        """Logs in to the service.

        The password is never logged.
        """
        rid = self._alloc_id()
        self._send(rid, _AUTH_TYPE, password)
        resp_rid, _resp_type, _payload = self._recv()
        if resp_rid == -1:
            _log.debug("_RconConnection.login: authentication rejected")
            raise ValueError("authentication rejected")
        _log.debug("_RconConnection.login: authentication accepted")

    def command(self, command: str) -> str:
        """Placeholder for command-related functionality."""
        rid = self._alloc_id()
        self._send(rid, _COMMAND_TYPE, command)
        _resp_rid, _resp_type, payload = self._recv()
        return payload
