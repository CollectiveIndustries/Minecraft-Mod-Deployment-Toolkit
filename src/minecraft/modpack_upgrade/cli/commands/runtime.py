# src/minecraft/modpack_upgrade/cli/commands/runtime.py

"""``minecraft-upgrade runtime inspect`` -- report effective runtime.

Reads the Compose file referenced by ``[docker].compose_file`` in
``config.d/deploy_pack.toml`` and prints each service's image, platform,
and container name. This is a read-only inspection -- it does not touch
containers.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from minecraft.deploy_pack.config_model import load_compose
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = ["run"]


def _resolve_compose_file(config_dir: Path, project_root: Path) -> Path:
    """Return the compose file path from ``[docker].compose_file`` or a default."""
    toml_path = config_dir / "deploy_pack.toml"
    if not toml_path.is_file():
        return project_root / "docker-compose.yml"
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpgradeCliError(3, f"could not read {toml_path}: {exc}") from exc
    docker = data.get("docker", {})
    if not isinstance(docker, dict):
        return project_root / "docker-compose.yml"
    raw = docker.get("compose_file")
    if not isinstance(raw, str) or not raw:
        return project_root / "docker-compose.yml"
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path


def run(*, config_dir: Path, project_root: Path) -> int:
    """Print the effective runtime derived from Compose. Returns 0 or 3."""
    compose_file = _resolve_compose_file(config_dir, project_root)
    if not compose_file.is_file():
        raise UpgradeCliError(3, f"compose file not found: {compose_file}")

    result = load_compose(compose_file)
    if not result.ok:
        raise UpgradeCliError(3, f"compose unreadable: {result.error}")
    compose = result.file
    assert compose is not None

    print(f"compose: {compose.path}")
    print(f"services: {len(compose.services)}")
    print()
    for name, svc in compose.services.items():
        print(f"  [{name}]")
        print(f"    image: {svc.image or '(unset)'}")
        print(f"    platform: {svc.platform or '(unset)'}")
        print(f"    container_name: {svc.container_name or '(unset)'}")
    return 0
