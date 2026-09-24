# tests/deploy_pack/test_overrides.py

"""Tests for deploy_pack.overrides, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * load: missing file → empty (no error)
  * load: valid sections, values
  * load: invalid value → ConfigError (exit 3)
  * load: case-sensitive value matching ("Client" invalid)
  * load: non-string value → ConfigError
  * load: malformed TOML → ConfigError
  * load: review section with "Last generated" comment parses cleanly
  * lookup precedence: by_id > by_filename > deployment_tool_review
  * apply: entries get 'side' replaced; non-matching entries untouched
  * save: file absent (whole-file generation)
  * save: section absent (append at EOF)
  * save: section present (replace in place)
  * save: line ending preservation (LF and CRLF)
  * save: blank-line separator preserved when replacing
  * save: "Last generated" comment authored with the given timestamp

Note: the four ``atomic_write`` tests that previously lived here moved to
``test_files.py`` in M7 (see §4.10 responsibility split).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.overrides import SideOverrides, apply_side_overrides, load_side_overrides, save_side_overrides


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    """Tests that loading a missing side-overrides file returns an empty result."""
    result = load_side_overrides(tmp_path / "nope.toml")
    assert result.is_empty()
    assert result.by_id == {}
    assert result.by_filename == {}
    assert result.deployment_tool_review == {}


def test_load_valid_sections(tmp_path: Path) -> None:
    """Tests that loading a valid side-overrides TOML file returns the expected sections."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '\n[by_id]\n"231951" = "both"\n\n[by_filename]\n"applied_kjs-1.0.0.jar" = "both"\n\n[deployment_tool_review]\n# Last generated: 2026-09-21T14:32:00Z\n"unknown.jar" = "client"\n',
        encoding="utf-8",
    )
    result = load_side_overrides(p)
    assert result.by_id == {"231951": "both"}
    assert result.by_filename == {"applied_kjs-1.0.0.jar": "both"}
    assert result.deployment_tool_review == {"unknown.jar": "client"}


def test_load_invalid_value_is_config_error(tmp_path: Path) -> None:
    """Tests that loading side overrides with an invalid value raises ConfigError."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"1" = "clients"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_case_sensitive_value(tmp_path: Path) -> None:
    """'Client' is not 'client' (§3.11)."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"1" = "Client"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_non_string_value_is_error(tmp_path: Path) -> None:
    """Tests that loading side overrides with a non-string value raises ConfigError."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"1" = 42\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_malformed_toml_is_error(tmp_path: Path) -> None:
    """Tests that malformed TOML content raises a ConfigError."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(":::not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_non_table_section_is_error(tmp_path: Path) -> None:
    """Tests that a non-table section in the TOML file raises a ConfigError."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('by_id = "not a table"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_all_valid_values(tmp_path: Path) -> None:
    """Tests loading side overrides from a TOML file containing all valid value types."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('\n[by_filename]\n"a.jar" = "client"\n"b.jar" = "server"\n"c.jar" = "both"\n"d.jar" = "skipped"\n', encoding="utf-8")
    result = load_side_overrides(p)
    assert result.by_filename == {"a.jar": "client", "b.jar": "server", "c.jar": "both", "d.jar": "skipped"}


def test_lookup_by_id_wins_over_by_filename() -> None:
    """Tests that an ID-based lookup takes precedence over a filename-based lookup."""
    ov = SideOverrides(by_id={"123": "client"}, by_filename={"foo.jar": "server"})
    assert ov.lookup("123", "foo.jar") == "client"


def test_lookup_by_filename_wins_over_review() -> None:
    """Tests that a filename-based lookup takes precedence over the deployment tool review mapping."""
    ov = SideOverrides(by_filename={"foo.jar": "server"}, deployment_tool_review={"foo.jar": "client"})
    assert ov.lookup("", "foo.jar") == "server"


def test_lookup_falls_through_to_review() -> None:
    """Tests that lookup falls back to the deployment tool review mapping when no other override matches."""
    ov = SideOverrides(deployment_tool_review={"foo.jar": "client"})
    assert ov.lookup("", "foo.jar") == "client"


def test_lookup_nothing_matches() -> None:
    """Tests that lookup returns None when no override matches the given id or filename."""
    ov = SideOverrides(by_id={"123": "client"})
    assert ov.lookup("999", "other.jar") is None


def test_lookup_empty_id_does_not_match() -> None:
    """An empty mod_id must not match an empty-string key by accident."""
    ov = SideOverrides(by_id={"": "client"})
    assert ov.lookup("", "foo.jar") is None


def test_apply_replaces_side() -> None:
    """Tests that apply_side_overrides mutates the input list in place and only replaces sides for entries with matching overrides."""
    entries = [{"id": "123", "file": "foo.jar", "side": "both"}, {"id": "456", "file": "bar.jar", "side": "both"}]
    ov = SideOverrides(by_id={"123": "client"})
    result = apply_side_overrides(entries, ov)
    assert result is entries
    assert entries[0]["side"] == "client"
    assert entries[1]["side"] == "both"


def test_apply_by_filename() -> None:
    """Tests that a by-filename override sets the matching entry's side field."""
    entries = [{"id": "999", "file": "foo.jar", "side": "both"}]
    ov = SideOverrides(by_filename={"foo.jar": "server"})
    apply_side_overrides(entries, ov)
    assert entries[0]["side"] == "server"


def test_apply_review_section() -> None:
    """Tests that a deployment tool review override sets the entry's side field."""
    entries = [{"id": "999", "file": "foo.jar", "side": "both"}]
    ov = SideOverrides(deployment_tool_review={"foo.jar": "skipped"})
    apply_side_overrides(entries, ov)
    assert entries[0]["side"] == "skipped"


def test_apply_empty_overrides_is_noop() -> None:
    """Tests that applying empty overrides leaves entries unchanged."""
    entries = [{"id": "123", "file": "foo.jar", "side": "both"}]
    result = apply_side_overrides(entries, SideOverrides())
    assert result[0]["side"] == "both"


def test_apply_precedence_by_id_wins() -> None:
    """Tests that by_id overrides take precedence over other override sources."""
    entries = [{"id": "123", "file": "foo.jar", "side": "both"}]
    ov = SideOverrides(by_id={"123": "client"}, by_filename={"foo.jar": "server"}, deployment_tool_review={"foo.jar": "skipped"})
    apply_side_overrides(entries, ov)
    assert entries[0]["side"] == "client"


def test_save_file_absent_writes_whole_file(tmp_path: Path) -> None:
    """Tests that saving when the file is absent writes the entire file with sorted entries."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {"foo.jar": "client", "bar.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    content = p.read_text(encoding="utf-8")
    assert content == '[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"bar.jar" = "server"\n"foo.jar" = "client"\n'


def test_save_file_absent_with_no_entries(tmp_path: Path) -> None:
    """Tests that saving with no entries creates a file containing only the section header and timestamp."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == "[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"


def test_save_appends_section_when_absent(tmp_path: Path) -> None:
    """Tests that saving appends a new section when it is absent from an existing file."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8") == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "server"\n'
    )


def test_save_appends_when_no_trailing_newline(tmp_path: Path) -> None:
    """Tests that side overrides are appended with blank-line separation when the file lacks a trailing newline."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8") == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "client"\n'
    )


def test_save_appends_when_trailing_blank_already_present(tmp_path: Path) -> None:
    """Tests that side overrides are appended with a single blank separator when the file already ends with a trailing blank line."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n\n', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8") == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "client"\n'
    )


def test_save_appends_when_file_empty(tmp_path: Path) -> None:
    """Tests that side overrides are appended to an empty file with a header and timestamp."""
    p = tmp_path / "side_overrides.toml"
    p.write_text("", encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == '[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "client"\n'


def test_save_replaces_existing_section(tmp_path: Path) -> None:
    """Tests that save_side_overrides replaces an existing deployment tool review section while preserving surrounding sections."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"old.jar" = "both"\n\n[by_filename]\n"a.jar" = "server"\n',
        encoding="utf-8",
    )
    save_side_overrides(p, {"new.jar": "skipped"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8")
        == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"new.jar" = "skipped"\n\n[by_filename]\n"a.jar" = "server"\n'
    )


def test_save_replaces_section_at_eof(tmp_path: Path) -> None:
    """Tests that save_side_overrides replaces the deployment tool review section when it is the last section in the file."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"old.jar" = "both"\n', encoding="utf-8")
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8") == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"new.jar" = "client"\n'
    )


def test_save_replaces_section_without_trailing_blank(tmp_path: Path) -> None:
    """Tests that replacing a section does not add a trailing blank line."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n"old.jar" = "both"\n[by_filename]\n"a.jar" = "server"\n', encoding="utf-8")
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_text(encoding="utf-8")
        == '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"new.jar" = "client"\n[by_filename]\n"a.jar" = "server"\n'
    )


def test_save_preserves_crlf_appending(tmp_path: Path) -> None:
    """Tests that saving preserves CRLF line endings when appending a section."""
    p = tmp_path / "side_overrides.toml"
    p.write_bytes(b'[by_id]\r\n"123" = "client"\r\n')
    save_side_overrides(p, {"foo.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    assert (
        p.read_bytes() == b'[by_id]\r\n"123" = "client"\r\n\r\n[deployment_tool_review]\r\n# Last generated: 2026-09-23T12:00:00Z\r\n"foo.jar" = "server"\r\n'
    )


def test_save_preserves_crlf_replacing(tmp_path: Path) -> None:
    """Tests that saving preserves CRLF line endings when replacing a section."""
    p = tmp_path / "side_overrides.toml"
    p.write_bytes(b'[deployment_tool_review]\r\n# Last generated: 2020-01-01T00:00:00Z\r\n"old.jar" = "both"\r\n')
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_bytes() == b'[deployment_tool_review]\r\n# Last generated: 2026-09-23T12:00:00Z\r\n"new.jar" = "client"\r\n'


def test_save_rewrites_last_generated_comment(tmp_path: Path) -> None:
    """Tests that saving rewrites the last generated timestamp comment."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"a.jar" = "client"\n', encoding="utf-8")
    save_side_overrides(p, {"a.jar": "client"}, timestamp="2030-12-31T23:59:59Z")
    content = p.read_text(encoding="utf-8")
    assert "# Last generated: 2030-12-31T23:59:59Z" in content
    assert "2020-01-01T00:00:00Z" not in content


def test_save_default_timestamp_is_utc_now(tmp_path: Path) -> None:
    """Verifies that saved side overrides include a default UTC timestamp in the expected format."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {"a.jar": "client"})
    content = p.read_text(encoding="utf-8")
    import re as _re

    m = _re.search("# Last generated: (\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z)", content)
    assert m is not None


def test_save_preserves_unrelated_comments_and_spacing(tmp_path: Path) -> None:
    """Everything outside [deployment_tool_review] is preserved verbatim (§6.1)."""
    original = '# a leading comment\n\n[by_id]\n# inline comment inside by_id\n"231951"    =    "both"   # trailing comment\n\n[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"old.jar" = "both"\n\n[by_filename]\n# a comment before the section\n"applied_kjs-1.0.0.jar" = "both"\n'
    p = tmp_path / "side_overrides.toml"
    p.write_text(original, encoding="utf-8")
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    result = p.read_text(encoding="utf-8")
    assert "# a leading comment" in result
    assert "# inline comment inside by_id" in result
    assert '"231951"    =    "both"   # trailing comment' in result
    assert "# a comment before the section" in result
    assert '"applied_kjs-1.0.0.jar" = "both"' in result
    assert '"old.jar"' not in result
    assert '"new.jar" = "client"' in result
