# tests/modpack_upgrade/test_manifest.py

"""Manifest schema validation tests (Manifest_Schema Section 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.manifest import (
    LOADERS,
    NO_VERSION_VALUES,
    SELECTION_VALUES,
    load_manifest,
    validate_manifest_bytes,
)


def _valid_manifest_bytes() -> bytes:
    """Return a valid manifest body as bytes."""
    return b'[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "52.0.1"\n\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n'


def _write_manifest(tmp_path: Path, body: bytes) -> Path:
    """Write a manifest file and return its path."""
    p = tmp_path / "modpack.manifest.toml"
    p.write_bytes(body)
    return p


# ---------------------------------------------------------------------------
# Enums sanity
# ---------------------------------------------------------------------------


def test_loader_enum_matches_spec() -> None:
    """The loader enum is the four values from Section 1."""
    assert set(LOADERS) == {"forge", "neoforge", "fabric", "quilt"}


def test_selection_enum_matches_spec() -> None:
    """The selection enum is the three values from Section 3.1."""
    assert set(SELECTION_VALUES) == {
        "release_then_beta",
        "release_only",
        "release_then_beta_then_alpha",
    }


def test_no_version_enum_matches_spec() -> None:
    """The no_version enum is the three values from Section 3.2."""
    assert set(NO_VERSION_VALUES) == {"ask", "drop", "fail"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_manifest_valid(tmp_path: Path) -> None:
    """A complete valid manifest loads and hashes cleanly."""
    m = load_manifest(_write_manifest(tmp_path, _valid_manifest_bytes()))
    assert m.minecraft_version == "1.21.1"
    assert m.loader == "forge"
    assert m.loader_version == "52.0.1"
    assert m.selection == "release_then_beta"
    assert m.no_version == "ask"
    assert len(m.raw_sha256) == 64


def test_validate_manifest_bytes_returns_no_violations() -> None:
    """The bytes validator returns an empty violation list on valid input."""
    data, violations = validate_manifest_bytes(_valid_manifest_bytes())
    assert data is not None
    assert violations == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    """A missing manifest file raises SchemaError."""
    with pytest.raises(SchemaError, match="not found"):
        load_manifest(tmp_path / "nope.toml")


def test_load_manifest_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML raises SchemaError with the parse error."""
    p = _write_manifest(tmp_path, b"not = valid = toml\n")
    with pytest.raises(SchemaError, match="invalid TOML"):
        load_manifest(p)


def test_validate_manifest_bytes_malformed_returns_violation() -> None:
    """The bytes validator reports a TOML parse error as a violation."""
    _, violations = validate_manifest_bytes(b"not valid")
    assert any("invalid TOML" in v for v in violations)


@pytest.mark.parametrize(
    "body,expected_substring",
    [
        # Missing [minecraft]
        (b'[policy]\nselection = "release_only"\nno_version = "ask"\n', "missing top-level key"),
        # Missing [policy]
        (
            b'[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "1.0"\n',
            "missing top-level key",
        ),
        # Extra top-level key -- must appear BEFORE any [section] to stay at the root
        (
            b'extra = "x"\n'
            b'[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "1.0"\n'
            b'[policy]\nselection = "release_only"\nno_version = "ask"\n',
            "unexpected top-level key",
        ),
        # Bad loader
        (
            b'[minecraft]\nversion = "1.21.1"\nloader = "bogus"\nloader_version = "1.0"\n[policy]\nselection = "release_only"\nno_version = "ask"\n',
            "loader must be one of",
        ),
        # Empty version
        (
            b'[minecraft]\nversion = ""\nloader = "forge"\nloader_version = "1.0"\n[policy]\nselection = "release_only"\nno_version = "ask"\n',
            "version must be a non-empty string",
        ),
        # Bad selection
        (
            b'[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "1.0"\n[policy]\nselection = "release_only"\nno_version = "bogus"\n',
            "no_version must be one of",
        ),
    ],
)
def test_validate_manifest_bytes_reports_each_violation(body: bytes, expected_substring: str) -> None:
    """Each documented Section 8 violation appears in the violation list."""
    _, violations = validate_manifest_bytes(body)
    assert any(expected_substring in v for v in violations), violations


def test_validate_manifest_bytes_empty_body_reports_missing_keys() -> None:
    """An empty TOML body reports both top-level keys as missing.

    Note: TOML documents always parse to a table at the top level, so the
    ``_collect_violations`` non-dict branch (``top-level TOML must be a
    table``) is unreachable via ``validate_manifest_bytes``. That branch
    exists as defensive code for direct callers; this test exercises the
    reachable behavior: an empty body parses to ``{}`` and reports both
    sections as missing.
    """
    _, violations = validate_manifest_bytes(b"")
    assert any("missing top-level key" in v for v in violations)
    assert any("minecraft" in v for v in violations)
    assert any("policy" in v for v in violations)


def test_load_manifest_aggregates_all_violations(tmp_path: Path) -> None:
    """Every Section 8 violation is reported, not just the first."""
    p = _write_manifest(
        tmp_path,
        b'[minecraft]\nversion = ""\nloader = "bogus"\n[policy]\nselection = "bogus"\nno_version = "bogus"\n',
    )
    with pytest.raises(SchemaError) as ei:
        load_manifest(p)
    # Message is "manifest schema violation(s):\n  - ..." with every violation.
    msg = str(ei.value)
    assert msg.count("  - ") >= 4
