# tests/modpack_upgrade/test_pw_toml.py

"""Tests for deterministic ``.pw.toml`` rendering and parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack import deps
from minecraft.modpack_upgrade import pw_toml
from minecraft.modpack_upgrade.errors import SchemaError


def _external_entry() -> pw_toml.PwTomlEntry:
    return pw_toml.PwTomlEntry(
        filename="create-1.20.1-6.0.8.jar",
        side="both",
        name="Create",
        download_url="https://cdn.modrinth.com/data/abc/versions/123/create-1.20.1-6.0.8.jar",
        download_hash_format="sha512",
        download_hash="a" * 128,
        modrinth_mod_id="create",
        modrinth_version="abc123",
        dependencies=(pw_toml.Dependency(addon_id="flywheel", type="required"),),
    )


def test_external_round_trips_through_real_parser(tmp_path: Path):
    """Tests that an external provider entry round-trips through the real TOML parser.

    Writes an external PwToml entry to disk, verifies the returned digest has the
    expected SHA-256 length, then parses the file with deps.parse_prism_toml and
    asserts all fields, including nested dependencies, are preserved.
    """
    entry = _external_entry()
    path = tmp_path / "create.pw.toml"
    digest = pw_toml.write_pw_toml(entry, path)
    assert len(digest) == 64

    parsed = deps.parse_prism_toml(path)
    assert parsed is not None
    assert parsed["file"] == entry.filename
    assert parsed["side"] == "both"
    assert parsed["side_raw"] == "both"
    assert parsed["source"] == "modrinth"
    assert parsed["project_id"] == "create"
    assert parsed["file_id"] == "abc123"
    assert parsed["dependencies"] == [{"addon_id": "flywheel", "type": "REQUIRED"}]


def test_in_house_entry_emits_no_provider_block(tmp_path: Path):
    """Tests that an in-house entry is written without any provider block.

    Writes a provider-less PwToml entry to disk and asserts that parsing it yields
    an unknown source with no project or file identifiers.
    """
    entry = pw_toml.PwTomlEntry(
        filename="custom-mod-1.0.0.jar",
        side="both",
        name="Custom Mod",
    )
    path = tmp_path / "custom.pw.toml"
    pw_toml.write_pw_toml(entry, path)
    parsed = deps.parse_prism_toml(path)
    assert parsed is not None
    assert parsed["source"] == "unknown"
    assert parsed["project_id"] is None
    assert parsed["file_id"] is None


def test_unknown_side_sentinel_is_written_and_detected(tmp_path: Path):
    """Tests that an unknown side sentinel is written to and detected in a pw.toml file.

    Verifies that writing an entry with side 'unknown' and parsing it back coerces the
    side to 'both' while preserving 'unknown' in side_raw, and that the entry is
    identified as unmarked.
    """
    entry = pw_toml.PwTomlEntry(
        filename="mystery.jar",
        side="unknown",
        name="Mystery",
    )
    path = tmp_path / "mystery.pw.toml"
    pw_toml.write_pw_toml(entry, path)
    parsed = deps.parse_prism_toml(path)
    assert parsed is not None
    # Parser coerces to 'both' but preserves 'unknown' in side_raw
    assert parsed["side"] == "both"
    assert parsed["side_raw"] == "unknown"
    assert deps.is_unmarked(parsed) is True


def test_two_provider_blocks_are_rejected():
    """Tests that specifying both Modrinth and CurseForge provider blocks is rejected.

    Builds an entry containing both provider identifiers and asserts that
    rendering the PwToml raises SchemaError.
    """
    entry = pw_toml.PwTomlEntry(
        filename="x.jar",
        side="both",
        modrinth_mod_id="m",
        modrinth_version="v",
        curseforge_project_id=1,
        curseforge_file_id=2,
    )
    with pytest.raises(SchemaError):
        pw_toml.render_pw_toml(entry)


def test_partial_download_block_is_rejected():
    """Tests that rendering a partially specified download block raises a SchemaError."""
    entry = pw_toml.PwTomlEntry(
        filename="x.jar",
        side="both",
        download_url="https://example.com/x.jar",
    )
    with pytest.raises(SchemaError):
        pw_toml.render_pw_toml(entry)


def test_partial_modrinth_block_is_rejected():
    """Tests that rendering a partially specified Modrinth block raises a SchemaError."""
    entry = pw_toml.PwTomlEntry(
        filename="x.jar",
        side="both",
        modrinth_mod_id="m",
    )
    with pytest.raises(SchemaError):
        pw_toml.render_pw_toml(entry)


def test_path_separator_in_filename_is_rejected():
    """Tests that filenames containing path separators are rejected with a SchemaError."""
    entry = pw_toml.PwTomlEntry(filename="sub/x.jar", side="both")
    with pytest.raises(SchemaError):
        pw_toml.render_pw_toml(entry)


def test_output_is_byte_stable():
    """Tests that rendering the same entry twice produces byte-identical output."""
    entry = _external_entry()
    assert pw_toml.render_pw_toml(entry) == pw_toml.render_pw_toml(entry)
