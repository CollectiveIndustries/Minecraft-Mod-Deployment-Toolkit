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

Structure
---------

The module is organised into six sections with explicit banner
comments. Each section corresponds to one responsibility and its
helpers are only called from within that section or from the top-level
public API.

    §1  Entry model + constants
    §2  Prism index parsing          (§3.11 source model)
    §3  Jar manifest scanning        (§6.3 + closure input)
    §4  Dependency closure           (§4.4 change detection input)
    §5  Diagnostic formatting        (--debug-deps)
    §6  Unmarked detection + mod source resolution (§6.3, §9.2)

Aggressive decomposition: every function does exactly one thing. The
helpers below the public entry points are deliberately small and
single-purpose so that a failure inside e.g. jar parsing can be traced
to a specific helper without reading the entire module.

Logging
-------

Parsing, filtering, closure, and unmarked detection each log coarse
milestones at INFO and per-entry detail at DEBUG. Skips and misconfig
that a user needs to act on log at WARN. The module logger is
``minecraft.deploy_pack.deps``; callers may inject an override via
``logger=`` for a single call.
"""

from __future__ import annotations

import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .logging_setup import get_logger
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

_log = get_logger(__name__)


# ===========================================================================
# §1  Entry model + constants
# ===========================================================================


_VALID_SIDES = frozenset({"client", "server", "both"})

# Tried in order; the first one that parses wins (§3.11 source model).
_MANIFEST_CANDIDATES = ("META-INF/mods.toml", "META-INF/neoforge.mods.toml")


# ===========================================================================
# §2  Prism index parsing
# ===========================================================================


def _read_prism_toml_file(toml_path: Path, logger: Any) -> dict | None:
    """Open and TOML-parse a ``.pw.toml``. Returns None on any read/parse failure."""
    try:
        with open(toml_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        logger.warning(f"{toml_path}: malformed TOML; skipping ({exc})")
        return None
    except OSError as exc:
        logger.warning(f"{toml_path}: could not read; skipping ({exc})")
        return None


def _normalize_side(raw_side: Any, toml_path: Path, logger: Any) -> tuple[str, str | None]:
    """Return ``(coerced_side, side_raw)``.

    ``side_raw`` is the lowercased original, or None if the key was
    absent. ``coerced_side`` is inside ``_VALID_SIDES``; anything
    outside is coerced to ``"both"`` while ``side_raw`` is preserved
    for §6.3's unmarked check.
    """
    if raw_side is None:
        return ("both", None)
    side_raw = str(raw_side).lower()
    if side_raw in _VALID_SIDES:
        return (side_raw, side_raw)
    logger.debug(f"{toml_path}: side={side_raw!r} outside valid set; coercing to 'both' (side_raw preserved)")
    return ("both", side_raw)


def _parse_update_block(data: dict) -> tuple[int | str | None, int | str | None, str]:
    """Return ``(project_id, file_id, source)`` from the update block.

    CurseForge wins over Modrinth when both are present (matches the
    original ``if/elif`` order). ``source`` is one of ``curseforge``,
    ``modrinth``, ``unknown``.
    """
    cf_update = data.get("update", {}).get("curseforge")
    if cf_update:
        return (cf_update.get("project-id"), cf_update.get("file-id"), "curseforge")
    mr_update = data.get("update", {}).get("modrinth")
    if mr_update:
        return (mr_update.get("mod-id"), mr_update.get("version"), "modrinth")
    return (None, None, "unknown")


def _parse_prism_dependencies(data: dict) -> list[dict]:
    """Extract ``[[x-prismlauncher-dependencies]]`` blocks into normalised dicts."""
    raw_deps = data.get("x-prismlauncher-dependencies")
    deps: list[dict] = []
    if not isinstance(raw_deps, list):
        return deps
    for block in raw_deps:
        if not isinstance(block, dict):
            continue
        addon_id = block.get("addonId")
        if addon_id is None:
            continue
        deps.append(
            {
                "addon_id": str(addon_id),
                "type": str(block.get("type", "")).upper(),
            }
        )
    return deps


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
    Malformed files and missing filenames log at WARN.
    """
    data = _read_prism_toml_file(toml_path, _log)
    if data is None:
        return None

    filename = data.get("filename")
    if not filename:
        _log.warning(f"{toml_path}: no 'filename' key; skipping")
        return None

    name = data.get("name", "")
    side, side_raw = _normalize_side(data.get("side"), toml_path, _log)
    project_id, file_id, source = _parse_update_block(data)
    mod_id = str(project_id) if project_id else str(filename)
    dependencies = _parse_prism_dependencies(data)

    download = data.get("download", {})
    download_url = download.get("url")
    hash_value = download.get("hash")
    hash_format = download.get("hash-format", "sha512")

    _log.debug(f"parsed {toml_path.name}: file={filename!r} side={side!r} side_raw={side_raw!r} source={source} deps={len(dependencies)}")
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

    Returns entries in filesystem-glob order, which is stable for a
    given filesystem. A missing index directory logs at WARN and
    returns an empty list.
    """
    entries: list[dict] = []
    if not index_dir.is_dir():
        _log.warning(f"Prism index directory missing: {index_dir}")
        return entries
    paths = sorted(index_dir.glob("*.pw.toml"))
    _log.debug(f"loading Prism index from {index_dir} ({len(paths)} .pw.toml file(s))")
    for toml_path in paths:
        entry = parse_prism_toml(toml_path)
        if entry is not None:
            entries.append(entry)
    _log.info(f"loaded {len(entries)} Prism entr(ies) from {index_dir}")
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
    out = [e for e in entries if e.get("side") in ("both", target_side)]
    _log.debug(f"side filter [{target_side}]: {len(entries)} -> {len(out)}")
    return out


# ===========================================================================
# §3  Jar manifest scanning
# ===========================================================================


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
    _log.debug("manifest cache cleared")


def _try_read_manifest_bytes(zf: zipfile.ZipFile, candidate: str, jar_path: Path, logger: Any) -> bytes | None:
    """Read ``candidate`` from the zip. Returns None if the entry is absent."""
    try:
        return zf.read(candidate)
    except KeyError:
        return None


def _parse_manifest_bytes(raw: bytes, candidate: str, jar_path: Path, logger: Any) -> dict | None:
    """Decode+parse manifest bytes. Returns None on decode/parse failure."""
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"Could not parse {candidate} in {jar_path.name}: {exc}")
        return None


def _read_any_manifest(zf: zipfile.ZipFile, jar_path: Path, logger: Any) -> dict | None:
    """Try each known manifest path; return the first that parses.

    A parse failure short-circuits without the "no mods.toml found"
    log (matches the original): that message is only emitted when the
    loop completes naturally, i.e. no candidate was parsable.
    """
    for candidate in _MANIFEST_CANDIDATES:
        raw = _try_read_manifest_bytes(zf, candidate, jar_path, logger)
        if raw is None:
            continue
        data = _parse_manifest_bytes(raw, candidate, jar_path, logger)
        if data is None:
            return None
        logger.debug(f"read manifest from {jar_path.name} via {candidate}")
        return data
    logger.debug(f"no mods.toml found in {jar_path.name}")
    return None


def _collect_mod_ids(data: dict) -> set[str]:
    """Extract modIds from the ``[[mods]]`` blocks."""
    out: set[str] = set()
    for mod_block in data.get("mods") or []:
        if isinstance(mod_block, dict):
            mid = mod_block.get("modId")
            if mid:
                out.add(str(mid))
    return out


def _collect_required_by_side(data: dict) -> tuple[set[str], set[str]]:
    """Return ``(required_client, required_server)`` from mandatory deps.

    Only entries with ``mandatory = true`` are collected; ``side`` is
    upper-cased and classified as client-only, server-only, or both.
    """
    client: set[str] = set()
    server: set[str] = set()
    deps_table = data.get("dependencies") or {}
    if not isinstance(deps_table, dict):
        return (client, server)
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
                client.add(dep_mod_id)
            if side in ("BOTH", "SERVER"):
                server.add(dep_mod_id)
    return (client, server)


def _read_jar_manifest(jar_path: Path, logger: Any) -> JarManifest | None:
    """Parse ``META-INF/mods.toml`` from a jar. Never raises."""
    if not jar_path.is_file():
        logger.debug(f"jar not present: {jar_path.name}")
        return None

    try:
        with zipfile.ZipFile(jar_path) as zf:
            data = _read_any_manifest(zf, jar_path, logger)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(f"Could not read jar {jar_path.name}: {exc}")
        return None

    if data is None:
        return None

    manifest = JarManifest(
        filename=jar_path.name,
        mod_ids=_collect_mod_ids(data),
    )
    manifest.required_client, manifest.required_server = _collect_required_by_side(data)

    logger.debug(
        f"manifest {jar_path.name}: modIds={sorted(manifest.mod_ids)} "
        f"required_client={sorted(manifest.required_client)} "
        f"required_server={sorted(manifest.required_server)}"
    )
    return manifest


def scan_manifests(entries: list[dict], modpack_dir: Path, logger: Any = None) -> dict[str, JarManifest]:
    """Read every entry's jar manifest, keyed by filename.

    Cached per ``modpack_dir`` for the lifetime of the process. The
    multiple ``load_mod_list``-style calls in one deploy therefore only
    pay the scan cost once. Call ``clear_manifest_cache`` to reset.
    """
    if logger is None:
        logger = _log
    key = modpack_dir.resolve()
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        logger.debug(f"manifest cache hit for {key} ({len(cached)} jar(s))")
        return cached
    logger.debug(f"manifest cache miss for {key}; scanning {len(entries)} entr(ies)")
    result: dict[str, JarManifest] = {}
    for entry in entries:
        filename = entry.get("file")
        if not filename:
            continue
        manifest = _read_jar_manifest(modpack_dir / filename, logger)
        if manifest is not None:
            result[filename] = manifest
    _MANIFEST_CACHE[key] = result
    logger.info(f"scanned {len(result)} jar manifest(s) under {key}")
    return result


# ===========================================================================
# §4  Dependency closure
# ===========================================================================


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

    def grouped(self) -> dict[str, tuple[dict, list[tuple[dict, str]]]]:
        """Return ``{dep_id: (dep_entry, [(dependent, reason), ...])}``."""
        grouped: dict[str, tuple[dict, list[tuple[dict, str]]]] = {}
        for dependent, dependency, reason in self.forced:
            dep_id = str(dependency.get("id"))
            if dep_id not in grouped:
                grouped[dep_id] = (dependency, [])
            grouped[dep_id][1].append((dependent, reason))
        return grouped


def _index_entry_modids(
    entry: dict,
    manifests: dict[str, JarManifest],
    by_modid: dict[str, dict],
    logger: Any,
) -> None:
    """Index one entry's jar manifest modIds into ``by_modid`` (first wins).

    Called once per entry that has an ``id``; entries without an ``id``
    are skipped by :func:`_build_lookup` before this runs, matching the
    original single-pass behavior.
    """
    filename = entry.get("file")
    manifest = manifests.get(filename) if filename else None
    if manifest is None:
        return
    for mid in manifest.mod_ids:
        existing = by_modid.get(mid)
        if existing is not None and existing is not entry:
            logger.debug(f"modId {mid!r} provided by both {existing.get('file')!r} and {filename!r}; using the first")
            continue
        by_modid[mid] = entry


def _build_lookup(
    entries: list[dict],
    manifests: dict[str, JarManifest],
    logger: Any,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Return ``(by_id, by_project, by_modid)``.

    Single pass over ``entries`` so that an entry without an ``id`` is
    skipped from the modId index too (the original behavior).
    """
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
        _index_entry_modids(entry, manifests, entry_by_modid, logger)
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


def _jar_deps(entry: dict, manifests: dict[str, JarManifest], target_side: str) -> set[str]:
    """ModIds that this entry's jar marks REQUIRED for the target side."""
    filename = entry.get("file")
    manifest = manifests.get(filename) if filename else None
    if manifest is None:
        return set()
    if target_side == "client":
        return set(manifest.required_client)
    return set(manifest.required_server)


@dataclass(frozen=True)
class _DepNamespace:
    """Describes one dependency namespace for :func:`_force_include`.

    ``label`` and ``reason_prefix`` are the two hard-coded variations
    between the index-addonId and jar-modId namespaces.
    """

    lookup: dict[str, dict]
    label: str
    reason_prefix: str


def _force_include(
    ns: _DepNamespace,
    key: str,
    current_entry: dict,
    side: str,
    closure: set[str],
    worklist: list[str],
    forced: list[tuple[dict, dict, str]],
    logger: Any,
) -> None:
    """Look up ``key`` in ``ns.lookup``; force-include it if not already closed.

    The tail (dedup, append to closure + worklist, record the reason)
    is identical between the index and jar namespaces; only the missing-
    entry log and the reason text differ, and those are supplied by
    ``ns``.
    """
    dep_entry = ns.lookup.get(key)
    if dep_entry is None:
        logger.debug(f"closure [{side}]: {current_entry.get('id')} -> {ns.label} {key}: no indexed entry")
        return
    dep_id = str(dep_entry.get("id"))
    if dep_id in closure:
        return
    closure.add(dep_id)
    worklist.append(dep_id)
    reason = f"{ns.reason_prefix}={key}"
    forced.append((current_entry, dep_entry, reason))
    logger.debug(f"closure [{side}]: force-include {dep_entry.get('file')} (reason: {reason})")


def expand_with_required(
    all_entries: list[dict],
    seed_entries: list[dict],
    target_side: str,
    modpack_dir: Path,
    logger: Any = None,
) -> ClosureResult:
    """Expand a side-filtered seed with its transitive mandatory deps.

    ``target_side`` must be ``"client"`` or ``"server"``. Entries whose
    own ``side`` excludes them from the seed are still pulled in if
    something already in the set requires them.

    The returned list preserves ``all_entries`` order so output is
    deterministic across builds.
    """
    if logger is None:
        logger = _log
    side = target_side.lower()
    if side not in ("client", "server"):
        raise ValueError(f"target_side must be 'client' or 'server', got {target_side!r}")

    logger.debug(f"closure [{side}]: seed={len(seed_entries)} of {len(all_entries)} entr(ies)")
    manifests = scan_manifests(all_entries, modpack_dir, logger)
    entry_by_id, entry_by_project, entry_by_modid = _build_lookup(all_entries, manifests, logger)
    logger.debug(f"closure [{side}]: lookup tables id={len(entry_by_id)} project={len(entry_by_project)} modid={len(entry_by_modid)}")

    index_ns = _DepNamespace(lookup=entry_by_project, label="project", reason_prefix="index addonId")
    jar_ns = _DepNamespace(lookup=entry_by_modid, label="modId", reason_prefix="jar modId")

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
            _force_include(index_ns, project_id, entry, side, closure, worklist, forced, logger)
        for dep_mod_id in _jar_deps(entry, manifests, side):
            _force_include(jar_ns, dep_mod_id, entry, side, closure, worklist, forced, logger)

    ordered = [e for e in all_entries if str(e.get("id")) in closure]
    logger.info(f"closure [{side}]: expanded {len(seed_ids)} -> {len(ordered)} mod(s); {len(forced)} forced")
    return ClosureResult(entries=ordered, seed_ids=seed_ids, forced=forced)


# ===========================================================================
# §5  Diagnostic formatting (--debug-deps)
# ===========================================================================


def _format_diagnostic_header(all_entries: list[dict], modpack_dir: Path) -> list[str]:
    """Header lines shared by every ``--debug-deps`` report."""
    return [
        "=== Dependency closure diagnostic ===",
        f"Modpack dir:         {modpack_dir}",
        f"Total mods in index: {len(all_entries)}",
        "",
    ]


def _format_forced_block(result: ClosureResult) -> list[str]:
    """Render the force-included entries for one side, grouped by dep id."""
    lines: list[str] = []
    grouped = result.grouped()
    for dep_id in sorted(grouped.keys()):
        dep_entry, dependents = grouped[dep_id]
        side_str = dep_entry.get("side", "?")
        lines.append(f"  + {dep_entry.get('file')}  (declared side={side_str!r})")
        for dependent, reason in dependents:
            lines.append(f"      <- {dependent.get('file')}  [{reason}]")
        lines.append("")
    return lines


def _format_side_section(side: str, seed: list[dict], result: ClosureResult, logger: Any) -> list[str]:
    """Render one side's section: header, counts, forced entries."""
    lines: list[str] = [
        f"--- Side: {side} ---",
        f"  Seed (side filter): {len(seed)} mods",
        f"  Closure:            {len(result.entries)} mods",
        f"  Force-included:     {result.forced_count}",
    ]
    if not result.forced:
        lines.append("  (nothing needed to be force-included)")
        lines.append("")
        return lines
    lines.append("")
    lines.extend(_format_forced_block(result))
    return lines


def format_diagnostic(
    all_entries: list[dict],
    seeds: dict[str, list[dict]],
    modpack_dir: Path,
    logger: Any = None,
) -> str:
    """Multi-section human-readable diagnostic for ``--debug-deps``.

    ``seeds`` maps ``"client"`` / ``"server"`` to a side-filtered entry
    list. For each side, prints the seed count, closure count, and every
    force-included entry with its dependents.
    """
    if logger is None:
        logger = _log
    lines = _format_diagnostic_header(all_entries, modpack_dir)
    for side in ("client", "server"):
        seed = seeds.get(side, [])
        result = expand_with_required(
            all_entries=all_entries,
            seed_entries=seed,
            target_side=side,
            modpack_dir=modpack_dir,
            logger=logger,
        )
        lines.extend(_format_side_section(side, seed, result, logger))
    return "\n".join(lines).rstrip() + "\n"


# ===========================================================================
# §6  Unmarked detection + mod source resolution (§6.3, §9.2)
# ===========================================================================


@dataclass
class UnmarkedJar:
    """A jar that §6.3 classifies as unmarked."""

    filename: str
    reason: str


def _find_unindexed_jars(modpack_dir: Path, indexed_files: set[str]) -> dict[str, UnmarkedJar]:
    """§6.3 rule 1: ``*.jar`` on disk with no matching index entry."""
    out: dict[str, UnmarkedJar] = {}
    for jar in modpack_dir.glob("*.jar"):
        if jar.name not in indexed_files:
            out[jar.name] = UnmarkedJar(filename=jar.name, reason="no .pw.toml")
    return out


def _find_invalid_side_entries(entries: list[dict]) -> dict[str, UnmarkedJar]:
    """§6.3 rule 2: entries whose ``side_raw`` is present but outside the valid set."""
    out: dict[str, UnmarkedJar] = {}
    for entry in entries:
        raw = entry.get("side_raw")
        if raw is None or raw in _VALID_SIDES:
            continue
        filename = entry.get("file")
        if not filename:
            continue
        out[str(filename)] = UnmarkedJar(
            filename=str(filename),
            reason=f"declared side {raw!r} is outside {{client, server, both}}",
        )
    return out


def find_unmarked(modpack_dir: Path, entries: list[dict], logger: Any = None) -> list[UnmarkedJar]:
    """Return jars that are unmarked per §6.3.

    Two sources, both reported:

      1. ``*.jar`` files in ``modpack_dir`` whose filename does not
         appear as any entry's ``file`` field.
      2. Entries whose ``side_raw`` is present but not in
         ``{client, server, both}``.

    ``side_raw is None`` (no ``side`` key in the .pw.toml) is treated as
    marked, matching the parser's default of ``"both"``.

    Returns results sorted by filename, with no duplicates.

    Raises ConfigError if ``modpack_dir`` is not a directory.
    """
    if logger is None:
        logger = _log
    if not modpack_dir.is_dir():
        raise ConfigError(f"modpack_dir is not a directory: {modpack_dir}")

    indexed_files: set[str] = {str(e["file"]) for e in entries if e.get("file")}
    by_filename = _find_unindexed_jars(modpack_dir, indexed_files)
    by_filename.update(_find_invalid_side_entries(entries))

    result = [by_filename[name] for name in sorted(by_filename)]
    if result:
        logger.debug(f"find_unmarked: {len(result)} jar(s) unmarked")
        for u in result:
            logger.debug(f"  {u.filename}: {u.reason}")
    else:
        logger.debug("find_unmarked: no unmarked jars")
    return result


def remove_unmarked(entries: list[dict], unmarked: list[UnmarkedJar], logger: Any = None) -> list[dict]:
    """Return ``entries`` with every unmarked filename removed.

    Used by ``--non-interactive`` (§6.2): unmarked mods are not
    deployed, not written, and remain unmarked. Interactive callers
    instead prompt for each unmarked entry and splice the decisions
    back in via ``overrides.py``.
    """
    if logger is None:
        logger = _log
    drop = {u.filename for u in unmarked}
    out = [e for e in entries if e.get("file") not in drop]
    if drop:
        logger.debug(f"remove_unmarked: dropped {len(drop)} jar(s); {len(entries)} -> {len(out)}")
    return out


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


def _load_marked_entries(
    modpack_dir: Path,
    target_side: str,
    overrides: Any,
    logger: Any,
) -> list[dict]:
    """Load the Prism index and filter it: drop unmarked, apply overrides.

    Returns the marked set. Empty when the index is missing or empty;
    the caller treats both as "no sources".
    """
    index_dir = modpack_dir / ".index"
    if not index_dir.is_dir():
        logger.warning(f"resolve_mod_sources: Prism index directory missing: {index_dir}")
        return []
    entries = load_prism_index(index_dir)
    if not entries:
        logger.warning(f"resolve_mod_sources: no entries loaded from {index_dir}")
        return []

    marked = [e for e in entries if (not is_unmarked(e)) or overrides.matches(e)]
    logger.debug(f"resolve_mod_sources [{target_side}]: {len(entries)} -> {len(marked)} after unmarked filter")

    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)
        logger.debug(f"resolve_mod_sources [{target_side}]: overrides applied")
    return marked


def _collect_existing_sources(
    entries: list[dict],
    modpack_dir: Path,
    target_side: str,
    logger: Any,
) -> dict[str, Path]:
    """Return ``{filename: path}`` for every entry present on disk.

    Missing files are logged at WARN and skipped. Emits the closing
    INFO line with the source-set size.
    """
    out: dict[str, Path] = {}
    missing = 0
    for entry in entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = modpack_dir / filename
        if not path.is_file():
            logger.warning(f"mod source missing from disk, skipping: {filename}")
            missing += 1
            continue
        out[str(filename)] = path
    logger.info(f"resolve_mod_sources [{target_side}]: {len(out)} source(s) ({missing} declared but missing on disk)")
    return out


def resolve_mod_sources(
    modpack_dir: Path,
    target_side: str,
    overrides: Any,
    logger: Any = None,
) -> dict[str, Path]:
    """Prism index -> marked -> overridden -> side-filtered -> closed -> {filename: path}.

    The shared "intent set" used by preflight's change computation and by
    both write scopes. ``overrides`` must be a
    :class:`minecraft.deploy_pack.overrides.SideOverrides` instance; it is
    duck-typed here to avoid a circular import.
    """
    if logger is None:
        logger = _log
    marked = _load_marked_entries(modpack_dir, target_side, overrides, logger)
    if not marked:
        return {}

    side_entries = filter_prism_entries_by_side(marked, target_side)
    closure = expand_with_required(
        all_entries=marked,
        seed_entries=side_entries,
        target_side=target_side,
        modpack_dir=modpack_dir,
        logger=logger,
    )
    return _collect_existing_sources(closure.entries, modpack_dir, target_side, logger)
