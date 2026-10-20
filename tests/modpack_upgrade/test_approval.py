# tests/modpack_upgrade/test_approval.py

"""Unit tests for ``modpack_upgrade.approval``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.approval import (
    ApprovalBinding,
    ApprovalRecord,
    load_approval,
    record_approval,
    verify_approval,
)


def _binding(**overrides: object) -> ApprovalBinding:
    """Return a five-field binding with placeholder hashes."""
    base: dict[str, object] = {
        "slip_content_sha256": "a" * 64,
        "runtime_sha256": "b" * 64,
        "server_inputs_sha256": "c" * 64,
        "client_inputs_sha256": "d" * 64,
        "resolution_report_sha256": "e" * 64,
    }
    base.update(overrides)
    return ApprovalBinding(**base)  # type: ignore[arg-type]


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(state_dir, run_dir)`` under ``tmp_path``."""
    state_dir = tmp_path / "state"
    run_dir = state_dir / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    return (state_dir, run_dir)


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_binding_hash_is_deterministic() -> None:
    """The same binding hashes to the same value across calls."""
    b = _binding()
    assert b.hash() == b.hash()


def test_binding_hash_differs_on_field_change() -> None:
    """Changing any required field changes the hash."""
    base = _binding().hash()
    for field_name in (
        "slip_content_sha256",
        "runtime_sha256",
        "server_inputs_sha256",
        "client_inputs_sha256",
        "resolution_report_sha256",
    ):
        mutated = _binding(**{field_name: "f" * 64})
        assert mutated.hash() != base, f"changing {field_name} did not change hash"


def test_binding_optional_snapshot_changes_hash() -> None:
    """Adding a snapshot_id changes the hash."""
    without = _binding().hash()
    with_snapshot = _binding(snapshot_id="snap-1").hash()
    assert without != with_snapshot


def test_binding_optional_validation_report_changes_hash() -> None:
    """Adding a validation_report_sha256 changes the hash."""
    without = _binding().hash()
    with_val = _binding(validation_report_sha256="f" * 64).hash()
    assert without != with_val


def test_binding_optional_fields_omitted_from_canonical() -> None:
    """Optional fields absent from to_canonical when None."""
    canonical = _binding().to_canonical()
    assert "snapshot_id" not in canonical
    assert "validation_report_sha256" not in canonical


# ---------------------------------------------------------------------------
# record_approval
# ---------------------------------------------------------------------------


def test_record_approval_writes_current_and_audit(tmp_path: Path) -> None:
    """A new approval writes both the current file and the audit log."""
    state_dir, run_dir = _paths(tmp_path)
    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
    )
    assert (run_dir / "approval.mod_set.json").is_file()
    assert (state_dir / "approvals.jsonl").is_file()
    assert record.decision == "approved"
    assert record.signed_by == "alice"


def test_record_approval_sets_expiry_seven_days(tmp_path: Path) -> None:
    """Default expiry is seven days from signing."""
    state_dir, run_dir = _paths(tmp_path)
    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
        now="2026-01-01T00:00:00Z",
    )
    assert record.expires_at_utc == "2026-01-08T00:00:00Z"


def test_record_approval_custom_expiry(tmp_path: Path) -> None:
    """A custom ``expires_in_hours`` is honoured."""
    state_dir, run_dir = _paths(tmp_path)
    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
        expires_in_hours=1,
        now="2026-01-01T00:00:00Z",
    )
    assert record.expires_at_utc == "2026-01-01T01:00:00Z"


def test_record_approval_writes_binding_hash(tmp_path: Path) -> None:
    """The record carries the binding's canonical hash."""
    state_dir, run_dir = _paths(tmp_path)
    canonical_binding = _binding()
    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=canonical_binding,
        signed_by="alice",
    )
    assert record.binding_hash == canonical_binding.hash()


def test_record_approval_invalid_scope_raises(tmp_path: Path) -> None:
    """An invalid scope raises ValueError."""
    state_dir, run_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        record_approval(
            state_dir=state_dir,
            run_dir=run_dir,
            run_id="run-1",
            scope="bogus",
            decision="approved",
            binding=_binding(),
            signed_by="alice",
        )


def test_record_approval_invalid_decision_raises(tmp_path: Path) -> None:
    """An invalid decision raises ValueError."""
    state_dir, run_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="decision"):
        record_approval(
            state_dir=state_dir,
            run_dir=run_dir,
            run_id="run-1",
            scope="mod_set",
            decision="bogus",
            binding=_binding(),
            signed_by="alice",
        )


def test_record_approval_replacement_overwrites_current(tmp_path: Path) -> None:
    """A second approval for the same scope replaces the current file."""
    state_dir, run_dir = _paths(tmp_path)
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
    )
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="rejected",
        binding=_binding(),
        signed_by="bob",
        reason="not ready",
    )
    current = json.loads((run_dir / "approval.mod_set.json").read_text())
    assert current["decision"] == "rejected"
    assert current["signed_by"] == "bob"


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


def test_record_approval_replay_returns_existing(tmp_path: Path) -> None:
    """The same interaction_id returns the first record, not a new one."""
    state_dir, run_dir = _paths(tmp_path)
    first = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
        interaction_id="interaction-1",
    )
    second = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="rejected",
        binding=_binding(),
        signed_by="bob",
        interaction_id="interaction-1",
    )
    assert first.approval_id == second.approval_id
    assert second.decision == "approved"
    log_lines = [line for line in (state_dir / "approvals.jsonl").read_text().splitlines() if line.strip()]
    assert len(log_lines) == 1


def test_record_approval_different_interactions_both_recorded(tmp_path: Path) -> None:
    """Distinct interaction IDs produce distinct records."""
    state_dir, run_dir = _paths(tmp_path)
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
        interaction_id="interaction-1",
    )
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="rejected",
        binding=_binding(),
        signed_by="bob",
        interaction_id="interaction-2",
    )
    log_lines = [line for line in (state_dir / "approvals.jsonl").read_text().splitlines() if line.strip()]
    assert len(log_lines) == 2


def test_record_approval_no_interaction_id_no_replay_check(tmp_path: Path) -> None:
    """Omitting interaction_id skips replay; two calls create two records."""
    state_dir, run_dir = _paths(tmp_path)
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
    )
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="rejected",
        binding=_binding(),
        signed_by="bob",
    )
    log_lines = [line for line in (state_dir / "approvals.jsonl").read_text().splitlines() if line.strip()]
    assert len(log_lines) == 2


# ---------------------------------------------------------------------------
# load_approval
# ---------------------------------------------------------------------------


def test_load_approval_returns_none_when_missing(tmp_path: Path) -> None:
    """No current approval file yields None."""
    _state_dir, run_dir = _paths(tmp_path)
    assert load_approval(run_dir, "mod_set") is None


def test_load_approval_returns_record(tmp_path: Path) -> None:
    """A written approval loads cleanly."""
    state_dir, run_dir = _paths(tmp_path)
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="alice",
    )
    loaded = load_approval(run_dir, "mod_set")
    assert loaded is not None
    assert loaded.decision == "approved"


def test_load_approval_malformed_returns_none(tmp_path: Path) -> None:
    """Malformed JSON yields None (fails closed at the caller's verify)."""
    _state_dir, run_dir = _paths(tmp_path)
    (run_dir / "approval.mod_set.json").write_text("not json")
    assert load_approval(run_dir, "mod_set") is None


def test_load_approval_wrong_schema_version_returns_none(tmp_path: Path) -> None:
    """An unknown schema_version yields None."""
    _state_dir, run_dir = _paths(tmp_path)
    (run_dir / "approval.mod_set.json").write_text(json.dumps({"schema_version": 99, "approval_id": "x"}))
    assert load_approval(run_dir, "mod_set") is None


# ---------------------------------------------------------------------------
# verify_approval
# ---------------------------------------------------------------------------


def _valid_record(
    tmp_path: Path,
    *,
    decision: str = "approved",
    now: str = "2026-01-01T00:00:00Z",
) -> tuple[ApprovalRecord, ApprovalBinding]:
    """Record and return a valid approval plus its binding."""
    state_dir, run_dir = _paths(tmp_path)
    canonical_binding = _binding()
    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision=decision,
        binding=canonical_binding,
        signed_by="alice",
        now=now,
    )
    return (record, canonical_binding)


def test_verify_approval_valid(tmp_path: Path) -> None:
    """A valid, unexpired, matching approval verifies."""
    record, canonical_binding = _valid_record(tmp_path)
    ok, err = verify_approval(record, expected_binding=canonical_binding, now="2026-01-02T00:00:00Z")
    assert ok is True
    assert err is None


def test_verify_approval_none_returns_false(tmp_path: Path) -> None:
    """A missing approval fails verification."""
    ok, err = verify_approval(None, expected_binding=_binding())
    assert ok is False
    assert "no approval record" in (err or "")


def test_verify_approval_rejected_fails(tmp_path: Path) -> None:
    """A rejection does not authorize."""
    record, canonical_binding = _valid_record(tmp_path, decision="rejected")
    ok, err = verify_approval(record, expected_binding=canonical_binding, now="2026-01-02T00:00:00Z")
    assert ok is False
    assert "not 'approved'" in (err or "")


def test_verify_approval_expired_fails(tmp_path: Path) -> None:
    """An expired approval does not authorize."""
    record, canonical_binding = _valid_record(tmp_path, now="2026-01-01T00:00:00Z")
    ok, err = verify_approval(record, expected_binding=canonical_binding, now="2026-02-01T00:00:00Z")
    assert ok is False
    assert "expired" in (err or "")


def test_verify_approval_binding_mismatch_fails(tmp_path: Path) -> None:
    """A changed candidate identity invalidates the approval."""
    record, _original_binding = _valid_record(tmp_path)
    other_binding = _binding(slip_content_sha256="f" * 64)
    ok, err = verify_approval(record, expected_binding=other_binding, now="2026-01-02T00:00:00Z")
    assert ok is False
    assert "binding does not match" in (err or "")


def test_verify_approval_just_before_expiry_passes(tmp_path: Path) -> None:
    """A check one second before expiry still passes."""
    record, canonical_binding = _valid_record(tmp_path, now="2026-01-01T00:00:00Z")
    ok, _err = verify_approval(record, expected_binding=canonical_binding, now="2026-01-07T23:59:59Z")
    assert ok is True


def test_verify_approval_at_expiry_fails(tmp_path: Path) -> None:
    """A check exactly at ``expires_at_utc`` fails."""
    record, canonical_binding = _valid_record(tmp_path, now="2026-01-01T00:00:00Z")
    ok, _err = verify_approval(record, expected_binding=canonical_binding, now="2026-01-08T00:00:00Z")
    assert ok is False
