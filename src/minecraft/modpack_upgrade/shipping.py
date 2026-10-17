# src/minecraft/modpack_upgrade/shipping.py

"""Shipping transaction (spec Section 3.5, Section 13.2, Section 13.4; T08, T17).

Responsibilities:
  * Diff a desired file set against a target directory's current `.jar`
    inventory.
  * Pre-stage every new/changed file to a sibling staging directory and
    verify each staged file's SHA-256 against its source.
  * Back up every file about to be removed or replaced to a sibling
    backup directory.
  * Record every applied operation to an append-only JSONL journal.
  * Apply each operation atomically per file (`os.replace`).
  * Verify the resulting inventory against the desired set.
  * On any failure during apply or verify, walk the journal backwards and
    restore the prior inventory.
  * Clean up staging and backup on success.

Non-responsibilities:
  * Deciding what to ship. The caller supplies the source set.
  * Stopping or starting services. The caller wraps this in lifecycle
    phases.
  * Handling configs, kubejs, resource packs, or any non-mod content.
    Those have their own modes in `deploy_pack.files.copy_tree`.

The transaction is *logically* atomic: either the final inventory matches
the desired set, or the final inventory matches the pre-transaction
snapshot. It is not filesystem-atomic across the entire set; per-file
operations are atomic via `os.replace`, and the journal is what makes the
whole sequence recoverable.

The post-apply verification compares the target's `.jar` inventory against
a computed "expected after ship" map: the desired source set at their
source hashes, plus any protected files retained at their pre-transaction
hashes. Files the transaction did not touch do not appear in the map and
must not appear in the target.

Journal ordering
----------------

Records are written *after* the corresponding filesystem operation
succeeds. Rollback therefore cannot undo an operation whose journal write
failed (e.g. disk full between the op and the journal append). This is a
deliberate simplification: the journal and the target share a filesystem,
so the failure mode requires a full disk in the tiny window between two
syscalls. A stronger crash-safe variant would use an intent-then-commit
journal with idempotent replay; that is out of scope for this slice.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .errors import UpgradeError

__all__ = [
    "FileOps",
    "RealFileOps",
    "ShipRequest",
    "ShipResult",
    "ship",
]

_JOURNAL_FILENAME = "journal.jsonl"


# ===========================================================================
# Section 1  File operations protocol
# ===========================================================================


class FileOps(Protocol):
    """Injection seam for filesystem operations.

    Production uses :class:`RealFileOps`. Tests substitute failures by
    wrapping a real instance or subclassing.
    """

    def mkdir(self, path: Path) -> None:
        """Create ``path`` and any missing parents."""
        ...

    def copy(self, src: Path, dst: Path) -> None:
        """Copy ``src`` to ``dst``."""
        ...

    def replace(self, src: Path, dst: Path) -> None:
        """Atomically move ``src`` onto ``dst``."""
        ...

    def unlink(self, path: Path) -> None:
        """Remove ``path``."""
        ...

    def sha256(self, path: Path) -> str:
        """Return the lowercase hex SHA-256 of ``path``."""
        ...

    def exists(self, path: Path) -> bool:
        """Return True if ``path`` exists."""
        ...

    def is_file(self, path: Path) -> bool:
        """Return True if ``path`` is a regular file."""
        ...


class RealFileOps:
    """Default FileOps backed by shutil and os."""

    def mkdir(self, path: Path) -> None:
        """Create ``path`` and any missing parents."""
        path.mkdir(parents=True, exist_ok=True)

    def copy(self, src: Path, dst: Path) -> None:
        """Copy ``src`` to ``dst`` with metadata."""
        shutil.copy2(src, dst)

    def replace(self, src: Path, dst: Path) -> None:
        """Atomically move ``src`` onto ``dst``."""
        os.replace(src, dst)

    def unlink(self, path: Path) -> None:
        """Remove ``path``."""
        path.unlink()

    def sha256(self, path: Path) -> str:
        """Return the lowercase hex SHA-256 of ``path``."""
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def exists(self, path: Path) -> bool:
        """Return True if ``path`` exists."""
        return path.exists()

    def is_file(self, path: Path) -> bool:
        """Return True if ``path`` is a regular file."""
        return path.is_file()


# ===========================================================================
# Section 2  Request and result types
# ===========================================================================


@dataclass(frozen=True)
class ShipRequest:
    """Inputs to a shipping transaction."""

    source_set: dict[str, Path]
    target_dir: Path
    protect_patterns: tuple[str, ...] = ()


@dataclass
class ShipResult:
    """Outcome of a shipping transaction."""

    success: bool = True
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    protected_kept: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failure_message: str | None = None
    rollback_performed: bool = False
    rollback_succeeded: bool = True
    staging_dir: Path | None = None
    backup_dir: Path | None = None
    journal_path: Path | None = None

    @property
    def changed(self) -> int:
        """Total number of added, updated, and removed files."""
        return len(self.added) + len(self.updated) + len(self.removed)

    @property
    def any_change(self) -> bool:
        """True when at least one file was added, updated, or removed."""
        return bool(self.added or self.updated or self.removed)


# ===========================================================================
# Section 3  Input validation
# ===========================================================================


def _validate_source_filenames(source_set: dict[str, Path]) -> None:
    """Reject any source entry that is not a plain ``.jar`` filename.

    ``.jar.disabled`` and other non-``.jar`` extensions are excluded by
    the suffix check; a directory component is excluded by the separator
    check.
    """
    for filename in source_set:
        if not filename.endswith(".jar"):
            raise UpgradeError(f"shipping: source filename must end in .jar: {filename!r}")
        if "/" in filename or "\\" in filename:
            raise UpgradeError(f"shipping: source filename must not contain path separators: {filename!r}")
        if filename in (".", ".."):
            raise UpgradeError(f"shipping: source filename must not be '.' or '..': {filename!r}")


# ===========================================================================
# Section 4  Inventory scan
# ===========================================================================


def _scan_jar_inventory(target_dir: Path, ops: FileOps) -> dict[str, str]:
    """Return ``{filename: sha256}`` for every ``.jar`` directly in ``target_dir``.

    Only regular files are considered. Directories, symlinks, and files
    whose suffix is not ``.jar`` are ignored. ``.jar.disabled`` is not
    a ``.jar`` and is therefore preserved as an unrelated artifact.
    """
    if not ops.exists(target_dir):
        return {}
    out: dict[str, str] = {}
    for entry in sorted(target_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".jar":
            continue
        try:
            out[entry.name] = ops.sha256(entry)
        except OSError:
            continue
    return out


def _is_protected(filename: str, patterns: tuple[str, ...]) -> bool:
    """Return True when ``filename`` matches any protect pattern.

    Matching is fnmatch-style against the filename directly. Shipping
    operates on a flat directory, so the pattern set is flat too;
    component matching is deferred to the config layer.
    """
    if not patterns:
        return False
    return any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)


# ===========================================================================
# Section 5  Diff computation
# ===========================================================================


@dataclass
class _Diff:
    """The four buckets that describe what a ship call will do."""

    added: dict[str, Path]
    updated: dict[str, Path]
    removed: list[str]
    unchanged: list[str]
    protected_kept: list[str]


def _hash_source_set(source_set: dict[str, Path], ops: FileOps) -> dict[str, str]:
    """Return ``{filename: sha256}`` for the desired file set.

    Raises :class:`UpgradeError` if any source file cannot be read.
    """
    out: dict[str, str] = {}
    for filename, path in source_set.items():
        try:
            out[filename] = ops.sha256(path)
        except OSError as exc:
            raise UpgradeError(f"shipping: cannot read source {path}: {exc}") from exc
    return out


def _compute_diff(
    source_hashes: dict[str, str],
    target_inventory: dict[str, str],
    protect_patterns: tuple[str, ...],
) -> _Diff:
    """Split the source set and target inventory into the four buckets."""
    added: dict[str, Path] = {}
    updated: dict[str, Path] = {}
    unchanged: list[str] = []

    for filename, source_sha in source_hashes.items():
        target_sha = target_inventory.get(filename)
        if target_sha is None:
            added[filename] = _placeholder()
        elif target_sha != source_sha:
            updated[filename] = _placeholder()
        else:
            unchanged.append(filename)

    removed: list[str] = []
    protected_kept: list[str] = []
    for filename in target_inventory:
        if filename in source_hashes:
            continue
        if _is_protected(filename, protect_patterns):
            protected_kept.append(filename)
        else:
            removed.append(filename)

    return _Diff(
        added=added,
        updated=updated,
        removed=sorted(removed),
        unchanged=sorted(unchanged),
        protected_kept=sorted(protected_kept),
    )


def _placeholder() -> Path:
    """Return a sentinel Path used as a value in the diff's maps."""
    return Path()


def _expected_after_ship(
    source_hashes: dict[str, str],
    target_inventory: dict[str, str],
    protected_kept: list[str],
) -> dict[str, str]:
    """Return the inventory the target must have once the ship succeeds.

    Every shipped file is expected at its source hash. Every protected
    file that the transaction did not touch is expected at its
    pre-transaction hash. Files the transaction removed are absent.
    """
    expected = dict(source_hashes)
    for filename in protected_kept:
        expected[filename] = target_inventory[filename]
    return expected


# ===========================================================================
# Section 6  Journal
# ===========================================================================


def _append_journal(journal_path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record to the journal file, followed by a newline."""
    with open(journal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _read_journal(journal_path: Path) -> list[dict[str, Any]]:
    """Return the journal records in file order."""
    records: list[dict[str, Any]] = []
    with open(journal_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            records.append(json.loads(stripped))
    return records


# ===========================================================================
# Section 7  Stage and backup
# ===========================================================================


def _make_sibling_dir(target: Path, kind: str) -> Path:
    """Return a unique sibling directory path for staging or backup."""
    stamp = f"{int(time.time() * 1000)}.{os.getpid()}"
    return target.parent / f".{target.name}.{kind}.{stamp}"


def _stage_sources(
    sources: dict[str, Path],
    source_hashes: dict[str, str],
    staging_dir: Path,
    ops: FileOps,
) -> None:
    """Copy each source into ``staging_dir`` and verify its hash."""
    ops.mkdir(staging_dir)
    for filename, source in sources.items():
        dest = staging_dir / filename
        try:
            ops.copy(source, dest)
        except OSError as exc:
            raise UpgradeError(f"shipping: could not stage {source} -> {dest}: {exc}") from exc
        expected = source_hashes[filename]
        actual = ops.sha256(dest)
        if expected != actual:
            raise UpgradeError(f"shipping: staged hash mismatch for {filename}: source={expected} staged={actual}")


def _backup_targets(
    filenames: list[str],
    target_dir: Path,
    backup_dir: Path,
    ops: FileOps,
) -> dict[str, str]:
    """Copy each named target file into ``backup_dir`` and return its sha256."""
    ops.mkdir(backup_dir)
    backup_hashes: dict[str, str] = {}
    for filename in filenames:
        src = target_dir / filename
        if not ops.is_file(src):
            continue
        dest = backup_dir / filename
        try:
            ops.copy(src, dest)
        except OSError as exc:
            raise UpgradeError(f"shipping: could not back up {src} -> {dest}: {exc}") from exc
        backup_hashes[filename] = ops.sha256(dest)
    return backup_hashes


# ===========================================================================
# Section 8  Apply
# ===========================================================================


def _apply_remove(
    filename: str,
    target_dir: Path,
    backup_hashes: dict[str, str],
    journal_path: Path,
    ops: FileOps,
) -> None:
    """Unlink one removed target and journal the operation."""
    target = target_dir / filename
    try:
        ops.unlink(target)
    except OSError as exc:
        raise UpgradeError(f"shipping: could not remove {target}: {exc}") from exc
    _append_journal(
        journal_path,
        {
            "op": "remove",
            "filename": filename,
            "prev_sha256": backup_hashes.get(filename),
        },
    )


def _apply_replace(
    filename: str,
    staging_dir: Path,
    target_dir: Path,
    backup_hashes: dict[str, str],
    journal_path: Path,
    ops: FileOps,
) -> None:
    """Replace one target file with its staged counterpart and journal."""
    staged = staging_dir / filename
    target = target_dir / filename
    try:
        ops.replace(staged, target)
    except OSError as exc:
        raise UpgradeError(f"shipping: could not replace {target}: {exc}") from exc
    _append_journal(
        journal_path,
        {
            "op": "replace",
            "filename": filename,
            "prev_sha256": backup_hashes.get(filename),
        },
    )


def _apply_add(
    filename: str,
    staging_dir: Path,
    target_dir: Path,
    journal_path: Path,
    ops: FileOps,
) -> None:
    """Move a staged file into the target and journal the add."""
    staged = staging_dir / filename
    target = target_dir / filename
    try:
        ops.replace(staged, target)
    except OSError as exc:
        raise UpgradeError(f"shipping: could not add {target}: {exc}") from exc
    _append_journal(
        journal_path,
        {
            "op": "add",
            "filename": filename,
        },
    )


def _apply_all(
    diff: _Diff,
    staging_dir: Path,
    target_dir: Path,
    backup_hashes: dict[str, str],
    journal_path: Path,
    ops: FileOps,
) -> None:
    """Apply every operation in safe order: removals, replaces, adds."""
    for filename in diff.removed:
        _apply_remove(filename, target_dir, backup_hashes, journal_path, ops)

    for filename in sorted(diff.updated):
        _apply_replace(filename, staging_dir, target_dir, backup_hashes, journal_path, ops)

    for filename in sorted(diff.added):
        _apply_add(filename, staging_dir, target_dir, journal_path, ops)


# ===========================================================================
# Section 9  Verify and rollback
# ===========================================================================


def _verify_inventory(
    target_dir: Path,
    expected: dict[str, str],
    ops: FileOps,
) -> str | None:
    """Return None when the target inventory matches ``expected``, else a diagnostic."""
    actual = _scan_jar_inventory(target_dir, ops)
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        parts: list[str] = []
        if missing:
            parts.append(f"missing: {missing}")
        if extra:
            parts.append(f"unexpected: {extra}")
        return "; ".join(parts)
    for filename, expected_sha in expected.items():
        if actual.get(filename) != expected_sha:
            return f"hash mismatch: {filename}"
    return None


def _undo_one(
    record: dict[str, Any],
    target_dir: Path,
    backup_dir: Path,
    ops: FileOps,
) -> None:
    """Undo a single journal record, best-effort."""
    operation = record.get("op")
    filename = record.get("filename")
    if not filename:
        return
    target = target_dir / filename
    if operation == "add":
        with contextlib.suppress(OSError):
            if ops.exists(target):
                ops.unlink(target)
    elif operation in ("remove", "replace"):
        backup = backup_dir / filename
        if ops.is_file(backup):
            with contextlib.suppress(OSError):
                ops.replace(backup, target)


def _rollback(
    journal_path: Path,
    target_dir: Path,
    backup_dir: Path,
    ops: FileOps,
) -> bool:
    """Walk the journal in reverse and restore the prior inventory.

    Returns True when the resulting inventory matches the snapshot
    recorded in the journal's opening record. Returns False when the
    journal cannot be read or the final verification fails.
    """
    try:
        records = _read_journal(journal_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    snapshot: dict[str, str] = {}
    ops_records: list[dict[str, Any]] = []
    for record in records:
        if record.get("op") == "snapshot":
            snapshot = record.get("inventory") or {}
            ops_records = []
        else:
            ops_records.append(record)

    for record in reversed(ops_records):
        _undo_one(record, target_dir, backup_dir, ops)

    return _verify_inventory(target_dir, snapshot, ops) is None


# ===========================================================================
# Section 10  Cleanup
# ===========================================================================


def _cleanup_dir(path: Path | None) -> None:
    """Remove ``path`` recursively, ignoring errors."""
    if path is None:
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(path)


# ===========================================================================
# Section 11  Public entry point
# ===========================================================================


def _noop_result(diff: _Diff) -> ShipResult:
    """Build the ShipResult for the "nothing to do" case."""
    return ShipResult(
        success=True,
        added=[],
        updated=[],
        removed=[],
        protected_kept=list(diff.protected_kept),
        unchanged=list(diff.unchanged),
    )


def _populate_result(result: ShipResult, diff: _Diff) -> None:
    """Copy the diff buckets onto the result."""
    result.protected_kept = list(diff.protected_kept)
    result.unchanged = list(diff.unchanged)
    result.added = sorted(diff.added)
    result.updated = sorted(diff.updated)
    result.removed = list(diff.removed)


def ship(
    request: ShipRequest,
    *,
    ops: FileOps | None = None,
) -> ShipResult:
    """Execute a shipping transaction.

    On success, the target directory's ``.jar`` inventory matches
    ``request.source_set`` plus any protected files retained at their
    pre-transaction hashes. On failure, the inventory matches the
    pre-transaction snapshot, and ``result.rollback_succeeded`` reports
    whether the rollback itself completed cleanly.

    Never raises on a shipping failure; the result carries the outcome.
    """
    file_ops: FileOps = ops if ops is not None else RealFileOps()
    result = ShipResult()
    target_dir = request.target_dir

    try:
        _validate_source_filenames(request.source_set)
        file_ops.mkdir(target_dir)
        target_inventory = _scan_jar_inventory(target_dir, file_ops)
        source_hashes = _hash_source_set(request.source_set, file_ops)
    except UpgradeError as exc:
        result.success = False
        result.failure_message = str(exc)
        return result

    diff = _compute_diff(source_hashes, target_inventory, request.protect_patterns)
    _populate_result(result, diff)

    if not (diff.added or diff.updated or diff.removed):
        return _noop_result(diff)

    staging_dir = _make_sibling_dir(target_dir, "ship-staging")
    backup_dir = _make_sibling_dir(target_dir, "ship-backup")
    journal_path = backup_dir / _JOURNAL_FILENAME
    result.staging_dir = staging_dir
    result.backup_dir = backup_dir
    result.journal_path = journal_path

    try:
        staged_sources: dict[str, Path] = {}
        for filename in diff.added:
            staged_sources[filename] = request.source_set[filename]
        for filename in diff.updated:
            staged_sources[filename] = request.source_set[filename]

        _stage_sources(staged_sources, source_hashes, staging_dir, file_ops)

        backup_filenames = list(diff.updated) + list(diff.removed)
        file_ops.mkdir(backup_dir)
        _append_journal(
            journal_path,
            {"op": "snapshot", "inventory": target_inventory},
        )
        backup_hashes = _backup_targets(backup_filenames, target_dir, backup_dir, file_ops)

        _apply_all(
            diff,
            staging_dir,
            target_dir,
            backup_hashes,
            journal_path,
            file_ops,
        )

        expected_after = _expected_after_ship(source_hashes, target_inventory, diff.protected_kept)
        verify_error = _verify_inventory(target_dir, expected_after, file_ops)
        if verify_error is not None:
            raise UpgradeError(f"shipping: post-apply inventory mismatch: {verify_error}")

    except UpgradeError as exc:
        result.success = False
        result.failure_message = str(exc)
        result.rollback_performed = True
        result.rollback_succeeded = _rollback(journal_path, target_dir, backup_dir, file_ops)
        _cleanup_dir(staging_dir)
        if result.rollback_succeeded:
            _cleanup_dir(backup_dir)
        return result

    _cleanup_dir(staging_dir)
    _cleanup_dir(backup_dir)
    result.staging_dir = None
    result.backup_dir = None
    result.journal_path = None
    return result
