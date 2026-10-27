# src/minecraft/modpack_upgrade/cli/commands/snapshot_world.py

"""``minecraft-upgrade snapshot-world [survival|creative]`` -- snapshot prod world (Section 10).

rsync-over-SSH from prod to DEV, producing:
  - the copied world tree under ``<dest_root>/<snapshot_name>/world/``
  - a canonical file manifest at ``<dest_root>/<snapshot_name>/snapshot-manifest.json``
  - a printed ``snapshot_id`` (SHA-256 of the manifest's canonical bytes)

Manifest entries (Section 10.3): ``path`` (POSIX relative), ``size_bytes``,
``sha256``. Modification times, ownership, and group IDs are deliberately
excluded -- they do not survive cross-filesystem round-trips reproducibly.

Config (from ``[world_snapshot]`` in ``config.d/upgrade.toml``):

    source_host     required; no default
    source_user     default "deploy"
    source_path     default "/srv/minecraft/survival/world"
    ssh_key         default "~/.ssh/id_deploy_world_ro"
    dest_root       default "~/minecraft-dev/snapshots"
    exclude         default ["session.lock", "*.tmp", "*.lock"]
    timeout_seconds default 1800
    default_world   default "survival"
    stop_prod       default False  (accepted but unused here; the stop is a
                                    manual step on prod, per Section 10.4)

Subprocess runner is injectable for tests via the module-level
:data:`SubprocessRunner`. Production uses :func:`_default_runner`, which
mirrors the ``RsyncTransport`` pattern in ``daemon/pull.py``.

Exit codes:
  0  snapshot created and manifest written
  1  rsync failed, timed out, or produced zero files
  3  config error (missing source_host, unreadable ssh key, etc.)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = ["SubprocessRunner", "run"]

_log = get_logger(__name__)
_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.snapshot_world")

#: ``(argv, timeout) -> (returncode, stdout, stderr)``
SubprocessRunner = Callable[[list[str], float], "tuple[int, str, str]"]

_DEFAULT_CONFIG: dict = {
    "source_host": "",
    "source_user": "deploy",
    "source_path": "/srv/minecraft/survival/world",
    "ssh_key": "~/.ssh/id_deploy_world_ro",
    "dest_root": "~/minecraft-dev/snapshots",
    "exclude": ["session.lock", "*.tmp", "*.lock"],
    "timeout_seconds": 1800,
    "default_world": "survival",
    "stop_prod": False,
}

_MANIFEST_FILENAME = "snapshot-manifest.json"


def _default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run ``argv`` as a subprocess; return ``(rc, stdout, stderr)``.

    Mirrors ``daemon/pull.py::_default_transport``: FileNotFoundError yields
    rc 127 (missing binary), TimeoutExpired yields rc 124 (timeout). Both
    mimic the shell convention so failure classification is symmetric.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return (127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return (124, "", str(exc))
    return (result.returncode, result.stdout, result.stderr)


def _load_snapshot_config(config_dir: Path) -> dict:
    """Load and validate ``[world_snapshot]`` from ``upgrade.toml``."""
    toml_path = config_dir / "upgrade.toml"
    raw: dict = {}
    if toml_path.is_file():
        try:
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise UpgradeCliError(3, f"could not read {toml_path}: {exc}") from exc
        section = data.get("world_snapshot")
        if isinstance(section, dict):
            raw = section

    merged = dict(_DEFAULT_CONFIG)
    merged.update(raw)

    if not merged["source_host"]:
        raise UpgradeCliError(
            3,
            "[world_snapshot].source_host is required; configure it in config.d/upgrade.toml before snapshotting a world",
        )
    excludes = merged.get("exclude")
    if not isinstance(excludes, list) or not all(isinstance(p, str) for p in excludes):
        raise UpgradeCliError(3, "[world_snapshot].exclude must be an array of strings")
    try:
        merged["timeout_seconds"] = int(merged["timeout_seconds"])
    except (TypeError, ValueError) as exc:
        raise UpgradeCliError(3, "[world_snapshot].timeout_seconds must be an integer") from exc
    return merged


def _resolve_paths(config: dict) -> tuple[Path, Path]:
    """Return ``(ssh_key_path, dest_root_path)`` with ``~`` expanded."""
    return (Path(str(config["ssh_key"])).expanduser(), Path(str(config["dest_root"])).expanduser())


def _build_rsync_argv(config: dict, ssh_key: Path, target_dir: Path) -> list[str]:
    """Return the rsync argv for the world snapshot."""
    source = f"{config['source_user']}@{config['source_host']}:{config['source_path']}/"
    ssh_cmd = f"ssh -i {ssh_key} -o BatchMode=yes -o StrictHostKeyChecking=yes"
    argv: list[str] = ["rsync", "-az", "--checksum", "--delete", "-e", ssh_cmd]
    for pattern in config["exclude"]:
        argv.extend(["--exclude", pattern])
    argv.extend([source, f"{target_dir}/"])
    return argv


def _walk_manifest(root: Path) -> list[dict]:
    """Return the canonical file manifest for ``root`` (Section 10.3)."""
    entries: list[dict] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            full = Path(dirpath) / fname
            if not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            try:
                content = full.read_bytes()
            except OSError:
                continue
            entries.append(
                {
                    "path": rel,
                    "size_bytes": full.stat().st_size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    entries.sort(key=lambda e: e["path"])
    return entries


def _write_manifest(snapshot_dir: Path, entries: list[dict], source_path: str) -> str:
    """Write the snapshot manifest; return its SHA-256 (the ``snapshot_id``)."""
    manifest_path = snapshot_dir / _MANIFEST_FILENAME
    body = (
        json.dumps(
            {
                "schema_version": 1,
                "source": source_path,
                "created_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": entries,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    data = body.encode("utf-8")
    manifest_path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def run(
    *,
    config_dir: Path,
    project_root: Path,
    world: str | None = None,
    runner: SubprocessRunner | None = None,
) -> int:
    """Snapshot a prod world to DEV and write its manifest."""
    del project_root  # paths come from config.d/upgrade.toml
    config = _load_snapshot_config(config_dir)
    effective_world = world or config["default_world"]

    ssh_key, dest_root = _resolve_paths(config)
    if not ssh_key.is_file():
        raise UpgradeCliError(3, f"ssh key not found: {ssh_key}")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot_name = f"{effective_world}-{timestamp}"
    snapshot_dir = dest_root / snapshot_name
    world_dir = snapshot_dir / "world"

    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        world_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not create snapshot directory {snapshot_dir}: {exc}") from exc

    _log_cli.info(f"snapshot: {snapshot_dir}")
    _log_cli.info(f"source: {config['source_user']}@{config['source_host']}:{config['source_path']}")

    argv = _build_rsync_argv(config, ssh_key, world_dir)
    effective_runner = runner if runner is not None else _default_runner
    _log_cli.info(f"running rsync (timeout={config['timeout_seconds']}s)")
    try:
        rc, _stdout, stderr = effective_runner(argv, float(config["timeout_seconds"]))
    except Exception as exc:
        raise UpgradeCliError(1, f"rsync invocation raised: {exc}") from exc

    if rc == 127:
        raise UpgradeCliError(1, f"rsync not available: {stderr.strip()[:300]}")
    if rc == 124:
        raise UpgradeCliError(1, f"rsync timed out after {config['timeout_seconds']}s")
    if rc != 0:
        raise UpgradeCliError(1, f"rsync failed (exit {rc}): {stderr.strip()[:300]}")

    _log_cli.info("computing manifest")
    try:
        entries = _walk_manifest(world_dir)
    except OSError as exc:
        raise UpgradeCliError(1, f"manifest walk failed: {exc}") from exc

    if not entries:
        raise UpgradeCliError(1, "snapshot is empty (rsync succeeded but produced no files)")

    snapshot_id = _write_manifest(snapshot_dir, entries, config["source_path"])
    _log_cli.info("")
    _log_cli.info(f"files: {len(entries)}")
    _log_cli.info(f"snapshot_id: {snapshot_id}")
    _log_cli.info(f"snapshot dir: {snapshot_dir}")
    _log_cli.info(f"world dir: {world_dir}")
    return 0
