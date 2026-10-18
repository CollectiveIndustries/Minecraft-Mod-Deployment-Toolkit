# src/minecraft/modpack_upgrade/cli/commands/_compose.py

"""Compose file resolution and hashing, shared across the CLI commands.

Two responsibilities:

  * :func:`resolve_compose_path` - the single source of truth for
    "where is the effective ``docker-compose.yml``". Reads
    ``config.d/deploy_pack.toml`` ``[docker].compose_file`` when present
    and falls back to ``<project_root>/docker-compose.yml`` when the
    config is missing, malformed, or does not declare an override.
    Never raises: a broken config surfaces at the integrity check, not
    here.

  * :func:`compute_compose_sha256` - hash the effective compose file's
    raw bytes. This is what ``state.toml`` records as
    ``effective_compose_sha256``; ``acknowledge`` and ``materialize``
    both recompute it at their own time so drift between the two
    invocations is caught (Resolution_Report Section 6.3 step 5).
    Raises :class:`UpgradeCliError(3)` on any failure - missing file,
    read error, or parse error.

The leading underscore on the module name is deliberate: it is not part
of the public CLI surface. Callers (``acknowledge``, ``materialize``,
``update``) import the helpers directly; no other module should.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from minecraft.deploy_pack.config_model import load_compose
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = ["compute_compose_sha256", "resolve_compose_path"]


def resolve_compose_path(config_dir: Path, project_root: Path) -> Path:
    """Return the effective compose file path.

    Precedence:

      1. ``config.d/deploy_pack.toml`` ``[docker].compose_file`` when
         it exists, parses as TOML, declares a ``[docker]`` table, and
         that table contains a non-empty string ``compose_file`` key.
         Relative paths resolve against ``project_root``; absolute paths
         are used verbatim.
      2. Otherwise ``<project_root>/docker-compose.yml``.

    A malformed or unreadable ``deploy_pack.toml`` is treated as "no
    override": the default is returned. This is deliberate - callers
    invoke this function *before* the integrity check, and the integrity
    check is where a bad config should surface with a diagnostic. Making
    this function raise would shadow that diagnostic.
    """
    toml_path = config_dir / "deploy_pack.toml"
    if not toml_path.is_file():
        return project_root / "docker-compose.yml"
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return project_root / "docker-compose.yml"
    docker = data.get("docker", {})
    if not isinstance(docker, dict):
        return project_root / "docker-compose.yml"
    raw = docker.get("compose_file")
    if not isinstance(raw, str) or not raw:
        return project_root / "docker-compose.yml"
    path = Path(raw)
    return path if path.is_absolute() else project_root / path


def compute_compose_sha256(config_dir: Path, project_root: Path) -> str:
    """Return the SHA-256 of the effective compose file's raw bytes.

    Raises :class:`UpgradeCliError(3)` when:

      * the resolved compose file does not exist;
      * reading the file raises :class:`OSError`;
      * the file is not valid YAML / does not declare a ``services``
        table (delegated to :func:`load_compose`).

    Section 6.3 step 5 requires ``acknowledge`` and ``materialize`` to
    each recompute this at their own time - a compose edit between the
    two invocations is drift that must be caught, not silently accepted.
    """
    compose_path = resolve_compose_path(config_dir, project_root)
    if not compose_path.is_file():
        raise UpgradeCliError(3, f"compose file not found: {compose_path}")
    try:
        data = compose_path.read_bytes()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read compose file {compose_path}: {exc}") from exc
    parsed = load_compose(compose_path)
    if not parsed.ok:
        raise UpgradeCliError(3, f"compose unreadable: {parsed.error}")
    return hashlib.sha256(data).hexdigest()
