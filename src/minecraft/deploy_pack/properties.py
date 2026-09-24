# src/minecraft/deploy_pack/properties.py

"""Surgical ``server.properties`` edits (Project_Specs.md §7.4).

Four keys are managed by the resource-pack scope:

    require-resource-pack     bool, lowercase
    resource-pack             URL
    resource-pack-prompt      string (empty is valid)
    resource-pack-sha1        lowercase hex

Everything else in the file is preserved byte-for-byte: comments, blank
lines, key ordering, unrelated keys. Only the value portion of the last
occurrence of a managed key is rewritten; the key and the ``=`` stay put.

Design notes
------------

* **Binary I/O.** §7.4 specifies binary read/write, and §10.1 requires
  that non-ASCII bytes survive an edit. So the file is treated as bytes
  throughout; keys are matched with a bytes regex and lines are split at
  byte level. Only the "before" value is decoded (for logging / diffs),
  with ``errors="replace"``.

* **Value comparison.** A property is changed only when the effective
  value differs from the target (§4.4). The effective value is the bytes
  after the first ``=``, stripped of surrounding whitespace - which is
  what Minecraft's parser sees. Target values are encoded as UTF-8;
  server.properties is UTF-8 by convention.

* **Duplicate keys.** Last occurrence is authoritative (§7.4). Only the
  last occurrence is rewritten; earlier ones are left alone. A single
  warning is emitted per duplicated key per call.

* **No-op writes.** If nothing would change, the file is not touched.
  This preserves mtime and keeps §4.4's "not a change" property honest
  at the filesystem level.

* **Missing file.** ``ConfigError`` (exit 3). §7.4 classifies this as a
  preflight error; the scope code checks for the file before calling
  here, so this is defensive.

Public API
----------

    MANAGED_KEYS         the four key names this module knows about
    PropertyEdit         one (key, target value) to apply
    PropertyChange       one effective change: (key, before | None, after)
    PropertiesDiff       the set of changes; exposes ``any``, ``has``,
                         ``changed_keys``
    compute_diff(path, edits)      read-only; does not write
    apply_edits(path, edits)       atomic; returns the diff it applied
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .files import atomic_write

__all__ = ["MANAGED_KEYS", "PropertiesDiff", "PropertyChange", "PropertyEdit", "apply_edits", "compute_diff"]
MANAGED_KEYS = frozenset({"require-resource-pack", "resource-pack", "resource-pack-prompt", "resource-pack-sha1"})


@dataclass
class PropertyEdit:
    """One managed property to write. ``value`` is the target string."""

    key: str
    value: str


@dataclass
class PropertyChange:
    """A single effective change between the current file and the edits."""

    key: str
    before: str | None
    after: str


@dataclass
class PropertiesDiff:
    """The set of effective changes produced by applying a list of edits."""

    changes: list[PropertyChange]

    @property
    def any(self) -> bool:
        """Returns True if there are any changes, otherwise False."""
        return bool(self.changes)

    @property
    def changed_keys(self) -> list[str]:
        """Returns the keys of all recorded changes."""
        return [c.key for c in self.changes]

    def has(self, key: str) -> bool:
        """Checks whether a change with the given key exists."""
        return any(c.key == key for c in self.changes)


_KEY_RE_CACHE: dict[str, re.Pattern[bytes]] = {}


def _key_regex(key: str) -> re.Pattern[bytes]:
    """Bytes regex matching '<leading ws><key><ws>=' at start of a line."""
    pat = _KEY_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(b"^[ \\t]*" + re.escape(key.encode("ascii")) + b"[ \\t]*=")
        _KEY_RE_CACHE[key] = pat
    return pat


def _split_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    r"""Split ``data`` into (content, terminator) pairs.

    Terminators are ``b"\\r\\n"``, ``b"\\n"``, or ``b""`` for the final
    line if the file does not end with a newline. A lone ``\\r`` is not
    a line boundary (§7.4).
    """
    result: list[tuple[bytes, bytes]] = []
    n = len(data)
    i = 0
    line_start = 0
    while i < n:
        if data[i : i + 1] == b"\n":
            raw = data[line_start:i]
            if raw.endswith(b"\r"):
                result.append((raw[:-1], b"\r\n"))
            else:
                result.append((raw, b"\n"))
            i += 1
            line_start = i
        else:
            i += 1
    if line_start < n:
        result.append((data[line_start:], b""))
    return result


def _detect_eol(data: bytes) -> bytes:
    r"""Return the file's line-ending style for appends (§7.4).

    Uses the last ``\\n`` in the file: if preceded by ``\\r``, the style
    is CRLF; otherwise LF. An empty file (or one with no newlines) uses
    LF, per the spec's "for an empty file, use ``\\n``".
    """
    idx = data.rfind(b"\n")
    if idx > 0 and data[idx - 1 : idx] == b"\r":
        return b"\r\n"
    return b"\n"


def _read_or_error(path: Path) -> bytes:
    if not path.is_file():
        raise ConfigError(f"server.properties not found: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _parse_existing(data: bytes, path: Path, logger: Any) -> dict[str, tuple[int, bytes]]:
    """Return {key: (line_index_of_last_occurrence, stripped_value_bytes)}.

    Only the four managed keys are considered. A warning is emitted once
    per key when the file contains multiple occurrences.
    """
    lines = _split_lines(data)
    result: dict[str, tuple[int, bytes]] = {}
    seen_count: dict[str, int] = {}
    for idx, (content, _term) in enumerate(lines):
        for key in MANAGED_KEYS:
            m = _key_regex(key).match(content)
            if m is None:
                continue
            result[key] = (idx, content[m.end() :].strip())
            seen_count[key] = seen_count.get(key, 0) + 1
            break
    if logger is not None:
        for key, count in seen_count.items():
            if count > 1:
                logger.warning(f"{path}: duplicate key {key!r} ({count} occurrences); the last is authoritative (§7.4)")
    return result


def _dedupe_last_wins(edits: list[PropertyEdit]) -> list[PropertyEdit]:
    """Deduplicate by key: last value wins; order of first appearance kept."""
    last: dict[str, PropertyEdit] = {}
    order: list[str] = []
    for e in edits:
        if e.key not in last:
            order.append(e.key)
        last[e.key] = e
    return [last[k] for k in order]


def _compute_diff(data: bytes, edits: list[PropertyEdit], path: Path, logger: Any) -> PropertiesDiff:
    existing = _parse_existing(data, path, logger)
    changes: list[PropertyChange] = []
    for edit in _dedupe_last_wins(edits):
        target = edit.value.encode("utf-8")
        entry = existing.get(edit.key)
        if entry is not None:
            _idx, current = entry
            if current == target:
                continue
            changes.append(PropertyChange(key=edit.key, before=current.decode("utf-8", "replace"), after=edit.value))
        else:
            changes.append(PropertyChange(key=edit.key, before=None, after=edit.value))
    return PropertiesDiff(changes=changes)


def _apply(data: bytes, edits: list[PropertyEdit], path: Path, logger: Any) -> bytes:
    lines = _split_lines(data)
    existing = _parse_existing(data, path, logger)
    default_eol = _detect_eol(data)
    updates: dict[int, bytes] = {}
    appends: list[bytes] = []
    for edit in _dedupe_last_wins(edits):
        target = edit.value.encode("utf-8")
        entry = existing.get(edit.key)
        if entry is not None:
            idx, current = entry
            if current == target:
                continue
            content, term = lines[idx]
            m = _key_regex(edit.key).match(content)
            assert m is not None
            prefix = content[: m.end()]
            updates[idx] = prefix + target + term
        else:
            appends.append(edit.key.encode("ascii") + b"=" + target)
    out = bytearray()
    for idx, (content, term) in enumerate(lines):
        if idx in updates:
            out += updates[idx]
        else:
            out += content + term
    if appends:
        if out and (not out.endswith(b"\n")):
            out += default_eol
        for line in appends:
            out += line + default_eol
    return bytes(out)


def compute_diff(path: Path, edits: list[PropertyEdit], logger: Any = None) -> PropertiesDiff:
    """Return which edits would change the file. Read-only.

    Raises ConfigError if the file is missing (§7.4).
    """
    data = _read_or_error(path)
    return _compute_diff(data, edits, path, logger)


def apply_edits(path: Path, edits: list[PropertyEdit], logger: Any = None) -> PropertiesDiff:
    """Apply the edits atomically and return the diff that was applied.

    If no effective change is needed, the file is not rewritten. Writes
    go through :func:`files.atomic_write` (§4.10): same-directory temp
    file, fsync, preserved mode and ownership, then ``os.replace``.

    Raises ConfigError if the file is missing (§7.4).
    """
    data = _read_or_error(path)
    diff = _compute_diff(data, edits, path, logger)
    if not diff.any:
        return diff
    new_data = _apply(data, edits, path, logger)
    atomic_write(path, new_data, logger=logger)
    return diff
