# src/minecraft/deploy_pack/hooks.py

"""Docker lifecycle orchestration (Project_Specs.md §4.14, §4.13, §4.7, §4.8, §8.8).

Responsibilities (§9.2):
  * compute ``warned_and_running`` from the restart set and preflight states (§4.5, §4.14 step 3)
  * execute the stop phase: re-inspect, stop ``to_stop``, collect outcomes (§4.14 step 6)
  * on stop failure, run the §8.8 recovery flow
  * execute the post phase: start every container this deployment actually
    stopped, wait for health, collect start/health results (§4.13)
  * expose the recovery flow as a standalone function so write-failure
    (§4.7) and reload-failure (§4.8) paths can invoke the same logic

Non-responsibilities:
  * RCON notice dispatch and the restart wait. Those interleave with
    lifecycle phases but are caller concerns (§4.14 steps 4-5). Hooks
    exposes the sets; the caller drives the notices.
  * Discord notifications. Hooks reports facts; notifications renders.
  * Deciding whether an instance is in ``restart_set``. That is the
    restart-policy adapter's job (scope_server).
  * Preflight-state capture. That happens in preflight.

Daemon-loss policy (§2.4)
-------------------------

Preflight lets ``DockerUnavailableError`` propagate (exit 3). Every
function here wraps its runtime calls so that a ``DockerUnavailableError``
arising *after* preflight is converted to ``DockerRuntimeError`` (exit 1).
Callers upstream of hooks can therefore rely on: any daemon loss that
reaches them is exit 1.

Instance names vs container names
---------------------------------

Deploy-state sets (``restart_set``, ``warned_and_running``,
``stopped_by_deployment``, ``preflight_states``) are keyed by *instance
name*. The Docker SDK is keyed by *container name*. The two are often
the same in test fixtures but usually differ in production (instance
``"survival"`` -> container ``"mc-survival"``).

Every function that talks to the SDK accepts a ``container_of`` mapping
and translates at the boundary. When ``container_of`` is None the two
names are assumed identical; existing callers that use opaque
identifiers see no behavior change. The return values are always keyed
by instance name.

Logging
-------

Every entry point logs its arguments at DEBUG, every SDK call logs at
DEBUG, every state transition logs at INFO, and every failure logs at
ERROR. Recovery flows log at INFO with failures at WARN. The module
logger is ``minecraft.deploy_pack.hooks``; callers may inject an
override via ``logger=`` for that call only.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .docker_runtime import Clock, ContainerState, DockerRuntime, HealthResult, StartResult, StopOutcome, StopResult
from .errors import DockerRuntimeError, DockerUnavailableError
from .logging_setup import get_logger

_log = get_logger(__name__)

__all__ = [
    "CancelNoticeFn",
    "PostHookResult",
    "PreHookResult",
    "ReachabilityProbeFn",
    "RecoveryContext",
    "RecoveryResult",
    "compute_warned_and_running",
    "execute_post_hook",
    "execute_pre_hook",
    "recover_stopped_containers",
]
ReachabilityProbeFn = Callable[[str], bool]
'Return True if the named container is currently RCON-reachable.\n\nMust not raise. Implementations should treat any exception as "not\nreachable" - the wait loop here is best-effort (§4.7).'
CancelNoticeFn = Callable[[list[str]], None]
"Send the in-game cancellation notice to each named container.\n\nBest-effort. Must not raise. Recipients are the set of containers that\nshould receive the notice; filtering has already been done by the\ncaller (see :func:`recover_stopped_containers`)."


@dataclass
class RecoveryContext:
    """Callbacks and timings for a recovery flow.

    Grouped so signatures stay readable and so the write-failure (§4.7),
    reload-failure (§4.8), and stop-failure (§8.8) paths all pass the
    same bundle.
    """

    reachability_probe: ReachabilityProbeFn
    cancel_notice_fn: CancelNoticeFn
    cancel_ready_timeout: float
    poll_interval: float
    clock: Clock


@dataclass
class RecoveryResult:
    """Outcome of a recovery flow (§4.7, §4.8, §8.8)."""

    started: list[str] = field(default_factory=list)
    start_failed: list[str] = field(default_factory=list)
    reachable: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def any_failure(self) -> bool:
        """Checks whether any failure has occurred."""
        return bool(self.start_failed) or bool(self.unreachable)


@dataclass
class PreHookResult:
    """Outcome of the stop phase (§4.14 step 6) plus §8.8 recovery, if any."""

    stopped: list[str] = field(default_factory=list)
    exited_before_stop: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    recovery: RecoveryResult | None = None

    @property
    def ok(self) -> bool:
        """Checks whether the operation succeeded."""
        return not self.failed


@dataclass
class PostHookResult:
    """Outcome of the start + health phase (§4.13)."""

    started: list[str] = field(default_factory=list)
    start_failed: list[str] = field(default_factory=list)
    healthy: list[str] = field(default_factory=list)
    health_failed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def any_start_failure(self) -> bool:
        """Checks whether any start failure has occurred."""
        return bool(self.start_failed)

    @property
    def any_health_failure(self) -> bool:
        """Checks whether any health failure has occurred."""
        return bool(self.health_failed)

    @property
    def failure_stage(self) -> str | None:
        """Return the §5.10 failure stage for this phase, or None.

        §4.13: ``post_hook`` takes precedence when both start and health
        failures occur; the error text notes the health timeouts.
        """
        if self.any_start_failure:
            return "post_hook"
        if self.any_health_failure:
            return "health_timeout"
        return None

    def error_summary(self) -> str:
        """Human-readable summary of start and health failures.

        When both occur, start failures are listed first (they dominate
        the failure stage) and health timeouts follow with a note.
        """
        parts: list[str] = []
        if self.start_failed:
            names = ", ".join(self.start_failed)
            parts.append(f"start failed: {names}")
        if self.health_failed:
            names = ", ".join(self.health_failed)
            suffix = ""
            if self.start_failed:
                suffix = " (in addition to start failures above)"
            parts.append(f"health check timed out: {names}{suffix}")
        return "; ".join(parts) if parts else "no failure"


@contextmanager
def _runtime_errors(context: str):
    """Convert DockerUnavailableError into DockerRuntimeError (§2.4).

    Once preflight has passed, any loss of the daemon is a runtime
    failure (exit 1), not a configuration failure (exit 3). This
    wrapper encodes that transition at the exact boundary.
    """
    try:
        yield
    except DockerUnavailableError as exc:
        raise DockerRuntimeError(f"{context}: {exc}") from exc


def _container_name(instance_name: str, container_of: dict[str, str] | None) -> str:
    """Translate an instance name to its container name.

    Deploy-state sets are keyed by instance name; the Docker SDK is
    keyed by container name. When ``container_of`` is None the two are
    the same and the name is returned unchanged.
    """
    if container_of is None:
        return instance_name
    return container_of.get(instance_name, instance_name)


def compute_warned_and_running(
    runtime: DockerRuntime,
    restart_set: list[str],
    preflight_states: dict[str, ContainerState],
    logger: Any = None,
    container_of: dict[str, str] | None = None,
) -> list[str]:
    """Return restart_set members running at preflight AND at re-inspection.

    The set is computed once, before notice dispatch (§4.5). Membership
    describes eligibility, not notice delivery: a member may be in this
    set and fail to receive its notice.

    Preserves ``restart_set`` order so callers relying on partition
    ordering (lexicographic, §2.9) get the right notice dispatch order.

    The returned list is keyed by instance name; the SDK is queried with
    the corresponding container name when ``container_of`` is supplied.
    """
    if logger is None:
        logger = _log
    logger.debug(f"compute_warned_and_running: restart_set={restart_set} preflight_states={list(preflight_states.keys())}")
    result: list[str] = []
    for name in restart_set:
        pre = preflight_states.get(name)
        if pre is None:
            logger.debug(f"compute_warned_and_running: {name} has no preflight state; not eligible")
            continue
        if not pre.is_running:
            logger.debug(f"compute_warned_and_running: {name} not running at preflight capture (status={pre.status!r})")
            continue
        container = _container_name(name, container_of)
        logger.debug(f"compute_warned_and_running: re-inspecting {name} (container {container})")
        with _runtime_errors(f"compute_warned_and_running: inspect {name}"):
            current = runtime.inspect(container)
        if current.is_running:
            logger.debug(f"compute_warned_and_running: {name} is running at re-inspection; eligible")
            result.append(name)
        else:
            logger.debug(f"compute_warned_and_running: {name} not running at pre-notice re-inspection (status={current.status!r})")
    logger.debug(f"compute_warned_and_running: returning {result}")
    return result


def execute_pre_hook(
    runtime: DockerRuntime,
    warned_and_running: list[str],
    stop_timeouts: dict[str, int],
    recovery_ctx: RecoveryContext,
    logger: Any = None,
    container_of: dict[str, str] | None = None,
) -> PreHookResult:
    """Re-inspect, stop the to_stop set, and recover on failure.

    ``warned_and_running`` is the set computed by
    :func:`compute_warned_and_running`. The pre-stop re-inspection
    filters it down to containers still running immediately before the
    stop call; those are ``to_stop`` (§4.14 step 6).

    A stop failure triggers the §8.8 recovery flow inline: every
    container we actually stopped is restarted, an in-game cancel
    notice is dispatched to running members of ``warned_and_running``,
    and the outcome is returned on ``result.recovery``.

    Raise policy: a real stop failure is captured in
    ``PreHookResult.failed``; a daemon loss is raised as
    ``DockerRuntimeError`` (exit 1).
    """
    if logger is None:
        logger = _log
    logger.debug(f"execute_pre_hook: warned_and_running={warned_and_running} stop_timeouts={stop_timeouts}")

    stopped: list[str] = []
    exited_before_stop: list[str] = []
    failed: list[str] = []
    to_stop: list[str] = []

    for name in warned_and_running:
        container = _container_name(name, container_of)
        logger.debug(f"pre-hook: pre-stop re-inspection of {name} (container {container})")
        with _runtime_errors(f"pre-hook: inspect {name}"):
            state = runtime.inspect(container)
        if state.is_running:
            to_stop.append(name)
        else:
            logger.warning(f"pre-hook: {name} not running at pre-stop re-inspection (status={state.status!r}); not stopping")

    if not to_stop and warned_and_running:
        logger.warning("pre-hook: to_stop is empty - every warned container exited in the interim")
    else:
        logger.debug(f"pre-hook: to_stop={to_stop}")

    for name in to_stop:
        container = _container_name(name, container_of)
        timeout = stop_timeouts.get(name, 10)
        logger.debug(f"pre-hook: stopping {name} (container {container}) timeout={timeout}s")
        with _runtime_errors(f"pre-hook: stop {name}"):
            result: StopResult = runtime.stop(container, timeout)
        if result.outcome == StopOutcome.STOPPED:
            logger.info(f"pre-hook: {name} stopped")
            stopped.append(name)
        elif result.outcome == StopOutcome.EXITED_BEFORE_STOP:
            logger.info(f"pre-hook: {name} exited before stop; not lifecycle-touched")
            exited_before_stop.append(name)
        else:
            logger.error(f"pre-hook: stop {name} failed: {result.error or 'unknown error'}")
            failed.append(name)

    recovery: RecoveryResult | None = None
    if failed:
        logger.error(f"pre-hook: {len(failed)} stop failure(s); running §8.8 recovery for {stopped}")
        recovery = recover_stopped_containers(
            runtime=runtime,
            stopped_by_deployment=stopped,
            warned_and_running=warned_and_running,
            ctx=recovery_ctx,
            logger=logger,
            container_of=container_of,
        )
    else:
        logger.debug(f"pre-hook: complete; stopped={stopped} exited_before_stop={exited_before_stop}")

    return PreHookResult(stopped=stopped, exited_before_stop=exited_before_stop, failed=failed, recovery=recovery)


def execute_post_hook(
    runtime: DockerRuntime,
    stopped_by_deployment: list[str],
    preflight_states: dict[str, ContainerState],
    health_timeout: int,
    poll_interval: float,
    logger: Any = None,
    container_of: dict[str, str] | None = None,
) -> PostHookResult:
    """Start every container this deployment actually stopped; then health-check.

    ``stopped_by_deployment`` is the ``stopped`` list from
    :class:`PreHookResult` - real stops only, no no-op stops (§4.5).

    Start attempts are not halted on first failure (§4.13). Containers
    that start successfully are then health-polled. A ``.State.Health``
    block missing post-start is treated as a health failure (§8.3), and
    a container observed ``unhealthy`` at preflight has its error text
    prefixed with that fact (§4.12).

    Raise policy: start and health failures are captured in the result;
    a daemon loss is raised as ``DockerRuntimeError``.
    """
    if logger is None:
        logger = _log
    logger.debug(f"execute_post_hook: stopped_by_deployment={stopped_by_deployment} health_timeout={health_timeout}s poll_interval={poll_interval}s")

    started: list[str] = []
    start_failed: list[str] = []
    healthy: list[str] = []
    health_failed: list[str] = []
    errors: dict[str, str] = {}

    for name in stopped_by_deployment:
        container = _container_name(name, container_of)
        logger.debug(f"post-hook: starting {name} (container {container})")
        with _runtime_errors(f"post-hook: start {name}"):
            result: StartResult = runtime.start(container)
        if result.success:
            logger.info(f"post-hook: {name} started")
            started.append(name)
        else:
            logger.error(f"post-hook: start {name} failed: {result.error or 'unknown error'}")
            start_failed.append(name)
            errors[name] = result.error or "start failed"

    for name in started:
        container = _container_name(name, container_of)
        pre = preflight_states.get(name)
        preexisting_unhealthy = pre is not None and pre.health == "unhealthy"
        if preexisting_unhealthy:
            logger.debug(f"post-hook: {name} was unhealthy at preflight; will annotate health failure")
        logger.debug(f"post-hook: waiting for {name} to become healthy (timeout={health_timeout}s)")
        with _runtime_errors(f"post-hook: health {name}"):
            result: HealthResult = runtime.wait_healthy(
                container,
                timeout=health_timeout,
                poll_interval=poll_interval,
                preexisting_unhealthy=preexisting_unhealthy,
            )
        if result.healthy:
            logger.info(f"post-hook: {name} is healthy")
            healthy.append(name)
        else:
            logger.error(f"post-hook: {name} health failed: {result.error or 'unknown error'}")
            health_failed.append(name)
            errors[name] = result.error or "health check failed"

    logger.debug(f"post-hook: complete; started={started} start_failed={start_failed} healthy={healthy} health_failed={health_failed}")
    return PostHookResult(started=started, start_failed=start_failed, healthy=healthy, health_failed=health_failed, errors=errors)


def _probe_reachable(probe: ReachabilityProbeFn, name: str, logger: Any) -> bool:
    """Call ``probe``; any exception is treated as not-reachable."""
    try:
        return bool(probe(name))
    except Exception as exc:
        logger.debug(f"reachability probe for {name} raised: {exc}")
        return False


def _wait_for_reachability(names: list[str], ctx: RecoveryContext, logger: Any) -> tuple[list[str], list[str]]:
    """Wait for ``names`` to become RCON-reachable (§4.7).

    Parallel: every pending name is probed once per iteration. Bounded
    by ``ctx.cancel_ready_timeout``; a value of 0 disables the wait
    (single probe, then return). Returns (reachable, unreachable) in
    the original ``names`` order.

    ``names`` are instance names; the probe callback is responsible for
    translating to whatever the RCON transport needs.
    """
    if not names:
        return ([], [])
    logger.debug(f"recovery: waiting for RCON-reachability of {names} (timeout={ctx.cancel_ready_timeout}s)")

    pending = list(names)
    reachable: list[str] = []
    still_pending: list[str] = []

    for name in pending:
        if _probe_reachable(ctx.reachability_probe, name, logger):
            logger.debug(f"recovery: {name} reachable on first probe")
            reachable.append(name)
        else:
            still_pending.append(name)
    pending = still_pending

    if ctx.cancel_ready_timeout <= 0 or not pending:
        if pending:
            logger.warning(f"recovery: cancel_ready_timeout is 0; giving up on {pending}")
        return (reachable, pending)

    deadline = ctx.clock.now() + ctx.cancel_ready_timeout
    while pending:
        now = ctx.clock.now()
        if now >= deadline:
            logger.warning(f"recovery: timeout reached; {len(pending)} container(s) unreachable: {pending}")
            break
        ctx.clock.sleep(min(ctx.poll_interval, deadline - now))
        still_pending = []
        for name in pending:
            if _probe_reachable(ctx.reachability_probe, name, logger):
                logger.debug(f"recovery: {name} became reachable")
                reachable.append(name)
            else:
                still_pending.append(name)
        pending = still_pending

    reachable_set = set(reachable)
    ordered_reachable = [n for n in names if n in reachable_set]
    ordered_unreachable = [n for n in names if n not in reachable_set]
    return (ordered_reachable, ordered_unreachable)


def _select_cancel_recipients(
    runtime: DockerRuntime,
    warned_and_running: list[str],
    logger: Any,
    container_of: dict[str, str] | None = None,
) -> list[str]:
    """warned_and_running members that are currently running (§8.7)."""
    result: list[str] = []
    for name in warned_and_running:
        container = _container_name(name, container_of)
        with _runtime_errors(f"recovery: inspect {name}"):
            state = runtime.inspect(container)
        if state.is_running:
            result.append(name)
    logger.debug(f"recovery: cancel-notice recipients = {result}")
    return result


def recover_stopped_containers(
    runtime: DockerRuntime,
    stopped_by_deployment: list[str],
    warned_and_running: list[str],
    ctx: RecoveryContext,
    logger: Any = None,
    container_of: dict[str, str] | None = None,
) -> RecoveryResult:
    """Restart containers we stopped and cancel any pending restart promise.

    This is the shared recovery flow used by:

      * §8.8 - a stop failure aborted the restart phase. The pre-hook
        calls this inline.
      * §4.7 - a non-server-scope write failed after the stop phase.
        The caller invokes this directly.
      * §4.8 - a reload failed after the stop phase. Same as §4.7.

    Flow:

      1. Start every container in ``stopped_by_deployment``. Attempt
         all, do not halt on failure.
      2. Wait for RCON-reachability of the started containers, bounded
         by ``ctx.cancel_ready_timeout`` (§4.7).
      3. Send the in-game cancel notice to every ``warned_and_running``
         member that is currently running (§8.7). Best-effort.

    Returns a :class:`RecoveryResult` describing what came back up. A
    daemon loss raises ``DockerRuntimeError`` (exit 1); the caller
    decides how to report it.
    """
    if logger is None:
        logger = _log
    logger.info(f"recovery: starting §4.7/§8.8 flow; stopped_by_deployment={stopped_by_deployment} warned_and_running={warned_and_running}")

    started: list[str] = []
    start_failed: list[str] = []
    errors: dict[str, str] = {}

    for name in stopped_by_deployment:
        container = _container_name(name, container_of)
        logger.debug(f"recovery: starting {name} (container {container})")
        with _runtime_errors(f"recovery: start {name}"):
            result: StartResult = runtime.start(container)
        if result.success:
            logger.info(f"recovery: {name} restarted")
            started.append(name)
        else:
            logger.error(f"recovery: start {name} failed: {result.error or 'unknown error'}")
            start_failed.append(name)
            errors[name] = result.error or "recovery start failed"

    reachable, unreachable = _wait_for_reachability(started, ctx, logger)
    for name in unreachable:
        errors.setdefault(name, "recovery: not RCON-reachable within timeout")

    if started and reachable:
        logger.info(f"recovery: {len(reachable)}/{len(started)} container(s) reachable: {reachable}")
    if unreachable:
        logger.warning(f"recovery: {len(unreachable)} container(s) not reachable: {unreachable}")

    recipients = _select_cancel_recipients(runtime, warned_and_running, logger, container_of)
    if recipients:
        logger.debug(f"recovery: dispatching cancel notice to {recipients}")
        try:
            ctx.cancel_notice_fn(recipients)
            logger.info(f"recovery: cancel notice sent to {recipients}")
        except Exception as exc:
            logger.warning(f"recovery: cancel notice raised: {exc}")
    else:
        logger.info("recovery: no running warned instances; no cancel notice")

    result = RecoveryResult(
        started=started,
        start_failed=start_failed,
        reachable=reachable,
        unreachable=unreachable,
        errors=errors,
    )
    logger.debug(f"recovery: complete; any_failure={result.any_failure} errors={errors}")
    return result
