# src/minecraft/modpack_upgrade/cli/commands/acknowledge.py

"""``minecraft-upgrade acknowledge <run-id>`` -- human-decision gate (15.1.2).

Flow (6.4 of Resolution_Report):

  1. Load state.toml; verify status == awaiting_acknowledgment.
  2. Run 6.3 integrity checks.
  3. Present losses; require explicit acknowledgment, or accept with
     --accept-losses. Non-interactive without --accept-losses -> exit 6.
  4. Write acknowledgment.json.
  5. Update state.status = acknowledged.
  6. Print ``next: materialize --run-id <id>``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import load_compose
from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.integrity import IntegrityContext, check_integrity
from minecraft.modpack_upgrade.report import ResolutionReport, load_report
from minecraft.modpack_upgrade.state import (
    STATUS_ACKNOWLEDGED,
    STATUS_AWAITING_ACKNOWLEDGMENT,
    RunState,
    load_state,
    save_state,
)

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.acknowledge")


def _now_utc() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_compose_path(config_dir: Path, project_root: Path) -> Path:
    """Return the compose file path from ``[docker].compose_file`` or a default.

    Mirrors ``cli/commands/runtime._resolve_compose_file`` but returns the
    default without raising when ``deploy_pack.toml`` cannot be parsed, so a
    broken config surfaces at the integrity check rather than here.
    """
    toml_path = config_dir / "deploy_pack.toml"
    if not toml_path.is_file():
        return project_root / "docker-compose.yml"
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return project_root / "docker-compose.yml"
    docker = data.get("docker", {})
    if not isinstance(docker, dict):
        return project_root / "docker-compose.yml"
    raw = docker.get("compose_file")
    if not isinstance(raw, str) or not raw:
        return project_root / "docker-compose.yml"
    p = Path(raw)
    return p if p.is_absolute() else project_root / p


def _compute_compose_sha256(config_dir: Path, project_root: Path) -> str:
    """Return the SHA-256 of the effective compose file's raw bytes.

    Raises ``UpgradeCliError(3)`` when the compose file is missing or
    unreadable. 6.3 step 5 requires the caller to recompute this at both
    ``acknowledge`` and ``materialize`` time.
    """
    compose_path = _resolve_compose_path(config_dir, project_root)
    if not compose_path.is_file():
        raise UpgradeCliError(3, f"compose file not found: {compose_path}")
    try:
        data = compose_path.read_bytes()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read compose file {compose_path}: {exc}") from exc
    parsed = load_compose(compose_path)
    if not parsed.ok:
        raise UpgradeCliError(3, f"compose unreadable: {parsed.error}")
    return hashlib.sha256(data).hexdigest()


def _present_losses(report: ResolutionReport) -> None:
    """Emit the loss table the operator is being asked to accept."""
    losses = list(report.lost) + list(report.cascade)
    if not losses:
        _log_cli.info("No losses to acknowledge.")
        return
    _log_cli.info(f"{len(losses)} loss(es) require acknowledgment:")
    for entry in losses:
        kind = "cascade" if hasattr(entry, "dependency_chain") else "lost"
        _log_cli.info(f"  [{kind}] {entry.project_id or entry.filename}")
        if kind == "cascade":
            _log_cli.info(f"          chain: {' -> '.join(entry.dependency_chain)}")
        else:
            _log_cli.info(f"          reason: {entry.reason}")
            _log_cli.info(f"          detail: {entry.detail}")


def _get_confirmation(accept_losses: bool) -> bool:
    """Return True if the operator confirms acknowledgment."""
    if accept_losses:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("Acknowledge all losses and proceed to materialize? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _write_acknowledgment(
    path: Path,
    state: RunState,
    report: ResolutionReport,
    operator: str,
) -> None:
    """Serialize acknowledgment.json per 6.2."""
    losses: list[dict[str, str]] = []
    for entry in report.lost:
        losses.append({"project_id": entry.project_id, "reason": entry.reason})
    for entry in report.cascade:
        losses.append({"project_id": entry.project_id, "reason": entry.reason})
    body = {
        "schema_version": 1,
        "run_id": state.run_id,
        "report_sha256": state.report_sha256,
        "resolution_inputs_sha256": state.resolution_inputs_sha256,
        "acknowledged_losses": losses,
        "acknowledged_by": operator,
        "acknowledged_at_utc": _now_utc(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run(*, config_dir: Path, project_root: Path, run_id: str, accept_losses: bool, operator: str) -> int:
    """Execute the acknowledge command. Returns 0, 3, or 6."""
    daemon_config = load_daemon_config(config_dir)
    run_dir = Path(daemon_config.state_dir) / "runs" / run_id
    state_path = run_dir / "state.toml"

    try:
        state = load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    if state.status != STATUS_AWAITING_ACKNOWLEDGMENT:
        raise UpgradeCliError(
            3,
            f"run {run_id!r} has status {state.status!r}; acknowledge only accepts {STATUS_AWAITING_ACKNOWLEDGMENT!r}",
        )

    report_path = project_root / "www" / "runs" / run_id / "resolution-report.toml"
    effective_compose_sha: str | None = None
    if state.release_mode == "update":
        effective_compose_sha = _compute_compose_sha256(config_dir, project_root)

    integrity_ctx = IntegrityContext(
        config_dir=config_dir,
        modpack_dir=project_root / "sync" / "downloads",
        manifest_path=Path(state.manifest_path),
        report_path=report_path,
        effective_compose_sha256=effective_compose_sha,
    )

    # BUG-02: any schema violation in the current inputs (manifest, seed,
    # side_overrides) is an integrity failure -> exit 3, not exit 1.
    try:
        failures = check_integrity(state, integrity_ctx)
    except SchemaError as exc:
        raise UpgradeCliError(3, f"integrity check failed: {exc}") from exc
    if failures:
        raise UpgradeCliError(3, "integrity check failed:\n  - " + "\n  - ".join(failures))

    try:
        report = load_report(report_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, f"could not load report: {exc}") from exc

    _present_losses(report)
    if not _get_confirmation(accept_losses):
        return 6

    ack_path = run_dir / "acknowledgment.json"
    _write_acknowledgment(ack_path, state, report, operator)

    new_state = replace(state, status=STATUS_ACKNOWLEDGED)
    save_state(state_path, new_state)

    _log_cli.info("")
    _log_cli.info(f"acknowledgment written: {ack_path}")
    _log_cli.info(f"next: materialize --run-id {run_id}")
    return 0
