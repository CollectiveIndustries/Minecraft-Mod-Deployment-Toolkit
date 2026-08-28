# src/minecraft/common/manifest.py
"""Manifest loading and filtering."""

from pathlib import Path

import yaml


def load_manifest(manifest_path: Path) -> list:
    """Load YAML manifest and return the 'mods' list."""
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    with manifest_path.open() as f:
        data = yaml.safe_load(f)
    return data.get("mods", [])


def filter_mods_by_side(mods: list, target_side: str) -> list:
    """
    Return mod entries where 'side' is 'both' or matches target_side.
    target_side: 'client' or 'server'
    """
    result = []
    for entry in mods:
        side = entry.get("side", "both").lower()
        if side == "both" or side == target_side:
            result.append(entry)
    return result
