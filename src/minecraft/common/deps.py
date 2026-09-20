# src/minecraft/common/deps.py

"""Dependency closure for the modpack side split.

Mod authors and platform metadata are not reliable sources of truth for
which jars need to be present at load time. A .pw.toml can declare
side='server' for a library that a client-side mod also requires. When
that happens, the client ZIP ships without the library and Forge
refuses to start with "mod X requires Y, Y is not installed".

This module computes a transitive closure over mandatory (REQUIRED)
dependencies and force-includes anything the side filter would have
excluded. Two sources of edges are used:

  1. Prism's [[x-prismlauncher-dependencies]] blocks -- fast, already
     in the index, uses the project-id namespace.
  2. The jar's META-INF/mods.toml [[dependencies.<modid>]] blocks --
     authoritative, uses the modId namespace, read from disk.

The two namespaces are bridged by scanning every jar once for its
declared modIds. Both sources are unioned; there is no attempt to
decide which is "more correct". Including a dependency that only one
source declares is safe. Missing one that either source declares is
what produces the crash.

Everything is read fresh on each build. No sidecar file, no pinned
versions, no hardcoded overrides. When upstream metadata improves,
the closure simply gets smaller.
"""

from __future__ import annotations

import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ClosureResult", "JarManifest", "expand_with_required", "format_diagnostic", "scan_manifests"]


@dataclass
class JarManifest:
    """Dependencies declared inside a jar's META-INF/mods.toml."""

    filename: str
    mod_ids: set[str] = field(default_factory=set)
    required_client: set[str] = field(default_factory=set)
    required_server: set[str] = field(default_factory=set)


_MANIFEST_CACHE: dict[Path, dict[str, JarManifest]] = {}


def _read_jar_manifest(jar_path: Path, logger) -> JarManifest | None:
    """Parse META-INF/mods.toml from a jar. Never raises."""
    if not jar_path.is_file():
        return None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            data = None
            for candidate in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                try:
                    raw = zf.read(candidate)
                except KeyError:
                    continue
                try:
                    data = tomllib.loads(raw.decode("utf-8"))
                except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                    logger.warning(f"Could not parse {candidate} in {jar_path.name}: {exc}")
                    return None
                break
            if data is None:
                return None
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(f"Could not read jar {jar_path.name}: {exc}")
        return None
    manifest = JarManifest(filename=jar_path.name)
    for mod_block in data.get("mods") or []:
        if isinstance(mod_block, dict):
            mid = mod_block.get("modId")
            if mid:
                manifest.mod_ids.add(str(mid))
    deps_table = data.get("dependencies") or {}
    if not isinstance(deps_table, dict):
        return manifest
    for dep_list in deps_table.values():
        if not isinstance(dep_list, list):
            continue
        for dep in dep_list:
            if not isinstance(dep, dict):
                continue
            if not dep.get("mandatory", False):
                continue
            dep_mod_id = dep.get("modId")
            if not dep_mod_id:
                continue
            dep_mod_id = str(dep_mod_id)
            side = str(dep.get("side", "BOTH")).upper()
            if side in ("BOTH", "CLIENT"):
                manifest.required_client.add(dep_mod_id)
            if side in ("BOTH", "SERVER"):
                manifest.required_server.add(dep_mod_id)
    return manifest


def scan_manifests(entries: list[dict], modpack_dir: Path, logger) -> dict[str, JarManifest]:
    """Read the manifest of every entry's jar, keyed by filename.

    Cached per modpack_dir for the lifetime of the process, so the
    multiple load_mod_list() calls in a single deploy only pay the scan
    cost once.
    """
    key = modpack_dir.resolve()
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        return cached
    result: dict[str, JarManifest] = {}
    for entry in entries:
        filename = entry.get("file")
        if not filename:
            continue
        manifest = _read_jar_manifest(modpack_dir / filename, logger)
        if manifest is not None:
            result[filename] = manifest
    _MANIFEST_CACHE[key] = result
    return result


@dataclass
class ClosureResult:
    """The result of expanding a side-filtered seed with required deps."""

    entries: list[dict]
    seed_ids: set[str]
    forced: list[tuple[dict, dict, str]]

    @property
    def forced_count(self) -> int:
        """Counts the number of forced entries.

        Returns:
            int: The number of forced entries.
        """
        return len(self.entries) - len(self.seed_ids)

    def grouped(self) -> dict[str, tuple[dict, list[tuple[dict, str]]]]:
        """Return {dep_id: (dep_entry, [(dependent, reason), ...])}."""
        grouped: dict[str, tuple[dict, list[tuple[dict, str]]]] = {}
        for dependent, dependency, reason in self.forced:
            dep_id = str(dependency.get("id"))
            if dep_id not in grouped:
                grouped[dep_id] = (dependency, [])
            grouped[dep_id][1].append((dependent, reason))
        return grouped


def _build_lookup(entries: list[dict], manifests: dict[str, JarManifest], logger) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Return (entry_by_id, entry_by_project, entry_by_modid)."""
    entry_by_id: dict[str, dict] = {}
    entry_by_project: dict[str, dict] = {}
    entry_by_modid: dict[str, dict] = {}
    for entry in entries:
        eid = entry.get("id")
        if not eid:
            continue
        entry_by_id[str(eid)] = entry
        pid = entry.get("project_id")
        if pid:
            entry_by_project[str(pid)] = entry
        filename = entry.get("file")
        manifest = manifests.get(filename) if filename else None
        if manifest is None:
            continue
        for mid in manifest.mod_ids:
            existing = entry_by_modid.get(mid)
            if existing is not None and existing is not entry:
                logger.debug(f"modId '{mid}' provided by both {existing.get('file')!r} and {filename!r}; using the first")
                continue
            entry_by_modid[mid] = entry
    return (entry_by_id, entry_by_project, entry_by_modid)


def _index_deps(entry: dict) -> set[str]:
    """Project ids that this entry's index metadata marks REQUIRED."""
    result: set[str] = set()
    for dep in entry.get("dependencies") or []:
        if str(dep.get("type", "")).upper() != "REQUIRED":
            continue
        addon_id = dep.get("addon_id")
        if addon_id is not None:
            result.add(str(addon_id))
    return result


def _jar_deps(entry: dict, manifests: dict[str, JarManifest], target_side: str) -> set[str]:
    """ModIds that this entry's jar marks REQUIRED for the target side."""
    filename = entry.get("file")
    manifest = manifests.get(filename) if filename else None
    if manifest is None:
        return set()
    if target_side == "client":
        return set(manifest.required_client)
    return set(manifest.required_server)


def expand_with_required(all_entries: list[dict], seed_entries: list[dict], target_side: str, modpack_dir: Path, logger) -> ClosureResult:
    """Expand a side-filtered seed with its transitive mandatory deps.

    ``target_side`` must be ``"client"`` or ``"server"``. Entries whose
    own ``side`` excludes them from the seed are still pulled in if
    something already in the set requires them.

    The returned list preserves ``all_entries`` order so output is
    deterministic across builds.
    """
    side = target_side.lower()
    if side not in ("client", "server"):
        raise ValueError(f"target_side must be 'client' or 'server', got {target_side!r}")
    manifests = scan_manifests(all_entries, modpack_dir, logger)
    entry_by_id, entry_by_project, entry_by_modid = _build_lookup(all_entries, manifests, logger)
    seed_ids: set[str] = {str(e.get("id")) for e in seed_entries if e.get("id")}
    closure: set[str] = set(seed_ids)
    worklist: list[str] = list(seed_ids)
    forced: list[tuple[dict, dict, str]] = []
    while worklist:
        current_id = worklist.pop()
        entry = entry_by_id.get(current_id)
        if entry is None:
            continue
        for project_id in _index_deps(entry):
            dep_entry = entry_by_project.get(project_id)
            if dep_entry is None:
                continue
            dep_id = str(dep_entry.get("id"))
            if dep_id in closure:
                continue
            closure.add(dep_id)
            worklist.append(dep_id)
            forced.append((entry, dep_entry, f"index addonId={project_id}"))
        for dep_mod_id in _jar_deps(entry, manifests, side):
            dep_entry = entry_by_modid.get(dep_mod_id)
            if dep_entry is None:
                continue
            dep_id = str(dep_entry.get("id"))
            if dep_id in closure:
                continue
            closure.add(dep_id)
            worklist.append(dep_id)
            forced.append((entry, dep_entry, f"jar modId={dep_mod_id}"))
    ordered = [e for e in all_entries if str(e.get("id")) in closure]
    return ClosureResult(entries=ordered, seed_ids=seed_ids, forced=forced)


def format_diagnostic(all_entries: list[dict], seeds: dict[str, list[dict]], modpack_dir: Path, logger) -> str:
    """Return a multi-section human-readable diagnostic string.

    ``seeds`` maps side name ("client"/"server") to the side-filtered
    entry list. For each side this prints the seed count, closure
    count, and every force-included entry with the dependents that
    pulled it in.
    """
    lines: list[str] = []
    lines.append("=== Dependency closure diagnostic ===")
    lines.append(f"Modpack dir:         {modpack_dir}")
    lines.append(f"Total mods in index: {len(all_entries)}")
    lines.append("")
    for side in ("client", "server"):
        seed = seeds.get(side, [])
        result = expand_with_required(all_entries=all_entries, seed_entries=seed, target_side=side, modpack_dir=modpack_dir, logger=logger)
        lines.append(f"--- Side: {side} ---")
        lines.append(f"  Seed (side filter): {len(seed)} mods")
        lines.append(f"  Closure:            {len(result.entries)} mods")
        lines.append(f"  Force-included:     {result.forced_count}")
        if not result.forced:
            lines.append("  (nothing needed to be force-included)")
            lines.append("")
            continue
        lines.append("")
        grouped = result.grouped()
        for dep_id in sorted(grouped.keys()):
            dep_entry, dependents = grouped[dep_id]
            side_str = dep_entry.get("side", "?")
            lines.append(f"  + {dep_entry.get('file')}  (declared side='{side_str}')")
            for dependent, reason in dependents:
                lines.append(f"      <- {dependent.get('file')}  [{reason}]")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
