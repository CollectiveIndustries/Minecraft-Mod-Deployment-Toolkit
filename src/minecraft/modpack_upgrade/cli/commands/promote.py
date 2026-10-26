# src/minecraft/modpack_upgrade/cli/commands/promote.py

"""``minecraft-upgrade promote <tag>`` - stage slip + metadata (15.1.7).

* Load ``state`` and ``packing_slip.toml`` from ``candidates/<run-id>/``.
* Enforce the mod-set approval gate. A valid, unexpired ``approved``
  record for scope ``mod_set`` bound to the sealed candidate's identity
  is required. The gate consumes the *pre-reconcile* slip: upgrade-mode
  reconciliation changes ``content_sha256`` and the approval is bound to
  the sealed candidate, not the post-reconcile release slip.
* If ``release_mode == "upgrade"``: reconcile docker-compose.yml against
  the slip's ``[runtime]``, update ``[inputs.git].compose_tree``, and
  recompute ``content_sha256``. The re-sealed slip is what gets copied to
  the release registry.
* Copy slip to ``sync/packing_slips/<tag>.toml``, re-sealing it with
  ``release_id = <tag>`` so the release slip's identity matches its
  filename and the release registry (Upgrade_System_specifications
  Section 5.2, Section 5.3 rule 1).
* Copy candidate metadata ``.pw.toml`` files to ``sync/downloads/.index/``.
* ``git add`` those paths (plus ``docker-compose.yml`` when reconciliation
  modified it).
* Emit exact commit/tag/push instructions and STOP.

Approval gate ordering
----------------------
The gate runs after overwrite safety but before any mutation. Ordering:

1. Load state and slip.
2. Snapshot the pre-reconcile slip for approval binding verification.
3. Reconcile docker-compose.yml (upgrade mode only; modifies the
   candidate slip in place).
4. Refuse to overwrite an existing release slip that differs from the
   candidate on disk.
5. Verify mod_set approval against the pre-reconcile slip.
6. Copy, re-seal, and stage.

Any ``UpgradeCliError(3)`` from the gate names the reason and points the
operator at ``minecraft-upgrade approve <run-id>``. Acceptance test T11
covers replay, expiry, and identity-change rejection at this gate.

Two distinct identities
-----------------------

The **candidate** slip at ``candidates/<run-id>/packing_slip.toml`` has
``release_id = <run-id>`` -- its identity is the resolver run that
produced it. The **release** slip at ``sync/packing_slips/<tag>.toml``
has ``release_id = <tag>`` -- its identity is the operator-chosen tag
that the release registry, publication, and Phase B approval all bind
to. ``promote`` is the boundary where one becomes the other.

Upgrade-mode reconciliation refreshes the candidate slip's
``compose_tree`` in place (Section 4.3 of the specs, deliberate
exception). The release slip's ``release_id`` refresh is a separate,
unconditional re-seal applied to the copy -- it happens for both update
and upgrade modes, and the candidate slip keeps ``release_id =
<run-id>`` regardless.

Git operations (``git add``, ``git hash-object``) delegate to
:mod:`PlatformCore.git`; see Turn C1 for the migration rationale.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import tomli_w
from LoggingCore import get_logger
from PlatformCore.git import hash_object, stage_paths

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.approval import (
    ApprovalBinding,
    load_approval,
    verify_approval,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.slip import load_slip
from minecraft.modpack_upgrade.state import RunState, load_state

from . import _compose

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.promote")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_add(project_root: Path, paths: list[Path]) -> None:
    """Stage the given paths in the project git repo.

    Path containment violations (a path that is not under ``project_root``)
    raise ``ValueError`` from ``Path.relative_to`` *before* git is invoked,
    matching the pre-migration behavior; that ``ValueError`` propagates
    uncaught and becomes exit 1 via main.py's generic exception handler.
    All git-level failures become ``UpgradeCliError(3)``.
    """
    rel = [str(p.relative_to(project_root)) for p in paths]
    if not rel:
        return
    try:
        stage_paths(rel, repo_path=str(project_root), timeout=30)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() if exc.stderr else str(exc)
        raise UpgradeCliError(3, f"git add failed: {stderr}") from exc
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpgradeCliError(3, f"git add failed: {exc}") from exc


def _git_blob_hash(project_root: Path, relpath: str) -> str:
    """Return the git blob hash of the file at ``relpath`` on disk.

    This is the hash that ``git add`` followed by ``git commit`` would
    produce, and what ``git rev-parse HEAD:<relpath>`` will return after
    commit. Distinct from ``git rev-parse HEAD:<relpath>`` which reads
    the *committed* blob (the "before commit" state).

    ``relpath`` is interpreted as repo-relative to ``project_root``.
    ``PlatformCore.git.hash_object`` computes the blob hash without
    writing to the object store (``write=False``).

    Raises :class:`UpgradeCliError(3)` on any failure. Promote is a
    staging step whose whole point is to prepare the git commit, so a
    missing git binary or a non-repo working directory is a hard error
    rather than a silent placeholder.
    """
    try:
        hash_value = hash_object(relpath, repo_path=str(project_root), timeout=10)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() if exc.stderr else str(exc)
        raise UpgradeCliError(3, f"git hash-object failed for {relpath}: {stderr}") from exc
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpgradeCliError(3, f"git hash-object unavailable for {relpath}: {exc}") from exc
    if not hash_value:
        raise UpgradeCliError(3, f"git hash-object returned empty output for {relpath}")
    return hash_value


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


def _require_mod_set_approval(
    *,
    state_dir: Path,
    run_id: str,
    slip: slip_mod.PackingSlip,
    state: RunState,
) -> None:
    """Raise :class:`UpgradeCliError(3)` when no valid mod_set approval exists.

    Verifies, in order, that an approval record exists for scope
    ``mod_set``, that its decision is ``approved``, that its binding
    hash matches the sealed candidate identity, and that its expiry is
    in the future. Any failure names the specific reason in the error
    message and points the operator at ``minecraft-upgrade approve
    <run-id>``.

    The binding is constructed from the *pre-reconcile* slip: upgrade
    mode's in-place reconciliation changes ``content_sha256``, so
    approval must be checked against the identity that was actually
    approved.
    """
    binding = ApprovalBinding(
        slip_content_sha256=slip.content_sha256,
        runtime_sha256=slip.fingerprints.runtime_sha256,
        server_inputs_sha256=slip.fingerprints.server_inputs_sha256,
        client_inputs_sha256=slip.fingerprints.client_inputs_sha256,
        resolution_report_sha256=state.report_sha256,
    )
    run_dir = state_dir / "runs" / run_id
    record = load_approval(run_dir, "mod_set")
    ok, reason = verify_approval(record, expected_binding=binding)
    if not ok:
        raise UpgradeCliError(
            3,
            f"approval gate: {reason}. Run `minecraft-upgrade approve {run_id}` first.",
        )


# ---------------------------------------------------------------------------
# Slip helpers
# ---------------------------------------------------------------------------


def _write_slip_atomic(path: Path, raw: dict) -> None:
    """Write ``raw`` to ``path`` as TOML atomically."""
    text = tomli_w.dumps(raw)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _reseal_release_slip(dst_slip: Path, tag: str) -> None:
    """Re-seal ``dst_slip`` with ``release_id = tag``.

    The candidate slip uses ``release_id = <run-id>``; the release slip
    must use ``release_id = <tag>`` so its identity matches the release
    registry (Upgrade_System_specifications Section 5.2 and Section 5.3
    rule 1). Called after the copy, only when the two identities differ
    (idempotent when the operator re-runs promote with an already-tagged
    release).

    The re-seal updates ``release_id`` and recomputes ``content_sha256``;
    no other fields are touched. ``parse_slip`` re-validates as
    defence-in-depth before the write.
    """
    raw = tomllib.loads(dst_slip.read_text(encoding="utf-8"))
    if raw.get("release_id") == tag:
        return
    raw["release_id"] = tag
    new_content_sha = slip_mod.compute_content_sha256(raw)
    raw["content_sha256"] = new_content_sha
    slip_mod.parse_slip(raw)  # schema validation; defensive
    _write_slip_atomic(dst_slip, raw)
    _log_cli.info(f"re-sealed release slip: release_id={tag!r} content_sha256={new_content_sha[:12]}...")


def _reconcile_for_upgrade(
    *,
    slip: slip_mod.PackingSlip,
    slip_path: Path,
    config_dir: Path,
    project_root: Path,
) -> tuple[slip_mod.PackingSlip, Path | None]:
    """Reconcile docker-compose.yml against slip [runtime]; re-seal slip if needed.

    Returns ``(slip, compose_to_stage)``. ``compose_to_stage`` is non-None
    iff the compose file was actually modified and needs to be
    ``git add``-ed by the caller.

    The slip's ``[inputs.git].compose_tree`` is updated to reflect the
    working-tree blob hash of the compose file, even when no runtime
    change was needed. This handles the case where the operator edited
    the compose file (comments, unrelated keys) between materialize and
    promote but did not change the runtime values - the slip must still
    record what will actually be committed.
    """
    target = _compose.ComposeRuntime(
        minecraft_version=slip.runtime.minecraft_version,
        loader=slip.runtime.loader_type,
        loader_version=slip.runtime.loader_version,
    )

    _log_cli.info("reconciling docker-compose.yml against slip [runtime]...")
    diff = _compose.reconcile_compose(config_dir, project_root, target)
    compose_changed = diff.needed
    if compose_changed:
        for change in diff.changes:
            if change.new_value is None:
                _log_cli.info(f"  remove {change.key}={change.old_value!r}")
            elif change.old_value is None:
                _log_cli.info(f"  add {change.key}={change.new_value!r}")
            else:
                _log_cli.info(f"  update {change.key}: {change.old_value!r} -> {change.new_value!r}")
        _compose.apply_compose_diff(diff)
    else:
        _log_cli.info("  (no changes needed)")

    try:
        compose_relpath = str(diff.compose_path.relative_to(project_root))
    except ValueError as exc:
        raise UpgradeCliError(
            3,
            f"compose file is outside the project root: {diff.compose_path}",
        ) from exc
    new_compose_tree = _git_blob_hash(project_root, compose_relpath)

    if not compose_changed and new_compose_tree == slip.inputs_git.compose_tree:
        _log_cli.info("  (slip [inputs.git].compose_tree already current)")
        return slip, None

    # Re-seal: update compose_tree, recompute content_sha256, atomic write.
    raw = tomllib.loads(slip_path.read_text(encoding="utf-8"))
    raw["inputs"]["git"]["compose_tree"] = new_compose_tree
    new_content_sha = slip_mod.compute_content_sha256(raw)
    raw["content_sha256"] = new_content_sha
    slip_mod.parse_slip(raw)  # schema validation; defensive
    _write_slip_atomic(slip_path, raw)

    _log_cli.info(f"  re-sealed candidate slip: compose_tree={new_compose_tree[:12]}... content_sha256={new_content_sha[:12]}...")
    reloaded = slip_mod.load_slip(slip_path)
    return reloaded, (diff.compose_path if compose_changed else None)


# ---------------------------------------------------------------------------
# Candidate lookup
# ---------------------------------------------------------------------------


def _find_run_id_for_slip(candidate_root: Path) -> str | None:
    """Enumerate ``candidates/*/packing_slip.toml`` and return the run id.

    Promote takes a tag, not a run id. The tag names the *output* file.
    The input is the most recently materialized candidate. This build
    operates on the single candidate directory supplied on disk;
    multi-candidate selection lands with the operator workflow.
    """
    if not candidate_root.is_dir():
        return None
    candidates = [p.name for p in candidate_root.iterdir() if (p / "packing_slip.toml").is_file()]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise UpgradeCliError(
            3,
            f"multiple materialized candidates under {candidate_root}: {candidates}; promote needs a selection mechanism",
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(*, config_dir: Path, project_root: Path, tag: str) -> int:
    """Stage a candidate slip under ``<tag>`` and emit instructions. Returns 0 or 3."""
    daemon_config = load_daemon_config(config_dir)
    candidate_store = Path(daemon_config.state_dir) / "candidates"

    run_id = _find_run_id_for_slip(candidate_store)
    if run_id is None:
        raise UpgradeCliError(3, f"no materialized candidate under {candidate_store}")

    candidate_dir = candidate_store / run_id
    state_path = Path(daemon_config.state_dir) / "runs" / run_id / "state.toml"
    try:
        state = load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    slip_path = candidate_dir / "packing_slip.toml"
    try:
        slip = load_slip(slip_path)
    except Exception as exc:
        raise UpgradeCliError(3, f"could not load candidate slip: {exc}") from exc

    # Snapshot the pre-reconcile slip for approval binding verification.
    original_slip = slip

    # Upgrade mode: reconcile compose, re-seal candidate slip if needed.
    compose_to_stage: Path | None = None
    if state.release_mode == "upgrade":
        slip, compose_to_stage = _reconcile_for_upgrade(
            slip=slip,
            slip_path=slip_path,
            config_dir=config_dir,
            project_root=project_root,
        )

    slip_dir = Path(daemon_config.slip_dir)
    slip_dir.mkdir(parents=True, exist_ok=True)
    dst_slip = slip_dir / f"{tag}.toml"
    if dst_slip.exists() and dst_slip.read_bytes() != slip_path.read_bytes():
        raise UpgradeCliError(3, f"refusing to overwrite existing different slip at {dst_slip}")

    # Approval gate: consumed pre-reconcile slip identity.
    _require_mod_set_approval(
        state_dir=Path(daemon_config.state_dir),
        run_id=run_id,
        slip=original_slip,
        state=state,
    )

    shutil.copy2(slip_path, dst_slip)
    _log_cli.info(f"copied slip: {slip_path} -> {dst_slip}")

    # Release slip carries the tag as release_id (Section 5.2, Section 5.3
    # rule 1). The candidate slip keeps release_id = <run-id> -- its
    # identity is the resolver run, not the operator-chosen tag.
    _reseal_release_slip(dst_slip, tag)

    metadata_dir = candidate_dir / "metadata"
    index_dir = project_root / "sync" / "downloads" / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    staged_metadata: list[Path] = []
    if metadata_dir.is_dir():
        for src in sorted(metadata_dir.glob("*.pw.toml")):
            dst = index_dir / src.name
            shutil.copy2(src, dst)
            staged_metadata.append(dst)
            _log_cli.info(f"staged metadata: {src.name} -> {dst}")

    paths_to_stage: list[Path] = [dst_slip, *staged_metadata]
    if compose_to_stage is not None:
        paths_to_stage.append(compose_to_stage)
    _git_add(project_root, paths_to_stage)

    _log_cli.info("")
    _log_cli.info("promote staged. Next steps (human runs these):")
    _log_cli.info("")
    _log_cli.info(f'    git commit -m "Release {tag}: <summary>"')
    _log_cli.info(f'    git tag -a {tag} -m "Shipping label"')
    _log_cli.info("    git push origin main --tags")
    _log_cli.info("")
    _log_cli.info(f'    echo "{tag}.toml" > sync/packing_slips/stable')
    _log_cli.info("    git add sync/packing_slips/stable")
    _log_cli.info(f'    git commit -m "Mark {tag} stable"')
    _log_cli.info("    git push")
    _log_cli.info("")
    _log_cli.info(f"after tag push, publish with: minecraft-upgrade publish {tag}")
    return 0
