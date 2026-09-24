# tests/deploy_pack/test_properties.py

"""Tests for deploy_pack.properties, per Project_Specs.md v3.0 §10.1.

Required coverage:
  * existing key replaced
  * missing key appended (with and without trailing newline, LF and CRLF)
  * empty file append uses LF
  * comments preserved
  * duplicate key handling
  * value containing '='
  * non-ASCII bytes
  * leading whitespace tolerated
  * empty value written correctly

Additional coverage:
  * compute_diff does not write
  * idempotency (second apply is a no-op)
  * mixed LF / CRLF preserved line-by-line
  * multiple edits in one call
  * '#' in the middle of a value is not a comment
  * last line without terminator stays without terminator when modified
  * value comparison is by stripped effective value
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.properties import MANAGED_KEYS, PropertiesDiff, PropertyEdit, apply_edits, compute_diff


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _read(path: Path) -> bytes:
    return path.read_bytes()


def test_compute_diff_missing_file_is_error(tmp_path: Path) -> None:
    """Tests that compute_diff raises ConfigError when the target file is missing."""
    with pytest.raises(ConfigError):
        compute_diff(tmp_path / "nope.properties", [])


def test_compute_diff_unchanged_file_empty_diff(tmp_path: Path) -> None:
    """Tests that compute_diff returns an empty diff when no changes are needed."""
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=true\n")
    diff = compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert isinstance(diff, PropertiesDiff)
    assert not diff.any
    assert diff.changed_keys == []


def test_compute_diff_does_not_write(tmp_path: Path) -> None:
    """Tests that compute_diff does not modify the file on disk."""
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=false\n")
    before = p.stat().st_mtime_ns
    compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.stat().st_mtime_ns == before
    assert _read(p) == b"require-resource-pack=false\n"


def test_compute_diff_changed_value(tmp_path: Path) -> None:
    """Tests that compute_diff reports a changed property value.

    Verifies that the returned PropertiesDiff includes the changed key, marks it as present,
    and records the correct before and after values.
    """
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=false\n")
    diff = compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert diff.changed_keys == ["require-resource-pack"]
    assert diff.has("require-resource-pack")
    c = diff.changes[0]
    assert c.before == "false"
    assert c.after == "true"


def test_compute_diff_missing_key_before_is_none(tmp_path: Path) -> None:
    """Tests that computing a diff for a missing key reports the key as changed with a None before value."""
    p = tmp_path / "server.properties"
    _write(p, b"some-other-key=x\n")
    diff = compute_diff(p, [PropertyEdit("resource-pack", "http://example/p.zip")])
    assert diff.changed_keys == ["resource-pack"]
    assert diff.changes[0].before is None


def test_compute_diff_value_matches_after_strip(tmp_path: Path) -> None:
    """Effective value is the stripped bytes after '='."""
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=   true   \n")
    diff = compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert not diff.any


def test_replace_existing_key_simple(tmp_path: Path) -> None:
    """Tests that an existing key is replaced and a non-empty diff is returned."""
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=false\n")
    diff = apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert diff.any
    assert _read(p) == b"require-resource-pack=true\n"


def test_replace_existing_key_preserves_equals_position(tmp_path: Path) -> None:
    """Whitespace before '=' is preserved; everything after '=' is replaced."""
    p = tmp_path / "server.properties"
    _write(p, b"key  =  old\n")
    apply_edits(p, [PropertyEdit("key", "new")])
    assert _read(p) == b"key  =new\n"


def test_replace_existing_key_leading_whitespace_tolerated(tmp_path: Path) -> None:
    """Tests that an existing key with leading whitespace is replaced while preserving the whitespace."""
    p = tmp_path / "server.properties"
    _write(p, b"   require-resource-pack=true\n")
    apply_edits(p, [PropertyEdit("require-resource-pack", "false")])
    assert _read(p) == b"   require-resource-pack=false\n"


def test_append_missing_key_with_trailing_newline_lf(tmp_path: Path) -> None:
    """Tests that a missing key is appended without adding an extra newline when the file ends with an LF."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\n")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert _read(p) == b"a=1\nb=2\n"


def test_append_missing_key_without_trailing_newline(tmp_path: Path) -> None:
    """Tests that a missing key is appended with a newline when the file lacks a trailing newline."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert _read(p) == b"a=1\nb=2\n"


def test_append_missing_key_with_crlf_file(tmp_path: Path) -> None:
    """Tests that applying an edit appends a missing key using CRLF line endings."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\r\n")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert _read(p) == b"a=1\r\nb=2\r\n"


def test_append_missing_key_without_trailing_newline_crlf_file(tmp_path: Path) -> None:
    """Tests appending a missing key to a CRLF file without a trailing newline."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\r\nb=2")
    apply_edits(p, [PropertyEdit("c", "3")])
    assert _read(p) == b"a=1\r\nb=2\r\nc=3\r\n"


def test_append_empty_file_uses_lf(tmp_path: Path) -> None:
    """Tests that appending to an empty file uses LF line endings."""
    p = tmp_path / "server.properties"
    _write(p, b"")
    apply_edits(p, [PropertyEdit("a", "1")])
    assert _read(p) == b"a=1\n"


def test_append_to_file_that_is_only_a_newline(tmp_path: Path) -> None:
    """Tests appending a new property to a file containing only a newline."""
    p = tmp_path / "server.properties"
    _write(p, b"\n")
    apply_edits(p, [PropertyEdit("a", "1")])
    assert _read(p) == b"\na=1\n"


def test_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    """Tests that editing a property preserves comments and blank lines in the file."""
    original = b"# leading comment\n\na=1\n# a comment between keys\nrequire-resource-pack=false\n\nb=2\n"
    p = tmp_path / "server.properties"
    _write(p, original)
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert _read(p) == b"# leading comment\n\na=1\n# a comment between keys\nrequire-resource-pack=true\n\nb=2\n"


def test_comment_line_containing_managed_key_is_untouched(tmp_path: Path) -> None:
    """A comment line '# require-resource-pack=true' is not the key."""
    p = tmp_path / "server.properties"
    _write(p, b"# require-resource-pack=true\nrequire-resource-pack=false\n")
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert _read(p) == b"# require-resource-pack=true\nrequire-resource-pack=true\n"


def test_key_ordering_preserved(tmp_path: Path) -> None:
    """Tests that editing a property preserves the original ordering of keys."""
    p = tmp_path / "server.properties"
    _write(p, b"c=3\na=1\nb=2\n")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert _read(p) == b"c=3\na=1\nb=20\n"


def test_hash_inside_value_is_not_a_comment(tmp_path: Path) -> None:
    """Tests that a hash character inside a property value is not treated as a comment.

    Verifies that a value containing '#frag' is parsed as part of the value,
    so an identical desired value results in no diff.
    """
    p = tmp_path / "server.properties"
    _write(p, b"resource-pack=http://x/p.zip#frag\n")
    diff = compute_diff(p, [PropertyEdit("resource-pack", "http://x/p.zip#frag")])
    assert not diff.any


def test_value_containing_equals(tmp_path: Path) -> None:
    """Tests that a property value containing an equals sign produces no diff.

    Verifies that when the current and desired values are identical and contain an
    equals sign, compute_diff reports no changes.
    """
    p = tmp_path / "server.properties"
    _write(p, b"k=a=b\n")
    diff = compute_diff(p, [PropertyEdit("k", "a=b")])
    assert not diff.any


def test_value_containing_equals_replaced(tmp_path: Path) -> None:
    """Tests that a property value containing an equals sign is correctly replaced.

    Verifies that applying a PropertyEdit whose new value contains an equals sign
    updates the file content as expected, without misinterpreting the value.
    """
    p = tmp_path / "server.properties"
    _write(p, b"k=a=b\n")
    apply_edits(p, [PropertyEdit("k", "x=y")])
    assert _read(p) == b"k=x=y\n"


def test_empty_value_written_correctly(tmp_path: Path) -> None:
    """Tests that an empty value is written correctly for an existing property."""
    p = tmp_path / "server.properties"
    _write(p, b"resource-pack-prompt=old\n")
    apply_edits(p, [PropertyEdit("resource-pack-prompt", "")])
    assert _read(p) == b"resource-pack-prompt=\n"


def test_empty_value_from_absent(tmp_path: Path) -> None:
    """Tests that an empty value is added as a new property when the key is absent."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\n")
    apply_edits(p, [PropertyEdit("resource-pack-prompt", "")])
    assert _read(p) == b"a=1\nresource-pack-prompt=\n"


def test_empty_value_unchanged_is_noop(tmp_path: Path) -> None:
    """Tests that applying an edit that sets an empty value to empty is a no-op."""
    p = tmp_path / "server.properties"
    _write(p, b"resource-pack-prompt=\n")
    diff = apply_edits(p, [PropertyEdit("resource-pack-prompt", "")])
    assert not diff.any
    assert _read(p) == b"resource-pack-prompt=\n"


def test_non_ascii_bytes_in_unrelated_line_preserved(tmp_path: Path) -> None:
    """Tests that non-ASCII bytes in an unrelated line are preserved when editing another property."""
    original = "# café\nrequire-resource-pack=false\n".encode()
    p = tmp_path / "server.properties"
    _write(p, original)
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert _read(p) == "# café\nrequire-resource-pack=true\n".encode()


def test_non_ascii_bytes_in_arbitrary_positions_preserved(tmp_path: Path) -> None:
    """Tests that non-ASCII bytes in arbitrary positions outside the edited property line are preserved."""
    original = b"# \xa7 \xe9\nrequire-resource-pack=false\n# \xff\n"
    p = tmp_path / "server.properties"
    _write(p, original)
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert _read(p) == b"# \xa7 \xe9\nrequire-resource-pack=true\n# \xff\n"


def test_non_ascii_in_managed_value_utf8(tmp_path: Path) -> None:
    """Tests that a managed property value containing non-ASCII UTF-8 characters produces no diff when unchanged."""
    p = tmp_path / "server.properties"
    _write(p, "resource-pack-prompt=café\n".encode())
    diff = compute_diff(p, [PropertyEdit("resource-pack-prompt", "café")])
    assert not diff.any


def test_per_line_line_endings_preserved(tmp_path: Path) -> None:
    """CRLF on one line, LF on another: each is preserved as-is."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\r\nb=2\nc=3\r\n")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert _read(p) == b"a=1\r\nb=20\nc=3\r\n"


def test_modified_last_line_without_terminator(tmp_path: Path) -> None:
    """Modifying the last line must not invent a trailing newline."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\nb=2")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert _read(p) == b"a=1\nb=20"


def test_modified_last_line_no_terminator_with_append(tmp_path: Path) -> None:
    """Modify the last line AND append: append adds the newline."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\nb=2")
    apply_edits(p, [PropertyEdit("b", "20"), PropertyEdit("c", "3")])
    assert _read(p) == b"a=1\nb=20\nc=3\n"


def test_duplicate_keys_last_replaced_earlier_untouched(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Tests that for duplicate keys, only the last occurrence is replaced while earlier ones remain untouched."""
    p = tmp_path / "server.properties"
    _write(p, b"require-resource-pack=false\n# in between\nrequire-resource-pack=false\n")

    with caplog.at_level(logging.WARNING):
        diff = apply_edits(p, [PropertyEdit("require-resource-pack", "true")], logger=logging.getLogger("test"))
    assert diff.any
    assert _read(p) == b"require-resource-pack=false\n# in between\nrequire-resource-pack=true\n"


def test_duplicate_keys_effective_value_is_last(tmp_path: Path) -> None:
    """If the last occurrence already matches, no change is needed."""
    p = tmp_path / "server.properties"
    _write(p, b"k=false\nk=true\n")
    diff = compute_diff(p, [PropertyEdit("k", "true")])
    assert not diff.any


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """Tests that applying the same edits twice is idempotent."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\n")
    first = apply_edits(p, [PropertyEdit("b", "2")])
    assert first.any
    before = p.stat().st_mtime_ns
    second = apply_edits(p, [PropertyEdit("b", "2")])
    assert not second.any
    assert p.stat().st_mtime_ns == before


def test_multiple_edits_in_one_call(tmp_path: Path) -> None:
    """Tests that multiple property edits are applied correctly in a single call, updating existing keys, adding new ones, and reporting changed keys in the diff."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\nresource-pack=old\n")
    diff = apply_edits(
        p, [PropertyEdit("resource-pack", "http://x/p.zip"), PropertyEdit("require-resource-pack", "true"), PropertyEdit("resource-pack-sha1", "abcdef0123")]
    )
    assert set(diff.changed_keys) == {"resource-pack", "require-resource-pack", "resource-pack-sha1"}
    assert _read(p) == b"a=1\nresource-pack=http://x/p.zip\nrequire-resource-pack=true\nresource-pack-sha1=abcdef0123\n"


def test_all_four_managed_keys_smoke(tmp_path: Path) -> None:
    """The four keys this module is specified to manage, end-to-end."""
    assert frozenset({"require-resource-pack", "resource-pack", "resource-pack-prompt", "resource-pack-sha1"}) == MANAGED_KEYS
    p = tmp_path / "server.properties"
    _write(p, b"")
    apply_edits(
        p,
        [
            PropertyEdit("require-resource-pack", "true"),
            PropertyEdit("resource-pack", "http://example/pack.zip"),
            PropertyEdit("resource-pack-prompt", "Please accept"),
            PropertyEdit("resource-pack-sha1", "0123456789abcdef"),
        ],
    )
    assert (
        _read(p)
        == b"require-resource-pack=true\nresource-pack=http://example/pack.zip\nresource-pack-prompt=Please accept\nresource-pack-sha1=0123456789abcdef\n"
    )


def test_duplicate_edit_keys_last_wins(tmp_path: Path) -> None:
    """Tests that when multiple edits target the same key, the last edit's value is applied."""
    p = tmp_path / "server.properties"
    _write(p, b"")
    apply_edits(p, [PropertyEdit("k", "first"), PropertyEdit("k", "second")])
    assert _read(p) == b"k=second\n"


def test_empty_edit_list_is_noop(tmp_path: Path) -> None:
    """Tests that applying an empty edit list does not modify the file or its modification time."""
    p = tmp_path / "server.properties"
    _write(p, b"a=1\n")
    before = p.stat().st_mtime_ns
    diff = apply_edits(p, [])
    assert not diff.any
    assert p.stat().st_mtime_ns == before
