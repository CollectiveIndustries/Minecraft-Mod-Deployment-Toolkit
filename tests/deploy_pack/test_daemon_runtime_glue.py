# tests/deploy_pack/test_daemon_runtime_glue.py

"""Unit tests for the Slice 13 Turn 4 runtime glue modules.

Covers :class:`DockerServiceControl` and :class:`DockerHealthProbe`
against fake Docker SDK objects. Both are thin wrappers, so each test
pins one classification or idempotency rule rather than exercising
Docker itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import NotFound

from minecraft.deploy_pack.daemon.health_probe import DockerHealthProbe
from minecraft.deploy_pack.daemon.service_control import DockerServiceControl
from minecraft.deploy_pack.docker_runtime import (
    StartResult,
    StopOutcome,
    StopResult,
)
from minecraft.deploy_pack.errors import DockerRuntimeError

# ---------------------------------------------------------------------------
# DockerServiceControl
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Stand-in for ``DockerRuntime`` with scripted stop/start results."""

    def __init__(self) -> None:
        """Initialize empty scripted outcomes."""
        self._stop_results: dict[str, StopResult] = {}
        self._start_results: dict[str, StartResult] = {}
        self.stop_calls: list[tuple[str, int]] = []
        self.start_calls: list[str] = []

    def script_stop(self, container: str, result: StopResult) -> None:
        """Set the stop result for ``container``."""
        self._stop_results[container] = result

    def script_start(self, container: str, result: StartResult) -> None:
        """Set the start result for ``container``."""
        self._start_results[container] = result

    def stop(self, name: str, timeout: int) -> StopResult:
        """Record the call and return the scripted result."""
        self.stop_calls.append((name, timeout))
        return self._stop_results.get(name, StopResult(name, StopOutcome.STOPPED))

    def start(self, name: str) -> StartResult:
        """Record the call and return the scripted result."""
        self.start_calls.append(name)
        return self._start_results.get(name, StartResult(name, True))


def test_service_control_stop_success() -> None:
    """A STOPPED outcome returns normally."""
    runtime = _FakeRuntime()
    control = DockerServiceControl(runtime=runtime, stop_timeouts={"mc-survival": 30})
    control.stop("mc-survival")
    assert runtime.stop_calls == [("mc-survival", 30)]


def test_service_control_stop_default_timeout() -> None:
    """A container not in the timeout map uses 10 seconds."""
    runtime = _FakeRuntime()
    control = DockerServiceControl(runtime=runtime)
    control.stop("mc-survival")
    assert runtime.stop_calls == [("mc-survival", 10)]


def test_service_control_stop_exited_before_is_success() -> None:
    """EXITED_BEFORE_STOP is idempotent success, not failure."""
    runtime = _FakeRuntime()
    runtime.script_stop("mc-survival", StopResult("mc-survival", StopOutcome.EXITED_BEFORE_STOP))
    control = DockerServiceControl(runtime=runtime)
    control.stop("mc-survival")  # No exception.


def test_service_control_stop_failed_raises() -> None:
    """FAILED outcome raises :class:`DockerRuntimeError` with the SDK error."""
    runtime = _FakeRuntime()
    runtime.script_stop("mc-survival", StopResult("mc-survival", StopOutcome.FAILED, error="boom"))
    control = DockerServiceControl(runtime=runtime)
    with pytest.raises(DockerRuntimeError, match="stop mc-survival: boom"):
        control.stop("mc-survival")


def test_service_control_start_success() -> None:
    """A successful start returns normally."""
    runtime = _FakeRuntime()
    control = DockerServiceControl(runtime=runtime)
    control.start("mc-survival")
    assert runtime.start_calls == ["mc-survival"]


def test_service_control_start_failed_raises() -> None:
    """A failed start raises :class:`DockerRuntimeError`."""
    runtime = _FakeRuntime()
    runtime.script_start("mc-survival", StartResult("mc-survival", False, "container missing"))
    control = DockerServiceControl(runtime=runtime)
    with pytest.raises(DockerRuntimeError, match="start mc-survival: container missing"):
        control.start("mc-survival")


# ---------------------------------------------------------------------------
# DockerHealthProbe
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Minimal SDK container stand-in."""

    def __init__(self, attrs: dict, logs: bytes | str = b"") -> None:
        """Store the attrs and logs."""
        self.attrs = attrs
        self._logs = logs

    def logs(self, tail: int = 500) -> bytes | str:
        """Return the scripted log payload."""
        del tail
        return self._logs


class _FakeContainers:
    """``client.containers`` stand-in keyed by container name."""

    def __init__(self, containers: dict[str, Any]) -> None:
        """Store the container map."""
        self._containers = containers

    def get(self, name: str) -> Any:
        """Return the container or raise NotFound."""
        if name not in self._containers:
            raise NotFound(f"no such container: {name}")
        return self._containers[name]


class _FakeClient:
    """``client`` stand-in exposing only ``containers.get``."""

    def __init__(self, containers: dict[str, Any]) -> None:
        """Store the containers map."""
        self.containers = _FakeContainers(containers)


class _FakeTransport:
    """Stand-in for :class:`RconTransport` with a scripted outcome."""

    def __init__(self, outcome: tuple[bool, str]) -> None:
        """Store the scripted outcome."""
        self._outcome = outcome

    def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Return the scripted outcome."""
        del command, timeout
        return self._outcome


def _running_attrs(restart_count: int = 0) -> dict:
    """Return attrs for a running container."""
    return {
        "State": {"Running": True},
        "RestartCount": restart_count,
    }


def test_probe_is_running_true() -> None:
    """A running container yields True."""
    client = _FakeClient({"mc-survival": _FakeContainer(_running_attrs())})
    probe = DockerHealthProbe(client=client)
    assert probe.is_running("mc-survival") is True


def test_probe_is_running_false_when_stopped() -> None:
    """A stopped container yields False."""
    client = _FakeClient({"mc-survival": _FakeContainer({"State": {"Running": False}})})
    probe = DockerHealthProbe(client=client)
    assert probe.is_running("mc-survival") is False


def test_probe_is_running_false_when_missing() -> None:
    """A missing container yields False without raising."""
    client = _FakeClient({})
    probe = DockerHealthProbe(client=client)
    assert probe.is_running("gone") is False


def test_probe_restart_count() -> None:
    """The restart count is read from attrs."""
    client = _FakeClient({"mc-survival": _FakeContainer(_running_attrs(restart_count=5))})
    probe = DockerHealthProbe(client=client)
    assert probe.restart_count("mc-survival") == 5


def test_probe_restart_count_missing_container_is_zero() -> None:
    """A missing container yields zero."""
    client = _FakeClient({})
    probe = DockerHealthProbe(client=client)
    assert probe.restart_count("gone") == 0


def test_probe_recent_logs_bytes_decoded() -> None:
    """Bytes logs are decoded to str."""
    client = _FakeClient({"mc-survival": _FakeContainer(_running_attrs(), logs=b"hello\n")})
    probe = DockerHealthProbe(client=client)
    assert probe.recent_logs("mc-survival") == "hello\n"


def test_probe_recent_logs_str_passthrough() -> None:
    """A str payload is returned verbatim."""
    client = _FakeClient({"mc-survival": _FakeContainer(_running_attrs(), logs="hi\n")})
    probe = DockerHealthProbe(client=client)
    assert probe.recent_logs("mc-survival") == "hi\n"


def test_probe_recent_logs_missing_container_is_empty() -> None:
    """A missing container yields an empty log."""
    client = _FakeClient({})
    probe = DockerHealthProbe(client=client)
    assert probe.recent_logs("gone") == ""


def test_probe_rcon_list_success() -> None:
    """A configured transport's outcome is returned verbatim."""
    client = _FakeClient({})
    probe = DockerHealthProbe(
        client=client,
        rcon_transports={"mc-survival": _FakeTransport((True, "3 players"))},
    )
    assert probe.rcon_list("mc-survival") == (True, "3 players")


def test_probe_rcon_list_no_transport() -> None:
    """A container without a transport yields a clear failure."""
    client = _FakeClient({})
    probe = DockerHealthProbe(client=client)
    ok, msg = probe.rcon_list("mc-survival")
    assert ok is False
    assert "no RCON transport" in msg


def test_probe_rcon_list_records_setup_error() -> None:
    """An RCON setup error is included in the failure message."""
    client = _FakeClient({})
    probe = DockerHealthProbe(
        client=client,
        rcon_errors={"mc-survival": "secret not declared"},
    )
    ok, msg = probe.rcon_list("mc-survival")
    assert ok is False
    assert "secret not declared" in msg


def test_probe_rcon_list_transport_raises() -> None:
    """An exception from the transport yields a failure, not a raise."""

    class _ExplodingTransport:
        def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
            del command, timeout
            raise RuntimeError("connection reset")

    client = _FakeClient({})
    probe = DockerHealthProbe(
        client=client,
        rcon_transports={"mc-survival": _ExplodingTransport()},
    )
    ok, msg = probe.rcon_list("mc-survival")
    assert ok is False
    assert "connection reset" in msg
