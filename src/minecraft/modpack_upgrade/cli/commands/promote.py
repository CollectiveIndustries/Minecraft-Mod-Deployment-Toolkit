# src/minecraft/modpack_upgrade/cli/commands/promote.py

"""``minecraft-upgrade promote <tag>`` -- stage slip + metadata (15.1.7).

* Load ``state`` and ``packing_slip.toml`` from ``candidates/<run-id>/``.
* If ``release_mode == "upgrade"``: reconcile docker-compose.yml against
  the slip's ``[runtime]``, update ``[inputs.git].compose_tree``, and
  recompute ``content_sha256``. The re-sealed slip is what gets copied to
  the release registry.
* Copy slip to ``sync/packing_slips/<tag>.toml``.
* Copy candidate metadata ``.pw.toml`` files to ``sync/downloads/.index/``.
* ``git add`` those paths (plus ``docker-compose.yml`` when reconciliation
  modified it).
* Emit exact commit/tag/push instructions and STOP.

The upgrade-mode reconciliation invalidates the sealed candidate slip's
``content_sha256`` identity by construction - the slip is re-sealed with
a new ``compose_tree`` and a new ``content_sha256``. This happens before
the slip is copied to the release registry, so the release slip is what
was actually tested against the reconciled compose. A spec amendment
note accompanies this behavior in ``Upgrade_System_specifications.md``
Section 4.3 and ``Resolution_Report.md`` Section 8.1.

Git operations (``git add``, ``git hash-object``) delegate to
:mod:`PlatformCore.git` so the shared repository API stays the single
source of truth for path normalization, `--` separators, and timeout
pass-through. This module is a thin application adapter that translates
``PlatformCore.git``'s exceptions into the CLI's ``UpgradeCliError(3)``
so that exit-code semantics stay with the application.
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
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.slip import load_slip
from minecraft.modpack_upgrade.state import load_state

from . import _compose

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.promote")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
#
# Thin application adapters over PlatformCore.git. The shared layer owns
# path normalization, the ``--`` separator, and timeout pass-through; this
# layer owns the CLI contract -- specifically, that any git failure
# becomes ``UpgradeCliError(3)`` so main.py's dispatcher returns the right
# exit code. The individual message strings are preserved from the
# pre-migration implementation so operator-facing text does not regress.


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
    rather than a silent placeholder. Contrast with
    ``materialize._git_tree_hash``, which fails open because materialize
    is useful even without git (the tree hashes are descriptive).
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
# Slip helpers
# ---------------------------------------------------------------------------


def _write_slip_atomic(path: Path, raw: dict) -> None:
    """Write ``raw`` to ``path`` as TOML atomically."""
    text = tomli_w.dumps(raw)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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

    # Upgrade mode: reconcile compose, re-seal slip if needed.
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
    shutil.copy2(slip_path, dst_slip)
    _log_cli.info(f"copied slip: {slip_path} -> {dst_slip}")

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
