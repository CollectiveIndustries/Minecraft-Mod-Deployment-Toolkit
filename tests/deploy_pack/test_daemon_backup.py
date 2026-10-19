# tests/deploy_pack/test_daemon_backup.py

"""Unit tests for ``daemon.backup`` (spec v1.3 Section 10.8, Section 12.10).

Backup creation is exercised against real directories under
``tmp_path``. Verification is exercised against both a clean backup
and one that has been tampered with.
"""

from __future__ import annotations

import json
from pathlib import Path

from minecraft.deploy_pack.daemon.backup import (
    MANIFEST_FILENAME,
    create_backup,
    verify_backup,
)


def _seed_world(world: Path) -> None:
    """Write a small world directory with representative files."""
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"level-data")
    (world / "session.lock").write_bytes(b"lock-bytes")
    (world / "region").mkdir()
    (world / "region" / "r.0.0.mca").write_bytes(b"\x00" * 100)
    (world / "data").mkdir()
    (world / "data" / "raids.dat").write_bytes(b"raids")


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


def test_create_backup_copies_world(tmp_path: Path) -> None:
    """Every file in the source world is present in the backup."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")

    assert result.success is True
    assert result.backup_dir == tmp_path / "backups" / "v1.0.0"
    assert (result.backup_dir / "level.dat").read_bytes() == b"level-data"
    assert (result.backup_dir / "region" / "r.0.0.mca").is_file()


def test_create_backup_writes_manifest(tmp_path: Path) -> None:
    """A manifest is written listing every copied file."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")

    manifest_path = result.backup_dir / MANIFEST_FILENAME
    assert manifest_path.is_file()
    data = json.loads(manifest_path.read_bytes())
    paths = {entry["path"] for entry in data["files"]}
    assert paths == {"level.dat", "session.lock", "region/r.0.0.mca", "data/raids.dat"}
    assert data["total_size_bytes"] > 0
    assert data["source"] == str(world)


def test_create_backup_refuses_existing_dir(tmp_path: Path) -> None:
    """An existing backup directory is not overwritten."""
    world = tmp_path / "world"
    _seed_world(world)
    backups = tmp_path / "backups"
    existing = backups / "v1.0.0"
    existing.mkdir(parents=True)
    (existing / "old.txt").write_bytes(b"pre-existing")

    result = create_backup(source_world=world, backup_root=backups, release_id="v1.0.0")

    assert result.success is False
    assert "already exists" in (result.failure_message or "")
    assert (existing / "old.txt").read_bytes() == b"pre-existing"


def test_create_backup_missing_world_fails(tmp_path: Path) -> None:
    """A missing source world returns failure without touching disk."""
    result = create_backup(
        source_world=tmp_path / "does-not-exist",
        backup_root=tmp_path / "backups",
        release_id="v1.0.0",
    )
    assert result.success is False
    assert "not found" in (result.failure_message or "")


def test_create_backup_creates_backup_root(tmp_path: Path) -> None:
    """A missing backup_root directory is created."""
    world = tmp_path / "world"
    _seed_world(world)
    nested = tmp_path / "deep" / "nested" / "backups"
    result = create_backup(source_world=world, backup_root=nested, release_id="v1.0.0")
    assert result.success is True
    assert nested.is_dir()


# ---------------------------------------------------------------------------
# verify_backup
# ---------------------------------------------------------------------------


def test_verify_backup_success(tmp_path: Path) -> None:
    """An untouched backup verifies cleanly."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    assert result.success is True

    ok, err = verify_backup(result.backup_dir)
    assert ok is True
    assert err is None


def test_verify_backup_missing_manifest(tmp_path: Path) -> None:
    """A backup directory without a manifest fails."""
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "file.txt").write_bytes(b"x")

    ok, err = verify_backup(backup)
    assert ok is False
    assert "manifest missing" in (err or "")


def test_verify_backup_missing_file(tmp_path: Path) -> None:
    """A manifest entry whose file was deleted is reported."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    (result.backup_dir / "level.dat").unlink()

    ok, err = verify_backup(result.backup_dir)
    assert ok is False
    assert "missing file: level.dat" in (err or "")


def test_verify_backup_size_mismatch(tmp_path: Path) -> None:
    """A file whose size changed since the manifest fails."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    (result.backup_dir / "level.dat").write_bytes(b"short")

    ok, err = verify_backup(result.backup_dir)
    assert ok is False
    assert "size mismatch: level.dat" in (err or "")


def test_verify_backup_sha_mismatch(tmp_path: Path) -> None:
    """A file whose bytes changed (same size) fails."""
    world = tmp_path / "world"
    _seed_world(world)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    # Same length as the original ("level-data" = 10 bytes) but different
    # content. A [:10] slice of a longer string would leave the bytes
    # unchanged; write the tampered bytes directly.
    (result.backup_dir / "level.dat").write_bytes(b"XXXXXXXXXX")

    ok, err = verify_backup(result.backup_dir)
    assert ok is False
    assert "sha256 mismatch: level.dat" in (err or "")
