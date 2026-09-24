# src/minecraft/deploy_pack/overrides.py

"""Side override handling (§3.11) and audit-tool write-back (§6.1).

Two responsibilities, both independent of the rest of the pipeline:

  * load and apply ``config.d/side_overrides.toml``, which maps mod
    identifiers to a forced side (``client``, ``server``, ``both``, or
    ``skipped``). Applied over Prism index entries *before* the side
    filter runs, so an overridden entry is classified by the override
    rather than by its declared side.

  * write the ``[deployment_tool_review]`` section back to the file
    after the ``--audit-mods`` TUI runs (§6.1). The write is a
    line-based splice: the tool never parses-then-reserialises the
    file, so comments, whitespace, and key ordering outside the review
    section are preserved byte-for-byte.

Precedence (§3.11): ``by_id`` > ``by_filename`` > ``deployment_tool_review``.

Values are case-sensitive: ``"client"`` is valid, ``"Client"`` is not.
Any non-string value, or any string outside the valid set, is a
configuration error (exit 3).
"""

from __future__ import annotations

import datetime
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .files import atomic_write

__all__ = ["SideOverrides", "apply_side_overrides", "load_side_overrides", "save_side_overrides"]
_VALID_SIDES = frozenset({"client", "server", "both", "skipped"})
_REVIEW_SECTION = "deployment_tool_review"
_ANY_SECTION_RE = re.compile("^[ \\t]*\\[[^\\]]+\\]", re.MULTILINE)


@dataclass
class SideOverrides:
    """Parsed contents of side_overrides.toml, split by section."""

    by_id: dict[str, str] = field(default_factory=dict)
    by_filename: dict[str, str] = field(default_factory=dict)
    deployment_tool_review: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Checks whether the object is empty.

        Returns:
            bool: True if empty, False otherwise.
        """
        return not (self.by_id or self.by_filename or self.deployment_tool_review)

    def lookup(self, mod_id: str, filename: str) -> str | None:
        """Return the override value for the given mod, or None.

        Precedence (§3.11): by_id > by_filename > deployment_tool_review.
        ``mod_id`` and ``filename`` are matched verbatim; callers pass
        already-stringified values.
        """
        if mod_id and mod_id in self.by_id:
            return self.by_id[mod_id]
        if filename and filename in self.by_filename:
            return self.by_filename[filename]
        if filename and filename in self.deployment_tool_review:
            return self.deployment_tool_review[filename]
        return None


def load_side_overrides(path: Path) -> SideOverrides:
    """Load and validate side_overrides.toml (§3.11).

    Missing file → empty overrides, no error.
    Invalid values or malformed TOML → ConfigError (exit 3).

    The ``[deployment_tool_review]`` section may carry a leading
    ``# Last generated: ...`` comment; tomllib treats it as a comment
    and it does not affect the parsed values.
    """
    if not path.is_file():
        return SideOverrides()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    result = SideOverrides()
    for section_name in ("by_id", "by_filename", _REVIEW_SECTION):
        raw = data.get(section_name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: [{section_name}] must be a table, got {type(raw).__name__}")
        target = getattr(result, section_name)
        for key, value in raw.items():
            key_str = str(key)
            if not isinstance(value, str):
                raise ConfigError(f"{path}: [{section_name}] {key_str!r}: value must be a string, got {type(value).__name__}")
            if value not in _VALID_SIDES:
                raise ConfigError(
                    f"{path}: [{section_name}] {key_str!r}: invalid value {value!r}; expected one of {sorted(_VALID_SIDES)} (values are case-sensitive)"
                )
            target[key_str] = value
    return result


def apply_side_overrides(entries: list[dict], overrides: SideOverrides) -> list[dict]:
    """Apply overrides to Prism index entries in place, and return the list.

    Each entry's ``side`` field is replaced when an override matches.
    Matching is by ``id`` (stringified) first, then by ``file``. Entries
    with no match are left untouched.

    Ordering matters: callers must apply overrides *before* running the
    side filter, so the override's value is what the filter sees.
    """
    if overrides.is_empty():
        return entries
    for entry in entries:
        mod_id = str(entry.get("id", ""))
        filename = str(entry.get("file", ""))
        value = overrides.lookup(mod_id, filename)
        if value is not None:
            entry["side"] = value
    return entries


def _detect_eol(text: str) -> str:
    r"""Return the eol style of the last newline in text, defaulting to '\n'."""
    idx = text.rfind("\n")
    if idx >= 1 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _count_newline_sequences(text: str) -> int:
    """Count logical newlines in ``text``. A CRLF pair counts as one."""
    count = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\r" and i + 1 < len(text) and (text[i + 1] == "\n"):
            count += 1
            i += 2
        elif ch == "\n":
            count += 1
            i += 1
        else:
            i += 1
    return count


def _append_gap(original: str) -> str:
    """Return text to insert between ``original`` and an appended section.

    Ensures exactly one blank line separates the existing content from
    the appended header, without adding more than necessary. Preserves
    any already-present trailing blank lines rather than collapsing them.
    """
    if not original:
        return ""
    stripped = original.rstrip("\r\n")
    trailing = original[len(stripped) :]
    count = _count_newline_sequences(trailing)
    if count >= 2:
        return ""
    if count == 1:
        return _detect_eol(original)
    return _detect_eol(original) * 2


def _toml_escape(s: str) -> str:
    """Escape a string for a TOML basic string.

    Filenames rarely need this, but a backslash or double quote in a
    key would otherwise break the generated section.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_review_section(entries: Mapping[str, str], timestamp: str, eol: str) -> str:
    """Render [deployment_tool_review] with its header comment, ending in ``eol``.

    Keys are sorted for a deterministic file across runs.
    """
    lines = [f"[{_REVIEW_SECTION}]", f"# Last generated: {timestamp}"]
    for key in sorted(entries):
        lines.append(f'"{_toml_escape(key)}" = "{_toml_escape(entries[key])}"')
    return eol.join(lines) + eol


def _find_section_range(text: str, section_name: str) -> tuple[int, int] | None:
    """Locate a TOML section in raw text.

    Returns (start, end) offsets, or None if the section is absent.

    ``start`` is the offset of the section header line. ``end`` is the
    offset of the first line after the section: the next top-level
    section header line, or ``len(text)``.

    A section header is only recognised when ``[`` is the first
    non-whitespace character on its line, so a string value such as
    ``foo = "[bar]"`` cannot be mistaken for a header.
    """
    header_re = re.compile("^[ \\t]*\\[" + re.escape(section_name) + "\\][^\\n]*\\n?", re.MULTILINE)
    m = header_re.search(text)
    if m is None:
        return None
    start = m.start()
    rest = text[m.end() :]
    next_m = _ANY_SECTION_RE.search(rest)
    end = m.end() + (next_m.start() if next_m is not None else len(rest))
    return (start, end)


def _read_text_preserving_eol(path: Path) -> str:
    r"""Read ``path`` as UTF-8 without universal-newline translation.

    ``Path.read_text`` collapses ``\r\n`` to ``\n``, which would make
    every CRLF file look like LF to :func:`_detect_eol` and defeat the
    line-ending preservation guarantee. Reading with ``newline=""``
    keeps the original bytes intact.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return f.read()
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def save_side_overrides(path: Path, review_entries: Mapping[str, str], timestamp: str | None = None, logger: Any = None) -> None:
    """Write ``[deployment_tool_review]`` to ``path``, preserving everything else (§6.1).

    Behaviour:

      * File does not exist → the generated section is the entire file.
      * Section absent → append at EOF, preceded by one blank line.
      * Section present → replace in place, preserving a trailing blank
        line before the next section if one was there.

    The generated section's line endings match the file's last existing
    line ending style, or LF for an empty file. Everything outside the
    review section is preserved byte-for-byte.

    ``timestamp`` is the value written into the ``# Last generated:``
    comment. Defaults to now (UTC) formatted ``%Y-%m-%dT%H:%M:%SZ``.

    Writes atomically with metadata preservation (§4.10).
    """
    ts = timestamp if timestamp is not None else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not path.exists():
        content = _render_review_section(review_entries, ts, "\n")
    else:
        original = _read_text_preserving_eol(path)
        eol = _detect_eol(original)
        generated = _render_review_section(review_entries, ts, eol)
        range_ = _find_section_range(original, _REVIEW_SECTION)
        if range_ is None:
            gap = _append_gap(original)
            content = original + gap + generated
        else:
            start, end = range_
            section_text = original[start:end]
            ends_with_blank = section_text.endswith("\n\n") or section_text.endswith("\r\n\r\n")
            replacement = generated + eol if ends_with_blank else generated
            content = original[:start] + replacement + original[end:]
    atomic_write(path, content.encode("utf-8"), logger=logger)
