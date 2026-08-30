# src/minecraft/common/overrides.py
"""Side override handling for Prism mod entries."""

import tomllib
from pathlib import Path


def load_side_overrides(override_path: Path) -> dict[str, str]:
    """Load side overrides from a TOML file.

    Expected format:
        [by_id]
        "123" = "client"
        "456" = "server"
        [by_filename]
        "invtweaks-1.20.1-1.2.2.jar" = "client"
    Returns a dict mapping identifier to side ('client', 'server', 'both').
    """
    if not override_path.is_file():
        return {}

    try:
        with open(override_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return {}

    result = {}
    # Merge both sections
    for section in ["by_id", "by_filename"]:
        section_data = data.get(section, {})
        if not isinstance(section_data, dict):
            continue
        for key, value in section_data.items():
            if value in ("client", "server", "both"):
                result[str(key)] = value
    return result


def apply_side_overrides(entries: list[dict], overrides: dict[str, str]) -> list[dict]:
    """Apply side overrides to a list of Prism index entries (in-place).

    Overrides are checked against entry['id'] and entry['file'].
    """
    for entry in entries:
        mod_id = str(entry.get("id", ""))
        file_name = entry.get("file", "")
        if mod_id in overrides:
            entry["side"] = overrides[mod_id]
        elif file_name in overrides:
            entry["side"] = overrides[file_name]
    return entries
