# tests/unit/common/test_file_utils_safety.py

"""Safety tests for file_utils: protection, exclusion, and clean behavior.

These tests exist to catch regressions in the code that DELETES files
during a clean deploy. Every assertion here corresponds to a scenario
where a bug would destroy user data on a live server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.minecraft.common import file_utils


@pytest.fixture
def logger():
    """Return a MagicMock logger for file_utils calls."""
    return MagicMock()


def _write(root: Path, rel: str, content: str = "x") -> Path:
    """Write content to root/rel, creating parent directories as needed."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestIsProtectedPath:
    """is_protected_path returns True for anything matching a protect pattern."""

    def test_empty_patterns_returns_false(self):
        """With no patterns, nothing is protected."""
        assert file_utils.is_protected_path(Path("anything.txt"), []) is False
        assert file_utils.is_protected_path(Path("anything.txt"), None) is False

    def test_component_match(self):
        """A bare filename pattern matches at any depth."""
        assert file_utils.is_protected_path(Path("config/auth/tokens.json"), ["tokens.json"]) is True
        assert file_utils.is_protected_path(Path("tokens.json"), ["tokens.json"]) is True

    def test_full_path_match(self):
        """A pattern with slashes matches the exact relative path."""
        assert file_utils.is_protected_path(Path("config/auth/tokens.json"), ["config/auth/tokens.json"]) is True
        assert file_utils.is_protected_path(Path("tokens.json"), ["config/auth/tokens.json"]) is False

    def test_wildcard_pattern(self):
        """Wildcards work in both component and full-path positions."""
        assert file_utils.is_protected_path(Path("config/secrets.key"), ["*.key"]) is True
        assert file_utils.is_protected_path(Path("config/auth/tokens.json"), ["config/*/tokens.json"]) is True

    def test_no_match_returns_false(self):
        """A path that matches none of the patterns is not protected."""
        assert file_utils.is_protected_path(Path("config/normal.toml"), ["tokens.json", "*.secret"]) is False

    def test_directory_component_match(self):
        """A directory name in the pattern protects the whole subtree."""
        assert file_utils.is_protected_path(Path("world/level.dat"), ["world"]) is True
        assert file_utils.is_protected_path(Path("world/region/r.0.0.mca"), ["world"]) is True


class TestGetProtectPatterns:
    """get_protect_patterns reads the pattern file and enforces presence."""

    def test_missing_file_raises(self, tmp_path, logger):
        """A missing protect file is a hard error, not a silent no-op."""
        with pytest.raises(FileNotFoundError):
            file_utils.get_protect_patterns(tmp_path / "nope", logger)

    def test_empty_file_warns(self, tmp_path, logger):
        """An empty protect file logs a warning and returns an empty list."""
        p = tmp_path / ".deploy_protect"
        p.write_text("")
        assert file_utils.get_protect_patterns(p, logger) == []
        logger.warning.assert_called()

    def test_comments_and_blanks_ignored(self, tmp_path, logger):
        """Lines starting with '#' and blank lines are skipped."""
        p = tmp_path / ".deploy_protect"
        p.write_text("# this is a comment\n\ntokens.json\n   \nserver.properties\n# another comment\n")
        assert file_utils.get_protect_patterns(p, logger) == [
            "tokens.json",
            "server.properties",
        ]

    def test_trailing_slash_stripped(self, tmp_path, logger):
        """A single trailing slash is removed, so 'world/' matches like 'world'."""
        p = tmp_path / ".deploy_protect"
        p.write_text("world/\n")
        assert file_utils.get_protect_patterns(p, logger) == ["world"]


class TestCopyWithExclusionsClean:
    """copy_with_exclusions with clean=True must not delete protected files."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create empty src and dst directories under tmp_path."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        return src, dst

    def test_clean_deletes_unprotected_stale_files(self, tmp_path, logger):
        """Files present only in the target are removed by clean."""
        src, dst = self._setup(tmp_path)
        _write(dst, "stale.txt")
        _write(dst, "subdir/also_stale.txt")
        _write(src, "managed.txt", "new")

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True)

        assert not (dst / "stale.txt").exists()
        assert not (dst / "subdir/also_stale.txt").exists()
        assert (dst / "managed.txt").read_text() == "new"

    def test_clean_preserves_protected_files(self, tmp_path, logger):
        """Files matching a protect pattern survive clean."""
        src, dst = self._setup(tmp_path)
        _write(dst, "config/auth/tokens.json", '{"secret": "keep-me"}')
        _write(dst, "server.properties", "motd=keep-me")
        _write(dst, "stale.txt")

        file_utils.copy_with_exclusions(
            src,
            dst,
            [],
            logger,
            clean=True,
            protect_patterns=["tokens.json", "server.properties"],
        )

        assert (dst / "config/auth/tokens.json").read_text() == '{"secret": "keep-me"}'
        assert (dst / "server.properties").read_text() == "motd=keep-me"
        assert not (dst / "stale.txt").exists()

    def test_clean_preserves_excluded_files(self, tmp_path, logger):
        """Excluded files are never deleted either."""
        src, dst = self._setup(tmp_path)
        _write(dst, "keep.tmp")
        _write(dst, "stale.txt")

        file_utils.copy_with_exclusions(src, dst, ["*.tmp"], logger, clean=True)

        assert (dst / "keep.tmp").exists()
        assert not (dst / "stale.txt").exists()

    def test_clean_preserves_protected_directories(self, tmp_path, logger):
        """A protected directory and its contents survive clean."""
        src, dst = self._setup(tmp_path)
        _write(dst, "world/level.dat", "world data")
        _write(dst, "world/region/r.0.0.mca", "region data")
        _write(dst, "stale.txt")

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True, protect_patterns=["world"])

        assert (dst / "world/level.dat").exists()
        assert (dst / "world/region/r.0.0.mca").exists()
        assert not (dst / "stale.txt").exists()

    def test_no_clean_preserves_everything(self, tmp_path, logger):
        """clean=False never deletes anything, protected or not."""
        src, dst = self._setup(tmp_path)
        _write(dst, "stale.txt")
        _write(src, "managed.txt", "new")

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=False)

        assert (dst / "stale.txt").exists()
        assert (dst / "managed.txt").read_text() == "new"

    def test_clean_copies_new_source_files(self, tmp_path, logger):
        """Files in source but not target are copied in."""
        src, dst = self._setup(tmp_path)
        _write(src, "new_file.txt", "content")
        _write(src, "new_dir/nested.txt", "nested")

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True)

        assert (dst / "new_file.txt").read_text() == "content"
        assert (dst / "new_dir/nested.txt").read_text() == "nested"

    def test_clean_overwrites_existing_source_managed_files(self, tmp_path, logger):
        """A file present in both source and target takes the source content."""
        src, dst = self._setup(tmp_path)
        _write(src, "config.toml", "NEW")
        _write(dst, "config.toml", "OLD")

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True)

        assert (dst / "config.toml").read_text() == "NEW"

    def test_clean_creates_missing_target_directory(self, tmp_path, logger):
        """If the target directory doesn't exist, it is created."""
        src = tmp_path / "src"
        src.mkdir()
        _write(src, "file.txt", "content")
        dst = tmp_path / "does-not-exist"

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True)

        assert (dst / "file.txt").read_text() == "content"

    def test_clean_preserves_empty_protected_directories(self, tmp_path, logger):
        """An empty directory matching a protect pattern survives clean."""
        src, dst = self._setup(tmp_path)
        (dst / "world").mkdir()

        file_utils.copy_with_exclusions(src, dst, [], logger, clean=True, protect_patterns=["world"])

        assert (dst / "world").is_dir()
