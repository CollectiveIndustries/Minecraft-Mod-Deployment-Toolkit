# src/minecraft/modpack_upgrade/cli/commands/_compose.py

"""Compose file resolution, hashing, and reconciliation, shared across the CLI.

Responsibilities:

  * :func:`resolve_compose_path` - single source of truth for "where is
    the effective ``docker-compose.yml``" (existing).

  * :func:`compute_compose_sha256` - hash the effective compose file's
    raw bytes; the value recorded in ``state.toml`` as
    ``effective_compose_sha256`` (existing).

  * :func:`reconcile_compose` - compute the env-var changes needed to
    bring the compose file's runtime fields into agreement with a target
    :class:`ComposeRuntime`. Read-only. Returns a :class:`ComposeDiff`
    the caller can present to the operator and then apply.

  * :func:`apply_compose_diff` - apply a :class:`ComposeDiff` atomically,
    verify the resulting file re-parses with the expected changes, and
    emit a WARN about possible formatting rewrites. Returns the resulting
    compose bytes so the caller can compute the new tree hash without
    re-reading.

The reconciliation reads and writes the compose file directly with
``yaml`` (PyYAML). It does NOT preserve YAML comments, anchors, or
non-canonical formatting on rewrite - the git diff is the operator's
review step, and the WARN tells them to look at it. A future improvement
could splice only the affected environment block to preserve comments;
the API stays the same.

The leading underscore on the module name is deliberate: it is not part
of the public CLI surface.
"""

from __future__ import annotations

import contextlib
import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import load_compose
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError

__all__ = [
    "ComposeDiff",
    "ComposeRuntime",
    "EnvChange",
    "apply_compose_diff",
    "compute_compose_sha256",
    "reconcile_compose",
    "resolve_compose_path",
]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Compose env-var convention (Manifest_Schema Section 2.1)
# ---------------------------------------------------------------------------
#
# Duplicated from ``update.py`` for one turn. T4 consolidates by having
# update.py import these names from ``_compose``. Keeping them private
# until then keeps the modules independent.

_ENV_MC_VERSION: tuple[str, ...] = ("VERSION", "MINECRAFT_VERSION")
_ENV_LOADER: tuple[str, ...] = ("TYPE",)
_ENV_LOADER_VERSION_BY_LOADER: dict[str, tuple[str, ...]] = {
    "forge": ("FORGE_VERSION", "FORGEVERSION"),
    "neoforge": ("NEOFORGE_VERSION", "NEOFORGEVERSION"),
    "fabric": ("FABRIC_LOADER_VERSION", "FABRICVERSION"),
    "quilt": ("QUILT_LOADER_VERSION", "QUILT_LOADER"),
}

_KNOWN_LOADERS: frozenset[str] = frozenset(_ENV_LOADER_VERSION_BY_LOADER)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposeRuntime:
    """The three runtime values Compose reconciliation cares about.

    ``loader`` is the canonical lowercase form (``forge`` / ``neoforge``
    / ``fabric`` / ``quilt``), matching the manifest ``[minecraft].loader``
    enum. The env-var convention carries it uppercase (``FORGE``), but the
    canonical form is what flows through the resolver and report.
    """

    minecraft_version: str
    loader: str
    loader_version: str


@dataclass(frozen=True)
class EnvChange:
    """One environment-variable change needed to reconcile a compose file.

    The ``(old_value, new_value)`` pair encodes the four possible actions:

      * ``(X, Y)`` where ``X != Y``: update the key from ``X`` to ``Y``.
      * ``(None, Y)``: add the key with value ``Y`` (it was absent).
      * ``(X, None)``: remove the key (it currently holds ``X``).
      * ``(None, None)``: never emitted.

    ``key`` is the env-var name exactly as it appears (or should appear)
    in the compose file's ``environment`` block.
    """

    key: str
    old_value: str | None
    new_value: str | None


@dataclass(frozen=True)
class ComposeDiff:
    """The set of env-var changes needed to reconcile a compose file.

    ``current`` is ``None`` when the file's env vars do not form a
    complete :class:`ComposeRuntime` (e.g. ``TYPE`` is absent, or the
    loader is unrecognized). ``reconcile_compose`` still computes changes
    in that case; it just cannot describe the "before" state as a
    :class:`ComposeRuntime`.

    ``changes`` is empty (``needed`` is ``False``) when the file already
    matches ``target``.
    """

    compose_path: Path
    target: ComposeRuntime
    current: ComposeRuntime | None
    changes: tuple[EnvChange, ...]

    @property
    def needed(self) -> bool:
        """True when at least one change is required."""
        return bool(self.changes)


# ---------------------------------------------------------------------------
# Existing helpers (unchanged)
# ---------------------------------------------------------------------------


def resolve_compose_path(config_dir: Path, project_root: Path) -> Path:
    """Return the effective compose file path.

    Precedence:

      1. ``config.d/deploy_pack.toml`` ``[docker].compose_file`` when
         it exists, parses as TOML, declares a ``[docker]`` table, and
         that table contains a non-empty string ``compose_file`` key.
      2. Otherwise ``<project_root>/docker-compose.yml``.

    A malformed or unreadable ``deploy_pack.toml`` is treated as "no
    override": the default is returned.
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

    Raises :class:`UpgradeCliError(3)` when the resolved file does not
    exist, cannot be read, or does not parse as a valid Compose file.
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


# ---------------------------------------------------------------------------
# Env-var parsing helpers (duplicated from update.py; consolidated in T4)
# ---------------------------------------------------------------------------


def _env_as_mapping(raw: Any) -> dict[str, str]:
    """Normalize a compose service's ``environment`` field to a mapping.

    Handles both dict form and ``- KEY=VALUE`` list form. Bare list
    entries that would reference the host env are dropped: this layer
    does not read host environment variables.
    """
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            out[str(key)] = "" if value is None else str(value)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, _, value = item.partition("=")
            out[key] = value
    return out


def _pick_first(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty value among ``keys`` in ``env``, or None."""
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return None


def _pick_key(env: dict[str, str], candidates: tuple[str, ...], *, default: str) -> str:
    """Return the first existing key from ``candidates``, or ``default``.

    Used to choose which name to update when multiple aliases exist
    (e.g. ``VERSION`` vs. ``MINECRAFT_VERSION``). Existing keys are
    preserved; a new key is inserted under ``default`` only when neither
    alias is present.
    """
    for key in candidates:
        if key in env:
            return key
    return default


def _normalize_loader(raw: str) -> str | None:
    """Map the compose ``TYPE`` value to the canonical loader name."""
    lowered = raw.strip().lower()
    return lowered if lowered in _KNOWN_LOADERS else None


# ---------------------------------------------------------------------------
# Compose service discovery and env extraction
# ---------------------------------------------------------------------------


def _find_minecraft_service(data: Any, compose_path: Path) -> dict:
    """Return the single service whose image declares a minecraft variant.

    Raises :class:`UpgradeCliError(3)` when:

      * ``data`` is not a mapping;
      * there is no ``services`` mapping;
      * no service's ``image`` contains ``"minecraft"``;
      * more than one service declares a minecraft image (ambiguous - the
        operator must pick one before reconciliation can proceed).
    """
    if not isinstance(data, dict):
        raise UpgradeCliError(3, f"compose file {compose_path}: top-level is not a mapping")
    services = data.get("services")
    if not isinstance(services, dict):
        raise UpgradeCliError(3, f"compose file {compose_path}: no [services] mapping")
    matches = [svc for svc in services.values() if isinstance(svc, dict) and "minecraft" in str(svc.get("image") or "").lower()]
    if not matches:
        raise UpgradeCliError(
            3,
            f"compose file {compose_path}: no service declares an image containing 'minecraft'; reconciliation requires exactly one",
        )
    if len(matches) > 1:
        raise UpgradeCliError(
            3,
            f"compose file {compose_path}: {len(matches)} services declare minecraft images; reconciliation requires exactly one",
        )
    return matches[0]


def _extract_current_runtime(env: dict[str, str]) -> ComposeRuntime | None:
    """Return the current runtime, or None when any value is missing/unrecognized.

    Returning ``None`` lets ``reconcile_compose`` still emit additions for
    missing fields: an incomplete compose block can be reconciled as long
    as the operator's intent is unambiguous.
    """
    mc = _pick_first(env, _ENV_MC_VERSION)
    if not mc:
        return None
    type_raw = _pick_first(env, _ENV_LOADER)
    if not type_raw:
        return None
    loader = _normalize_loader(type_raw)
    if loader is None:
        return None
    lv = _pick_first(env, _ENV_LOADER_VERSION_BY_LOADER[loader])
    if not lv:
        return None
    return ComposeRuntime(minecraft_version=mc, loader=loader, loader_version=lv)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile_compose(
    config_dir: Path,
    project_root: Path,
    target_runtime: ComposeRuntime,
) -> ComposeDiff:
    """Compute the env-var changes needed to match ``target_runtime``.

    Read-only. Raises :class:`UpgradeCliError(3)` on any file or parse
    failure, or when the minecraft service cannot be identified
    unambiguously.

    The computed diff has three parts, always in this order:

      1. minecraft version (existing ``VERSION`` or ``MINECRAFT_VERSION``);
      2. loader type (``TYPE``), uppercased to match the env convention;
      3. loader version (existing ``<LOADER>_VERSION`` alias from the
         target loader's candidate set).

    Additionally, when the loader is changing (e.g. ``FORGE`` ->
    ``NEOFORGE``), the old loader-version key is emitted for **removal**
    so the resulting file does not carry a stale setting. The removal is
    ordered after the additions.
    """
    if target_runtime.loader not in _ENV_LOADER_VERSION_BY_LOADER:
        raise UpgradeCliError(
            3,
            f"reconcile_compose: unknown target loader {target_runtime.loader!r}; expected one of {sorted(_ENV_LOADER_VERSION_BY_LOADER)}",
        )

    compose_path = resolve_compose_path(config_dir, project_root)
    if not compose_path.is_file():
        raise UpgradeCliError(3, f"compose file not found: {compose_path}")
    try:
        raw = compose_path.read_bytes()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read compose file {compose_path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise UpgradeCliError(3, f"compose file {compose_path} is not valid YAML: {exc}") from exc

    service = _find_minecraft_service(data, compose_path)
    env = _env_as_mapping(service.get("environment"))

    current = _extract_current_runtime(env)
    changes: list[EnvChange] = []

    # 1. Minecraft version.
    mc_key = _pick_key(env, _ENV_MC_VERSION, default=_ENV_MC_VERSION[0])
    if env.get(mc_key) != target_runtime.minecraft_version:
        changes.append(EnvChange(key=mc_key, old_value=env.get(mc_key), new_value=target_runtime.minecraft_version))

    # 2. Loader type.
    type_key = _ENV_LOADER[0]
    target_type = target_runtime.loader.upper()
    current_type = env.get(type_key)
    current_type_normalized = _normalize_loader(current_type) if current_type is not None else None
    if current_type != target_type:
        changes.append(EnvChange(key=type_key, old_value=current_type, new_value=target_type))

    # 3. Loader version.
    target_lv_keys = _ENV_LOADER_VERSION_BY_LOADER[target_runtime.loader]
    target_lv_key = _pick_key(env, target_lv_keys, default=target_lv_keys[0])
    if env.get(target_lv_key) != target_runtime.loader_version:
        changes.append(
            EnvChange(
                key=target_lv_key,
                old_value=env.get(target_lv_key),
                new_value=target_runtime.loader_version,
            )
        )

    # 4. Remove stale loader-version key when the loader changed.
    if current_type_normalized is not None and current_type_normalized != target_runtime.loader:
        stale_keys = _ENV_LOADER_VERSION_BY_LOADER[current_type_normalized]
        for stale_key in stale_keys:
            if stale_key in env and stale_key != target_lv_key:
                changes.append(EnvChange(key=stale_key, old_value=env[stale_key], new_value=None))

    return ComposeDiff(
        compose_path=compose_path,
        target=target_runtime,
        current=current,
        changes=tuple(changes),
    )


# ---------------------------------------------------------------------------
# Applying a diff
# ---------------------------------------------------------------------------


def apply_compose_diff(diff: ComposeDiff) -> bytes:
    """Apply ``diff`` atomically (if needed) and return the resulting bytes.

    Returns ``diff.compose_path``'s current bytes when ``diff.needed`` is
    False (no write performed). Returns the newly-written bytes
    otherwise. The caller can use the returned bytes to compute the new
    tree hash without re-reading.

    Verify-before-write: the serialized output is re-parsed and every
    change is confirmed before the temp file is renamed into place. A
    failure therefore leaves the original compose file untouched - no
    rollback is needed.

    Emits a WARN about possible comment/formatting rewrites so the
    operator knows to inspect ``git diff`` before committing.

    Raises :class:`UpgradeCliError(3)` on read, parse, serialize,
    verification, or write failure.
    """
    compose_path = diff.compose_path

    if not diff.needed:
        try:
            return compose_path.read_bytes()
        except OSError as exc:
            raise UpgradeCliError(3, f"compose file disappeared between reconcile and apply: {compose_path}: {exc}") from exc

    try:
        original = compose_path.read_bytes()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read compose file {compose_path}: {exc}") from exc

    try:
        data = yaml.safe_load(original)
    except yaml.YAMLError as exc:
        raise UpgradeCliError(3, f"compose file {compose_path} is not valid YAML: {exc}") from exc

    service = _find_minecraft_service(data, compose_path)
    env = _env_as_mapping(service.get("environment"))

    for change in diff.changes:
        if change.new_value is None:
            env.pop(change.key, None)
        else:
            env[change.key] = change.new_value
    service["environment"] = env

    try:
        new_bytes = yaml.safe_dump(data, default_flow_style=False, sort_keys=False).encode("utf-8")
    except yaml.YAMLError as exc:
        raise UpgradeCliError(3, f"could not serialize reconciled compose file: {exc}") from exc

    # Verify-before-write: re-parse and confirm every change is reflected.
    try:
        verify_data = yaml.safe_load(new_bytes)
    except yaml.YAMLError as exc:
        raise UpgradeCliError(3, f"reconciled compose file failed to re-parse: {exc}") from exc
    verify_service = _find_minecraft_service(verify_data, compose_path)
    verify_env = _env_as_mapping(verify_service.get("environment"))
    for change in diff.changes:
        if change.new_value is None:
            if change.key in verify_env:
                raise UpgradeCliError(
                    3,
                    f"internal: compose reconciliation failed to remove {change.key!r}",
                )
        elif verify_env.get(change.key) != change.new_value:
            raise UpgradeCliError(
                3,
                f"internal: compose reconciliation failed for {change.key!r}: expected {change.new_value!r}, got {verify_env.get(change.key)!r}",
            )

    # Atomic write.
    tmp = compose_path.with_suffix(compose_path.suffix + ".tmp")
    try:
        tmp.write_bytes(new_bytes)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not write temp compose file {tmp}: {exc}") from exc
    try:
        tmp.replace(compose_path)
    except OSError as exc:
        # Best-effort cleanup of the temp file before propagating.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise UpgradeCliError(3, f"could not replace compose file {compose_path}: {exc}") from exc

    _log.warning(
        f"compose reconciled: {len(diff.changes)} change(s) applied to {compose_path}. "
        f"Comments and formatting may have been rewritten; review the git diff before committing."
    )
    return new_bytes
