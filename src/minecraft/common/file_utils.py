# src/minecraft/common/file_utils.py

"""File utilities."""

import fnmatch
import hashlib
import os
import shutil
import zipfile
from pathlib import Path

import requests
from requests.exceptions import RequestException


def is_protected_path(rel_path: Path, protect_patterns: list[str] | None = None) -> bool:
    """Return True if rel_path must never be deleted during a clean.

    All protection is driven by the ``protect_patterns`` list, which is
    loaded from ``.deploy_protect`` at the repo root. A path is
    protected if either:

      - the full relative path matches a pattern
        (e.g. ``config/something/tokens.json``)
      - any single path component matches a pattern
        (e.g. ``tokens.json`` matches at any depth)

    Patterns use ``fnmatch`` glob syntax. There is no built-in baseline
    set anymore -- every protected path lives in ``.deploy_protect`` so
    there is exactly one place to look and one place to edit.

    An empty or missing pattern list means nothing is protected. That
    is a footgun for operations, but it is the correct failure mode for
    a data-driven policy: if the file is missing, the deploy refuses to
    be clever.
    """
    if not protect_patterns:
        return False
    parts = rel_path.parts
    rel_str = str(rel_path).replace("\\", "/")
    for pat in protect_patterns:
        if fnmatch.fnmatch(rel_str, pat):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def get_exclude_patterns(exclude_file_path: Path, logger) -> list:
    """Reads a file containing exclusion patterns, ignoring blank lines and comments.

    A single trailing slash is stripped so that ``world/`` behaves identically
    to ``world``. This matters because ``fnmatch`` treats ``/`` as a literal
    and paths produced during matching never end with a slash, so a pattern
    like ``logs/`` would otherwise match nothing.
    """
    patterns = []
    if not exclude_file_path.is_file():
        logger.warning(f"Exclude file not found: {exclude_file_path}")
        return patterns
    with exclude_file_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("/") and len(line) > 1:
                line = line.rstrip("/")
            patterns.append(line)
    return patterns


def get_protect_patterns(protect_file_path: Path, logger) -> list:
    """Reads ``.deploy_protect`` and returns the list of protection globs.

    Same format as the exclude file: one glob per line, blank lines and
    ``#`` comments ignored, single trailing slash stripped.

    Because ``.deploy_protect`` is committed to the repo and is the ONLY
    source of clean-phase protection, a missing file is treated as a
    fatal configuration error rather than a silent no-op. A pack whose
    protection list has been accidentally deleted is one deploy away
    from destroying server state; refusing to run is the safer default.
    """
    if not protect_file_path.is_file():
        raise FileNotFoundError(
            f"Protection file not found: {protect_file_path}. Create it or pass --no-deploy. Refusing to run a clean deploy without a protection list."
        )
    patterns: list[str] = []
    with protect_file_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("/") and len(line) > 1:
                line = line.rstrip("/")
            patterns.append(line)
    if not patterns:
        logger.warning(f"Protection file is empty: {protect_file_path}")
    else:
        logger.info(f"Loaded {len(patterns)} protect pattern(s) from {protect_file_path}")
    return patterns


def copy_directory_contents(src: Path, dst: Path, logger):
    """Copy all contents of src into dst (merging directories)."""
    if not src.exists():
        logger.warning(f"Source not found, skipping: {src}")
        return
    if not src.is_dir():
        logger.warning(f"Source is not a directory, skipping: {src}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dest_item = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest_item, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest_item)
    logger.debug(f"Copied {src} -> {dst}")


def copy_with_exclusions(
    src: Path,
    dst: Path,
    exclude_patterns: list,
    logger,
    clean: bool = False,
    protect_patterns: list | None = None,
):
    """Copy contents of src into dst, skipping excluded patterns.

    If clean=True, remove any files and directories in dst that are not
    part of the incoming sync, with two safety carve-outs:

      - files matching a pattern in ``protect_patterns`` are never removed
      - directories are only removed if they are empty after file removal

    The second rule is what makes protection work at any depth. A
    directory like ``config/`` is not itself protected when
    ``.deploy_protect`` lists ``tokens.json``, but its contents are. A
    naive directory sweep would rmtree ``config/`` and destroy the
    protected file inside it. By only removing empty directories, any
    directory containing a protected descendant survives automatically.
    """
    if not src.is_dir():
        raise NotADirectoryError(f"Source not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)

    def is_excluded(rel_path: str | Path) -> bool:
        return any(fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns)

    src_files: set[Path] = set()
    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for file in files:
            rel_path = rel_root / file
            if not is_excluded(rel_path):
                src_files.add(rel_path)

    if clean and dst.exists():
        # Pass 1: remove stale files (unprotected, unexcluded).
        for root, _dirs, files in os.walk(dst):
            rel_root = Path(root).relative_to(dst)
            for file in files:
                rel_path = rel_root / file
                if rel_path not in src_files and not is_excluded(rel_path) and not is_protected_path(rel_path, protect_patterns):
                    (dst / rel_path).unlink()
                    logger.debug(f"Removed extra file: {rel_path}")

        # Pass 2: remove empty directories bottom-up. A directory is
        # only removed if it is now empty, which means every file inside
        # was either removed as stale or never existed. Any directory
        # containing a protected file will fail rmdir and be kept.
        for root, _dirs, _files in os.walk(dst, topdown=False):
            rel_dir = Path(root).relative_to(dst)
            if rel_dir == Path("."):
                continue
            if is_excluded(rel_dir):
                continue
            if is_protected_path(rel_dir, protect_patterns):
                continue
            try:
                (dst / rel_dir).rmdir()
                logger.debug(f"Removed empty directory: {rel_dir}")
            except OSError:
                # Directory is not empty: something inside it is
                # protected, excluded, or was just written by an
                # earlier pass. Leave it alone.
                pass

    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for file in files:
            full_path = Path(root) / file
            rel_path = rel_root / file
            if is_excluded(rel_path):
                logger.debug(f"Skipping excluded: {rel_path}")
                continue
            dest_path = dst / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full_path, dest_path)
    logger.info(f"Copied with exclusions to {dst}")


def create_zip_from_staging(staging_dir: Path, output_zip: Path, exclude_patterns: list, logger):
    """Creates a ZIP archive from a staging directory, respecting exclude patterns and logging progress."""
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(staging_dir):
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(staging_dir)
                excluded = any(fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns)
                if excluded:
                    logger.debug(f"Skipping excluded: {rel_path}")
                    continue
                zf.write(full_path, arcname=str(rel_path))
    logger.info(f"Created zip: {output_zip}")


def download_file(url: str, output_path: Path):
    """Downloads a file from the given URL and saves it to the specified output path."""
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.writelines(response.iter_content(chunk_size=8192))


def compute_file_hash(filepath: Path, hash_format: str = "sha512") -> str:
    """Compute the hash of a file using the specified algorithm."""
    if not filepath.is_file():
        raise FileNotFoundError(f"File not found: {filepath}")
    hasher = hashlib.new(hash_format)
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_file_hash(filepath: Path, expected_hash: str, hash_format: str = "sha512") -> bool:
    """Return True if the file exists and its hash matches the expected value."""
    if not filepath.is_file():
        return False
    try:
        actual = compute_file_hash(filepath, hash_format)
        return actual.lower() == expected_hash.lower()
    except (OSError, ValueError):
        return False


def ensure_mod_file(filepath: Path, download_url: str | None, expected_hash: str | None, hash_format: str = "sha512", logger=None) -> bool:
    """Ensure the mod file exists and (if hash provided) matches the hash.

    If missing or hash mismatch, download from the given URL and verify again.
    Returns True if the file is present and valid after attempt.
    """
    if filepath.is_file() and (expected_hash is None or verify_file_hash(filepath, expected_hash, hash_format)):
        return True
    if download_url:
        try:
            if logger:
                logger.info(f"Downloading {filepath.name} from {download_url}")
            download_file(download_url, filepath)
            if expected_hash is not None:
                if verify_file_hash(filepath, expected_hash, hash_format):
                    return True
                else:
                    if logger:
                        logger.error(f"Downloaded file hash mismatch for {filepath.name}")
                    filepath.unlink(missing_ok=True)
                    return False
            else:
                return True
        except (RequestException, OSError) as e:
            if logger:
                logger.error(f"Failed to download {filepath.name}: {e}")
            return False
    else:
        if logger:
            logger.error(f"No download URL provided for {filepath.name} and file is missing or hash mismatch")
        return False
