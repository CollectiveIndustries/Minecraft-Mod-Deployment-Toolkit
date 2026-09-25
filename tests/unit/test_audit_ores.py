# tests/unit/test_audit_ores.py

"""Tests for src/minecraft/audit_ores.py.

Coverage areas:
  * path parsers: ``parse_resource_id`` and its blockstate / loot-table /
    worldgen / tag wrappers, including rejection cases
  * tag grammar: ``parse_tag_entries``, ``tag_is_ore_tag``, ``_tag_path``
  * material extraction: ``extract_material_from_tag``,
    ``extract_material_from_name`` (all prefix/suffix permutations),
    ``name_looks_like_ore``, ``extract_name_safe``
  * silk-touch detection: ``_condition_requires_silk_touch``
  * loot walking: ``_walk_loot_entries``, ``extract_drops_from_loot_table``
  * ore features: ``is_ore_feature``, ``extract_ore_targets``
  * data classes: ``OreRecord.to_json``, ``AuditResults.get_or_create``
  * jar I/O: ``JarArchive.read_json``, ``iter_jars``
  * collection and resolution: ``collect_jar``, ``_resolve_ore_tags``
    (transitive closure), ``_select_candidates``
  * end-to-end: ``run_audit`` against a synthetic jar, ``group_by_material``,
    ``build_json_report``, ``build_markdown_report``, ``write_report``
  * filtering: ``filter_materials``, ``filter_markdown``
  * path resolution and CLI: ``resolve_jars_path``, ``resolve_out_dir``,
    ``build_parser``, ``main`` exit codes

The synthetic-jar helper :func:`_make_jar` serialises dict/list values
as JSON, str values as UTF-8, and bytes verbatim. Tests build one
realistic jar via :func:`_build_demo_jar` and run the full pipeline
against it.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from minecraft import audit_ores
from minecraft.audit_ores import (
    AuditResults,
    JarArchive,
    OreRecord,
    _condition_requires_silk_touch,
    _resolve_ore_tags,
    _select_candidates,
    _tag_path,
    _walk_loot_entries,
    build_json_report,
    build_markdown_report,
    collect_jar,
    extract_drops_from_loot_table,
    extract_material_from_name,
    extract_material_from_tag,
    extract_name_safe,
    extract_ore_targets,
    filter_markdown,
    filter_materials,
    group_by_material,
    is_ore_feature,
    iter_jars,
    name_looks_like_ore,
    parse_blockstate_path,
    parse_loot_table_path,
    parse_resource_id,
    parse_tag_entries,
    parse_tag_path,
    parse_worldgen_path,
    resolve_jars_path,
    resolve_out_dir,
    run_audit,
    tag_is_ore_tag,
    write_report,
)


def _make_jar(path: Path, files: dict) -> Path:
    """Write a zip at ``path`` with ``files`` as members.

    Dict and list values are JSON-serialised; str values are UTF-8
    encoded; bytes are written verbatim.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content).encode("utf-8")
            elif isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return path


class TestParseResourceId:
    """The generic ``assets|data/<ns>/<folder>/<name>.json`` parser."""

    def test_assets_root(self) -> None:
        """Tests parsing an asset resource ID from a nested assets path."""
        assert parse_resource_id("assets/minecraft/blockstates/stone.json", "blockstates") == ("minecraft", "stone")

    def test_data_root(self) -> None:
        """Tests parsing a data resource ID from a nested data path."""
        assert parse_resource_id("data/forge/recipes/foo.json", "recipes") == ("forge", "foo")

    def test_nested_name(self) -> None:
        """Tests that a resource ID with a nested name is parsed correctly."""
        assert parse_resource_id("assets/create/blockstates/copper/block.json", "blockstates") == ("create", "copper/block")

    def test_unknown_root_rejected(self) -> None:
        """Tests that a path with an unknown root directory is rejected and returns None."""
        assert parse_resource_id("sounds/minecraft/blockstates/x.json", "blockstates") is None

    def test_wrong_folder_rejected(self) -> None:
        """Tests that a path with the wrong intermediate folder is rejected and returns None."""
        assert parse_resource_id("assets/minecraft/models/x.json", "blockstates") is None

    def test_non_json_rejected(self) -> None:
        """Tests that a non-JSON file path is rejected and returns None."""
        assert parse_resource_id("assets/minecraft/blockstates/x.txt", "blockstates") is None

    def test_too_short_rejected(self) -> None:
        """Tests that a path with too few segments is rejected and returns None."""
        assert parse_resource_id("assets/mc.json", "blockstates") is None


class TestParseBlockstatePath:
    """Tests for parsing blockstate file paths."""

    def test_valid(self) -> None:
        """Tests that a valid blockstate path is parsed correctly."""
        assert parse_blockstate_path("assets/minecraft/blockstates/diamond_ore.json") == ("minecraft", "diamond_ore")

    def test_invalid_folder(self) -> None:
        """Tests that a blockstate path in an invalid folder returns None."""
        assert parse_blockstate_path("assets/minecraft/models/x.json") is None


class TestParseLootTablePath:
    """Tests for parsing loot table file paths."""

    def test_valid(self) -> None:
        """Tests that a valid loot table path is parsed into its namespace and name."""
        assert parse_loot_table_path("data/minecraft/loot_tables/blocks/diamond_ore.json") == ("minecraft", "diamond_ore")

    def test_nested_name_ok(self) -> None:
        """Tests that a loot table path with a nested name is parsed correctly."""
        assert parse_loot_table_path("data/mod/loot_tables/blocks/deep/x.json") == ("mod", "deep/x")

    def test_non_blocks_folder_rejected(self) -> None:
        """Tests that a loot table path not under the blocks folder is rejected."""
        assert parse_loot_table_path("data/minecraft/loot_tables/entities/zombie.json") is None

    def test_assets_root_rejected(self) -> None:
        """Tests that a loot table path under the assets root is rejected."""
        assert parse_loot_table_path("assets/minecraft/loot_tables/blocks/x.json") is None

    def test_too_short_rejected(self) -> None:
        """Tests that a too-short loot table path is rejected as invalid."""
        assert parse_loot_table_path("data/minecraft/loot_tables.json") is None


class TestParseWorldgenPath:
    """Tests for parsing worldgen file paths."""

    def test_valid(self) -> None:
        """Tests that a valid worldgen path is parsed into its namespace and name."""
        assert parse_worldgen_path("data/minecraft/worldgen/configured_feature/ore_copper.json") == ("minecraft", "ore_copper")

    def test_wrong_subfolder_rejected(self) -> None:
        """Tests that a worldgen path under the wrong subfolder is rejected."""
        assert parse_worldgen_path("data/minecraft/worldgen/placed_feature/x.json") is None


class TestParseTagPath:
    """Tests for parsing tag file paths."""

    def test_valid(self) -> None:
        """Tests that a valid tag path is parsed correctly for the given kind."""
        assert parse_tag_path("data/forge/tags/blocks/ores/copper.json", "blocks") == ("forge", "ores/copper")

    def test_wrong_kind_rejected(self) -> None:
        """Tests that a tag path with a mismatched kind is rejected as None."""
        assert parse_tag_path("data/forge/tags/items/ores.json", "blocks") is None


class TestParseTagEntries:
    """Tests for parsing tag entries."""

    def test_none_and_empty(self) -> None:
        """Tests that None and empty input yield empty blocks and tags sets."""
        assert parse_tag_entries(None) == (set(), set())
        assert parse_tag_entries([]) == (set(), set())

    def test_plain_strings_are_blocks(self) -> None:
        """Tests that plain entry strings are parsed as block identifiers."""
        blocks, tags = parse_tag_entries(["minecraft:stone", "mod:copper_ore"])
        assert blocks == {"minecraft:stone", "mod:copper_ore"}
        assert tags == set()

    def test_hash_prefix_is_tag_reference(self) -> None:
        """Tests that entries prefixed with a hash are parsed as tag references."""
        blocks, tags = parse_tag_entries(["#forge:ores", "#c:ores/lead"])
        assert blocks == set()
        assert tags == {"forge:ores", "c:ores/lead"}

    def test_dict_form(self) -> None:
        """Tests parsing of tag entries containing a block and a tag."""
        blocks, tags = parse_tag_entries([{"id": "minecraft:stone"}, {"id": "#forge:ores"}])
        assert blocks == {"minecraft:stone"}
        assert tags == {"forge:ores"}

    def test_dict_missing_id_skipped(self) -> None:
        """Tests that dict entries missing an id are skipped."""
        assert parse_tag_entries([{"required": False}]) == (set(), set())

    def test_unknown_scalar_skipped(self) -> None:
        """Tests that unknown scalar entries are skipped."""
        assert parse_tag_entries([42, None, True]) == (set(), set())

    def test_mixed_form(self) -> None:
        """Tests parsing of mixed tag entry forms."""
        blocks, tags = parse_tag_entries(["minecraft:stone", "#forge:ores", {"id": "mod:copper_ore"}, {"id": "#c:ores"}])
        assert blocks == {"minecraft:stone", "mod:copper_ore"}
        assert tags == {"forge:ores", "c:ores"}


class TestTagIsOreTag:
    """Tests for tag_is_ore_tag()."""

    @pytest.mark.parametrize("tag", ["c:ores", "forge:ores", "minecraft:ores", "c:ores/copper", "forge:ores/lead/deepslate", "ores", "ores/copper"])
    def test_positive(self, tag: str) -> None:
        """Tests that the function returns a positive result."""
        assert tag_is_ore_tag(tag) is True

    @pytest.mark.parametrize("tag", ["c:ore", "c:oresomething", "c:raw_materials", "forge:blocks", "", "c:"])
    def test_negative(self, tag: str) -> None:
        """Tests that a non-ore tag is correctly identified as not being an ore tag."""
        assert tag_is_ore_tag(tag) is False


class TestTagPath:
    """Tests for _tag_path()."""

    def test_strips_namespace(self) -> None:
        """Tests that the namespace prefix is stripped from a tag path."""
        assert _tag_path("forge:ores/lead") == "ores/lead"

    def test_no_namespace_returns_whole(self) -> None:
        """Tests that a tag path without a namespace is returned unchanged."""
        assert _tag_path("ores/lead") == "ores/lead"


class TestExtractMaterialFromTag:
    """Tests for extract_material_from_tag()."""

    def test_basic(self) -> None:
        """Tests extraction of material from a basic ore tag."""
        assert extract_material_from_tag("c:ores/copper") == "copper"

    def test_hierarchical_takes_first_segment(self) -> None:
        """Tests that hierarchical tags return the first segment as material."""
        assert extract_material_from_tag("c:ores/lead/deepslate") == "lead"

    def test_top_level_ores_has_no_material(self) -> None:
        """Tests that the top-level ores tag has no material."""
        assert extract_material_from_tag("c:ores") is None

    @pytest.mark.parametrize(
        "tag", ["c:ores/deepslate", "c:ores/nether", "c:ores/stone", "c:ores/blackstone", "c:ores/basalt", "c:ores/netherrack", "c:ores/end"]
    )
    def test_stone_type_words_rejected(self, tag: str) -> None:
        """Tests that stone type words are rejected as ores."""
        assert extract_material_from_tag(tag) is None

    def test_non_ore_tag_returns_none(self) -> None:
        """Tests that a non-ore tag returns None."""
        assert extract_material_from_tag("c:raw_materials/copper") is None


class TestNameLooksLikeOre:
    """Tests for name_looks_like_ore()."""

    @pytest.mark.parametrize("name", ["copper_ore", "ore_copper", "deepslate_copper_ore", "ore", "copper_ore_block"])
    def test_positive(self, name: str) -> None:
        """Tests that valid ore names are recognized."""
        assert name_looks_like_ore(name) is True

    @pytest.mark.parametrize("name", ["copper", "oreganized", "more", "core", ""])
    def test_negative(self, name: str) -> None:
        """Tests that a name which does not look like an ore is correctly identified as not an ore."""
        assert name_looks_like_ore(name) is False


class TestExtractMaterialFromName:
    """Tests for extract_material_from_name()."""

    def test_material_ore_suffix(self) -> None:
        """Tests extraction of the material from an ore name with an ore suffix."""
        assert extract_material_from_name("copper_ore") == "copper"

    def test_ore_material_prefix(self) -> None:
        """Tests extraction of the material from an ore name with an ore prefix."""
        assert extract_material_from_name("ore_aluminum") == "aluminum"

    def test_deepslate_prefix_stripped(self) -> None:
        """Tests that a deepslate prefix is stripped when extracting the material."""
        assert extract_material_from_name("deepslate_copper_ore") == "copper"

    def test_nether_prefix_stripped(self) -> None:
        """Tests that a nether prefix is stripped when extracting the material."""
        assert extract_material_from_name("nether_gold_ore") == "gold"

    def test_raw_prefix_alone_yields_none(self) -> None:
        """Tests that a raw prefix alone yields no material."""
        assert extract_material_from_name("raw_copper") is None

    def test_non_ore_name_returns_none(self) -> None:
        """Tests that a non-ore material name returns None."""
        assert extract_material_from_name("copper") is None

    def test_bare_ore_returns_none(self) -> None:
        """Tests that a bare 'ore' name yields no material."""
        assert extract_material_from_name("ore") is None

    def test_stone_ore_material_pins_current_behavior(self) -> None:
        """Tests that "stone_ore_copper" extracts "stone" as the material.

        This pins the current behavior where the stone prefix is kept instead of
        being reduced to the material, contrary to the module docstring. If the
        implementation is fixed, update the expected value to "copper".
        """
        assert extract_material_from_name("stone_ore_copper") == "stone"


class TestExtractNameSafe:
    """Tests for extract_name_safe, verifying name extraction from qualified identifiers and handling of unqualified names."""

    def test_delegates(self) -> None:
        """Tests that extract_name_safe delegates to the underlying extraction for valid names and returns None otherwise."""
        assert extract_name_safe("copper_ore") == "copper"
        assert extract_name_safe("copper") is None


class TestConditionRequiresSilkTouch:
    """Tests for _condition_requires_silk_touch, checking detection of silk touch requirements across various loot condition types."""

    def test_match_tool_with_silk_touch(self) -> None:
        """Tests that a match_tool condition with Silk Touch requires Silk Touch."""
        cond = {"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}}
        assert _condition_requires_silk_touch(cond) is True

    def test_match_tool_with_other_enchantment(self) -> None:
        """Tests that a match_tool condition with a non-Silk Touch enchantment does not require Silk Touch."""
        cond = {"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:fortune"}]}}
        assert _condition_requires_silk_touch(cond) is False

    def test_match_tool_missing_enchantments(self) -> None:
        """Tests that a match_tool condition without enchantments does not require Silk Touch."""
        cond = {"condition": "minecraft:match_tool", "predicate": {}}
        assert _condition_requires_silk_touch(cond) is False

    def test_match_tool_missing_predicate(self) -> None:
        """Tests that a match_tool condition without a predicate returns false."""
        assert _condition_requires_silk_touch({"condition": "minecraft:match_tool"}) is False

    def test_all_of_finds_silk_among_terms(self) -> None:
        """Tests that an all_of condition returns true when any term requires silk touch."""
        cond = {
            "condition": "minecraft:all_of",
            "terms": [
                {"condition": "minecraft:survives_explosion"},
                {"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}},
            ],
        }
        assert _condition_requires_silk_touch(cond) is True

    def test_any_of_no_silk(self) -> None:
        """Tests that an any_of condition with no silk touch terms returns false."""
        cond = {"condition": "minecraft:any_of", "terms": [{"condition": "minecraft:survives_explosion"}]}
        assert _condition_requires_silk_touch(cond) is False

    def test_inverted_returns_false(self) -> None:
        """Tests that an inverted condition does not require silk touch."""
        cond = {
            "condition": "minecraft:inverted",
            "term": {"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}},
        }
        assert _condition_requires_silk_touch(cond) is False

    def test_non_dict_returns_false(self) -> None:
        """Tests that non-dict inputs return False."""
        assert _condition_requires_silk_touch("not a dict") is False
        assert _condition_requires_silk_touch(None) is False

    def test_unknown_condition_type(self) -> None:
        """Tests that an unrecognized condition type does not require silk touch."""
        assert _condition_requires_silk_touch({"condition": "minecraft:random_chance"}) is False


class TestWalkLootEntries:
    """Tests for _walk_loot_entries, verifying collection of normal and silk-touch loot entries from nested loot structures."""

    def test_item_entry_normal(self) -> None:
        """Tests that an item loot entry without silk touch is recorded in the normal set."""
        normal: set[str] = set()
        silk: set[str] = set()
        _walk_loot_entries({"type": "minecraft:item", "name": "minecraft:raw_copper"}, False, normal, silk)
        assert normal == {"minecraft:raw_copper"}
        assert silk == set()

    def test_item_entry_silk(self) -> None:
        """Tests that an item loot entry with a silk touch condition is recorded in the silk set."""
        normal: set[str] = set()
        silk: set[str] = set()
        entry = {
            "type": "minecraft:item",
            "name": "minecraft:copper_ore",
            "conditions": [{"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}}],
        }
        _walk_loot_entries(entry, False, normal, silk)
        assert normal == set()
        assert silk == {"minecraft:copper_ore"}

    def test_tag_entry_prefixed(self) -> None:
        """Tests that a tag loot entry is recorded with a leading hash prefix in the normal set."""
        normal: set[str] = set()
        silk: set[str] = set()
        _walk_loot_entries({"type": "minecraft:tag", "name": "forge:raw_materials/copper"}, False, normal, silk)
        assert normal == {"#forge:raw_materials/copper"}

    def test_children_inherit_silk(self) -> None:
        """Tests that child entries of an alternatives loot entry inherit silk touch from the parent condition."""
        normal: set[str] = set()
        silk: set[str] = set()
        entry = {
            "type": "minecraft:alternatives",
            "conditions": [{"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}}],
            "children": [{"type": "minecraft:item", "name": "minecraft:copper_ore"}],
        }
        _walk_loot_entries(entry, False, normal, silk)
        assert normal == set()
        assert silk == {"minecraft:copper_ore"}

    def test_entries_key_also_walked(self) -> None:
        """Tests that entries nested under a sequence entry's 'entries' key are walked and collected."""
        normal: set[str] = set()
        silk: set[str] = set()
        entry = {"type": "minecraft:sequence", "entries": [{"type": "minecraft:item", "name": "minecraft:stone"}]}
        _walk_loot_entries(entry, False, normal, silk)
        assert normal == {"minecraft:stone"}

    def test_non_dict_ignored(self) -> None:
        """Tests that a non-dict entry is ignored."""
        normal: set[str] = set()
        silk: set[str] = set()
        _walk_loot_entries("nope", False, normal, silk)
        assert normal == set()
        assert silk == set()


class TestExtractDropsFromLootTable:
    """Tests for extract_drops_from_loot_table, verifying separation of normal and silk-touch drops from loot tables."""

    def test_empty_inputs(self) -> None:
        """Tests that empty, None, and non-dict inputs yield two empty sets."""
        assert extract_drops_from_loot_table({}) == (set(), set())
        assert extract_drops_from_loot_table(None) == (set(), set())
        assert extract_drops_from_loot_table("nope") == (set(), set())

    def test_simple_pool(self) -> None:
        """Tests that a simple single-entry pool yields one normal drop and no silk drops."""
        table = {"pools": [{"entries": [{"type": "minecraft:item", "name": "minecraft:raw_iron"}]}]}
        normal, silk = extract_drops_from_loot_table(table)
        assert normal == {"minecraft:raw_iron"}
        assert silk == set()

    def test_normal_and_silk_split(self) -> None:
        """Tests extraction of normal and silk touch drops from a loot table."""
        table = {
            "pools": [
                {
                    "entries": [
                        {"type": "minecraft:item", "name": "minecraft:raw_copper"},
                        {
                            "type": "minecraft:item",
                            "name": "minecraft:copper_ore",
                            "conditions": [{"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}}],
                        },
                    ]
                }
            ]
        }
        normal, silk = extract_drops_from_loot_table(table)
        assert normal == {"minecraft:raw_copper"}
        assert silk == {"minecraft:copper_ore"}

    def test_tag_reference_prefixed(self) -> None:
        """Tests that a tag-referenced entry is recorded with a leading hash prefix."""
        table = {"pools": [{"entries": [{"type": "minecraft:tag", "name": "forge:raw_materials/copper"}]}]}
        normal, _silk = extract_drops_from_loot_table(table)
        assert normal == {"#forge:raw_materials/copper"}


class TestIsOreFeature:
    """Tests for is_ore_feature, verifying detection of ore-type features from feature type strings."""

    @pytest.mark.parametrize("ftype", ["minecraft:ore", "forge:ore", "c:ore", "minecraft:scattered_ore"])
    def test_known_types(self, ftype: str) -> None:
        """Tests that known ore types are recognized as ore features."""
        assert is_ore_feature({"type": ftype}) is True

    def test_suffix_ore_matches(self) -> None:
        """Tests that a type ending with 'ore' is recognized as an ore feature."""
        assert is_ore_feature({"type": "create:ore"}) is True

    def test_non_ore_rejected(self) -> None:
        """Tests that a non-ore type is rejected as an ore feature."""
        assert is_ore_feature({"type": "minecraft:tree"}) is False

    def test_non_dict_rejected(self) -> None:
        """Tests that non-dictionary inputs are rejected as ore features."""
        assert is_ore_feature(None) is False
        assert is_ore_feature("minecraft:ore") is False

    def test_missing_type(self) -> None:
        """Tests that a missing type key is not recognized as an ore feature."""
        assert is_ore_feature({}) is False


class TestExtractOreTargets:
    """Tests for extract_ore_targets covering basic extraction, missing config, missing targets, missing state, and non-dict input."""

    def test_basic(self) -> None:
        """Tests that ore target states are extracted from a basic feature config."""
        feature = {"type": "minecraft:ore", "config": {"targets": [{"state": {"Name": "mod:copper_ore"}}, {"state": {"Name": "mod:deepslate_copper_ore"}}]}}
        assert extract_ore_targets(feature) == ["mod:copper_ore", "mod:deepslate_copper_ore"]

    def test_no_config(self) -> None:
        """Tests that a feature without a config key yields no results."""
        assert extract_ore_targets({"type": "minecraft:ore"}) == []

    def test_no_targets(self) -> None:
        """Tests that a config without a targets list yields no results."""
        assert extract_ore_targets({"type": "minecraft:ore", "config": {}}) == []

    def test_target_missing_state(self) -> None:
        """Tests that a target missing the state key yields no results."""
        assert extract_ore_targets({"type": "minecraft:ore", "config": {"targets": [{"weight": 1}]}}) == []

    def test_non_dict(self) -> None:
        """Tests that extract_ore_targets returns an empty list for None input."""
        assert extract_ore_targets(None) == []


class TestJarArchive:
    """Tests for the JarArchive context manager and JSON reading."""

    def test_read_json_roundtrip(self, tmp_path: Path) -> None:
        """Tests reading JSON from a JAR archive round-trips correctly."""
        jar_path = _make_jar(tmp_path / "a.jar", {"data/x.json": {"hello": "world"}})
        with JarArchive(jar_path) as jar:
            assert jar.read_json("data/x.json") == {"hello": "world"}

    def test_read_json_missing_member(self, tmp_path: Path) -> None:
        """Tests that reading a missing JSON member returns None."""
        jar_path = _make_jar(tmp_path / "a.jar", {})
        with JarArchive(jar_path) as jar:
            assert jar.read_json("nope.json") is None

    def test_read_json_malformed(self, tmp_path: Path) -> None:
        """Tests that reading malformed JSON from a JAR archive returns None."""
        jar_path = _make_jar(tmp_path / "a.jar", {"bad.json": "this is not json {"})
        with JarArchive(jar_path) as jar:
            assert jar.read_json("bad.json") is None


class TestIterJars:
    """Tests for the iter_jars helper."""

    def test_single_file(self, tmp_path: Path) -> None:
        """Tests that iter_jars yields a single JAR file when given one path."""
        p = _make_jar(tmp_path / "one.jar", {})
        assert list(iter_jars(p)) == [p]

    def test_directory_sorted(self, tmp_path: Path) -> None:
        """Tests that iter_jars returns jars from a directory in sorted order."""
        a = _make_jar(tmp_path / "a.jar", {})
        b = _make_jar(tmp_path / "b.jar", {})
        assert list(iter_jars(tmp_path)) == [a, b]

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        """Tests that iterating a nonexistent path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            list(iter_jars(tmp_path / "does-not-exist"))


class TestOreRecord:
    """Tests for OreRecord JSON serialization."""

    def test_to_json_shape(self) -> None:
        """Tests that OreRecord.to_json produces the expected dictionary structure."""
        rec = OreRecord(block_id="mod:copper_ore", source_jar="a.jar")
        rec.tags = ["c:ores/copper"]
        rec.drops_normal = ["minecraft:raw_copper"]
        rec.classification = ["NAME", "TAG"]
        rec.material = "copper"
        payload = rec.to_json()
        assert payload["block_id"] == "mod:copper_ore"
        assert payload["drops"] == {"normal": ["minecraft:raw_copper"], "silk_touch": []}
        assert payload["classification"] == ["NAME", "TAG"]
        assert payload["material"] == "copper"
        assert payload["tags"] == ["c:ores/copper"]


class TestAuditResults:
    """Tests for AuditResults record creation, reuse, and JSON serialization."""

    def test_get_or_create_creates(self) -> None:
        """Tests that get_or_create creates and stores a new record for an unknown block ID."""
        results = AuditResults()
        rec = results.get_or_create("mod:copper_ore", "a.jar")
        assert rec.block_id == "mod:copper_ore"
        assert results.ores["mod:copper_ore"] is rec

    def test_get_or_create_reuses_existing(self) -> None:
        """Tests that get_or_create returns the same record for an existing block ID."""
        results = AuditResults()
        a = results.get_or_create("mod:copper_ore", "a.jar")
        b = results.get_or_create("mod:copper_ore", "b.jar")
        assert a is b


class TestCollectJar:
    """Tests for collecting ore data from jar files into audit results."""

    def test_blockstates_and_tags(self, tmp_path: Path) -> None:
        """Tests that blockstate files and block tags are collected into audit results."""
        jar = _make_jar(
            tmp_path / "mod.jar",
            {
                "assets/mod/blockstates/copper_ore.json": {},
                "assets/mod/blockstates/copper_block.json": {},
                "data/mod/tags/blocks/ores/copper.json": {"values": ["mod:copper_ore"]},
            },
        )
        results = AuditResults()
        collect_jar(jar, results, quiet=True)
        assert "mod:copper_ore" in results.block_ids
        assert "mod:copper_block" in results.block_ids
        assert results.block_to_jar["mod:copper_ore"] == "mod.jar"
        assert "mod:copper_ore" in results.tag_to_blocks["mod:ores/copper"]
        assert "mod:ores/copper" in results.block_to_tags["mod:copper_ore"]

    def test_loot_table_index(self, tmp_path: Path) -> None:
        """Test that a block loot table is indexed by its block identifier."""
        jar = _make_jar(tmp_path / "mod.jar", {"data/mod/loot_tables/blocks/copper_ore.json": {"pools": []}})
        results = AuditResults()
        collect_jar(jar, results, quiet=True)
        assert "mod:copper_ore" in results.loot_table_index

    def test_worldgen_ore_feature(self, tmp_path: Path) -> None:
        """Test that an ore configured feature maps the target block to its worldgen feature."""
        feature = {"type": "minecraft:ore", "config": {"targets": [{"state": {"Name": "mod:copper_ore"}}]}}
        jar = _make_jar(tmp_path / "mod.jar", {"data/mod/worldgen/configured_feature/ore_copper.json": feature})
        results = AuditResults()
        collect_jar(jar, results, quiet=True)
        assert "mod:copper_ore" in results.worldgen_by_block
        assert "mod:ore_copper" in results.worldgen_by_block["mod:copper_ore"]

    def test_tag_references_recorded(self, tmp_path: Path) -> None:
        """Test that tag references and block entries are recorded during jar collection."""
        jar = _make_jar(tmp_path / "mod.jar", {"data/mod/tags/blocks/ores.json": {"values": ["#c:ores", "mod:copper_ore"]}})
        results = AuditResults()
        collect_jar(jar, results, quiet=True)
        assert "c:ores" in results.tag_to_tag_refs["mod:ores"]
        assert "mod:copper_ore" in results.tag_to_blocks["mod:ores"]


class TestResolveOreTags:
    """Tests for resolving ore tag closures, including transitive and cyclic references."""

    def test_simple_closure(self) -> None:
        """Test that resolving a simple ore tag yields the expected block set."""
        results = AuditResults()
        results.tag_to_blocks["mod:ores"] = {"mod:copper_ore"}
        resolved = _resolve_ore_tags(results)
        assert resolved["mod:ores"] == {"mod:copper_ore"}

    def test_transitive_through_tag_ref(self) -> None:
        """Tests that a tag reference resolves transitively to the blocks of the referenced tag."""
        results = AuditResults()
        results.tag_to_blocks["c:ores"] = {"minecraft:copper_ore"}
        results.tag_to_tag_refs["mod:ores"] = {"c:ores"}
        resolved = _resolve_ore_tags(results)
        assert resolved["mod:ores"] == {"minecraft:copper_ore"}

    def test_cycle_safe(self) -> None:
        """Tests that cyclic tag references resolve safely to empty sets."""
        results = AuditResults()
        results.tag_to_tag_refs["a:ores"] = {"b:ores"}
        results.tag_to_tag_refs["b:ores"] = {"a:ores"}
        resolved = _resolve_ore_tags(results)
        assert resolved["a:ores"] == set()
        assert resolved["b:ores"] == set()

    def test_non_ore_tags_ignored(self) -> None:
        """Tests that non-ore tags are excluded from resolved ore tags."""
        results = AuditResults()
        results.tag_to_blocks["mod:raw_materials"] = {"mod:raw_copper"}
        resolved = _resolve_ore_tags(results)
        assert "mod:raw_materials" not in resolved


class TestSelectCandidates:
    """Tests for selecting candidate ore blocks from audit results."""

    def test_by_name(self) -> None:
        """Tests that candidate selection matches blocks whose names indicate ore blocks."""
        results = AuditResults()
        results.block_ids.add("mod:copper_ore")
        results.block_ids.add("mod:copper_block")
        assert _select_candidates(results) == {"mod:copper_ore"}

    def test_by_ore_tag(self) -> None:
        """Tests that candidate selection matches blocks belonging to ore tags."""
        results = AuditResults()
        results.block_ids.add("mod:mystery_metal")
        results.block_to_tags["mod:mystery_metal"] = {"mod:ores/mystery"}
        assert _select_candidates(results) == {"mod:mystery_metal"}

    def test_non_ore_blocks_not_candidates(self) -> None:
        """Test that non-ore blocks are excluded from candidate selection."""
        results = AuditResults()
        results.block_ids.add("mod:copper_block")
        results.block_ids.add("mod:stone")
        assert _select_candidates(results) == set()


def _build_demo_jar(tmp_path: Path) -> Path:
    """Build a jar with a copper ore, a lead ore, and a non-ore block.

    The lead ore is reached only via a transitive tag reference:
    ``demo:ores/lead`` -> ``#c:ores/lead`` -> ``demo:deepslate_lead_ore``,
    which exercises cross-jar tag composition (both tags live in the
    same jar here for simplicity, but the closure logic is identical).
    """
    ore_copper_feature = {"type": "minecraft:ore", "config": {"targets": [{"state": {"Name": "demo:copper_ore"}}]}}
    ore_lead_feature = {"type": "minecraft:ore", "config": {"targets": [{"state": {"Name": "demo:deepslate_lead_ore"}}]}}
    copper_loot = {
        "pools": [
            {
                "entries": [
                    {"type": "minecraft:item", "name": "minecraft:raw_copper"},
                    {
                        "type": "minecraft:item",
                        "name": "demo:copper_ore",
                        "conditions": [{"condition": "minecraft:match_tool", "predicate": {"enchantments": [{"enchantment": "minecraft:silk_touch"}]}}],
                    },
                ]
            }
        ]
    }
    lead_loot = {
        "pools": [
            {
                "entries": [
                    {"type": "minecraft:item", "name": "demo:raw_lead"},
                    {"type": "minecraft:item", "name": "demo:raw_lead", "conditions": [{"condition": "minecraft:survives_explosion"}]},
                ]
            }
        ]
    }
    return _make_jar(
        tmp_path / "demo.jar",
        {
            "assets/demo/blockstates/copper_ore.json": {},
            "assets/demo/blockstates/deepslate_lead_ore.json": {},
            "assets/demo/blockstates/copper_block.json": {},
            "data/demo/tags/blocks/ores/copper.json": {"values": ["demo:copper_ore"]},
            "data/demo/tags/blocks/ores/lead.json": {"values": ["#c:ores/lead"]},
            "data/c/tags/blocks/ores/lead.json": {"values": ["demo:deepslate_lead_ore"]},
            "data/demo/loot_tables/blocks/copper_ore.json": copper_loot,
            "data/demo/loot_tables/blocks/deepslate_lead_ore.json": lead_loot,
            "data/demo/worldgen/configured_feature/ore_copper.json": ore_copper_feature,
            "data/demo/worldgen/configured_feature/ore_lead.json": ore_lead_feature,
        },
    )


class TestRunAuditIntegration:
    """Integration tests for the full ore audit pipeline."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        """Tests the full audit pipeline on a demo jar."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        assert "demo:copper_ore" in results.ores
        assert "demo:deepslate_lead_ore" in results.ores
        assert "demo:copper_block" not in results.ores
        assert results.jars_scanned == 1

    def test_material_from_tag_beats_name(self, tmp_path: Path) -> None:
        """Tests that material inference from tags takes precedence over name-based inference."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        assert results.ores["demo:copper_ore"].material == "copper"
        assert results.ores["demo:deepslate_lead_ore"].material == "lead"

    def test_classification_flags(self, tmp_path: Path) -> None:
        """Tests that classification flags are assigned to ores."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        copper = results.ores["demo:copper_ore"]
        assert "NAME" in copper.classification
        assert "TAG" in copper.classification
        assert "WORLDGEN" in copper.classification
        assert "LOOT" in copper.classification

    def test_silk_touch_drops_separated(self, tmp_path: Path) -> None:
        """Tests that normal and silk touch drops are stored separately."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        copper = results.ores["demo:copper_ore"]
        assert "minecraft:raw_copper" in copper.drops_normal
        assert "demo:copper_ore" in copper.drops_silk_touch

    def test_transitive_tag_closure(self, tmp_path: Path) -> None:
        """Tests that ore blocks receive transitively closed tag associations."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        lead = results.ores["demo:deepslate_lead_ore"]
        assert "demo:ores/lead" in lead.tags

    def test_no_jars(self, tmp_path: Path) -> None:
        """Tests that no jars are found when scanning an empty directory."""
        results = run_audit(tmp_path, quiet=True)
        assert results.jars_scanned == 0
        assert results.ores == {}

    def test_progress_output_when_not_quiet(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tests that progress output is printed when quiet mode is disabled."""
        _build_demo_jar(tmp_path)
        run_audit(tmp_path, quiet=False)
        out = capsys.readouterr().out
        assert "Ore audit" in out
        assert "Jars: 1" in out
        assert "Ore candidates: 2" in out


class TestGroupByMaterial:
    """Tests for grouping ore records by material."""

    def test_groups_and_sorts(self) -> None:
        """Tests that ores are grouped by material and sorted by block ID."""
        results = AuditResults()
        results.ores["mod:zinc_ore"] = OreRecord("mod:zinc_ore", "a.jar", material="zinc")
        results.ores["mod:copper_ore"] = OreRecord("mod:copper_ore", "a.jar", material="copper")
        grouped = group_by_material(results)
        assert list(grouped.keys()) == ["copper", "zinc"]
        assert grouped["copper"][0].block_id == "mod:copper_ore"

    def test_unknown_group(self) -> None:
        """Tests that ores without a known material are grouped under 'unknown'."""
        results = AuditResults()
        results.ores["mod:strange_ore"] = OreRecord("mod:strange_ore", "a.jar")
        grouped = group_by_material(results)
        assert "unknown" in grouped


class TestBuildJsonReport:
    """Tests for build_json_report verifying the shape and contents of the generated audit report."""

    def test_shape(self, tmp_path: Path) -> None:
        """Tests the shape of the generated audit report."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        report = build_json_report(results)
        assert report["jars_scanned"] == 1
        assert report["ore_count"] == 2
        assert "copper" in report["materials"]
        assert "lead" in report["materials"]
        copper = report["materials"]["copper"]
        assert copper["ore_blocks"] == ["demo:copper_ore"]
        assert "minecraft:raw_copper" in copper["distinct_normal_drops"]
        assert "demo:copper_ore" in copper["distinct_silk_touch_drops"]
        assert "generated" in report
        assert "ores" in report


class TestBuildMarkdownReport:
    """Tests for build_markdown_report covering material headers and normalization candidates section."""

    def test_contains_material_headers(self, tmp_path: Path) -> None:
        """Tests that the Markdown report contains expected material headers."""
        _build_demo_jar(tmp_path)
        results = run_audit(tmp_path, quiet=True)
        md = build_markdown_report(results)
        assert "# Ore Audit" in md
        assert "## COPPER" in md
        assert "## LEAD" in md
        assert "`demo:copper_ore`" in md

    def test_normalization_candidates_section(self) -> None:
        """Tests that the markdown report includes a normalization candidates section."""
        results = AuditResults()
        a = OreRecord("mod:copper_ore", "a.jar", material="copper")
        a.drops_normal = ["minecraft:raw_copper"]
        b = OreRecord("mod:copper_ore_deepslate", "a.jar", material="copper")
        b.drops_normal = ["minecraft:raw_copper_block"]
        results.ores["mod:copper_ore"] = a
        results.ores["mod:copper_ore_deepslate"] = b
        md = build_markdown_report(results)
        assert "Normalization candidates" in md


class TestWriteReport:
    """Tests for write_report verifying that both JSON and Markdown report files are written and paths are printed."""

    def test_writes_both_files(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tests that both JSON and Markdown report files are written and their paths are printed."""
        write_report({"k": "v"}, "# md\n", tmp_path)
        assert (tmp_path / "ore-audit.json").is_file()
        assert (tmp_path / "ore-audit.md").read_text() == "# md\n"
        captured = capsys.readouterr()
        assert "ore-audit.json" in captured.out
        assert "ore-audit.md" in captured.out


class TestFilterMaterials:
    """Tests for filter_materials covering matching filters, no filter passthrough, case insensitivity, and multiple materials."""

    def test_filter_keeps_matching(self) -> None:
        """Tests that filtering keeps only the matching material and its ores."""
        report = {
            "materials": {"copper": {"ore_blocks": ["m:copper_ore"]}, "lead": {"ore_blocks": ["m:lead_ore"]}},
            "ores": {"m:copper_ore": {}, "m:lead_ore": {}},
        }
        out = filter_materials(report, "copper")
        assert list(out["materials"]) == ["copper"]
        assert list(out["ores"]) == ["m:copper_ore"]

    def test_no_filter_passthrough(self) -> None:
        """Tests that a None filter returns the report unchanged."""
        report = {"materials": {}, "ores": {}}
        assert filter_materials(report, None) is report

    def test_case_insensitive(self) -> None:
        """Tests that the filter matches case-insensitively."""
        report = {"materials": {"copper": {"ore_blocks": []}}, "ores": {}}
        assert "copper" in filter_materials(report, "COPPER")["materials"]

    def test_multiple(self) -> None:
        """Tests that a comma-separated filter keeps multiple materials."""
        report = {"materials": {"copper": {"ore_blocks": []}, "lead": {"ore_blocks": []}, "zinc": {"ore_blocks": []}}, "ores": {}}
        out = filter_materials(report, "copper, zinc")
        assert set(out["materials"]) == {"copper", "zinc"}


class TestFilterMarkdown:
    """Tests for filter_markdown section filtering behavior."""

    SAMPLE = "\n".join(["# Ore Audit", "", "## COPPER", "", "- ore blocks: 1", "", "## LEAD", "", "- ore blocks: 1"])

    def test_keeps_selected_only(self) -> None:
        """Tests that only the selected section is retained while headers and other sections are preserved or removed."""
        out = filter_markdown(self.SAMPLE, "copper")
        assert "## COPPER" in out
        assert "## LEAD" not in out
        assert "# Ore Audit" in out

    def test_passthrough_when_none(self) -> None:
        """Tests that markdown content is returned unchanged when no filter is provided."""
        assert filter_markdown(self.SAMPLE, None) == self.SAMPLE


class TestResolvePaths:
    """Tests for resolving jar and output directory paths."""

    def test_jars_default(self, tmp_path: Path) -> None:
        """Tests that a None jars path resolves to the default sync/downloads directory."""
        assert resolve_jars_path(None, tmp_path) == tmp_path / "sync/downloads"

    def test_jars_relative(self, tmp_path: Path) -> None:
        """Tests that a relative jars path is resolved against the provided base directory."""
        assert resolve_jars_path(Path("custom/jars"), tmp_path) == tmp_path / "custom/jars"

    def test_jars_absolute(self, tmp_path: Path) -> None:
        """Tests that an absolute jars path is returned unchanged regardless of the base directory."""
        abs_path = Path("/tmp/absolute-jars")
        assert resolve_jars_path(abs_path, tmp_path) == abs_path

    def test_out_default(self, tmp_path: Path) -> None:
        """Tests that a missing output directory defaults to sync/audit."""
        assert resolve_out_dir(None, tmp_path) == tmp_path / "sync/audit"

    def test_out_relative(self, tmp_path: Path) -> None:
        """Tests that a relative output directory resolves against the repo root."""
        assert resolve_out_dir(Path("out"), tmp_path) == tmp_path / "out"


class TestCli:
    """Tests for the command-line interface behavior."""

    def test_parser_defaults(self) -> None:
        """Tests that the argument parser provides the expected default values."""
        parser = audit_ores.build_parser()
        args = parser.parse_args([])
        assert args.jars is None
        assert args.out_dir is None
        assert args.only is None
        assert args.quiet is False

    def test_main_missing_jars_returns_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that main returns 2 and reports missing jars when none are found."""
        monkeypatch.setattr("sys.argv", ["audit_ores.py", "--repo-root", str(tmp_path)])
        rc = audit_ores.main()
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_main_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that main succeeds and writes both audit output files."""
        jars_dir = tmp_path / "sync" / "downloads"
        _make_jar(jars_dir / "a.jar", {"assets/demo/blockstates/copper_ore.json": {}})
        out_dir = tmp_path / "sync" / "audit"
        monkeypatch.setattr("sys.argv", ["audit_ores.py", "--repo-root", str(tmp_path), "--out-dir", str(out_dir), "--quiet"])
        rc = audit_ores.main()
        assert rc == 0
        assert (out_dir / "ore-audit.json").is_file()
        assert (out_dir / "ore-audit.md").is_file()
