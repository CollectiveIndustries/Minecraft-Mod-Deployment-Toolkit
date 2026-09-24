# tests/deploy_pack/test_deps.py

"""Tests for deploy_pack.deps, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * parse_prism_toml: full parse, missing filename, malformed TOML,
    side default, side case-insensitivity, side_raw preservation,
    dependency blocks, CF/MR source detection
  * load_prism_index: directory scan
  * filter_prism_entries_by_side: both / client / server
  * JarManifest: reads META-INF/mods.toml and neoforge.mods.toml,
    mandatory-only, side BOTH / CLIENT / SERVER
  * scan_manifests: cache, missing jars, cache clear
  * expand_with_required: index-namespace closure, jar-namespace
    closure, two-namespace bridging, forced reporting, order
    preservation, idempotence, missing deps
  * find_unmarked: jar with no .pw.toml, side="skipped" → unmarked,
    side=None → marked, valid side → marked, sorted, no duplicates
  * remove_unmarked: filters by filename
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import pytest

from minecraft.deploy_pack.deps import (
    UnmarkedJar,
    clear_manifest_cache,
    expand_with_required,
    filter_prism_entries_by_side,
    find_unmarked,
    format_diagnostic,
    load_prism_index,
    parse_prism_toml,
    remove_unmarked,
    scan_manifests,
)
from minecraft.deploy_pack.errors import ConfigError

_LOG = logging.getLogger("test_deps")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_manifest_cache()
    yield
    clear_manifest_cache()


def _pw(
    tmp_path: Path,
    stem: str,
    *,
    filename: str = "mod.jar",
    name: str = "Mod",
    side: str | None = "both",
    curseforge: tuple[int, int] | None = None,
    modrinth: tuple[str, str] | None = None,
    download_url: str | None = None,
    hash_value: str | None = None,
    dependencies: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a .pw.toml and return its path."""
    lines = [f'name = "{name}"', f'filename = "{filename}"']
    if side is not None:
        lines.append(f'side = "{side}"')
    if curseforge is not None:
        pid, fid = curseforge
        lines.append("[update.curseforge]")
        lines.append(f"project-id = {pid}")
        lines.append(f"file-id = {fid}")
    elif modrinth is not None:
        mid, ver = modrinth
        lines.append("[update.modrinth]")
        lines.append(f'mod-id = "{mid}"')
        lines.append(f'version = "{ver}"')
    if download_url:
        lines.append("[download]")
        lines.append(f'url = "{download_url}"')
        if hash_value:
            lines.append(f'hash = "{hash_value}"')
    if dependencies:
        for addon_id, dep_type in dependencies:
            lines.append("[[x-prismlauncher-dependencies]]")
            lines.append(f'addonId = "{addon_id}"')
            lines.append(f'type = "{dep_type}"')
    p = tmp_path / f"{stem}.pw.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _jar(path: Path, *, mod_ids: list[str], deps: list[tuple[str, bool, str]] | None = None, neoforge: bool = False) -> Path:
    """Create a jar with a mods.toml. ``deps`` is (modId, mandatory, side)."""
    toml_lines: list[str] = []
    for mid in mod_ids:
        toml_lines.append("[[mods]]")
        toml_lines.append(f'modId = "{mid}"')
    if deps:
        for dep_id, mandatory, side in deps:
            toml_lines.append(f"[[dependencies.{mod_ids[0]}]]")
            toml_lines.append(f'modId = "{dep_id}"')
            toml_lines.append(f"mandatory = {('true' if mandatory else 'false')}")
            toml_lines.append(f'side = "{side}"')
    body = "\n".join(toml_lines).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        manifest_name = "META-INF/neoforge.mods.toml" if neoforge else "META-INF/mods.toml"
        zf.writestr(manifest_name, body)
    return path


def test_parse_minimal(tmp_path: Path) -> None:
    """Tests that a minimal valid TOML file is parsed with expected defaults."""
    p = _pw(tmp_path, "a", filename="foo.jar", side="both")
    e = parse_prism_toml(p)
    assert e is not None
    assert e["file"] == "foo.jar"
    assert e["side"] == "both"
    assert e["side_raw"] == "both"
    assert e["index_file"] == "a.pw.toml"
    assert e["source"] == "unknown"
    assert e["dependencies"] == []


def test_parse_missing_filename_returns_none(tmp_path: Path) -> None:
    """Tests that parsing a TOML file without a filename returns None."""
    p = tmp_path / "x.pw.toml"
    p.write_text('name = "X"\n', encoding="utf-8")
    assert parse_prism_toml(p) is None


def test_parse_malformed_toml_returns_none(tmp_path: Path) -> None:
    """Tests that parsing malformed TOML content returns None."""
    p = tmp_path / "x.pw.toml"
    p.write_text(":::not toml", encoding="utf-8")
    assert parse_prism_toml(p) is None


def test_parse_missing_file_returns_none(tmp_path: Path) -> None:
    """Tests that parse_prism_toml returns None for a missing file."""
    assert parse_prism_toml(tmp_path / "nope.pw.toml") is None


def test_parse_side_default_is_both(tmp_path: Path) -> None:
    """Tests that parse_prism_toml defaults side to "both" when side is None."""
    p = _pw(tmp_path, "a", side=None)
    e = parse_prism_toml(p)
    assert e["side"] == "both"
    assert e["side_raw"] is None


@pytest.mark.parametrize("raw", ["CLIENT", "Server", "BOTH"])
def test_parse_side_case_insensitive(tmp_path: Path, raw: str) -> None:
    """Tests case-insensitive parsing of the side field in a Prism TOML file.

    Verifies that the side value is lowercased and the original raw value is also
    normalized to lowercase.

    Args:
        tmp_path: Temporary directory path provided by pytest.
        raw: The raw side value to test, supplied via parametrization.
    """
    p = _pw(tmp_path, "a", side=raw)
    e = parse_prism_toml(p)
    assert e["side"] == raw.lower()
    assert e["side_raw"] == raw.lower()


def test_parse_side_invalid_coerced_but_raw_preserved(tmp_path: Path) -> None:
    """Tests handling of an invalid side value in a Prism TOML file.

    Verifies that an unrecognized side value is coerced to "both" while the
    original raw value is preserved in side_raw.
    """
    p = _pw(tmp_path, "a", side="skipped")
    e = parse_prism_toml(p)
    assert e["side"] == "both"
    assert e["side_raw"] == "skipped"


def test_parse_curseforge(tmp_path: Path) -> None:
    """Tests parsing of a CurseForge-sourced mod entry from a Prism TOML file.

    Verifies that source, project_id, file_id, and id are correctly extracted for
    CurseForge mod metadata.
    """
    p = _pw(tmp_path, "a", curseforge=(12345, 67890))
    e = parse_prism_toml(p)
    assert e["source"] == "curseforge"
    assert e["project_id"] == 12345
    assert e["file_id"] == 67890
    assert e["id"] == "12345"


def test_parse_modrinth(tmp_path: Path) -> None:
    """Tests parsing of a Modrinth-sourced mod entry from a Prism TOML file.

    Verifies that source, project_id, file_id, and id are correctly extracted for
    Modrinth mod metadata.
    """
    p = _pw(tmp_path, "a", modrinth=("sodium", "abc123"))
    e = parse_prism_toml(p)
    assert e["source"] == "modrinth"
    assert e["project_id"] == "sodium"
    assert e["file_id"] == "abc123"
    assert e["id"] == "sodium"


def test_parse_download_block(tmp_path: Path) -> None:
    """Tests parsing of a download block entry from a Prism TOML file.

    Verifies that download_url, hash_value, and hash_format are correctly extracted
    and defaults to sha512 for hash_format.
    """
    p = _pw(tmp_path, "a", download_url="https://example/foo.jar", hash_value="deadbeef")
    e = parse_prism_toml(p)
    assert e["download_url"] == "https://example/foo.jar"
    assert e["hash_value"] == "deadbeef"
    assert e["hash_format"] == "sha512"


def test_parse_dependencies_block(tmp_path: Path) -> None:
    """Tests that Prism TOML dependency entries are parsed and normalized."""
    p = _pw(tmp_path, "a", dependencies=[("111", "required"), ("222", "optional")])
    e = parse_prism_toml(p)
    assert e["dependencies"] == [{"addon_id": "111", "type": "REQUIRED"}, {"addon_id": "222", "type": "OPTIONAL"}]


def test_parse_id_falls_back_to_filename(tmp_path: Path) -> None:
    """Verifies that parse_prism_toml uses the filename as the id when no id is specified."""
    p = _pw(tmp_path, "a", filename="standalone.jar")
    e = parse_prism_toml(p)
    assert e["id"] == "standalone.jar"


def test_load_index(tmp_path: Path) -> None:
    """Verifies that load_prism_index reads all .pw files and returns entries with their filenames."""
    index = tmp_path / ".index"
    index.mkdir()
    _pw(index, "a", filename="a.jar", name="A")
    _pw(index, "b", filename="b.jar", name="B")
    entries = load_prism_index(index)
    assert {e["file"] for e in entries} == {"a.jar", "b.jar"}


def test_load_index_missing_dir(tmp_path: Path) -> None:
    """Verifies that load_prism_index returns an empty list when the index directory does not exist."""
    assert load_prism_index(tmp_path / "nope") == []


def test_load_index_skips_non_pw_files(tmp_path: Path) -> None:
    """Verifies that load_prism_index ignores non-.pw files in the index directory."""
    index = tmp_path / ".index"
    index.mkdir()
    _pw(index, "a", filename="a.jar")
    (index / "readme.txt").write_text("hi", encoding="utf-8")
    entries = load_prism_index(index)
    assert len(entries) == 1


def test_filter_both_matches_both_sides() -> None:
    """Tests that entries marked as both are included when filtering for either client or server side."""
    entries = [{"file": "a.jar", "side": "both"}, {"file": "b.jar", "side": "client"}, {"file": "c.jar", "side": "server"}]
    client = filter_prism_entries_by_side(entries, "client")
    server = filter_prism_entries_by_side(entries, "server")
    assert {e["file"] for e in client} == {"a.jar", "b.jar"}
    assert {e["file"] for e in server} == {"a.jar", "c.jar"}


def test_filter_invalid_target_side() -> None:
    """Tests that filtering with an invalid target side raises a ValueError."""
    with pytest.raises(ValueError):
        filter_prism_entries_by_side([], "both")


def test_read_jar_manifest_basic(tmp_path: Path) -> None:
    """Tests reading a basic jar manifest, verifying mod IDs and required client and server dependencies."""
    jar = _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "BOTH"), ("opt", False, "BOTH")])
    entries = [{"file": "foo.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    m = manifests["foo.jar"]
    assert m.mod_ids == {"foo"}
    assert m.required_client == {"lib"}
    assert m.required_server == {"lib"}


def test_read_jar_manifest_side_client_only(tmp_path: Path) -> None:
    """Tests parsing of a jar manifest with a client-only required dependency.

    Creates a jar whose module "foo" requires module "lib" on the client side
    only, then verifies the parsed manifest places "lib" in required_client and
    leaves required_server empty.
    """
    jar = _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "CLIENT")])
    entries = [{"file": "foo.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    m = manifests["foo.jar"]
    assert m.required_client == {"lib"}
    assert m.required_server == set()


def test_read_jar_manifest_side_server_only(tmp_path: Path) -> None:
    """Tests that a server-only dependency is correctly recorded in the manifest."""
    jar = _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "SERVER")])
    entries = [{"file": "foo.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    m = manifests["foo.jar"]
    assert m.required_client == set()
    assert m.required_server == {"lib"}


def test_read_jar_manifest_neoforge(tmp_path: Path) -> None:
    """Tests that a NeoForge mod jar manifest is successfully read."""
    jar = _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "BOTH")], neoforge=True)
    entries = [{"file": "foo.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    assert "foo.jar" in manifests


def test_read_jar_manifest_optional_ignored(tmp_path: Path) -> None:
    """Tests that an optional dependency is ignored in the manifest."""
    jar = _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("opt", False, "BOTH")])
    entries = [{"file": "foo.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    assert manifests["foo.jar"].required_client == set()


def test_read_jar_manifest_missing_file(tmp_path: Path) -> None:
    """Tests that a missing jar file results in no manifest entries."""
    entries = [{"file": "nope.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    assert manifests == {}


def test_read_jar_manifest_bad_zip(tmp_path: Path) -> None:
    """Tests that an invalid zip file results in no manifest entries."""
    (tmp_path / "bad.jar").write_bytes(b"not a zip")
    entries = [{"file": "bad.jar"}]
    manifests = scan_manifests(entries, tmp_path, _LOG)
    assert manifests == {}


def test_manifest_cache_returns_same_object(tmp_path: Path) -> None:
    """Tests that repeated manifest scans return the cached object.

    Scans manifests for the same entries twice and verifies both calls return
    the identical object.
    """
    _jar(tmp_path / "foo.jar", mod_ids=["foo"])
    entries = [{"file": "foo.jar"}]
    a = scan_manifests(entries, tmp_path, _LOG)
    b = scan_manifests(entries, tmp_path, _LOG)
    assert a is b


def test_manifest_cache_cleared(tmp_path: Path) -> None:
    """Tests that clearing the manifest cache produces a fresh result object.

    Scans manifests for a jar, clears the cache, and scans again. Verifies the
    two results are distinct objects but contain equal data.
    """
    _jar(tmp_path / "foo.jar", mod_ids=["foo"])
    entries = [{"file": "foo.jar"}]
    a = scan_manifests(entries, tmp_path, _LOG)
    clear_manifest_cache()
    b = scan_manifests(entries, tmp_path, _LOG)
    assert a is not b
    assert a == b


def _entry(file: str, mid: str, side: str = "both", *, project_id: str | None = None, index_deps: list[tuple[str, str]] | None = None) -> dict:
    return {
        "id": mid,
        "file": file,
        "side": side,
        "side_raw": side,
        "index_file": f"{file}.pw.toml",
        "project_id": project_id,
        "file_id": None,
        "display_name": mid,
        "source": "unknown",
        "download_url": None,
        "hash_value": None,
        "hash_format": "sha512",
        "dependencies": [{"addon_id": aid, "type": t} for aid, t in index_deps or []],
    }


def test_closure_no_deps(tmp_path: Path) -> None:
    """Tests closure expansion when no entries have dependencies.

    Creates two independent jars with no dependency relationships and verifies
    that expanding the client-side closure includes both jars and forces nothing.
    """
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a"), _entry("b.jar", "b")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    assert result.forced_count == 0


def test_closure_pulls_in_jar_dep(tmp_path: Path) -> None:
    """A requires b via mods.toml; b declares side=server, so the client
    side filter excludes it. The closure pulls b back in.
    """
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a", "both"), _entry("b.jar", "b", "server")]
    seed = filter_prism_entries_by_side(entries, "client")
    assert {e["file"] for e in seed} == {"a.jar"}
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    assert result.forced_count == 1


def test_closure_pulls_in_index_dep(tmp_path: Path) -> None:
    """A requires b via [[x-prismlauncher-dependencies]]; b declares
    side=server and is force-included on the client.
    """
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a", "both", project_id="1", index_deps=[("2", "REQUIRED")]), _entry("b.jar", "b", "server", project_id="2")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    assert result.forced_count == 1
    _, forced_entry, reason = result.forced[0]
    assert forced_entry["file"] == "b.jar"
    assert "addonId=2" in reason


def test_closure_index_optional_ignored(tmp_path: Path) -> None:
    """Tests that optional index dependencies are ignored during closure expansion.

    Creates two jars where module "a" optionally depends on module "b" (which is
    server-side only). Verifies that expanding the client-side closure from "a"
    does not pull in "b" and that only "a.jar" remains in the result.
    """
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a", project_id="1", index_deps=[("2", "OPTIONAL")]), _entry("b.jar", "b", "server", project_id="2")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar"}


def test_closure_transitive(tmp_path: Path) -> None:
    """A → b → c; only a is in the seed. Both b and c are pulled in."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"], deps=[("c", True, "BOTH")])
    _jar(tmp_path / "c.jar", mod_ids=["c"])
    entries = [_entry("a.jar", "a", "both"), _entry("b.jar", "b", "server"), _entry("c.jar", "c", "server")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar", "c.jar"}


def test_closure_side_specific(tmp_path: Path) -> None:
    """Client-side closure does not pull in a SERVER-only dep."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "SERVER")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a", "both"), _entry("b.jar", "b", "server")]
    client_seed = filter_prism_entries_by_side(entries, "client")
    server_seed = filter_prism_entries_by_side(entries, "server")
    client_result = expand_with_required(entries, client_seed, "client", tmp_path, _LOG)
    server_result = expand_with_required(entries, server_seed, "server", tmp_path, _LOG)
    assert {e["file"] for e in client_result.entries} == {"a.jar"}
    assert {e["file"] for e in server_result.entries} == {"a.jar", "b.jar"}


def test_closure_missing_dep_is_skipped(tmp_path: Path) -> None:
    """A dependency whose modId has no indexed jar is silently skipped."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("missing", True, "BOTH")])
    entries = [_entry("a.jar", "a")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar"}
    assert result.forced_count == 0


def test_closure_order_preserves_all_entries(tmp_path: Path) -> None:
    """Result order follows all_entries, not discovery order."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    _jar(tmp_path / "z.jar", mod_ids=["z"])
    entries = [_entry("z.jar", "z", "both"), _entry("b.jar", "b", "server"), _entry("a.jar", "a", "both")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert [e["file"] for e in result.entries] == ["z.jar", "b.jar", "a.jar"]


def test_closure_invalid_side() -> None:
    """Tests that expand_with_required raises ValueError when given an invalid side argument."""
    with pytest.raises(ValueError):
        expand_with_required([], [], "invalid", Path("/"), _LOG)


def test_format_diagnostic_smoke(tmp_path: Path) -> None:
    """Smoke test for format_diagnostic, verifying output contains expected headers and entry names."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a", "both"), _entry("b.jar", "b", "server")]
    seeds = {"client": filter_prism_entries_by_side(entries, "client"), "server": filter_prism_entries_by_side(entries, "server")}
    out = format_diagnostic(entries, seeds, tmp_path, _LOG)
    assert "Dependency closure diagnostic" in out
    assert "--- Side: client ---" in out
    assert "--- Side: server ---" in out
    assert "b.jar" in out


def test_find_unmarked_no_index_entry(tmp_path: Path) -> None:
    """Tests that a jar file with no corresponding index entry is reported as unmarked with reason 'no .pw.toml'."""
    _jar(tmp_path / "orphan.jar", mod_ids=["orphan"])
    unmarked = find_unmarked(tmp_path, [])
    assert len(unmarked) == 1
    assert unmarked[0].filename == "orphan.jar"
    assert unmarked[0].reason == "no .pw.toml"


def test_find_unmarked_valid_side_is_marked(tmp_path: Path) -> None:
    """Tests that an entry with a valid side value is not reported as unmarked."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    entries = [_entry("a.jar", "a", "both")]
    assert find_unmarked(tmp_path, entries) == []


def test_find_unmarked_no_side_key_is_marked(tmp_path: Path) -> None:
    """Tests that an entry with no side key (side_raw is None) is not reported as unmarked."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a")
    e["side_raw"] = None
    assert find_unmarked(tmp_path, [e]) == []


def test_find_unmarked_skipped_side(tmp_path: Path) -> None:
    """Tests that an entry with a skipped side value is reported as unmarked with a reason containing 'skipped'."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a", "both")
    e["side_raw"] = "skipped"
    unmarked = find_unmarked(tmp_path, [e])
    assert len(unmarked) == 1
    assert "skipped" in unmarked[0].reason


def test_find_unmarked_arbitrary_invalid_side(tmp_path: Path) -> None:
    """Tests that an entry with an arbitrary invalid side value is reported as unmarked."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a", "both")
    e["side_raw"] = "sometimes"
    unmarked = find_unmarked(tmp_path, [e])
    assert len(unmarked) == 1


def test_find_unmarked_sorted(tmp_path: Path) -> None:
    """Verifies that find_unmarked returns JAR files in sorted order by filename when the index is empty."""
    for name in ("z.jar", "a.jar", "m.jar"):
        _jar(tmp_path / name, mod_ids=[name.replace(".jar", "")])
    unmarked = find_unmarked(tmp_path, [])
    assert [u.filename for u in unmarked] == ["a.jar", "m.jar", "z.jar"]


def test_find_unmarked_no_duplicates(tmp_path: Path) -> None:
    """A jar cannot be unmarked by both rules simultaneously."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a", "both")
    assert find_unmarked(tmp_path, [e]) == []


def test_find_unmarked_index_only_entry(tmp_path: Path) -> None:
    """An index entry with an invalid side whose jar is absent is still
    reported - the entry exists but is unmarked.
    """
    e = _entry("ghost.jar", "ghost")
    e["side_raw"] = "skipped"
    unmarked = find_unmarked(tmp_path, [e])
    assert [u.filename for u in unmarked] == ["ghost.jar"]


def test_find_unmarked_nonexistent_dir_raises() -> None:
    """Tests that find_unmarked raises ConfigError when given a nonexistent directory path."""
    with pytest.raises(ConfigError):
        find_unmarked(Path("/nonexistent-modpack"), [])


def test_remove_unmarked() -> None:
    """Tests that entries whose files appear in the unmarked list are removed, leaving only marked entries."""
    entries = [{"file": "a.jar"}, {"file": "b.jar"}, {"file": "c.jar"}]
    unmarked = [UnmarkedJar(filename="b.jar", reason="x"), UnmarkedJar(filename="c.jar", reason="y")]
    result = remove_unmarked(entries, unmarked)
    assert [e["file"] for e in result] == ["a.jar"]


def test_remove_unmarked_empty_list() -> None:
    """Tests that removing unmarked entries with an empty mark list returns the original entries unchanged."""
    entries = [{"file": "a.jar"}]
    assert remove_unmarked(entries, []) == entries
