# tests/deploy_pack/test_deps.py

"""Tests for deploy_pack.deps.

Coverage:

  * Prism index parsing: ``*.pw.toml`` -> entry dict with id, file, side,
    side_raw, index_file, source, dependencies
  * side filter: both + target
  * jar manifests: META-INF/mods.toml and META-INF/neoforge.mods.toml,
    mandatory-only, side=BOTH/CLIENT/SERVER
  * dependency closure: index namespace, jar namespace, transitive
    expansion, order preservation, missing-dep skip
  * §6.3 unmarked detection: jar without .pw.toml, or .pw.toml whose
    side is outside {client, server, both}
  * §6.2 remove_unmarked: filters by filename

``parse_prism_toml`` never raises: malformed TOML, missing files, and
entries with no filename all return None.
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
    is_unmarked,
    load_prism_index,
    parse_prism_toml,
    remove_unmarked,
    scan_manifests,
)
from minecraft.deploy_pack.errors import ConfigError

_LOG = logging.getLogger("test_deps")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Reset the per-modpack manifest cache around every test."""
    clear_manifest_cache()
    yield
    clear_manifest_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pw(
    tmp_path: Path,
    stem: str,
    *,
    filename: str = "mod.jar",
    side: str | None = "both",
    curseforge: tuple[int, int] | None = None,
    modrinth: tuple[str, str] | None = None,
    dependencies: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a .pw.toml and return its path."""
    lines = [f'filename = "{filename}"']
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
    for addon_id, dep_type in dependencies or []:
        lines.append("[[x-prismlauncher-dependencies]]")
        lines.append(f'addonId = "{addon_id}"')
        lines.append(f'type = "{dep_type}"')
    p = tmp_path / f"{stem}.pw.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _jar(
    path: Path,
    *,
    mod_ids: list[str],
    deps: list[tuple[str, bool, str]] | None = None,
    neoforge: bool = False,
) -> Path:
    """Create a jar containing a mods.toml. ``deps`` is (modId, mandatory, side)."""
    lines: list[str] = []
    for mid in mod_ids:
        lines.append("[[mods]]")
        lines.append(f'modId = "{mid}"')
    for dep_id, mandatory, side in deps or []:
        lines.append(f"[[dependencies.{mod_ids[0]}]]")
        lines.append(f'modId = "{dep_id}"')
        lines.append(f"mandatory = {'true' if mandatory else 'false'}")
        lines.append(f'side = "{side}"')
    body = "\n".join(lines).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_name = "META-INF/neoforge.mods.toml" if neoforge else "META-INF/mods.toml"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(manifest_name, body)
    return path


def _entry(
    file: str,
    mid: str,
    side: str = "both",
    *,
    project_id: str | None = None,
    index_deps: list[tuple[str, str]] | None = None,
) -> dict:
    """Build a Prism-entry dict shaped like parse_prism_toml's output."""
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


# ---------------------------------------------------------------------------
# parse_prism_toml
# ---------------------------------------------------------------------------


def test_parse_prism_toml_minimal_entry(tmp_path: Path) -> None:
    """A minimal .pw.toml yields the required fields, side defaulting to both."""
    e = parse_prism_toml(_pw(tmp_path, "a", filename="foo.jar", side="both"))
    assert e is not None
    assert e["file"] == "foo.jar"
    assert e["side"] == "both"
    assert e["side_raw"] == "both"
    assert e["index_file"] == "a.pw.toml"
    assert e["source"] == "unknown"
    assert e["dependencies"] == []


def test_parse_prism_toml_missing_filename_returns_none(tmp_path: Path) -> None:
    """An entry without a filename field is unusable and returns None."""
    p = tmp_path / "x.pw.toml"
    p.write_text('name = "X"\n', encoding="utf-8")
    assert parse_prism_toml(p) is None


def test_parse_prism_toml_malformed_returns_none(tmp_path: Path) -> None:
    """Malformed TOML returns None rather than raising."""
    p = tmp_path / "x.pw.toml"
    p.write_text(":::not toml", encoding="utf-8")
    assert parse_prism_toml(p) is None


def test_parse_prism_toml_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing file returns None rather than raising."""
    assert parse_prism_toml(tmp_path / "nope.pw.toml") is None


def test_parse_prism_toml_side_absent_defaults_to_both(tmp_path: Path) -> None:
    """An absent side key is treated as 'both' and side_raw is None."""
    e = parse_prism_toml(_pw(tmp_path, "a", side=None))
    assert e["side"] == "both"
    assert e["side_raw"] is None


@pytest.mark.parametrize("raw", ["CLIENT", "Server", "BOTH"])
def test_parse_prism_toml_side_is_case_insensitive(tmp_path: Path, raw: str) -> None:
    """Side is lowercased; side_raw is the lowercased original."""
    e = parse_prism_toml(_pw(tmp_path, "a", side=raw))
    assert e["side"] == raw.lower()
    assert e["side_raw"] == raw.lower()


def test_parse_prism_toml_invalid_side_is_coerced_but_side_raw_preserved(tmp_path: Path) -> None:
    """§6.3: side_raw preserves the original value that the side filter cannot see."""
    e = parse_prism_toml(_pw(tmp_path, "a", side="skipped"))
    assert e["side"] == "both"
    assert e["side_raw"] == "skipped"


def test_parse_prism_toml_curseforge_source(tmp_path: Path) -> None:
    """A curseforge update block yields source='curseforge' and integer ids."""
    e = parse_prism_toml(_pw(tmp_path, "a", curseforge=(12345, 67890)))
    assert e["source"] == "curseforge"
    assert e["project_id"] == 12345
    assert e["file_id"] == 67890
    assert e["id"] == "12345"


def test_parse_prism_toml_modrinth_source(tmp_path: Path) -> None:
    """A modrinth update block yields source='modrinth' and string ids."""
    e = parse_prism_toml(_pw(tmp_path, "a", modrinth=("sodium", "abc123")))
    assert e["source"] == "modrinth"
    assert e["project_id"] == "sodium"
    assert e["file_id"] == "abc123"
    assert e["id"] == "sodium"


def test_parse_prism_toml_dependencies_block(tmp_path: Path) -> None:
    """[[x-prismlauncher-dependencies]] blocks are collected and uppercased."""
    e = parse_prism_toml(_pw(tmp_path, "a", dependencies=[("111", "required"), ("222", "optional")]))
    assert e["dependencies"] == [
        {"addon_id": "111", "type": "REQUIRED"},
        {"addon_id": "222", "type": "OPTIONAL"},
    ]


def test_parse_prism_toml_id_falls_back_to_filename(tmp_path: Path) -> None:
    """When no update block is present, id is the filename."""
    e = parse_prism_toml(_pw(tmp_path, "a", filename="standalone.jar"))
    assert e["id"] == "standalone.jar"


# ---------------------------------------------------------------------------
# load_prism_index
# ---------------------------------------------------------------------------


def test_load_prism_index_reads_every_pw_toml(tmp_path: Path) -> None:
    """load_prism_index reads all *.pw.toml files in the directory."""
    index = tmp_path / ".index"
    index.mkdir()
    _pw(index, "a", filename="a.jar")
    _pw(index, "b", filename="b.jar")
    assert {e["file"] for e in load_prism_index(index)} == {"a.jar", "b.jar"}


def test_load_prism_index_missing_directory_is_empty(tmp_path: Path) -> None:
    """A missing index directory yields an empty list."""
    assert load_prism_index(tmp_path / "nope") == []


def test_load_prism_index_ignores_non_pw_files(tmp_path: Path) -> None:
    """Only *.pw.toml files are parsed."""
    index = tmp_path / ".index"
    index.mkdir()
    _pw(index, "a", filename="a.jar")
    (index / "readme.txt").write_text("hi", encoding="utf-8")
    assert len(load_prism_index(index)) == 1


# ---------------------------------------------------------------------------
# filter_prism_entries_by_side
# ---------------------------------------------------------------------------


def test_filter_by_side_includes_both_and_target() -> None:
    """Entries with side='both' or side=target are kept."""
    entries = [
        {"file": "a.jar", "side": "both"},
        {"file": "b.jar", "side": "client"},
        {"file": "c.jar", "side": "server"},
    ]
    assert {e["file"] for e in filter_prism_entries_by_side(entries, "client")} == {"a.jar", "b.jar"}
    assert {e["file"] for e in filter_prism_entries_by_side(entries, "server")} == {"a.jar", "c.jar"}


def test_filter_by_side_rejects_unknown_target() -> None:
    """target_side must be 'client' or 'server'."""
    with pytest.raises(ValueError):
        filter_prism_entries_by_side([], "both")


# ---------------------------------------------------------------------------
# scan_manifests / JarManifest
# ---------------------------------------------------------------------------


def test_scan_manifests_reads_mods_toml(tmp_path: Path) -> None:
    """mods.toml declares modIds and mandatory deps per side."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "BOTH"), ("opt", False, "BOTH")])
    manifests = scan_manifests([{"file": "foo.jar"}], tmp_path, _LOG)
    m = manifests["foo.jar"]
    assert m.mod_ids == {"foo"}
    assert m.required_client == {"lib"}
    assert m.required_server == {"lib"}


def test_scan_manifests_reads_neoforge_mods_toml(tmp_path: Path) -> None:
    """The neoforge manifest path is also recognised."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "BOTH")], neoforge=True)
    assert "foo.jar" in scan_manifests([{"file": "foo.jar"}], tmp_path, _LOG)


def test_scan_manifests_ignores_optional_dependencies(tmp_path: Path) -> None:
    """Only mandatory=true dependencies enter the closure."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("opt", False, "BOTH")])
    m = scan_manifests([{"file": "foo.jar"}], tmp_path, _LOG)["foo.jar"]
    assert m.required_client == set()
    assert m.required_server == set()


def test_scan_manifests_side_client_only(tmp_path: Path) -> None:
    """A CLIENT-only mandatory dep appears only in required_client."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "CLIENT")])
    m = scan_manifests([{"file": "foo.jar"}], tmp_path, _LOG)["foo.jar"]
    assert m.required_client == {"lib"}
    assert m.required_server == set()


def test_scan_manifests_side_server_only(tmp_path: Path) -> None:
    """A SERVER-only mandatory dep appears only in required_server."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"], deps=[("lib", True, "SERVER")])
    m = scan_manifests([{"file": "foo.jar"}], tmp_path, _LOG)["foo.jar"]
    assert m.required_client == set()
    assert m.required_server == {"lib"}


def test_scan_manifests_skips_missing_jars(tmp_path: Path) -> None:
    """A missing jar file yields no manifest."""
    assert scan_manifests([{"file": "nope.jar"}], tmp_path, _LOG) == {}


def test_scan_manifests_skips_bad_zip(tmp_path: Path) -> None:
    """A non-zip jar file yields no manifest."""
    (tmp_path / "bad.jar").write_bytes(b"not a zip")
    assert scan_manifests([{"file": "bad.jar"}], tmp_path, _LOG) == {}


def test_scan_manifests_is_cached_per_modpack_dir(tmp_path: Path) -> None:
    """The manifest scan is cached for a modpack_dir's lifetime."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"])
    entries = [{"file": "foo.jar"}]
    a = scan_manifests(entries, tmp_path, _LOG)
    b = scan_manifests(entries, tmp_path, _LOG)
    assert a is b


def test_clear_manifest_cache_yields_a_fresh_object(tmp_path: Path) -> None:
    """clear_manifest_cache resets the cache; a fresh scan returns a new equal object."""
    _jar(tmp_path / "foo.jar", mod_ids=["foo"])
    entries = [{"file": "foo.jar"}]
    a = scan_manifests(entries, tmp_path, _LOG)
    clear_manifest_cache()
    b = scan_manifests(entries, tmp_path, _LOG)
    assert a is not b
    assert a == b


# ---------------------------------------------------------------------------
# expand_with_required -- the dependency closure
# ---------------------------------------------------------------------------


def test_expand_with_required_no_deps_returns_the_seed(tmp_path: Path) -> None:
    """No mandatory deps anywhere -> the closure equals the seed."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a"), _entry("b.jar", "b")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    assert result.forced_count == 0


def test_expand_with_required_pulls_jar_namespace_dep(tmp_path: Path) -> None:
    """A mandatory modId dep declared by a jar is pulled into the closure."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a"), _entry("b.jar", "b", "server")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    assert result.forced_count == 1


def test_expand_with_required_pulls_index_namespace_dep(tmp_path: Path) -> None:
    """A REQUIRED [[x-prismlauncher-dependencies]] dep is pulled into the closure."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [
        _entry("a.jar", "a", "both", project_id="1", index_deps=[("2", "REQUIRED")]),
        _entry("b.jar", "b", "server", project_id="2"),
    ]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
    _, forced_entry, reason = result.forced[0]
    assert forced_entry["file"] == "b.jar"
    assert "addonId=2" in reason


def test_expand_with_required_ignores_optional_index_deps(tmp_path: Path) -> None:
    """An OPTIONAL index dep does not enter the closure."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [
        _entry("a.jar", "a", project_id="1", index_deps=[("2", "OPTIONAL")]),
        _entry("b.jar", "b", "server", project_id="2"),
    ]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar"}


def test_expand_with_required_is_transitive(tmp_path: Path) -> None:
    """A -> b -> c is expanded to {a, b, c} from the seed {a}."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"], deps=[("c", True, "BOTH")])
    _jar(tmp_path / "c.jar", mod_ids=["c"])
    entries = [_entry("a.jar", "a"), _entry("b.jar", "b", "server"), _entry("c.jar", "c", "server")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar", "b.jar", "c.jar"}


def test_expand_with_required_respects_target_side(tmp_path: Path) -> None:
    """A SERVER-only mandatory dep is not pulled into the client closure."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "SERVER")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    entries = [_entry("a.jar", "a"), _entry("b.jar", "b", "server")]
    client_seed = filter_prism_entries_by_side(entries, "client")
    server_seed = filter_prism_entries_by_side(entries, "server")
    client = expand_with_required(entries, client_seed, "client", tmp_path, _LOG)
    server = expand_with_required(entries, server_seed, "server", tmp_path, _LOG)
    assert {e["file"] for e in client.entries} == {"a.jar"}
    assert {e["file"] for e in server.entries} == {"a.jar", "b.jar"}


def test_expand_with_required_silently_skips_missing_dep(tmp_path: Path) -> None:
    """A mandatory dep whose modId has no indexed jar is silently skipped."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("missing", True, "BOTH")])
    entries = [_entry("a.jar", "a")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert {e["file"] for e in result.entries} == {"a.jar"}
    assert result.forced_count == 0


def test_expand_with_required_preserves_all_entries_order(tmp_path: Path) -> None:
    """Output order follows all_entries, not discovery order."""
    _jar(tmp_path / "a.jar", mod_ids=["a"], deps=[("b", True, "BOTH")])
    _jar(tmp_path / "b.jar", mod_ids=["b"])
    _jar(tmp_path / "z.jar", mod_ids=["z"])
    entries = [_entry("z.jar", "z"), _entry("b.jar", "b", "server"), _entry("a.jar", "a")]
    seed = filter_prism_entries_by_side(entries, "client")
    result = expand_with_required(entries, seed, "client", tmp_path, _LOG)
    assert [e["file"] for e in result.entries] == ["z.jar", "b.jar", "a.jar"]


def test_expand_with_required_rejects_invalid_target_side() -> None:
    """target_side must be 'client' or 'server'."""
    with pytest.raises(ValueError):
        expand_with_required([], [], "invalid", Path("/"), _LOG)


# ---------------------------------------------------------------------------
# §6.3: is_unmarked
# ---------------------------------------------------------------------------


def test_is_unmarked_side_raw_none_is_marked() -> None:
    """§6.3: no explicit side key means the parser defaulted it to 'both', so the entry is marked."""
    assert not is_unmarked({"side_raw": None})


def test_is_unmarked_valid_sides_are_marked() -> None:
    """§6.3: client, server, and both are all inside the valid set."""
    assert not is_unmarked({"side_raw": "client"})
    assert not is_unmarked({"side_raw": "server"})
    assert not is_unmarked({"side_raw": "both"})


def test_is_unmarked_values_outside_the_set_are_unmarked() -> None:
    """§6.3: any explicit side outside {client, server, both} is unmarked."""
    assert is_unmarked({"side_raw": "universal"})
    assert is_unmarked({"side_raw": "skipped"})
    assert is_unmarked({"side_raw": ""})


# ---------------------------------------------------------------------------
# §6.3: find_unmarked
# ---------------------------------------------------------------------------


def test_find_unmarked_jar_without_pw_toml(tmp_path: Path) -> None:
    """§6.3 rule 1: a jar with no matching index entry is unmarked."""
    _jar(tmp_path / "orphan.jar", mod_ids=["orphan"])
    unmarked = find_unmarked(tmp_path, [])
    assert len(unmarked) == 1
    assert unmarked[0].filename == "orphan.jar"
    assert unmarked[0].reason == "no .pw.toml"


def test_find_unmarked_valid_side_is_not_reported(tmp_path: Path) -> None:
    """§6.3: an entry with a valid side is marked and not reported."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    assert find_unmarked(tmp_path, [_entry("a.jar", "a", "both")]) == []


def test_find_unmarked_side_raw_none_is_not_reported(tmp_path: Path) -> None:
    """§6.3: side_raw is None means the entry is treated as marked."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a")
    e["side_raw"] = None
    assert find_unmarked(tmp_path, [e]) == []


def test_find_unmarked_side_outside_the_set_is_reported(tmp_path: Path) -> None:
    """§6.3 rule 2: an entry whose declared side is outside the set is unmarked."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a", "both")
    e["side_raw"] = "skipped"
    unmarked = find_unmarked(tmp_path, [e])
    assert len(unmarked) == 1
    assert "skipped" in unmarked[0].reason


def test_find_unmarked_arbitrary_invalid_side_is_reported(tmp_path: Path) -> None:
    """§6.3 rule 2 covers any value, not just the ones the spec names."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    e = _entry("a.jar", "a", "both")
    e["side_raw"] = "sometimes"
    assert len(find_unmarked(tmp_path, [e])) == 1


def test_find_unmarked_results_are_sorted_by_filename(tmp_path: Path) -> None:
    """§6.3: results are sorted for deterministic output."""
    for name in ("z.jar", "a.jar", "m.jar"):
        _jar(tmp_path / name, mod_ids=[name.replace(".jar", "")])
    unmarked = find_unmarked(tmp_path, [])
    assert [u.filename for u in unmarked] == ["a.jar", "m.jar", "z.jar"]


def test_find_unmarked_no_duplicates(tmp_path: Path) -> None:
    """§6.3: a jar cannot be unmarked by both rules simultaneously."""
    _jar(tmp_path / "a.jar", mod_ids=["a"])
    assert find_unmarked(tmp_path, [_entry("a.jar", "a", "both")]) == []


def test_find_unmarked_index_only_entry_still_reported(tmp_path: Path) -> None:
    """§6.3: an entry with an invalid side is reported even when its jar is absent."""
    e = _entry("ghost.jar", "ghost")
    e["side_raw"] = "skipped"
    assert [u.filename for u in find_unmarked(tmp_path, [e])] == ["ghost.jar"]


def test_find_unmarked_nonexistent_directory_raises_config_error() -> None:
    """§6.3: a missing modpack_dir is a configuration error."""
    with pytest.raises(ConfigError):
        find_unmarked(Path("/nonexistent-modpack"), [])


# ---------------------------------------------------------------------------
# §6.2: remove_unmarked
# ---------------------------------------------------------------------------


def test_remove_unmarked_filters_by_filename() -> None:
    """§6.2: every entry whose file appears in the unmarked list is dropped."""
    entries = [{"file": "a.jar"}, {"file": "b.jar"}, {"file": "c.jar"}]
    unmarked = [UnmarkedJar(filename="b.jar", reason="x"), UnmarkedJar(filename="c.jar", reason="y")]
    assert [e["file"] for e in remove_unmarked(entries, unmarked)] == ["a.jar"]


def test_remove_unmarked_empty_list_is_a_noop() -> None:
    """§6.2: nothing unmarked -> entries unchanged."""
    entries = [{"file": "a.jar"}]
    assert remove_unmarked(entries, []) == entries
