# tests/deploy_pack/test_hooks.py

"""Tests for deploy_pack.hooks, Project_Specs.md §4.5, §4.7, §4.8, §4.13, §4.14, §8.7, §8.8.

Coverage, in spec-section order:

  * §4.5 / §4.14 step 3 - compute_warned_and_running: the two-moment test
  * §4.14 step 6       - execute_pre_hook: the pre-stop re-inspection,
                         real stop vs. no-op stop vs. stop failure
  * §8.8               - pre-hook stop failure triggers the recovery flow
  * §4.13              - execute_post_hook: start every container, then
                         health-check; failure_stage selection
  * §4.7 / §4.8        - recover_stopped_containers: start, wait for
                         RCON-reachability, send the cancel notice
  * §8.7               - cancel-notice recipients are currently-running
                         warned-and-running members
  * §2.4               - DockerUnavailableError raised inside a hook is
                         re-raised as DockerRuntimeError

The Docker SDK boundary is the only thing faked here; every function
in hooks.py is executed against a fake runtime with the four methods
hooks calls (inspect, stop, start, wait_healthy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from minecraft.deploy_pack.docker_runtime import (
    Clock,
    ContainerState,
    HealthResult,
    StartResult,
    StopOutcome,
    StopResult,
)
from minecraft.deploy_pack.errors import DockerRuntimeError, DockerUnavailableError
from minecraft.deploy_pack.hooks import (
    PostHookResult,
    RecoveryContext,
    compute_warned_and_running,
    execute_post_hook,
    execute_pre_hook,
    recover_stopped_containers,
)

# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


class FakeClock(Clock):
    """Clock with a controllable monotonic time and recorded sleeps."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        """Return the current fake monotonic time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Record the sleep duration and advance the fake clock."""
        self.sleeps.append(seconds)
        self._now += seconds


# ---------------------------------------------------------------------------
# Fake runtime
# ---------------------------------------------------------------------------


def _state(name: str, running: bool = True, health: str | None = "healthy") -> ContainerState:
    """Build a ContainerState with the given running flag and health."""
    status = "running" if running else "exited"
    return ContainerState(name=name, exists=True, status=status, running=running, health=health, raw=None)


@dataclass
class FakeRuntime:
    """Minimal stand-in for DockerRuntime.

    Only the methods hooks actually call are implemented. Behaviour is
    driven by the four queues below; a queue empties on each call and
    the last value is reused if the queue is exhausted via the
    ``default_*`` fallbacks.
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
        """Return the configured state or a healthy default."""
        self._record("inspect", name)
        self._maybe_raise("inspect", name)
        queue = self.inspect_states.get(name)
        if queue:
            return queue.pop(0)
        if self.default_state is not None:
            return self.default_state
        return _state(name, running=True)

    def stop(self, name: str, timeout: int) -> StopResult:
        """Return the configured stop result or a STOPPED result."""
        self._record("stop", name, timeout)
        self._maybe_raise("stop", name)
        queue = self.stop_results.get(name)
        if queue:
            return queue.pop(0)
        return StopResult(name, StopOutcome.STOPPED)

    def start(self, name: str) -> StartResult:
        """Return the configured start result or a successful one."""
        self._record("start", name)
        self._maybe_raise("start", name)
        queue = self.start_results.get(name)
        if queue:
            return queue.pop(0)
        return StartResult(name, True)

    def wait_healthy(self, name: str, timeout: float, poll_interval: float, preexisting_unhealthy: bool = False) -> HealthResult:
        """Return the configured health result or a healthy one."""
        self._record("wait_healthy", name, timeout, poll_interval, preexisting_unhealthy)
        self._maybe_raise("wait_healthy", name)
        queue = self.health_results.get(name)
        if queue:
            return queue.pop(0)
        return HealthResult(name, True, "healthy")


# ---------------------------------------------------------------------------
# Recovery context builder
# ---------------------------------------------------------------------------


def _ctx(
    *,
    probe: Any = None,
    notice: Any = None,
    timeout: float = 5.0,
    interval: float = 1.0,
    clock: Clock | None = None,
) -> tuple[RecoveryContext, list[str], list[list[str]]]:
    """Build a RecoveryContext with recorded callbacks.

    Returns (context, probe_calls, notice_calls). The default probe
    always reports reachable; the default notice records recipients.
    """
    probe_calls: list[str] = []
    notice_calls: list[list[str]] = []

    def default_probe(name: str) -> bool:
        probe_calls.append(name)
        return True

    def default_notice(names: list[str]) -> None:
        notice_calls.append(list(names))

    ctx = RecoveryContext(
        reachability_probe=probe or default_probe,
        cancel_notice_fn=notice or default_notice,
        cancel_ready_timeout=timeout,
        poll_interval=interval,
        clock=clock or FakeClock(),
    )
    return (ctx, probe_calls, notice_calls)


# ---------------------------------------------------------------------------
# §4.5 / §4.14 step 3: compute_warned_and_running
# ---------------------------------------------------------------------------


def test_warned_and_running_all_running() -> None:
    """§4.5: restart_set members running at preflight and re-inspection are kept."""
    runtime = FakeRuntime()
    preflight = {"a": _state("a"), "b": _state("b")}
    assert compute_warned_and_running(runtime, ["a", "b"], preflight) == ["a", "b"]


def test_warned_and_running_excludes_not_running_at_preflight() -> None:
    """§4.5: a member not running at preflight is not eligible."""
    runtime = FakeRuntime()
    preflight = {"a": _state("a"), "b": _state("b", running=False)}
    assert compute_warned_and_running(runtime, ["a", "b"], preflight) == ["a"]


def test_warned_and_running_excludes_not_running_at_reinspection() -> None:
    """§4.5: a member that exited between preflight and re-inspection is excluded."""
    runtime = FakeRuntime(inspect_states={"a": [_state("a", running=False)]})
    preflight = {"a": _state("a"), "b": _state("b")}
    assert compute_warned_and_running(runtime, ["a", "b"], preflight) == ["b"]


def test_warned_and_running_missing_preflight_state_excludes_member() -> None:
    """§4.5: a restart_set member with no captured preflight state is not eligible."""
    runtime = FakeRuntime()
    assert compute_warned_and_running(runtime, ["a"], {}) == []


def test_warned_and_running_preserves_restart_set_order() -> None:
    """§2.9: the returned order matches the restart_set's (partition) order."""
    runtime = FakeRuntime()
    preflight = {"c": _state("c"), "a": _state("a"), "b": _state("b")}
    assert compute_warned_and_running(runtime, ["c", "a", "b"], preflight) == ["c", "a", "b"]


def test_warned_and_running_daemon_loss_is_converted_to_runtime_error() -> None:
    """§2.4: a DockerUnavailableError during re-inspection becomes DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"inspect:a": DockerUnavailableError("boom")})
    with pytest.raises(DockerRuntimeError):
        compute_warned_and_running(runtime, ["a"], {"a": _state("a")})


# ---------------------------------------------------------------------------
# §4.14 step 6 / §8.8: execute_pre_hook
# ---------------------------------------------------------------------------


def test_pre_hook_stops_every_running_warned_member() -> None:
    """§4.14 step 6: every warned member still running at the pre-stop re-inspection is stopped."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a", "b"], {"a": 30, "b": 30}, ctx)
    assert result.stopped == ["a", "b"]
    assert result.exited_before_stop == []
    assert result.failed == []
    assert result.recovery is None
    assert result.ok


def test_pre_hook_stop_timeout_is_taken_from_the_timeouts_map() -> None:
    """§8.10: the stop timeout is the instance's stop_grace_period seconds."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    execute_pre_hook(runtime, ["a"], {"a": 42}, ctx)
    assert runtime.calls["stop"] == [("a", 42)]


def test_pre_hook_exited_before_stop_is_not_counted_as_stopped() -> None:
    """§4.5 / §8.10: a no-op stop is not 'actually stopped by this deployment'."""
    runtime = FakeRuntime(stop_results={"a": [StopResult("a", StopOutcome.EXITED_BEFORE_STOP)]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.stopped == []
    assert result.exited_before_stop == ["a"]
    assert result.failed == []


def test_pre_hook_member_not_running_at_reinspection_is_not_stopped() -> None:
    """§4.14 step 6: only members running at the pre-stop re-inspection enter to_stop."""
    runtime = FakeRuntime(inspect_states={"a": [_state("a", running=False)]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.stopped == []
    assert result.exited_before_stop == []
    assert result.failed == []
    assert "stop" not in runtime.calls


def test_pre_hook_stop_failure_triggers_recovery() -> None:
    """§8.8: a stop failure invokes the recovery flow over the actually-stopped members."""
    runtime = FakeRuntime(
        stop_results={
            "a": [StopResult("a", StopOutcome.STOPPED)],
            "b": [StopResult("b", StopOutcome.FAILED, error="nope")],
        }
    )
    ctx, _, notices = _ctx()
    result = execute_pre_hook(runtime, ["a", "b"], {"a": 10, "b": 10}, ctx)
    assert result.stopped == ["a"]
    assert result.failed == ["b"]
    assert result.recovery is not None
    assert runtime.calls["start"] == [("a",)]
    assert notices == [["a", "b"]]


def test_pre_hook_all_stops_fail_does_not_start_anything() -> None:
    """§8.8: with no actually-stopped members, recovery has nothing to start."""
    runtime = FakeRuntime(stop_results={"a": [StopResult("a", StopOutcome.FAILED, error="x")]})
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)
    assert result.failed == ["a"]
    assert result.recovery is not None
    assert result.recovery.started == []
    assert "start" not in runtime.calls


def test_pre_hook_empty_warned_list_is_a_noop() -> None:
    """§4.14 step 6: no warned members means no stop phase."""
    runtime = FakeRuntime()
    ctx, _, _ = _ctx()
    result = execute_pre_hook(runtime, [], {}, ctx)
    assert result.ok
    assert runtime.calls == {}


def test_pre_hook_daemon_loss_during_stop_is_a_runtime_error() -> None:
    """§2.4: a daemon loss during a stop becomes DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"stop:a": DockerUnavailableError("boom")})
    ctx, _, _ = _ctx()
    with pytest.raises(DockerRuntimeError):
        execute_pre_hook(runtime, ["a"], {"a": 10}, ctx)


# ---------------------------------------------------------------------------
# §4.13: execute_post_hook
# ---------------------------------------------------------------------------


def test_post_hook_all_healthy() -> None:
    """§4.13: every container that starts healthy is reported as started and healthy."""
    runtime = FakeRuntime()
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.started == ["a", "b"]
    assert result.healthy == ["a", "b"]
    assert result.failure_stage is None


def test_post_hook_start_failure_does_not_halt_remaining_attempts() -> None:
    """§4.13: start attempts are all made, no halt on first failure."""
    runtime = FakeRuntime(start_results={"a": [StartResult("a", False, error="nope")]})
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.start_failed == ["a"]
    assert result.started == ["b"]
    assert [c[0] for c in runtime.calls["wait_healthy"]] == ["b"]
    assert result.healthy == ["b"]


def test_post_hook_health_timeout_is_the_health_timeout_stage() -> None:
    """§4.13 / §5.10: when only health fails, failure_stage is 'health_timeout'."""
    runtime = FakeRuntime(health_results={"a": [HealthResult("a", False, "unhealthy", error="timeout")]})
    result = execute_post_hook(runtime, ["a"], {}, 600, 2)
    assert result.health_failed == ["a"]
    assert result.failure_stage == "health_timeout"
    assert result.error_summary() == "health check timed out: a"


def test_post_hook_mixed_failures_take_the_post_hook_stage() -> None:
    """§4.13: when start and health both fail, post_hook takes precedence."""
    runtime = FakeRuntime(
        start_results={"a": [StartResult("a", False, error="nope")]},
        health_results={"b": [HealthResult("b", False, "unhealthy", error="timeout")]},
    )
    result = execute_post_hook(runtime, ["a", "b"], {}, 600, 2)
    assert result.failure_stage == "post_hook"
    summary = result.error_summary()
    assert "start failed: a" in summary
    assert "health check timed out: b" in summary
    assert "in addition to start failures" in summary


def test_post_hook_forwards_preexisting_unhealthy_to_health_poll() -> None:
    """§4.12 / §8.3: an unhealthy-at-preflight container has the flag forwarded to wait_healthy."""
    runtime = FakeRuntime()
    execute_post_hook(runtime, ["a"], {"a": _state("a", health="unhealthy")}, 600, 2)
    (call,) = runtime.calls["wait_healthy"]
    assert call == ("a", 600, 2, True)


def test_post_hook_empty_list_is_a_noop() -> None:
    """§4.13: nothing to start, nothing to health-check."""
    runtime = FakeRuntime()
    result = execute_post_hook(runtime, [], {}, 600, 2)
    assert result.started == []
    assert result.failure_stage is None
    assert runtime.calls == {}


def test_post_hook_daemon_loss_is_a_runtime_error() -> None:
    """§2.4: a daemon loss during start becomes DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"start:a": DockerUnavailableError("boom")})
    with pytest.raises(DockerRuntimeError):
        execute_post_hook(runtime, ["a"], {}, 600, 2)


# ---------------------------------------------------------------------------
# §4.7 / §4.8 / §8.8: recover_stopped_containers
# ---------------------------------------------------------------------------


def test_recover_starts_every_stopped_member_and_sends_the_cancel_notice() -> None:
    """§4.7 / §8.7: every stopped container is started; running warned members receive the cancel notice."""
    runtime = FakeRuntime()
    ctx, _probes, notices = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b"], warned_and_running=["a", "b"], ctx=ctx)
    assert result.started == ["a", "b"]
    assert result.reachable == ["a", "b"]
    assert result.unreachable == []
    assert not result.any_failure
    assert notices == [["a", "b"]]


def test_recover_start_failure_is_recorded() -> None:
    """§4.7: a start failure during recovery is reported in the result."""
    runtime = FakeRuntime(start_results={"a": [StartResult("a", False, error="boom")]})
    ctx, _, _ = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b"], warned_and_running=["a", "b"], ctx=ctx)
    assert result.start_failed == ["a"]
    assert result.started == ["b"]
    assert result.errors["a"] == "boom"
    assert result.reachable == ["b"]


def test_recover_reachability_timeout_marks_unreachable() -> None:
    """§4.7: a container that never becomes RCON-reachable is reported as unreachable."""

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


def test_recover_zero_timeout_disables_the_wait() -> None:
    """§4.7: cancel_ready_timeout=0 performs one probe and no sleep."""
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


def test_recover_cancel_notice_recipients_are_currently_running_only() -> None:
    """§8.7: the cancel notice goes to warned members that are currently running."""
    runtime = FakeRuntime(inspect_states={"b": [_state("b", running=False)]})
    ctx, _, notices = _ctx()
    recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a", "b"], ctx=ctx)
    assert notices == [["a"]]


def test_recover_cancel_notice_raising_does_not_fail_the_recovery() -> None:
    """§8.7: the cancel notice is best-effort."""

    def boom(names: list[str]) -> None:
        raise RuntimeError("nope")

    ctx, _, _ = _ctx(notice=boom)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.started == ["a"]


def test_recover_probe_exception_is_treated_as_unreachable() -> None:
    """§4.7: the reachability probe is best-effort; exceptions mean not reachable."""

    def probe(name: str) -> bool:
        raise RuntimeError("probe blew up")

    ctx, _, _ = _ctx(probe=probe, timeout=0)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)
    assert result.unreachable == ["a"]


def test_recover_empty_stopped_list_is_a_noop() -> None:
    """§4.7: nothing was stopped, so nothing is started and no notice is sent."""
    runtime = FakeRuntime()
    ctx, _, notices = _ctx()
    result = recover_stopped_containers(runtime, stopped_by_deployment=[], warned_and_running=[], ctx=ctx)
    assert result.started == []
    assert not result.any_failure
    assert notices == []


def test_recover_preserves_input_order_in_reachable_and_unreachable() -> None:
    """§4.7: reachability results preserve the caller's input order."""
    reachable_names = {"c", "a"}

    def probe(name: str) -> bool:
        return name in reachable_names

    ctx, _, _ = _ctx(probe=probe, timeout=0)
    runtime = FakeRuntime()
    result = recover_stopped_containers(runtime, stopped_by_deployment=["a", "b", "c"], warned_and_running=[], ctx=ctx)
    assert result.reachable == ["a", "c"]
    assert result.unreachable == ["b"]


def test_recover_daemon_loss_is_a_runtime_error() -> None:
    """§2.4: a daemon loss during recovery becomes DockerRuntimeError."""
    runtime = FakeRuntime(raise_on={"start:a": DockerUnavailableError("boom")})
    ctx, _, _ = _ctx()
    with pytest.raises(DockerRuntimeError):
        recover_stopped_containers(runtime, stopped_by_deployment=["a"], warned_and_running=["a"], ctx=ctx)


# ---------------------------------------------------------------------------
# PostHookResult: derived properties
# ---------------------------------------------------------------------------


def test_post_hook_result_healthy_has_no_failure_stage() -> None:
    """§5.10: a healthy result reports no failure stage."""
    r = PostHookResult(started=["a"], healthy=["a"])
    assert r.failure_stage is None
    assert r.error_summary() == "no failure"


def test_post_hook_result_start_only_uses_post_hook_stage() -> None:
    """§5.10: a start-only failure uses the post_hook stage."""
    r = PostHookResult(start_failed=["a"], errors={"a": "boom"})
    assert r.failure_stage == "post_hook"
    assert r.error_summary() == "start failed: a"


def test_post_hook_result_health_only_uses_health_timeout_stage() -> None:
    """§5.10: a health-only failure uses the health_timeout stage."""
    r = PostHookResult(health_failed=["a", "b"], errors={"a": "x", "b": "y"})
    assert r.failure_stage == "health_timeout"
    assert "health check timed out: a, b" in r.error_summary()
    assert "in addition" not in r.error_summary()
