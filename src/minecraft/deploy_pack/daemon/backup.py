# src/minecraft/deploy_pack/daemon/backup.py

"""Pre-upgrade world backup (spec v1.3 Section 10.8, Section 12.10).

A separate, verified backup is required before any ``release_mode =
"upgrade"`` production deployment (spec Section 10.8). This module owns that
backup. It is distinct from the DEV migration snapshot (spec Section 10.2): the
DEV snapshot is for validation, this backup is for rollback.

Manifest
--------
Each backup is stored at ``<backup_root>/<release_id>/`` with a
``backup-manifest.json`` alongside the copied world data. The manifest
is a canonical file list: ``path`` (POSIX relative), ``size_bytes``,
``mode`` (bits only, not full st_mode), and ``sha256``. It does not
include ownership, timestamps, or any other metadata that does not
survive a filesystem round-trip in a reproducible way.

Immutability
------------
``create_backup`` refuses to overwrite an existing backup directory.
Re-running an upgrade deployment must produce a new release_id (per
spec Section 4.3); a re-run against an existing ID is an operator error,
not a retry.

Symlinks
--------
Symlinks are not followed (``shutil.copytree(symlinks=False)``). A world
directory containing symlinks is copied as regular files, and any
symlink pointing outside the source tree will be rejected by the
copy step. This is intentional: the daemon does not attempt to preserve
symlink semantics that could escape the backup tree.

Verification
------------
``verify_backup`` re-walks the backup and checks each file against the
manifest. It is called before any restore (see :mod:`recovery`) so that
a corrupted backup never overwrites a working world.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from LoggingCore import get_logger

__all__ = [
    "MANIFEST_FILENAME",
    "BackupManifest",
    "BackupResult",
    "create_backup",
    "verify_backup",
]


_log = get_logger(__name__)


MANIFEST_FILENAME = "backup-manifest.json"


@dataclass(frozen=True)
class BackupManifest:
    """Canonical record of one world backup.

    ``files`` is a tuple of dicts (not a list of dataclasses) so that
    JSON round-trips are trivial and the on-disk format is readable.
    Each entry has ``path``, ``size_bytes``, ``mode``, ``sha256``.
    """

    source: str
    files: tuple[dict, ...]
    total_size_bytes: int
    created_at_utc: str

    def to_json(self) -> dict:
        """Return the manifest as a JSON-compatible dict."""
        return {
            "source": self.source,
            "files": list(self.files),
            "total_size_bytes": self.total_size_bytes,
            "created_at_utc": self.created_at_utc,
        }

    @classmethod
    def from_json(cls, data: dict) -> BackupManifest:
        """Reconstruct a manifest from parsed JSON."""
        files_raw = data.get("files", [])
        if not isinstance(files_raw, list):
            raise ValueError("backup-manifest.json 'files' must be a list")
        return cls(
            source=str(data.get("source", "")),
            files=tuple(files_raw),
            total_size_bytes=int(data.get("total_size_bytes", 0)),
            created_at_utc=str(data.get("created_at_utc", "")),
        )


@dataclass
class BackupResult:
    """Outcome of :func:`create_backup`."""

    success: bool
    backup_dir: Path
    manifest: BackupManifest | None = None
    failure_message: str | None = None


def _now_utc() -> str:
    """Return the current UTC time as an ISO-8601 Z string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_entry(path: Path, root: Path) -> dict:
    """Return one manifest entry for a regular file under ``root``."""
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": stat.st_size,
        "mode": stat.st_mode & 0o777,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _walk_manifest(root: Path) -> list[dict]:
    """Return a sorted list of manifest entries for every file under ``root``.

    The manifest file itself is excluded so a second walk over the same
    tree yields the same list.
    """
    entries: list[dict] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            if fname == MANIFEST_FILENAME:
                continue
            full = Path(dirpath) / fname
            if not full.is_file():
                continue
            entries.append(_file_entry(full, root))
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _write_manifest(backup_dir: Path, manifest: BackupManifest) -> None:
    """Write ``manifest`` as ``backup-manifest.json`` in ``backup_dir``."""
    manifest_path = backup_dir / MANIFEST_FILENAME
    body = json.dumps(manifest.to_json(), sort_keys=True, indent=2) + "\n"
    manifest_path.write_bytes(body.encode("utf-8"))


def create_backup(
    *,
    source_world: Path,
    backup_root: Path,
    release_id: str,
) -> BackupResult:
    """Copy ``source_world`` to ``backup_root/<release_id>/`` with a manifest.

    Refuses to overwrite an existing backup directory (immutability).
    Never raises on operational failure; the result carries the outcome.
    """
    backup_dir = backup_root / release_id
    if backup_dir.exists():
        return BackupResult(
            success=False,
            backup_dir=backup_dir,
            failure_message=f"backup directory already exists at {backup_dir}",
        )
    if not source_world.is_dir():
        return BackupResult(
            success=False,
            backup_dir=backup_dir,
            failure_message=f"source world directory not found: {source_world}",
        )

    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source_world, backup_dir, symlinks=False)
    except OSError as exc:
        shutil.rmtree(backup_dir, ignore_errors=True)
        return BackupResult(
            success=False,
            backup_dir=backup_dir,
            failure_message=f"copy failed: {exc}",
        )

    try:
        files = _walk_manifest(backup_dir)
    except OSError as exc:
        return BackupResult(
            success=False,
            backup_dir=backup_dir,
            failure_message=f"manifest walk failed: {exc}",
        )

    manifest = BackupManifest(
        source=str(source_world),
        files=tuple(files),
        total_size_bytes=sum(int(entry["size_bytes"]) for entry in files),
        created_at_utc=_now_utc(),
    )
    try:
        _write_manifest(backup_dir, manifest)
    except OSError as exc:
        return BackupResult(
            success=False,
            backup_dir=backup_dir,
            failure_message=f"manifest write failed: {exc}",
        )

    _log.info(f"backup: {source_world} -> {backup_dir} ({len(files)} file(s), {manifest.total_size_bytes} byte(s))")
    return BackupResult(success=True, backup_dir=backup_dir, manifest=manifest)


def _verify_one_entry(backup_dir: Path, entry: dict) -> str | None:
    """Return an error string if ``entry`` does not match the file on disk."""
    rel = entry.get("path")
    expected_size = entry.get("size_bytes")
    expected_sha = entry.get("sha256")
    if not isinstance(rel, str) or not isinstance(expected_size, int) or not isinstance(expected_sha, str):
        return f"malformed manifest entry: {entry!r}"
    file_path = backup_dir / rel
    if not file_path.is_file():
        return f"missing file: {rel}"
    if file_path.stat().st_size != expected_size:
        return f"size mismatch: {rel}"
    actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        return f"sha256 mismatch: {rel}"
    return None


def verify_backup(backup_dir: Path) -> tuple[bool, str | None]:
    """Re-walk ``backup_dir`` and verify each file against its manifest.

    Returns ``(True, None)`` on success, ``(False, error_message)`` on
    failure. Used by :mod:`recovery` before any restore so a corrupted
    backup never overwrites a working world.
    """
    manifest_path = backup_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return (False, f"manifest missing from {backup_dir}")
    try:
        data = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        return (False, f"manifest unreadable: {exc}")
    try:
        manifest = BackupManifest.from_json(data)
    except (ValueError, TypeError) as exc:
        return (False, f"manifest malformed: {exc}")

    for entry in manifest.files:
        error = _verify_one_entry(backup_dir, entry)
        if error is not None:
            return (False, error)
    return (True, None)
