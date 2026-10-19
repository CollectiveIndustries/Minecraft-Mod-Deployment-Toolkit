# src/minecraft/deploy_pack/daemon/recovery.py

"""Roll back a failed deployment (spec v1.3 Section 12.10, Section 12.11).

Two independent restore operations, composed by :func:`attempt_recovery`:

  * :func:`restore_world` - verify the world backup then replace the
    target world directory with its contents.
  * :func:`restore_mods` - re-ship the prior bundle into the target
    mods directory, using the same shipping transaction that a normal
    deployment uses.

The orchestrator invokes :func:`attempt_recovery` when a deployment
fails *after* the stop phase. Before the stop phase, no files have been
touched and recovery is unnecessary (see :mod:`deploy`).

What recovery does NOT do
-------------------------
Recovery restores **file state**. It does not restart containers, does
not re-run health checks, and does not decide whether the deployment is
now considered rolled back. The orchestrator owns the container
lifecycle and the final outcome; this module returns a structured
:class:`RecoveryOutcome` that the orchestrator consumes.

Prior bundle may be absent
--------------------------
On a first-ever deployment there is no prior bundle to restore mods
from. In that case ``prior_bundle_dir`` is ``None`` and
:func:`attempt_recovery` skips mods restore, leaving the target mods
directory in whatever state the shipping transaction left it (which
is the pre-transaction state on a shipping rollback, or the new state
on a post-ship failure). The orchestrator's failure outcome reports
this so the operator knows manual cleanup may be needed.

World backup may be absent
--------------------------
Similarly, non-upgrade deployments do not create a world backup, so
``world_backup_dir`` is ``None`` and world restore is skipped.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.slip import load_slip

from .backup import MANIFEST_FILENAME, verify_backup
from .journal import DeploymentJournal, Stage
from .ship import ship_release

__all__ = ["RecoveryOutcome", "attempt_recovery", "restore_mods", "restore_world"]


_log = get_logger(__name__)


@dataclass
class RecoveryOutcome:
    """Result of a recovery attempt.

    ``world_restored`` and ``mods_restored`` record which of the two
    restore operations actually ran and succeeded (they can be True
    together, individually, or neither if both inputs were ``None``).
    ``failure_message`` is non-``None`` only when ``success`` is ``False``.
    """

    success: bool
    world_restored: bool = False
    mods_restored: bool = False
    failure_message: str | None = None


def restore_world(*, backup_dir: Path, target_world: Path) -> tuple[bool, str | None]:
    """Replace ``target_world`` with the contents of ``backup_dir``.

    Verifies the backup before touching the target, so a corrupted
    backup never overwrites a working world. The manifest file is not
    copied into the target -- it belongs to the backup, not the world.

    Returns ``(True, None)`` on success, ``(False, error_message)`` on
    failure.
    """
    ok, err = verify_backup(backup_dir)
    if not ok:
        return (False, f"backup verification failed: {err}")
    try:
        if target_world.exists():
            shutil.rmtree(target_world)
        shutil.copytree(
            backup_dir,
            target_world,
            symlinks=False,
            ignore=shutil.ignore_patterns(MANIFEST_FILENAME),
        )
    except OSError as exc:
        return (False, f"world restore failed: {exc}")
    _log.info(f"recovery: world restored from {backup_dir} to {target_world}")
    return (True, None)


def restore_mods(
    *,
    prior_bundle_dir: Path,
    target_mods: Path,
    protect_patterns: tuple[str, ...],
    journal: DeploymentJournal,
    run_id: str,
    release_id: str,
) -> tuple[bool, str | None]:
    """Re-ship the prior bundle into ``target_mods``.

    Loads the prior bundle's slip and invokes the shipping transaction.
    The transaction is diff-aware, so re-shipping the prior bundle after
    a partial new-ship reverts the diff and re-establishes the prior
    inventory. Returns ``(True, None)`` on success, ``(False, error)`` on
    failure.
    """
    slip_path = prior_bundle_dir / "packing_slip.toml"
    if not slip_path.is_file():
        return (False, f"prior slip not found: {slip_path}")
    try:
        slip = load_slip(slip_path)
    except Exception as exc:
        return (False, f"prior slip unreadable: {exc}")

    result = ship_release(
        release_id=release_id,
        run_id=run_id,
        slip=slip,
        bundle_dir=prior_bundle_dir,
        target_dir=target_mods,
        protect_patterns=protect_patterns,
        journal=journal,
    )
    if not result.success:
        return (False, f"prior mods re-ship failed: {result.failure_message}")
    _log.info(f"recovery: mods re-shipped from {prior_bundle_dir} to {target_mods}")
    return (True, None)


def attempt_recovery(
    *,
    run_id: str,
    release_id: str,
    world_backup_dir: Path | None,
    target_world: Path | None,
    prior_bundle_dir: Path | None,
    target_mods: Path,
    protect_patterns: tuple[str, ...],
    journal: DeploymentJournal,
) -> RecoveryOutcome:
    """Restore world and mods as applicable.

    Both operations are attempted independently. A failure in one does
    not prevent the other from running: the operator benefits from
    seeing the world restored even if the mods re-ship failed (or vice
    versa).

    Journals ``RECOVERY_STARTED`` before either operation, then
    ``RECOVERY_COMPLETED`` or ``RECOVERY_FAILED``. The orchestrator
    always journals its own terminal ``FAILED`` stage afterwards, so
    the run is resolved either way.
    """
    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=Stage.RECOVERY_STARTED,
        detail="",
    )

    world_restored = False
    mods_restored = False
    errors: list[str] = []

    if world_backup_dir is not None and target_world is not None:
        ok, err = restore_world(backup_dir=world_backup_dir, target_world=target_world)
        world_restored = ok
        if not ok:
            errors.append(f"world: {err}")

    if prior_bundle_dir is not None:
        ok, err = restore_mods(
            prior_bundle_dir=prior_bundle_dir,
            target_mods=target_mods,
            protect_patterns=protect_patterns,
            journal=journal,
            run_id=run_id,
            release_id=release_id,
        )
        mods_restored = ok
        if not ok:
            errors.append(f"mods: {err}")

    outcome = RecoveryOutcome(
        success=not errors,
        world_restored=world_restored,
        mods_restored=mods_restored,
        failure_message=" | ".join(errors) if errors else None,
    )
    terminal = Stage.RECOVERY_COMPLETED if outcome.success else Stage.RECOVERY_FAILED
    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=terminal,
        detail=outcome.failure_message or "ok",
    )
    return outcome
