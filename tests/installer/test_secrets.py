# tests/installer/test_secrets.py

"""Tests for minecraft.installer.secrets."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from minecraft.installer.errors import SecretsError
from minecraft.installer.secrets import (
    ensure_secret_file,
    generate_hex_secret,
    read_secret_file,
    write_secret_file,
)

# ---------------------------------------------------------------------------
# generate_hex_secret
# ---------------------------------------------------------------------------


def test_generate_hex_secret_default_length() -> None:
    """Default output is 64 lowercase hex characters (32 bytes)."""
    s = generate_hex_secret()
    assert len(s) == 64
    assert all(c in "0123456789abcdef" for c in s)


def test_generate_hex_secret_custom_length() -> None:
    """Custom byte count controls the output length."""
    s = generate_hex_secret(nbytes=8)
    assert len(s) == 16


def test_generate_hex_secret_produces_distinct_values() -> None:
    """Two calls produce different values (CSPRNG, not a constant)."""
    a = generate_hex_secret()
    b = generate_hex_secret()
    assert a != b


def test_generate_hex_secret_rejects_zero() -> None:
    """Nbytes must be positive."""
    with pytest.raises(SecretsError):
        generate_hex_secret(nbytes=0)


def test_generate_hex_secret_rejects_negative() -> None:
    """Nbytes must be positive."""
    with pytest.raises(SecretsError):
        generate_hex_secret(nbytes=-1)


# ---------------------------------------------------------------------------
# read_secret_file
# ---------------------------------------------------------------------------


def test_read_secret_file_missing_returns_none(tmp_path: Path) -> None:
    """A missing file returns None (not an error)."""
    assert read_secret_file(tmp_path / "nope") is None


def test_read_secret_file_empty_raises(tmp_path: Path) -> None:
    """An empty file raises SecretsError."""
    p = tmp_path / "empty"
    p.write_text("", encoding="utf-8")
    with pytest.raises(SecretsError, match="empty"):
        read_secret_file(p)


def test_read_secret_file_whitespace_only_raises(tmp_path: Path) -> None:
    """A whitespace-only file is treated as empty."""
    p = tmp_path / "blank"
    p.write_text("   \n\n  \n", encoding="utf-8")
    with pytest.raises(SecretsError, match="empty"):
        read_secret_file(p)


def test_read_secret_file_non_hex_raises(tmp_path: Path) -> None:
    """Non-hex content raises SecretsError."""
    p = tmp_path / "bad"
    p.write_text("password123!\n", encoding="utf-8")
    with pytest.raises(SecretsError, match="non-hex"):
        read_secret_file(p)


def test_read_secret_file_strips_trailing_whitespace(tmp_path: Path) -> None:
    """Trailing newline and spaces are stripped."""
    p = tmp_path / "ok"
    p.write_text("deadbeef  \n\n", encoding="utf-8")
    assert read_secret_file(p) == "deadbeef"


def test_read_secret_file_accepts_uppercase_hex(tmp_path: Path) -> None:
    """Uppercase hex is accepted (and preserved on read)."""
    p = tmp_path / "up"
    p.write_text("DEADBEEF\n", encoding="utf-8")
    assert read_secret_file(p) == "DEADBEEF"


def test_read_secret_file_directory_raises(tmp_path: Path) -> None:
    """A directory at the secret path raises SecretsError."""
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(SecretsError, match="regular file"):
        read_secret_file(d)


# ---------------------------------------------------------------------------
# write_secret_file
# ---------------------------------------------------------------------------


def test_write_secret_file_writes_hex(tmp_path: Path) -> None:
    """Writing produces the expected content (with trailing newline)."""
    p = tmp_path / "s"
    write_secret_file(p, "deadbeef")
    assert p.read_text(encoding="utf-8") == "deadbeef\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX perms are not meaningful on Windows")
def test_write_secret_file_sets_0600(tmp_path: Path) -> None:
    """The written file has mode 0600."""
    p = tmp_path / "s"
    write_secret_file(p, "deadbeef")
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX perms are not meaningful on Windows")
def test_write_secret_file_overwrites_existing_mode(tmp_path: Path) -> None:
    """An existing loose-mode file is tightened to 0600 on write."""
    p = tmp_path / "s"
    p.write_text("old\n", encoding="utf-8")
    p.chmod(0o644)
    write_secret_file(p, "deadbeef")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_write_secret_file_rejects_empty(tmp_path: Path) -> None:
    """Empty secret raises SecretsError."""
    with pytest.raises(SecretsError, match="empty"):
        write_secret_file(tmp_path / "s", "")


def test_write_secret_file_rejects_non_hex(tmp_path: Path) -> None:
    """Non-hex secret raises SecretsError."""
    with pytest.raises(SecretsError, match="hex"):
        write_secret_file(tmp_path / "s", "not-hex!")


def test_write_secret_file_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent directories are created."""
    p = tmp_path / "a" / "b" / "c" / "s"
    write_secret_file(p, "deadbeef")
    assert p.is_file()


# ---------------------------------------------------------------------------
# ensure_secret_file
# ---------------------------------------------------------------------------


def test_ensure_secret_file_creates_when_missing(tmp_path: Path) -> None:
    """A missing file is generated and written."""
    p = tmp_path / "s"
    secret, changed = ensure_secret_file(p, rotate=False)
    assert changed is True
    assert len(secret) == 64
    assert p.is_file()
    assert read_secret_file(p) == secret


def test_ensure_secret_file_reuses_existing(tmp_path: Path) -> None:
    """An existing valid file is reused unchanged."""
    p = tmp_path / "s"
    p.write_text("deadbeef\n", encoding="utf-8")
    secret, changed = ensure_secret_file(p, rotate=False)
    assert changed is False
    assert secret == "deadbeef"
    assert p.read_text(encoding="utf-8") == "deadbeef\n"


def test_ensure_secret_file_regenerates_on_rotate(tmp_path: Path) -> None:
    """rotate=True replaces the existing secret."""
    p = tmp_path / "s"
    p.write_text("deadbeef\n", encoding="utf-8")
    secret, changed = ensure_secret_file(p, rotate=True)
    assert changed is True
    assert secret != "deadbeef"
    assert read_secret_file(p) == secret


def test_ensure_secret_file_rotate_on_missing_creates(tmp_path: Path) -> None:
    """rotate=True on a missing file creates it."""
    p = tmp_path / "s"
    secret, changed = ensure_secret_file(p, rotate=True)
    assert changed is True
    assert len(secret) == 64
