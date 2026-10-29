# tests/installer/test_velocity.py

"""Tests for minecraft.installer.velocity."""

from __future__ import annotations

from pathlib import Path

from minecraft.installer.velocity import (
    read_pcf_forwarding_secret,
    update_pcf_forwarding_secret,
    write_default_pcf,
)


def test_write_default_pcf_creates_file(tmp_path: Path) -> None:
    """The default PCF template includes the secret."""
    p = tmp_path / "pcf.toml"
    write_default_pcf(p, "the-secret")
    body = p.read_text(encoding="utf-8")
    assert "[forwarding]" in body
    assert 'secret = "the-secret"' in body


def test_update_creates_when_missing(tmp_path: Path) -> None:
    """A missing PCF file is created with the default template."""
    p = tmp_path / "pcf.toml"
    changed = update_pcf_forwarding_secret(p, "new-secret")
    assert changed is True
    assert read_pcf_forwarding_secret(p) == "new-secret"


def test_update_replaces_existing(tmp_path: Path) -> None:
    """An existing PCF file has its secret updated."""
    p = tmp_path / "pcf.toml"
    write_default_pcf(p, "old-secret")
    changed = update_pcf_forwarding_secret(p, "new-secret")
    assert changed is True
    assert read_pcf_forwarding_secret(p) == "new-secret"


def test_update_is_idempotent(tmp_path: Path) -> None:
    """A second call with the same secret is a no-op."""
    p = tmp_path / "pcf.toml"
    write_default_pcf(p, "the-secret")
    changed = update_pcf_forwarding_secret(p, "the-secret")
    assert changed is False


def test_update_preserves_comments(tmp_path: Path) -> None:
    """Comments around the secret line are preserved."""
    p = tmp_path / "pcf.toml"
    p.write_text(
        '[forwarding]\n# a comment before the secret\nsecret = "old"\n# a comment after the secret\nenabled = true\n',
        encoding="utf-8",
    )
    update_pcf_forwarding_secret(p, "new")
    body = p.read_text(encoding="utf-8")
    assert "# a comment before the secret\n" in body
    assert "# a comment after the secret\n" in body
    assert 'secret = "new"\n' in body


def test_update_preserves_indentation(tmp_path: Path) -> None:
    """Indented secret lines keep their indentation."""
    p = tmp_path / "pcf.toml"
    p.write_text(
        '[forwarding]\n        secret = "old"\n        enabled = true\n',
        encoding="utf-8",
    )
    update_pcf_forwarding_secret(p, "new")
    body = p.read_text(encoding="utf-8")
    assert '        secret = "new"\n' in body


def test_update_preserves_other_sections(tmp_path: Path) -> None:
    """Other TOML sections are preserved."""
    p = tmp_path / "pcf.toml"
    p.write_text(
        '[forwarding]\nsecret = "old"\n\n[advanced]\nmodernForwardingVersion = "NO_OVERRIDE"\n',
        encoding="utf-8",
    )
    update_pcf_forwarding_secret(p, "new")
    body = p.read_text(encoding="utf-8")
    assert 'secret = "new"' in body
    assert "[advanced]" in body
    assert 'modernForwardingVersion = "NO_OVERRIDE"' in body


def test_update_inserts_secret_when_missing(tmp_path: Path) -> None:
    """A [forwarding] section without a secret line gets one inserted."""
    p = tmp_path / "pcf.toml"
    p.write_text(
        '[forwarding]\nenabled = true\nmode = "MODERN"\n',
        encoding="utf-8",
    )
    changed = update_pcf_forwarding_secret(p, "new-secret")
    assert changed is True
    assert read_pcf_forwarding_secret(p) == "new-secret"
    body = p.read_text(encoding="utf-8")
    assert 'secret = "new-secret"' in body


def test_update_appends_forwarding_section_when_missing(tmp_path: Path) -> None:
    """A file with no [forwarding] section gets one appended."""
    p = tmp_path / "pcf.toml"
    p.write_text(
        '[advanced]\nmodernForwardingVersion = "NO_OVERRIDE"\n',
        encoding="utf-8",
    )
    changed = update_pcf_forwarding_secret(p, "new-secret")
    assert changed is True
    body = p.read_text(encoding="utf-8")
    assert "[forwarding]" in body
    assert 'secret = "new-secret"' in body


def test_read_pcf_forwarding_secret_missing_file(tmp_path: Path) -> None:
    """A missing file returns None."""
    assert read_pcf_forwarding_secret(tmp_path / "nope") is None


def test_read_pcf_forwarding_secret_no_forwarding_section(tmp_path: Path) -> None:
    """A file without [forwarding] returns None."""
    p = tmp_path / "pcf.toml"
    p.write_text("[advanced]\nfoo = 1\n", encoding="utf-8")
    assert read_pcf_forwarding_secret(p) is None


def test_read_pcf_forwarding_secret_without_quotes(tmp_path: Path) -> None:
    """A bare value (no quotes) is returned as-is."""
    p = tmp_path / "pcf.toml"
    p.write_text("[forwarding]\nsecret = barevalue\n", encoding="utf-8")
    assert read_pcf_forwarding_secret(p) == "barevalue"
