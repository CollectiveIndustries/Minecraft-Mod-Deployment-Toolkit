# src/minecraft/common/prism.py
"""Utilities for parsing Prism launcher .index files (.pw.toml)."""

import tomllib
from pathlib import Path


def parse_prism_toml(toml_path: Path) -> dict | None:
    """
    Parse a Prism .pw.toml file and return a dict with:
      - id: project_id as string
      - file: filename
      - side: 'client', 'server', or 'both'
      - project_id: int
      - file_id: int
      - display_name: str
      - source: 'curseforge'
    Returns None if CurseForge data is missing or unsupported.
    """
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None

    filename = data.get("filename")
    name = data.get("name", "")
    side = data.get("side", "both").lower()
    if not side:
        side = "both"

    # CurseForge update info
    cf_update = data.get("update", {}).get("curseforge")
    if not cf_update:
        return None  # only CurseForge supported

    project_id = cf_update.get("project-id")
    file_id = cf_update.get("file-id")
    if not project_id or not file_id:
        return None

    return {
        "id": str(project_id),
        "file": filename,
        "side": side,
        "project_id": project_id,
        "file_id": file_id,
        "display_name": name,
        "source": "curseforge",
    }


def load_prism_index(index_dir: Path) -> list[dict]:
    """Load all .pw.toml files from index_dir and return parsed entries."""
    entries = []
    for toml_path in index_dir.glob("*.pw.toml"):
        entry = parse_prism_toml(toml_path)
        if entry is not None:
            entries.append(entry)
    return entries


def filter_prism_entries_by_side(entries: list[dict], target_side: str) -> list[dict]:
    """Return entries where side is 'both' or matches target_side."""
    result = []
    for entry in entries:
        side = entry.get("side", "both")
        if side == "both" or side == target_side:
            result.append(entry)
    return result
