# src/minecraft/deploy_pack/daemon/deploy.py

"""Deployment orchestrator (spec v1.3 Section 12.4 steps 11-19).

Drives one deployment end-to-end:

  stage 11  Acquire the deployment lock          -> DeploymentLockHeldError (25)
  stage 12  Pull the release bundle              -> DeployCommandFailedError (22)
  stage 13  Create pre-upgrade world backup      -> ActivationFailedError (21)
  stage 14  Load the slip                        -> DeployCommandFailedError (22)
  stage 15  Stop services                        -> DeployCommandFailedError (22)
  stage 16  Ship the artifact set                -> DeployCommandFailedError (22)
  stage 17  Start services                       -> DeployCommandFailedError (22)
  stage 18  Run post-start health checks         -> PostDeployVerifyFailedError (23)
  stage 19  Journal COMPLETE and release lock

Any failure after stage 15 triggers :func:`recovery.attempt_recovery`
followed by a start-services attempt. If recovery itself fails, the
outcome is a ``RecoveryFailedError`` (24); the operator must intervene.

Refusal-before-start
--------------------
At entry, :func:`deploy` reads the journal for unresolved prior runs
(spec Section 12.7). If any exist, the deployment is refused with an
``ActivationFailedError`` (21) and the operator must resolve the
unresolved run before any new deployment can proceed. This is the
"halt and require operator intervention" contract.

Never raises on operational failure
-----------------------------------
The return type is :class:`DeployOutcome`. Callers (the HTTP plugin in
particular) translate the outcome into HTTP error responses. Exceptions
from the transport, lock, or any injected probe are captured and
reflected in the outcome, not propagated.

Injectables
-----------
Production wires real implementations. Tests script fakes:

  * ``service_control`` - stop/start of named containers.
  * ``health_probe``    - from :mod:`health`.
  * ``clock``           - from :mod:`health`.
  * ``pull_transport``  - from :mod:`pull`.
  * ``journal``         - from :mod:`journal`.
  * ``lock``            - from :mod:`lock`, embedded in the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from LoggingCore import get_logger

from minecraft.modpack_upgrade.slip import PackingSlip, load_slip

from .backup import create_backup
from .errors import DeploymentLockHeldError
from .health import HealthCheckSpec, HealthProbe, run_health_checks
from .journal import DeploymentJournal, Stage
from .lock import DeploymentLock
from .pull import PullRequest, RsyncTransport, pull_release
from .recovery import attempt_recovery
from .ship import ship_release

__all__ = [
    "DeployOutcome",
    "DeployRequest",
    "ServiceControl",
    "deploy",
]


_log = get_logger(__name__)


class ServiceControl(Protocol):
    """Injection seam over container stop/start for the orchestrator.

    Implementations MUST be idempotent: stopping an already-stopped
    container is a no-op, starting an already-running container is a
    no-op. Failures MUST be raised as exceptions so the orchestrator can
    classify and recover.
    """

    def stop(self, container: str) -> None:
        """Stop ``container``."""
        ...

    def start(self, container: str) -> None:
        """Start ``container``."""
        ...


class Clock(Protocol):
    """Wall clock plus sleep; injected for health-check polling."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds``."""
        ...


@dataclass(frozen=True)
class DeployRequest:
    """Everything needed to execute one deployment."""

    run_id: str
    release_id: str
    pull: PullRequest
    target_mods_dir: Path
    target_world_dir: Path | None
    world_backup_root: Path | None
    prior_bundle_dir: Path | None
    containers: tuple[str, ...]
    protect_patterns: tuple[str, ...]
    lock: DeploymentLock
    health_specs: tuple[HealthCheckSpec, ...]
    is_upgrade: bool = False


@dataclass
class DeployOutcome:
    """Outcome of :func:`deploy`.

    ``error_code`` is the numeric code from :mod:`errors` on failure,
    ``None`` on success. The HTTP plugin uses it to select the typed
    exception for response shape.
    """

    success: bool
    release_id: str
    stage: str | None = None
    message: str | None = None
    error_code: int | None = None


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------


def _fail(
    request: DeployRequest,
    journal: DeploymentJournal,
    stage: str,
    message: str,
    error_code: int,
) -> DeployOutcome:
    """Journal the terminal FAILED stage and return the failure outcome."""
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.FAILED,
        detail=f"{stage}: {message}",
    )
    _log.error(f"deploy: {request.release_id!r} failed at stage {stage!r}: {message}")
    return DeployOutcome(
        success=False,
        release_id=request.release_id,
        stage=stage,
        message=message,
        error_code=error_code,
    )


def _stage_pull(
    request: DeployRequest,
    journal: DeploymentJournal,
    transport: RsyncTransport | None,
) -> tuple[DeployOutcome | None, Path]:
    """Ensure the bundle exists locally, pulling if necessary."""
    bundle_dir = request.pull.final_dir
    if bundle_dir.is_dir():
        _log.debug(f"deploy: bundle already present at {bundle_dir}; skipping pull")
        return (None, bundle_dir)
    result = pull_release(
        request=request.pull,
        journal=journal,
        run_id=request.run_id,
        transport=transport,
    )
    if not result.success:
        return (
            _fail(request, journal, "pull", result.failure_message or "pull failed", 22),
            bundle_dir,
        )
    return (None, bundle_dir)


def _stage_backup(
    request: DeployRequest,
    journal: DeploymentJournal,
) -> tuple[DeployOutcome | None, Path | None]:
    """Create the pre-upgrade world backup if this is an upgrade."""
    if not request.is_upgrade:
        return (None, None)
    if request.target_world_dir is None or request.world_backup_root is None:
        return (
            _fail(request, journal, "backup", "upgrade requires target_world_dir and world_backup_root", 21),
            None,
        )
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.BACKUP_STARTED,
        detail=str(request.target_world_dir),
    )
    result = create_backup(
        source_world=request.target_world_dir,
        backup_root=request.world_backup_root,
        release_id=request.release_id,
    )
    if not result.success:
        return (
            _fail(request, journal, "backup", result.failure_message or "backup failed", 21),
            None,
        )
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.BACKUP_COMPLETED,
        detail=str(result.backup_dir),
    )
    return (None, result.backup_dir)


def _stage_load_slip(
    request: DeployRequest,
    journal: DeploymentJournal,
    bundle_dir: Path,
) -> tuple[DeployOutcome | None, PackingSlip | None]:
    """Load and validate the release slip."""
    slip_path = bundle_dir / "packing_slip.toml"
    try:
        slip = load_slip(slip_path)
    except Exception as exc:
        return (_fail(request, journal, "slip", f"slip load failed: {exc}", 22), None)
    return (None, slip)


def _stage_stop(
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
) -> DeployOutcome | None:
    """Stop every container in the deployment set."""
    for container in request.containers:
        try:
            service_control.stop(container)
        except Exception as exc:
            return _fail(request, journal, "stop", f"stop {container}: {exc}", 22)
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.SERVICES_STOPPED,
        detail=f"containers={list(request.containers)}",
    )
    return None


def _stage_ship(
    request: DeployRequest,
    journal: DeploymentJournal,
    slip: PackingSlip,
    bundle_dir: Path,
) -> DeployOutcome | None:
    """Ship the release artifact set into the target mods directory."""
    result = ship_release(
        release_id=request.release_id,
        run_id=request.run_id,
        slip=slip,
        bundle_dir=bundle_dir,
        target_dir=request.target_mods_dir,
        protect_patterns=request.protect_patterns,
        journal=journal,
    )
    if not result.success:
        return _fail(request, journal, "ship", result.failure_message or "ship failed", 22)
    return None


def _stage_start(
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
) -> DeployOutcome | None:
    """Start every container in the deployment set."""
    for container in request.containers:
        try:
            service_control.start(container)
        except Exception as exc:
            return _fail(request, journal, "start", f"start {container}: {exc}", 22)
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.SERVICES_STARTED,
        detail=f"containers={list(request.containers)}",
    )
    return None


def _stage_health(
    request: DeployRequest,
    journal: DeploymentJournal,
    health_probe: HealthProbe,
    clock: Clock,
) -> DeployOutcome | None:
    """Run post-start health checks for every container."""
    report = run_health_checks(
        specs=request.health_specs,
        probe=health_probe,
        clock=clock,
        journal=journal,
        run_id=request.run_id,
        release_id=request.release_id,
    )
    if report.passed:
        return None
    failed = [r.container for r in report.results if not r.passed]
    return _fail(request, journal, "health", f"health check failed for: {failed}", 23)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _start_services_after_failure(
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
) -> tuple[bool, str | None]:
    """Attempt to start every container; return (ok, error_message)."""
    for container in request.containers:
        try:
            service_control.start(container)
        except Exception as exc:
            return (False, f"start {container}: {exc}")
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.SERVICES_STARTED,
        detail=f"post-failure recovery; containers={list(request.containers)}",
    )
    return (True, None)


def _abort_with_recovery(
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
    *,
    failure_stage: str,
    failure_message: str,
    failure_code: int,
    backup_dir: Path | None,
    need_file_restore: bool,
) -> DeployOutcome:
    """Recover and bring services back; return the original failure.

    Restores files if the failure occurred after the stop phase and a
    restore source exists. Then ensures every container is started.
    Returns a ``RecoveryFailedError``-coded outcome if either the file
    restore or the start attempt failed; otherwise returns the original
    failure stage and message, preserving the trigger for the operator.
    """
    if need_file_restore and (request.prior_bundle_dir is not None or backup_dir is not None):
        recovery = attempt_recovery(
            run_id=request.run_id,
            release_id=request.release_id,
            world_backup_dir=backup_dir,
            target_world=request.target_world_dir,
            prior_bundle_dir=request.prior_bundle_dir,
            target_mods=request.target_mods_dir,
            protect_patterns=request.protect_patterns,
            journal=journal,
        )
        if not recovery.success:
            return _fail(
                request,
                journal,
                "recovery",
                f"recovery failed after {failure_stage}: {recovery.failure_message}",
                24,
            )

    start_ok, start_err = _start_services_after_failure(request, journal, service_control)
    if not start_ok:
        return _fail(
            request,
            journal,
            "recovery",
            f"post-failure start failed after {failure_stage}: {start_err}",
            24,
        )

    return _fail(request, journal, failure_stage, failure_message, failure_code)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _run_stages(
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
    health_probe: HealthProbe,
    clock: Clock,
    pull_transport: RsyncTransport | None,
) -> DeployOutcome:
    """The nine stages after the lock is acquired. Short-circuits on failure."""
    pull_outcome, bundle_dir = _stage_pull(request, journal, pull_transport)
    if pull_outcome is not None:
        return pull_outcome

    backup_outcome, backup_dir = _stage_backup(request, journal)
    if backup_outcome is not None:
        return backup_outcome

    slip_outcome, slip = _stage_load_slip(request, journal, bundle_dir)
    if slip_outcome is not None or slip is None:
        return slip_outcome or _fail(request, journal, "slip", "slip is None after load", 22)

    stop_outcome = _stage_stop(request, journal, service_control)
    if stop_outcome is not None:
        return _abort_with_recovery(
            request,
            journal,
            service_control,
            failure_stage="stop",
            failure_message=stop_outcome.message or "stop failed",
            failure_code=22,
            backup_dir=backup_dir,
            need_file_restore=False,
        )

    ship_outcome = _stage_ship(request, journal, slip, bundle_dir)
    if ship_outcome is not None:
        return _abort_with_recovery(
            request,
            journal,
            service_control,
            failure_stage="ship",
            failure_message=ship_outcome.message or "ship failed",
            failure_code=22,
            backup_dir=backup_dir,
            need_file_restore=True,
        )

    start_outcome = _stage_start(request, journal, service_control)
    if start_outcome is not None:
        return _abort_with_recovery(
            request,
            journal,
            service_control,
            failure_stage="start",
            failure_message=start_outcome.message or "start failed",
            failure_code=22,
            backup_dir=backup_dir,
            need_file_restore=True,
        )

    health_outcome = _stage_health(request, journal, health_probe, clock)
    if health_outcome is not None:
        return _abort_with_recovery(
            request,
            journal,
            service_control,
            failure_stage="health",
            failure_message=health_outcome.message or "health check failed",
            failure_code=23,
            backup_dir=backup_dir,
            need_file_restore=True,
        )

    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.COMPLETE,
        detail="",
    )
    _log.info(f"deploy: {request.release_id!r} complete")
    return DeployOutcome(success=True, release_id=request.release_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def deploy(
    *,
    request: DeployRequest,
    journal: DeploymentJournal,
    service_control: ServiceControl,
    health_probe: HealthProbe,
    clock: Clock,
    pull_transport: RsyncTransport | None = None,
) -> DeployOutcome:
    """Execute one deployment end-to-end.

    Refuses if the journal contains any unresolved prior runs (spec
    Section 12.7). Acquires the deployment lock, runs every stage, releases
    the lock in ``finally``. Never raises on operational failure; the
    outcome carries the terminal state.
    """
    _log.info(f"deploy: run={request.run_id!r} release={request.release_id!r}")

    unresolved = journal.unresolved_runs()
    if unresolved:
        message = f"unresolved prior deployment(s) require operator intervention: {unresolved}"
        _log.error(f"deploy: {message}")
        journal.append_stage(
            run_id=request.run_id,
            release_id=request.release_id,
            stage=Stage.FAILED,
            detail=f"preflight: {message}",
        )
        return DeployOutcome(
            success=False,
            release_id=request.release_id,
            stage="preflight",
            message=message,
            error_code=21,
        )

    try:
        request.lock.acquire(holder=request.run_id)
    except DeploymentLockHeldError as exc:
        return DeployOutcome(
            success=False,
            release_id=request.release_id,
            stage="lock",
            message=exc.message,
            error_code=25,
        )
    journal.append_stage(
        run_id=request.run_id,
        release_id=request.release_id,
        stage=Stage.LOCK_ACQUIRED,
        detail="",
    )

    try:
        return _run_stages(
            request,
            journal,
            service_control,
            health_probe,
            clock,
            pull_transport,
        )
    finally:
        try:
            request.lock.release()
        except Exception as exc:
            _log.warning(f"deploy: lock release failed: {exc}")
