# tests/deploy_pack/test_overrides.py

r"""Tests for deploy_pack.overrides, Project_Specs.md §3.11 and §6.1.

§3.11 -- load and apply side_overrides.toml:
  * missing file -> empty overrides, no error
  * valid values: client, server, both, skipped (case-sensitive)
  * any other value, or any non-string, -> ConfigError (exit 3)
  * precedence: by_id > by_filename > deployment_tool_review
  * apply replaces each matching entry's `side` field

§6.1 -- save is a line-based splice, not parse-then-reserialize:
  * file absent -> generated section is the entire file
  * section absent -> appended at EOF, preceded by a blank line
  * section present -> replaced in place
  * every byte outside the review section is preserved
  * line endings match the file's last line; empty file uses \n
  * a `# Last generated:` header comment is written with the
    caller's (or current UTC) timestamp in %Y-%m-%dT%H:%M:%SZ
  * atomic write with metadata preservation (§4.10)

The splice preserves comments, whitespace, and key ordering outside
the review section byte-for-byte.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import minecraft.deploy_pack.overrides as overrides_module
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.overrides import (
    SideOverrides,
    apply_side_overrides,
    load_side_overrides,
    save_side_overrides,
)

# ---------------------------------------------------------------------------
# §3.11: load
# ---------------------------------------------------------------------------


def test_load_missing_file_is_empty_and_does_not_raise(tmp_path: Path) -> None:
    """§3.11: an absent side_overrides.toml is treated as empty, no error."""
    result = load_side_overrides(tmp_path / "nope.toml")
    assert result.is_empty()
    assert result.by_id == {}
    assert result.by_filename == {}
    assert result.deployment_tool_review == {}


def test_load_all_three_sections(tmp_path: Path) -> None:
    """§3.11: the three sections parse independently."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[by_id]\n"231951" = "both"\n\n'
        '[by_filename]\n"applied_kjs-1.0.0.jar" = "both"\n\n'
        '[deployment_tool_review]\n# Last generated: 2026-09-21T14:32:00Z\n"unknown.jar" = "client"\n',
        encoding="utf-8",
    )
    result = load_side_overrides(p)
    assert result.by_id == {"231951": "both"}
    assert result.by_filename == {"applied_kjs-1.0.0.jar": "both"}
    assert result.deployment_tool_review == {"unknown.jar": "client"}


@pytest.mark.parametrize("value", ["client", "server", "both", "skipped"])
def test_load_accepts_each_valid_value(tmp_path: Path, value: str) -> None:
    """§3.11: the four valid values load verbatim."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(f'[by_filename]\n"a.jar" = "{value}"\n', encoding="utf-8")
    assert load_side_overrides(p).by_filename == {"a.jar": value}


@pytest.mark.parametrize("value", ["Client", "CLIENT", "clients", "none", ""])
def test_load_rejects_values_outside_the_valid_set(tmp_path: Path, value: str) -> None:
    """§3.11: values are case-sensitive; anything outside the set is exit 3."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(f'[by_filename]\n"a.jar" = "{value}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_rejects_non_string_value(tmp_path: Path) -> None:
    """§3.11: non-string values are a configuration error."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"1" = 42\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_rejects_malformed_toml(tmp_path: Path) -> None:
    """§3.11: a malformed file is exit 3."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(":::not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_rejects_non_table_section(tmp_path: Path) -> None:
    """§3.11: a known section must be a table, not a scalar."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('by_id = "not a table"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_side_overrides(p)


def test_load_review_section_with_header_comment(tmp_path: Path) -> None:
    """§6.1: the `# Last generated:` comment is not parsed as a value."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"a.jar" = "client"\n',
        encoding="utf-8",
    )
    assert load_side_overrides(p).deployment_tool_review == {"a.jar": "client"}


# ---------------------------------------------------------------------------
# §3.11: lookup precedence
# ---------------------------------------------------------------------------


def test_lookup_by_id_wins_over_by_filename() -> None:
    """§3.11: by_id > by_filename."""
    ov = SideOverrides(by_id={"123": "client"}, by_filename={"foo.jar": "server"})
    assert ov.lookup("123", "foo.jar") == "client"


def test_lookup_by_filename_wins_over_review() -> None:
    """§3.11: by_filename > deployment_tool_review."""
    ov = SideOverrides(by_filename={"foo.jar": "server"}, deployment_tool_review={"foo.jar": "client"})
    assert ov.lookup("", "foo.jar") == "server"


def test_lookup_falls_through_to_review() -> None:
    """§3.11: deployment_tool_review is the last match attempted."""
    ov = SideOverrides(deployment_tool_review={"foo.jar": "client"})
    assert ov.lookup("", "foo.jar") == "client"


def test_lookup_returns_none_when_nothing_matches() -> None:
    """§3.11: no match -> None (entry left untouched)."""
    assert SideOverrides(by_id={"123": "client"}).lookup("999", "other.jar") is None


def test_lookup_empty_id_does_not_match_empty_key() -> None:
    """An empty mod_id must not accidentally hit an empty-string key."""
    assert SideOverrides(by_id={"": "client"}).lookup("", "foo.jar") is None


# ---------------------------------------------------------------------------
# §3.11: apply
# ---------------------------------------------------------------------------


def test_apply_replaces_side_for_matching_entry() -> None:
    """§3.11: apply replaces the `side` field on matching entries and returns the list."""
    entries = [
        {"id": "123", "file": "foo.jar", "side": "both"},
        {"id": "456", "file": "bar.jar", "side": "both"},
    ]
    result = apply_side_overrides(entries, SideOverrides(by_id={"123": "client"}))
    assert result is entries
    assert entries[0]["side"] == "client"
    assert entries[1]["side"] == "both"


def test_apply_by_filename() -> None:
    """§3.11: by_filename matches on the entry's file field."""
    entries = [{"id": "999", "file": "foo.jar", "side": "both"}]
    apply_side_overrides(entries, SideOverrides(by_filename={"foo.jar": "server"}))
    assert entries[0]["side"] == "server"


def test_apply_review_section() -> None:
    """§3.11: deployment_tool_review is applied like the other sections."""
    entries = [{"id": "999", "file": "foo.jar", "side": "both"}]
    apply_side_overrides(entries, SideOverrides(deployment_tool_review={"foo.jar": "skipped"}))
    assert entries[0]["side"] == "skipped"


def test_apply_empty_overrides_is_noop() -> None:
    """§3.11: an empty SideOverrides does not modify entries."""
    entries = [{"id": "123", "file": "foo.jar", "side": "both"}]
    apply_side_overrides(entries, SideOverrides())
    assert entries[0]["side"] == "both"


def test_apply_precedence_by_id_wins() -> None:
    """§3.11: by_id beats by_filename beats review when all three match."""
    entries = [{"id": "123", "file": "foo.jar", "side": "both"}]
    ov = SideOverrides(
        by_id={"123": "client"},
        by_filename={"foo.jar": "server"},
        deployment_tool_review={"foo.jar": "skipped"},
    )
    apply_side_overrides(entries, ov)
    assert entries[0]["side"] == "client"


# ---------------------------------------------------------------------------
# §6.1: save -- file absent
# ---------------------------------------------------------------------------


def test_save_when_file_absent_writes_whole_file(tmp_path: Path) -> None:
    """§6.1: file does not exist -> the generated section is the entire file."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {"foo.jar": "client", "bar.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == ('[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"bar.jar" = "server"\n"foo.jar" = "client"\n')


def test_save_with_no_entries_writes_header_only(tmp_path: Path) -> None:
    """§6.1: an empty review dict produces a section with only the header comment."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == "[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"


# ---------------------------------------------------------------------------
# §6.1: save -- section absent
# ---------------------------------------------------------------------------


def test_save_appends_section_when_absent(tmp_path: Path) -> None:
    """§6.1: section absent -> appended at EOF, preceded by a blank line."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == (
        '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "server"\n'
    )


def test_save_appends_when_no_trailing_newline(tmp_path: Path) -> None:
    """§6.1: a file that lacks a trailing newline gets one before the appended section."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == (
        '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "client"\n'
    )


def test_save_appends_when_file_empty(tmp_path: Path) -> None:
    """§6.1: an empty file becomes the generated section verbatim."""
    p = tmp_path / "side_overrides.toml"
    p.write_text("", encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == ('[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"foo.jar" = "client"\n')


def test_save_appended_section_has_blank_separator(tmp_path: Path) -> None:
    """§6.1: exactly one blank line separates prior content from the appended header."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n', encoding="utf-8")
    save_side_overrides(p, {"foo.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    text = p.read_text(encoding="utf-8")
    assert '[by_id]\n"123" = "client"\n\n[deployment_tool_review]' in text


# ---------------------------------------------------------------------------
# §6.1: save -- section present
# ---------------------------------------------------------------------------


def test_save_replaces_existing_section_in_place(tmp_path: Path) -> None:
    """§6.1: present -> replaced in place; the surrounding sections are untouched."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[by_id]\n"123" = "client"\n\n'
        '[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"old.jar" = "both"\n\n'
        '[by_filename]\n"a.jar" = "server"\n',
        encoding="utf-8",
    )
    save_side_overrides(p, {"new.jar": "skipped"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == (
        '[by_id]\n"123" = "client"\n\n'
        "[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"
        '"new.jar" = "skipped"\n\n'
        '[by_filename]\n"a.jar" = "server"\n'
    )


def test_save_replaces_section_at_eof(tmp_path: Path) -> None:
    """§6.1: a review section at EOF is replaced cleanly."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"old.jar" = "both"\n',
        encoding="utf-8",
    )
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_text(encoding="utf-8") == (
        '[by_id]\n"123" = "client"\n\n[deployment_tool_review]\n# Last generated: 2026-09-23T12:00:00Z\n"new.jar" = "client"\n'
    )


def test_save_rewrites_last_generated_comment(tmp_path: Path) -> None:
    """§6.1: the header comment is rewritten with the caller's timestamp."""
    p = tmp_path / "side_overrides.toml"
    p.write_text(
        '[deployment_tool_review]\n# Last generated: 2020-01-01T00:00:00Z\n"a.jar" = "client"\n',
        encoding="utf-8",
    )
    save_side_overrides(p, {"a.jar": "client"}, timestamp="2030-12-31T23:59:59Z")
    text = p.read_text(encoding="utf-8")
    assert "# Last generated: 2030-12-31T23:59:59Z" in text
    assert "2020-01-01T00:00:00Z" not in text


# ---------------------------------------------------------------------------
# §6.1: byte-for-byte preservation outside the review section
# ---------------------------------------------------------------------------


def test_save_preserves_unrelated_comments_and_spacing(tmp_path: Path) -> None:
    """§6.1: everything outside [deployment_tool_review] is preserved verbatim."""
    original = (
        "# a leading comment\n\n"
        "[by_id]\n"
        "# inline comment inside by_id\n"
        '"231951"    =    "both"   # trailing comment\n\n'
        "[deployment_tool_review]\n"
        "# Last generated: 2020-01-01T00:00:00Z\n"
        '"old.jar" = "both"\n\n'
        "[by_filename]\n"
        "# a comment before the section\n"
        '"applied_kjs-1.0.0.jar" = "both"\n'
    )
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


# ---------------------------------------------------------------------------
# §6.1: line-ending preservation
# ---------------------------------------------------------------------------


def test_save_preserves_crlf_on_append(tmp_path: Path) -> None:
    """§6.1: an appended section uses the file's last-line EOL (CRLF)."""
    p = tmp_path / "side_overrides.toml"
    p.write_bytes(b'[by_id]\r\n"123" = "client"\r\n')
    save_side_overrides(p, {"foo.jar": "server"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_bytes() == (
        b'[by_id]\r\n"123" = "client"\r\n\r\n[deployment_tool_review]\r\n# Last generated: 2026-09-23T12:00:00Z\r\n"foo.jar" = "server"\r\n'
    )


def test_save_preserves_crlf_on_replace(tmp_path: Path) -> None:
    """§6.1: a replaced section also uses the file's last-line EOL."""
    p = tmp_path / "side_overrides.toml"
    p.write_bytes(b'[deployment_tool_review]\r\n# Last generated: 2020-01-01T00:00:00Z\r\n"old.jar" = "both"\r\n')
    save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_bytes() == (b'[deployment_tool_review]\r\n# Last generated: 2026-09-23T12:00:00Z\r\n"new.jar" = "client"\r\n')


# ---------------------------------------------------------------------------
# §6.1: default timestamp
# ---------------------------------------------------------------------------


def test_save_default_timestamp_is_utc_now_in_iso_z_format(tmp_path: Path) -> None:
    """§6.1: with no timestamp supplied, use %Y-%m-%dT%H:%M:%SZ of now(UTC)."""
    p = tmp_path / "side_overrides.toml"
    save_side_overrides(p, {"a.jar": "client"})
    text = p.read_text(encoding="utf-8")
    assert re.search(r"# Last generated: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)


# ---------------------------------------------------------------------------
# §4.10 / §6.1: save writes atomically
# ---------------------------------------------------------------------------


def test_save_replaces_file_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§6.1: a crash mid-save leaves the original file intact (§4.10)."""
    p = tmp_path / "side_overrides.toml"
    p.write_text('[by_id]\n"123" = "client"\n', encoding="utf-8")
    original = p.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    # overrides.py imports atomic_write by name, so patch that binding.
    monkeypatch.setattr(overrides_module, "atomic_write", boom)
    with pytest.raises(OSError):
        save_side_overrides(p, {"new.jar": "client"}, timestamp="2026-09-23T12:00:00Z")
    assert p.read_bytes() == original


def test_matches_returns_true_for_overridden_entry() -> None:
    """An entry matched by any section reports as overridden."""
    ov = SideOverrides(by_id={"123": "client"})
    assert ov.matches({"id": "123", "file": "x.jar"}) is True


def test_matches_returns_true_for_by_filename_override() -> None:
    """A filename match is enough to count as overridden."""
    ov = SideOverrides(by_filename={"x.jar": "server"})
    assert ov.matches({"id": "999", "file": "x.jar"}) is True


def test_matches_returns_true_for_review_override() -> None:
    """A deployment_tool_review entry counts as an override."""
    ov = SideOverrides(deployment_tool_review={"x.jar": "both"})
    assert ov.matches({"id": "999", "file": "x.jar"}) is True


def test_matches_returns_false_for_unoverridden_entry() -> None:
    """An entry with no override in any section returns False."""
    ov = SideOverrides(by_id={"123": "client"})
    assert ov.matches({"id": "999", "file": "x.jar"}) is False


def test_matches_handles_missing_fields() -> None:
    """An entry with no id and no file does not raise."""
    ov = SideOverrides(by_id={"123": "client"})
    assert ov.matches({}) is False


def test_matches_handles_empty_overrides() -> None:
    """An empty SideOverrides reports every entry as unoverridden."""
    assert SideOverrides().matches({"id": "1", "file": "x.jar"}) is False
