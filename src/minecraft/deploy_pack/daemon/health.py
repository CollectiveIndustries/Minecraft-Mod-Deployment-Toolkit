# src/minecraft/deploy_pack/daemon/health.py

"""Post-deployment health checks (spec v1.3 Section 12.9).

Runs five checks per container:

  1. Container is running.
  2. Startup-completion log sentinel observed within
     ``timeout_seconds`` (default 600).
  3. No unexpected restart during the health window (baseline captured
     at the start of the health check).
  4. RCON ``list`` succeeds ``rcon_list_required_successes`` times
     (default 3) with ``rcon_list_interval_seconds`` between successes
     (default 15).
  5. No configured fatal log marker appears in the recent logs.

Each check runs against a :class:`HealthProbe` implementation so the
production wiring (Docker SDK + RCON transport) stays out of this
module. The probe and clock are both injection seams; tests script
container state, logs, and RCON outcomes deterministically.

Fatal marker checks during the wait loops
-----------------------------------------
The startup-sentinel wait and the RCON-list wait both check the recent
logs for fatal markers on every poll. A container that crashes *before*
reaching the sentinel or *during* the RCON window is reported with the
specific marker (for example ``---- Minecraft Crash Report ----``)
instead of a generic "sentinel timeout" or "rcon list failed". This
makes the diagnostic actionable: an operator sees the crash idiom in
the health failure message without having to cross-reference the log.

The final ``_check_no_fatal_markers`` call at the end of the sequence
remains in place: it catches a marker that appears after the RCON
window closes (a crash in the last few seconds).

Fatal marker set
----------------
The default fatal markers mirror :data:`iterate._MINIMAL_FATAL_MARKERS`
so an operator sees the same vocabulary in ``iterate`` diagnostics and
in daemon health failures. Callers can widen the set per
:class:`HealthCheckSpec` when a specific runtime emits additional crash
idioms.

Journaling
----------
``run_health_checks`` journals ``HEALTH_CHECK_STARTED`` before running
any checks and ``HEALTH_CHECK_PASSED`` / ``HEALTH_CHECK_FAILED`` after.
Failure details (per-container reasons) land in the terminal record so
recovery can reconstruct which container failed and why.

Scope
-----
Velocity proxy health (the sixth item in spec Section 12.9) is out of scope
for this module -- it requires a running Velocity instance and a
backend-handshake probe that belong to a later slice. Callers that need
it should compose their own check on top of :class:`HealthReport`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from LoggingCore import get_logger

from .journal import DeploymentJournal, Stage

__all__ = [
    "ContainerHealthResult",
    "HealthCheckSpec",
    "HealthProbe",
    "HealthReport",
    "run_health_checks",
]


_log = get_logger(__name__)


_DEFAULT_FATAL_MARKERS: tuple[str, ...] = (
    "---- Minecraft Crash Report ----",
    "Exception in thread",
    "[FATAL]",
    "UnsupportedClassVersionError",
)


class Clock(Protocol):
    """Wall clock plus sleep; injected so polls are testable."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds``."""
        ...


class HealthProbe(Protocol):
    """Injection seam over Docker + RCON for post-start health checks.

    Production wiring lives in ``daemon/deploy.py``; this module only
    depends on the four methods below. Implementations MUST NOT raise on
    a container being absent, stopped, or having no health block -- those
    states are reported as ``False`` from :meth:`is_running` or an empty
    log string from :meth:`recent_logs` so the check classifies them
    rather than the probe raising.
    """

    def is_running(self, container: str) -> bool:
        """Return True iff ``container`` exists and is running."""
        ...

    def restart_count(self, container: str) -> int:
        """Return the container's cumulative restart count."""
        ...

    def recent_logs(self, container: str) -> str:
        """Return recent log output as UTF-8 text (lossy decode ok)."""
        ...

    def rcon_list(self, container: str) -> tuple[bool, str]:
        """Run ``list`` over RCON; return ``(success, response_or_error)``."""
        ...


@dataclass(frozen=True)
class HealthCheckSpec:
    """Per-container health-check parameters."""

    container: str
    startup_sentinel: str = "Done ("
    rcon_list_interval_seconds: float = 15.0
    rcon_list_required_successes: int = 3
    timeout_seconds: float = 600.0
    poll_interval_seconds: float = 5.0
    fatal_markers: tuple[str, ...] = _DEFAULT_FATAL_MARKERS


@dataclass(frozen=True)
class ContainerHealthResult:
    """Outcome of the health checks for one container."""

    container: str
    passed: bool
    failure_reasons: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class HealthReport:
    """Aggregate outcome across all containers."""

    results: tuple[ContainerHealthResult, ...]

    @property
    def passed(self) -> bool:
        """Return True iff every container passed."""
        return all(r.passed for r in self.results)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _first_fatal_marker(logs: str, markers: Sequence[str]) -> str | None:
    """Return the first fatal marker present in ``logs``, or None."""
    for marker in markers:
        if marker in logs:
            return marker
    return None


def _check_running(probe: HealthProbe, container: str) -> str | None:
    """Return an error string if the container is not running."""
    if not probe.is_running(container):
        return f"{container}: container is not running"
    return None


def _check_startup_sentinel(
    probe: HealthProbe,
    container: str,
    spec: HealthCheckSpec,
    clock: Clock,
) -> str | None:
    """Return an error string if the sentinel never appears within the timeout.

    Fails fast on a fatal marker: a container that never reaches the
    startup sentinel *because it crashed* is reported with the specific
    marker rather than a generic sentinel timeout.
    """
    deadline = clock.now() + spec.timeout_seconds
    while True:
        logs = probe.recent_logs(container)
        marker = _first_fatal_marker(logs, spec.fatal_markers)
        if marker is not None:
            return f"{container}: fatal marker observed before startup sentinel: {marker!r}"
        if spec.startup_sentinel in logs:
            return None
        remaining = deadline - clock.now()
        if remaining <= 0:
            return f"{container}: startup sentinel {spec.startup_sentinel!r} not observed within {spec.timeout_seconds:g}s"
        clock.sleep(min(spec.poll_interval_seconds, remaining))


def _check_no_unexpected_restart(
    probe: HealthProbe,
    container: str,
    baseline_restarts: int,
) -> str | None:
    """Return an error string if the restart count changed since baseline."""
    current = probe.restart_count(container)
    if current != baseline_restarts:
        delta = current - baseline_restarts
        return f"{container}: container restarted {delta} time(s) during health window (baseline={baseline_restarts}, current={current})"
    return None


def _check_rcon_list(
    probe: HealthProbe,
    container: str,
    spec: HealthCheckSpec,
    clock: Clock,
    logger: Any,
) -> str | None:
    """Return an error string if RCON ``list`` does not succeed enough times.

    Checks the log for fatal markers on every poll so a container that
    crashes *during* the health window is reported with the specific
    marker rather than a generic RCON failure.
    """
    successes = 0
    deadline = clock.now() + spec.timeout_seconds
    while successes < spec.rcon_list_required_successes:
        logs = probe.recent_logs(container)
        marker = _first_fatal_marker(logs, spec.fatal_markers)
        if marker is not None:
            return f"{container}: fatal marker observed during RCON health poll: {marker!r}"
        ok, detail = probe.rcon_list(container)
        remaining = deadline - clock.now()
        if ok:
            successes += 1
            logger.debug(f"health: {container} rcon list {successes}/{spec.rcon_list_required_successes}")
            if successes >= spec.rcon_list_required_successes:
                return None
            if remaining <= 0:
                return f"{container}: rcon list did not complete {spec.rcon_list_required_successes} times within {spec.timeout_seconds:g}s"
            clock.sleep(min(spec.rcon_list_interval_seconds, remaining))
        else:
            if remaining <= 0:
                return f"{container}: rcon list failed: {detail}"
            clock.sleep(min(spec.poll_interval_seconds, remaining))
    return None


def _check_no_fatal_markers(
    probe: HealthProbe,
    container: str,
    spec: HealthCheckSpec,
) -> str | None:
    """Return an error string if any fatal marker appears in recent logs.

    Final snapshot check that catches a marker appearing after the RCON
    window closed. The two wait loops above catch markers earlier with
    more specific messages; this is the belt-and-braces final pass.
    """
    logs = probe.recent_logs(container)
    marker = _first_fatal_marker(logs, spec.fatal_markers)
    if marker is not None:
        return f"{container}: fatal marker observed in logs: {marker!r}"
    return None


# ---------------------------------------------------------------------------
# Per-container orchestration
# ---------------------------------------------------------------------------


def _check_one_container(
    spec: HealthCheckSpec,
    probe: HealthProbe,
    clock: Clock,
    logger: Any,
) -> ContainerHealthResult:
    """Run every health check for one container, short-circuiting on failure."""
    start = clock.now()
    container = spec.container

    running_error = _check_running(probe, container)
    if running_error is not None:
        return ContainerHealthResult(container, False, (running_error,), clock.now() - start)

    baseline_restarts = probe.restart_count(container)

    sentinel_error = _check_startup_sentinel(probe, container, spec, clock)
    if sentinel_error is not None:
        return ContainerHealthResult(container, False, (sentinel_error,), clock.now() - start)

    restart_error = _check_no_unexpected_restart(probe, container, baseline_restarts)
    if restart_error is not None:
        return ContainerHealthResult(container, False, (restart_error,), clock.now() - start)

    rcon_error = _check_rcon_list(probe, container, spec, clock, logger)
    if rcon_error is not None:
        return ContainerHealthResult(container, False, (rcon_error,), clock.now() - start)

    fatal_error = _check_no_fatal_markers(probe, container, spec)
    if fatal_error is not None:
        return ContainerHealthResult(container, False, (fatal_error,), clock.now() - start)

    return ContainerHealthResult(container, True, (), clock.now() - start)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _journal_terminal(
    journal: DeploymentJournal,
    run_id: str,
    release_id: str,
    report: HealthReport,
) -> None:
    """Journal ``HEALTH_CHECK_PASSED`` or ``HEALTH_CHECK_FAILED`` with details."""
    if report.passed:
        journal.append_stage(
            run_id=run_id,
            release_id=release_id,
            stage=Stage.HEALTH_CHECK_PASSED,
            detail=f"containers={[r.container for r in report.results]}",
        )
        return
    failures = [f"{r.container}: {r.failure_reasons}" for r in report.results if not r.passed]
    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=Stage.HEALTH_CHECK_FAILED,
        detail=" | ".join(failures),
    )


def run_health_checks(
    *,
    specs: Sequence[HealthCheckSpec],
    probe: HealthProbe,
    clock: Clock,
    journal: DeploymentJournal,
    run_id: str,
    release_id: str,
    logger: Any = None,
) -> HealthReport:
    """Run every check for every container; return the aggregate report.

    Journals ``HEALTH_CHECK_STARTED`` before any check and
    ``HEALTH_CHECK_PASSED`` / ``HEALTH_CHECK_FAILED`` after. Never
    raises on container health failure: the report carries the outcome
    and the journal records the terminal stage.
    """
    effective_logger = logger if logger is not None else _log
    containers = [s.container for s in specs]
    effective_logger.info(f"health: checking {len(specs)} container(s): {containers}")

    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=Stage.HEALTH_CHECK_STARTED,
        detail=f"containers={containers}",
    )

    results = tuple(_check_one_container(spec, probe, clock, effective_logger) for spec in specs)
    report = HealthReport(results=results)
    _journal_terminal(journal, run_id, release_id, report)

    if report.passed:
        effective_logger.info(f"health: all {len(specs)} container(s) healthy")
    else:
        failed = [r.container for r in results if not r.passed]
        effective_logger.error(f"health: {len(failed)} container(s) failed: {failed}")
    return report
