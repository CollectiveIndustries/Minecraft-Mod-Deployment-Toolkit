# src/minecraft/modpack_upgrade/cli/commands/manifest.py

"""``minecraft-upgrade manifest check`` -- validate a manifest against Section 8.

Parses and schema-validates ``sync/modpack.manifest.toml`` per
``docs/Manifest_Schema.md`` Section 1, Section 3, and Section 8. On success, prints the
parsed fields and the raw-bytes SHA-256. On failure, prints every
violation to stderr and exits 3.

``--release-mode update`` manifest-vs-Compose equivalence (Section 2 of the
manifest schema) is NOT evaluated here; that is the caller's
responsibility during ``update``. ``manifest check`` is context-free.
"""

from __future__ import annotations

from pathlib import Path

from LoggingCore import get_logger

from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SchemaError
from minecraft.modpack_upgrade.manifest import (
    load_manifest,
    validate_manifest_bytes,
)

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.manifest")


def _resolve_manifest_path(project_root: Path, path: str | None) -> Path:
    """Return the manifest path from ``path`` or the default."""
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = project_root / p
        return p
    return project_root / "sync" / "modpack.manifest.toml"


def run(*, config_dir: Path, project_root: Path, path: str | None) -> int:
    """Validate the manifest. Returns 0 on success, 3 on any violation."""
    del config_dir  # manifest path is project-relative
    manifest_path = _resolve_manifest_path(project_root, path)

    if not manifest_path.is_file():
        raise UpgradeCliError(3, f"manifest not found: {manifest_path}")

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read {manifest_path}: {exc}") from exc

    _, violations = validate_manifest_bytes(raw)
    if violations:
        _log_cli.info(f"manifest: {manifest_path}")
        _log_cli.error(f"status: invalid ({len(violations)} violation(s))")
        for v in violations:
            _log_cli.error(f"  - {v}")
        return 3

    try:
        m = load_manifest(manifest_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    _log_cli.info(f"manifest: {manifest_path}")
    _log_cli.info(f"raw_sha256: {m.raw_sha256}")
    _log_cli.info(f"[minecraft].version: {m.minecraft_version}")
    _log_cli.info(f"[minecraft].loader: {m.loader}")
    _log_cli.info(f"[minecraft].loader_version: {m.loader_version}")
    _log_cli.info(f"[policy].selection: {m.selection}")
    _log_cli.info(f"[policy].no_version: {m.no_version}")
    _log_cli.info("")
    _log_cli.info("status: valid")
    return 0
