# tests/deploy_pack/test_properties.py

r"""Tests for deploy_pack.properties, Project_Specs.md §7.4.

Every test in this file traces to a specific clause in §7.4:

  * binary read/write with per-line EOL preservation
  * match regex: ^\s*<key>\s*=, replace from the first = to EOL
  * preserve comments, blank lines, key ordering, unrelated keys
  * append missing keys with the file's last-line EOL style
  * empty file appends with \n
  * duplicate keys: last occurrence authoritative, warn once per key
  * no-op when the effective value equals the target (§4.4)
  * missing file -> ConfigError (exit 3)

The four managed keys (§7.4) are
require-resource-pack, resource-pack, resource-pack-prompt,
resource-pack-sha1.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.properties import (
    MANAGED_KEYS,
    PropertiesDiff,
    PropertyChange,
    PropertyEdit,
    apply_edits,
    compute_diff,
)

# ---------------------------------------------------------------------------
# §7.4: the four managed keys
# ---------------------------------------------------------------------------


def test_managed_keys_are_exactly_the_four_from_the_spec() -> None:
    """§7.4 names four managed keys and no others."""
    assert (
        frozenset(
            {
                "require-resource-pack",
                "resource-pack",
                "resource-pack-prompt",
                "resource-pack-sha1",
            }
        )
        == MANAGED_KEYS
    )


# ---------------------------------------------------------------------------
# §7.4: missing file -> ConfigError (exit 3)
# ---------------------------------------------------------------------------


def test_compute_diff_missing_file_raises_config_error(tmp_path: Path) -> None:
    """§7.4: a missing server.properties is a preflight error, exit 3."""
    with pytest.raises(ConfigError):
        compute_diff(tmp_path / "nope.properties", [])


def test_apply_edits_missing_file_raises_config_error(tmp_path: Path) -> None:
    """§7.4: apply_edits on a missing file raises ConfigError before any write."""
    with pytest.raises(ConfigError):
        apply_edits(tmp_path / "nope.properties", [PropertyEdit("k", "v")])


# ---------------------------------------------------------------------------
# §4.4: "changed" means target value != effective value
# ---------------------------------------------------------------------------


def test_compute_diff_unchanged_file_returns_empty_diff(tmp_path: Path) -> None:
    """§4.4: a property already matching the target is not a change."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=true\n")
    diff = compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert isinstance(diff, PropertiesDiff)
    assert not diff.any
    assert diff.changed_keys == []


def test_compute_diff_is_read_only(tmp_path: Path) -> None:
    """compute_diff never writes; mtime is unchanged."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=false\n")
    before = p.stat().st_mtime_ns
    compute_diff(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.stat().st_mtime_ns == before
    assert p.read_bytes() == b"require-resource-pack=false\n"


def test_apply_edits_no_effective_change_is_a_noop(tmp_path: Path) -> None:
    """§4.4: no effective change -> the file is not rewritten."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\n")
    before = p.stat().st_mtime_ns
    diff = apply_edits(p, [PropertyEdit("a", "1")])
    assert not diff.any
    assert p.stat().st_mtime_ns == before


def test_apply_edits_is_idempotent(tmp_path: Path) -> None:
    """§4.4: a second apply of the same edits is a no-op."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\n")
    first = apply_edits(p, [PropertyEdit("b", "2")])
    assert first.any
    before = p.stat().st_mtime_ns
    second = apply_edits(p, [PropertyEdit("b", "2")])
    assert not second.any
    assert p.stat().st_mtime_ns == before


def test_compute_diff_uses_stripped_effective_value(tmp_path: Path) -> None:
    """§7.4: effective value is the bytes after the first =, whitespace-stripped."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=   true   \n")
    assert not compute_diff(p, [PropertyEdit("require-resource-pack", "true")]).any


def test_compute_diff_reports_before_and_after(tmp_path: Path) -> None:
    """§7.4: a change records (key, before, after)."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=false\n")
    (change,) = compute_diff(p, [PropertyEdit("require-resource-pack", "true")]).changes
    assert change == PropertyChange(key="require-resource-pack", before="false", after="true")


def test_compute_diff_missing_key_reports_before_as_none(tmp_path: Path) -> None:
    """§7.4: an absent key has before=None; it is a change."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"some-other-key=x\n")
    (change,) = compute_diff(p, [PropertyEdit("resource-pack", "http://example/p.zip")]).changes
    assert change.before is None


# ---------------------------------------------------------------------------
# §7.4: replacing an existing key
# ---------------------------------------------------------------------------


def test_replace_existing_key_simple(tmp_path: Path) -> None:
    """§7.4: replace writes the new value; the rest of the line is preserved."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=false\n")
    assert apply_edits(p, [PropertyEdit("require-resource-pack", "true")]).any
    assert p.read_bytes() == b"require-resource-pack=true\n"


def test_replace_preserves_whitespace_before_equals(tmp_path: Path) -> None:
    """§7.4: only the value portion is replaced; the = position is preserved."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"key  =  old\n")
    apply_edits(p, [PropertyEdit("key", "new")])
    assert p.read_bytes() == b"key  =new\n"


def test_replace_preserves_leading_whitespace_before_key(tmp_path: Path) -> None:
    r"""§7.4: ^\s*<key>\s*= matches; leading whitespace survives the edit."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"   require-resource-pack=true\n")
    apply_edits(p, [PropertyEdit("require-resource-pack", "false")])
    assert p.read_bytes() == b"   require-resource-pack=false\n"


def test_replace_value_containing_equals(tmp_path: Path) -> None:
    """§7.4: everything after the first = is the value; embedded = is part of it."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"k=a=b\n")
    apply_edits(p, [PropertyEdit("k", "x=y")])
    assert p.read_bytes() == b"k=x=y\n"


def test_replace_value_containing_hash_is_not_a_comment(tmp_path: Path) -> None:
    """§7.4: # inside a value is not a comment."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"resource-pack=http://x/p.zip#frag\n")
    assert not compute_diff(p, [PropertyEdit("resource-pack", "http://x/p.zip#frag")]).any


def test_replace_empty_value(tmp_path: Path) -> None:
    """§4.15: the empty prompt is written as `key=` with no value."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"resource-pack-prompt=old\n")
    apply_edits(p, [PropertyEdit("resource-pack-prompt", "")])
    assert p.read_bytes() == b"resource-pack-prompt=\n"


# ---------------------------------------------------------------------------
# §7.4: appending a missing key
# ---------------------------------------------------------------------------


def test_append_missing_key_file_ends_with_lf(tmp_path: Path) -> None:
    r"""§7.4: file already ends in \n -> the appended line follows immediately."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\n")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert p.read_bytes() == b"a=1\nb=2\n"


def test_append_missing_key_file_no_trailing_newline(tmp_path: Path) -> None:
    """§7.4: file lacks a trailing newline -> one is added before the appended key."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert p.read_bytes() == b"a=1\nb=2\n"


def test_append_to_empty_file_uses_lf(tmp_path: Path) -> None:
    r"""§7.4: for an empty file, use \n."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"")
    apply_edits(p, [PropertyEdit("a", "1")])
    assert p.read_bytes() == b"a=1\n"


def test_append_uses_last_line_eol_crlf(tmp_path: Path) -> None:
    """§7.4: appended line uses the EOL of the file's last existing line."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\r\n")
    apply_edits(p, [PropertyEdit("b", "2")])
    assert p.read_bytes() == b"a=1\r\nb=2\r\n"


def test_append_crlf_file_no_trailing_newline(tmp_path: Path) -> None:
    """§7.4: CRLF file without a final newline gets CRLF before the appended key."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\r\nb=2")
    apply_edits(p, [PropertyEdit("c", "3")])
    assert p.read_bytes() == b"a=1\r\nb=2\r\nc=3\r\n"


def test_append_empty_value(tmp_path: Path) -> None:
    """§4.15: an empty prompt value is appended correctly."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\n")
    apply_edits(p, [PropertyEdit("resource-pack-prompt", "")])
    assert p.read_bytes() == b"a=1\nresource-pack-prompt=\n"


# ---------------------------------------------------------------------------
# §7.4: preserved content
# ---------------------------------------------------------------------------


def test_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    """§7.4: comments and blank lines are preserved verbatim."""
    original = b"# leading comment\n\na=1\n# between\nrequire-resource-pack=false\n\nb=2\n"
    p = tmp_path / "server.properties"
    p.write_bytes(original)
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.read_bytes() == b"# leading comment\n\na=1\n# between\nrequire-resource-pack=true\n\nb=2\n"


def test_comment_line_containing_key_is_not_the_key(tmp_path: Path) -> None:
    r"""§7.4: ^\s*<key>\s*= does not match a comment line."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"# require-resource-pack=true\nrequire-resource-pack=false\n")
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.read_bytes() == b"# require-resource-pack=true\nrequire-resource-pack=true\n"


def test_key_ordering_is_preserved(tmp_path: Path) -> None:
    """§7.4: an in-place edit does not reorder keys."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"c=3\na=1\nb=2\n")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert p.read_bytes() == b"c=3\na=1\nb=20\n"


def test_per_line_eol_preserved(tmp_path: Path) -> None:
    """§7.4: each line's EOL is preserved as-is; mixed LF/CRLF is supported."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\r\nb=2\nc=3\r\n")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert p.read_bytes() == b"a=1\r\nb=20\nc=3\r\n"


def test_last_line_without_terminator_stays_without_terminator(tmp_path: Path) -> None:
    """§7.4: an edit to a last line with no terminator must not invent one."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\nb=2")
    apply_edits(p, [PropertyEdit("b", "20")])
    assert p.read_bytes() == b"a=1\nb=20"


def test_last_line_no_terminator_with_append(tmp_path: Path) -> None:
    """§7.4: edit last line AND append -> the terminator is added by the append."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\nb=2")
    apply_edits(p, [PropertyEdit("b", "20"), PropertyEdit("c", "3")])
    assert p.read_bytes() == b"a=1\nb=20\nc=3\n"


def test_non_ascii_bytes_preserved_elsewhere(tmp_path: Path) -> None:
    """§10.1: non-ASCII bytes in unrelated lines survive an edit."""
    original = "# café\nrequire-resource-pack=false\n".encode()
    p = tmp_path / "server.properties"
    p.write_bytes(original)
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.read_bytes() == "# café\nrequire-resource-pack=true\n".encode()


def test_non_ascii_bytes_preserved_arbitrary_positions(tmp_path: Path) -> None:
    """§10.1: arbitrary non-ASCII bytes survive an edit."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"# \xa7 \xe9\nrequire-resource-pack=false\n# \xff\n")
    apply_edits(p, [PropertyEdit("require-resource-pack", "true")])
    assert p.read_bytes() == b"# \xa7 \xe9\nrequire-resource-pack=true\n# \xff\n"


def test_non_ascii_managed_value_utf8_matches(tmp_path: Path) -> None:
    """§7.4: managed values are UTF-8; comparison is byte-exact."""
    p = tmp_path / "server.properties"
    p.write_bytes("resource-pack-prompt=café\n".encode())
    assert not compute_diff(p, [PropertyEdit("resource-pack-prompt", "café")]).any


# ---------------------------------------------------------------------------
# §7.4: duplicate keys -- last occurrence authoritative
# ---------------------------------------------------------------------------


def test_duplicate_keys_last_replaced_earlier_untouched(tmp_path: Path) -> None:
    """§7.4: only the last occurrence is rewritten; earlier ones are left alone."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"require-resource-pack=false\n# in between\nrequire-resource-pack=false\n")
    assert apply_edits(p, [PropertyEdit("require-resource-pack", "true")]).any
    assert p.read_bytes() == b"require-resource-pack=false\n# in between\nrequire-resource-pack=true\n"


def test_duplicate_keys_effective_value_is_last(tmp_path: Path) -> None:
    """§7.4: comparison uses the last occurrence's effective value."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"k=false\nk=true\n")
    assert not compute_diff(p, [PropertyEdit("k", "true")]).any


def test_duplicate_keys_emits_one_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§7.4: a single warning per duplicated key per call."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"k=a\nk=b\nk=c\n")
    with caplog.at_level(logging.WARNING):
        apply_edits(p, [PropertyEdit("k", "z")], logger=logging.getLogger("test"))
    assert sum("duplicate" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# Multiple edits and edge cases
# ---------------------------------------------------------------------------


def test_multiple_edits_in_one_call(tmp_path: Path) -> None:
    """Multiple edits apply in one atomic write."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\nresource-pack=old\n")
    diff = apply_edits(
        p,
        [
            PropertyEdit("resource-pack", "http://x/p.zip"),
            PropertyEdit("require-resource-pack", "true"),
            PropertyEdit("resource-pack-sha1", "abcdef0123"),
        ],
    )
    assert set(diff.changed_keys) == {"resource-pack", "require-resource-pack", "resource-pack-sha1"}
    assert p.read_bytes() == b"a=1\nresource-pack=http://x/p.zip\nrequire-resource-pack=true\nresource-pack-sha1=abcdef0123\n"


def test_duplicate_edit_keys_last_wins(tmp_path: Path) -> None:
    """Last edit for a key wins; order of first appearance is preserved."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"")
    apply_edits(p, [PropertyEdit("k", "first"), PropertyEdit("k", "second")])
    assert p.read_bytes() == b"k=second\n"


def test_empty_edit_list_is_noop(tmp_path: Path) -> None:
    """An empty edit list touches nothing."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"a=1\n")
    before = p.stat().st_mtime_ns
    diff = apply_edits(p, [])
    assert not diff.any
    assert p.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# §7.4 end-to-end: the four keys, from an empty file
# ---------------------------------------------------------------------------


def test_all_four_managed_keys_written_from_empty_file(tmp_path: Path) -> None:
    """§7.4: the resource-pack scope writes exactly these four keys."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"")
    apply_edits(
        p,
        [
            PropertyEdit("require-resource-pack", "true"),
            PropertyEdit("resource-pack", "http://example/pack.zip"),
            PropertyEdit("resource-pack-prompt", "Please accept"),
            PropertyEdit("resource-pack-sha1", "0123456789abcdef"),
        ],
    )
    assert p.read_bytes() == (
        b"require-resource-pack=true\nresource-pack=http://example/pack.zip\nresource-pack-prompt=Please accept\nresource-pack-sha1=0123456789abcdef\n"
    )


def test_boolean_values_are_lowercase(tmp_path: Path) -> None:
    """§7.4: booleans are true / false, lowercase."""
    p = tmp_path / "server.properties"
    p.write_bytes(b"")
    apply_edits(p, [PropertyEdit("require-resource-pack", "false")])
    assert p.read_bytes() == b"require-resource-pack=false\n"
