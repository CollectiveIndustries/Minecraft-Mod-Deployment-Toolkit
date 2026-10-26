# tests/modpack_upgrade/test_approval_fsync.py

"""Tests for audit-log durability in ``approval._append_audit`` (Slice 15 T11).

Replay protection (``_lookup_interaction``) scans ``approvals.jsonl``.
A record lost between the write and the OS flush would silently allow a
replayed Discord interaction to be accepted as a fresh approval. The
fsync in ``_append_audit`` is therefore load-bearing, not cosmetic.

The prior implementation called ``f.flush()`` twice; ``flush`` pushes the
Python buffer to the OS, but only ``fsync`` pushes the OS buffer to
stable storage. This test suite pins the correct behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minecraft.modpack_upgrade import approval as approval_mod
from minecraft.modpack_upgrade.approval import (
    ApprovalBinding,
    record_approval,
)


def _binding() -> ApprovalBinding:
    """Return a minimal valid binding."""
    return ApprovalBinding(
        slip_content_sha256="a" * 64,
        runtime_sha256="b" * 64,
        server_inputs_sha256="c" * 64,
        client_inputs_sha256="d" * 64,
        resolution_report_sha256="e" * 64,
    )


def test_append_audit_calls_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_append_audit`` calls ``os.fsync`` on the log fd after each write.

    This is the load-bearing assertion: without the fsync, a crash between
    write and the kernel's eventual flush can lose the record and thereby
    allow interaction-ID replay.
    """
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("minecraft.modpack_upgrade.approval.os.fsync", _spy)

    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="test",
    )

    assert len(fsync_calls) == 1, "expected exactly one fsync per audit-log append"


def test_audit_record_is_readable_immediately_after_write(tmp_path: Path) -> None:
    """After ``record_approval`` returns, the record is on disk and parseable.

    Combined with the fsync test above, this pins the contract: the
    append either fully lands (with fsync) or raises. There is no
    intermediate state in which ``record_approval`` returns success but
    the audit log does not contain the record.
    """
    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"

    record = record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="test",
        interaction_id="interaction-xyz",
    )

    # Record is immediately resolvable from the audit log by interaction ID.
    found = approval_mod._lookup_interaction(state_dir, "interaction-xyz")
    assert found is not None
    assert found.approval_id == record.approval_id
    assert found.nonce == record.nonce


def test_append_audit_fsyncs_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fsync happens *before* ``record_approval`` returns, not deferred.

    The audit log is a durability boundary: if the process crashes
    after ``record_approval`` returns, the record must already be in
    stable storage. A hypothetical deferred-fsync implementation would
    pass the "readable immediately" test but fail this one.
    """
    fsync_completed: list[bool] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        real_fsync(fd)
        fsync_completed.append(True)

    monkeypatch.setattr("minecraft.modpack_upgrade.approval.os.fsync", _spy)

    state_dir = tmp_path / "state"
    run_dir = tmp_path / "runs" / "run-1"
    record_approval(
        state_dir=state_dir,
        run_dir=run_dir,
        run_id="run-1",
        scope="mod_set",
        decision="approved",
        binding=_binding(),
        signed_by="test",
    )

    # fsync has already run by the time record_approval returns.
    assert fsync_completed == [True]
