# tests/unit/common/test_deps.py

"""Unit tests for the dependency closure module.

These tests exercise the two-source closure that keeps the client and
server packs loadable. The failure they exist to prevent is the one
that produced the September 2026 crash report: a library tagged
``side='server'`` in the Prism index but required at load time by a
mod tagged ``side='both'``, shipped in the client ZIP without its
dependency.

The closure reads both:

  - the Prism index's ``[[x-prismlauncher-dependencies]]`` blocks
    (project-id namespace)
  - each jar's ``META-INF/mods.toml`` (modId namespace)

and pulls missing REQUIRED deps back into the side-filtered set
regardless of what their own ``side`` field says.

The two namespaces are bridged by scanning every jar once for its
declared modIds. Every test in this file exercises a piece of that
bridge, the closure walk, or the diagnostic output.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.minecraft.common import deps

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class DummyLogger:
    """A logger that discards everything (avoids formatting surprises)."""

    def debug(self, msg, *args, **kwargs):
        """No-op."""
        pass

    def info(self, msg, *args, **kwargs):
        """No-op."""
        pass

    def warning(self, msg, *args, **kwargs):
        """No-op."""
        pass

    def error(self, msg, *args, **kwargs):
        """No-op."""
        pass


@pytest.fixture
def dummy_logger():
    """Return a no-op logger for deps.py calls."""
    return DummyLogger()


@pytest.fixture
def magic_logger():
    """Return a MagicMock logger when assertions on log calls matter."""
    return MagicMock()


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    """Clear deps.py's module-level manifest cache between tests.

    The cache is keyed by resolved modpack_dir. Every test creates its
    own tmp_path, so collisions shouldn't happen in practice - but
    clearing explicitly guarantees no test can observe another test's
    stale manifests if pytest ever reuses a tmp root.
    """
    deps._MANIFEST_CACHE.clear()
    yield
    deps._MANIFEST_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    file: str,
    *,
    side: str = "both",
    project_id: str | int | None = None,
    index_deps: list[tuple[str, str]] | None = None,
) -> dict:
    """Build an entry dict shaped like prism.parse_prism_toml's output.

    ``index_deps`` is a list of ``(addon_id, type)`` tuples, e.g.
    ``[("531761", "REQUIRED")]``. The ``type`` is upper-cased to match
    what prism.py produces.
    """
    dependencies = [{"addon_id": str(aid), "type": dtype.upper()} for aid, dtype in (index_deps or [])]
    return {
        "id": str(project_id) if project_id else file,
        "file": file,
        "side": side,
        "project_id": project_id,
        "file_id": None,
        "display_name": file,
        "source": "unknown",
        "download_url": None,
        "hash_value": None,
        "hash_format": "sha512",
        "dependencies": dependencies,
    }


def _make_jar(path: Path, mod_id: str, deps_spec: list[dict] | None = None, *, extra_mod_ids: list[str] | None = None) -> Path:
    """Write a jar containing a META-INF/mods.toml.

    ``deps_spec`` is a list of dicts with keys:
      - modId       (required)
      - mandatory   (default True)
      - side        (default "BOTH")
      - versionRange(default "[0,)")

    ``extra_mod_ids`` adds more ``[[mods]]`` blocks to simulate a jar
    that provides more than one mod id.
    """
    lines = [
        'modLoader = "javafml"',
        'loaderVersion = "[47,)"',
        "",
        "[[mods]]",
        f'modId = "{mod_id}"',
    ]
    for extra in extra_mod_ids or []:
        lines += ["", "[[mods]]", f'modId = "{extra}"']

    for dep in deps_spec or []:
        mandatory = "true" if dep.get("mandatory", True) else "false"
        side = dep.get("side", "BOTH")
        lines += [
            "",
            f"[[dependencies.{mod_id}]]",
            f'modId = "{dep["modId"]}"',
            f"mandatory = {mandatory}",
            f'versionRange = "{dep.get("versionRange", "[0,)")}"',
            'ordering = "NONE"',
            f'side = "{side}"',
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", "\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# JarManifest dataclass
# ---------------------------------------------------------------------------


class TestJarManifest:
    """Structural tests for the JarManifest dataclass."""

    def test_defaults_are_empty_sets(self):
        """A freshly constructed manifest has empty sets, not shared state."""
        a = deps.JarManifest(filename="a.jar")
        b = deps.JarManifest(filename="b.jar")
        assert a.mod_ids == set()
        assert a.required_client == set()
        assert a.required_server == set()
        # Ensure no shared-mutable-default footgun.
        a.mod_ids.add("x")
        assert b.mod_ids == set()


# ---------------------------------------------------------------------------
# _read_jar_manifest
# ---------------------------------------------------------------------------


class TestReadJarManifest:
    """Reading META-INF/mods.toml out of a jar."""

    def test_reads_simple_manifest(self, tmp_path, dummy_logger):
        """A single [[mods]] block yields one modId and no deps."""
        jar = _make_jar(tmp_path / "simple.jar", "examplemod")
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m is not None
        assert m.mod_ids == {"examplemod"}
        assert m.required_client == set()
        assert m.required_server == set()

    def test_reads_multiple_mod_ids(self, tmp_path, dummy_logger):
        """A jar with two [[mods]] blocks exposes both modIds."""
        jar = _make_jar(
            tmp_path / "multi.jar",
            "primarymod",
            extra_mod_ids=["secondarymod"],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.mod_ids == {"primarymod", "secondarymod"}

    def test_mandatory_both_dep_lands_on_both_sides(self, tmp_path, dummy_logger):
        """A BOTH-side mandatory dep appears on both required sets."""
        jar = _make_jar(
            tmp_path / "m.jar",
            "consumer",
            deps_spec=[{"modId": "lib", "side": "BOTH"}],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.required_client == {"lib"}
        assert m.required_server == {"lib"}

    def test_client_only_dep_lands_only_on_client(self, tmp_path, dummy_logger):
        """A CLIENT-side mandatory dep is not required on the server."""
        jar = _make_jar(
            tmp_path / "m.jar",
            "consumer",
            deps_spec=[{"modId": "clientlib", "side": "CLIENT"}],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.required_client == {"clientlib"}
        assert m.required_server == set()

    def test_server_only_dep_lands_only_on_server(self, tmp_path, dummy_logger):
        """A SERVER-side mandatory dep is not required on the client."""
        jar = _make_jar(
            tmp_path / "m.jar",
            "consumer",
            deps_spec=[{"modId": "serverlib", "side": "SERVER"}],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.required_client == set()
        assert m.required_server == {"serverlib"}

    def test_optional_dep_is_ignored(self, tmp_path, dummy_logger):
        """Mandatory = false deps are not part of the closure."""
        jar = _make_jar(
            tmp_path / "m.jar",
            "consumer",
            deps_spec=[{"modId": "optional_lib", "mandatory": False}],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.required_client == set()
        assert m.required_server == set()

    def test_side_lowercase_is_normalized(self, tmp_path, dummy_logger):
        """A lowercase side value is uppercased before comparison."""
        jar = _make_jar(
            tmp_path / "m.jar",
            "consumer",
            deps_spec=[{"modId": "lib", "side": "client"}],
        )
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m.required_client == {"lib"}
        assert m.required_server == set()

    def test_missing_mods_toml_returns_none(self, tmp_path, dummy_logger):
        """A jar with no mods.toml yields None rather than an empty manifest."""
        jar = tmp_path / "empty.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("README.txt", "not a forge mod")
        assert deps._read_jar_manifest(jar, dummy_logger) is None

    def test_neoforge_manifest_fallback(self, tmp_path, dummy_logger):
        """META-INF/neoforge.mods.toml is used when mods.toml is absent."""
        jar = tmp_path / "neo.jar"
        content = 'modLoader = "javafml"\nloaderVersion = "[47,)"\n\n[[mods]]\nmodId = "neomod"\n'
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("META-INF/neoforge.mods.toml", content)
        m = deps._read_jar_manifest(jar, dummy_logger)
        assert m is not None
        assert m.mod_ids == {"neomod"}

    def test_invalid_zip_returns_none(self, tmp_path, dummy_logger):
        """A file that isn't a zip at all is handled gracefully."""
        jar = tmp_path / "notzip.jar"
        jar.write_bytes(b"this is not a zip")
        assert deps._read_jar_manifest(jar, dummy_logger) is None

    def test_missing_file_returns_none(self, tmp_path, dummy_logger):
        """A nonexistent path returns None without raising."""
        assert deps._read_jar_manifest(tmp_path / "nope.jar", dummy_logger) is None

    def test_bad_toml_logs_and_returns_none(self, tmp_path, dummy_logger):
        """A malformed mods.toml is reported and yields None."""
        jar = tmp_path / "bad.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("META-INF/mods.toml", "this is not [valid toml =")
        assert deps._read_jar_manifest(jar, dummy_logger) is None


# ---------------------------------------------------------------------------
# scan_manifests
# ---------------------------------------------------------------------------


class TestScanManifests:
    """Reading every jar referenced by a list of entries."""

    def test_keyed_by_filename(self, tmp_path, dummy_logger):
        """The result dict is keyed by entry['file'], not by mod id."""
        _make_jar(tmp_path / "a.jar", "mod_a")
        _make_jar(tmp_path / "b.jar", "mod_b")
        entries = [_entry("a.jar"), _entry("b.jar")]
        result = deps.scan_manifests(entries, tmp_path, dummy_logger)
        assert set(result.keys()) == {"a.jar", "b.jar"}
        assert result["a.jar"].mod_ids == {"mod_a"}

    def test_skips_entries_without_file(self, tmp_path, dummy_logger):
        """An entry with no 'file' key is silently skipped."""
        _make_jar(tmp_path / "a.jar", "mod_a")
        entries = [_entry("a.jar"), {"id": "x", "file": None}]
        result = deps.scan_manifests(entries, tmp_path, dummy_logger)
        assert set(result.keys()) == {"a.jar"}

    def test_missing_jar_is_not_in_result(self, tmp_path, dummy_logger):
        """An entry pointing at a nonexistent jar is omitted."""
        entries = [_entry("ghost.jar")]
        result = deps.scan_manifests(entries, tmp_path, dummy_logger)
        assert result == {}

    def test_cache_returns_same_object(self, tmp_path, dummy_logger):
        """A second call for the same modpack_dir reuses the cache."""
        _make_jar(tmp_path / "a.jar", "mod_a")
        entries = [_entry("a.jar")]
        first = deps.scan_manifests(entries, tmp_path, dummy_logger)
        second = deps.scan_manifests(entries, tmp_path, dummy_logger)
        assert first is second

    def test_cache_keyed_by_resolved_path(self, tmp_path, dummy_logger):
        """Different modpack_dirs do not share a cache entry."""
        d1 = tmp_path / "p1"
        d2 = tmp_path / "p2"
        _make_jar(d1 / "a.jar", "mod_a")
        _make_jar(d2 / "a.jar", "mod_different")
        r1 = deps.scan_manifests([_entry("a.jar")], d1, dummy_logger)
        r2 = deps.scan_manifests([_entry("a.jar")], d2, dummy_logger)
        assert r1["a.jar"].mod_ids == {"mod_a"}
        assert r2["a.jar"].mod_ids == {"mod_different"}


# ---------------------------------------------------------------------------
# expand_with_required
# ---------------------------------------------------------------------------


class TestExpandWithRequired:
    """The closure walk itself."""

    def test_seed_preserved_when_no_deps(self, tmp_path, dummy_logger):
        """With no dependency edges, the closure equals the seed."""
        _make_jar(tmp_path / "a.jar", "mod_a")
        a = _entry("a.jar", side="both")
        result = deps.expand_with_required(
            all_entries=[a],
            seed_entries=[a],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert result.entries == [a]
        assert result.forced_count == 0
        assert result.forced == []

    def test_index_namespace_dep_is_pulled_in(self, tmp_path, dummy_logger):
        """A dep declared via x-prismlauncher-dependencies is force-included."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(tmp_path / "user.jar", "usermod")
        lib = _entry("lib.jar", side="server", project_id="100")
        user = _entry("user.jar", side="both", index_deps=[("100", "REQUIRED")])

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"lib.jar", "user.jar"}
        assert result.forced_count == 1
        assert result.forced[0][0]["file"] == "user.jar"
        assert result.forced[0][1]["file"] == "lib.jar"
        assert "index addonId=100" in result.forced[0][2]

    def test_jar_namespace_dep_is_pulled_in(self, tmp_path, dummy_logger):
        """A dep declared via mods.toml modId is force-included."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "libmod"}],
        )
        lib = _entry("lib.jar", side="server")
        user = _entry("user.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"lib.jar", "user.jar"}
        assert result.forced_count == 1
        assert "jar modId=libmod" in result.forced[0][2]

    def test_transitive_closure(self, tmp_path, dummy_logger):
        """A -> B -> C pulls both B and C in."""
        _make_jar(tmp_path / "c.jar", "cmod")
        _make_jar(tmp_path / "b.jar", "bmod", deps_spec=[{"modId": "cmod"}])
        _make_jar(tmp_path / "a.jar", "amod", deps_spec=[{"modId": "bmod"}])
        c = _entry("c.jar", side="server")
        b = _entry("b.jar", side="server")
        a = _entry("a.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[a, b, c],
            seed_entries=[a],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"a.jar", "b.jar", "c.jar"}
        assert result.forced_count == 2

    def test_dep_already_in_seed_not_counted_as_forced(self, tmp_path, dummy_logger):
        """A dep already in the seed does not appear in the forced list."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(tmp_path / "user.jar", "usermod", deps_spec=[{"modId": "libmod"}])
        lib = _entry("lib.jar", side="both")
        user = _entry("user.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[lib, user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert result.forced_count == 0
        assert result.forced == []

    def test_dep_missing_from_pack_is_ignored(self, tmp_path, dummy_logger):
        """A declared dep that isn't in the pack is silently skipped."""
        _make_jar(tmp_path / "user.jar", "usermod", deps_spec=[{"modId": "forge"}])
        user = _entry("user.jar", side="both")
        result = deps.expand_with_required(
            all_entries=[user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert result.entries == [user]
        assert result.forced_count == 0

    def test_index_dep_missing_from_pack_is_ignored(self, tmp_path, dummy_logger):
        """An index dep whose project id isn't in the pack is skipped."""
        _make_jar(tmp_path / "user.jar", "usermod")
        user = _entry("user.jar", side="both", index_deps=[("9999", "REQUIRED")])
        result = deps.expand_with_required(
            all_entries=[user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert result.forced_count == 0

    def test_optional_index_dep_is_ignored(self, tmp_path, dummy_logger):
        """Only REQUIRED (not OPTIONAL/EMBEDDED) index edges count."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(tmp_path / "user.jar", "usermod")
        lib = _entry("lib.jar", side="server", project_id="100")
        user = _entry("user.jar", side="both", index_deps=[("100", "OPTIONAL")])

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"user.jar"}

    def test_client_only_jar_dep_not_pulled_for_server(self, tmp_path, dummy_logger):
        """A CLIENT-side jar dep is not force-included on the server side."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "libmod", "side": "CLIENT"}],
        )
        lib = _entry("lib.jar", side="client")
        user = _entry("user.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[user],
            target_side="server",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"user.jar"}

    def test_server_only_jar_dep_not_pulled_for_client(self, tmp_path, dummy_logger):
        """A SERVER-side jar dep is not force-included on the client side."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "libmod", "side": "SERVER"}],
        )
        lib = _entry("lib.jar", side="server")
        user = _entry("user.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[lib, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"user.jar"}

    def test_output_preserves_all_entries_order(self, tmp_path, dummy_logger):
        """The returned list follows all_entries order, not seed order."""
        _make_jar(tmp_path / "a.jar", "amod")
        _make_jar(tmp_path / "b.jar", "bmod")
        _make_jar(tmp_path / "c.jar", "cmod")
        a = _entry("a.jar", side="both")
        b = _entry("b.jar", side="server")
        c = _entry("c.jar", side="both")

        # Seed is [c, a]; all_entries is [a, b, c]; closure should be [a, b, c].
        result = deps.expand_with_required(
            all_entries=[a, b, c],
            seed_entries=[c, a],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        # b is not required by anything, so it should NOT be in the closure.
        assert [e["file"] for e in result.entries] == ["a.jar", "c.jar"]

    def test_invalid_target_side_raises(self, tmp_path, dummy_logger):
        """Only 'client' and 'server' are accepted."""
        with pytest.raises(ValueError):
            deps.expand_with_required(
                all_entries=[],
                seed_entries=[],
                target_side="nonsense",
                modpack_dir=tmp_path,
                logger=dummy_logger,
            )

    def test_side_case_is_normalized(self, tmp_path, dummy_logger):
        """'CLIENT' and 'client' are equivalent."""
        _make_jar(tmp_path / "a.jar", "amod")
        a = _entry("a.jar")
        r1 = deps.expand_with_required(
            all_entries=[a],
            seed_entries=[a],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        r2 = deps.expand_with_required(
            all_entries=[a],
            seed_entries=[a],
            target_side="CLIENT",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert r1.entries == r2.entries

    def test_modid_namespace_collision_first_wins(self, tmp_path, magic_logger):
        """If two jars declare the same modId, the first one wins."""
        _make_jar(tmp_path / "first.jar", "sharedmod")
        _make_jar(tmp_path / "second.jar", "sharedmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "sharedmod"}],
        )
        first = _entry("first.jar", side="server")
        second = _entry("second.jar", side="server")
        user = _entry("user.jar", side="both")

        result = deps.expand_with_required(
            all_entries=[first, second, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=magic_logger,
        )
        assert {e["file"] for e in result.entries} == {"first.jar", "user.jar"}

    def test_both_index_and_jar_edges_are_used(self, tmp_path, dummy_logger):
        """A mod with both an index dep and a jar dep pulls in both."""
        _make_jar(tmp_path / "ilib.jar", "imod")
        _make_jar(tmp_path / "jlib.jar", "jmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "jmod"}],
        )
        ilib = _entry("ilib.jar", side="server", project_id="100")
        jlib = _entry("jlib.jar", side="server")
        user = _entry("user.jar", side="both", index_deps=[("100", "REQUIRED")])

        result = deps.expand_with_required(
            all_entries=[ilib, jlib, user],
            seed_entries=[user],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {
            "ilib.jar",
            "jlib.jar",
            "user.jar",
        }

    def test_cycle_does_not_loop_forever(self, tmp_path, dummy_logger):
        """Two mods that require each other terminate."""
        _make_jar(tmp_path / "a.jar", "amod", deps_spec=[{"modId": "bmod"}])
        _make_jar(tmp_path / "b.jar", "bmod", deps_spec=[{"modId": "amod"}])
        a = _entry("a.jar", side="both")
        b = _entry("b.jar", side="server")

        result = deps.expand_with_required(
            all_entries=[a, b],
            seed_entries=[a],
            target_side="client",
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert {e["file"] for e in result.entries} == {"a.jar", "b.jar"}
        assert result.forced_count == 1


# ---------------------------------------------------------------------------
# ClosureResult
# ---------------------------------------------------------------------------


class TestClosureResult:
    """Tests for the ClosureResult dataclass."""

    def test_forced_count(self):
        """forced_count is the gap between entries and seed_ids."""
        r = deps.ClosureResult(
            entries=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
            seed_ids={"a", "b"},
            forced=[],
        )
        assert r.forced_count == 1

    def test_grouped_collapses_multiple_dependents(self):
        """grouped() returns one key per dependency with all its dependents."""
        dep_a = {"id": "lib", "file": "lib.jar"}
        user1 = {"id": "u1", "file": "u1.jar"}
        user2 = {"id": "u2", "file": "u2.jar"}
        r = deps.ClosureResult(
            entries=[user1, user2, dep_a],
            seed_ids={"u1", "u2"},
            forced=[(user1, dep_a, "jar modId=lib"), (user2, dep_a, "jar modId=lib")],
        )
        grouped = r.grouped()
        assert set(grouped.keys()) == {"lib"}
        entry, dependents = grouped["lib"]
        assert entry is dep_a
        assert {d["id"] for d, _ in dependents} == {"u1", "u2"}

    def test_grouped_empty(self):
        """With no forced entries the grouped dict is empty."""
        r = deps.ClosureResult(entries=[], seed_ids=set(), forced=[])
        assert r.grouped() == {}


# ---------------------------------------------------------------------------
# format_diagnostic
# ---------------------------------------------------------------------------


class TestFormatDiagnostic:
    """Tests for the human-readable closure diagnostic."""

    def test_header_present(self, tmp_path, dummy_logger):
        """The header reports modpack dir and total count."""
        _make_jar(tmp_path / "a.jar", "amod")
        entries = [_entry("a.jar", side="both")]
        out = deps.format_diagnostic(
            all_entries=entries,
            seeds={"client": entries, "server": entries},
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert "=== Dependency closure diagnostic ===" in out
        assert f"Modpack dir:         {tmp_path}" in out
        assert "Total mods in index: 1" in out

    def test_both_sides_present(self, tmp_path, dummy_logger):
        """Both client and server sections appear."""
        _make_jar(tmp_path / "a.jar", "amod")
        entries = [_entry("a.jar", side="both")]
        out = deps.format_diagnostic(
            all_entries=entries,
            seeds={"client": entries, "server": entries},
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert "--- Side: client ---" in out
        assert "--- Side: server ---" in out

    def test_empty_closure_says_nothing_needed(self, tmp_path, dummy_logger):
        """With no forced entries, the 'nothing needed' line appears twice."""
        _make_jar(tmp_path / "a.jar", "amod")
        entries = [_entry("a.jar", side="both")]
        out = deps.format_diagnostic(
            all_entries=entries,
            seeds={"client": entries, "server": entries},
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert out.count("(nothing needed to be force-included)") == 2

    def test_forced_entry_lists_dependent_and_reason(self, tmp_path, dummy_logger):
        """A force-included dep is listed with the dependent and the reason."""
        _make_jar(tmp_path / "lib.jar", "libmod")
        _make_jar(
            tmp_path / "user.jar",
            "usermod",
            deps_spec=[{"modId": "libmod"}],
        )
        lib = _entry("lib.jar", side="server")
        user = _entry("user.jar", side="both")

        out = deps.format_diagnostic(
            all_entries=[lib, user],
            seeds={"client": [user], "server": [user, lib]},
            modpack_dir=tmp_path,
            logger=dummy_logger,
        )
        assert "lib.jar" in out
        assert "declared side='server'" in out
        assert "user.jar" in out
        assert "jar modId=libmod" in out


# ---------------------------------------------------------------------------
# The actual crash-report scenario
# ---------------------------------------------------------------------------


class TestCrashReportScenario:
    """End-to-end reproduction of the September 2026 client-pack crash.

    The crash listed 19 mandatory-dep failures. Seven distinct libraries
    were reported missing. All seven were tagged side='server' in the
    Prism index while their dependents were tagged side='both' or ''.
    The closure must pull all seven back into the client set.
    """

    def _build_tree(self, tmp_path: Path) -> tuple[list[dict], list[dict]]:
        """Build a mini index replicating the seven missing libraries.

        Returns (all_entries, client_seed) where client_seed is what
        filter_prism_entries_by_side would have produced.
        """
        modpack = tmp_path / "downloads"

        # (filename, mod_id, side) for every jar in the mini pack.
        jars = [
            # The seven libraries: declared server, actually needed by client.
            ("ImmersiveEngineering.jar", "immersiveengineering", "server"),
            ("Balm.jar", "balm", "server"),
            ("AnviansLib.jar", "anvianslib", "server"),
            ("CodeChickenLib.jar", "codechickenlib", "server"),
            ("Jade.jar", "jade", "server"),
            ("JEI.jar", "jei", "server"),
            ("Moonlight.jar", "moonlight", "server"),
            # Dependents: declared both, will require the libraries.
            ("EngineeredSchematics.jar", "engineered_schematics", "both"),
            ("CraftingTweaks.jar", "craftingtweaks", "both"),
            ("CreateUnbreakable.jar", "create_unbreakable", "both"),
            ("ProjectRed.jar", "projectred_core", "both"),
            ("CBMultipart.jar", "cb_multipart", "both"),
            ("JadeAddons.jar", "jadeaddons", "both"),
            ("JEP.jar", "justenoughprofessions", "both"),
            ("JER.jar", "jeresources", "both"),
            ("ImmersiveWeathering.jar", "immersive_weathering", "both"),
        ]

        # Every dependent declares its library via its jar manifest.
        manifest_deps = {
            "EngineeredSchematics.jar": ["immersiveengineering"],
            "CraftingTweaks.jar": ["balm"],
            "CreateUnbreakable.jar": ["anvianslib"],
            "ProjectRed.jar": ["codechickenlib"],
            "CBMultipart.jar": ["codechickenlib"],
            "JadeAddons.jar": ["jade"],
            "JEP.jar": ["jei"],
            "JER.jar": ["jei"],
            "ImmersiveWeathering.jar": ["moonlight"],
        }

        for filename, mod_id, _side in jars:
            deps_spec = [{"modId": dep} for dep in manifest_deps.get(filename, [])]
            _make_jar(modpack / filename, mod_id, deps_spec=deps_spec)

        all_entries = [_entry(f, side=s) for f, _, s in jars]
        client_seed = [e for e in all_entries if e["side"] in ("both", "client")]
        return all_entries, client_seed

    def test_all_seven_libraries_are_force_included(self, tmp_path, dummy_logger):
        """Every library from the crash report lands in the client closure."""
        all_entries, client_seed = self._build_tree(tmp_path)

        # Sanity check: the seed has exactly the dependents, no libraries.
        seed_files = {e["file"] for e in client_seed}
        for lib in (
            "ImmersiveEngineering.jar",
            "Balm.jar",
            "AnviansLib.jar",
            "CodeChickenLib.jar",
            "Jade.jar",
            "JEI.jar",
            "Moonlight.jar",
        ):
            assert lib not in seed_files, f"{lib} should not be in the seed"

        result = deps.expand_with_required(
            all_entries=all_entries,
            seed_entries=client_seed,
            target_side="client",
            modpack_dir=tmp_path / "downloads",
            logger=dummy_logger,
        )

        closure_files = {e["file"] for e in result.entries}
        expected_libs = {
            "ImmersiveEngineering.jar",
            "Balm.jar",
            "AnviansLib.jar",
            "CodeChickenLib.jar",
            "Jade.jar",
            "JEI.jar",
            "Moonlight.jar",
        }
        assert expected_libs.issubset(closure_files)
        assert result.forced_count == 7

    def test_every_forced_entry_has_a_reason(self, tmp_path, dummy_logger):
        """Each force-included entry names the dependent that pulled it in."""
        all_entries, client_seed = self._build_tree(tmp_path)
        result = deps.expand_with_required(
            all_entries=all_entries,
            seed_entries=client_seed,
            target_side="client",
            modpack_dir=tmp_path / "downloads",
            logger=dummy_logger,
        )
        assert len(result.forced) == 7
        for dependent, dependency, reason in result.forced:
            assert dependent["file"] != dependency["file"]
            assert reason.startswith("jar modId=")
