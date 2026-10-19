# src/minecraft/deploy_pack/daemon/journal.py

"""Durable operation journal for the deployment orchestration (spec v1.3 Section 12.7).

Append-only JSONL log recording every stage transition of every
deployment. The daemon writes one record at each stage boundary; the
records are the authoritative account of what the daemon has done.

Read semantics
--------------
On daemon startup, :meth:`DeploymentJournal.unresolved_runs` returns the
run IDs whose last recorded stage is not terminal (COMPLETE, FAILED,
RECOVERY_COMPLETED, RECOVERY_FAILED). A non-empty result means the
previous daemon crashed mid-deployment; spec Section 12.7 requires that the
daemon halt and require operator intervention rather than guess at
recovery. The deploy orchestrator (a later module) calls this on startup
and refuses to accept new deployment requests until the operator
resolves the unresolved runs.

Write semantics
---------------
Every :meth:`append` call opens the file in append mode, writes one JSON
line, and calls ``os.fsync`` before returning. This is intentionally
slow -- the journal is written a dozen times per deployment, not
thousands, and durability is the whole point. A missing journal file is
created on first append (parent directories included).

Malformed lines
---------------
If the journal file contains a line that does not parse as a JSON
object, or that lacks a required field, :meth:`read_all` logs a warning
and skips the line. This preserves the invariant that the daemon can
always read its own journal even after a partial write (for example,
power loss mid-append), and matches the robustness of the shipping
transaction's own journal reader.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from LoggingCore import get_logger

__all__ = [
    "TERMINAL_STAGES",
    "DeploymentJournal",
    "JournalRecord",
    "Stage",
]


_log = get_logger(__name__)


class Stage(StrEnum):
    """One deployment stage transition.

    The values are the strings that appear in the journal file. Adding a
    new stage is a wire-format change: consumers that pattern-match on
    :data:`TERMINAL_STAGES` or dispatch on stage names must be updated
    in the same commit.
    """

    PULL_STARTED = "pull_started"
    PULL_COMPLETED = "pull_completed"
    BACKUP_STARTED = "backup_started"
    BACKUP_COMPLETED = "backup_completed"
    LOCK_ACQUIRED = "lock_acquired"
    SHIP_STARTED = "ship_started"
    SHIP_COMPLETED = "ship_completed"
    SHIP_FAILED = "ship_failed"
    SERVICES_STOPPED = "services_stopped"
    SERVICES_STARTED = "services_started"
    HEALTH_CHECK_STARTED = "health_check_started"
    HEALTH_CHECK_PASSED = "health_check_passed"
    HEALTH_CHECK_FAILED = "health_check_failed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    RECOVERY_FAILED = "recovery_failed"
    COMPLETE = "complete"
    FAILED = "failed"


TERMINAL_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.COMPLETE,
        Stage.FAILED,
        Stage.RECOVERY_COMPLETED,
        Stage.RECOVERY_FAILED,
    }
)


@dataclass(frozen=True)
class JournalRecord:
    """One line of the operation journal.

    ``timestamp_utc`` is set at construction time by the writer (not read
    from the filesystem) so records within one deployment are
    monotonically ordered even across daemon restarts.
    """

    timestamp_utc: str
    run_id: str
    release_id: str
    stage: Stage
    detail: str = ""

    def to_json(self) -> dict:
        """Return the record as a JSON-compatible dict."""
        return {
            "timestamp_utc": self.timestamp_utc,
            "run_id": self.run_id,
            "release_id": self.release_id,
            "stage": self.stage.value,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: dict) -> JournalRecord | None:
        """Return a record from a parsed JSON dict, or None if malformed."""
        try:
            return cls(
                timestamp_utc=str(data["timestamp_utc"]),
                run_id=str(data["run_id"]),
                release_id=str(data["release_id"]),
                stage=Stage(data["stage"]),
                detail=str(data.get("detail", "")),
            )
        except (KeyError, ValueError, TypeError):
            return None


def _now_utc() -> str:
    """Return the current UTC time with microsecond precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DeploymentJournal:
    """Append-only JSONL journal (spec Section 12.7).

    Constructed once per daemon and shared across all deployments. The
    path is stored; the file is opened per-write so the journal survives
    crashes without leaking file descriptors.
    """

    def __init__(self, path: Path) -> None:
        """Store the path; no I/O until the first :meth:`append`."""
        self._path = path

    @property
    def path(self) -> Path:
        """Return the journal file path."""
        return self._path

    def append(self, record: JournalRecord) -> None:
        """Append ``record`` to the journal and fsync.

        Raises OSError on any I/O failure. The caller (the deploy
        orchestrator) treats journal write failure as fatal: without a
        journal, recovery is impossible and the spec requires a halt.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json(), sort_keys=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _log.debug(f"journal: run={record.run_id} stage={record.stage.value}")

    def append_stage(
        self,
        *,
        run_id: str,
        release_id: str,
        stage: Stage,
        detail: str = "",
    ) -> None:
        """Build a record with the current time and append it."""
        self.append(
            JournalRecord(
                timestamp_utc=_now_utc(),
                run_id=run_id,
                release_id=release_id,
                stage=stage,
                detail=detail,
            )
        )

    def read_all(self) -> list[JournalRecord]:
        """Return every valid record in file order.

        Malformed lines log a warning and are skipped. A missing file
        returns ``[]``.
        """
        if not self._path.is_file():
            return []
        out: list[JournalRecord] = []
        with open(self._path, encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    _log.warning(f"journal: malformed line {line_number} in {self._path}; skipping")
                    continue
                record = JournalRecord.from_json(data)
                if record is None:
                    _log.warning(f"journal: incomplete record at line {line_number} in {self._path}; skipping")
                    continue
                out.append(record)
        return out

    def find_run(self, run_id: str) -> list[JournalRecord]:
        """Return every record for ``run_id``, in file order."""
        return [r for r in self.read_all() if r.run_id == run_id]

    def last_stage_for_run(self, run_id: str) -> Stage | None:
        """Return the last recorded stage for ``run_id``, or None."""
        records = self.find_run(run_id)
        return records[-1].stage if records else None

    def unresolved_runs(self) -> list[str]:
        """Return run IDs whose last recorded stage is not terminal.

        Ordered by first appearance. Used at daemon startup (spec Section 12.7).
        """
        last_stage: dict[str, Stage] = {}
        order: list[str] = []
        for record in self.read_all():
            if record.run_id not in last_stage:
                order.append(record.run_id)
            last_stage[record.run_id] = record.stage
        return [rid for rid in order if last_stage[rid] not in TERMINAL_STAGES]
