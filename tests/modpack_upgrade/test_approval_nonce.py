# tests/modpack_upgrade/test_approval_nonce.py

"""Tests for the ``ApprovalRecord.nonce`` field (Slice 15 T11, spec Section 11.4).

The nonce is a one-time, server-generated identifier for a specific
approval event. It does NOT participate in the binding hash (which covers
candidate identity); it exists as audit-correlated data so downstream
systems can name a specific approval occurrence.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from minecraft.modpack_upgrade.approval import (
    ApprovalBinding,
    ApprovalRecord,
    load_approval,
    record_approval,
)


def _binding() -> ApprovalBinding:
    """Return a minimal valid binding for tests."""
    return ApprovalBinding(
        slip_content_sha256="a" * 64,
        runtime_sha256="b" * 64,
        server_inputs_sha256="c" * 64,
        client_inputs_sha256="d" * 64,
        resolution_report_sha256="e" * 64,
    )


def _record(state_dir: Path, run_dir: Path, **kwargs) -> ApprovalRecord:
    """Record an approval with sensible defaults."""
    defaults = {
        "state_dir": state_dir,
        "run_dir": run_dir,
        "run_id": "run-1",
        "scope": "mod_set",
        "decision": "approved",
        "binding": _binding(),
        "signed_by": "test",
    }
    defaults.update(kwargs)
    return record_approval(**defaults)


# ---------------------------------------------------------------------------
# Field presence
# ---------------------------------------------------------------------------


def test_new_record_has_nonce(tmp_path: Path) -> None:
    """A newly-recorded approval carries a nonce."""
    record = _record(tmp_path / "state", tmp_path / "runs" / "run-1")
    assert record.nonce is not None
    assert isinstance(record.nonce, str)
    assert len(record.nonce) >= 32  # UUID4 str is 36 chars


def test_nonce_is_uuid4_shaped(tmp_path: Path) -> None:
    """The auto-generated nonce parses as a UUID."""
    record = _record(tmp_path / "state", tmp_path / "runs" / "run-1")
    parsed = uuid.UUID(record.nonce)  # raises if not a valid UUID
    assert parsed.version == 4


def test_caller_supplied_nonce_is_preserved(tmp_path: Path) -> None:
    """When the caller supplies a nonce, it is used verbatim."""
    custom = "caller-supplied-nonce-abc"
    record = _record(tmp_path / "state", tmp_path / "runs" / "run-1", nonce=custom)
    assert record.nonce == custom


def test_each_approval_gets_a_distinct_nonce(tmp_path: Path) -> None:
    """Two approvals for different interactions produce distinct nonces."""
    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    r1 = _record(state_dir, run_dir, interaction_id="i-1", scope="mod_set")
    r2 = _record(state_dir, run_dir, interaction_id="i-2", scope="world_migration")
    assert r1.nonce != r2.nonce


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_nonce_round_trips_through_current_file(tmp_path: Path) -> None:
    """The current-approval file preserves the nonce on load."""
    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    recorded = _record(state_dir, run_dir)
    loaded = load_approval(run_dir, "mod_set")
    assert loaded is not None
    assert loaded.nonce == recorded.nonce


def test_nonce_round_trips_through_audit_log(tmp_path: Path) -> None:
    """The audit log JSON line contains the nonce verbatim."""
    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    recorded = _record(state_dir, run_dir)
    audit_lines = (state_dir / "approvals.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    parsed = json.loads(audit_lines[0])
    assert parsed["nonce"] == recorded.nonce


# ---------------------------------------------------------------------------
# Replay interaction
# ---------------------------------------------------------------------------


def test_replay_returns_identical_nonce(tmp_path: Path) -> None:
    """Replay of the same interaction returns the same record, nonce included."""
    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    first = _record(state_dir, run_dir, interaction_id="i-1")
    second = _record(state_dir, run_dir, interaction_id="i-1")
    assert first.nonce == second.nonce
    assert first.approval_id == second.approval_id


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_legacy_record_without_nonce_loads(tmp_path: Path) -> None:
    """A pre-T11 record (no ``nonce`` field) loads with ``nonce=None``.

    Backward compatibility contract: the audit log is durable, so records
    written before the nonce existed must remain readable after the field
    is added. The default value on the dataclass is what makes this work.
    """
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    # Write a legacy-shaped record directly to the current-approval file.
    legacy_body = {
        "schema_version": 1,
        "approval_id": "legacy-approval",
        "run_id": "run-1",
        "scope": "mod_set",
        "decision": "approved",
        "binding_hash": "0" * 64,
        "binding": {},
        "interaction_id": None,
        "signed_by": "legacy-cli",
        "signed_at_utc": "2026-01-01T00:00:00Z",
        "expires_at_utc": "2026-01-08T00:00:00Z",
        "reason": None,
        # Deliberately no "nonce" key.
    }
    (run_dir / "approval.mod_set.json").write_text(json.dumps(legacy_body), encoding="utf-8")

    loaded = load_approval(run_dir, "mod_set")
    assert loaded is not None
    assert loaded.nonce is None
