# src/minecraft/modpack_upgrade/inhouse.py

"""In-house mod identification (Section 6.1, Section 9.5, A10).

Before slice 13 introduces a `.pw.toml` writer-level marker, in-house
artifacts are identified by filename in `config.d/upgrade.toml`:

    [in-house]
    filenames = ["custom-mod-1.0.0.jar", "another-custom.jar"]

The list is normalized (sorted lexicographically) before use so it
participates deterministically in `resolution_inputs_sha256` (Section A2.2).
A missing `[in-house]` table is equivalent to `filenames = []`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .errors import SchemaError

__all__ = ["load_in_house_filenames"]


def load_in_house_filenames(config_dir: Path) -> list[str]:
    """Return the sorted `[in-house].filenames` list, or `[]` when absent.

    Raises `SchemaError` if `[in-house]` is present but `filenames` is not,
    or if `filenames` is not a list of strings (per Section A10).
    """
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return []
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SchemaError(f"could not read {toml_path}: {exc}") from exc

    section = data.get("in-house")
    if section is None:
        return []
    if not isinstance(section, dict):
        raise SchemaError(f"{toml_path}: [in-house] must be a table")
    filenames = section.get("filenames")
    if filenames is None:
        raise SchemaError(f"{toml_path}: [in-house] present but filenames is missing")
    if not isinstance(filenames, list) or not all(isinstance(x, str) for x in filenames):
        raise SchemaError(f"{toml_path}: [in-house].filenames must be an array of strings")
    return sorted(filenames)
