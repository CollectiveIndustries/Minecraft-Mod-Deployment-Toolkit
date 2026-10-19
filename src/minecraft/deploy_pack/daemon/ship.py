# src/minecraft/deploy_pack/daemon/ship.py

"""Ship a release bundle into the target mods directory (spec v1.3 Section 13.2).

Wraps :func:`modpack_upgrade.shipping.ship` with two daemon-specific
additions:

  1. Build the source set from the slip (``[[jar]]`` entries whose
     ``side`` is ``server`` or ``both``), reading bytes from
     ``bundle_dir/artifacts/``.
  2. Verify each source's SHA-256 against the slip's recorded value
     *before* invoking the shipping transaction (spec Section 13.2 step 2).

The shipping transaction itself is unchanged: it stages, backs up,
applies, verifies, and (on failure) rolls back the resulting inventory.
Preflight checks from spec Section 13.2 step 4 -- "enabled JAR not in slip",
".jar.disabled", "nested directories", "unexpected non-JAR files" -- are
the caller's concern (``daemon/deploy.py``) and happen before
:func:`ship_release` is invoked.

Journal integration
-------------------
The journal records two stages per ship: ``SHIP_STARTED`` before the
transaction, and ``SHIP_COMPLETED`` or ``SHIP_FAILED`` after. The
transaction's own file-level journal (``shipping.py``) provides
intra-stage recovery detail; the deployment journal provides
inter-stage recovery. They serve different purposes and are kept
separate. Source-hash verification failures do NOT journal
``SHIP_FAILED`` -- the ship phase never started; the caller records
the terminal failure.

Idempotency
-----------
``shipping.ship`` is diff-aware: re-invoking it against an already-shipped
target performs no writes and returns all-unchanged. Combined with the
append-only journal (last write wins per stage), this makes
:func:`ship_release` safely re-invocable after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.files import compute_sha256
from minecraft.modpack_upgrade.errors import UpgradeError
from minecraft.modpack_upgrade.shipping import ShipRequest, ShipResult, ship
from minecraft.modpack_upgrade.slip import PackingSlip

from .errors import DeployCommandFailedError
from .journal import DeploymentJournal, Stage

__all__ = ["ShipOrchestrationResult", "build_source_set_from_slip", "ship_release"]


_log = get_logger(__name__)


@dataclass
class ShipOrchestrationResult:
    """Result of :func:`ship_release`."""

    success: bool
    release_id: str
    source_set: dict[str, Path]
    target_dir: Path
    ship_result: ShipResult | None = None
    failure_message: str | None = None


def build_source_set_from_slip(slip: PackingSlip, bundle_dir: Path) -> dict[str, Path]:
    """Return ``{filename: path}`` for every jar with side ``server`` or ``both``.

    The path is ``bundle_dir / "artifacts" / filename``. Existence is
    verified by :func:`ship_release` immediately after this call.
    """
    out: dict[str, Path] = {}
    for jar in slip.jars:
        if jar.side not in ("server", "both"):
            continue
        out[jar.filename] = bundle_dir / "artifacts" / jar.filename
    return out


def _verify_source_hashes(*, source_set: dict[str, Path], slip: PackingSlip) -> None:
    """Verify each source exists and hashes to the slip's recorded value.

    Raises :class:`DeployCommandFailedError` on any failure.
    """
    expected = {jar.filename: jar.sha256 for jar in slip.jars if jar.side in ("server", "both")}
    for filename, path in source_set.items():
        if not path.is_file():
            raise DeployCommandFailedError(
                f"ship: source artifact missing for {filename!r}",
                detail=f"expected at {path}",
            )
        actual = compute_sha256(path)
        want = expected[filename]
        if actual != want:
            raise DeployCommandFailedError(
                f"ship: source artifact hash mismatch for {filename!r}",
                detail=f"slip={want} actual={actual}",
            )
    _log.debug(f"ship: verified {len(source_set)} source artifact(s)")


def ship_release(
    *,
    release_id: str,
    run_id: str,
    slip: PackingSlip,
    bundle_dir: Path,
    target_dir: Path,
    protect_patterns: tuple[str, ...],
    journal: DeploymentJournal,
) -> ShipOrchestrationResult:
    """Ship a release bundle's server-side jars into ``target_dir``.

    Steps:

      1. Build source set from slip (side in {server, both}).
      2. Verify each source exists and hashes to the slip.
      3. Journal ``SHIP_STARTED``.
      4. Run the shipping transaction.
      5. Journal ``SHIP_COMPLETED`` (success) or ``SHIP_FAILED`` (failure).

    Returns :class:`ShipOrchestrationResult`. Never raises on shipping
    failure: the result carries the outcome and the journal records it.
    Source-hash verification failures return a failed result without
    journaling (the ship phase never started).

    ``protect_patterns`` is passed through to the shipping transaction
    unchanged (``fnmatch`` semantics, flat-filename scope per spec Section 13.5).
    """
    source_set = build_source_set_from_slip(slip, bundle_dir)
    _log.info(f"ship: release={release_id!r} run={run_id!r} sources={len(source_set)} target={target_dir}")

    try:
        _verify_source_hashes(source_set=source_set, slip=slip)
    except DeployCommandFailedError as exc:
        # Not a shipping-phase failure -- verification never reached the
        # transaction. The caller records the terminal failure.
        return ShipOrchestrationResult(
            success=False,
            release_id=release_id,
            source_set=source_set,
            target_dir=target_dir,
            ship_result=None,
            failure_message=exc.message,
        )

    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=Stage.SHIP_STARTED,
        detail=f"sources={len(source_set)} target={target_dir}",
    )

    try:
        ship_result = ship(
            ShipRequest(
                source_set=source_set,
                target_dir=target_dir,
                protect_patterns=protect_patterns,
            )
        )
    except (UpgradeError, OSError) as exc:
        journal.append_stage(
            run_id=run_id,
            release_id=release_id,
            stage=Stage.SHIP_FAILED,
            detail=str(exc),
        )
        _log.error(f"ship: transaction raised: {exc}")
        return ShipOrchestrationResult(
            success=False,
            release_id=release_id,
            source_set=source_set,
            target_dir=target_dir,
            ship_result=None,
            failure_message=str(exc),
        )

    if not ship_result.success:
        journal.append_stage(
            run_id=run_id,
            release_id=release_id,
            stage=Stage.SHIP_FAILED,
            detail=ship_result.failure_message or "shipping transaction reported failure",
        )
        return ShipOrchestrationResult(
            success=False,
            release_id=release_id,
            source_set=source_set,
            target_dir=target_dir,
            ship_result=ship_result,
            failure_message=ship_result.failure_message,
        )

    journal.append_stage(
        run_id=run_id,
        release_id=release_id,
        stage=Stage.SHIP_COMPLETED,
        detail=(f"added={len(ship_result.added)} updated={len(ship_result.updated)} removed={len(ship_result.removed)} unchanged={len(ship_result.unchanged)}"),
    )
    _log.info(f"ship: complete added={len(ship_result.added)} updated={len(ship_result.updated)} removed={len(ship_result.removed)}")
    return ShipOrchestrationResult(
        success=True,
        release_id=release_id,
        source_set=source_set,
        target_dir=target_dir,
        ship_result=ship_result,
    )
