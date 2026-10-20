# src/minecraft/modpack_upgrade/cli/commands/_approval_cli.py

"""Shared implementation for the ``approve`` and ``reject`` commands.

Both commands:

1. Load the run's ``state.toml``.
2. Load the run's sealed candidate slip (from ``candidates/<run-id>/``).
3. Build an :class:`ApprovalBinding` from the slip and state.
4. Record the decision via :func:`approval.record_approval`.
5. Print a summary.

Exit codes:

  0  success
  2  invalid arguments (bad scope, bad interaction-id)
  3  state/slip missing or invalid
"""

from __future__ import annotations

from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.approval import (
    VALID_SCOPES,
    ApprovalBinding,
    ApprovalRecord,
    record_approval,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.slip import PackingSlip, load_slip
from minecraft.modpack_upgrade.state import RunState, load_state

__all__ = ["run_approval_command"]


_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands._approval_cli")


def _resolve_state_dir(config_dir: Path, state_dir: Path | None) -> Path:
    """Return the state dir from ``state_dir`` or from daemon config."""
    if state_dir is not None:
        return state_dir
    return Path(load_daemon_config(config_dir).state_dir)


def _load_state_or_raise(run_dir: Path) -> RunState:
    """Load ``state.toml`` from ``run_dir``, translating errors."""
    state_path = run_dir / "state.toml"
    if not state_path.is_file():
        raise UpgradeCliError(3, f"state.toml not found: {state_path}")
    try:
        return load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc


def _load_slip_or_raise(candidate_dir: Path, run_id: str) -> PackingSlip:
    """Load the sealed candidate slip, translating errors."""
    slip_path = candidate_dir / "packing_slip.toml"
    if not slip_path.is_file():
        raise UpgradeCliError(
            3,
            f"candidate slip not found: {slip_path}. Run `minecraft-upgrade materialize --run-id {run_id}` first.",
        )
    try:
        return load_slip(slip_path)
    except Exception as exc:
        raise UpgradeCliError(3, f"could not load candidate slip: {exc}") from exc


def _build_binding(slip: PackingSlip, state: RunState) -> ApprovalBinding:
    """Construct the five-field binding from slip and state."""
    return ApprovalBinding(
        slip_content_sha256=slip.content_sha256,
        runtime_sha256=slip.fingerprints.runtime_sha256,
        server_inputs_sha256=slip.fingerprints.server_inputs_sha256,
        client_inputs_sha256=slip.fingerprints.client_inputs_sha256,
        resolution_report_sha256=state.report_sha256,
    )


def _validate_cli_inputs(scope: str, interaction_id: str | None) -> None:
    """Validate CLI-side arguments before touching the filesystem."""
    if scope not in VALID_SCOPES:
        raise UpgradeCliError(
            2,
            f"--scope must be one of {list(VALID_SCOPES)}, got {scope!r}",
        )
    if interaction_id is not None and not interaction_id.strip():
        raise UpgradeCliError(2, "--interaction-id must be non-empty when supplied")


def _print_record(record: ApprovalRecord) -> None:
    """Print the recorded approval to the CLI channel."""
    _log_cli.info(f"approval recorded: {record.approval_id}")
    _log_cli.info(f"  run_id: {record.run_id}")
    _log_cli.info(f"  scope: {record.scope}")
    _log_cli.info(f"  decision: {record.decision}")
    _log_cli.info(f"  signed_by: {record.signed_by}")
    _log_cli.info(f"  signed_at_utc: {record.signed_at_utc}")
    _log_cli.info(f"  expires_at_utc: {record.expires_at_utc}")
    _log_cli.info(f"  binding_hash: {record.binding_hash}")
    if record.reason:
        _log_cli.info(f"  reason: {record.reason}")
    if record.interaction_id:
        _log_cli.info(f"  interaction_id: {record.interaction_id}")


def run_approval_command(
    *,
    decision: str,
    config_dir: Path,
    project_root: Path,
    run_id: str,
    scope: str,
    operator: str,
    interaction_id: str | None,
    reason: str | None,
    state_dir: Path | None,
    now: str | None,
) -> int:
    """Record an approve or reject decision. Returns exit code.

    ``decision`` must be ``"approved"`` or ``"rejected"`` -- callers
    pass the fixed literal; it comes from which CLI command was invoked.
    """
    del project_root  # paths come from config_dir; kept for signature symmetry
    _validate_cli_inputs(scope, interaction_id)

    effective_state_dir = _resolve_state_dir(config_dir, state_dir)
    run_dir = effective_state_dir / "runs" / run_id
    candidate_dir = effective_state_dir / "candidates" / run_id

    state = _load_state_or_raise(run_dir)
    slip = _load_slip_or_raise(candidate_dir, run_id)
    binding = _build_binding(slip, state)

    try:
        record = record_approval(
            state_dir=effective_state_dir,
            run_dir=run_dir,
            run_id=run_id,
            scope=scope,
            decision=decision,
            binding=binding,
            signed_by=operator,
            interaction_id=interaction_id,
            reason=reason,
            now=now,
        )
    except ValueError as exc:
        raise UpgradeCliError(2, str(exc)) from exc
    except OSError as exc:
        raise UpgradeCliError(3, f"could not write approval record: {exc}") from exc

    _print_record(record)
    return 0
