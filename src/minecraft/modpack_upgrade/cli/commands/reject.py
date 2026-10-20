# src/minecraft/modpack_upgrade/cli/commands/reject.py

"""``minecraft-upgrade reject <run-id>`` -- record a rejection decision.

Records that the operator has rejected the candidate for the given
scope. ``promote`` refuses when a rejection is the current decision.

A ``--reason`` is strongly recommended. The reason is recorded in the
approval file and in the audit log for post-mortem.

Usage:

    minecraft-upgrade reject <run-id> --reason "Create 7 breaks KubeJS"
    minecraft-upgrade reject <run-id> --scope world_migration --reason "..."

Exit codes: same as ``approve``.
"""

from __future__ import annotations

from pathlib import Path

from ._approval_cli import run_approval_command

__all__ = ["run"]


def run(
    *,
    config_dir: Path,
    project_root: Path,
    run_id: str,
    scope: str = "mod_set",
    operator: str = "interactive-cli",
    interaction_id: str | None = None,
    reason: str | None = None,
    state_dir: Path | None = None,
    now: str | None = None,
) -> int:
    """Record a rejection for ``run_id`` and ``scope``."""
    return run_approval_command(
        decision="rejected",
        config_dir=config_dir,
        project_root=project_root,
        run_id=run_id,
        scope=scope,
        operator=operator,
        interaction_id=interaction_id,
        reason=reason,
        state_dir=state_dir,
        now=now,
    )
