# tests/modpack_upgrade/test_shipping.py

"""Shipping transaction tests (spec Section 3.5, Section 13.2, Section 13.4; T08, T17)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from minecraft.modpack_upgrade import shipping  # noqa: F401  (imported for side-effect coverage)
from minecraft.modpack_upgrade.shipping import (
    RealFileOps,
    ShipRequest,
    ship,
)


def _sha(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _seed(directory: Path, contents: dict[str, bytes]) -> dict[str, str]:
    """Write the given files into ``directory``; return {filename: sha256}."""
    directory.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name, data in contents.items():
        path = directory / name
        path.write_bytes(data)
        hashes[name] = _sha(data)
    return hashes


def _source(sources_dir: Path, contents: dict[str, bytes]) -> dict[str, Path]:
    """Write source files into ``sources_dir``; return {filename: path}."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, data in contents.items():
        path = sources_dir / name
        path.write_bytes(data)
        out[name] = path
    return out


def _leftover_dirs(target: Path) -> list[Path]:
    """Return any staging or backup directories left next to ``target``."""
    parent = target.parent
    prefixes = (
        f".{target.name}.ship-staging.",
        f".{target.name}.ship-backup.",
    )
    leftovers: list[Path] = []
    for entry in parent.iterdir():
        if any(entry.name.startswith(p) for p in prefixes):
            leftovers.append(entry)
    return sorted(leftovers)


class _ScriptedFileOps:
    """A FileOps that injects failures at configured call sites."""

    def __init__(self, base: RealFileOps | None = None) -> None:
        """Wrap ``base`` or a fresh RealFileOps."""
        self._base = base if base is not None else RealFileOps()
        self._failures: list[tuple[str, str, int]] = []
        self._corrupt_armed: str | None = None
        self._counts: dict[tuple[str, str], int] = {}

    def fail_on(self, method: str, pathname: str, call_index: int) -> _ScriptedFileOps:
        """Configure a failure on the Nth call of ``method`` for ``pathname``."""
        self._failures.append((method, pathname, call_index))
        return self

    def corrupt_after_replace(self, pathname: str) -> _ScriptedFileOps:
        """Corrupt the destination on the next replace of ``pathname`` only.

        The arming is consumed after the first matching replace, so a
        subsequent rollback that restores the file is not re-corrupted.
        """
        self._corrupt_armed = pathname
        return self

    def _should_fail(self, method: str, path: Path) -> bool:
        """Return True when this call matches a configured failure."""
        key = (method, path.name)
        self._counts[key] = self._counts.get(key, 0) + 1
        return any(m == method and pname == path.name and self._counts[key] == idx for m, pname, idx in self._failures)

    def mkdir(self, path: Path) -> None:
        """Delegate to base, injecting a failure when configured."""
        if self._should_fail("mkdir", path):
            raise OSError(f"scripted mkdir failure: {path}")
        self._base.mkdir(path)

    def copy(self, src: Path, dst: Path) -> None:
        """Delegate to base, injecting a failure when configured."""
        if self._should_fail("copy", dst):
            raise OSError(f"scripted copy failure: {src} -> {dst}")
        self._base.copy(src, dst)

    def replace(self, src: Path, dst: Path) -> None:
        """Delegate to base, injecting failures and one-shot post-replace corruption."""
        if self._should_fail("replace", dst):
            raise OSError(f"scripted replace failure: {src} -> {dst}")
        self._base.replace(src, dst)
        if dst.name == self._corrupt_armed:
            self._corrupt_armed = None
            dst.write_bytes(b"corrupted")

    def unlink(self, path: Path) -> None:
        """Delegate to base, injecting a failure when configured."""
        if self._should_fail("unlink", path):
            raise OSError(f"scripted unlink failure: {path}")
        self._base.unlink(path)

    def sha256(self, path: Path) -> str:
        """Delegate to base."""
        return self._base.sha256(path)

    def exists(self, path: Path) -> bool:
        """Delegate to base."""
        return self._base.exists(path)

    def is_file(self, path: Path) -> bool:
        """Delegate to base."""
        return self._base.is_file(path)


# ===========================================================================
# Happy-path tests
# ===========================================================================


def test_noop_when_target_matches_source(tmp_path: Path) -> None:
    """Ships an identical set and asserts no staging or backup is created."""
    target = tmp_path / "mods"
    contents = {"a.jar": b"aaa", "b.jar": b"bbb"}
    _seed(target, contents)
    sources = _source(tmp_path / "src", contents)

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert not result.any_change
    assert result.unchanged == ["a.jar", "b.jar"]
    assert _leftover_dirs(target) == []


def test_creates_target_when_missing(tmp_path: Path) -> None:
    """Ships into a nonexistent target directory and asserts it is created."""
    target = tmp_path / "new-mods"
    sources = _source(tmp_path / "src", {"a.jar": b"aaa"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert target.is_dir()
    assert (target / "a.jar").read_bytes() == b"aaa"
    assert result.added == ["a.jar"]


def test_adds_new_file(tmp_path: Path) -> None:
    """Adds a file not present in the target."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa"})
    sources = _source(tmp_path / "src", {"a.jar": b"aaa", "b.jar": b"bbb"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.added == ["b.jar"]
    assert result.updated == []
    assert result.removed == []
    assert result.unchanged == ["a.jar"]
    assert (target / "b.jar").read_bytes() == b"bbb"


def test_updates_changed_file(tmp_path: Path) -> None:
    """Replaces a target file whose content differs from the source."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"old"})
    sources = _source(tmp_path / "src", {"a.jar": b"new"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.updated == ["a.jar"]
    assert (target / "a.jar").read_bytes() == b"new"


def test_removes_stale_file(tmp_path: Path) -> None:
    """Removes a target `.jar` not present in the source set."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa", "stale.jar": b"old"})
    sources = _source(tmp_path / "src", {"a.jar": b"aaa"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.removed == ["stale.jar"]
    assert not (target / "stale.jar").exists()
    assert (target / "a.jar").exists()


def test_mixed_add_update_remove(tmp_path: Path) -> None:
    """Exercises all three buckets in a single transaction."""
    target = tmp_path / "mods"
    _seed(target, {"keep.jar": b"keep", "update.jar": b"old", "stale.jar": b"old"})
    sources = _source(
        tmp_path / "src",
        {"keep.jar": b"keep", "update.jar": b"new", "add.jar": b"add"},
    )

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.added == ["add.jar"]
    assert result.updated == ["update.jar"]
    assert result.removed == ["stale.jar"]
    assert result.unchanged == ["keep.jar"]
    assert (target / "add.jar").read_bytes() == b"add"
    assert (target / "update.jar").read_bytes() == b"new"
    assert not (target / "stale.jar").exists()


def test_protected_file_is_kept(tmp_path: Path) -> None:
    """A pattern-matched stale file survives the removal pass."""
    target = tmp_path / "mods"
    _seed(target, {"keep.jar": b"keep", "pinned.jar": b"pin", "stale.jar": b"x"})
    sources = _source(tmp_path / "src", {"keep.jar": b"keep"})

    result = ship(
        ShipRequest(
            source_set=sources,
            target_dir=target,
            protect_patterns=("pinned.jar",),
        )
    )

    assert result.success
    assert result.protected_kept == ["pinned.jar"]
    assert result.removed == ["stale.jar"]
    assert (target / "pinned.jar").exists()
    assert (target / "pinned.jar").read_bytes() == b"pin"


def test_non_jar_files_are_ignored(tmp_path: Path) -> None:
    """A non-`.jar` file in the target is neither removed nor shipped."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa"})
    (target / "README.txt").write_text("hello")
    sources = _source(tmp_path / "src", {"a.jar": b"aaa"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.removed == []
    assert (target / "README.txt").exists()


def test_jar_disabled_files_are_ignored(tmp_path: Path) -> None:
    """A `.jar.disabled` file is preserved as an unrelated artifact."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa"})
    (target / "b.jar.disabled").write_bytes(b"bbb")
    sources = _source(tmp_path / "src", {"a.jar": b"aaa"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.removed == []
    assert (target / "b.jar.disabled").exists()


def test_nested_directories_are_ignored(tmp_path: Path) -> None:
    """Nested directories are neither traversed nor removed."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa"})
    nested = target / "extra"
    nested.mkdir()
    (nested / "b.jar").write_bytes(b"bbb")
    sources = _source(tmp_path / "src", {"a.jar": b"aaa"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.removed == []
    assert (nested / "b.jar").exists()


def test_successful_ship_cleans_up(tmp_path: Path) -> None:
    """Staging and backup directories do not survive a successful ship."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"old"})
    sources = _source(tmp_path / "src", {"a.jar": b"new"})

    result = ship(ShipRequest(source_set=sources, target_dir=target))

    assert result.success
    assert result.staging_dir is None
    assert result.backup_dir is None
    assert result.journal_path is None
    assert _leftover_dirs(target) == []


# ===========================================================================
# Failure-injection tests (T08)
# ===========================================================================


def test_unreadable_source_returns_failure(tmp_path: Path) -> None:
    """A source that cannot be read yields a failure result with no side effects."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"aaa"})
    missing = tmp_path / "src" / "gone.jar"

    result = ship(ShipRequest(source_set={"gone.jar": missing}, target_dir=target))

    assert not result.success
    assert result.failure_message is not None
    assert not result.rollback_performed
    assert _leftover_dirs(target) == []
    assert (target / "a.jar").read_bytes() == b"aaa"


def test_staging_copy_failure_leaves_target_unchanged(tmp_path: Path) -> None:
    """A staging copy failure aborts before any target write."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"old"})
    sources = _source(tmp_path / "src", {"a.jar": b"new", "b.jar": b"bbb"})

    ops = _ScriptedFileOps().fail_on("copy", "b.jar", 1)
    result = ship(ShipRequest(source_set=sources, target_dir=target), ops=ops)

    assert not result.success
    assert result.rollback_performed
    assert result.rollback_succeeded
    assert (target / "a.jar").read_bytes() == b"old"
    assert not (target / "b.jar").exists()
    assert _leftover_dirs(target) == []


def test_apply_replace_failure_rolls_back(tmp_path: Path) -> None:
    """A replace failure mid-apply restores the pre-transaction inventory."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"old-a", "b.jar": b"old-b"})
    sources = _source(
        tmp_path / "src",
        {"a.jar": b"new-a", "b.jar": b"new-b"},
    )

    ops = _ScriptedFileOps().fail_on("replace", "b.jar", 1)
    result = ship(ShipRequest(source_set=sources, target_dir=target), ops=ops)

    assert not result.success
    assert result.rollback_performed
    assert result.rollback_succeeded
    assert (target / "a.jar").read_bytes() == b"old-a"
    assert (target / "b.jar").read_bytes() == b"old-b"
    assert _leftover_dirs(target) == []


def test_apply_unlink_failure_rolls_back(tmp_path: Path) -> None:
    """An unlink failure mid-apply restores the pre-transaction inventory."""
    target = tmp_path / "mods"
    _seed(target, {"stale-a.jar": b"sa", "stale-b.jar": b"sb"})
    sources: dict[str, Path] = {}

    ops = _ScriptedFileOps().fail_on("unlink", "stale-b.jar", 1)
    result = ship(ShipRequest(source_set=sources, target_dir=target), ops=ops)

    assert not result.success
    assert result.rollback_performed
    assert result.rollback_succeeded
    assert (target / "stale-a.jar").read_bytes() == b"sa"
    assert (target / "stale-b.jar").read_bytes() == b"sb"
    assert _leftover_dirs(target) == []


def test_verify_failure_rolls_back(tmp_path: Path) -> None:
    """A post-apply verification mismatch rolls back to the pre-transaction state."""
    target = tmp_path / "mods"
    _seed(target, {"a.jar": b"old"})
    sources = _source(tmp_path / "src", {"a.jar": b"new"})

    ops = _ScriptedFileOps().corrupt_after_replace("a.jar")
    result = ship(ShipRequest(source_set=sources, target_dir=target), ops=ops)

    assert not result.success
    assert result.rollback_performed
    assert result.rollback_succeeded
    assert (target / "a.jar").read_bytes() == b"old"
    assert _leftover_dirs(target) == []


def test_invalid_source_filename_is_rejected(tmp_path: Path) -> None:
    """A source entry that is not a plain `.jar` filename is rejected."""
    target = tmp_path / "mods"
    source_file = tmp_path / "src" / "notes.txt"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"x")

    result = ship(ShipRequest(source_set={"notes.txt": source_file}, target_dir=target))

    assert not result.success
    assert "must end in .jar" in (result.failure_message or "")


def test_path_separator_in_source_filename_is_rejected(tmp_path: Path) -> None:
    """A source entry containing a path separator is rejected."""
    target = tmp_path / "mods"
    source_file = tmp_path / "src" / "a.jar"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"x")

    result = ship(ShipRequest(source_set={"sub/a.jar": source_file}, target_dir=target))

    assert not result.success
    assert "path separators" in (result.failure_message or "")
