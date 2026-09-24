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


def _is_connection_error(exc: BaseException) -> bool:
    """Best-effort detection of "daemon unreachable" from a docker error."""
    cause = exc.__cause__
    if cause is not None:
        mod = type(cause).__module__ or ""
        name = type(cause).__name__
        if mod.startswith("requests") and name in ("ConnectionError", "ConnectTimeout"):
            return True
    text = str(exc).lower()
    for needle in (
        "connection refused",
        "cannot connect to the docker daemon",
        "connection error",
        "connection aborted",
        "error while fetching server api version",
        "socket",
        "unix socket",
    ):
        if needle in text:
            return True
    return False


def _is_not_running_error(exc: BaseException) -> bool:
    """Detect a stop() call on a container that is no longer running."""
    if "ContainerNotRunning" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "not running" in text or "already stopped" in text


def _raise_if_connection(exc: BaseException) -> None:
    """Translate a connection error into DockerUnavailableError."""
    if _is_connection_error(exc):
        raise DockerUnavailableError(f"Cannot connect to Docker daemon: {exc}") from exc


def _state_from_attrs(name: str, attrs: dict) -> ContainerState:
    state_block = attrs.get("State") or {}
    status = str(state_block.get("Status") or "unknown")
    running = bool(state_block.get("Running") or False)
    health_block = state_block.get("Health")
    health = str(health_block.get("Status")) if isinstance(health_block, dict) and health_block.get("Status") else None
    return ContainerState(name=name, exists=True, status=status, running=running, health=health, raw=attrs)


def _missing_state(name: str) -> ContainerState:
    return ContainerState(name=name, exists=False, status="missing", running=False, health=None, raw=None)


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
            try:
                client = docker.from_env()
            except DockerException as exc:
                raise DockerUnavailableError(f"Cannot connect to Docker daemon: {exc}") from exc
        self._client = client
        self._clock = clock if clock is not None else Clock()

    def ping(self) -> None:
        """Ping the daemon. Raises DockerUnavailableError on failure."""
        try:
            self._client.ping()
        except DockerException as exc:
            _raise_if_connection(exc)
            raise DockerUnavailableError(f"Docker daemon ping failed: {exc}") from exc

    def inspect(self, name: str) -> ContainerState:
        """Return the current state of a container, or a "missing" snapshot.

        Raises DockerUnavailableError if the daemon cannot be reached.
        """
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return _missing_state(name)
        except DockerException as exc:
            _raise_if_connection(exc)
            raise
        try:
            attrs = container.attrs
        except NotFound:
            return _missing_state(name)
        except DockerException as exc:
            _raise_if_connection(exc)
            raise
        return _state_from_attrs(name, attrs)

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the container's bind mounts (Type == "bind")."""
        state = self.inspect(name)
        if not state.exists or state.raw is None:
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
        return out

    def published_ports(self, name: str) -> dict[str, list[tuple[str, int]]]:
        """Return {container_port_proto: [(host_ip, host_port), ...]}.

        Only ports with at least one published mapping appear.
        """
        state = self.inspect(name)
        if not state.exists or state.raw is None:
            raise DockerUnavailableError(f"container {name!r} is not present")
        net = state.raw.get("NetworkSettings") or {}
        ports = net.get("Ports") or {}
        out: dict[str, list[tuple[str, int]]] = {}
        for key, mappings in ports.items():
            if not mappings:
                continue
            entries: list[tuple[str, int]] = []
            for m in mappings:
                if not isinstance(m, dict):
                    continue
                host_ip = str(m.get("HostIp") or "")
                try:
                    host_port = int(m.get("HostPort") or 0)
                except (TypeError, ValueError):
                    continue
                entries.append((host_ip, host_port))
            if entries:
                out[str(key)] = entries
        return out

    def exec_run(self, name: str, args: list[str]) -> tuple[int, str]:
        """Run ``args`` in a container. Returns (exit_code, decoded output).

        Raises DockerUnavailableError if the daemon cannot be reached.
        """
        try:
            container = self._client.containers.get(name)
        except NotFound:
            raise DockerUnavailableError(f"container {name!r} not found") from None
        except DockerException as exc:
            _raise_if_connection(exc)
            raise
        try:
            result = container.exec_run(args)
        except NotFound:
            raise DockerUnavailableError(f"container {name!r} not found") from None
        except DockerException as exc:
            _raise_if_connection(exc)
            raise
        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", b"")
        if isinstance(output, (bytes, bytearray)):
            text = bytes(output).decode("utf-8", "replace")
        else:
            text = str(output)
        return (int(exit_code) if exit_code is not None else -1, text)

    def start(self, name: str) -> StartResult:
        """Start a container. Never raises on a start failure.

        A start failure is reported in the returned StartResult; the
        caller collects them all (§4.13).
        """
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return StartResult(name, False, f"container {name!r} not found")
        except DockerException as exc:
            _raise_if_connection(exc)
            return StartResult(name, False, str(exc))
        try:
            container.start()
        except NotFound:
            return StartResult(name, False, f"container {name!r} not found")
        except DockerException as exc:
            _raise_if_connection(exc)
            return StartResult(name, False, str(exc))
        return StartResult(name, True)

    def stop(self, name: str, timeout: int) -> StopResult:
        """Stop a container. Distinguishes real stop from no-op (§8.10).

        Raises DockerUnavailableError on daemon loss; that is a runtime
        failure and the caller decides (hooks converts to exit 1).
        """
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return StopResult(name, StopOutcome.FAILED, error=f"container {name!r} not found")
        except DockerException as exc:
            _raise_if_connection(exc)
            return StopResult(name, StopOutcome.FAILED, error=str(exc))
        try:
            attrs = container.attrs
        except NotFound:
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        except DockerException as exc:
            _raise_if_connection(exc)
            return StopResult(name, StopOutcome.FAILED, error=str(exc))
        state_block = attrs.get("State") or {}
        if not state_block.get("Running", False):
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        try:
            result = container.stop(timeout=timeout)
        except NotFound:
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        except DockerException as exc:
            _raise_if_connection(exc)
            if _is_not_running_error(exc):
                return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
            return StopResult(name, StopOutcome.FAILED, error=str(exc))
        if result == 304:
            return StopResult(name, StopOutcome.EXITED_BEFORE_STOP)
        return StopResult(name, StopOutcome.STOPPED)

    def wait_for_restarting_settle(self, names: list[str], total_timeout: float, poll_interval: float) -> dict[str, ContainerState]:
        """Bounded wait on a set of ``restarting`` containers (§4.12).

        All names are inspected once per iteration; the wait exits as
        soon as none are still ``restarting``, or the total timeout
        elapses. ``total_timeout <= 0`` disables the wait: states are
        captured once and returned.
        """
        if not names:
            return {}
        if total_timeout <= 0:
            return {n: self.inspect(n) for n in names}
        deadline = self._clock.now() + total_timeout
        while True:
            states = {n: self.inspect(n) for n in names}
            if not any(s.status == "restarting" for s in states.values()):
                return states
            now = self._clock.now()
            if now >= deadline:
                return states
            self._clock.sleep(min(poll_interval, deadline - now))

    def wait_healthy(self, name: str, timeout: float, poll_interval: float, preexisting_unhealthy: bool = False) -> HealthResult:
        """Poll ``.State.Health.Status`` until ``healthy`` or timeout (§8.3).

        A missing ``.State.Health`` block on a running container is a
        health failure with a distinct message. If the container was
        observed ``unhealthy`` at preflight and this poll times out,
        the returned error is prefixed with a note about the
        pre-existing state.
        """
        deadline = self._clock.now() + timeout
        while True:
            state = self.inspect(name)
            if not state.exists:
                return HealthResult(name, False, None, error=f"{name}: container is missing")
            if not state.running:
                return HealthResult(name, False, state.health, error=f"{name}: container is not running")
            if state.health is None:
                return HealthResult(
                    name,
                    False,
                    None,
                    error=f"{name}: running container does not expose .State.Health; the healthcheck may not have been created with the container",
                )
            if state.health == "healthy":
                return HealthResult(name, True, "healthy")
            now = self._clock.now()
            if now >= deadline:
                msg = f"{name}: health poll timed out after {timeout:g}s (last status: {state.health!r})"
                if preexisting_unhealthy:
                    msg = f"{name}: container was unhealthy before the deployment; {msg}"
                return HealthResult(name, False, state.health, error=msg)
            self._clock.sleep(min(poll_interval, deadline - now))


def check_mount_drift(runtime: DockerRuntime, container_name: str, expected: list[tuple[Path, str]]) -> None:
    """Raise ConfigError if a required mount is missing or drifted (§3.17).

    ``expected`` is a list of (resolved_host_source, container_target).
    Callers decide which targets to check: ``/data`` always, ``/data/mods``
    only when the server scope will touch ``mods_dir``. Extra mounts on
    the container are permitted and ignored - the check is one-directional.

    Realpath failures on either side raise ConfigError, per §3.17.
    """
    try:
        mounts = runtime.list_mounts(container_name)
    except DockerUnavailableError:
        raise
    by_dest = {m.destination: m.source for m in mounts}
    for host_source, target in expected:
        actual = by_dest.get(target)
        if actual is None:
            raise ConfigError(f"container {container_name!r}: no bind mount at {target!r} (compose declares it, the running container does not)")
        try:
            a = os.path.realpath(str(host_source), strict=True).rstrip("/")
            b = os.path.realpath(str(actual), strict=True).rstrip("/")
        except OSError as exc:
            raise ConfigError(f"container {container_name!r}: realpath failed for {target!r}: {exc}") from exc
        if a != b:
            raise ConfigError(f"container {container_name!r}: mount drift at {target!r}: compose={a} container={b}")


def _read_env_file_value(path: Path, key: str) -> str | None:
    """Return ``key``'s value from a dotenv-style file, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read env_file {path}: {exc}") from exc
    value: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            value = v.strip()
    return value


def resolve_rcon_port(service: ComposeService, compose: ComposeFile) -> int:
    """Resolve ``RCON_PORT`` for a service (§8.4).

    Priority: ``services.<svc>.environment.RCON_PORT``, then the
    service's ``env_file`` entries left-to-right (last wins), then 25575.

    A missing ``env_file`` is an error only when the file would be needed
    to resolve ``RCON_PORT`` (i.e. it hasn't been found yet).
    """
    env_value = service.environment.get("RCON_PORT")
    if env_value is not None:
        try:
            return int(env_value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"services.{service.name}.environment.RCON_PORT={env_value!r} is not an integer") from exc
    found: str | None = None
    for env_file in service.env_files:
        if not env_file.is_file():
            if found is None:
                raise ConfigError(f"services.{service.name}.env_file is missing: {env_file}")
            continue
        value = _read_env_file_value(env_file, "RCON_PORT")
        if value is not None:
            found = value
    if found is not None:
        try:
            return int(found)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"RCON_PORT={found!r} in env_file is not an integer") from exc
    return 25575


def load_rcon_password(compose: ComposeFile, service: ComposeService) -> str:
    """Read the RCON password from ``secrets.rcon_password.file`` (§8.4).

    Raises ConfigError if the service doesn't reference ``rcon_password``,
    if the compose file doesn't declare ``secrets.rcon_password.file``,
    or if the file cannot be read.
    """
    if "rcon_password" not in service.secrets:
        raise ConfigError(f"services.{service.name}: RCON is required but the service does not declare the rcon_password secret")
    path = compose.secret_files.get("rcon_password")
    if path is None:
        raise ConfigError(f"services.{service.name}: rcon_password is referenced but secrets.rcon_password.file is not declared")
    if not path.is_file():
        raise ConfigError(f"rcon_password secret file not found: {path}")
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


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
        args = ["rcon-cli", *command.split(" ", 1)]
        try:
            exit_code, output = self._runtime.exec_run(self._container_name, args)
        except DockerUnavailableError:
            raise
        except Exception as exc:
            return (False, f"exec failed: {exc}")
        return (exit_code == 0, output.strip())


def _default_sock_factory(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


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
        try:
            sock = self._sock_factory(self.host, self.port, timeout)
        except OSError as exc:
            return (False, f"connect failed: {exc}")
        try:
            with contextlib.suppress(OSError):
                sock.settimeout(timeout)
            conn = _RconConnection(sock, timeout)
            try:
                conn.login(self._password)
            except (OSError, ValueError) as exc:
                return (False, f"RCON login failed: {exc}")
            try:
                payload = conn.command(command)
            except (OSError, ValueError) as exc:
                return (False, f"RCON command failed: {exc}")
            return (True, payload)
        finally:
            with contextlib.suppress(OSError):
                sock.close()


def select_rcon_transport(runtime: DockerRuntime, service: ComposeService, compose: ComposeFile, container_name: str, rcon_host: str | None) -> RconTransport:
    """Choose Option A or Option B per §8.4.

    Raises ConfigError on any configuration problem (missing secret file,
    multiple published mappings, remote rcon_host without a published
    mapping, etc.). No automatic fallback between transports.
    """
    rcon_port = resolve_rcon_port(service, compose)
    published = runtime.published_ports(container_name)
    mappings = published.get(f"{rcon_port}/tcp") or []
    if rcon_host:
        if not mappings:
            raise ConfigError(f"container {container_name!r}: [docker].rcon_host is set but RCON port {rcon_port}/tcp is not published")
        if len(mappings) > 1:
            raise ConfigError(f"container {container_name!r}: RCON port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")
        _ip, host_port = mappings[0]
        password = load_rcon_password(compose, service)
        return TcpRconTransport(host=rcon_host, port=host_port, password=password)
    if len(mappings) == 1:
        _ip, host_port = mappings[0]
        password = load_rcon_password(compose, service)
        return TcpRconTransport(host="127.0.0.1", port=host_port, password=password)
    if not mappings:
        return ExecRconTransport(runtime, container_name)
    raise ConfigError(f"container {container_name!r}: RCON port {rcon_port}/tcp is published {len(mappings)} times; expected exactly one")


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
        """Logs in to the service."""
        rid = self._alloc_id()
        self._send(rid, _AUTH_TYPE, password)
        resp_rid, _resp_type, _payload = self._recv()
        if resp_rid == -1:
            raise ValueError("authentication rejected")

    def command(self, command: str) -> str:
        """Placeholder for command-related functionality."""
        rid = self._alloc_id()
        self._send(rid, _COMMAND_TYPE, command)
        _resp_rid, _resp_type, payload = self._recv()
        return payload
