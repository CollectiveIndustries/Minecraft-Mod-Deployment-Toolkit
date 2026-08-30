# tests/unit/common/test_prism.py
"""Unit tests for Prism .index parsing (platform‑agnostic)."""

from src.minecraft.common import prism


def test_parse_prism_toml_valid(tmp_path):
    """Should parse a valid .pw.toml with filename and optional side."""
    toml_content = """
filename = "testmod.jar"
name = "Test Mod"
side = "server"
"""
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
    assert result["hash_format"] == "sha512"  # default


def test_parse_prism_toml_missing_filename(tmp_path):
    """Should return None if no 'filename' field is present."""
    toml_content = """
name = "No Filename Mod"
side = "both"
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is None


def test_parse_prism_toml_empty_side(tmp_path):
    """Should default side to 'both' if empty or missing."""
    toml_content = """
filename = "test.jar"
side = ""
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result["side"] == "both"


def test_parse_prism_toml_invalid_side(tmp_path):
    """Should default side to 'both' if invalid value."""
    toml_content = """
filename = "test.jar"
side = "invalid"
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result["side"] == "both"


def test_parse_prism_toml_with_curseforge_block(tmp_path):
    """
    Should accept a .pw.toml with a CurseForge block and extract IDs.
    Also should read download fields if present.
    """
    toml_content = """
filename = "cf_mod.jar"
[update.curseforge]
project-id = 123
file-id = 456
[download]
url = "https://example.com/cf_mod.jar"
hash = "abc123"
hash-format = "sha256"
"""
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
    """
    Should accept a .pw.toml with a Modrinth block and download info.
    """
    toml_content = """
filename = "mr_mod.jar"
[update.modrinth]
mod-id = "xyz"
version = "abc"
[download]
url = "https://cdn.modrinth.com/mr_mod.jar"
hash = "def456"
"""
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
    assert result["hash_format"] == "sha512"  # default if not specified


def test_load_prism_index(tmp_path):
    """Should load all .pw.toml files that have a filename."""
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    # File with filename (accepted)
    (index_dir / "mod1.pw.toml").write_text("""
filename = "mod1.jar"
""")
    # File with filename (accepted)
    (index_dir / "mod2.pw.toml").write_text("""
filename = "mod2.jar"
""")
    # File without filename (rejected)
    (index_dir / "mod3.pw.toml").write_text("""
name = "No Filename"
""")
    entries = prism.load_prism_index(index_dir)
    assert len(entries) == 2
    filenames = {e["file"] for e in entries}
    assert filenames == {"mod1.jar", "mod2.jar"}


def test_filter_prism_entries_by_side():
    entries = [
        {"id": "1", "side": "both"},
        {"id": "2", "side": "client"},
        {"id": "3", "side": "server"},
        {"id": "4", "side": "both"},
    ]
    client = prism.filter_prism_entries_by_side(entries, "client")
    assert {e["id"] for e in client} == {"1", "2", "4"}
    server = prism.filter_prism_entries_by_side(entries, "server")
    assert {e["id"] for e in server} == {"1", "3", "4"}
