# tests/deploy_pack/test_scope_server_deletions.py

"""Unit tests for ``scope_server._apply_config_deletions``.

Slip-mode helper: applies the slip's per-instance ``config_deletions``
list under merge mode. Every branch of the per-entry loop is exercised
here so the function's operator-facing diagnostics are pinned by tests:

  * empty deletions tuple
  * non-directory root
  * absent target
  * protected path
  * regular file (unlink)
  * directory (rmtree)
  * symlink (unlink, destination untouched)
  * OSError during unlink (logged, continues)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from minecraft.deploy_pack.scope_server import _apply_config_deletions


def _logger() -> Mock:
    """Return a mock logger accepting debug/info/warning/error calls."""
    return Mock()


# ---------------------------------------------------------------------------
# Early returns
# ---------------------------------------------------------------------------


def test_empty_deletions_is_noop(tmp_path: Path) -> None:
    """An empty deletions tuple returns without touching the filesystem."""
    (tmp_path / "keep.txt").write_text("x")
    logger = _logger()
    _apply_config_deletions(tmp_path, (), (), logger)
    assert (tmp_path / "keep.txt").exists()
    logger.debug.assert_not_called()


def test_root_not_a_directory_is_noop(tmp_path: Path) -> None:
    """A non-directory root is silently skipped."""
    logger = _logger()
    _apply_config_deletions(tmp_path / "does-not-exist", ("file.txt",), (), logger)
    assert not (tmp_path / "does-not-exist").exists()


# ---------------------------------------------------------------------------
# Per-entry branches
# ---------------------------------------------------------------------------


def test_absent_target_is_skipped(tmp_path: Path) -> None:
    """An entry that is not present under root logs and continues."""
    logger = _logger()
    _apply_config_deletions(tmp_path, ("missing.txt",), (), logger)
    logger.debug.assert_called()


def test_regular_file_is_unlinked(tmp_path: Path) -> None:
    """A regular file target is removed."""
    (tmp_path / "config.toml").write_text("x")
    logger = _logger()
    _apply_config_deletions(tmp_path, ("config.toml",), (), logger)
    assert not (tmp_path / "config.toml").exists()


def test_directory_is_removed_recursively(tmp_path: Path) -> None:
    """A directory target is removed with all its contents."""
    sub = tmp_path / "world"
    sub.mkdir()
    (sub / "level.dat").write_bytes(b"\x00")
    (sub / "region").mkdir()
    (sub / "region" / "r.0.0.mca").write_bytes(b"\x00")

    logger = _logger()
    _apply_config_deletions(tmp_path, ("world",), (), logger)
    assert not (tmp_path / "world").exists()


def test_symlink_is_unlinked_without_following(tmp_path: Path) -> None:
    """A symlink target is removed; the link's destination is untouched."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep me")

    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)

    logger = _logger()
    _apply_config_deletions(root, ("link",), (), logger)

    assert not (root / "link").exists()
    assert (outside / "secret.txt").read_text() == "keep me"


def test_protected_target_is_skipped(tmp_path: Path) -> None:
    """A target matching a protect pattern is preserved and logged."""
    (tmp_path / "tokens.json").write_text("secret")
    logger = _logger()
    _apply_config_deletions(tmp_path, ("tokens.json",), ("tokens.json",), logger)
    assert (tmp_path / "tokens.json").exists()
    assert (tmp_path / "tokens.json").read_text() == "secret"
    logger.warning.assert_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_oserror_during_unlink_warns_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError during unlink logs a warning and continues (no re-raise)."""
    (tmp_path / "locked.txt").write_text("x")
    (tmp_path / "other.txt").write_text("y")

    real_unlink = Path.unlink

    def _fail(self: Path) -> None:
        if self.name == "locked.txt":
            raise OSError("simulated failure")
        return real_unlink(self)

    monkeypatch.setattr(Path, "unlink", _fail)
    logger = _logger()
    _apply_config_deletions(tmp_path, ("locked.txt", "other.txt"), (), logger)

    # First deletion failed and was logged; second succeeded.
    assert (tmp_path / "locked.txt").exists()
    assert not (tmp_path / "other.txt").exists()
    logger.warning.assert_called()
