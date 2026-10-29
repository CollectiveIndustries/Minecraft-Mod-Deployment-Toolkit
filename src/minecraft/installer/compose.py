# src/minecraft/installer/compose.py

"""Docker Compose integration: derive instance roots, bring the stack up."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from minecraft.deploy_pack.config_model import (
    derive_instance_root,
    load_compose,
    resolve_compose_path,
)

from .errors import ComposeError

__all__ = ["bring_up", "derive_instance_roots"]


def derive_instance_roots(
    compose_path: Path,
    *,
    log: Any,
) -> list[tuple[str, Path]]:
    """Return ``[(container_name, instance_root), ...]`` from a compose file.

    Services without a ``/data`` or ``/data/*`` bind are skipped (e.g. an
    nginx service, a Velocity proxy). Services without a ``container_name``
    are skipped too.
    """
    result = load_compose(compose_path)
    if not result.ok:
        raise ComposeError(f"could not load {compose_path}: {result.error}")
    compose = result.file
    out: list[tuple[str, Path]] = []
    for svc in compose.services.values():
        if svc.container_name is None:
            log.debug(f"skipping service {svc.name!r} with no container_name")
            continue
        raw_root = derive_instance_root(svc)
        if raw_root is None:
            log.debug(f"skipping service {svc.name!r}: no /data bind")
            continue
        resolved = resolve_compose_path(raw_root, compose.base_dir)
        out.append((svc.container_name, resolved))
    return out


def bring_up(compose_path: Path, *, log: Any) -> None:
    """Run ``docker compose -f <compose_path> up -d``."""
    if not compose_path.is_file():
        raise ComposeError(f"compose file not found: {compose_path}")
    args = ["docker", "compose", "-f", str(compose_path), "up", "-d"]
    log.debug(f"running: {' '.join(args)}")
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ComposeError("docker is not installed or not on PATH") from exc
    if result.returncode != 0:
        raise ComposeError(f"docker compose up failed (exit {result.returncode}): {result.stderr.strip()}")
    log.info(f"docker compose up succeeded for {compose_path.name}")
