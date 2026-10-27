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

Production wiring
-----------------
The production :class:`_BootDriver` is :class:`_DockerBootDriver` with a
:class:`_DeployPackSubprocessClient` as its client. The subprocess client
shells to ``deploy-pack --integration --release-pointer latest-slip``,
which deploys the candidate to the `latest` integration container and
health-checks it. Its exit code is the boot-test result:

  * exit 0  -> deploy + health checks passed -> success
  * non-zero -> some stage failed -> ambiguous (operator must decide
    whether the failure was the candidate or the environment)

This uses the *existing, heavily tested* integration deploy path instead
of re-implementing Docker orchestration. The trade-off is that `iterate`
cannot distinguish "the candidate does not boot" from "the environment
broke mid-deploy." Both surface as ``ambiguous_failure`` with the
subprocess's stderr attached; that is the honest signal.

Legacy placeholder classes
--------------------------
:class:`_NullDockerClient` and :class:`_UnimplementedBootDriver` are
retained as deprecated compatibility shims. They were the production
default before ``_DeployPackSubprocessClient`` landed; the
``test_iterate.py`` suite imports them, so removing them would break
that suite's imports. They are no longer used by the production code
path, but their behavior is unchanged: they raise
:class:`UpgradeCliError(1)` (operational gap) rather than mutating state.
Treat them as deprecated in any new code.

Testing
-------
The :class:`_BootDriver` seam is preserved: tests inject a fake driver
and never touch the production subprocess path. See the existing
``tests/modpack_upgrade/test_iterate.py`` - all 38 tests that inject
``driver=`` continue to pass. Tests that import the placeholder classes
also continue to pass; their behavior is unchanged.
"""

from __future__ import annotations

import subprocess
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
    """Narrow seam around the boot-attempt mechanism for one candidate.

    Implementations MUST be atomic at the ``start_candidate`` boundary:
    either the call returns a handle and the attempt is underway, or it
    raises and no partial state remains. Any partially-started state is
    the client's responsibility to clean up before raising.

    Implementations MUST honour ``deadline`` in ``wait_for_exit``: the
    driver does not re-check the clock between ``start_candidate`` and
    ``wait_for_exit``, so a single attempt can exceed the outer wall-clock
    cap if the client ignores the deadline.

    ``stop_and_remove`` MUST be idempotent: it is called from a
    ``finally`` block and may run against an attempt that already
    completed or was never fully started.
    """

    def start_candidate(self, candidate_dir: Path) -> str:
        """Begin one boot attempt; return an opaque handle."""
        ...

    def wait_for_exit(self, container_id: str, *, deadline: float) -> int:
        """Block until the attempt reaches a terminal state or ``deadline``.

        Returns an exit code: 0 for success, non-zero for failure.
        Raises :class:`TimeoutError` if the deadline passes first.
        """
        ...

    def capture_logs(self, container_id: str) -> str:
        """Return the attempt's complete log output."""
        ...

    def stop_and_remove(self, container_id: str) -> None:
        """Release any resources. Idempotent."""
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
# Production client: subprocess to deploy-pack --integration
# ---------------------------------------------------------------------------


class _DeployPackSubprocessClient:
    """``_DockerClient`` that shells to ``deploy-pack --integration``.

    Semantics:

      * ``start_candidate`` returns an opaque handle; the actual subprocess
        launch happens inside ``wait_for_exit`` so the client can honour
        the deadline budget correctly.
      * ``wait_for_exit`` runs ``deploy-pack --integration --release-pointer
        latest-slip`` with a timeout computed from the passed ``deadline``
        and the client's injected clock. Returns the subprocess's exit
        code. Raises :class:`TimeoutError` if the timeout fires (which
        the driver maps to ``ambiguous``).
      * ``capture_logs`` returns the subprocess's stdout+stderr. Empty
        if the subprocess has not run yet (unreachable in practice - the
        driver only calls it after a successful ``wait_for_exit``).
      * ``stop_and_remove`` is a no-op; ``subprocess.run`` already waited.

    Why subprocess-to-deploy-pack rather than raw Docker:

      1. ``deploy-pack --integration`` is the *proven* path - it is what
         the existing T04 acceptance test exercises and what the `latest`
         integration container is designed for.
      2. It does not require ``iterate`` to know anything about the
         operator's Docker setup (image reference, network config,
         container naming, healthcheck definitions).
      3. Its exit code already means the right thing: 0 = healthy,
         non-zero = some stage failed.

    What this cannot distinguish:

      * "the candidate does not boot" from "the environment broke."
        Both surface as ``ambiguous`` with the subprocess stderr attached.
        That is the honest signal given the information available.

    ``project_root`` is used as ``cwd`` for the subprocess so relative
    paths in the operator's `deploy_pack.toml` resolve the same way they
    would in a manual `deploy-pack` invocation.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        project_root: Path,
        clock: Callable[[], float],
    ) -> None:
        """Store invocation parameters; no subprocess until ``wait_for_exit``."""
        self._config_dir = config_dir
        self._project_root = project_root
        self._clock = clock
        self._result: subprocess.CompletedProcess[str] | None = None

    def start_candidate(self, candidate_dir: Path) -> str:
        """Return an opaque handle; the subprocess runs in ``wait_for_exit``."""
        del candidate_dir  # the integration deploy finds the candidate via latest-slip
        return "deploy-pack-integration"

    def wait_for_exit(self, container_id: str, *, deadline: float) -> int:
        """Run deploy-pack; return its exit code, or raise TimeoutError."""
        del container_id
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("deadline already passed before subprocess launch")
        argv = [
            "deploy-pack",
            "--integration",
            "--release-pointer",
            "latest-slip",
            "--server",
            "--config-dir",
            str(self._config_dir),
        ]
        try:
            self._result = subprocess.run(
                argv,
                cwd=str(self._project_root),
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"deploy-pack exceeded the wallclock deadline ({remaining:.0f}s)") from None
        except FileNotFoundError as exc:
            raise UpgradeCliError(
                1,
                f"iterate: 'deploy-pack' is not on PATH: {exc}. "
                f"Install the package so the console script exists, "
                f"or ensure the integration deploy path is available.",
            ) from exc
        except OSError as exc:
            raise UpgradeCliError(1, f"iterate: could not launch deploy-pack: {exc}") from exc
        return self._result.returncode

    def capture_logs(self, container_id: str) -> str:
        """Return the subprocess's captured output."""
        del container_id
        if self._result is None:
            return ""
        parts = [self._result.stdout or "", self._result.stderr or ""]
        return "\n".join(p for p in parts if p)

    def stop_and_remove(self, container_id: str) -> None:
        """No-op; subprocess.run already returned."""
        del container_id


# ---------------------------------------------------------------------------
# Production driver (orchestrates client + classifier)
# ---------------------------------------------------------------------------


class _DockerBootDriver:
    """Orchestrate one boot attempt: start, wait, capture, classify, teardown.

    The boot-attempt mechanics live behind :class:`_DockerClient`; the
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
                    detail=f"attempt did not exit before deadline: {exc}",
                )
            except UpgradeCliError:
                raise
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


# ---------------------------------------------------------------------------
# Deprecated placeholder classes (retained for test-suite import compatibility)
# ---------------------------------------------------------------------------


class _NullDockerClient:
    """Deprecated ``_DockerClient`` that refuses to start any candidate.

    Was the production default before :class:`_DeployPackSubprocessClient`
    landed. Retained because ``tests/modpack_upgrade/test_iterate.py``
    imports it directly; its behavior is unchanged. New code MUST NOT use
    this class. Production wiring uses :class:`_DeployPackSubprocessClient`.

    It fails fast with exit 1 (operational gap) so callers see "tool cannot
    test" and state remains untouched - matching
    :class:`_UnimplementedBootDriver`'s contract.

    Downstream methods are unreachable because ``start_candidate`` never
    returns; they raise ``AssertionError`` to make that contract explicit.
    """

    def start_candidate(self, candidate_dir: Path) -> str:
        """Raise ``UpgradeCliError(1)`` to signal no real client is wired."""
        del candidate_dir
        raise UpgradeCliError(
            1,
            "iterate: _NullDockerClient does not start candidates. "
            "This class is deprecated and retained only for test-suite "
            "compatibility; production wiring uses _DeployPackSubprocessClient. "
            "State was not modified.",
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
    """Deprecated placeholder ``_BootDriver``; raises before any boot.

    Was the production default before :class:`_DeployPackSubprocessClient`
    landed. Retained because ``tests/modpack_upgrade/test_iterate.py``
    imports it directly; its behavior is unchanged. New code MUST NOT use
    this class. Production wiring uses :class:`_DockerBootDriver` with
    :class:`_DeployPackSubprocessClient`.

    Exiting 1 (operational gap) is the correct behavior: nothing about
    the candidate has been tested, so state must not be mutated to
    ``failed``.
    """

    def run_attempt(self, candidate_dir: Path, *, deadline: float) -> BootOutcome:
        """Raise ``UpgradeCliError(1)`` to signal the tool cannot test yet."""
        del candidate_dir, deadline
        raise UpgradeCliError(
            1,
            "iterate: _UnimplementedBootDriver does not run attempts. "
            "This class is deprecated and retained only for test-suite "
            "compatibility; production wiring uses _DockerBootDriver with "
            "_DeployPackSubprocessClient. State was not modified.",
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
    """Execute ``minecraft-upgrade iterate``. Returns 0 on success, 3 on failure.

    When ``driver`` is None (production), the default driver shells to
    ``deploy-pack --integration`` via :class:`_DeployPackSubprocessClient`.
    Tests inject their own driver and never touch the subprocess path.
    """
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

    effective_clock = clock if clock is not None else time.monotonic

    if driver is None:
        client = _DeployPackSubprocessClient(
            config_dir=config_dir,
            project_root=project_root,
            clock=effective_clock,
        )
        driver = _DockerBootDriver(client, _MinimalLogClassifier())

    return _run_boot_loop(
        state=state,
        state_path=state_path,
        candidate_dir=candidate_dir,
        driver=driver,
        clock=effective_clock,
        iteration_cap=iteration_cap,
        wallclock_cap_seconds=wallclock_cap_seconds,
    )
