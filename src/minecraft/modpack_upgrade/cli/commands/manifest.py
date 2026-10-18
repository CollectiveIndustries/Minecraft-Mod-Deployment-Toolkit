# src/minecraft/modpack_upgrade/cli/commands/manifest.py

"""``minecraft-upgrade manifest check`` -- validate manifest syntax.

Parses ``modpack.manifest.toml`` as TOML and reports the top-level keys.
Schema validation (project IDs, versions, provider references) is
deferred until the manifest schema is frozen; the command currently
performs syntax-only validation.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = ["run"]


def _resolve_manifest_path(project_root: Path, path: str | None) -> Path:
    """Return the manifest path from ``path`` or the default."""
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = project_root / p
        return p
    return project_root / "sync" / "modpack.manifest.toml"


def _load_manifest(path: Path) -> dict:
    """Read and TOML-parse ``path``; raise UpgradeCliError on failure."""
    if not path.is_file():
        raise UpgradeCliError(3, f"manifest not found: {path}")
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise UpgradeCliError(3, f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read {path}: {exc}") from exc
    return data


def run(*, config_dir: Path, project_root: Path, path: str | None) -> int:
    """Parse the manifest, print its top-level keys. Returns 0 or 3."""
    del config_dir  # manifest path is project-relative
    manifest_path = _resolve_manifest_path(project_root, path)
    data = _load_manifest(manifest_path)

    print(f"manifest: {manifest_path}")
    print(f"top_level_keys: {len(data)}")
    for key in sorted(data.keys()):
        print(f"  {key}")
    print()
    print("status: syntax_ok")
    print("note: schema validation is not yet frozen; syntax-only for now")
    return 0
