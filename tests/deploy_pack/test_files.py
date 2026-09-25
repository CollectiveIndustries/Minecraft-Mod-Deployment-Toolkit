# tests/deploy_pack/test_files.py

"""Tests for deploy_pack.files, Project_Specs.md §3.13, §4.10, §4.11, §7.1, §7.2, §7.5.

Every test traces to a specific clause:

  * §3.13 - protect-file syntax and matching semantics
  * §4.10 - atomic publication: same-directory temp, fsync, mode and
            ownership preservation, os.replace, no directory fsync
  * §4.11 - mods_dir is flat; protected files outside the source set
            survive the clean and are not reported as deployment deltas
  * §7.1  - client ZIP is built by zipping a staging directory; archive
            paths are relative to the staging root
  * §7.2  - merge vs. delete clean semantics; unchanged files untouched
  * §7.5  - @www/... grammar; resource-pack URL construction; filename
            validation rules

Protect patterns govern deletion, not overwrite: a protected file that
also appears in the source tree is overwritten with the source content
(§3.13). ``@www/...`` mapping values are filesystem paths and URL
sub-paths at the same time; the grammar is enforced by
:func:`parse_shared_dest`.
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
    hash_flat_dir,
    hash_tree,
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
    """Populate a directory tree from a {relative_path: content} mapping."""
    for rel, content in structure.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _read_tree(root: Path) -> dict[str, str]:
    """Return {relative_path: content} for every file under ``root``."""
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            full = Path(dirpath) / f
            rel = full.relative_to(root)
            out[str(rel).replace(os.sep, "/")] = full.read_text(encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# §4.10: atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_fresh_file(tmp_path: Path) -> None:
    """§4.10: atomic_write publishes the data at the destination path."""
    p = tmp_path / "out.bin"
    atomic_write(p, b"hello")
    assert p.read_bytes() == b"hello"


def test_atomic_write_creates_missing_parent_directory(tmp_path: Path) -> None:
    """§4.10: the destination's parent directory is created if absent."""
    p = tmp_path / "sub" / "nested" / "out.bin"
    atomic_write(p, b"x")
    assert p.read_bytes() == b"x"


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    """§4.10: an existing destination is replaced with the new data."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")
    atomic_write(p, b"new")
    assert p.read_bytes() == b"new"


def test_atomic_write_preserves_destination_mode(tmp_path: Path) -> None:
    """§4.10: for an existing destination, its mode is copied to the temp file."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")
    os.chmod(p, 0o640)
    atomic_write(p, b"new")
    assert p.stat().st_mode & 0o777 == 0o640


def test_atomic_write_fresh_destination_uses_process_umask(tmp_path: Path) -> None:
    """§4.10: a fresh destination gets the process umask's default mode."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    old_umask = os.umask(0o022)
    try:
        p = tmp_path / "fresh.bin"
        atomic_write(p, b"x")
        assert p.stat().st_mode & 0o777 == 0o644
    finally:
        os.umask(old_umask)


def test_atomic_write_temp_is_in_same_directory_as_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.10: temp_path = <target_dir>/<target_name>.tmp.<pid>."""
    captured: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def capture(src: object, dst: object) -> None:
        captured.append((Path(str(src)), Path(str(dst))))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", capture)
    p = tmp_path / "sub" / "out.bin"
    atomic_write(p, b"x")
    ((src, dst),) = captured
    assert src.parent == dst.parent == tmp_path / "sub"
    assert src.name.startswith("out.bin.tmp.")


def test_atomic_write_chown_failure_warns_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """§4.10: os.chown failure logs a WARN and the write still succeeds."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated EPERM")

    monkeypatch.setattr(os, "chown", boom)
    with caplog.at_level(logging.WARNING):
        atomic_write(p, b"new", logger=logging.getLogger("test"))
    assert p.read_bytes() == b"new"
    assert any("chown" in r.message for r in caplog.records)


def test_atomic_write_chmod_failure_warns_and_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """§4.10: os.chmod failure logs a WARN and the write still succeeds."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated EPERM")

    monkeypatch.setattr(os, "chmod", boom)
    with caplog.at_level(logging.WARNING):
        atomic_write(p, b"new", logger=logging.getLogger("test"))
    assert p.read_bytes() == b"new"
    assert any("chmod" in r.message for r in caplog.records)


def test_atomic_write_cleans_temp_and_preserves_original_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.10: a failure during os.replace removes the temp file and leaves the original intact."""
    p = tmp_path / "out.bin"
    p.write_bytes(b"old")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, b"new")
    assert p.read_bytes() == b"old"
    assert list(tmp_path.glob("out.bin.tmp.*")) == []


# ---------------------------------------------------------------------------
# §4.10: atomic_copy
# ---------------------------------------------------------------------------


def test_atomic_copy_creates_fresh_destination(tmp_path: Path) -> None:
    """§4.10: atomic_copy publishes the source's content at the destination."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    dest = tmp_path / "out.bin"
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"hello"


def test_atomic_copy_replaces_existing(tmp_path: Path) -> None:
    """§4.10: an existing destination is replaced with the source's content."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new")
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"new"


def test_atomic_copy_preserves_destination_mode(tmp_path: Path) -> None:
    """§4.10: the existing destination's mode is preserved across the replace."""
    if os.name == "nt":
        pytest.skip("POSIX modes are not meaningful on Windows")
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    os.chmod(dest, 0o640)
    atomic_copy(src, dest)
    assert dest.stat().st_mode & 0o777 == 0o640


def test_atomic_copy_missing_source_raises_file_not_found(tmp_path: Path) -> None:
    """§4.10: a missing source is a FileNotFoundError before any write."""
    with pytest.raises(FileNotFoundError):
        atomic_copy(tmp_path / "nope.bin", tmp_path / "out.bin")


def test_atomic_copy_creates_parent_directory(tmp_path: Path) -> None:
    """§4.10: the destination's parent directory is created if absent."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    dest = tmp_path / "sub" / "nested" / "out.bin"
    atomic_copy(src, dest)
    assert dest.read_bytes() == b"x"


# ---------------------------------------------------------------------------
# §4.10 + §7.1: create_zip
# ---------------------------------------------------------------------------


def test_create_zip_round_trip(tmp_path: Path) -> None:
    """§7.1: archive paths are relative to the staging root; content round-trips."""
    src = tmp_path / "stage"
    _tree(src, {"a.txt": "one", "sub/b.txt": "two"})
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"a.txt", "sub/b.txt"}
        assert zf.read("a.txt") == b"one"
        assert zf.read("sub/b.txt") == b"two"


def test_create_zip_uses_forward_slashes_in_archive_names(tmp_path: Path) -> None:
    """§7.1: archive paths use forward slashes regardless of the host OS."""
    src = tmp_path / "stage"
    _tree(src, {"mods/foo.jar": "jardata"})
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["mods/foo.jar"]


def test_create_zip_missing_source_raises_not_a_directory(tmp_path: Path) -> None:
    """§7.1: a missing staging root is a NotADirectoryError."""
    with pytest.raises(NotADirectoryError):
        create_zip(tmp_path / "nope", tmp_path / "out.zip")


def test_create_zip_empty_source_still_builds_a_zip(tmp_path: Path) -> None:
    """§7.1: empty source directories produce an empty ZIP; the ZIP is still built."""
    src = tmp_path / "empty"
    src.mkdir()
    out = tmp_path / "pack.zip"
    create_zip(src, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == []


def test_create_zip_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.10: a failure during publication leaves no partial ZIP behind."""
    src = tmp_path / "stage"
    _tree(src, {"a.txt": "one"})
    out = tmp_path / "pack.zip"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        create_zip(src, out)
    assert not out.exists()
    assert list(tmp_path.glob("pack.zip.tmp.*")) == []


# ---------------------------------------------------------------------------
# §3.13: load_protect_patterns
# ---------------------------------------------------------------------------


def test_protect_missing_file_is_silent_noop(tmp_path: Path) -> None:
    """§3.13: a missing .deploy_protect file is a silent no-op, not an error."""
    assert load_protect_patterns(tmp_path / "nope") == []


def test_protect_none_path_is_silent_noop() -> None:
    """§3.13: a None path (no protect file configured) is a silent no-op."""
    assert load_protect_patterns(None) == []


def test_protect_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    """§3.13: comments and blank lines are ignored; whitespace is stripped."""
    p = tmp_path / ".deploy_protect"
    p.write_text("# a comment\n\ntokens.json\n  config/secrets.yaml  \n*.key\n", encoding="utf-8")
    assert load_protect_patterns(p) == ["tokens.json", "config/secrets.yaml", "*.key"]


def test_protect_strips_single_trailing_slash(tmp_path: Path) -> None:
    """§3.13: world/ is normalized to world."""
    p = tmp_path / ".deploy_protect"
    p.write_text("world/\n", encoding="utf-8")
    assert load_protect_patterns(p) == ["world"]


def test_protect_empty_file_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§3.13: an empty file (after comments) logs a WARN."""
    p = tmp_path / ".deploy_protect"
    p.write_text("# only comments\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        patterns = load_protect_patterns(p, logger=logging.getLogger("test"))
    assert patterns == []
    assert any("empty" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# §3.13: is_protected_path
# ---------------------------------------------------------------------------


def test_protect_empty_or_none_patterns_match_nothing() -> None:
    """§3.13: an empty pattern list protects nothing."""
    assert is_protected_path("any/file", []) is False
    assert is_protected_path("any/file", None) is False


def test_protect_full_path_match() -> None:
    """§3.13: a pattern matching the full relative path protects the file."""
    assert is_protected_path("config/tokens.json", ["config/tokens.json"])


def test_protect_single_component_match_at_any_depth() -> None:
    """§3.13: a bare filename pattern matches that filename at any depth."""
    assert is_protected_path("a/b/tokens.json", ["tokens.json"])
    assert is_protected_path("deeply/nested/tokens.json", ["tokens.json"])


def test_protect_fnmatch_star_spans_slash() -> None:
    """§3.13: fnmatch treats / as a literal, so *.key matches config/foo.key."""
    assert is_protected_path("config/foo.key", ["*.key"])
    assert is_protected_path("foo.key", ["*.key"])


def test_protect_multilevel_glob() -> None:
    """§3.13: config/*/tokens.json matches one-or-more intermediate segments."""
    assert is_protected_path("config/sub/tokens.json", ["config/*/tokens.json"])
    assert is_protected_path("config/a/b/tokens.json", ["config/*/tokens.json"])
    assert not is_protected_path("other/tokens.json", ["config/*/tokens.json"])


def test_protect_no_match_returns_false() -> None:
    """§3.13: a non-matching pattern leaves the file unprotected."""
    assert not is_protected_path("normal/file.txt", ["tokens.json"])


def test_protect_accepts_path_object() -> None:
    """§3.13: Path inputs are treated the same as string inputs."""
    assert is_protected_path(Path("a/b/c.key"), ["*.key"])


# ---------------------------------------------------------------------------
# §7.2: copy_tree -- merge semantics
# ---------------------------------------------------------------------------


def test_copy_tree_merge_copies_new_files(tmp_path: Path) -> None:
    """§7.2: merge mode copies files that exist only in the source."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "b.txt": "2"})
    dst.mkdir()
    result = copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "1", "b.txt": "2"}
    assert set(result.added) == {"a.txt", "b.txt"}
    assert result.updated == []
    assert result.removed == []


def test_copy_tree_merge_keeps_extras(tmp_path: Path) -> None:
    """§7.2: merge mode does not remove files that exist only in the destination."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "extra.txt": "keepme"})
    copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "1", "extra.txt": "keepme"}


def test_copy_tree_merge_updates_changed_files_in_place(tmp_path: Path) -> None:
    """§4.4: an updated file is reported as updated, not removed."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "new"})
    _tree(dst, {"a.txt": "old"})
    result = copy_tree(src, dst, mode="merge")
    assert _read_tree(dst) == {"a.txt": "new"}
    assert result.updated == ["a.txt"]
    assert result.removed == []


def test_copy_tree_merge_leaves_unchanged_files_untouched(tmp_path: Path) -> None:
    """§4.4: unchanged files (SHA-256 match) are never rewritten."""
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


# ---------------------------------------------------------------------------
# §7.2: copy_tree -- delete semantics
# ---------------------------------------------------------------------------


def test_copy_tree_delete_removes_extras(tmp_path: Path) -> None:
    """§7.2: delete mode removes unprotected files not in the source."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "extra.txt": "gone"})
    result = copy_tree(src, dst, mode="delete")
    assert _read_tree(dst) == {"a.txt": "1"}
    assert result.removed == ["extra.txt"]


def test_copy_tree_delete_prunes_stale_empty_directories(tmp_path: Path) -> None:
    """§7.2: empty directories left behind by a deletion are removed."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "sub/deep/stale.txt": "x"})
    copy_tree(src, dst, mode="delete")
    assert not (dst / "sub").exists()


def test_copy_tree_delete_preserves_directories_with_surviving_content(tmp_path: Path) -> None:
    """§7.2: a directory with any surviving file is not pruned."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "keep/inner.txt": "y"})
    _tree(dst, {"a.txt": "1", "keep/inner.txt": "y", "keep/other.txt": "z"})
    copy_tree(src, dst, mode="delete")
    assert (dst / "keep" / "inner.txt").is_file()
    assert not (dst / "keep" / "other.txt").exists()


def test_copy_tree_delete_updates_changed_content(tmp_path: Path) -> None:
    """§7.2: delete mode updates files present in both trees with differing content."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "new"})
    _tree(dst, {"a.txt": "old"})
    result = copy_tree(src, dst, mode="delete")
    assert _read_tree(dst) == {"a.txt": "new"}
    assert result.updated == ["a.txt"]


# ---------------------------------------------------------------------------
# §3.13 + §7.2: protect interaction with copy_tree
# ---------------------------------------------------------------------------


def test_copy_tree_delete_protected_extra_survives(tmp_path: Path) -> None:
    """§3.13: a protected file not in source survives delete mode."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "tokens.json": "secret"})
    result = copy_tree(src, dst, mode="delete", protect_patterns=["tokens.json"])
    assert (dst / "tokens.json").is_file()
    assert result.protected_kept == ["tokens.json"]


def test_copy_tree_protect_still_allows_overwrite(tmp_path: Path) -> None:
    """§3.13: protection governs deletion, not overwrite."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"tokens.json": "from-source"})
    _tree(dst, {"tokens.json": "old"})
    copy_tree(src, dst, mode="delete", protect_patterns=["tokens.json"])
    assert (dst / "tokens.json").read_text(encoding="utf-8") == "from-source"


def test_copy_tree_protect_directory_component_survives(tmp_path: Path) -> None:
    """§3.13: a directory component pattern protects the whole subtree."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1"})
    _tree(dst, {"a.txt": "1", "world/level.dat": "x"})
    copy_tree(src, dst, mode="delete", protect_patterns=["world"])
    assert (dst / "world" / "level.dat").is_file()


# ---------------------------------------------------------------------------
# §7.2: copy_tree -- error cases
# ---------------------------------------------------------------------------


def test_copy_tree_missing_source_raises_not_a_directory(tmp_path: Path) -> None:
    """§7.2: a missing source is a NotADirectoryError."""
    with pytest.raises(NotADirectoryError):
        copy_tree(tmp_path / "nope", tmp_path / "dst")


def test_copy_tree_invalid_mode_raises_value_error(tmp_path: Path) -> None:
    """§7.2: mode must be one of {merge, delete}."""
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValueError):
        copy_tree(src, tmp_path / "dst", mode="overwrite")


def test_copy_tree_creates_destination_when_absent(tmp_path: Path) -> None:
    """§7.2: the destination tree is created if it does not exist."""
    src = tmp_path / "src"
    _tree(src, {"a.txt": "1"})
    dst = tmp_path / "new_dst"
    copy_tree(src, dst, mode="delete")
    assert (dst / "a.txt").is_file()


def test_copy_tree_continues_after_a_single_file_copy_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """§7.2: a copy failure is logged and the remaining files still copy."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tree(src, {"a.txt": "1", "b.txt": "2"})
    dst.mkdir()
    real_copy2 = shutil_mod.copy2
    calls = {"n": 0}

    def flaky(src_p: object, dst_p: object, *args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated copy failure")
        return real_copy2(src_p, dst_p, *args, **kwargs)

    monkeypatch.setattr(shutil_mod, "copy2", flaky)
    with caplog.at_level(logging.WARNING):
        result = copy_tree(src, dst, mode="delete", logger=logging.getLogger("test"))
    assert len(result.added) == 1
    assert any("copy" in r.message and "failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# §4.11: deploy_flat_files
# ---------------------------------------------------------------------------


def test_flat_files_only_jar_files_are_in_the_effective_mod_set(tmp_path: Path) -> None:
    """§4.11: only *.jar in dst_dir are considered; other files are untouched."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "readme.txt": "notes"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "readme.txt").is_file()
    assert result.removed == []


def test_flat_files_subdirectories_ignored(tmp_path: Path) -> None:
    """§4.11: mods_dir is flat; subdirectories are invisible to the clean."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    (dst / "disabled").mkdir(parents=True)
    (dst / "disabled" / "b.jar").write_text("y", encoding="utf-8")
    _tree(dst, {"a.jar": "x"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "disabled" / "b.jar").is_file()
    assert result.removed == []


def test_flat_files_copies_new_files(tmp_path: Path) -> None:
    """§4.11: files in the source set but not in dst_dir are added."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x", "b.jar": "y"})
    dst = tmp_path / "mods"
    result = deploy_flat_files({"a.jar": src_dir / "a.jar", "b.jar": src_dir / "b.jar"}, dst)
    assert sorted(result.added) == ["a.jar", "b.jar"]
    assert (dst / "a.jar").read_text(encoding="utf-8") == "x"


def test_flat_files_removes_extras(tmp_path: Path) -> None:
    """§4.11: unprotected .jar files not in the source set are removed."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "stale.jar": "y"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.removed == ["stale.jar"]
    assert not (dst / "stale.jar").exists()


def test_flat_files_protected_extra_survives_clean(tmp_path: Path) -> None:
    """§3.13 + §4.11: a protected extra survives the flat clean."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "x", "keep.jar": "y"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst, protect_patterns=["keep.jar"])
    assert result.protected_kept == ["keep.jar"]
    assert (dst / "keep.jar").is_file()


def test_flat_files_protected_in_source_is_overwritten(tmp_path: Path) -> None:
    """§3.13 + §4.11: a protected file in the source set is overwritten."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"keep.jar": "new content"})
    dst = tmp_path / "mods"
    _tree(dst, {"keep.jar": "old content"})
    deploy_flat_files({"keep.jar": src_dir / "keep.jar"}, dst, protect_patterns=["keep.jar"])
    assert (dst / "keep.jar").read_text(encoding="utf-8") == "new content"


def test_flat_files_updates_changed_content_in_place(tmp_path: Path) -> None:
    """§4.4: a changed .jar is updated in place, not removed and re-added."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "new"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "old"})
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.updated == ["a.jar"]
    assert result.removed == []
    assert (dst / "a.jar").read_text(encoding="utf-8") == "new"


def test_flat_files_unchanged_not_touched(tmp_path: Path) -> None:
    """§4.4: unchanged files are not rewritten; mtime is preserved."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "same"})
    dst = tmp_path / "mods"
    _tree(dst, {"a.jar": "same"})
    before = (dst / "a.jar").stat().st_mtime_ns
    result = deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert result.unchanged == ["a.jar"]
    assert (dst / "a.jar").stat().st_mtime_ns == before


def test_flat_files_missing_source_file_is_skipped(tmp_path: Path) -> None:
    """§4.11: source entries whose file is missing are silently skipped."""
    dst = tmp_path / "mods"
    result = deploy_flat_files({"ghost.jar": tmp_path / "nope.jar"}, dst)
    assert result.added == []
    assert result.updated == []


def test_flat_files_creates_destination_directory(tmp_path: Path) -> None:
    """§4.11: the mods_dir is created if absent."""
    src_dir = tmp_path / "src"
    _tree(src_dir, {"a.jar": "x"})
    dst = tmp_path / "new_mods"
    deploy_flat_files({"a.jar": src_dir / "a.jar"}, dst)
    assert (dst / "a.jar").is_file()


# ---------------------------------------------------------------------------
# §7.5: @www grammar
# ---------------------------------------------------------------------------


def test_is_shared_dest_true_only_for_at_www_prefix() -> None:
    """§7.5: the shared-destination prefix is @www/."""
    assert is_shared_dest("@www/foo")
    assert not is_shared_dest("foo/bar")
    assert not is_shared_dest("")


@pytest.mark.parametrize("value,expected_subpath", [("@www/resourcepacks", "resourcepacks"), ("@www/packs/2026", "packs/2026")])
def test_parse_shared_dest_valid(value: str, expected_subpath: str) -> None:
    """§7.5: valid @www subpaths are returned with prefix www."""
    prefix, subpath = parse_shared_dest(value)
    assert prefix == "www"
    assert subpath == expected_subpath


@pytest.mark.parametrize("value", ["@www", "@www/", "@www//foo", "@www/./foo", "@www/../foo", "@www/foo/", "@www/a//b", "@mods/foo", "@"])
def test_parse_shared_dest_rejects_invalid_values(value: str) -> None:
    """§7.5: every rejected example from the grammar raises ConfigError."""
    with pytest.raises(ConfigError):
        parse_shared_dest(value)


def test_parse_shared_dest_rejects_nul_byte() -> None:
    """§7.5: segments must not contain NUL."""
    with pytest.raises(ConfigError):
        parse_shared_dest("@www/foo\x00bar")


def test_resolve_shared_dest_joins_under_www_dir(tmp_path: Path) -> None:
    """§7.5: the subpath is resolved relative to www_dir."""
    assert resolve_shared_dest("@www/resourcepacks", tmp_path) == tmp_path / "resourcepacks"
    assert resolve_shared_dest("@www/packs/2026", tmp_path) == tmp_path / "packs" / "2026"


def test_resolve_shared_dest_rejects_non_www_prefix() -> None:
    """§7.5: only @www is defined by the grammar."""
    with pytest.raises(ConfigError):
        resolve_shared_dest("@mods/foo", Path("/tmp"))


def test_resolve_resource_pack_dest_resolves_at_www(tmp_path: Path) -> None:
    """§7.5: resolve_resource_pack_dest is a thin alias for resolve_shared_dest."""
    assert resolve_resource_pack_dest("@www/resourcepacks", tmp_path) == tmp_path / "resourcepacks"


# ---------------------------------------------------------------------------
# §7.5: validate_resource_pack_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["pack.zip", "creative.zip", "my_pack_v2.zip", "a.b.c.zip"])
def test_resource_pack_filename_accepts_valid_names(name: str) -> None:
    """§7.5: single relative .zip filenames are accepted."""
    validate_resource_pack_filename(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "pack",
        "pack.tar.gz",
        "pack.zip/",
        "dir/pack.zip",
        "dir\\pack.zip",
        "..zip",
        ".hidden.zip",
        "pack..zip",
        "pack\x00.zip",
    ],
)
def test_resource_pack_filename_rejects_invalid_names(name: str) -> None:
    """§7.5: empty, path-separator, leading-dot, or non-.zip names are rejected."""
    with pytest.raises(ConfigError):
        validate_resource_pack_filename(name)


# ---------------------------------------------------------------------------
# §7.5: build_resource_pack_url
# ---------------------------------------------------------------------------


def test_build_resource_pack_url_joins_base_subpath_filename() -> None:
    """§7.5: URL is {base}/{subpath}/{filename}."""
    url = build_resource_pack_url("http://minecraft/downloads", "@www/resourcepacks", "pack.zip")
    assert url == "http://minecraft/downloads/resourcepacks/pack.zip"


def test_build_resource_pack_url_strips_trailing_base_slash() -> None:
    """§7.5: the download base URL has any trailing / stripped."""
    url = build_resource_pack_url("http://minecraft/downloads/", "@www/packs", "pack.zip")
    assert url == "http://minecraft/downloads/packs/pack.zip"


def test_build_resource_pack_url_supports_deep_subpaths() -> None:
    """§7.5: multi-segment subpaths are preserved."""
    url = build_resource_pack_url("http://x", "@www/packs/2026", "pack.zip")
    assert url == "http://x/packs/2026/pack.zip"


def test_build_resource_pack_url_rejects_empty_base() -> None:
    """§7.5: an empty download_base_url is exit 3."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("", "@www/resourcepacks", "pack.zip")


def test_build_resource_pack_url_rejects_bad_mapping() -> None:
    """§7.5: a non-@www mapping raises ConfigError."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("http://x", "@mods/foo", "pack.zip")


def test_build_resource_pack_url_rejects_bad_filename() -> None:
    """§7.5: the filename must satisfy the §7.5 validation rules."""
    with pytest.raises(ConfigError):
        build_resource_pack_url("http://x", "@www/resourcepacks", "pack.tar.gz")


# ---------------------------------------------------------------------------
# §3.9: resolve_mapping_for_side
# ---------------------------------------------------------------------------


def test_mapping_string_used_for_both_sides() -> None:
    """§3.9: a plain string mapping applies to both client and server."""
    assert resolve_mapping_for_side("config", "client") == "config"
    assert resolve_mapping_for_side("config", "server") == "config"


def test_mapping_dict_selects_per_side() -> None:
    """§3.9: a dict mapping yields the side-specific value."""
    m = {"server": "server_cfg", "client": "client_cfg"}
    assert resolve_mapping_for_side(m, "server") == "server_cfg"
    assert resolve_mapping_for_side(m, "client") == "client_cfg"


def test_mapping_dict_missing_side_is_excluded() -> None:
    """§3.9: a missing side key means the item is excluded on that side."""
    assert resolve_mapping_for_side({"server": "srv"}, "client") is None


def test_mapping_dict_explicit_none_is_excluded() -> None:
    """§3.9: an explicit None value excludes the item on that side."""
    assert resolve_mapping_for_side({"server": None}, "server") is None


def test_mapping_dict_minus_one_is_excluded() -> None:
    """§3.9: the legacy -1 convention excludes the item on that side."""
    assert resolve_mapping_for_side({"server": -1}, "server") is None


def test_mapping_dict_non_string_value_is_excluded() -> None:
    """§3.9: a non-string value is treated as excluded."""
    assert resolve_mapping_for_side({"server": 42}, "server") is None


def test_mapping_non_str_non_dict_returns_none() -> None:
    """§3.9: types other than str or dict yield None."""
    assert resolve_mapping_for_side(None, "client") is None
    assert resolve_mapping_for_side([1, 2], "client") is None


# ---------------------------------------------------------------------------
# §4.4: hash helpers (the change-detection mechanism)
# ---------------------------------------------------------------------------


def test_sha256_and_sha1_known_vectors(tmp_path: Path) -> None:
    """§4.4: SHA-256 and SHA-1 are the lowercase-hex digests of the file."""
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert compute_sha256(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert compute_sha1(p) == "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"


def test_sha256_bytes_known_vector() -> None:
    """§4.4: sha256_bytes is the SHA-256 of the supplied buffer."""
    assert sha256_bytes(b"hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_hash_flat_dir_considers_only_jar_files(tmp_path: Path) -> None:
    """§4.11: mods_dir is flat; only *.jar files are hashed."""
    d = tmp_path / "mods"
    d.mkdir()
    (d / "a.jar").write_bytes(b"aaa")
    (d / "b.jar").write_bytes(b"bbb")
    (d / "readme.txt").write_text("x", encoding="utf-8")
    assert set(hash_flat_dir(d)) == {"a.jar", "b.jar"}


def test_hash_flat_dir_missing_directory_is_empty(tmp_path: Path) -> None:
    """§4.11: a missing mods_dir yields an empty map."""
    assert hash_flat_dir(tmp_path / "nope") == {}


def test_hash_tree_keys_use_forward_slashes(tmp_path: Path) -> None:
    """§4.4: hash_tree keys are relative paths with forward slashes."""
    root = tmp_path / "tree"
    (root / "deep" / "deeper").mkdir(parents=True)
    (root / "deep" / "deeper" / "x.txt").write_text("x", encoding="utf-8")
    assert "deep/deeper/x.txt" in hash_tree(root)


def test_hash_tree_missing_directory_is_empty(tmp_path: Path) -> None:
    """§4.4: a missing directory yields an empty map."""
    assert hash_tree(tmp_path / "nope") == {}
