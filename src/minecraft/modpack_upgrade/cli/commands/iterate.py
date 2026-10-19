# src/minecraft/modpack_upgrade/cli/commands/iterate.py

"""`minecraft-upgrade iterate --run-id <run-id>` - boot loop (Section 15.1.6).

Refuses unless ``state.status == materialized``. Drives a candidate
through up to ``_BOOT_ITERATION_CAP`` boot attempts within
``_BOOT_WALLCLOCK_CAP_SECONDS``.

The loop policy - iteration count, monotonic wallclock deadline, outcome
dispatch, and failure-state persistence - is owned by :func:`_run_boot_loop`
so it can be exercised with a fake clock and a fake driver, independently
of Docker and log classification. Each attempt is delegated to an
injectable :class:`_BootDriver`.

On success (driver returns ``"success"``), state stays ``materialized``
and the command exits 0.

On any unrecoverable failure, state transitions to ``failed`` with
``reason in {"cap_exhausted", "ambiguous_failure"}`` (Section 15.1.6) and
the command exits 3.

The production boot driver - Docker lifecycle, log capture, classifier -
is not yet wired to a real Docker client. :class:`_UnimplementedBootDriver`
remains the production default and raises ``UpgradeCliError(1)`` before
any iteration runs, leaving state untouched. The :class:`_DockerBootDriver`,
:class:`_DockerClient`, and :class:`_LogClassifier` seams are shipped and
tested with fakes; connecting a real ``_DockerClient`` is a later slice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.state import (
    STATUS_FAILED,
    STATUS_MATERIALIZED,
    RunState,
    load_state,
    save_state,
)

__all__ = ["BootOutcome", "run"]

_log = get_logger(__name__)

# Section 15.1.6 caps. Overridable per-invocation for tests.
_BOOT_ITERATION_CAP = 15
_BOOT_WALLCLOCK_CAP_SECONDS = 2700.0  # 45 minutes

_BootResult = Literal["success", "failure", "ambiguous"]

# Section 15.1.6 reason codes. Persisted to state.reason as-is.
_REASON_CAP_EXHAUSTED = "cap_exhausted"
_REASON_AMBIGUOUS = "ambiguous_failure"


# ---------------------------------------------------------------------------
# Driver contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootOutcome:
    """Result of one boot attempt, as reported by a :class:`_BootDriver`.

    ``detail`` carries an optional operator-facing string (log excerpt,
    classifier rationale). Never persisted to ``state.toml`` - state.reason
    carries only the enum value.
    """

    outcome: _BootResult
    detail: str | None = None


class _BootDriver(Protocol):
    """Executes one candidate boot attempt and returns a classified outcome.

    The driver is responsible for:

      * starting the candidate container,
      * capturing its log,
      * classifying the outcome,
      * honouring ``deadline`` so a single attempt cannot silently
        exceed the outer wall-clock cap.

    The driver MUST NOT mutate run state. State transitions are owned by
    :func:`_run_boot_loop`.
    """

    def run_attempt(
        self,
        candidate_dir: Path,
        *,
        deadline: float,
    ) -> BootOutcome:
        """Run one attempt. ``deadline`` is an absolute monotonic value."""
        ...


class _DockerClient(Protocol):
    """Narrow seam around the Docker CLI/SDK for one candidate attempt.

    Implementations MUST be atomic at the ``start_candidate`` boundary:
    either the call returns a container_id and the container exists, or
    it raises and no candidate container remains. Any partially-started
    state is the client's responsibility to clean up before raising.

    Implementations MUST honour ``deadline`` in ``wait_for_exit``: the
    driver does not re-check the clock between ``start_candidate`` and
    ``wait_for_exit``, so a single attempt can exceed the outer wall-clock
    cap if the client ignores the deadline.

    ``stop_and_remove`` MUST be idempotent: it is called from a
    ``finally`` block and may run against a container that already exited
    or was removed.
    """

    def start_candidate(self, candidate_dir: Path) -> str:
        """Start the candidate container; return its container_id."""
        ...

    def wait_for_exit(self, container_id: str, *, deadline: float) -> int:
        """Block until the container exits or ``deadline`` passes.

        Returns the exit code. Raises :class:`TimeoutError` if the
        deadline passes first.
        """
        ...

    def capture_logs(self, container_id: str) -> str:
        """Return the container's complete log output."""
        ...

    def stop_and_remove(self, container_id: str) -> None:
        """Stop (if running) and remove the container. Idempotent."""
        ...


class _LogClassifier(Protocol):
    """Maps a boot attempt's exit code and logs to a :class:`BootOutcome`.

    Stateless and pure. The driver calls it once per attempt after logs
    are captured and before teardown.
    """

    def classify(self, *, exit_code: int, logs: str) -> BootOutcome:
        """Return the classified outcome for one boot attempt."""
        ...


# ---------------------------------------------------------------------------
# Conservative default classifier
# ---------------------------------------------------------------------------


# Conservative classification markers used by :class:`_MinimalLogClassifier`.
#
# The full classifier catalog is a later deliverable. This minimal set is
# chosen to be:
#   * small enough to audit,
#   * specific enough not to false-positive on normal WARN lines,
#   * broad enough to catch the mod-crash idioms that Forge/Fabric emit.
#
# When a marker is present (or the exit code is non-zero), the classifier
# returns ``ambiguous`` and escalates to the operator with a log excerpt.
# It does NOT return ``failure``: without the fix subsystem, a ``failure``
# would consume 14 more attempts chasing an unclassified problem.
_MINIMAL_FATAL_MARKERS: tuple[str, ...] = (
    "---- Minecraft Crash Report ----",
    "Exception in thread",
    "[FATAL]",
    "Caused by:",
    "UnsupportedClassVersionError",
)

_LOG_EXCERPT_LIMIT = 200


def _log_excerpt(logs: str) -> str:
    """Return a short excerpt of ``logs`` for embedding in an error detail."""
    if len(logs) <= _LOG_EXCERPT_LIMIT:
        return logs
    return logs[:_LOG_EXCERPT_LIMIT] + "..."


class _MinimalLogClassifier:
    """Conservative classifier shipped with the driver, pending the full catalog.

    Returns ``success`` only for a clean boot: exit 0 and no fatal marker
    in the log. Returns ``ambiguous`` for any non-zero exit, and for a
    zero exit whose log contains a fatal marker. Never returns
    ``failure`` (see module docstring and :data:`_MINIMAL_FATAL_MARKERS`).

    The operator sees the log excerpt in the CLI error, so escalation is
    informative even when the classifier cannot decide.
    """

    def classify(self, *, exit_code: int, logs: str) -> BootOutcome:
        """Return ``success`` only for a clean boot; ``ambiguous`` otherwise."""
        if exit_code != 0:
            return BootOutcome(
                outcome="ambiguous",
                detail=(f"nonzero exit code {exit_code}; log excerpt: {_log_excerpt(logs)!r}"),
            )
        for marker in _MINIMAL_FATAL_MARKERS:
            if marker in logs:
                return BootOutcome(
                    outcome="ambiguous",
                    detail=(f"zero exit but log contains {marker!r}; log excerpt: {_log_excerpt(logs)!r}"),
                )
        return BootOutcome(outcome="success")


# ---------------------------------------------------------------------------
# Production driver shell (real Docker client wired in a later slice)
# ---------------------------------------------------------------------------


class _DockerBootDriver:
    """Orchestrate one boot attempt: start, wait, capture, classify, teardown.

    The Docker-specific mechanics live behind :class:`_DockerClient`; the
    classifier behind :class:`_LogClassifier`. This class owns the
    sequencing and the cleanup guarantee:

      * teardown runs in ``finally`` whether the attempt succeeded,
        timed out, or raised;
      * a timeout during ``wait_for_exit`` is classified as
        ``ambiguous`` (a candidate that cannot boot within the wallclock
        cap is not a clean success);
      * other client errors are re-raised as :class:`UpgradeCliError(1)`
        so ``run()`` can distinguish "tool cannot test" (exit 1, state
        untouched) from "candidate tested and failed" (exit 3, state=failed).
    """

    def __init__(self, client: _DockerClient, classifier: _LogClassifier) -> None:
        """Store the client and classifier; no I/O until ``run_attempt``."""
        self._client = client
        self._classifier = classifier

    def run_attempt(
        self,
        candidate_dir: Path,
        *,
        deadline: float,
    ) -> BootOutcome:
        """Execute one attempt and return the classified outcome."""
        container_id = self._client.start_candidate(candidate_dir)
        try:
            try:
                exit_code = self._client.wait_for_exit(container_id, deadline=deadline)
            except TimeoutError as exc:
                return BootOutcome(
                    outcome="ambiguous",
                    detail=f"container did not exit before deadline: {exc}",
                )
            except Exception as exc:
                raise UpgradeCliError(1, f"iterate: wait_for_exit failed: {exc}") from exc

            try:
                logs = self._client.capture_logs(container_id)
            except Exception as exc:
                raise UpgradeCliError(1, f"iterate: capture_logs failed: {exc}") from exc

            return self._classifier.classify(exit_code=exit_code, logs=logs)
        finally:
            try:
                self._client.stop_and_remove(container_id)
            except Exception as exc:
                _log.warning(f"iterate: teardown of {container_id!r} failed: {exc}")


class _NullDockerClient:
    """Placeholder ``_DockerClient`` that refuses to start any candidate.

    Used when :class:`_DockerBootDriver` is wired in code but a real client
    has not yet been provided. Fails fast with exit 1 (operational gap), so
    ``run()``'s caller sees "tool cannot test yet" and state remains
    untouched - matching :class:`_UnimplementedBootDriver`'s contract.

    Downstream methods are unreachable because ``start_candidate`` never
    returns; they raise ``AssertionError`` to make that contract explicit.
    """

    def start_candidate(self, candidate_dir: Path) -> str:
        """Raise ``UpgradeCliError(1)`` to signal no real client is wired."""
        del candidate_dir
        raise UpgradeCliError(
            1,
            "iterate: _DockerBootDriver has no _DockerClient wired; substitute a real client before switching the production default. State was not modified.",
        )

    def wait_for_exit(self, container_id: str, *, deadline: float) -> int:
        """Unreachable: ``start_candidate`` must raise."""
        del container_id, deadline
        raise AssertionError("unreachable: _NullDockerClient.start_candidate must raise")

    def capture_logs(self, container_id: str) -> str:
        """Unreachable: ``start_candidate`` must raise."""
        del container_id
        raise AssertionError("unreachable: _NullDockerClient.start_candidate must raise")

    def stop_and_remove(self, container_id: str) -> None:
        """Unreachable: ``start_candidate`` must raise."""
        del container_id
        raise AssertionError("unreachable: _NullDockerClient.start_candidate must raise")


class _UnimplementedBootDriver:
    """Production placeholder; raises before any boot is attempted.

    The real driver ships with the Phase A boot loop and log classifier.
    Until then, exiting 1 (operational gap) is correct: nothing about the
    candidate has been tested, so state must not be mutated to ``failed``.

    The architecture shipped in Turn 2 (``_DockerBootDriver`` + ``_DockerClient``
    + ``_LogClassifier``) is ready to be wired by supplying a real client;
    swapping this default is a later slice, not part of Turn 2.
    """

    def run_attempt(self, candidate_dir: Path, *, deadline: float) -> BootOutcome:
        """Raise ``UpgradeCliError(1)`` to signal the tool cannot test yet."""
        del candidate_dir, deadline
        raise UpgradeCliError(
            1,
            "iterate: production boot driver not wired in this build. "
            "The driver architecture is shipped and tested with fakes; "
            "wiring a real _DockerClient is a later slice. "
            "State was not modified.",
        )


# ---------------------------------------------------------------------------
# State-machine internals
# ---------------------------------------------------------------------------


def _persist_failed(state: RunState, state_path: Path, *, reason: str) -> None:
    """Persist ``status=failed`` with ``reason`` to ``state.toml``.

    Called before the corresponding ``raise`` so the failure is durable
    even if the caller later crashes.
    """
    save_state(state_path, replace(state, status=STATUS_FAILED, reason=reason))


def _run_boot_loop(
    *,
    state: RunState,
    state_path: Path,
    candidate_dir: Path,
    driver: _BootDriver,
    clock: Callable[[], float],
    iteration_cap: int,
    wallclock_cap_seconds: float,
) -> int:
    """Run up to ``iteration_cap`` attempts within ``wallclock_cap_seconds``.

    Returns 0 on success (state stays ``materialized``). Raises
    ``UpgradeCliError(3)`` on cap or ambiguity; state is transitioned to
    ``failed`` with the appropriate ``reason`` before the raise.
    """
    deadline = clock() + wallclock_cap_seconds

    for iteration in range(1, iteration_cap + 1):
        if clock() >= deadline:
            _persist_failed(state, state_path, reason=_REASON_CAP_EXHAUSTED)
            raise UpgradeCliError(
                3,
                f"iterate: boot loop exceeded {wallclock_cap_seconds:.0f}s wallclock deadline "
                f"after {iteration - 1} iteration(s); reason={_REASON_CAP_EXHAUSTED!r}",
            )

        outcome = driver.run_attempt(candidate_dir, deadline=deadline)

        if outcome.outcome == "success":
            return 0
        if outcome.outcome == "ambiguous":
            _persist_failed(state, state_path, reason=_REASON_AMBIGUOUS)
            detail = outcome.detail or "no classifier detail"
            raise UpgradeCliError(
                3,
                f"iterate: boot loop terminated on ambiguous outcome at iteration {iteration} ({detail}); reason={_REASON_AMBIGUOUS!r}",
            )
        # outcome == "failure": loop continues.

    _persist_failed(state, state_path, reason=_REASON_CAP_EXHAUSTED)
    raise UpgradeCliError(
        3,
        f"iterate: boot loop exhausted {iteration_cap} iteration(s) without success; reason={_REASON_CAP_EXHAUSTED!r}",
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _state_dir(config_dir: Path) -> Path:
    """Return the daemon state directory."""
    return Path(load_daemon_config(config_dir).state_dir)


def _resolve_state_path(state_dir: Path, run_id: str) -> Path:
    """Return the path to the run's ``state.toml``."""
    return state_dir / "runs" / run_id / "state.toml"


def _candidate_dir(state_dir: Path, run_id: str) -> Path:
    """Return the run's candidate directory."""
    return state_dir / "candidates" / run_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    config_dir: Path,
    project_root: Path,
    run_id: str,
    driver: _BootDriver | None = None,
    clock: Callable[[], float] | None = None,
    iteration_cap: int = _BOOT_ITERATION_CAP,
    wallclock_cap_seconds: float = _BOOT_WALLCLOCK_CAP_SECONDS,
    state_dir: Path | None = None,
) -> int:
    """Execute ``minecraft-upgrade iterate``. Returns 0 on success, 3 on failure."""
    del project_root  # candidate_dir is derived from state_dir, not project_root.
    effective_state_dir = state_dir if state_dir is not None else _state_dir(config_dir)
    state_path = _resolve_state_path(effective_state_dir, run_id)

    try:
        state = load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    if state.status != STATUS_MATERIALIZED:
        raise UpgradeCliError(
            3,
            f"run {run_id!r} has status {state.status!r}; iterate requires {STATUS_MATERIALIZED!r}",
        )

    candidate_dir = _candidate_dir(effective_state_dir, run_id)
    slip_path = candidate_dir / "packing_slip.toml"
    if not slip_path.is_file():
        raise UpgradeCliError(3, f"candidate slip not found: {slip_path}")

    effective_driver = driver if driver is not None else _UnimplementedBootDriver()
    effective_clock = clock if clock is not None else time.monotonic

    return _run_boot_loop(
        state=state,
        state_path=state_path,
        candidate_dir=candidate_dir,
        driver=effective_driver,
        clock=effective_clock,
        iteration_cap=iteration_cap,
        wallclock_cap_seconds=wallclock_cap_seconds,
    )
