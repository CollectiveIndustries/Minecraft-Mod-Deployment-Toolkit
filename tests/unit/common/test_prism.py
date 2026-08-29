# tests/unit/common/test_prism.py
"""Unit tests for Prism .index parsing."""

from src.minecraft.common import prism


def test_parse_prism_toml_valid(tmp_path):
    """Should parse valid .pw.toml correctly."""
    toml_content = """
filename = "testmod.jar"
name = "Test Mod"
side = "server"
[update.curseforge]
project-id = 123
file-id = 456
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result == {
        "id": "123",
        "file": "testmod.jar",
        "side": "server",
        "project_id": 123,
        "file_id": 456,
        "display_name": "Test Mod",
        "source": "curseforge",
    }


def test_parse_prism_toml_missing_curseforge(tmp_path):
    """Should return None if no CurseForge update block."""
    toml_content = """
filename = "modrinth_mod.jar"
[update.modrinth]
mod-id = "xyz"
version = "abc"
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result is None


def test_parse_prism_toml_incomplete_curseforge(tmp_path):
    """Should return None if project-id or file-id missing."""
    toml_content = """
filename = "test.jar"
[update.curseforge]
project-id = 123
# missing file-id
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
[update.curseforge]
project-id = 123
file-id = 456
"""
    toml_file = tmp_path / "test.pw.toml"
    toml_file.write_text(toml_content)
    result = prism.parse_prism_toml(toml_file)
    assert result["side"] == "both"


def test_load_prism_index(tmp_path):
    """Should load all valid .pw.toml files from directory."""
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "mod1.pw.toml").write_text("""
filename = "mod1.jar"
[update.curseforge]
project-id = 1
file-id = 10
""")
    (index_dir / "mod2.pw.toml").write_text("""
filename = "mod2.jar"
[update.curseforge]
project-id = 2
file-id = 20
""")
    # Invalid (no CurseForge)
    (index_dir / "mod3.pw.toml").write_text("""
filename = "mod3.jar"
[update.modrinth]
mod-id = "x"
""")
    entries = prism.load_prism_index(index_dir)
    assert len(entries) == 2
    ids = {e["id"] for e in entries}
    assert ids == {"1", "2"}


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
