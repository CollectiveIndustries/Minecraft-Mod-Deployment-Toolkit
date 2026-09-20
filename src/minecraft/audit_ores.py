# src/minecraft/audit_ores.py

r"""Audit ore blocks and their loot drops across mod jars.

Read-only. Scans all jars in a directory, discovers block ids, ore
tags, worldgen configured features, and loot tables, then groups
candidate ore blocks by material. Writes a machine-readable JSON
report and a human-readable Markdown report.

This tool does not modify anything. It exists to inform a later
normalization pass that will generate KubeJS loot tables.

An ore candidate is a block that either:

  - has a block id matching the ``_ore`` / ``ore_`` naming convention,
    or
  - is a member of an ore tag (``c:ores``, ``forge:ores``,
    ``common:ores`` and their children).

Worldgen and loot-table signals enrich existing candidates but do not
create new ones, because non-ore blocks can be placed by ore features
and ordinary blocks can have loot tables.

Tag resolution is global across all jars, matching Forge's actual tag
semantics. A tag declared in one jar can reference a tag declared in
another jar, and the transitive closure is computed over the union.

Loot drops are split by silk-touch requirement so downstream consumers
can rewrite the normal branch without disturbing the silk-touch branch.

Usage:

    pdm run python src/minecraft/audit_ores.py \\
        --jars sync/downloads \\
        --out-dir sync/audit

Flags:

    --jars        Directory containing *.jar, or a single jar file.
                  Default: sync/downloads
    --out-dir     Directory for audit outputs. Default: sync/audit
    --only        Only report these materials (comma-separated).
                  Default: report all materials.
    --quiet       Suppress per-block progress output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_JARS_DIR = "sync/downloads"
DEFAULT_OUT_DIR = "sync/audit"
ORE_TAG_PATH = "ores"
ORE_NAME_RE = re.compile("(^|_)ore(_|$)")
ORE_FEATURE_TYPES = {"minecraft:ore", "forge:ore", "c:ore", "minecraft:scattered_ore"}
DIMENSION_PREFIXES = ("deepslate_", "nether_", "end_", "raw_")
DIMENSION_SUFFIXES = ("_deepslate", "_nether", "_end")
NON_MATERIAL_WORDS = {"deepslate", "nether", "end", "stone", "blackstone", "basalt", "netherrack"}
SILK_TOUCH_ENCHANTMENT = "minecraft:silk_touch"


@dataclass
class OreRecord:
    """Everything we know about one candidate ore block."""

    block_id: str
    source_jar: str
    tags: list[str] = field(default_factory=list)
    worldgen_features: list[str] = field(default_factory=list)
    loot_table: str | None = None
    drops_normal: list[str] = field(default_factory=list)
    drops_silk_touch: list[str] = field(default_factory=list)
    classification: list[str] = field(default_factory=list)
    material: str = "unknown"

    def to_json(self) -> dict:
        """Serializes the object to a JSON-compatible dictionary."""
        return {
            "block_id": self.block_id,
            "source_jar": self.source_jar,
            "tags": sorted(self.tags),
            "worldgen_features": sorted(self.worldgen_features),
            "loot_table": self.loot_table,
            "drops": {"normal": sorted(self.drops_normal), "silk_touch": sorted(self.drops_silk_touch)},
            "classification": sorted(self.classification),
            "material": self.material,
        }


class JarArchive:
    """Read JSON entries from a mod jar without extracting."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        self.names = tuple(self.zip.namelist())

    def close(self) -> None:
        """Closes the underlying resource, releasing any held handles."""
        self.zip.close()

    def __enter__(self) -> JarArchive:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def read_json(self, name: str):
        """Reads and parses a JSON file, returning None if missing or invalid."""
        try:
            data = self.zip.read(name)
        except KeyError:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None


def iter_jars(path: Path):
    """Yield jar paths from a file or directory."""
    if path.is_file():
        yield path
        return
    if path.is_dir():
        yield from sorted(path.glob("*.jar"))
        return
    raise FileNotFoundError(f"Not a file or directory: {path}")


def parse_blockstate_path(path: str) -> tuple[str, str] | None:
    """Parse ``assets/<ns>/blockstates/<name>.json`` -> (ns, name)."""
    return parse_resource_id(path, "blockstates")


def parse_resource_id(path: str, expected_folder: str) -> tuple[str, str] | None:
    """Parse ``assets/<ns>/<folder>/<name>.json`` or ``data/<ns>/<folder>/<name>.json``.

    Returns (namespace, name) or None if the path does not match.
    Name may contain slashes for nested folders.
    """
    parts = path.split("/")
    if len(parts) < 4:
        return None
    if parts[0] not in ("assets", "data"):
        return None
    if parts[2] != expected_folder:
        return None
    if not path.endswith(".json"):
        return None
    namespace = parts[1]
    name = "/".join(parts[3:])[: -len(".json")]
    return (namespace, name)


def parse_loot_table_path(path: str) -> tuple[str, str] | None:
    """Parse ``data/<ns>/loot_tables/blocks/<name>.json``.

    Returns (namespace, block_name) or None.
    """
    parts = path.split("/")
    if len(parts) < 5:
        return None
    if parts[0] != "data":
        return None
    if parts[2] != "loot_tables":
        return None
    if parts[3] != "blocks":
        return None
    if not path.endswith(".json"):
        return None
    namespace = parts[1]
    name = "/".join(parts[4:])[: -len(".json")]
    return (namespace, name)


def parse_worldgen_path(path: str) -> tuple[str, str] | None:
    """Parse ``data/<ns>/worldgen/configured_feature/<name>.json``.

    Returns (namespace, name) or None.
    """
    parts = path.split("/")
    if len(parts) < 5:
        return None
    if parts[0] != "data":
        return None
    if parts[2] != "worldgen":
        return None
    if parts[3] != "configured_feature":
        return None
    if not path.endswith(".json"):
        return None
    namespace = parts[1]
    name = "/".join(parts[4:])[: -len(".json")]
    return (namespace, name)


def parse_tag_path(path: str, tag_kind: str) -> tuple[str, str] | None:
    """Parse ``data/<ns>/tags/<tag_kind>/<name>.json``.

    Returns (namespace, name) or None.
    """
    parts = path.split("/")
    if len(parts) < 5:
        return None
    if parts[0] != "data":
        return None
    if parts[2] != "tags":
        return None
    if parts[3] != tag_kind:
        return None
    if not path.endswith(".json"):
        return None
    namespace = parts[1]
    name = "/".join(parts[4:])[: -len(".json")]
    return (namespace, name)


def parse_tag_entries(values) -> tuple[set[str], set[str]]:
    """Return (direct_block_ids, tag_references).

    Tag references come back without the leading ``#``. Handles both
    the 1.20.x plain-string format and the newer object-with-id format.
    """
    blocks: set[str] = set()
    tags: set[str] = set()
    for v in values or []:
        if isinstance(v, str):
            if v.startswith("#"):
                tags.add(v[1:])
            else:
                blocks.add(v)
        elif isinstance(v, dict):
            vid = v.get("id")
            if not isinstance(vid, str):
                continue
            if vid.startswith("#"):
                tags.add(vid[1:])
            else:
                blocks.add(vid)
    return (blocks, tags)


def _condition_requires_silk_touch(condition) -> bool:
    """Return True if this single condition object requires silk touch.

    Handles ``minecraft:match_tool`` with an enchantments predicate,
    plus ``minecraft:all_of`` and ``minecraft:any_of`` wrappers.
    Returns False for ``minecraft:inverted`` - an inverted silk-touch
    check is the normal-break path, not the silk-touch path.
    """
    if not isinstance(condition, dict):
        return False
    ctype = condition.get("condition", "")
    if ctype == "minecraft:match_tool":
        predicate = condition.get("predicate")
        if not isinstance(predicate, dict):
            return False
        enchants = predicate.get("enchantments")
        if not isinstance(enchants, list):
            return False
        for e in enchants:
            if not isinstance(e, dict):
                continue
            if e.get("enchantment") == SILK_TOUCH_ENCHANTMENT:
                return True
        return False
    if ctype in ("minecraft:all_of", "minecraft:any_of"):
        return any(_condition_requires_silk_touch(sub) for sub in condition.get("terms", []) or [])
    if ctype == "minecraft:inverted":
        return False
    return False


def _walk_loot_entries(entry, inherited_silk: bool, out_normal: set[str], out_silk: set[str]) -> None:
    """Recurse a loot-table entry tree, bucketing items by silk-touch.

    ``inherited_silk`` is the silk-touch state carried down from
    ancestors. Each entry may add its own silk-touch requirement via
    its ``conditions`` list; that requirement then propagates to
    children. ``minecraft:item`` and ``minecraft:tag`` entries emit
    their identifier into the appropriate bucket.
    """
    if not isinstance(entry, dict):
        return
    own_silk = False
    for condition in entry.get("conditions", []) or []:
        if _condition_requires_silk_touch(condition):
            own_silk = True
            break
    silk = inherited_silk or own_silk
    etype = entry.get("type", "")
    if etype == "minecraft:item":
        name = entry.get("name")
        if isinstance(name, str):
            if silk:
                out_silk.add(name)
            else:
                out_normal.add(name)
    elif etype == "minecraft:tag":
        name = entry.get("name")
        if isinstance(name, str):
            ref = f"#{name}"
            if silk:
                out_silk.add(ref)
            else:
                out_normal.add(ref)
    for key in ("children", "entries"):
        for child in entry.get(key, []) or []:
            _walk_loot_entries(child, silk, out_normal, out_silk)


def extract_drops_from_loot_table(table) -> tuple[set[str], set[str]]:
    """Return (normal_drops, silk_touch_drops) from a loot table.

    Every item and tag reference in every pool is walked. Entries nested
    under a silk-touch condition go to the silk-touch set. Everything
    else goes to the normal set. Tag references are prefixed with ``#``.
    """
    normal: set[str] = set()
    silk: set[str] = set()
    if not isinstance(table, dict):
        return (normal, silk)
    for pool in table.get("pools", []) or []:
        for entry in pool.get("entries", []) or []:
            _walk_loot_entries(entry, False, normal, silk)
    return (normal, silk)


def is_ore_feature(feature) -> bool:
    """Return True if the configured feature places ore blocks."""
    if not isinstance(feature, dict):
        return False
    ftype = feature.get("type", "")
    if not isinstance(ftype, str):
        return False
    if ftype in ORE_FEATURE_TYPES:
        return True
    return ftype.endswith(":ore")


def extract_ore_targets(feature) -> list[str]:
    """Return block ids targeted by an ore configured feature."""
    if not isinstance(feature, dict):
        return []
    config = feature.get("config")
    if not isinstance(config, dict):
        return []
    targets = config.get("targets")
    if not isinstance(targets, list):
        return []
    blocks: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            continue
        state = target.get("state")
        if not isinstance(state, dict):
            continue
        name = state.get("Name")
        if isinstance(name, str):
            blocks.add(name)
    return sorted(blocks)


def _tag_path(tag_name: str) -> str:
    """Return the path portion of a tag name, stripping the namespace."""
    if ":" in tag_name:
        return tag_name.split(":", 1)[1]
    return tag_name


def name_looks_like_ore(name: str) -> bool:
    """Return True if the block name matches common ore naming."""
    return bool(ORE_NAME_RE.search(name))


def tag_is_ore_tag(tag_name: str) -> bool:
    """Return True if the tag is a material ore tag.

    Accepts either fully qualified (``forge:ores/aluminum``) or
    path-only (``ores/aluminum``) forms. The path must be exactly
    ``ores`` or begin with ``ores/``.
    """
    path = _tag_path(tag_name)
    if path == ORE_TAG_PATH:
        return True
    return path.startswith(ORE_TAG_PATH + "/")


def extract_material_from_tag(tag_name: str) -> str | None:
    """Return the material segment of an ore tag, if any.

    Splits on the first ``/`` after ``ores`` so hierarchical tags like
    ``ores/lead/deepslate`` yield ``lead``. Rejects segments that are
    stone-type or dimension qualifiers.
    """
    path = _tag_path(tag_name)
    prefix = ORE_TAG_PATH + "/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix) :]
    material = remainder.split("/", 1)[0]
    if material in NON_MATERIAL_WORDS:
        return None
    return material


def extract_material_from_name(name: str) -> str | None:
    """Best-effort material extraction from a block name.

    Handles ``<material>_ore``, ``ore_<material>``, and
    ``<stone>_ore_<material>`` (via leading prefix strip). Dimension
    and stone prefixes are stripped before the ore pattern is matched,
    so ``deepslate_ore_aluminum`` reduces to ``ore_aluminum`` and then
    to ``aluminum``.
    """
    base = name
    for prefix in DIMENSION_PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    for suffix in DIMENSION_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.endswith("_ore"):
        base = base[: -len("_ore")]
    elif base.startswith("ore_"):
        base = base[len("ore_") :]
    elif "_ore_" in base:
        idx = base.index("_ore_")
        base = base[:idx]
    else:
        return None
    return base or None


@dataclass
class AuditResults:
    """Accumulated audit state.

    Intermediate maps (``block_to_tags``, ``tag_to_blocks``,
    ``tag_to_tag_refs``, ``worldgen_by_block``) are global across all
    jars, because Forge tags compose across the union of all loaded
    mods. ``loot_table_index`` maps a block id to the (jar, member)
    pair that owns its loot table so we can read it lazily in the
    finalize pass without re-scanning every jar.
    """

    ores: dict[str, OreRecord] = field(default_factory=dict)
    jars_scanned: int = 0
    block_ids: set[str] = field(default_factory=set)
    block_to_jar: dict[str, str] = field(default_factory=dict)
    block_to_tags: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tag_to_blocks: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tag_to_tag_refs: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    worldgen_by_block: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    loot_table_index: dict[str, tuple[Path, str]] = field(default_factory=dict)

    def get_or_create(self, block_id: str, source_jar: str) -> OreRecord:
        """Retrieves an existing record or creates and stores a new one."""
        rec = self.ores.get(block_id)
        if rec is None:
            rec = OreRecord(block_id=block_id, source_jar=source_jar)
            self.ores[block_id] = rec
        return rec


def collect_jar(jar_path: Path, results: AuditResults, quiet: bool) -> None:
    """Scan one jar for block ids, tags, worldgen, and loot tables.

    Everything is accumulated into ``results``. No per-jar resolution
    happens here - that is deferred to ``finalize_audit`` so tags can
    compose across the full set of scanned jars.
    """
    if not quiet:
        print(f"  scanning {jar_path.name}")
    with JarArchive(jar_path) as jar:
        for name in jar.names:
            parsed = parse_blockstate_path(name)
            if parsed is None:
                continue
            ns, block_name = parsed
            block_id = f"{ns}:{block_name}"
            results.block_ids.add(block_id)
            results.block_to_jar.setdefault(block_id, jar_path.name)
        for name in jar.names:
            parsed = parse_loot_table_path(name)
            if parsed is None:
                continue
            ns, block_name = parsed
            block_id = f"{ns}:{block_name}"
            results.loot_table_index.setdefault(block_id, (jar_path, name))
        for name in jar.names:
            parsed = parse_tag_path(name, "blocks")
            if parsed is None:
                continue
            ns, tag_name = parsed
            full_tag = f"{ns}:{tag_name}"
            data = jar.read_json(name)
            if not isinstance(data, dict):
                continue
            direct_blocks, tag_refs = parse_tag_entries(data.get("values"))
            for b in direct_blocks:
                results.block_to_tags[b].add(full_tag)
                results.tag_to_blocks[full_tag].add(b)
            for ref in tag_refs:
                results.tag_to_tag_refs[full_tag].add(ref)
        for name in jar.names:
            parsed = parse_worldgen_path(name)
            if parsed is None:
                continue
            ns, feature_name = parsed
            data = jar.read_json(name)
            if not is_ore_feature(data):
                continue
            full_feature = f"{ns}:{feature_name}"
            for block_id in extract_ore_targets(data):
                results.worldgen_by_block[block_id].add(full_feature)


def _resolve_ore_tags(results: AuditResults) -> dict[str, set[str]]:
    """Return {ore_tag: resolved_block_ids} over the global tag union.

    Walks every tag whose path is an ore tag, transitively following
    tag references. This is where cross-jar composition is applied.
    """
    resolved: dict[str, set[str]] = {}
    all_tags = set(results.tag_to_blocks.keys()) | set(results.tag_to_tag_refs.keys())
    for tag in all_tags:
        if not tag_is_ore_tag(tag):
            continue
        closure: set[str] = set()
        stack = [tag]
        seen: set[str] = set()
        while stack:
            t = stack.pop()
            if t in seen:
                continue
            seen.add(t)
            closure.update(results.tag_to_blocks.get(t, set()))
            for ref in results.tag_to_tag_refs.get(t, set()):
                stack.append(ref)
        resolved[tag] = closure
    return resolved


def _select_candidates(results: AuditResults) -> set[str]:
    """Return block ids that qualify as ore candidates.

    A candidate is a block whose short name matches the ore naming
    pattern, or a block that is a member of an ore tag (post
    resolution). Worldgen and loot-table signals enrich existing
    candidates but do not create new ones.
    """
    candidates: set[str] = set()
    for block_id in results.block_ids:
        short = block_id.split(":", 1)[1] if ":" in block_id else block_id
        if name_looks_like_ore(short):
            candidates.add(block_id)
            continue
        for tag in results.block_to_tags.get(block_id, set()):
            if tag_is_ore_tag(tag):
                candidates.add(block_id)
                break
    return candidates


def finalize_audit(results: AuditResults) -> None:
    """Resolve tags, select candidates, and read loot tables.

    Runs after all jars have been collected. Resolves ore tags over
    the global union, picks the candidate set, and reads loot tables
    for candidates in one batch per source jar.
    """
    ore_tags = _resolve_ore_tags(results)
    for tag, blocks in ore_tags.items():
        for b in blocks:
            results.block_to_tags[b].add(tag)
    candidates = _select_candidates(results)
    for block_id in sorted(candidates):
        source_jar = results.block_to_jar.get(block_id, "unknown")
        rec = results.get_or_create(block_id, source_jar)
        short = block_id.split(":", 1)[1] if ":" in block_id else block_id
        classification: list[str] = []
        if name_looks_like_ore(short):
            classification.append("NAME")
        block_tags = sorted(results.block_to_tags.get(block_id, set()))
        ore_tag_hit = False
        material_from_tag: str | None = None
        for tag in block_tags:
            if tag_is_ore_tag(tag):
                ore_tag_hit = True
                m = extract_material_from_tag(tag)
                if m and material_from_tag is None:
                    material_from_tag = m
        if ore_tag_hit:
            classification.append("TAG")
        rec.tags = block_tags
        if block_id in results.worldgen_by_block:
            classification.append("WORLDGEN")
            rec.worldgen_features = sorted(results.worldgen_by_block[block_id])
        if block_id in results.loot_table_index:
            classification.append("LOOT")
        rec.classification = sorted(classification)
        if material_from_tag:
            rec.material = material_from_tag
        else:
            m = extract_name_safe(short)
            if m:
                rec.material = m
    by_jar: dict[Path, list[tuple[str, str]]] = defaultdict(list)
    for block_id in candidates:
        entry = results.loot_table_index.get(block_id)
        if entry is None:
            continue
        jar_path, member = entry
        by_jar[jar_path].append((block_id, member))
    for jar_path, items in by_jar.items():
        with JarArchive(jar_path) as jar:
            for block_id, member in items:
                data = jar.read_json(member)
                if data is None:
                    continue
                normal, silk = extract_drops_from_loot_table(data)
                rec = results.ores[block_id]
                rec.drops_normal = sorted(normal)
                rec.drops_silk_touch = sorted(silk)
                parsed = parse_loot_table_path(member)
                if parsed:
                    ns, block_name = parsed
                    rec.loot_table = f"{ns}:blocks/{block_name}"


def extract_name_safe(short: str) -> str | None:
    """Return material from a short name, but only if the name is ore-shaped."""
    return extract_material_from_name(short)


def run_audit(jars_path: Path, quiet: bool) -> AuditResults:
    """Run the full audit over every jar in ``jars_path``."""
    results = AuditResults()
    jars = list(iter_jars(jars_path))
    if not quiet:
        print("=== Ore audit ===")
        print(f"Jars: {len(jars)}")
    for jar in jars:
        collect_jar(jar, results, quiet)
        results.jars_scanned += 1
    finalize_audit(results)
    if not quiet:
        print(f"Scanned: {results.jars_scanned}")
        print(f"Ore candidates: {len(results.ores)}")
    return results


def group_by_material(results: AuditResults) -> dict[str, list[OreRecord]]:
    """Return {material: [records]}."""
    grouped: dict[str, list[OreRecord]] = defaultdict(list)

    for rec in results.ores.values():
        grouped[rec.material].append(rec)

    for _material, records in grouped.items():
        records.sort(key=lambda r: r.block_id)

    return dict(sorted(grouped.items()))


def build_json_report(results: AuditResults) -> dict:
    """Return the machine-readable audit report."""
    grouped = group_by_material(results)
    materials_report: dict[str, dict] = {}
    for material, records in grouped.items():
        normal: set[str] = set()
        silk: set[str] = set()
        drops_by_ore: dict[str, dict] = {}
        for rec in records:
            drops_by_ore[rec.block_id] = {"normal": sorted(rec.drops_normal), "silk_touch": sorted(rec.drops_silk_touch)}
            normal.update(rec.drops_normal)
            silk.update(rec.drops_silk_touch)
        materials_report[material] = {
            "ore_blocks": [r.block_id for r in records],
            "distinct_normal_drops": sorted(normal),
            "distinct_silk_touch_drops": sorted(silk),
            "drops_by_ore": drops_by_ore,
        }
    return {
        "generated": datetime.now(UTC).isoformat(),
        "jars_scanned": results.jars_scanned,
        "ore_count": len(results.ores),
        "materials": materials_report,
        "ores": {bid: rec.to_json() for bid, rec in sorted(results.ores.items())},
    }


def build_markdown_report(results: AuditResults) -> str:
    """Return the human-readable audit report as Markdown."""
    lines: list[str] = []
    grouped = group_by_material(results)
    lines.append("# Ore Audit")
    lines.append("")
    lines.append(f"Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"Jars scanned: {results.jars_scanned}")
    lines.append(f"Ore candidates: {len(results.ores)}")
    lines.append(f"Materials: {len(grouped)}")
    lines.append("")
    for material, records in grouped.items():
        normal: set[str] = set()
        silk: set[str] = set()
        for rec in records:
            normal.update(rec.drops_normal)
            silk.update(rec.drops_silk_touch)
        lines.append(f"## {material.upper()}")
        lines.append("")
        lines.append(f"- ore blocks: {len(records)}")
        lines.append(f"- distinct normal drops: {len(normal)}")
        lines.append(f"- distinct silk-touch drops: {len(silk)}")
        lines.append("")
        lines.append("### Ore blocks")
        lines.append("")
        for rec in records:
            lines.append(f"#### `{rec.block_id}`")
            lines.append("")
            lines.append(f"- source: `{rec.source_jar}`")
            lines.append(f"- classification: {', '.join(rec.classification) or 'none'}")
            if rec.tags:
                lines.append("- tags:")
                for tag in rec.tags:
                    lines.append(f"  - `{tag}`")
            if rec.worldgen_features:
                lines.append("- worldgen:")
                for feat in rec.worldgen_features:
                    lines.append(f"  - `{feat}`")
            if rec.loot_table:
                lines.append(f"- loot table: `{rec.loot_table}`")
            if rec.drops_normal:
                lines.append("- drops (normal):")
                for drop in rec.drops_normal:
                    lines.append(f"  - `{drop}`")
            if rec.drops_silk_touch:
                lines.append("- drops (silk touch):")
                for drop in rec.drops_silk_touch:
                    lines.append(f"  - `{drop}`")
            if not rec.drops_normal and (not rec.drops_silk_touch):
                lines.append("- drops: `UNKNOWN / CODE-DERIVED`")
            lines.append("")
        if len(normal) > 1:
            lines.append("### Normalization candidates")
            lines.append("")
            lines.append("This material has multiple distinct normal drops:")
            lines.append("")
            for drop in sorted(normal):
                sources = [rec.block_id for rec in records if drop in rec.drops_normal]
                lines.append(f"- `{drop}` from {', '.join(f'`{s}`' for s in sources)}")
            lines.append("")
    return "\n".join(lines)


def write_report(report: dict, markdown: str, out_dir: Path) -> None:
    """Write both report files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "ore-audit.json"
    md_path = out_dir / "ore-audit.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


def resolve_jars_path(arg: Path | None, repo_root: Path) -> Path:
    """Return the jars directory or file path."""
    if arg is None:
        return repo_root / DEFAULT_JARS_DIR
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    return p


def resolve_out_dir(arg: Path | None, repo_root: Path) -> Path:
    """Return the output directory path."""
    if arg is None:
        return repo_root / DEFAULT_OUT_DIR
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    return p


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Audit ore blocks and loot drops across mod jars.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2], help="Repository root. Default: grandparent of this file.")
    parser.add_argument("--jars", type=Path, default=None, help=f"Jar file or directory of jars. Default: {DEFAULT_JARS_DIR}")
    parser.add_argument("--out-dir", type=Path, default=None, help=f"Output directory for audit reports. Default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--only", type=str, default=None, help="Comma-separated material whitelist. Default: report all.")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-jar progress output.")
    return parser


def filter_materials(report: dict, only: str | None) -> dict:
    """Filter the JSON report to a whitelist of materials."""
    if not only:
        return report
    wanted = {m.strip().lower() for m in only.split(",") if m.strip()}
    filtered_materials = {k: v for k, v in report["materials"].items() if k.lower() in wanted}
    wanted_ores = set()
    for m in filtered_materials.values():
        wanted_ores.update(m["ore_blocks"])
    filtered_ores = {k: v for k, v in report["ores"].items() if k in wanted_ores}
    return {**report, "materials": filtered_materials, "ores": filtered_ores}


def filter_markdown(markdown: str, only: str | None) -> str:
    """Filter the Markdown report to a whitelist of materials.

    Keeps the header and drops any ``## MATERIAL`` section not wanted.
    """
    if not only:
        return markdown
    wanted = {m.strip().lower() for m in only.split(",") if m.strip()}
    out: list[str] = []
    keep = True
    for line in markdown.split("\n"):
        if line.startswith("## ") and (not line.startswith("### ")):
            name = line[3:].strip().lower()
            keep = name in wanted
        if keep:
            out.append(line)
    return "\n".join(out)


def main() -> int:
    """Run the command-line tool."""
    parser = build_parser()
    args = parser.parse_args()
    repo_root = Path(args.repo_root).expanduser().resolve()
    jars_path = resolve_jars_path(args.jars, repo_root)
    out_dir = resolve_out_dir(args.out_dir, repo_root)
    if not jars_path.exists():
        print(f"ERROR: jars path not found: {jars_path}", file=sys.stderr)
        return 2
    try:
        results = run_audit(jars_path, quiet=args.quiet)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    report = build_json_report(results)
    report = filter_materials(report, args.only)
    markdown = build_markdown_report(results)
    markdown = filter_markdown(markdown, args.only)
    write_report(report, markdown, out_dir)
    if not args.quiet:
        print()
        print("=== Material summary ===")
        for material, entry in sorted(report["materials"].items()):
            normal_count = len(entry["distinct_normal_drops"])
            silk_count = len(entry["distinct_silk_touch_drops"])
            ore_count = len(entry["ore_blocks"])
            marker = " *" if normal_count > 1 else ""
            print(f"  {material:<20s} ores={ore_count:<3d} normal={normal_count:<3d} silk={silk_count:<3d}{marker}")
        print()
        print("  * = normalization candidate (multiple distinct normal drops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
