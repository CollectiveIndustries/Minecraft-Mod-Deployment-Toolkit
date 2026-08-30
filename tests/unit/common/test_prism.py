# tests/unit/common/test_prism.py

"""Unit tests for Prism .index parsing (platform-agnostic)."""

from src.minecraft.common import prism


def test_parse_prism_toml_valid(tmp_path):
    """Should parse a valid .pw.toml with filename and optional side."""
    toml_content = '\nfilename = "testmod.jar"\nname = "Test Mod"\nside = "server"\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is not None
    assert result["file"] == "testmod.jar"
    assert result["side"] == "server"
    assert result["id"] == "testmod.jar"
    assert result["project_id"] is None
    assert result["file_id"] is None
    assert result["source"] == "unknown"
    assert result["download_url"] is None
    assert result["hash_value"] is None
    assert result["hash_format"] == "sha512"


def test_parse_prism_toml_missing_filename(tmp_path):
    """Should return None if no 'filename' field is present."""
    toml_content = '\nname = "No Filename Mod"\nside = "both"\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is None


def test_parse_prism_toml_empty_side(tmp_path):
    """Should default side to 'both' if empty or missing."""
    toml_content = '\nfilename = "test.jar"\nside = ""\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result["side"] == "both"


def test_parse_prism_toml_invalid_side(tmp_path):
    """Should default side to 'both' if invalid value."""
    toml_content = '\nfilename = "test.jar"\nside = "invalid"\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result["side"] == "both"


def test_parse_prism_toml_with_curseforge_block(tmp_path):
    """Should accept a .pw.toml with a CurseForge block and extract IDs.

    Also should read download fields if present.
    """
    toml_content = '\nfilename = "cf_mod.jar"\n[update.curseforge]\nproject-id = 123\nfile-id = 456\n[download]\nurl = "https://example.com/cf_mod.jar"\nhash = "abc123"\nhash-format = "sha256"\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is not None
    assert result["file"] == "cf_mod.jar"
    assert result["id"] == "123"
    assert result["project_id"] == 123
    assert result["file_id"] == 456
    assert result["source"] == "curseforge"
    assert result["download_url"] == "https://example.com/cf_mod.jar"
    assert result["hash_value"] == "abc123"
    assert result["hash_format"] == "sha256"


def test_parse_prism_toml_with_modrinth_block(tmp_path):
    """Should accept a .pw.toml with a Modrinth block and download info."""
    toml_content = '\nfilename = "mr_mod.jar"\n[update.modrinth]\nmod-id = "xyz"\nversion = "abc"\n[download]\nurl = "https://cdn.modrinth.com/mr_mod.jar"\nhash = "def456"\n'
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is not None
    assert result["file"] == "mr_mod.jar"
    assert result["project_id"] is None
    assert result["file_id"] is None
    assert result["source"] == "modrinth"
    assert result["download_url"] == "https://cdn.modrinth.com/mr_mod.jar"
    assert result["hash_value"] == "def456"
    assert result["hash_format"] == "sha512"


def test_load_prism_index(tmp_path):
    """Should load all .pw.toml files that have a filename."""
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "mod1.pw.toml").write_text('\nfilename = "mod1.jar"\n')
    (index_dir / "mod2.pw.toml").write_text('\nfilename = "mod2.jar"\n')
    (index_dir / "mod3.pw.toml").write_text('\nname = "No Filename"\n')
    entries = prism.load_prism_index(index_dir)
    assert len(entries) == 2
    filenames = {e["file"] for e in entries}
    assert filenames == {"mod1.jar", "mod2.jar"}


def test_filter_prism_entries_by_side():
    """Test filtering PRISM entries by side. Asserts that entries with side 'both' are included in both client and server results, and side-specific entries are filtered correctly."""
    entries = [{"id": "1", "side": "both"}, {"id": "2", "side": "client"}, {"id": "3", "side": "server"}, {"id": "4", "side": "both"}]
    client = prism.filter_prism_entries_by_side(entries, "client")
    assert {e["id"] for e in client} == {"1", "2", "4"}
    server = prism.filter_prism_entries_by_side(entries, "server")
    assert {e["id"] for e in server} == {"1", "3", "4"}
