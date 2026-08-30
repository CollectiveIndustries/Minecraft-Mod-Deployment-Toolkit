# tests/unit/common/test_overrieds.py
"""Unit tests for side overrides module."""

from src.minecraft.common import overrides


def test_load_side_overrides_file_missing(temp_dir):
    """Should return empty dict if file missing."""
    result = overrides.load_side_overrides(temp_dir / "missing.toml")
    assert result == {}


def test_load_side_overrides_invalid_toml(temp_dir):
    """Should return empty dict on TOML parse error."""
    bad_file = temp_dir / "bad.toml"
    bad_file.write_text("this is not toml [")
    result = overrides.load_side_overrides(bad_file)
    assert result == {}


def test_load_side_overrides_valid(temp_dir):
    """Should load overrides from both sections."""
    toml_content = """
[by_id]
"123" = "client"
"456" = "server"

[by_filename]
"mod.jar" = "both"
"""
    override_file = temp_dir / "side_overrides.toml"
    override_file.write_text(toml_content)
    result = overrides.load_side_overrides(override_file)
    assert result == {
        "123": "client",
        "456": "server",
        "mod.jar": "both",
    }


def test_load_side_overrides_ignores_invalid_values(temp_dir):
    """Should ignore overrides with invalid side values."""
    toml_content = """
[by_id]
"123" = "client"
"456" = "invalid"
"""
    override_file = temp_dir / "side_overrides.toml"
    override_file.write_text(toml_content)
    result = overrides.load_side_overrides(override_file)
    assert result == {"123": "client"}


def test_apply_side_overrides(temp_dir):
    """Should apply overrides to entries."""
    entries = [
        {"id": "123", "file": "mod1.jar", "side": "both"},
        {"id": "456", "file": "mod2.jar", "side": "server"},
        {"id": "789", "file": "mod3.jar", "side": "client"},
    ]
    overrides_data = {
        "123": "client",
        "mod2.jar": "both",
    }
    result = overrides.apply_side_overrides(entries, overrides_data)
    # Check that entries are modified in-place
    assert result[0]["side"] == "client"
    assert result[1]["side"] == "both"
    assert result[2]["side"] == "client"
    # Ensure dict is returned
    assert result is entries
