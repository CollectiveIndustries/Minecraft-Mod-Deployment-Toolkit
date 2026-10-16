# src/minecraft/modpack_upgrade/pw_toml.py

"""Deterministic ``.pw.toml`` writer (spec Section 5, decision doc Section 3).

The writer produces the exact schema consumed by
``deploy_pack.deps.parse_prism_toml``:

  * ``filename`` - required, exact staged artifact filename.
  * ``name`` - optional display name.
  * ``side`` - always explicit: ``client`` / ``server`` / ``both`` or
    the sentinel ``unknown``. Never omitted: an omitted ``side``
    defaults to ``both`` in the parser and silently misclassifies.
  * ``[download]`` - emitted when url, hash-format, and hash are all
    present.
  * ``[update.modrinth]`` or ``[update.curseforge]`` - exactly one for
    an externally sourced artifact; zero for an in-house artifact that
    has no upstream identity. Never both.
  * ``[[x-prismlauncher-dependencies]]`` - project-ID edges only.
    Mod-ID edges stay in the packing slip and never leak here.

Output is byte-for-byte deterministic: field order is fixed, strings
are NFC-normalized, and only ``tomli_w``'s canonical emitter runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomli_w
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError("tomli-w is required by minecraft.modpack_upgrade.pw_toml; install it with `pdm add tomli-w`") from exc

from . import canonical
from .errors import SchemaError

__all__ = ["Dependency", "PwTomlEntry", "render_pw_toml", "write_pw_toml"]

VALID_SIDES: tuple[str, ...] = ("client", "server", "both", "unknown")


@dataclass(frozen=True)
class Dependency:
    """A project-ID dependency edge for ``[[x-prismlauncher-dependencies]]``."""

    addon_id: str
    type: str  # "required" | "optional" | ...


@dataclass(frozen=True)
class PwTomlEntry:
    """The full set of fields the writer needs to emit a ``.pw.toml``."""

    filename: str
    side: str

    name: str | None = None

    download_url: str | None = None
    download_hash_format: str | None = None
    download_hash: str | None = None

    modrinth_mod_id: str | None = None
    modrinth_version: str | None = None

    curseforge_project_id: int | None = None
    curseforge_file_id: int | None = None

    dependencies: tuple[Dependency, ...] = ()

    def provider_block_count(self) -> int:
        """Returns the number of provider blocks for the instance."""
        n = 0
        if self.modrinth_mod_id is not None or self.modrinth_version is not None:
            n += 1
        if self.curseforge_project_id is not None or self.curseforge_file_id is not None:
            n += 1
        return n


def _validate_filename(name: str) -> None:
    if not name:
        raise SchemaError("pw_toml: filename is required")
    if "\x00" in name:
        raise SchemaError("pw_toml: filename must not contain NUL")
    if "/" in name or "\\" in name:
        raise SchemaError("pw_toml: filename must not contain path separators")
    if name in (".", ".."):
        raise SchemaError("pw_toml: filename must not be '.' or '..'")


def _validate_download(entry: PwTomlEntry) -> None:
    fields = (entry.download_url, entry.download_hash_format, entry.download_hash)
    present = [f is not None for f in fields]
    if any(present) and not all(present):
        raise SchemaError("pw_toml: [download] requires url, hash-format, and hash together")


def _validate_providers(entry: PwTomlEntry) -> None:
    count = entry.provider_block_count()
    if count > 1:
        raise SchemaError(f"pw_toml: at most one provider update block is allowed, got {count}")
    has_modrinth = entry.modrinth_mod_id is not None or entry.modrinth_version is not None
    if has_modrinth and not (entry.modrinth_mod_id and entry.modrinth_version):
        raise SchemaError("pw_toml: [update.modrinth] requires both mod-id and version")
    has_cf = entry.curseforge_project_id is not None or entry.curseforge_file_id is not None
    if has_cf and (entry.curseforge_project_id is None or entry.curseforge_file_id is None):
        raise SchemaError("pw_toml: [update.curseforge] requires both project-id and file-id")


def _validate_entry(entry: PwTomlEntry) -> None:
    _validate_filename(entry.filename)
    if entry.side not in VALID_SIDES:
        raise SchemaError(f"pw_toml: side must be one of {VALID_SIDES}, got {entry.side!r}")
    _validate_download(entry)
    _validate_providers(entry)


def _build_download_block(entry: PwTomlEntry) -> dict[str, str] | None:
    if entry.download_url is None:
        return None
    return {
        "url": entry.download_url,
        "hash-format": entry.download_hash_format or "",
        "hash": entry.download_hash or "",
    }


def _build_update_block(entry: PwTomlEntry) -> dict[str, Any] | None:
    if entry.modrinth_mod_id is not None:
        return {
            "modrinth": {
                "mod-id": entry.modrinth_mod_id,
                "version": entry.modrinth_version,
            }
        }
    if entry.curseforge_project_id is not None:
        return {
            "curseforge": {
                "project-id": entry.curseforge_project_id,
                "file-id": entry.curseforge_file_id,
            }
        }
    return None


def _build_dependencies_block(entry: PwTomlEntry) -> list[dict[str, str]] | None:
    if not entry.dependencies:
        return None
    return [{"addonId": canonical.nfc(dep.addon_id), "type": dep.type} for dep in entry.dependencies]


def _build_body(entry: PwTomlEntry) -> dict[str, Any]:
    body: dict[str, Any] = {}
    body["filename"] = canonical.nfc(entry.filename)
    if entry.name is not None:
        body["name"] = canonical.nfc(entry.name)
    body["side"] = entry.side

    download = _build_download_block(entry)
    if download is not None:
        body["download"] = download

    update = _build_update_block(entry)
    if update is not None:
        body["update"] = update

    deps = _build_dependencies_block(entry)
    if deps is not None:
        body["x-prismlauncher-dependencies"] = deps

    return body


def render_pw_toml(entry: PwTomlEntry) -> bytes:
    """Render ``entry`` to ``.pw.toml`` bytes. Does not write to disk."""
    _validate_entry(entry)
    body = _build_body(entry)
    return tomli_w.dumps(body).encode("utf-8")


def write_pw_toml(entry: PwTomlEntry, dest: Path) -> str:
    """Write ``entry`` to ``dest``. Return the SHA-256 of the written bytes."""
    data = render_pw_toml(entry)
    dest.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
