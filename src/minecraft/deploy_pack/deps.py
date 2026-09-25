# src/minecraft/deploy_pack/deps.py

"""Prism index loading, dependency closure, and unmarked detection (§9.2).

Three responsibilities, in dependency order:

  1. **Prism index parsing** - read ``*.pw.toml`` files from
     ``<modpack_dir>/.index/`` and produce entry dicts. Ported from
     ``common/prism.py``, with the addition of ``side_raw`` (the
     original ``side`` string, lowercased, or None if absent) and
     ``index_file`` (the ``.pw.toml`` filename, for the audit UI).

  2. **Dependency closure** - expand a side-filtered seed with its
     transitive mandatory dependencies (§``common/deps.py``). Reads
     both the index's ``[[x-prismlauncher-dependencies]]`` blocks
     (project-id namespace) and each jar's ``META-INF/mods.toml``
     (modId namespace). The two namespaces are bridged by scanning
     every jar once for its declared modIds.

  3. **Unmarked detection** (§6.3) - report jars that have no
     ``.pw.toml`` or whose ``side`` is outside ``{client, server, both}``.
     The caller decides what to do: prompt (interactive) or skip
     (``--non-interactive``, §6.2).

Non-responsibilities:
  * Side overrides (``side_overrides.toml``) are loaded and applied by
    ``overrides.py``. This module sees entries *before* overrides.
  * The side filter itself (``filter_prism_entries_by_side``) is
    trivially here because both the closure and unmarked detection
    need the same entry shape.

Known limitation: an unmarked jar is invisible to the closure. A
``mandatory=true`` dependency on its modId cannot be satisfied. The fix
is to add a ``.pw.toml`` or an override; the tool does not guess.
"""

from __future__ import annotations

import logging
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .overrides import apply_side_overrides

__all__ = [
    "ClosureResult",
    "JarManifest",
    "UnmarkedJar",
    "clear_manifest_cache",
    "expand_with_required",
    "filter_prism_entries_by_side",
    "find_unmarked",
    "format_diagnostic",
    "is_unmarked",
    "load_prism_index",
    "parse_prism_toml",
    "remove_unmarked",
    "resolve_mod_sources",
    "scan_manifests",
]


_logger = logging.getLogger(__name__)


_VALID_SIDES = frozenset({"client", "server", "both"})


# ---------------------------------------------------------------------------
# Prism index parsing (ported and extended from common/prism.py)
# ---------------------------------------------------------------------------


def parse_prism_toml(toml_path: Path) -> dict | None:
    """Parse a Prism ``.pw.toml`` file and return an entry dict, or None.

    Fields:

      * ``id``           - unique identifier: CurseForge project-id,
                           Modrinth mod-id, or the filename as fallback.
      * ``file``         - the JAR filename.
      * ``side``         - coerced to ``client`` / ``server`` / ``both``
                           (invalid values become ``both``).
      * ``side_raw``     - the original ``side`` value, lowercased, or
                           ``None`` if the key was absent. §6.3 uses
                           this to detect unmarked entries.
      * ``index_file``   - the ``.pw.toml`` filename, for the audit UI
                           (§6.1).
      * ``project_id``   - CF project id (int) or Modrinth mod-id (str),
                           or None.
      * ``file_id``      - CF file-id (int) or Modrinth version id (str),
                           or None.
      * ``display_name`` - human-readable mod name.
      * ``source``       - ``curseforge`` / ``modrinth`` / ``unknown``.
      * ``download_url`` - direct download URL, or None.
      * ``hash_value``   - expected hash, or None.
      * ``hash_format``  - hash algorithm, default ``sha512``.
      * ``dependencies`` - list of ``{addon_id, type}`` dicts from the
                           ``[[x-prismlauncher-dependencies]]`` blocks.

    Returns None only if the file cannot be parsed or has no filename.
    """
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None

    filename = data.get("filename")
    if not filename:
        return None

    name = data.get("name", "")

    raw_side = data.get("side")
    if raw_side is None:
        side_raw: str | None = None
        side = "both"
    else:
        side_raw = str(raw_side).lower()
        side = side_raw if side_raw in _VALID_SIDES else "both"

    download = data.get("download", {})
    download_url = download.get("url")
    hash_value = download.get("hash")
    hash_format = download.get("hash-format", "sha512")

    cf_update = data.get("update", {}).get("curseforge")
    mr_update = data.get("update", {}).get("modrinth")

    project_id: int | str | None = None
    file_id: int | str | None = None
    source = "unknown"

    if cf_update:
        project_id = cf_update.get("project-id")
        file_id = cf_update.get("file-id")
        source = "curseforge"
    elif mr_update:
        project_id = mr_update.get("mod-id")
        file_id = mr_update.get("version")
        source = "modrinth"

    mod_id = str(project_id) if project_id else str(filename)

    raw_deps = data.get("x-prismlauncher-dependencies")
    dependencies: list[dict] = []
    if isinstance(raw_deps, list):
        for block in raw_deps:
            if not isinstance(block, dict):
                continue
            addon_id = block.get("addonId")
            if addon_id is None:
                continue
            dependencies.append(
                {
                    "addon_id": str(addon_id),
                    "type": str(block.get("type", "")).upper(),
                }
            )

    return {
        "id": mod_id,
        "file": str(filename),
        "side": side,
        "side_raw": side_raw,
        "index_file": toml_path.name,
        "project_id": project_id,
        "file_id": file_id,
        "display_name": name,
        "source": source,
        "download_url": download_url,
        "hash_value": hash_value,
        "hash_format": hash_format,
        "dependencies": dependencies,
    }


def load_prism_index(index_dir: Path) -> list[dict]:
    """Load all ``.pw.toml`` files from ``index_dir``.

    Returns entries in filesystem-glob order, which is stable for a given filesystem.
    """
    entries: list[dict] = []
    if not index_dir.is_dir():
        return entries
    for toml_path in index_dir.glob("*.pw.toml"):
        entry = parse_prism_toml(toml_path)
        if entry is not None:
            entries.append(entry)
    return entries


def filter_prism_entries_by_side(entries: list[dict], target_side: str) -> list[dict]:
    """Return entries whose ``side`` is ``both`` or matches ``target_side``.

    ``target_side`` must be ``"client"`` or ``"server"``. Entries with a
    coerced ``side`` of ``both`` are always included. Entries whose
    *raw* side was invalid were coerced to ``both`` by the parser, so
    they appear on both sides - unmarked detection (§6.3) is what
    filters them out, before this function is called.
    """
    if target_side not in ("client", "server"):
        raise ValueError(f"target_side must be 'client' or 'server', got {target_side!r}")
    return [e for e in entries if e.get("side") in ("both", target_side)]


# ---------------------------------------------------------------------------
# Jar manifests (ported from common/deps.py)
# ---------------------------------------------------------------------------


@dataclass
class JarManifest:
    """Dependencies declared inside a jar's ``META-INF/mods.toml``."""

    filename: str
    mod_ids: set[str] = field(default_factory=set)
    required_client: set[str] = field(default_factory=set)
    required_server: set[str] = field(default_factory=set)


_MANIFEST_CACHE: dict[Path, dict[str, JarManifest]] = {}


def clear_manifest_cache() -> None:
    """Clear the per-modpack manifest cache.

    The cache is process-lifetime and keyed by ``modpack_dir.resolve()``.
    Tests should call this between runs; production code does not need
    to, because one deploy uses one modpack_dir for its lifetime.
    """
    _MANIFEST_CACHE.clear()


def _read_jar_manifest(jar_path: Path, logger) -> JarManifest | None:
    """Parse ``META-INF/mods.toml`` from a jar. Never raises."""
    if not jar_path.is_file():
        return None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            data = None
            for candidate in (
                "META-INF/mods.toml",
                "META-INF/neoforge.mods.toml",
            ):
                try:
                    raw = zf.read(candidate)
                except KeyError:
                    continue
                try:
                    data = tomllib.loads(raw.decode("utf-8"))
                except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                    if logger is not None:
                        logger.warning(f"Could not parse {candidate} in {jar_path.name}: {exc}")
                    return None
                break
            if data is None:
                return None
    except (zipfile.BadZipFile, OSError) as exc:
        if logger is not None:
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
    """Read every entry's jar manifest, keyed by filename.

    Cached per ``modpack_dir`` for the lifetime of the process. The
    multiple ``load_mod_list``-style calls in one deploy therefore only
    pay the scan cost once. Call ``clear_manifest_cache`` to reset.
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


# ---------------------------------------------------------------------------
# Closure (ported from common/deps.py)
# ---------------------------------------------------------------------------


@dataclass
class ClosureResult:
    """The result of expanding a side-filtered seed with required deps."""

    entries: list[dict]
    seed_ids: set[str]
    forced: list[tuple[dict, dict, str]]

    @property
    def forced_count(self) -> int:
        """Number of entries that were force-included by the closure."""
        return len(self.entries) - len(self.seed_ids)

    def grouped(
        self,
    ) -> dict[str, tuple[dict, list[tuple[dict, str]]]]:
        """Return ``{dep_id: (dep_entry, [(dependent, reason), ...])}``."""
        grouped: dict[str, tuple[dict, list[tuple[dict, str]]]] = {}
        for dependent, dependency, reason in self.forced:
            dep_id = str(dependency.get("id"))
            if dep_id not in grouped:
                grouped[dep_id] = (dependency, [])
            grouped[dep_id][1].append((dependent, reason))
        return grouped


def _build_lookup(
    entries: list[dict],
    manifests: dict[str, JarManifest],
    logger,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Return (by_id, by_project, by_modid)."""
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
                if logger is not None:
                    logger.debug(f"modId {mid!r} provided by both {existing.get('file')!r} and {filename!r}; using the first")
                continue
            entry_by_modid[mid] = entry
    return entry_by_id, entry_by_project, entry_by_modid


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


def _jar_deps(
    entry: dict,
    manifests: dict[str, JarManifest],
    target_side: str,
) -> set[str]:
    """ModIds that this entry's jar marks REQUIRED for the target side."""
    filename = entry.get("file")
    manifest = manifests.get(filename) if filename else None
    if manifest is None:
        return set()
    if target_side == "client":
        return set(manifest.required_client)
    return set(manifest.required_server)


def expand_with_required(
    all_entries: list[dict],
    seed_entries: list[dict],
    target_side: str,
    modpack_dir: Path,
    logger,
) -> ClosureResult:
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


def format_diagnostic(
    all_entries: list[dict],
    seeds: dict[str, list[dict]],
    modpack_dir: Path,
    logger,
) -> str:
    """Multi-section human-readable diagnostic for ``--debug-deps``.

    ``seeds`` maps ``"client"`` / ``"server"`` to a side-filtered entry
    list. For each side, prints the seed count, closure count, and every
    force-included entry with its dependents.
    """
    lines: list[str] = []
    lines.append("=== Dependency closure diagnostic ===")
    lines.append(f"Modpack dir:         {modpack_dir}")
    lines.append(f"Total mods in index: {len(all_entries)}")
    lines.append("")
    for side in ("client", "server"):
        seed = seeds.get(side, [])
        result = expand_with_required(
            all_entries=all_entries,
            seed_entries=seed,
            target_side=side,
            modpack_dir=modpack_dir,
            logger=logger,
        )
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
            lines.append(f"  + {dep_entry.get('file')}  (declared side={side_str!r})")
            for dependent, reason in dependents:
                lines.append(f"      <- {dependent.get('file')}  [{reason}]")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Unmarked detection (§6.3)
# ---------------------------------------------------------------------------


@dataclass
class UnmarkedJar:
    """A jar that §6.3 classifies as unmarked."""

    filename: str
    reason: str
    """Human-readable: ``"no .pw.toml"`` or
    ``"declared side 'skipped' is outside {client, server, both}"``."""


def find_unmarked(modpack_dir: Path, entries: list[dict]) -> list[UnmarkedJar]:
    """Return jars that are unmarked per §6.3.

    Two sources, both reported:

      1. ``*.jar`` files in ``modpack_dir`` whose filename does not
         appear as any entry's ``file`` field.
      2. Entries whose ``side_raw`` is present but not in
         ``{client, server, both}``.

    ``side_raw is None`` (no ``side`` key in the .pw.toml) is treated as
    marked, matching the parser's default of ``"both"``.

    Returns results sorted by filename, with no duplicates: a jar that
    is unmarked by rule (1) cannot also be unmarked by rule (2), because
    rule (2) requires an index entry.

    Raises ConfigError if ``modpack_dir`` is not a directory.
    """
    if not modpack_dir.is_dir():
        raise ConfigError(f"modpack_dir is not a directory: {modpack_dir}")

    indexed_files: set[str] = {str(e["file"]) for e in entries if e.get("file")}

    by_filename: dict[str, UnmarkedJar] = {}

    for jar in modpack_dir.glob("*.jar"):
        if jar.name not in indexed_files:
            by_filename[jar.name] = UnmarkedJar(
                filename=jar.name,
                reason="no .pw.toml",
            )

    for entry in entries:
        raw = entry.get("side_raw")
        if raw is None:
            continue
        if raw in _VALID_SIDES:
            continue
        filename = entry.get("file")
        if not filename:
            continue
        by_filename[str(filename)] = UnmarkedJar(
            filename=str(filename),
            reason=(f"declared side {raw!r} is outside {{client, server, both}}"),
        )

    return [by_filename[name] for name in sorted(by_filename)]


def remove_unmarked(entries: list[dict], unmarked: list[UnmarkedJar]) -> list[dict]:
    """Return ``entries`` with every unmarked filename removed.

    Used by ``--non-interactive`` (§6.2): unmarked mods are not
    deployed, not written, and remain unmarked. Interactive callers
    instead prompt for each unmarked entry and splice the decisions
    back in via ``overrides.py``.
    """
    drop = {u.filename for u in unmarked}
    return [e for e in entries if e.get("file") not in drop]


# ---------------------------------------------------------------------------
# Shared mod-source resolution (used by preflight, scope_server, scope_client)
# ---------------------------------------------------------------------------


_UNMARKED_VALID_SIDES = frozenset({"client", "server", "both"})


def is_unmarked(entry: dict) -> bool:
    """§6.3: an entry whose declared side is outside {client, server, both}.

    ``side_raw is None`` means the ``.pw.toml`` had no ``side`` key at all;
    the parser defaults that to ``"both"``, so such an entry is marked.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in _UNMARKED_VALID_SIDES


def resolve_mod_sources(
    modpack_dir: Path,
    target_side: str,
    overrides: Any,
) -> dict[str, Path]:
    """Prism index -> marked -> overridden -> side-filtered -> closed -> {filename: path}.

    The shared "intent set" used by preflight's change computation and by
    both write scopes. ``overrides`` must be a
    :class:`minecraft.deploy_pack.overrides.SideOverrides` instance; it is
    duck-typed here to avoid a circular import.
    """
    index_dir = modpack_dir / ".index"
    if not index_dir.is_dir():
        return {}
    entries = load_prism_index(index_dir)
    if not entries:
        return {}
    marked = [e for e in entries if (not is_unmarked(e)) or overrides.matches(e)]
    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)
    side_entries = filter_prism_entries_by_side(marked, target_side)
    closure = expand_with_required(
        all_entries=marked,
        seed_entries=side_entries,
        target_side=target_side,
        modpack_dir=modpack_dir,
        logger=_logger,
    )
    out: dict[str, Path] = {}
    for entry in closure.entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = modpack_dir / filename
        if not path.is_file():
            _logger.warning(f"mod source missing from disk, skipping: {filename}")
            continue
        out[str(filename)] = path
    return out
