# tests/deploy_pack/test_daemon_journal.py

"""Unit tests for ``daemon.journal`` (spec v1.3 Section 12.7).

Covers journal write/read roundtrip, run filtering, terminal-stage
detection, malformed-line tolerance, and I/O failure propagation. All
tests exercise a real filesystem under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minecraft.deploy_pack.daemon.journal import (
    TERMINAL_STAGES,
    DeploymentJournal,
    JournalRecord,
    Stage,
)


def _record(
    *,
    run_id: str = "run-1",
    release_id: str = "v1.0.0",
    stage: Stage = Stage.PULL_STARTED,
    detail: str = "",
) -> JournalRecord:
    """Build a journal record with sensible defaults."""
    return JournalRecord(
        timestamp_utc="2026-01-01T00:00:00.000000Z",
        run_id=run_id,
        release_id=release_id,
        stage=stage,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def test_append_creates_file_and_writes_one_line(tmp_path: Path) -> None:
    """First append creates the file with a single JSON line."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record())
    assert journal.path.is_file()
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["run_id"] == "run-1"
    assert payload["stage"] == "pull_started"


def test_append_creates_parent_directories(tmp_path: Path) -> None:
    """Append creates the parent directory chain when missing."""
    journal = DeploymentJournal(tmp_path / "nested" / "deeper" / "journal.jsonl")
    journal.append(_record())
    assert journal.path.is_file()


def test_append_stage_uses_current_timestamp(tmp_path: Path) -> None:
    """``append_stage`` fills the timestamp field automatically."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append_stage(
        run_id="run-x",
        release_id="v1.0.0",
        stage=Stage.SHIP_STARTED,
    )
    record = journal.read_all()[0]
    assert record.run_id == "run-x"
    assert record.stage == Stage.SHIP_STARTED
    assert record.timestamp_utc.endswith("Z")


def test_append_raises_on_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError during append propagates (caller treats as fatal)."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    real_open = open

    def _fail(path: object, *args: object, **kwargs: object) -> object:
        if "a" in args:
            raise OSError("simulated write failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fail)
    with pytest.raises(OSError):
        journal.append(_record())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_read_all_empty_when_file_missing(tmp_path: Path) -> None:
    """A missing journal reads as an empty list."""
    journal = DeploymentJournal(tmp_path / "nonexistent.jsonl")
    assert journal.read_all() == []


def test_read_all_roundtrips_records_in_order(tmp_path: Path) -> None:
    """Records read back in the order they were written."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    for stage in (Stage.PULL_STARTED, Stage.PULL_COMPLETED, Stage.COMPLETE):
        journal.append(_record(stage=stage))
    records = journal.read_all()
    assert [r.stage for r in records] == [Stage.PULL_STARTED, Stage.PULL_COMPLETED, Stage.COMPLETE]


def test_read_all_skips_malformed_lines(tmp_path: Path) -> None:
    """A corrupt line is logged and skipped; subsequent lines still parse."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(stage=Stage.PULL_STARTED))
    with open(journal.path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    journal.append(_record(stage=Stage.PULL_COMPLETED))
    records = journal.read_all()
    assert [r.stage for r in records] == [Stage.PULL_STARTED, Stage.PULL_COMPLETED]


def test_read_all_skips_incomplete_records(tmp_path: Path) -> None:
    """A record missing required fields is skipped."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(stage=Stage.PULL_STARTED))
    with open(journal.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": "run-1"}) + "\n")
    records = journal.read_all()
    assert len(records) == 1


def test_read_all_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines between records are ignored."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(stage=Stage.PULL_STARTED))
    with open(journal.path, "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    journal.append(_record(stage=Stage.PULL_COMPLETED))
    assert len(journal.read_all()) == 2


# ---------------------------------------------------------------------------
# Run filtering and terminal detection
# ---------------------------------------------------------------------------


def test_find_run_filters_by_run_id(tmp_path: Path) -> None:
    """``find_run`` returns only records for the requested run."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(run_id="run-a", stage=Stage.PULL_STARTED))
    journal.append(_record(run_id="run-b", stage=Stage.PULL_STARTED))
    journal.append(_record(run_id="run-a", stage=Stage.PULL_COMPLETED))
    run_a = journal.find_run("run-a")
    assert len(run_a) == 2
    assert all(r.run_id == "run-a" for r in run_a)


def test_last_stage_for_run_returns_most_recent(tmp_path: Path) -> None:
    """``last_stage_for_run`` returns the last recorded stage."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(run_id="run-a", stage=Stage.PULL_STARTED))
    journal.append(_record(run_id="run-a", stage=Stage.SHIP_STARTED))
    assert journal.last_stage_for_run("run-a") == Stage.SHIP_STARTED


def test_last_stage_for_run_returns_none_for_unknown(tmp_path: Path) -> None:
    """An unknown run_id returns None."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    assert journal.last_stage_for_run("run-missing") is None


def test_unresolved_runs_empty_when_no_runs(tmp_path: Path) -> None:
    """An empty journal has no unresolved runs."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    assert journal.unresolved_runs() == []


def test_unresolved_runs_identifies_non_terminal(tmp_path: Path) -> None:
    """A run whose last stage is non-terminal is unresolved."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(run_id="run-done", stage=Stage.COMPLETE))
    journal.append(_record(run_id="run-in-flight", stage=Stage.SHIP_STARTED))
    assert journal.unresolved_runs() == ["run-in-flight"]


def test_unresolved_runs_treats_all_terminal_stages_as_resolved(tmp_path: Path) -> None:
    """Every TERMINAL_STAGES value marks a run as resolved."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    for index, stage in enumerate(sorted(TERMINAL_STAGES, key=lambda s: s.value)):
        journal.append(_record(run_id=f"run-{index}", stage=stage))
    assert journal.unresolved_runs() == []


def test_unresolved_runs_ordered_by_first_appearance(tmp_path: Path) -> None:
    """The unresolved-run list preserves first-appearance order."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(run_id="run-c", stage=Stage.PULL_STARTED))
    journal.append(_record(run_id="run-a", stage=Stage.PULL_STARTED))
    journal.append(_record(run_id="run-b", stage=Stage.PULL_STARTED))
    assert journal.unresolved_runs() == ["run-c", "run-a", "run-b"]


def test_unresolved_runs_ignores_resolved_then_reopened(tmp_path: Path) -> None:
    """A run that terminated and then received a new stage counts as unresolved."""
    journal = DeploymentJournal(tmp_path / "journal.jsonl")
    journal.append(_record(run_id="run-x", stage=Stage.COMPLETE))
    journal.append(_record(run_id="run-x", stage=Stage.SHIP_STARTED))
    assert journal.unresolved_runs() == ["run-x"]
