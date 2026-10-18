# src/minecraft/modpack_upgrade/cli/commands/iterate.py

"""`minecraft-upgrade iterate --run-id <run-id>` -- boot loop (Section 15.1.6).

Refuses unless `state.status == materialized`. Delegates to the existing
Phase A boot loop (the "existing Section 8.2 step 16 onward" that the v1.3.1 spec
references). Boot-loop wiring lives in the upgrade controller module that
lands with the log classifier.
"""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.state import STATUS_MATERIALIZED, load_state

__all__ = ["run"]


def run(*, config_dir: Path, project_root: Path, run_id: str) -> int:
    """Refuse unless materialized; delegate boot loop to the Phase A controller."""
    del project_root
    daemon_config = load_daemon_config(config_dir)
    state_path = Path(daemon_config.state_dir) / "runs" / run_id / "state.toml"
    try:
        state = load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    if state.status != STATUS_MATERIALIZED:
        raise UpgradeCliError(
            3,
            f"run {run_id!r} has status {state.status!r}; iterate requires {STATUS_MATERIALIZED!r}",
        )

    raise UpgradeCliError(
        1,
        "iterate: boot-loop controller is not implemented in this build (spec Section 15.1.6 "
        "delegates to the Phase A boot loop delivered alongside the log classifier)",
    )
