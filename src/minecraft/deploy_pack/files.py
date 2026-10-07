# src/minecraft/deploy_pack/files.py

r"""File manipulation: direct copy, clean semantics, protect, atomic publication (Project_Specs.md §3.13, §4.10, §4.11, §7.1, §7.2, §7.5).

Responsibilities (§9.2):
  * atomic write-then-rename for individual artifacts (§4.10)
  * copy-with-clean implementing the §7.2 semantics for both
    ``config_mode = "delete"`` and ``config_mode = "merge"``
  * protect-pattern matching (§3.13)
  * hash helpers (SHA-256 for change detection per §4.4, SHA-1 for
    resource-pack publication per §4.4)
  * ``@www/...`` destination parsing and resolution (§7.5)
  * per-side mapping resolution for sync items (§3.9)
  * ZIP creation for the client pack (§7.1)
  * flat-file deployment for the mods directory (§4.11)

Non-responsibilities:
  * Deciding which mode to use. §7.2 specifies merge vs. delete per
    content type; the scope code passes the mode in.
  * Deciding what goes in the client ZIP. scope_client assembles the
    staging directory; this module just zips it.
  * URL assembly for anything other than resource packs. Deploy
    notifications build their own URLs.

Structure
---------

The module is organised into eight sections with explicit banner
comments. Each section corresponds to one responsibility and its
helpers are only called from within that section or from the public
entry points declared in ``__all__``.

    §1  Atomic publication          (§4.10)
    §2  Hash helpers                (§4.4)
    §3  Protect patterns            (§3.13)
    §4  Tree copy                   (§7.2)
    §5  Flat-file deployment        (§4.11)
    §6  ZIP creation                (§7.1)
    §7  Shared destinations         (§7.5)
    §8  Resource-pack misc          (§3.9, §7.5)

Aggressive decomposition: every public entry point is a thin
orchestrator delegating to small single-purpose helpers. The
copy/clean functions (``copy_tree``, ``deploy_flat_files``) use the
same pattern: hash both sides, split into (new, updated, extras,
unchanged), run deletions, run copies, log a summary.

Logging
-------

Atomic publication logs at DEBUG with destination paths, at INFO when
a file lands, and at WARN when metadata preservation fails on an
existing destination (permission errors are non-fatal per §4.10).
Copy-with-clean logs at DEBUG with the full result breakdown and at
WARN when an individual unlink or copy fails. Protect patterns log at
INFO on load and WARN when the protect file is empty. Hash helpers and
parse/resolve helpers log at DEBUG. The module logger is
``minecraft.deploy_pack.files``; callers may inject an override via
``logger=`` for a single call.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .logging_setup import get_logger

_log = get_logger(__name__)

__all__ = [
    "CopyResult",
    "atomic_copy",
    "atomic_write",
    "build_resource_pack_url",
    "compute_sha1",
    "compute_sha256",
    "copy_tree",
    "create_zip",
    "deploy_flat_files",
    "is_protected_path",
    "is_shared_dest",
    "load_protect_patterns",
    "parse_shared_dest",
    "resolve_mapping_for_side",
    "resolve_resource_pack_dest",
    "resolve_shared_dest",
    "sha256_bytes",
    "validate_resource_pack_filename",
]


# ===========================================================================
# §1  Atomic publication (§4.10)
# ===========================================================================


def _make_temp_path(dest: Path) -> Path:
    """Return the same-directory temp path used by :func:`_publish_atomically`."""
    return dest.with_name(f"{dest.name}.tmp.{os.getpid()}")


def _fsync_path(p: Path) -> None:
    """Open ``p`` and fsync it, closing the fd regardless of outcome."""
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_metadata_from_existing(tmp: Path, dest: Path, logger: Any) -> None:
    """Copy mode and ownership from an existing ``dest`` onto ``tmp``, best-effort.

    Failures log at WARN and continue (§4.10: metadata preservation is
    not fatal). Called only when ``dest`` already exists.
    """
    try:
        st = os.stat(dest)
    except OSError as exc:
        logger.warning(f"atomic write: stat({dest}) failed: {exc}")
        return
    try:
        os.chmod(tmp, st.st_mode)
    except (PermissionError, OSError) as exc:
        logger.warning(f"atomic write: chmod({tmp}) failed: {exc}")
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except (PermissionError, OSError) as exc:
        logger.warning(f"atomic write: chown({tmp}) failed: {exc}")


def _cleanup_temp(tmp: Path) -> None:
    """Remove a temp file, suppressing any OSError."""
    with contextlib.suppress(OSError):
        tmp.unlink()


def _publish_atomically(dest: Path, writer: Callable[[Path], None], logger: Any = None) -> None:
    """Write ``dest`` atomically by staging in the same directory.

    ``writer`` is called with the temp path and is expected to write
    the file's content there. The sequence is:

      1. parent dir ensured
      2. writer(tmp) called
      3. fsync tmp
      4. mode and ownership copied from an existing dest, best-effort
      5. os.replace(tmp, dest)

    Same-directory placement guarantees same-filesystem replace. Any
    failure unlinks the temp file and re-raises. Directory-level fsync
    after replace is not performed (§4.10 durability note).
    """
    if logger is None:
        logger = _log
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _make_temp_path(dest)
    logger.debug(f"atomic publish: writing {dest} via temp {tmp.name}")
    try:
        writer(tmp)
        _fsync_path(tmp)
        if dest.exists():
            _copy_metadata_from_existing(tmp, dest, logger)
        os.replace(tmp, dest)
        logger.debug(f"atomic publish: {dest} published")
    except Exception:
        _cleanup_temp(tmp)
        raise


def atomic_write(path: Path, data: bytes, logger: Any = None) -> None:
    """Write ``data`` to ``path`` atomically (§4.10)."""
    if logger is None:
        logger = _log
    logger.debug(f"atomic_write: {path} ({len(data)} byte(s))")
    _publish_atomically(path, lambda p: p.write_bytes(data), logger)


def atomic_copy(src: Path, dest: Path, logger: Any = None) -> None:
    """Copy ``src`` onto ``dest`` atomically (§4.10).

    Same temp-fsync-metadata-replace sequence as :func:`atomic_write`,
    but the content is streamed from ``src`` via ``shutil.copyfile``
    rather than materialised in memory. Used for resource-pack ZIP
    publication, where the source can be large.

    Raises OSError on any unrecoverable failure; the temp file is
    unlinked first.
    """
    if logger is None:
        logger = _log
    if not src.is_file():
        raise FileNotFoundError(f"atomic_copy: source not found: {src}")
    logger.debug(f"atomic_copy: {src} -> {dest}")
    _publish_atomically(dest, lambda tmp: shutil.copyfile(src, tmp), logger)


# ===========================================================================
# §2  Hash helpers (§4.4)
# ===========================================================================


def _hash_file(path: Path, algo: str) -> str:
    """Return the lowercase-hex digest of ``path`` under ``algo``."""
    hasher = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_sha256(path: Path) -> str:
    """SHA-256 of a file, lowercase hex (§4.4)."""
    return _hash_file(path, "sha256")


def compute_sha1(path: Path) -> str:
    """SHA-1 of a file, lowercase hex (§4.4, resource-pack-sha1 only)."""
    return _hash_file(path, "sha1")


def sha256_bytes(data: bytes) -> str:
    """SHA-256 of a byte string, lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    """Return ``{rel_path: sha256}`` for every regular file under root.

    ``rel_path`` uses forward slashes. Symlinks are not followed for
    directory traversal; a symlink to a regular file is hashed as its
    target contents (matching ``Path.is_file()`` semantics).
    """
    result: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(root)
            rel_str = str(rel).replace(os.sep, "/")
            try:
                result[rel_str] = compute_sha256(full)
            except OSError:
                continue
    return result


def hash_tree(root: Path, logger: Any = None) -> dict[str, str]:
    """Return ``{rel_path: sha256}`` for every regular file under ``root``.

    Path keys use forward slashes. Symlink semantics match :func:`_hash_tree`.
    """
    if logger is None:
        logger = _log
    result = _hash_tree(root)
    logger.debug(f"hash_tree({root}): {len(result)} file(s) hashed")
    return result


def hash_flat_dir(root: Path, logger: Any = None) -> dict[str, str]:
    """Return ``{filename: sha256}`` for the ``.jar`` files directly in ``root``.

    Non-``.jar`` files and subdirectories are ignored (§4.11: ``mods_dir``
    is treated as flat).
    """
    if logger is None:
        logger = _log
    if not root.is_dir():
        logger.debug(f"hash_flat_dir({root}): directory missing; returning empty")
        return {}
    out: dict[str, str] = {}
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix != ".jar":
            continue
        try:
            out[entry.name] = compute_sha256(entry)
        except OSError:
            continue
    logger.debug(f"hash_flat_dir({root}): {len(out)} jar(s) hashed")
    return out


# ===========================================================================
# §3  Protect patterns (§3.13)
# ===========================================================================


def is_protected_path(rel_path: str | Path, protect_patterns: list[str] | None = None) -> bool:
    """Return True if ``rel_path`` must never be deleted during a clean.

    A path is protected if either:

      * the full relative path matches a pattern, OR
      * any single path component of the path matches a pattern.

    Patterns use ``fnmatch`` glob syntax. ``fnmatch`` treats ``/`` as a
    literal, so ``*.key`` matches both ``foo.key`` and ``config/foo.key``
    - the ``*`` spans ``/``.

    An empty or missing pattern list means nothing is protected
    (§3.13: missing ``.deploy_protect`` is a silent no-op).
    """
    if not protect_patterns:
        return False
    if isinstance(rel_path, Path):
        rel_str = str(rel_path).replace("\\", "/")
        parts: tuple[str, ...] = rel_path.parts
    else:
        rel_str = str(rel_path).replace("\\", "/")
        parts = tuple(rel_str.split("/"))
    for pat in protect_patterns:
        if fnmatch.fnmatch(rel_str, pat):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _strip_comment_and_blank(line: str) -> str | None:
    """Return the stripped pattern on this line, or None for comments/blank lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return line


def _normalize_protect_pattern(line: str) -> str:
    """Strip a single trailing ``/`` so ``world/`` behaves like ``world``."""
    if line.endswith("/") and len(line) > 1:
        return line.rstrip("/")
    return line


def load_protect_patterns(path: Path | None, logger: Any = None) -> list[str]:
    """Read ``.deploy_protect`` into a list of globs (§3.13).

    Missing file → empty list, silent no-op.
    Empty file → warning, empty list.
    Non-empty → log count, return patterns.

    Format: one pattern per line; blank lines and ``#`` comments ignored;
    leading/trailing whitespace stripped; a single trailing ``/`` is
    stripped so ``world/`` behaves like ``world``.
    """
    if logger is None:
        logger = _log
    if path is None:
        logger.debug("load_protect_patterns: no protect file configured")
        return []
    if not path.is_file():
        logger.debug(f"load_protect_patterns: {path} not present; no patterns loaded")
        return []
    patterns: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            candidate = _strip_comment_and_blank(line)
            if candidate is None:
                continue
            patterns.append(_normalize_protect_pattern(candidate))
    if not patterns:
        logger.warning(f"protect file is empty: {path}")
    else:
        logger.info(f"Loaded {len(patterns)} protect pattern(s) from {path}")
    return patterns


# ===========================================================================
# §4  Tree copy (§7.2)
# ===========================================================================


@dataclass
class CopyResult:
    """Outcome of a :func:`copy_tree` call."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    protected_kept: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def any_change(self) -> bool:
        """Checks whether any change has occurred."""
        return bool(self.added or self.updated or self.removed)

    @property
    def changed_count(self) -> int:
        """Counts the total number of changed items."""
        return len(self.added) + len(self.updated) + len(self.removed)


def _split_tree_maps(src_map: dict[str, str], dst_map: dict[str, str]) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return ``(new_files, updated, extras, unchanged)`` from the two hash maps.

    * ``new_files`` - present in src only
    * ``extras``    - present in dst only
    * ``updated``   - present in both, content differs
    * ``unchanged`` - present in both, content matches
    """
    src_set = set(src_map)
    dst_set = set(dst_map)
    new_files = src_set - dst_set
    extras = dst_set - src_set
    common = src_set & dst_set
    updated = {rel for rel in common if src_map[rel] != dst_map[rel]}
    unchanged = common - updated
    return (new_files, updated, extras, unchanged)


def _prune_empty_dirs(root: Path, protect_patterns: list[str], logger: Any) -> None:
    """Remove empty directories bottom-up, skipping protected ones."""
    for dirpath, dirnames, _filenames in os.walk(root, topdown=False):
        for dname in dirnames:
            full = Path(dirpath) / dname
            rel = full.relative_to(root)
            if is_protected_path(rel, protect_patterns):
                logger.debug(f"prune: keeping protected dir {rel}")
                continue
            with contextlib.suppress(OSError):
                full.rmdir()
                logger.debug(f"prune: removed empty dir {rel}")


def _copy_tree_deletions(dst: Path, extras: set[str], protect: list[str], result: CopyResult, logger: Any) -> None:
    """Delete-mode step: remove unprotected extras, keep protected ones."""
    for rel in sorted(extras):
        if is_protected_path(rel, protect):
            result.protected_kept.append(rel)
            logger.debug(f"copy_tree: keeping protected extra {rel}")
            continue
        try:
            (dst / rel).unlink()
            result.removed.append(rel)
        except OSError as exc:
            logger.warning(f"copy_tree: unlink {rel} failed: {exc}")


def _copy_tree_adds_and_updates(src: Path, dst: Path, new_files: set[str], updated: set[str], result: CopyResult, logger: Any) -> None:
    """Copy new and updated files, appending outcomes to ``result``.

    A file that exists as a directory at the destination raises
    :class:`IsADirectoryError` - this is a hard stop, not a skip.
    Individual copy failures are logged at WARN and skipped.
    """
    for rel in sorted(new_files | updated):
        src_file = src / rel
        dst_file = dst / rel
        if dst_file.is_dir():
            raise IsADirectoryError(f"copy_tree: dst path is a directory but src is a file: {dst_file}")
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_file, dst_file)
        except OSError as exc:
            logger.warning(f"copy_tree: copy {rel} failed: {exc}")
            continue
        if rel in new_files:
            result.added.append(rel)
        else:
            result.updated.append(rel)


def _log_copy_tree_result(result: CopyResult, src: Path, dst: Path, mode: str, logger: Any) -> None:
    """Emit the closing INFO/DEBUG line with the per-bucket counts."""
    if result.any_change:
        logger.info(
            f"copy_tree({src} -> {dst}, mode={mode}): "
            f"{len(result.added)} added, {len(result.updated)} updated, "
            f"{len(result.removed)} removed, {len(result.protected_kept)} protected-kept, "
            f"{len(result.unchanged)} unchanged"
        )
    else:
        logger.debug(f"copy_tree({src} -> {dst}, mode={mode}): no changes ({len(result.unchanged)} unchanged, {len(result.protected_kept)} protected-kept)")


def copy_tree(
    src: Path,
    dst: Path,
    mode: str = "merge",
    protect_patterns: list[str] | None = None,
    logger: Any = None,
) -> CopyResult:
    """Deploy ``src``'s contents into ``dst`` per §7.2.

    ``mode`` is one of:

      * ``"delete"`` - remove unprotected files that are not in source
        OR whose content differs from source; then copy new/changed.
        Unchanged files are never touched.
      * ``"merge"`` - delete only files that exist in both trees and
        whose content differs; then copy new/changed. Files present
        only in ``dst`` survive. Unchanged files are never touched.

    Protect patterns (§3.13) apply to *deletions only*. A protected
    file that also exists in the source is still overwritten - protection
    governs removal, not overwrite (§3.13, §4.11).

    Directory pruning runs after removals: an empty directory left
    behind by a deletion is removed unless it matches a protect pattern.

    Change detection is SHA-256 on file content (§4.4). Unchanged files
    are neither removed nor rewritten.
    """
    if logger is None:
        logger = _log
    if mode not in ("merge", "delete"):
        raise ValueError(f"mode must be 'merge' or 'delete', got {mode!r}")
    if not src.is_dir():
        raise NotADirectoryError(f"source directory not found: {src}")
    protect = protect_patterns or []
    dst.mkdir(parents=True, exist_ok=True)
    logger.debug(f"copy_tree: {src} -> {dst} (mode={mode}, {len(protect)} protect pattern(s))")

    src_map = _hash_tree(src)
    dst_map = _hash_tree(dst)
    new_files, updated, extras, unchanged = _split_tree_maps(src_map, dst_map)

    result = CopyResult()
    if mode == "delete":
        _copy_tree_deletions(dst, extras, protect, result, logger)
    _copy_tree_adds_and_updates(src, dst, new_files, updated, result, logger)
    result.unchanged = sorted(unchanged)
    if result.removed:
        _prune_empty_dirs(dst, protect, logger)

    _log_copy_tree_result(result, src, dst, mode, logger)
    return result


# ===========================================================================
# §5  Flat-file deployment (§4.11)
# ===========================================================================


def _hash_source_files(src_files: dict[str, Path], logger: Any) -> dict[str, str]:
    """Return ``{filename: sha256}`` for every readable source path.

    Unreadable sources log at WARN and are dropped from the map.
    """
    out: dict[str, str] = {}
    for name, path in src_files.items():
        try:
            out[name] = compute_sha256(path)
        except OSError as exc:
            logger.warning(f"deploy_flat_files: hash {name} failed: {exc}")
    return out


def _split_flat_sets(src_map: dict[str, str], dst_map: dict[str, str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return ``(new_files, updated, extras, unchanged)`` as sorted lists.

    Uses the same bucketing rule as :func:`_split_tree_maps`, but the
    flat-file caller wants deterministic list order rather than sets.
    """
    src_set = set(src_map)
    dst_set = set(dst_map)
    new_files = sorted(src_set - dst_set)
    extras = sorted(dst_set - src_set)
    common = src_set & dst_set
    updated = sorted(f for f in common if src_map[f] != dst_map[f])
    unchanged = sorted(common - set(updated))
    return (new_files, updated, extras, unchanged)


def _remove_flat_extras(dst_dir: Path, extras: list[str], protect: list[str], result: CopyResult, logger: Any) -> None:
    """Remove unprotected extras from ``dst_dir``, keeping protected ones."""
    for name in extras:
        if is_protected_path(name, protect):
            result.protected_kept.append(name)
            logger.debug(f"deploy_flat_files: keeping protected extra {name}")
            continue
        try:
            (dst_dir / name).unlink()
            result.removed.append(name)
        except OSError as exc:
            logger.warning(f"deploy_flat_files: unlink {name} failed: {exc}")


def _copy_flat_new_and_updated(
    src_files: dict[str, Path],
    dst_dir: Path,
    new_files: list[str],
    updated: list[str],
    result: CopyResult,
    logger: Any,
) -> None:
    """Copy new and updated files into ``dst_dir``, appending to ``result``."""
    for name in new_files + updated:
        src_path = src_files.get(name)
        if src_path is None:
            continue
        try:
            shutil.copy2(src_path, dst_dir / name)
        except OSError as exc:
            logger.warning(f"deploy_flat_files: copy {name} failed: {exc}")
            continue
        if name in new_files:
            result.added.append(name)
        else:
            result.updated.append(name)


def _log_flat_deploy(result: CopyResult, dst_dir: Path, logger: Any) -> None:
    """Emit the closing INFO/DEBUG line with the per-bucket counts."""
    if result.any_change:
        logger.info(
            f"deploy_flat_files({dst_dir}): "
            f"{len(result.added)} added, {len(result.updated)} updated, "
            f"{len(result.removed)} removed, {len(result.protected_kept)} protected-kept, "
            f"{len(result.unchanged)} unchanged"
        )
    else:
        logger.debug(f"deploy_flat_files({dst_dir}): no changes ({len(result.unchanged)} unchanged, {len(result.protected_kept)} protected-kept)")


def deploy_flat_files(
    src_files: dict[str, Path],
    dst_dir: Path,
    protect_patterns: list[str] | None = None,
    logger: Any = None,
) -> CopyResult:
    """Deploy a flat set of source files into ``dst_dir`` (§7.2, §4.11).

    ``src_files`` maps destination filename to source path. The source
    is a computed set (from the Prism index + side filter + closure),
    not a directory - hence a separate helper rather than a ``copy_tree``
    call over ``sync/downloads``.

    Only files ending in ``.jar`` in ``dst_dir`` are considered, per
    §2.9's drift definition and §4.11's flat-directory rule.

    Change detection is SHA-256 (§4.4). Unchanged files are left alone.
    Protected files (§3.13) that are not in the source set survive; a
    protected file that *is* in the source set is overwritten with
    source content.
    """
    if logger is None:
        logger = _log
    protect = protect_patterns or []
    dst_dir.mkdir(parents=True, exist_ok=True)
    logger.debug(f"deploy_flat_files: {len(src_files)} source(s) -> {dst_dir}")

    src_map = _hash_source_files(src_files, logger)
    dst_map = hash_flat_dir(dst_dir)
    new_files, updated, extras, unchanged = _split_flat_sets(src_map, dst_map)

    result = CopyResult()
    _remove_flat_extras(dst_dir, extras, protect, result, logger)
    _copy_flat_new_and_updated(src_files, dst_dir, new_files, updated, result, logger)
    result.unchanged = list(unchanged)

    _log_flat_deploy(result, dst_dir, logger)
    return result


# ===========================================================================
# §6  ZIP creation (§7.1)
# ===========================================================================


def _write_zip_entries(zf: zipfile.ZipFile, source_dir: Path, file_count: list[int]) -> None:
    """Write every regular file under ``source_dir`` into ``zf``.

    Arcnames are relative to ``source_dir`` with forward slashes.
    ``file_count`` is a one-element list so the caller can observe the
    number of files written (closures cannot rebind integers).
    """
    for dirpath, _dirnames, filenames in os.walk(source_dir, followlinks=False):
        for fname in sorted(filenames):
            full = Path(dirpath) / fname
            rel = full.relative_to(source_dir)
            zf.write(full, arcname=str(rel).replace(os.sep, "/"))
            file_count[0] += 1


def _make_zip_writer(source_dir: Path, file_count: list[int]) -> Callable[[Path], None]:
    """Return a writer callback suitable for :func:`_publish_atomically`."""

    def writer(tmp: Path) -> None:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            _write_zip_entries(zf, source_dir, file_count)

    return writer


def create_zip(source_dir: Path, output_zip: Path, logger: Any = None) -> None:
    """Zip the contents of ``source_dir`` into ``output_zip``, atomically.

    The archive contains paths relative to ``source_dir`` (no leading
    directory component). Atomic publication via :func:`_publish_atomically`
    means a partial ZIP can never appear at ``output_zip`` (§4.10).
    """
    if logger is None:
        logger = _log
    if not source_dir.is_dir():
        raise NotADirectoryError(f"source directory not found: {source_dir}")
    file_count = [0]
    logger.debug(f"create_zip: {source_dir} -> {output_zip}")
    _publish_atomically(output_zip, _make_zip_writer(source_dir, file_count), logger)
    logger.info(f"create_zip: wrote {file_count[0]} file(s) to {output_zip}")


# ===========================================================================
# §7  Shared destinations (§7.5)
# ===========================================================================


def is_shared_dest(value: str) -> bool:
    """Return True if ``value`` uses the ``@`` shared-destination prefix."""
    return isinstance(value, str) and value.startswith("@")


def _validate_at_www_segment(segment: str, value: str) -> None:
    """Raise ConfigError if ``segment`` is empty, dot, dot-dot, or contains NUL."""
    if not segment:
        raise ConfigError(f"empty segment in @www subpath: {value!r}")
    if segment in (".", ".."):
        raise ConfigError(f"dot or dot-dot segment in @www subpath: {value!r}")
    if "\x00" in segment:
        raise ConfigError(f"NUL byte in @www subpath: {value!r}")


def parse_shared_dest(value: str, logger: Any = None) -> tuple[str, str]:
    """Parse ``@www/<segment>(/<segment>)*`` per §7.5.

    Returns ``(prefix, subpath)`` - currently only ``prefix == "www"``
    is supported. ``subpath`` is the portion after ``@www/`` with no
    leading or trailing slash.

    Raises ConfigError on any malformed value.
    """
    if logger is None:
        logger = _log
    if not isinstance(value, str) or not value.startswith("@"):
        raise ConfigError(f"not a shared destination: {value!r}")
    head, sep, rest = value[1:].partition("/")
    if head != "www":
        raise ConfigError(f"Unsupported shared destination prefix @{head}: {value!r} (only @www is defined by §7.5)")
    if not sep or not rest:
        raise ConfigError(f"@www requires a non-empty subpath: {value!r}")
    for segment in rest.split("/"):
        _validate_at_www_segment(segment, value)
    logger.debug(f"parse_shared_dest: {value!r} -> (www, {rest!r})")
    return ("www", rest)


def resolve_shared_dest(value: str, www_dir: Path, logger: Any = None) -> Path:
    """Resolve ``@www/...`` to an absolute path under ``www_dir``.

    Raises ConfigError for any malformed value or non-``www`` prefix.
    """
    if logger is None:
        logger = _log
    prefix, subpath = parse_shared_dest(value, logger)
    assert prefix == "www"
    resolved = www_dir / subpath
    logger.debug(f"resolve_shared_dest: {value!r} -> {resolved}")
    return resolved


def resolve_resource_pack_dest(value: str, www_dir: Path, logger: Any = None) -> Path:
    """Return the directory under ``www_dir`` where resource packs land.

    ``value`` is the ``[sync_mapping].resourcepacks.resource_pack`` string,
    which must be a valid ``@www/...`` destination (§7.5).
    """
    if logger is None:
        logger = _log
    dest = resolve_shared_dest(value, www_dir, logger)
    logger.debug(f"resolve_resource_pack_dest: {value!r} -> {dest}")
    return dest


# ===========================================================================
# §8  Resource-pack misc (§3.9, §7.5)
# ===========================================================================


def resolve_mapping_for_side(mapping_value: Any, side: str) -> str | None:
    """Return the destination string for a side, or None if excluded.

    ``mapping_value`` may be:

      * a string - used for both sides
      * a dict with ``"server"`` / ``"client"`` keys - side-specific

    A dict's missing or ``None`` value for a side means the item is
    excluded on that side. A value of ``-1`` is also treated as
    excluded, matching the legacy convention.
    """
    if isinstance(mapping_value, str):
        return mapping_value
    if isinstance(mapping_value, dict):
        v = mapping_value.get(side)
        if v is None or v == -1:
            return None
        if not isinstance(v, str):
            return None
        return v
    return None


def validate_resource_pack_filename(name: str) -> None:
    r"""Validate a resource-pack filename per §7.5.

    Rules: non-empty, no ``/`` or ``\\``, no ``..`` anywhere, no leading
    ``.``, no NUL, ends in ``.zip``. Raises ConfigError on any violation.
    """
    if not name:
        raise ConfigError("resource-pack filename must be non-empty")
    if "\x00" in name:
        raise ConfigError("resource-pack filename must not contain NUL")
    if "/" in name or "\\" in name:
        raise ConfigError(f"resource-pack filename must not contain path separators: {name!r}")
    if name.startswith("."):
        raise ConfigError(f"resource-pack filename must not start with '.': {name!r}")
    if ".." in name:
        raise ConfigError(f"resource-pack filename must not contain '..': {name!r}")
    if not name.endswith(".zip"):
        raise ConfigError(f"resource-pack filename must end in '.zip': {name!r}")


def build_resource_pack_url(
    download_base_url: str,
    mapping_value: str,
    filename: str,
    logger: Any = None,
) -> str:
    """Compose the public URL for a resource-pack ZIP (§7.5).

    ``{base}/{subpath}/{filename}``, with ``base`` trailing-slash-stripped
    and ``subpath`` extracted from the ``@www/...`` mapping value.

    Raises ConfigError if the mapping value is malformed or the filename
    is invalid.
    """
    if logger is None:
        logger = _log
    if not download_base_url:
        raise ConfigError("download_base_url must be non-empty")
    validate_resource_pack_filename(filename)
    _prefix, subpath = parse_shared_dest(mapping_value, logger)
    base = download_base_url.rstrip("/")
    url = f"{base}/{subpath}/{filename}"
    logger.debug(f"build_resource_pack_url: -> {url}")
    return url
