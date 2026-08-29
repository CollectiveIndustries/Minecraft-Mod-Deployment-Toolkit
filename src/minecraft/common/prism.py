# src/minecraft/common/prism.py
"""Utilities for parsing Prism launcher .index files (.pw.toml)."""

import tomllib
from pathlib import Path


def parse_prism_toml(toml_path: Path) -> dict | None:
    """
    Parse a Prism .pw.toml file and return a dict with:
      - id:      (str) A unique identifier – falls back to filename if no project ID.
      - file:    (str) The JAR filename.
      - side:    (str) 'client', 'server', or 'both' (default 'both').
      - project_id: (int or None) CurseForge project ID if present.
      - file_id:    (int or None) CurseForge file ID if present.
      - display_name: (str) Human‑readable mod name.
      - source:  (str) 'curseforge', 'modrinth', or 'unknown'.

    Returns None only if the file cannot be parsed or no filename is present.
    """
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None

    filename = data.get("filename")
    if not filename:
        return None  # No filename – not a valid mod entry

    name = data.get("name", "")
    side = data.get("side", "both").lower()
    if side not in ("client", "server", "both"):
        side = "both"

    # Try to extract CurseForge or Modrinth IDs, but don't require them.
    cf_update = data.get("update", {}).get("curseforge")
    mr_update = data.get("update", {}).get("modrinth")

    project_id = None
    file_id = None
    source = "unknown"

    if cf_update:
        project_id = cf_update.get("project-id")
        file_id = cf_update.get("file-id")
        source = "curseforge"
    elif mr_update:
        # We don't need the IDs, but we can record the source
        source = "modrinth"

    # Use the filename as a fallback ID if we have no project ID
    mod_id = str(project_id) if project_id else filename

    return {
        "id": mod_id,
        "file": filename,
        "side": side,
        "project_id": project_id,
        "file_id": file_id,
        "display_name": name,
        "source": source,
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
