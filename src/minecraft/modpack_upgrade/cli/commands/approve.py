# src/minecraft/modpack_upgrade/cli/commands/approve.py

"""``minecraft-upgrade approve <run-id>`` -- record an approval decision.

Records that the operator has approved the candidate for the given
scope. ``promote`` consults this record before staging the slip.

Default scope is ``mod_set`` (Phase B). Other scopes
(``world_migration``, ``velocity_integration``) require an explicit
``--scope`` argument.

Usage:

    minecraft-upgrade approve <run-id>
    minecraft-upgrade approve <run-id> --scope world_migration
    minecraft-upgrade approve <run-id> --operator alice
    minecraft-upgrade approve <run-id> --interaction-id <discord-id>

Exit codes:

  0  approval recorded
  2  invalid arguments
  3  state/slip missing or invalid
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
    """Record an approval for ``run_id`` and ``scope``."""
    return run_approval_command(
        decision="approved",
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
