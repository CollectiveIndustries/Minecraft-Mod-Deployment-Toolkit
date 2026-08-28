# src/minecraft/common/file_utils.py
"""File utilities."""

import fnmatch
import os
import shutil
import zipfile
from pathlib import Path

import requests


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
    """Copy contents of src into dst, skipping excluded patterns.
    If clean=True, remove any files/dirs in dst that are not in src (like rsync --delete)."""
    if not src.is_dir():
        raise NotADirectoryError(f"Source not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)

    # Gather all relative paths in source
    src_files = set()
    src_dirs = set()
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        if rel_root != Path("."):
            src_dirs.add(rel_root)
        for file in files:
            full_path = Path(root) / file
            rel_path = rel_root / file
            excluded = any(
                fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns
            )
            if not excluded:
                src_files.add(rel_path)

    # If clean, remove destination files/dirs not in source
    if clean and dst.exists():
        for root, dirs, files in os.walk(dst):
            rel_root = Path(root).relative_to(dst)
            for file in files:
                rel_path = rel_root / file
                if rel_path not in src_files:
                    (dst / rel_path).unlink()
                    logger.debug(f"Removed extra file: {rel_path}")
            for dir_name in dirs:
                rel_dir = rel_root / dir_name
                if rel_dir not in src_dirs and not any(
                    p.parent == rel_dir for p in src_files
                ):
                    shutil.rmtree(dst / rel_dir)
                    logger.debug(f"Removed extra directory: {rel_dir}")

    # Copy source files to destination
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for file in files:
            full_path = Path(root) / file
            rel_path = rel_root / file
            excluded = any(
                fnmatch.fnmatch(str(rel_path), pat) for pat in exclude_patterns
            )
            if excluded:
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
