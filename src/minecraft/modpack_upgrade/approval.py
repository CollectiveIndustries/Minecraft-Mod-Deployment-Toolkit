# src/minecraft/modpack_upgrade/approval.py

"""Approval records for gated transitions (spec v1.4.0 Section 8.3, Section 11.4).

Two concerns:

1. **Recording.** :func:`record_approval` writes
   ``runs/<run-id>/approval.<scope>.json`` (the current decision for a
   scope) and appends the same record to ``state_dir/approvals.jsonl``
   (the durable audit log).

2. **Verification.** :func:`verify_approval` checks that a record
   exists, its decision is ``approved``, it has not expired, and its
   binding hash matches the current candidate identity.

Scopes
------
- ``mod_set`` - Phase B approval after ``iterate`` stabilizes.
- ``world_migration`` - Phase C approval after the migration test.
- ``velocity_integration`` - Phase D approval after proxy verification.

Each scope has at most one *current* approval per run. Recording a
new approval for the same scope replaces the current file; the audit
log preserves history.

Replay protection
-----------------
Every Discord interaction has a unique ``interaction_id``. When
``record_approval`` is called with one, it first checks the audit log
for a prior record with that ID. If found, the previous record is
returned as-is (idempotent). If not, the new record is written and
logged. This makes replay of the same Discord click produce no side
effects.

Binding
-------
An approval is bound to a canonical identity hash over the candidate's
fingerprints. Any change to a bound field invalidates the approval at
verify time. The bound fields are the five that identify the artifact
set and its resolution evidence; optional fields (``snapshot_id``,
``validation_report_sha256``) are included when present.

Nonce (spec Section 11.4)
------------------
Every newly-recorded approval carries a server-generated UUID4 nonce
(:attr:`ApprovalRecord.nonce`). The nonce is a one-time, unguessable
identifier for this specific approval event; it is audit-correlated
data, not a candidate-identity field, so it does not participate in the
binding hash. Records written before Slice 15 T11 have
``nonce=None``; consumers must treat ``None`` as "unknown nonce" and
must not attempt to validate it.

Durability
----------
The audit log writer fsyncs after each record. Replay protection scans
this log via :func:`_lookup_interaction`; a silently-lost line would
allow a replayed Discord interaction to be accepted as a fresh
approval. The ``current-decision`` file at
``runs/<run-id>/approval.<scope>.json`` is written atomically via
temp-then-replace; unlike the audit log, it does not need a per-write
fsync because the atomic rename guarantees it either has the new
content or preserves the old, and the audit log is the durable record
of what happened.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from LoggingCore import get_logger

from . import canonical
from .errors import SchemaError
from .slip import PackingSlip, load_slip
from .state import RunState, load_state

__all__ = [
    "DEFAULT_EXPIRY_HOURS",
    "VALID_DECISIONS",
    "VALID_SCOPES",
    "ApprovalBinding",
    "ApprovalRecord",
    "build_binding_from_run",
    "load_approval",
    "record_approval",
    "verify_approval",
]


_log = get_logger(__name__)


VALID_SCOPES: tuple[str, ...] = (
    "mod_set",
    "world_migration",
    "velocity_integration",
)
VALID_DECISIONS: tuple[str, ...] = ("approved", "rejected")
DEFAULT_EXPIRY_HOURS: int = 7 * 24  # 7 days


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalBinding:
    """The identity fields an approval is bound to.

    These are the five required fields from spec Section 11.4 plus two
    optional fields present only for specific scopes (``snapshot_id``
    for world_migration, ``validation_report_sha256`` for velocity).
    The canonical hash is computed over exactly the fields present.
    """

    slip_content_sha256: str
    runtime_sha256: str
    server_inputs_sha256: str
    client_inputs_sha256: str
    resolution_report_sha256: str
    snapshot_id: str | None = None
    validation_report_sha256: str | None = None

    def to_canonical(self) -> dict[str, str]:
        """Return the canonical dict representation.

        Optional fields are omitted when ``None``, so two approvals with
        the same required fields but different presence of optional
        fields hash differently -- which is correct: ``snapshot_id``
        being bound vs. not bound are different approval statements.
        """
        out: dict[str, str] = {
            "slip_content_sha256": self.slip_content_sha256,
            "runtime_sha256": self.runtime_sha256,
            "server_inputs_sha256": self.server_inputs_sha256,
            "client_inputs_sha256": self.client_inputs_sha256,
            "resolution_report_sha256": self.resolution_report_sha256,
        }
        if self.snapshot_id is not None:
            out["snapshot_id"] = self.snapshot_id
        if self.validation_report_sha256 is not None:
            out["validation_report_sha256"] = self.validation_report_sha256
        return out

    def hash(self) -> str:
        """Return the RFC 8785 canonical SHA-256 of the binding."""
        return canonical.digest(self.to_canonical())


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRecord:
    """A recorded approval or rejection decision.

    ``schema_version`` is fixed at 1 for v1.4.0; a future schema change
    bumps it and the reader rejects unknown versions.

    ``nonce`` is the spec Section 11.4 one-time approval nonce. Populated by
    :func:`record_approval` on every newly-recorded approval. ``None``
    only on records written before Slice 15 T11. Placed last with a
    default so pre-existing records (which lack the field) still parse.
    """

    schema_version: int
    approval_id: str
    run_id: str
    scope: str
    decision: str
    binding_hash: str
    binding: dict[str, str]
    interaction_id: str | None
    signed_by: str
    signed_at_utc: str
    expires_at_utc: str
    reason: str | None
    nonce: str | None = None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _approval_path(run_dir: Path, scope: str) -> Path:
    """Return the current-approval file for ``scope`` under ``run_dir``."""
    return run_dir / f"approval.{scope}.json"


def _audit_log_path(state_dir: Path) -> Path:
    """Return the append-only audit log path."""
    return state_dir / "approvals.jsonl"


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string ending in ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_iso(now: str | None) -> str:
    """Return ``now`` if given, else the current UTC time as ISO-8601 Z."""
    if now is not None:
        return now
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_binding_from_run(run_dir: Path, state: RunState) -> ApprovalBinding:
    """Build the binding for ``run_dir``'s candidate.

    Raises ``SchemaError`` if the sealed slip is missing or invalid.
    The five required fields come from the slip's fingerprints plus
    ``state.report_sha256``. Optional fields (``snapshot_id``,
    ``validation_report_sha256``) are not populated by this function
    -- callers that need them construct ``ApprovalBinding`` directly.
    """
    slip_path = run_dir.parent.parent / "candidates" / state.run_id / "packing_slip.toml"
    if not slip_path.is_file():
        raise SchemaError(f"candidate slip not found: {slip_path}")
    slip: PackingSlip = load_slip(slip_path)
    return ApprovalBinding(
        slip_content_sha256=slip.content_sha256,
        runtime_sha256=slip.fingerprints.runtime_sha256,
        server_inputs_sha256=slip.fingerprints.server_inputs_sha256,
        client_inputs_sha256=slip.fingerprints.client_inputs_sha256,
        resolution_report_sha256=state.report_sha256,
    )


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


def _lookup_interaction(state_dir: Path, interaction_id: str) -> ApprovalRecord | None:
    """Return the record for ``interaction_id`` if seen, else ``None``.

    Reads the audit log sequentially. Malformed lines are skipped;
    an unreadable log is treated as "no replay found" -- the write path
    will fail loudly if the log cannot be appended.
    """
    log_path = _audit_log_path(state_dir)
    if not log_path.is_file():
        return None
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if data.get("interaction_id") == interaction_id:
                    try:
                        return ApprovalRecord(**data)
                    except TypeError:
                        continue
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _validate_scope_decision(scope: str, decision: str) -> None:
    """Raise ``ValueError`` if ``scope`` or ``decision`` is invalid."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {list(VALID_SCOPES)}, got {scope!r}")
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {list(VALID_DECISIONS)}, got {decision!r}")


def _write_current(run_dir: Path, scope: str, record: ApprovalRecord) -> None:
    """Write ``record`` as ``run_dir/approval.<scope>.json`` atomically."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = _approval_path(run_dir, scope)
    body = json.dumps(dataclasses.asdict(record), indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def _append_audit(state_dir: Path, record: ApprovalRecord) -> None:
    """Append ``record`` to the audit log and fsync.

    The fsync is load-bearing: :func:`_lookup_interaction` scans this
    log for interaction-ID replay protection, so a record lost between
    the write and the OS flush would silently allow a replayed Discord
    interaction to be accepted as a fresh approval. The prior
    implementation used a second ``f.flush()`` here, which is a no-op
    relative to the first ``flush()`` and does *not* push bytes to
    stable storage.
    """
    log_path = _audit_log_path(state_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dataclasses.asdict(record), sort_keys=True) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def record_approval(
    *,
    state_dir: Path,
    run_dir: Path,
    run_id: str,
    scope: str,
    decision: str,
    binding: ApprovalBinding,
    signed_by: str,
    interaction_id: str | None = None,
    reason: str | None = None,
    expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
    now: str | None = None,
    nonce: str | None = None,
) -> ApprovalRecord:
    """Record an approval decision for ``scope``.

    Idempotent on ``interaction_id``: if the ID has been seen, the
    previous record is returned without writing a new one. This makes
    Discord replay attacks harmless.

    Writes both:
      * ``run_dir/approval.<scope>.json`` -- the current decision
      * ``state_dir/approvals.jsonl``      -- the durable audit log

    ``nonce`` is the spec Section 11.4 one-time approval nonce. When ``None``
    (the default), a fresh UUID4 is generated and stored; callers who
    need to correlate externally (e.g. a future distributed-approval
    flow) may supply their own unguessable value.

    Raises ``ValueError`` on invalid scope/decision, ``OSError`` on
    write failure.
    """
    _validate_scope_decision(scope, decision)

    if interaction_id is not None:
        existing = _lookup_interaction(state_dir, interaction_id)
        if existing is not None:
            _log.debug(f"approval: replay of interaction_id={interaction_id!r} returning existing record {existing.approval_id}")
            return existing

    effective_nonce = nonce if nonce is not None else str(uuid.uuid4())

    signed_at = _now_iso(now)
    signed_dt = _parse_iso(signed_at)
    expires_at = (signed_dt + timedelta(hours=expires_in_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    record = ApprovalRecord(
        schema_version=1,
        approval_id=str(uuid.uuid4()),
        run_id=run_id,
        scope=scope,
        decision=decision,
        binding_hash=binding.hash(),
        binding=binding.to_canonical(),
        interaction_id=interaction_id,
        signed_by=signed_by,
        signed_at_utc=signed_at,
        expires_at_utc=expires_at,
        reason=reason,
        nonce=effective_nonce,
    )

    _write_current(run_dir, scope, record)
    _append_audit(state_dir, record)
    _log.info(f"approval: recorded {decision} for run={run_id!r} scope={scope!r} by {signed_by!r} (nonce={effective_nonce[:8]}...)")
    return record


# ---------------------------------------------------------------------------
# Loading and verification
# ---------------------------------------------------------------------------


def load_approval(run_dir: Path, scope: str) -> ApprovalRecord | None:
    """Return the current approval for ``scope``, or ``None`` if absent."""
    path = _approval_path(run_dir, scope)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != 1:
        return None
    try:
        return ApprovalRecord(**data)
    except TypeError:
        return None


def verify_approval(
    record: ApprovalRecord | None,
    *,
    expected_binding: ApprovalBinding,
    now: str | None = None,
) -> tuple[bool, str | None]:
    """Return ``(True, None)`` if ``record`` authorizes the current candidate.

    Checks, in order:

      1. ``record`` is not ``None``.
      2. ``record.decision == 'approved'``.
      3. ``record.binding_hash`` matches ``expected_binding.hash()``.
      4. ``record.expires_at_utc`` is in the future relative to ``now``.

    Failure returns ``(False, reason)`` with a human-readable reason
    that the caller may surface to the operator.
    """
    if record is None:
        return (False, "no approval record found")
    if record.schema_version != 1:
        return (False, f"unsupported approval schema_version {record.schema_version!r}")
    if record.decision != "approved":
        return (False, f"approval decision is {record.decision!r}, not 'approved'")
    if record.binding_hash != expected_binding.hash():
        return (False, "approval binding does not match current candidate identity")
    try:
        expires_dt = _parse_iso(record.expires_at_utc)
    except ValueError:
        return (False, f"approval expires_at_utc is malformed: {record.expires_at_utc!r}")
    now_dt = _parse_iso(_now_iso(now))
    if now_dt >= expires_dt:
        return (False, f"approval expired at {record.expires_at_utc}")
    return (True, None)


# ---------------------------------------------------------------------------
# Convenience: full pipeline
# ---------------------------------------------------------------------------


def load_state_for_run(state_dir: Path, run_id: str) -> RunState:
    """Load ``state.toml`` for ``run_id`` from ``state_dir``."""
    state_path = state_dir / "runs" / run_id / "state.toml"
    return load_state(state_path)
