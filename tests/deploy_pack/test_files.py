# tests/deploy_pack/test_files.py

"""Tests for deploy_pack.files, per Project_Specs.md v3.0 §10.1.

Required coverage (§10.1):
  * protect
  * merge
  * delete
  * unchanged files not recopied
  * missing source
  * copy failure
  * atomic publication metadata
  * fresh-destination metadata defaults
  * os.chown failure warns
  * os.chmod failure warns

Additional coverage for §4.10, §4.11, §7.1, §7.5:
  * create_zip: round-trip, atomic (partial zips never visible)
  * atomic_copy: fresh, replace, metadata, missing source
  * deploy_flat_files: flat-jar semantics, protect, non-jar ignore
  * is_protected_path: full path vs component match, glob spans '/'
  * load_protect_patterns: missing is silent, empty warns, comments
  * @www grammar: all valid and invalid examples from §7.5
  * resource-pack filename and URL validation
  * resolve_mapping_for_side: str, dict, exclusion, -1
"""

from __future__ import annotations

import logging
import os
import shutil as shutil_mod
import zipfile
from pathlib import Path

import pytest

from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.files import (
    atomic_copy,
    atomic_write,
    build_resource_pack_url,
    compute_sha1,
    compute_sha256,
    copy_tree,
    create_zip,
    deploy_flat_files,
    is_protected_path,
    is_shared_dest,
    load_protect_patterns,
    parse_shared_dest,
    resolve_mapping_for_side,
    resolve_resource_pack_dest,
    resolve_shared_dest,
    sha256_bytes,
    validate_resource_pack_filename,
)


def _tree(root: Path, structure: dict[str, str]) -> None:
    """Build a directory tree from {relative_path: content}."""
    for rel, content in structure.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _read_tree(root: Path) -> dict[str, str]:
    """Return {relative_path: content} for every file under root."""
    result: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            full = Path(dirpath) / f
            rel = full.relative_to(root)
            result[str(rel).replace(os.sep, "/")] = full.read_text(encoding="utf-8")
    return result


def test_atomic_write_creates_fresh_file(tmp_path: Path) -> None:
    """Verifies that atomic_write creates a new file with the given contents."""
    p = tmp_path / "out.bin"
    atomic_write(p, b"hello")
    assert p.read_bytes() == b"hello"


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    """Tests that atomic_write replaces the contents of an existing destination file."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")
    atomic_write(p, b"new")
    assert p.read_bytes() == b"new"


def test_atomic_write_creates_parent_dir(tmp_path: Path) -> None:
    """Tests that atomic_write creates missing parent directories for the destination path."""
    p = tmp_path / "sub" / "nested" / "out.bin"
    atomic_write(p, b"x")
    assert p.read_bytes() == b"x"


def test_atomic_write_preserves_destination_mode(tmp_path: Path) -> None:
    """Tests that atomic_write preserves the existing destination file mode."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")
    os.chmod(p, 0o640)
    atomic_write(p, b"new")
    assert p.stat().st_mode & 0o777 == 0o640


def test_atomic_write_fresh_destination_uses_default_mode(tmp_path: Path) -> None:
    """Tests that atomic_write applies the default file mode when creating a fresh destination under a known umask."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    old_umask = os.umask(0o022)
    try:
        p = tmp_path / "fresh.bin"
        atomic_write(p, b"x")
        mode = p.stat().st_mode & 0o777
        assert mode == 0o644
    finally:
        os.umask(old_umask)


def test_atomic_write_chown_failure_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that atomic_write emits a warning when chown fails but still writes the data."""
    records: list[str] = []

    class Recorder(logging.Logger):
        def warning(self, msg, *args, **kwargs):
            """Records a warning message by converting it to a string and appending it."""
            records.append(str(msg))

    logger = Recorder("test")
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(*_args, **_kwargs):
        raise PermissionError("simulated EPERM")

    monkeypatch.setattr(os, "chown", boom)
    atomic_write(p, b"new", logger=logger)
    assert p.read_bytes() == b"new"
    assert any("chown" in r for r in records)


def test_atomic_write_chmod_failure_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that atomic_write still writes and warns when chmod fails."""
    records: list[str] = []

    class Recorder(logging.Logger):
        def warning(self, msg, *args, **kwargs):
            """Records a warning message by converting it to a string and appending it."""
            records.append(str(msg))

    logger = Recorder("test")
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(*_args, **_kwargs):
        raise PermissionError("simulated EPERM")

    monkeypatch.setattr(os, "chmod", boom)
    atomic_write(p, b"new", logger=logger)
    assert p.read_bytes() == b"new"
    assert any("chmod" in r for r in records)


def test_atomic_write_temp_cleaned_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that a failed atomic_write preserves the original file and removes temp files."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, b"new")
    assert p.read_bytes() == b"old"
    assert list(tmp_path.glob("out.bin.tmp.*")) == []


def test_atomic_copy_creates_fresh(tmp_path: Path) -> None:
    """Tests that atomic_copy creates a fresh destination file."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    dest = tmp_path / "out.bin"
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"hello"


def test_atomic_copy_replaces_existing(tmp_path: Path) -> None:
    """Tests that atomic_copy overwrites an existing destination file with the source contents."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new")
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"new"


def test_atomic_copy_preserves_dest_mode(tmp_path: Path) -> None:
    """Tests that atomic_copy preserves the existing permission mode of the destination file."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    os.chmod(dest, 0o640)
    atomic_copy(src, dest)
    assert dest.stat().st_mode & 0o777 == 0o640


def test_atomic_copy_missing_source(tmp_path: Path) -> None:
    """Tests that atomic_copy raises FileNotFoundError when the source file does not exist."""
    with pytest.raises(FileNotFoundError):
        atomic_copy(tmp_path / "nope.bin", tmp_path / "out.bin")


def test_atomic_copy_creates_parent_dir(tmp_path: Path) -> None:
    """Tests that atomic_copy creates missing parent directories for the destination file."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dest = tmp_path / "sub" / "nested" / "out.bin"
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"x"


def test_create_zip_roundtrip(tmp_path: Path) -> None:
    """Tests that create_zip produces a readable zip preserving names and contents.

    Args:
        tmp_path: Temporary directory for the source tree and output zip.
    """
    src = tmp_path / "stage"
    _tree(src, {"a.txt": "one", "sub/b.txt": "two"})
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"a.txt", "sub/b.txt"}
        assert zf.read("a.txt") == b"one"
        assert zf.read("sub/b.txt") == b"two"


def test_create_zip_flat_layout(tmp_path: Path) -> None:
    """Archive paths are relative to source_dir, no leading component."""
    src = tmp_path / "stage"
    _tree(src, {"mods/foo.jar": "jardata"})
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["mods/foo.jar"]


def test_create_zip_missing_source(tmp_path: Path) -> None:
    """Tests that create_zip raises NotADirectoryError when the source directory is missing.

    Args:
        tmp_path: Temporary directory used to build nonexistent source and output paths.
    """
    with pytest.raises(NotADirectoryError):
        create_zip(tmp_path / "nope", tmp_path / "out.zip")


def test_create_zip_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that create_zip is atomic and cleans up on failure.

    Simulates an os.replace failure and verifies no output zip or temporary files remain.

    Args:
        tmp_path: Temporary directory for the source tree and output zip.
        monkeypatch: Pytest fixture used to patch os.replace to raise OSError.
    """
    src = tmp_path / "stage"
    _tree(src, {"a.txt": "one"})
    out = tmp_path / "pack.zip"

    def boom(src_path, dst_path):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        create_zip(src, out)
    assert not out.exists()
    assert list(tmp_path.glob("pack.zip.tmp.*")) == []


def test_create_zip_empty_source_still_builds(tmp_path: Path) -> None:
    """§7.1: empty source produces an empty ZIP; the ZIP is still built."""
    src = tmp_path / "empty"
    src.mkdir()
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == []


def test_compute_sha256(tmp_path: Path) -> None:
    """Tests that compute_sha256 returns the correct SHA-256 digest for a known file.

    Args:
        tmp_path: Temporary directory in which to create the test file.
    """
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert compute_sha256(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_compute_sha1(tmp_path: Path) -> None:
    """Tests that compute_sha1 returns the correct SHA-1 digest for a known file.

    Args:
        tmp_path: Temporary directory in which to create the test file.
    """
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert compute_sha1(p) == "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"


def test_sha256_bytes() -> None:
    """Tests that sha256_bytes returns the correct SHA-256 hex digest for a known input."""
    assert sha256_bytes(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_protect_empty_patterns() -> None:
    """Tests that is_protected_path returns False when the patterns collection is empty or None."""
    assert is_protected_path("any/file", []) is False
    assert is_protected_path("any/file", None) is False


def test_protect_full_path_match() -> None:
    """Tests that is_protected_path matches when the pattern is the full path."""
    assert is_protected_path("config/tokens.json", ["config/tokens.json"])


def test_protect_component_match() -> None:
    """Tests that a bare filename pattern matches that filename at any depth in the path."""
    assert is_protected_path("a/b/tokens.json", ["tokens.json"])
    assert is_protected_path("deeply/nested/tokens.json", ["tokens.json"])


def test_protect_glob_spans_slash() -> None:
    """Fnmatch's * spans /, matching §3.13's documented behavior."""
    assert is_protected_path("config/foo.key", ["*.key"])
    assert is_protected_path("foo.key", ["*.key"])


def test_protect_multilevel_glob() -> None:
    """Tests that is_protected_path supports single-level glob patterns matching intermediate path components."""
    assert is_protected_path("config/sub/tokens.json", ["config/*/tokens.json"])
    assert not is_protected_path("config/a/b/tokens.json", ["config/*/tokens.json"])


def test_protect_no_match() -> None:
    """Tests that is_protected_path returns False when no pattern matches the given path."""
    assert not is_protected_path("normal/file.txt", ["tokens.json"])


def test_protect_path_object() -> None:
    """Tests that a Path object is correctly identified as protected when matching a glob pattern."""
    assert is_protected_path(Path("a/b/c.key"), ["*.key"])


def test_protect_missing_file_is_silent_noop(tmp_path: Path) -> None:
    """§3.13: missing .deploy_protect is a silent no-op, NOT an error."""
    assert load_protect_patterns(tmp_path / "nope") == []


def test_protect_none_path() -> None:
    """Tests that passing None as the protect patterns path returns an empty list."""
    assert load_protect_patterns(None) == []


def test_protect_basic(tmp_path: Path) -> None:
    """Tests that basic protect patterns are parsed correctly, ignoring comments and trimming whitespace."""
    p = tmp_path / ".deploy_protect"
    p.write_text("# a comment\n\ntokens.json\n  config/secrets.yaml  \n*.key\n", encoding="utf-8")
    patterns = load_protect_patterns(p)
    assert patterns == ["tokens.json", "config/secrets.yaml", "*.key"]


def test_protect_trailing_slash_normalized(tmp_path: Path) -> None:
    """Tests that trailing slashes in protect patterns are stripped during normalization."""
    p = tmp_path / ".deploy_protect"
    p.write_text("world/\n", encoding="utf-8")
    assert load_protect_patterns(p) == ["world"]


def test_protect_empty_file_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Tests that loading a protect patterns file containing only comments logs a warning and returns an empty list."""
    p = tmp_path / ".deploy_protect"
    p.write_text("# only comments\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        patterns = load_protect_patterns(p, logger=logging.getLogger("test"))
    assert patterns == []
    assert any("empty" in r.message for r in caplog.records)


def test_merge_copies_new_files(tmp_path: Path) -> None:
    """Tests that merge mode copies new files and reports them as added.

    Args:
        tmp_path (Path): Temporary directory provided by pytest.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "b.txt": "2"})
    dst.mkdir()
    result = copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "1", "b.txt": "2"}
    assert set(result.added) == {"a.txt", "b.txt"}
    assert result.updated == []
    assert result.removed == []


def test_merge_keeps_extras(tmp_path: Path) -> None:
    """Merge mode does not remove files that aren't in src."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "extra.txt": "keepme"})
    result = copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "1", "extra.txt": "keepme"}
    assert result.removed == []


def test_merge_updates_changed_content(tmp_path: Path) -> None:
    """§4.4: an updated file is not reported as removed.

    Files present in both trees with differing content are updated in
    place - one atomic overwrite, not an unlink-then-create.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "new"})
    _tree(dst, {"a.txt": "old"})
    result = copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "new"}
    assert result.updated == ["a.txt"]
    assert result.removed == []


def test_merge_unchanged_files_not_touched(tmp_path: Path) -> None:
    """Tests that merge mode leaves identical files untouched and reports them as unchanged.

    Args:
        tmp_path (Path): Temporary directory provided by pytest.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "same"})
    _tree(dst, {"a.txt": "same"})
    before = (dst / "a.txt").stat().st_mtime_ns
    result = copy_tree(src, dst, mode="merge")
    assert result.unchanged == ["a.txt"]
    assert result.added == []
    assert result.updated == []
    assert (dst / "a.txt").stat().st_mtime_ns == before


def test_delete_removes_extras(tmp_path: Path) -> None:
    """Tests that delete mode removes extra files and reports them as removed.

    Args:
        tmp_path (Path): Temporary directory provided by pytest.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "extra.txt": "gone"})
    result = copy_tree(src, dst, mode="delete")
    assert _read_tree(dst) == {"a.txt": "1"}
    assert result.removed == ["extra.txt"]


def test_delete_removes_stale_directories(tmp_path: Path) -> None:
    """Tests that delete mode removes directories no longer present in the source.

    Args:
        tmp_path (Path): Temporary directory provided by pytest.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "sub/deep/stale.txt": "x"})
    copy_tree(src, dst, mode="delete")
    assert not (dst / "sub").exists()


def test_delete_keeps_nonempty_directories(tmp_path: Path) -> None:
    """Tests that delete mode removes extra files while preserving nonempty directories and their shared contents."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "keep/inner.txt": "y"})
    _tree(dst, {"a.txt": "1", "keep/inner.txt": "y", "keep/other.txt": "z"})
    copy_tree(src, dst, mode="delete")
    assert (dst / "keep" / "inner.txt").is_file()
    assert not (dst / "keep" / "other.txt").exists()


def test_protect_keeps_extra_in_delete_mode(tmp_path: Path) -> None:
    """Tests that protected extra files are kept and reported in delete mode."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "tokens.json": "secret"})
    result = copy_tree(src, dst, mode="delete", protect_patterns=["tokens.json"])
    assert (dst / "tokens.json").is_file()
    assert result.protected_kept == ["tokens.json"]


def test_protect_still_allows_overwrite(tmp_path: Path) -> None:
    """§3.13: protection governs deletion, not overwrite."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"tokens.json": "from-source"})
    _tree(dst, {"tokens.json": "old"})
    copy_tree(src, dst, mode="delete", protect_patterns=["tokens.json"])
    assert (dst / "tokens.json").read_text() == "from-source"


def test_protect_directory_component(tmp_path: Path) -> None:
    """Tests that a protected pattern keeps a directory and its contents intact."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "world/level.dat": "x"})
    copy_tree(src, dst, mode="delete", protect_patterns=["world"])
    assert (dst / "world" / "level.dat").is_file()
    assert (dst / "world").is_dir()


def test_protect_deep_nested_file(tmp_path: Path) -> None:
    """Tests that a protected pattern keeps a deeply nested file in delete mode."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "very/deep/secret.key": "x"})
    copy_tree(src, dst, mode="delete", protect_patterns=["*.key"])
    assert (dst / "very" / "deep" / "secret.key").is_file()


def test_copy_tree_missing_source(tmp_path: Path) -> None:
    """Tests that copy_tree raises NotADirectoryError when the source path does not exist."""
    with pytest.raises(NotADirectoryError):
        copy_tree(tmp_path / "nope", tmp_path / "dst")


def test_copy_tree_invalid_mode(tmp_path: Path) -> None:
    """Tests that copy_tree raises ValueError when given an invalid mode.

    Args:
        tmp_path (Path): Temporary directory provided by pytest.

    Raises:
        ValueError: If mode is not one of the supported values.
    """
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValueError):
        copy_tree(src, tmp_path / "dst", mode="overwrite")


def test_copy_tree_creates_dst(tmp_path: Path) -> None:
    """Tests that copy_tree creates the destination directory when it does not exist."""
    src = tmp_path / "src"
    _tree(src, {"a.txt": "1"})
    dst = tmp_path / "new_dst"
    copy_tree(src, dst, mode="delete")
    assert (dst / "a.txt").is_file()


def test_copy_tree_copy_failure_logged_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Tests that a copy failure is logged as a warning and the tree copy continues."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "b.txt": "2"})
    dst.mkdir()
    real_copy2 = shutil_mod.copy2
    call_count = {"n": 0}

    def flaky(src_p, dst_p, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated copy failure")
        return real_copy2(src_p, dst_p, *args, **kwargs)

    monkeypatch.setattr(shutil_mod, "copy2", flaky)
    with caplog.at_level(logging.WARNING):
        result = copy_tree(src, dst, mode="delete", logger=logging.getLogger("test"))
    assert len(result.added) == 1
    assert any("copy" in r.message and "failed" in r.message for r in caplog.records)


def test_flat_new_files_copied(tmp_path: Path) -> None:
    """Tests that new files are copied into the destination during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x", "b.jar": "y"})
    dst = tmp_path / "mods"
    result = deploy_flat_files({"a.jar": src_dir / "a.jar", "b.jar": src_dir / "b.jar"}, dst)
    assert sorted(result.added) == ["a.jar", "b.jar"]
    assert (dst / "a.jar").read_text() == "x"


def test_flat_extras_removed(tmp_path: Path) -> None:
    """Tests that stale extra files in the destination are removed during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "stale.jar": "y"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.removed == ["stale.jar"]
    assert not (dst / "stale.jar").exists()
    assert (dst / "a.jar").is_file()


def test_flat_protected_extras_survive(tmp_path: Path) -> None:
    """Tests that protected extra files in the destination survive flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "keep.jar": "y"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst, protect_patterns=["keep.jar"])
    assert result.protected_kept == ["keep.jar"]
    assert (dst / "keep.jar").is_file()


def test_flat_protected_in_source_still_overwritten(tmp_path: Path) -> None:
    """§3.13: protection governs deletion, not overwrite."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"keep.jar": "new content"})
    dst = tmp_path / "mods"
    _tree(dst, {"keep.jar": "old content"})
    deploy_flat_files({"keep.jar": src_dir / "keep.jar"}, dst, protect_patterns=["keep.jar"])
    assert (dst / "keep.jar").read_text() == "new content"


def test_flat_updated_in_place(tmp_path: Path) -> None:
    """Tests that changed files are updated in place during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "new"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "old"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.updated == ["a.jar"]
    assert result.removed == []
    assert (dst / "a.jar").read_text() == "new"


def test_flat_unchanged_not_touched(tmp_path: Path) -> None:
    """Tests that unchanged files are not modified during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "same"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "same"})
    before = (dst / "a.jar").stat().st_mtime_ns
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.unchanged == ["a.jar"]
    assert (dst / "a.jar").stat().st_mtime_ns == before


def test_flat_non_jar_files_ignored(tmp_path: Path) -> None:
    """§2.9, §4.11: only *.jar is in the effective mod set."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "readme.txt": "notes"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "readme.txt").is_file()
    assert result.removed == []


def test_flat_subdirectories_ignored(tmp_path: Path) -> None:
    """Tests that subdirectories in the destination are ignored during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    (dst / "disabled").mkdir(parents=True)
    (dst / "disabled" / "b.jar").write_text("y")
    _tree(dst, {"a.jar": "x"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "disabled" / "b.jar").is_file()
    assert result.removed == []


def test_flat_creates_dst(tmp_path: Path) -> None:
    """Tests that the destination directory is created during flat deployment."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "new_mods"
    deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "a.jar").is_file()


def test_flat_missing_source_skipped(tmp_path: Path) -> None:
    """Tests that missing source files are skipped during flat deployment."""
    dst = tmp_path / "mods"
    result = deploy_flat_files({"ghost.jar": tmp_path / "nope.jar"}, dst)
    assert result.added == []
    assert result.updated == []


def test_is_shared_dest() -> None:
    """Tests identification of shared destination strings."""
    assert is_shared_dest("@www/foo")
    assert not is_shared_dest("foo/bar")
    assert not is_shared_dest("")


@pytest.mark.parametrize("value,expected_subpath", [("@www/resourcepacks", "resourcepacks"), ("@www/packs/2026", "packs/2026")])
def test_parse_shared_dest_valid(value: str, expected_subpath: str) -> None:
    """Tests that parsing a valid shared destination returns the expected prefix and subpath."""
    prefix, subpath = parse_shared_dest(value)
    assert prefix == "www"
    assert subpath == expected_subpath


@pytest.mark.parametrize("value", ["@www", "@www/", "@www//foo", "@www/./foo", "@www/../foo", "@www/foo/", "@www/a//b", "@mods/foo", "@", "www/foo"])
def test_parse_shared_dest_invalid(value: str) -> None:
    """Tests that parsing invalid shared destination values raises ConfigError."""
    with pytest.raises(ConfigError):
        parse_shared_dest(value)


def test_parse_shared_dest_nul_rejected() -> None:
    """Tests that parsing a shared destination containing a NUL character raises ConfigError."""
    with pytest.raises(ConfigError):
        parse_shared_dest("@www/foo\x00bar")


def test_resolve_shared_dest(tmp_path: Path) -> None:
    """Tests that shared destinations are resolved to the correct paths under a base directory."""
    assert resolve_shared_dest("@www/resourcepacks", tmp_path) == tmp_path / "resourcepacks"
    assert resolve_shared_dest("@www/packs/2026", tmp_path) == tmp_path / "packs" / "2026"


def test_resolve_shared_dest_invalid() -> None:
    """Tests that resolve_shared_dest raises ConfigError for an invalid shared destination."""
    with pytest.raises(ConfigError):
        resolve_shared_dest("@mods/foo", Path("/tmp"))


def test_mapping_string_used_for_both_sides() -> None:
    """Tests that a string mapping is used for both client and server sides."""
    assert resolve_mapping_for_side("config", "client") == "config"
    assert resolve_mapping_for_side("config", "server") == "config"


def test_mapping_dict_side_specific() -> None:
    """Tests that a dict mapping resolves side-specific values for client and server."""
    m = {"server": "server_cfg", "client": "client_cfg"}
    assert resolve_mapping_for_side(m, "server") == "server_cfg"
    assert resolve_mapping_for_side(m, "client") == "client_cfg"


def test_mapping_dict_missing_side_is_none() -> None:
    """Tests that a missing side key in a mapping dict resolves to None."""
    m = {"server": "server_cfg"}
    assert resolve_mapping_for_side(m, "client") is None


def test_mapping_dict_explicit_none_is_none() -> None:
    """Tests that an explicit None value in a mapping dict resolves to None."""
    m = {"server": None}
    assert resolve_mapping_for_side(m, "server") is None


def test_mapping_dict_minus_one_is_none() -> None:
    """Tests that a -1 value in a mapping dict resolves to None."""
    m = {"server": -1}
    assert resolve_mapping_for_side(m, "server") is None


def test_mapping_dict_non_string_value_is_none() -> None:
    """Tests that a non-string value in a mapping dict resolves to None."""
    m = {"server": 42}
    assert resolve_mapping_for_side(m, "server") is None


def test_mapping_non_str_non_dict_is_none() -> None:
    """Tests that non-string, non-dict mappings resolve to None."""
    assert resolve_mapping_for_side(None, "client") is None
    assert resolve_mapping_for_side([1, 2], "client") is None


@pytest.mark.parametrize("name", ["pack.zip", "creative.zip", "my_pack_v2.zip", "a.b.c.zip"])
def test_rp_filename_valid(name: str) -> None:
    """Tests that a valid resource pack filename passes validation."""
    validate_resource_pack_filename(name)


@pytest.mark.parametrize("name", ["", "pack", "pack.tar.gz", "pack.zip/", "dir/pack.zip", "dir\\pack.zip", "..zip", ".hidden.zip", "pack..zip", "pack\x00.zip"])
def test_rp_filename_invalid(name: str) -> None:
    """Tests that an invalid resource pack filename raises a ConfigError."""
    with pytest.raises(ConfigError):
        validate_resource_pack_filename(name)


def test_build_url_basic() -> None:
    """Tests that a resource pack URL is built correctly from a basic mapping."""
    url = build_resource_pack_url("http://minecraft/downloads", "@www/resourcepacks", "pack.zip")
    assert url == "http://minecraft/downloads/resourcepacks/pack.zip"


def test_build_url_strips_trailing_base_slash() -> None:
    """Tests that a trailing slash on the base URL is stripped when building a URL."""
    url = build_resource_pack_url("http://minecraft/downloads/", "@www/packs", "pack.zip")
    assert url == "http://minecraft/downloads/packs/pack.zip"


def test_build_url_deep_subpath() -> None:
    """Tests that a URL is built correctly from a deep subpath mapping."""
    url = build_resource_pack_url("http://x", "@www/packs/2026", "pack.zip")
    assert url == "http://x/packs/2026/pack.zip"


def test_build_url_empty_base() -> None:
    """Tests that building a resource pack URL with an empty base URL raises a ConfigError."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("", "@www/resourcepacks", "pack.zip")


def test_build_url_bad_mapping() -> None:
    """Tests that building a resource pack URL with an invalid path mapping raises a ConfigError."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("http://x", "@mods/foo", "pack.zip")


def test_build_url_bad_filename() -> None:
    """Tests that build_resource_pack_url raises ConfigError for an invalid filename."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("http://x", "@www/resourcepacks", "pack.tar.gz")


def test_resolve_rp_dest(tmp_path: Path) -> None:
    """Tests that resolve_resource_pack_dest resolves @www/resourcepacks to the resourcepacks directory."""
    assert resolve_resource_pack_dest("@www/resourcepacks", tmp_path) == tmp_path / "resourcepacks"


def test_resolve_rp_dest_invalid() -> None:
    """Tests that resolve_resource_pack_dest raises ConfigError for an invalid destination."""
    with pytest.raises(ConfigError):
        resolve_resource_pack_dest("@mods/foo", Path("/tmp"))
