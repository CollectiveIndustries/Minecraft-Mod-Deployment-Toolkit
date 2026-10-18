# src/minecraft/modpack_upgrade/state.py

"""Run state machine (Section 6.1, Section A6, Section 15.1.3).

`state.toml` is written by `update`, verified by `acknowledge` and
`materialize`, and updated by every step that advances the pipeline:

    resolved -> acknowledged -> materialized
    awaiting_acknowledgment -> acknowledged -> materialized
    failed                       (terminal)
    materialized -> failed       (iterate can flip it, Section 15.1.6)

Acceptance rules (Section 4 of Manifest_Schema):
    resolved / acknowledged  accepted by materialize
    awaiting_acknowledgment  refused by materialize (exit 6)
    materialized             accepted by iterate
    failed                   refused by everyone
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

try:
    import tomli_w
except ImportError as exc:  # pragma: no cover
    raise ImportError("tomli-w is required; install with `pdm add tomli-w`") from exc

from .errors import SchemaError

__all__ = [
    "STATUS_ACKNOWLEDGED",
    "STATUS_AWAITING_ACKNOWLEDGMENT",
    "STATUS_FAILED",
    "STATUS_MATERIALIZED",
    "STATUS_RESOLVED",
    "RunState",
    "load_state",
    "save_state",
]

STATUS_RESOLVED = "resolved"
STATUS_AWAITING_ACKNOWLEDGMENT = "awaiting_acknowledgment"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_MATERIALIZED = "materialized"
STATUS_FAILED = "failed"

_VALID_STATUSES = frozenset(
    {
        STATUS_RESOLVED,
        STATUS_AWAITING_ACKNOWLEDGMENT,
        STATUS_ACKNOWLEDGED,
        STATUS_MATERIALIZED,
        STATUS_FAILED,
    }
)

_REQUIRED_FIELDS = (
    "run_id",
    "status",
    "release_mode",
    "paused_at_utc",
    "report_sha256",
    "resolution_inputs_sha256",
    "manifest_path",
)


@dataclass(frozen=True)
class RunState:
    """One run's persisted state (Section 6.1)."""

    run_id: str
    status: str
    release_mode: str
    paused_at_utc: str
    report_sha256: str
    resolution_inputs_sha256: str
    manifest_path: str
    effective_compose_sha256: str | None = None  # only for release_mode == "update"
    reason: str | None = None  # Section 15.1.6: "ambiguous_failure" | "cap_exhausted"


def _to_toml_dict(state: RunState) -> dict:
    out: dict[str, object] = {
        "run_id": state.run_id,
        "status": state.status,
        "release_mode": state.release_mode,
        "paused_at_utc": state.paused_at_utc,
        "report_sha256": state.report_sha256,
        "resolution_inputs_sha256": state.resolution_inputs_sha256,
        "manifest_path": state.manifest_path,
    }
    if state.effective_compose_sha256 is not None:
        out["effective_compose_sha256"] = state.effective_compose_sha256
    if state.reason is not None:
        out["reason"] = state.reason
    return out


def load_state(path: Path) -> RunState:
    """Parse and validate `state.toml`. Raises `SchemaError` on any problem."""
    if not path.is_file():
        raise SchemaError(f"state.toml not found: {path}")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SchemaError(f"could not read {path}: {exc}") from exc

    for field in _REQUIRED_FIELDS:
        if field not in data:
            raise SchemaError(f"{path}: missing required field {field!r}")
    if data["status"] not in _VALID_STATUSES:
        raise SchemaError(f"{path}: invalid status {data['status']!r} (must be one of {sorted(_VALID_STATUSES)})")
    if data["release_mode"] not in ("update", "upgrade"):
        raise SchemaError(f"{path}: invalid release_mode {data['release_mode']!r}")
    if data["release_mode"] == "update" and "effective_compose_sha256" not in data:
        raise SchemaError(f"{path}: release_mode='update' requires effective_compose_sha256")

    return RunState(
        run_id=str(data["run_id"]),
        status=str(data["status"]),
        release_mode=str(data["release_mode"]),
        paused_at_utc=str(data["paused_at_utc"]),
        report_sha256=str(data["report_sha256"]),
        resolution_inputs_sha256=str(data["resolution_inputs_sha256"]),
        manifest_path=str(data["manifest_path"]),
        effective_compose_sha256=(str(data["effective_compose_sha256"]) if "effective_compose_sha256" in data else None),
        reason=(str(data["reason"]) if "reason" in data else None),
    )


def save_state(path: Path, state: RunState) -> None:
    """Serialize `state` to `path` as TOML, atomically (temp-then-replace)."""
    body = _to_toml_dict(state)
    text = tomli_w.dumps(body)
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
