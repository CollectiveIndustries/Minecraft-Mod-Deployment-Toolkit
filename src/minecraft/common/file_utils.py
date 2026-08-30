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

# Permanent protection – these files/directories are NEVER removed from destination
PROTECTED_PATHS = {
    # Root-level server files
    "server.properties",
    "eula.txt",
    "ops.json",
    "whitelist.json",
    "banned-ips.json",
    "banned-players.json",
    "usercache.json",
    "usernamecache.json",
    "user_jvm_args.txt",
    ".run-forge.env",
    ".forge-manifest.json",
    ".rcon-cli.env",
    ".rcon-cli.yaml",
    "run.sh",
    "run.bat",
    # Top-level directories that should never be removed
    "logs",
    "world",
    "world_nether",
    "world_the_end",
    "crash-reports",
    "patchouli_books",
    "schematics",
    "local",
    ".cache",
    ".mixin.out",
    "bin",
    "libraries",
    "defaultconfigs",
}


def _is_protected(rel_path: Path) -> bool:
    """Return True if the relative path (or any parent) is protected."""
    # Check exact match or any parent component
    parts = rel_path.parts
    for i in range(len(parts)):
        if parts[i] in PROTECTED_PATHS:
            return True
    return False


def get_exclude_patterns(exclude_file_path: Path, logger) -> list:
    patterns = []
    if not exclude_file_path.is_file():
        logger.warning(f"Exclude file not found: {exclude_file_path}")
        return patterns
    with exclude_file_path.open("r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
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
    src: Path, dst: Path, exclude_patterns: list, logger, clean: bool = False
):
    """
    Copy contents of src into dst, skipping excluded patterns.
    If clean=True, remove any files/dirs in dst that are not in src,
    EXCEPT those that are permanently protected (PROTECTED_PATHS) or excluded.
    """
    if not src.is_dir():
        raise NotADirectoryError(f"Source not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)

    def is_excluded(rel_path: str | Path) -> bool:
        return any(fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns)

    # Gather all relative paths in source (skipping excluded)
    src_files = set()
    src_dirs = set()
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        if rel_root != Path("."):
            src_dirs.add(rel_root)
        for file in files:
            full_path = Path(root) / file
            rel_path = rel_root / file
            if not is_excluded(rel_path):
                src_files.add(rel_path)

    # Clean destination: remove files/dirs not in src AND not excluded AND not protected
    if clean and dst.exists():
        for root, dirs, files in os.walk(dst):
            rel_root = Path(root).relative_to(dst)
            for file in files:
                rel_path = rel_root / file
                if (
                    rel_path not in src_files
                    and not is_excluded(rel_path)
                    and not _is_protected(rel_path)
                ):
                    (dst / rel_path).unlink()
                    logger.debug(f"Removed extra file: {rel_path}")
            for dir_name in dirs:
                rel_dir = rel_root / dir_name
                # Remove directory if:
                # - it's not in src_dirs
                # - no src file lives inside it
                # - it's not excluded
                # - it's not protected
                if (
                    rel_dir not in src_dirs
                    and not any(p.parent == rel_dir for p in src_files)
                    and not is_excluded(rel_dir)
                    and not _is_protected(rel_dir)
                ):
                    shutil.rmtree(dst / rel_dir)
                    logger.debug(f"Removed extra directory: {rel_dir}")

    # Copy source files (excluding patterns)
    for root, dirs, files in os.walk(src):
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


def create_zip_from_staging(
    staging_dir: Path, output_zip: Path, exclude_patterns: list, logger
):
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(staging_dir):
            for file in files:
                full_path = Path(root) / file
                rel_path = full_path.relative_to(staging_dir)
                excluded = any(
                    fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns
                )
                if excluded:
                    logger.debug(f"Skipping excluded: {rel_path}")
                    continue
                zf.write(full_path, arcname=str(rel_path))
    logger.info(f"Created zip: {output_zip}")


def download_file(url: str, output_path: Path):
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


def verify_file_hash(
    filepath: Path, expected_hash: str, hash_format: str = "sha512"
) -> bool:
    """Return True if the file exists and its hash matches the expected value."""
    if not filepath.is_file():
        return False
    try:
        actual = compute_file_hash(filepath, hash_format)
        return actual.lower() == expected_hash.lower()
    except (OSError, ValueError):
        return False


def ensure_mod_file(
    filepath: Path,
    download_url: str | None,
    expected_hash: str | None,
    hash_format: str = "sha512",
    logger=None,
) -> bool:
    """
    Ensure the mod file exists and (if hash provided) matches the hash.
    If missing or hash mismatch, download from the given URL and verify again.
    Returns True if the file is present and valid after attempt.
    """
    if filepath.is_file() and (
        expected_hash is None or verify_file_hash(filepath, expected_hash, hash_format)
    ):
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
                        logger.error(
                            f"Downloaded file hash mismatch for {filepath.name}"
                        )
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
            logger.error(
                f"No download URL provided for {filepath.name} and file is missing or hash mismatch"
            )
        return False
