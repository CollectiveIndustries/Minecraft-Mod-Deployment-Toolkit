# src/minecraft/modpack_upgrade/side_determination.py

"""Final side determination at materialize time (Side_Determination Section 1-Section 4).

At materialize the jar bytes are present, so the second step of the
precedence chain - jar metadata - becomes available. The chain is:

    override > jar metadata > provider > .pw.toml > unknown

``resolve_final_side`` returns ``(side, source)``. The caller treats
``side == "unknown"`` as exit 3 (Section 7 of Side_Determination).

The JAR reader is deliberately narrow:

  * Fabric / Quilt: ``fabric.mod.json`` or ``quilt.mod.json``
    ``environment`` field (Section 3.1). Absent -> ``"both"``. Array form ->
    ``None`` (requires override).
  * Forge / NeoForge: ``META-INF/mods.toml`` (or
    ``META-INF/neoforge.mods.toml``) ``[[mods]] clientSideOnly = true``
    (Section 3.2). Absent or false -> ``None`` (skip to provider).

The "``None`` means skip to provider" contract is the entire reason
this module exists: JAR metadata is a hint, not a mandate. Only an
unambiguous signal returns a side; everything else falls through.
"""

from __future__ import annotations

import json
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from LoggingCore import get_logger

__all__ = ["SideResolution", "resolve_final_side"]

_log = get_logger(__name__)

_FABRIC_MANIFESTS = ("fabric.mod.json", "quilt.mod.json")
_FORGE_MANIFESTS = ("META-INF/mods.toml", "META-INF/neoforge.mods.toml")
_VALID_SIDES = frozenset({"client", "server", "both"})


@dataclass(frozen=True)
class SideResolution:
    """Outcome of the precedence chain."""

    side: str
    source: str


def resolve_final_side(
    *,
    filename: str,
    project_id: str | None,
    jar_path: Path | None,
    loader: str,
    side_override_result: str | None,
    provider_side: str | None,
    side_declared: str,
) -> SideResolution:
    """Apply the precedence chain and return ``(side, source)``.

    Precedence per Side_Determination Section 1:

      1. override (final authority)
      2. jar metadata for the target loader
      3. provider metadata
      4. .pw.toml side field
      5. unknown -> caller exits 3
    """
    if side_override_result is not None:
        return SideResolution(side=side_override_result, source="override")
    if jar_path is not None and jar_path.is_file():
        jar_side = _jar_side(jar_path, loader)
        if jar_side is not None:
            _log.debug(f"side: {filename} resolved from jar metadata ({jar_side})")
            return SideResolution(side=jar_side, source="jar")
    if provider_side is not None and provider_side != "unknown":
        _log.debug(f"side: {filename} resolved from provider metadata ({provider_side})")
        return SideResolution(side=provider_side, source="provider")
    if side_declared in _VALID_SIDES:
        _log.warning(f"side: {filename} side from .pw.toml. Add override to be sure.")
        return SideResolution(side=side_declared, source="pw_toml")
    return SideResolution(side="unknown", source="unknown")


def _jar_side(jar_path: Path, loader: str) -> str | None:
    """Return the jar metadata side for ``loader``, or ``None``."""
    loader_lower = loader.lower()
    if loader_lower in ("fabric", "quilt"):
        return _fabric_side(jar_path)
    if loader_lower in ("forge", "neoforge"):
        return _forge_side(jar_path)
    return None


def _fabric_side(jar_path: Path) -> str | None:
    """Read ``environment`` from fabric.mod.json or quilt.mod.json (Section 3.1)."""
    for manifest_name in _FABRIC_MANIFESTS:
        parsed = _read_json_from_jar(jar_path, manifest_name)
        if parsed is None or not isinstance(parsed, dict):
            continue
        environment = parsed.get("environment", "*")
        if environment == "*":
            return "both"
        if environment == "client":
            return "client"
        if environment == "server":
            return "server"
        # Array form or unknown value -> requires override
        return None
    return None


def _forge_side(jar_path: Path) -> str | None:
    """Read ``clientSideOnly = true`` from mods.toml (Section 3.2)."""
    for manifest_name in _FORGE_MANIFESTS:
        parsed = _read_toml_from_jar(jar_path, manifest_name)
        if parsed is None or not isinstance(parsed, dict):
            continue
        for mod_entry in parsed.get("mods", []) or []:
            if isinstance(mod_entry, dict) and mod_entry.get("clientSideOnly") is True:
                return "client"
        # Present but no clientSideOnly=true signal -> ambiguous, skip
        return None
    return None


def _read_json_from_jar(jar_path: Path, entry_name: str) -> object | None:
    """Read and parse a JSON entry from a jar; None on any failure."""
    data = _read_from_jar(jar_path, entry_name)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_toml_from_jar(jar_path: Path, entry_name: str) -> object | None:
    """Read and parse a TOML entry from a jar; None on any failure."""
    data = _read_from_jar(jar_path, entry_name)
    if data is None:
        return None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def _read_from_jar(jar_path: Path, entry_name: str) -> bytes | None:
    """Read one file's bytes from a jar; None on any failure."""
    try:
        with zipfile.ZipFile(jar_path) as zf:
            try:
                return zf.read(entry_name)
            except KeyError:
                return None
    except (zipfile.BadZipFile, OSError):
        return None
