# tests/deploy_pack/test_hooks.py

"""Tests for deploy_pack.hooks, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * compute_warned_and_running: three-moment test
  * execute_pre_hook: stop phase, real stop vs no-op stop, failure
  * execute_pre_hook: §8.8 recovery inline on stop failure
  * execute_post_hook: start-then-health, no halt on start failure
  * execute_post_hook: failure_stage selection (post_hook vs
    health_timeout, precedence)
  * recover_stopped_containers: start, reachability wait, cancel notice
  * recover_stopped_containers: timeout=0 disables the wait
  * recover_stopped_containers: cancel notice recipients (currently
    running members of warned_and_running)
  * DockerUnavailableError → DockerRuntimeError conversion (§2.4)
  * clock injection: no real sleeping
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from minecraft.deploy_pack.docker_runtime import Clock, ContainerState, HealthResult, StartResult, StopOutcome, StopResult
from minecraft.deploy_pack.errors import DockerRuntimeError, DockerUnavailableError
from minecraft.deploy_pack.hooks import (
    PostHookResult,
    RecoveryContext,
    compute_warned_and_running,
    execute_post_hook,
    execute_pre_hook,
    recover_stopped_containers,
)


class FakeClock(Clock):
    """A fake clock implementation for controlling time in tests."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        """Returns the current time of the fake clock."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Records a sleep duration and advances the fake clock by that amount."""
        self.sleeps.append(seconds)
        self._now += seconds


def _state(name: str, running: bool = True, health: str | None = "healthy") -> ContainerState:
    status = "running" if running else "exited"
    return ContainerState(name=name, exists=True, status=status, running=running, health=health, raw=None)


@dataclass
class FakeRuntime:
    """Minimal stand-in for DockerRuntime.

    Only the methods hooks actually calls are implemented. Tests configure
    per-container behaviour with the four dicts below.
    """

    inspect_states: dict[str, list[ContainerState]] = field(default_factory=dict)
    default_state: ContainerState | None = None
    stop_results: dict[str, list[StopResult]] = field(default_factory=dict)
    start_results: dict[str, list[StartResult]] = field(default_factory=dict)
    health_results: dict[str, list[HealthResult]] = field(default_factory=dict)
    raise_on: dict[str, BaseException] = field(default_factory=dict)
    calls: dict[str, list[Any]] = field(default_factory=dict)

    def _record(self, method: str, *args: Any) -> None:
        self.calls.setdefault(method, []).append(args)

    def _maybe_raise(self, method: str, name: str) -> None:
        key = f"{method}:{name}"
        if key in self.raise_on:
            raise self.raise_on[key]
        if method in self.raise_on:
            raise self.raise_on[method]

    def inspect(self, name: str) -> ContainerState:
        """Inspects the container state for the given name."""
        self._record("inspect", name)
        self._maybe_raise("inspect", name)
        queue = self.inspect_states.get(name)
        if queue:
            return queue.pop(0)
        if self.default_state is not None:
            return self.default_state
        return _state(name, running=True)

    def stop(self, name: str, timeout: int) -> StopResult:
        """Stops the operation."""
        self._record("stop", name, timeout)
        self._maybe_raise("stop", name)
        queue = self.stop_results.get(name)
        if queue:
            return queue.pop(0)
        return StopResult(name, StopOutcome.STOPPED)

    def start(self, name: str) -> StartResult:
        """Starts the process."""
        self._record("start", name)
        self._maybe_raise("start", name)
        queue = self.start_results.get(name)
        if queue:
            return queue.pop(0)
        return StartResult(name, True)

    def wait_healthy(self, name: str, timeout: float, poll_interval: float, preexisting_unhealthy: bool = False) -> HealthResult:
        """Waits for the service to become healthy."""
        self._record("wait_healthy", name, timeout, poll_interval, preexisting_unhealthy)
        self._maybe_raise("wait_healthy", name)
        queue = self.health_results.get(name)
        if queue:
            return queue.pop(0)
        return HealthResult(name, True, "healthy")


def _ctx(
    *, probe: Any = None, notice: Any = None, timeout: float = 5.0, interval: float = 1.0, clock: Clock | None = None
) -> tuple[RecoveryContext, list, list]:
    """Build a RecoveryContext with recorded callbacks."""
    probe_calls: list[str] = []
    notice_calls: list[list[str]] = []

    def default_probe(name: str) -> bool:
        probe_calls.append(name)
        return True

    def default_notice(names: list[str]) -> None:
        notice_calls.append(list(names))

    return (
        RecoveryContext(
            reachability_probe=probe or default_probe,
            cancel_notice_fn=notice or default_notice,
            cancel_ready_timeout=timeout,
            poll_interval=interval,
            clock=clock or FakeClock(),
        ),
        probe_calls,
        notice_calls,
    )


def test_warned_and_running_all_running() -> None:
    """Tests that all warned and running daemons are reported as running."""
    runtime = FakeRuntime()
    preflight = {"a": _state("a"), "b": _state("b")}
    result = compute_warned_and_running(runtime, ["a", "b"], preflight)
    assert result == ["a", "b"]


def test_warned_and_running_not_running_at_preflight() -> None:
    """{"def test_warned_and_running_not_running_at_preflight()": "Tests that compute_warned_and_running returns only warned and running items when a warned item is not running at preflight."}"""
    runtime = FakeRuntime()
    preflight = {"a": _state("a"), "b": _state("b", running=False)}
    result = compute_warned_and_running(runtime, ["a", "b"], preflight)
    assert result == ["a"]


def test_warned_and_running_not_running_at_reinspection() -> None:
    """{"def test_warned_and_running_not_running_at_reinspection()": "Tests that a warned, previously running resource is excluded when it is no longer running at reinspection."}"""
    runtime = FakeRuntime(inspect_states={"a": [_state("a", running=False)]})
    preflight = {"a": _state("a"), "b": _state("b")}
    result = compute_warned_and_running(runtime, ["a", "b"], preflight)
    assert result == ["b"]


def test_warned_and_running_missing_preflight_state() -> None:
    """{"def test_warned_and_running_missing_preflight_state()": "Tests that compute_warned_and_running returns an empty list when the preflight state is missing."}"""
    runtime = FakeRuntime()
    preflight: dict[str, ContainerState] = {}
    result = compute_warned_and_running(runtime, ["a"], preflight)
    assert result == []


def test_warned_and_running_preserves_order() -> None:
    """{"def test_warned_and_running_preserves_order()": "Tests that compute_warned_and_running preserves the order of input keys."}"""
    runtime = FakeRuntime()
    preflight = {"c": _state("c"), "a": _state("a"), "b": _state("b")}
    result = compute_warned_and_running(runtime, ["c", "a", "b"], preflight)
    assert result == ["c", "a", "b"]


def test_warned_and_running_daemon_loss_is_runtime_error() -> None:
    """Tests that losing a warned and running daemon raises a DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"inspect:a": DockerUnavailableError("boom")})
    preflight = {"a": _state("a")}
    with pytest.raises(DockerRuntimeError):
        compute_warned_and_running(runtime, ["a"], preflight)


def test_pre_hook_all_running_all_stopped() -> None:
    """Tests that all running pre-hooks are stopped successfully."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a", "b"], {"a": 30, "b": 30}, ctx)
    assert result.stopped == ["a", "b"]
    assert result.exited_before_stop == []
    assert result.failed == []
    assert result.recovery is None
    assert result.ok


def test_pre_hook_stop_timeout_from_dict() -> None:
    """Tests that a pre-hook stops with a timeout value from a dictionary."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    execute_pre_hook(runtime, ["a"], {"a": 42}, ctx)
    assert runtime.calls["stop"] == [("a", 42)]


def test_pre_hook_exited_before_stop() -> None:
    """Tests that execute_pre_hook reports a pre-hook as exited_before_stop when its stop result has the EXITED_BEFORE_STOP outcome."""
    runtime = FakeRuntime(stop_results={"a": [StopResult("a", StopOutcome.EXITED_BEFORE_STOP)]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.stopped == []
    assert result.exited_before_stop == ["a"]
    assert result.failed == []


def test_pre_hook_not_running_at_reinspection_skipped() -> None:
    """A warned member that has exited before the pre-stop re-inspection
    is not in to_stop, and does not appear in any result list.
    """
    runtime = FakeRuntime(inspect_states={"a": [_state("a", running=False)]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.stopped == []
    assert result.exited_before_stop == []
    assert result.failed == []
    assert "stop" not in runtime.calls


def test_pre_hook_stop_failure_triggers_recovery() -> None:
    """Tests that a failed pre-hook stop triggers recovery and starts previously stopped containers."""
    runtime = FakeRuntime(stop_results={"a": [StopResult("a", StopOutcome.STOPPED)], "b": [StopResult("b", StopOutcome.FAILED, error="nope")]})
    ctx, _, notices = _ctx()
    result = execute_pre_hook(runtime, ["a", "b"], {"a": 10, "b": 10}, ctx)
    assert result.stopped == ["a"]
    assert result.failed == ["b"]
    assert result.recovery is not None
    assert runtime.calls["start"] == [("a",)]
    assert notices == [["a", "b"]]


def test_pre_hook_all_stops_fail_no_recovery_start() -> None:
    """Tests that when all pre-hook stops fail, recovery does not start."""
    runtime = FakeRuntime(stop_results={"a": [StopResult("a", StopOutcome.FAILED, error="x")]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.failed == ["a"]
    assert result.recovery is not None
    assert result.recovery.started == []
    assert "start" not in runtime.calls


def test_pre_hook_empty_warned_is_noop() -> None:
    """Tests that an empty warned list makes the pre hook a successful no-op with no runtime calls."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, [], {}, ctx)
    assert result.ok
    assert runtime.calls == {}


def test_pre_hook_daemon_loss_is_runtime_error() -> None:
    """Tests that daemon loss during a pre-hook stop raises a DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"stop:a": DockerUnavailableError("boom")})
    ctx, _, _ = _ctx()
    with pytest.raises(DockerRuntimeError):
        execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)


def test_post_hook_all_healthy() -> None:
    """Tests that the post hook reports all services as started and healthy with no failure stage."""
    runtime = FakeRuntime()
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.started == ["a", "b"]
    assert result.healthy == ["a", "b"]
    assert result.failure_stage is None


def test_post_hook_start_failure_continues() -> None:
    """§4.13: do not halt on the first start failure."""
    runtime = FakeRuntime(start_results={"a": [StartResult("a", False, error="nope")]})
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.start_failed == ["a"]
    assert result.started == ["b"]
    assert [c[0] for c in runtime.calls["wait_healthy"]] == ["b"]
    assert result.healthy == ["b"]


def test_post_hook_health_failure() -> None:
    """Tests that a health check failure is recorded with the correct stage, failed service, and error summary."""
    runtime = FakeRuntime(health_results={"a": [HealthResult("a", False, "unhealthy", error="timeout")]})
    result = execute_post_hook(runtime, ["a"], {}, 600, 2)
    assert result.health_failed == ["a"]
    assert result.failure_stage == "health_timeout"
    assert result.error_summary() == "health check timed out: a"


def test_post_hook_failure_stage_post_hook_precedence() -> None:
    """§4.13: post_hook takes precedence when both start and health fail."""
    runtime = FakeRuntime(
        start_results={"a": [StartResult("a", False, error="nope")]}, health_results={"b": [HealthResult("b", False, "unhealthy", error="timeout")]}
    )
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.failure_stage == "post_hook"
    summary = result.error_summary()
    assert "start failed: a" in summary
    assert "health check timed out: b" in summary
    assert "in addition to start failures" in summary


def test_post_hook_preexisting_unhealthy_flag() -> None:
    """Tests that a preexisting unhealthy flag causes the post hook to wait for health with the flag set."""
    runtime = FakeRuntime()
    preflight = {"a": _state("a", health="unhealthy")}
    execute_post_hook(runtime, ["a"], preflight, 600, 2)
    (call,) = runtime.calls["wait_healthy"]
    assert call == ("a", 600, 2, True)


def test_post_hook_empty_is_noop() -> None:
    """Tests that executing an empty post-hook list is a no-op.

    Verifies that no containers are started, no failure stage is reported, and no
    runtime calls are made when the post-hook list is empty.
    """
    runtime = FakeRuntime()
    result = execute_post_hook(runtime, [], {}, 600, 2)
    assert result.started == []
    assert result.failure_stage is None
    assert runtime.calls == {}


def test_post_hook_daemon_loss_is_runtime_error() -> None:
    """Tests that a Docker daemon loss during a post-hook raises DockerRuntimeError.

    Simulates a runtime that raises DockerUnavailableError when starting a
    container, and asserts that execute_post_hook surfaces it as a
    DockerRuntimeError.
    """
    runtime = FakeRuntime(raise_on={"start:a": DockerUnavailableError("boom")})
    with pytest.raises(DockerRuntimeError):
        execute_post_hook(runtime, ["a"], {}, 600, 2)


def test_recover_all_reachable() -> None:
    """Tests that all stopped containers are recovered when every container is reachable."""
    runtime = FakeRuntime()
    ctx, probe_calls, notices = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b"], warned_and_running=["a", "b"], ctx=ctx)
    assert result.started == ["a", "b"]
    assert result.reachable == ["a", "b"]
    assert result.unreachable == []
    assert not result.any_failure
    assert notices == [["a", "b"]]


def test_recover_start_failure() -> None:
    """Tests that a container start failure is recorded while remaining containers are recovered."""
    runtime = FakeRuntime(start_results={"a": [StartResult("a", False, error="boom")]})
    ctx, _, _ = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b"], warned_and_running=["a", "b"], ctx=ctx)
    assert result.start_failed == ["a"]
    assert result.started == ["b"]
    assert result.errors["a"] == "boom"
    assert result.reachable == ["b"]


def test_recover_reachability_timeout() -> None:
    """A container that never becomes reachable is marked unreachable."""

    def probe(name: str) -> bool:
        return False

    clock = FakeClock()
    ctx, _, _ = _ctx(probe=probe, timeout=3, interval=1, clock=clock)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.reachable == []
    assert result.unreachable == ["a"]
    assert result.any_failure
    assert "not RCON-reachable" in result.errors["a"]
    assert len(clock.sleeps) == 3


def test_recover_zero_timeout_disables_wait() -> None:
    """cancel_ready_timeout=0: single probe, no sleep."""
    calls = {"n": 0}

    def probe(name: str) -> bool:
        calls["n"] += 1
        return False

    clock = FakeClock()
    ctx, _, _ = _ctx(probe=probe, timeout=0, interval=1, clock=clock)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.unreachable == ["a"]
    assert calls["n"] == 1
    assert clock.sleeps == []


def test_recover_cancel_notice_only_currently_running() -> None:
    """A warned member that is not currently running is not a recipient."""
    runtime = FakeRuntime(inspect_states={"b": [_state("b", running=False)]})
    ctx, _, notices = _ctx()
    recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a", "b"], ctx=ctx)
    assert notices == [["a"]]


def test_recover_cancel_notice_not_raised_on_failure() -> None:
    """Best-effort: a raising cancel notice does not fail the recovery."""

    def boom(names: list[str]) -> None:
        raise RuntimeError("nope")

    ctx, _, _ = _ctx(notice=boom)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.started == ["a"]


def test_recover_probe_exception_treated_as_unreachable() -> None:
    """Tests that a probe raising an exception marks the container as unreachable."""

    def probe(name: str) -> bool:
        raise RuntimeError("probe blew up")

    ctx, _, _ = _ctx(probe=probe, timeout=0)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.unreachable == ["a"]


def test_recover_empty_stopped_is_noop() -> None:
    """Tests that recovery does nothing when no containers are stopped."""
    runtime = FakeRuntime()
    ctx, _, notices = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=[], warned_and_running=[], ctx=ctx)
    assert result.started == []
    assert not result.any_failure
    assert notices == []


def test_recover_preserves_order_in_reachable_lists() -> None:
    """Reachability results preserve the input order regardless of probe order."""
    reachable_names = {"c", "a"}

    def probe(name: str) -> bool:
        return name in reachable_names

    ctx, _, _ = _ctx(probe=probe, timeout=0)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b", "c"], warned_and_running=[], ctx=ctx)
    assert result.reachable == ["a", "c"]
    assert result.unreachable == ["b"]


def test_recover_daemon_loss_is_runtime_error() -> None:
    """Tests that a Docker daemon loss during recovery raises DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"start:a": DockerUnavailableError("boom")})
    ctx, _, _ = _ctx()
    with pytest.raises(DockerRuntimeError):
        recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)


def test_post_hook_result_failure_stage_none_when_healthy() -> None:
    """Tests that a healthy post-hook result has no failure stage and reports no failure."""
    r = PostHookResult(started=["a"], healthy=["a"])
    assert r.failure_stage is None
    assert r.error_summary() == "no failure"


def test_post_hook_result_start_only() -> None:
    """Tests a post-hook result with only start failures."""
    r = PostHookResult(start_failed=["a"], errors={"a": "boom"})
    assert r.failure_stage == "post_hook"
    assert r.error_summary() == "start failed: a"


def test_post_hook_result_health_only() -> None:
    """Tests a post-hook result with only health failures."""
    r = PostHookResult(health_failed=["a", "b"], errors={"a": "x", "b": "y"})
    assert r.failure_stage == "health_timeout"
    assert "health check timed out: a, b" in r.error_summary()
    assert "in addition" not in r.error_summary()
