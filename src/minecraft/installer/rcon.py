# src/minecraft/installer/rcon.py

"""RCON password distribution into server.properties files.

Reuses :mod:`minecraft.deploy_pack.properties` so the three managed keys
(``enable-rcon``, ``rcon.password``, ``rcon.port``) are edited with the
same atomic, comment-preserving splice the resource-pack scope uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from minecraft.deploy_pack.properties import PropertyEdit, apply_edits

from .errors import SecretsError

__all__ = ["configure_rcon", "configure_server_properties"]

_RCON_PORT = "25575"


def configure_server_properties(path: Path, secret: str) -> bool:
    """Ensure ``path`` has the three RCON keys set.

    Creates the file (with the RCON keys only) if it does not exist.
    Returns True iff the file changed.
    """
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    edits = [
        PropertyEdit("enable-rcon", "true"),
        PropertyEdit("rcon.password", secret),
        PropertyEdit("rcon.port", _RCON_PORT),
    ]
    diff = apply_edits(path, edits)
    return diff.any


def configure_rcon(
    instance_roots: Iterable[tuple[str, Path]],
    secret: str,
    *,
    log: Any,
) -> list[Path]:
    """Configure RCON for each instance root; return the touched paths."""
    touched: list[Path] = []
    for container_name, root in instance_roots:
        path = root / "server.properties"
        try:
            changed = configure_server_properties(path, secret)
        except Exception as exc:
            raise SecretsError(f"could not configure RCON in {path}: {exc}") from exc
        state = "updated" if changed else "unchanged"
        log.info(f"[{container_name}] {path}: {state}")
        if changed:
            touched.append(path)
    return touched
