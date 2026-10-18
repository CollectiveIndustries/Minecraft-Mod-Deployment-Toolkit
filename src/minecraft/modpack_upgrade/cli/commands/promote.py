# src/minecraft/modpack_upgrade/cli/commands/promote.py

"""``minecraft-upgrade promote <tag>`` -- stage slip + metadata (15.1.7).

* Load ``state`` and ``packing_slip.toml`` from ``candidates/<run-id>/``.
* If release_mode == "upgrade": reconcile docker-compose.yml and update
  the slip's ``[inputs.git].compose_tree`` + recompute ``content_sha256``.
  (Slice 7b: this reconciliation is *scaffolded* -- it raises unless a
  no-op reconciliation is required; the real reconcile is a separate
  deliverable.)
* Copy slip to ``sync/packing_slips/<tag>.toml``.
* Copy candidate metadata ``.pw.toml`` files to ``sync/downloads/.index/``.
* ``git add`` those paths (compose too, when changed).
* Emit exact commit/tag/push instructions and STOP.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.slip import load_slip
from minecraft.modpack_upgrade.state import load_state

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.promote")


def _find_run_id_for_slip(candidate_root: Path) -> str | None:
    """Enumerate ``candidates/*/packing_slip.toml`` and return the run id.

    Promote takes a tag, not a run id. The tag names the *output* file.
    The input is the most recently materialized candidate. In slice 7b this
    command operates on the single candidate directory supplied on disk;
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


def _git_add(project_root: Path, paths: list[Path]) -> None:
    """Stage the given paths in the project git repo."""
    rel = [str(p.relative_to(project_root)) for p in paths]
    if not rel:
        return
    try:
        result = subprocess.run(
            ["git", "add", *rel],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpgradeCliError(3, f"git add failed: {exc}") from exc
    if result.returncode != 0:
        raise UpgradeCliError(3, f"git add failed: {result.stderr.strip()}")


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
        # load_slip is called for validation + hash verification; the parsed
        # PackingSlip object itself is not needed by promote beyond the check.
        load_slip(slip_path)
    except Exception as exc:
        raise UpgradeCliError(3, f"could not load candidate slip: {exc}") from exc

    if state.release_mode == "upgrade":
        raise UpgradeCliError(
            3,
            "release_mode='upgrade' requires docker-compose.yml reconciliation before promotion; the reconciliation step is not yet implemented in this build",
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

    _git_add(project_root, [dst_slip, *staged_metadata])

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
