# tests/deploy_pack/test_daemon_recovery.py

"""Unit tests for ``daemon.recovery`` (spec v1.3 Section 12.10, Section 12.11).

``restore_world`` is exercised against real backup directories.
``restore_mods`` is exercised against a real prior bundle and a real
target mods directory (the shipping transaction does the actual work).
``attempt_recovery`` is exercised for the four input combinations
(world-only, mods-only, both, neither).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import tomli_w

from minecraft.deploy_pack.daemon.backup import create_backup
from minecraft.deploy_pack.daemon.journal import DeploymentJournal
from minecraft.deploy_pack.daemon.recovery import (
    attempt_recovery,
    restore_mods,
    restore_world,
)
from minecraft.modpack_upgrade import slip as slip_mod


def _sha(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _jar_entry(filename: str, content: bytes) -> dict:
    """Build a schema-valid ``[[jar]]`` entry."""
    return {
        "filename": filename,
        "sha256": _sha(content),
        "side": "both",
        "provider": "modrinth",
        "pw_toml": f"{filename.removesuffix('.jar')}.pw.toml",
        "pw_toml_sha256": "0" * 64,
        "dependencies": [],
    }


def _slip_body(jar_entries: list[dict], release_id: str = "v1.0.0") -> dict:
    """Build a valid slip body with a fresh ``content_sha256``."""
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": "upgrade",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + "0" * 64,
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": "a" * 40,
                "kubejs_tree": "a" * 40,
                "compose_tree": "a" * 40,
                "deploy_toml_tree": "a" * 40,
            },
        },
        "fingerprints": {
            "manifest_sha256": "0" * 64,
            "resolution_inputs_sha256": "0" * 64,
            "runtime_sha256": "0" * 64,
            "server_inputs_sha256": "0" * 64,
            "client_inputs_sha256": "0" * 64,
        },
        "jar": jar_entries,
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_prior_bundle(bundle: Path, jars: dict[str, bytes]) -> None:
    """Write a minimal bundle with the given jars and a valid slip."""
    bundle.mkdir(parents=True)
    (bundle / "artifacts").mkdir()
    entries = []
    for filename, content in jars.items():
        (bundle / "artifacts" / filename).write_bytes(content)
        entries.append(_jar_entry(filename, content))
    body = _slip_body(entries)
    (bundle / "packing_slip.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))


def _journal(tmp_path: Path) -> DeploymentJournal:
    """Return a journal rooted under ``tmp_path``."""
    return DeploymentJournal(tmp_path / "journal.jsonl")


# ---------------------------------------------------------------------------
# restore_world
# ---------------------------------------------------------------------------


def test_restore_world_replaces_target(tmp_path: Path) -> None:
    """The target world ends up matching the backup contents."""
    source = tmp_path / "world"
    source.mkdir()
    (source / "level.dat").write_bytes(b"original")
    backup_result = create_backup(source_world=source, backup_root=tmp_path / "backups", release_id="v1.0.0")
    assert backup_result.success is True

    # Corrupt the live world; restore should fix it.
    (source / "level.dat").write_bytes(b"corrupted")
    (source / "new-file.txt").write_bytes(b"unexpected")

    ok, err = restore_world(backup_dir=backup_result.backup_dir, target_world=source)
    assert ok is True
    assert err is None
    assert (source / "level.dat").read_bytes() == b"original"
    assert not (source / "new-file.txt").exists()


def test_restore_world_does_not_copy_manifest(tmp_path: Path) -> None:
    """The backup manifest does not leak into the restored world."""
    source = tmp_path / "world"
    source.mkdir()
    (source / "level.dat").write_bytes(b"x")
    backup_result = create_backup(source_world=source, backup_root=tmp_path / "backups", release_id="v1.0.0")

    ok, _ = restore_world(backup_dir=backup_result.backup_dir, target_world=source)
    assert ok is True
    assert not (source / "backup-manifest.json").exists()


def test_restore_world_refuses_corrupt_backup(tmp_path: Path) -> None:
    """A backup that fails verification does not overwrite the target."""
    source = tmp_path / "world"
    source.mkdir()
    (source / "level.dat").write_bytes(b"original")
    backup_result = create_backup(source_world=source, backup_root=tmp_path / "backups", release_id="v1.0.0")

    # Corrupt the backup (not the target).
    (backup_result.backup_dir / "level.dat").write_bytes(b"tampered")

    ok, err = restore_world(backup_dir=backup_result.backup_dir, target_world=source)
    assert ok is False
    assert "backup verification failed" in (err or "")
    assert (source / "level.dat").read_bytes() == b"original"


# ---------------------------------------------------------------------------
# restore_mods
# ---------------------------------------------------------------------------


def test_restore_mods_reships_prior_bundle(tmp_path: Path) -> None:
    """Re-shipping the prior bundle restores its jar set."""
    prior = tmp_path / "prior-bundle"
    _write_prior_bundle(prior, {"old-a.jar": b"a-bytes", "old-b.jar": b"b-bytes"})

    # Target currently has the new release's jars (simulate a partial deploy).
    target = tmp_path / "mods"
    target.mkdir()
    (target / "new.jar").write_bytes(b"new")
    (target / "old-a.jar").write_bytes(b"a-tampered")

    journal = _journal(tmp_path)
    ok, err = restore_mods(
        prior_bundle_dir=prior,
        target_mods=target,
        protect_patterns=(),
        journal=journal,
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert ok is True
    assert err is None
    assert (target / "old-a.jar").read_bytes() == b"a-bytes"
    assert (target / "old-b.jar").read_bytes() == b"b-bytes"
    assert not (target / "new.jar").exists()


def test_restore_mods_prior_slip_missing(tmp_path: Path) -> None:
    """A prior bundle without a slip returns failure."""
    prior = tmp_path / "prior-bundle"
    prior.mkdir()
    target = tmp_path / "mods"

    ok, err = restore_mods(
        prior_bundle_dir=prior,
        target_mods=target,
        protect_patterns=(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )
    assert ok is False
    assert "prior slip not found" in (err or "")


# ---------------------------------------------------------------------------
# attempt_recovery
# ---------------------------------------------------------------------------


def test_attempt_recovery_both_sources(tmp_path: Path) -> None:
    """Both world and mods are restored when both sources are supplied."""
    # Set up world + backup.
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"original")
    backup_result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    (world / "level.dat").write_bytes(b"tampered")

    # Set up prior bundle + target mods.
    prior = tmp_path / "prior-bundle"
    _write_prior_bundle(prior, {"old.jar": b"old"})
    target_mods = tmp_path / "mods"
    target_mods.mkdir()
    (target_mods / "new.jar").write_bytes(b"new")

    journal = _journal(tmp_path)
    outcome = attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=backup_result.backup_dir,
        target_world=world,
        prior_bundle_dir=prior,
        target_mods=target_mods,
        protect_patterns=(),
        journal=journal,
    )

    assert outcome.success is True
    assert outcome.world_restored is True
    assert outcome.mods_restored is True
    assert (world / "level.dat").read_bytes() == b"original"
    assert (target_mods / "old.jar").read_bytes() == b"old"
    assert not (target_mods / "new.jar").exists()


def test_attempt_recovery_world_only(tmp_path: Path) -> None:
    """Only the world is restored when no prior bundle is supplied."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"original")
    backup_result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    (world / "level.dat").write_bytes(b"tampered")

    journal = _journal(tmp_path)
    outcome = attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=backup_result.backup_dir,
        target_world=world,
        prior_bundle_dir=None,
        target_mods=tmp_path / "mods",
        protect_patterns=(),
        journal=journal,
    )

    assert outcome.success is True
    assert outcome.world_restored is True
    assert outcome.mods_restored is False


def test_attempt_recovery_mods_only(tmp_path: Path) -> None:
    """Only mods are restored when no world backup is supplied."""
    prior = tmp_path / "prior-bundle"
    _write_prior_bundle(prior, {"old.jar": b"old"})
    target_mods = tmp_path / "mods"
    target_mods.mkdir()
    (target_mods / "new.jar").write_bytes(b"new")

    journal = _journal(tmp_path)
    outcome = attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=None,
        target_world=None,
        prior_bundle_dir=prior,
        target_mods=target_mods,
        protect_patterns=(),
        journal=journal,
    )

    assert outcome.success is True
    assert outcome.world_restored is False
    assert outcome.mods_restored is True


def test_attempt_recovery_neither_source_succeeds_trivially(tmp_path: Path) -> None:
    """With neither source, recovery is a no-op and reported as success."""
    outcome = attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=None,
        target_world=None,
        prior_bundle_dir=None,
        target_mods=tmp_path / "mods",
        protect_patterns=(),
        journal=_journal(tmp_path),
    )
    assert outcome.success is True
    assert outcome.world_restored is False
    assert outcome.mods_restored is False


def test_attempt_recovery_journals_started_and_completed(tmp_path: Path) -> None:
    """The journal records RECOVERY_STARTED then RECOVERY_COMPLETED."""
    journal = _journal(tmp_path)
    attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=None,
        target_world=None,
        prior_bundle_dir=None,
        target_mods=tmp_path / "mods",
        protect_patterns=(),
        journal=journal,
    )
    stages = [r.stage.value for r in journal.read_all()]
    assert "recovery_started" in stages
    assert "recovery_completed" in stages


def test_attempt_recovery_journals_failure(tmp_path: Path) -> None:
    """A failed recovery is journaled as RECOVERY_FAILED with details."""
    # Prior bundle without a slip forces restore_mods to fail.
    prior = tmp_path / "prior-bundle"
    prior.mkdir()
    journal = _journal(tmp_path)
    outcome = attempt_recovery(
        run_id="run-1",
        release_id="v1.0.0",
        world_backup_dir=None,
        target_world=None,
        prior_bundle_dir=prior,
        target_mods=tmp_path / "mods",
        protect_patterns=(),
        journal=journal,
    )

    assert outcome.success is False
    stages = [r.stage.value for r in journal.read_all()]
    assert "recovery_failed" in stages
