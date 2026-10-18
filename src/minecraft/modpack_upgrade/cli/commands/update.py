# src/minecraft/modpack_upgrade/cli/commands/update.py

"""``minecraft-upgrade update --release-mode <mode>`` - resolve versions (Section 15.1.1).

Reads manifest + seed + side overrides. Queries providers. Walks the
dependency closure. Writes the resolution report and run state. Exits
per Section A6 (0 resolved, 6 awaiting acknowledgment, 4 failed, 3 preflight,
1 provider error).

Does NOT materialize. Downloads happen at ``materialize``.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from LoggingCore import get_logger

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import ProviderError, SchemaError
from minecraft.modpack_upgrade.inhouse import load_in_house_filenames
from minecraft.modpack_upgrade.manifest import Manifest, load_manifest
from minecraft.modpack_upgrade.overrides import load_side_overrides
from minecraft.modpack_upgrade.providers.config import build_registry, load_provider_config
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry
from minecraft.modpack_upgrade.report import write_report
from minecraft.modpack_upgrade.resolution_inputs import compute_resolution_inputs_sha256
from minecraft.modpack_upgrade.resolver import CycleError, Resolver, ResolverInputs
from minecraft.modpack_upgrade.seed import load_seed
from minecraft.modpack_upgrade.state import RunState, save_state

from . import _compose

__all__ = ["run"]

_log = get_logger(__name__)
_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.update")


_STATE_BY_REPORT_STATUS: dict[str, str] = {
    "complete": "resolved",
    "paused_awaiting_acknowledgment": "awaiting_acknowledgment",
    "failed": "failed",
}

_EXIT_BY_REPORT_STATUS: dict[str, int] = {
    "complete": 0,
    "paused_awaiting_acknowledgment": 6,
    "failed": 4,
}


# ---------------------------------------------------------------------------
# Manifest vs. Compose equivalence (Manifest_Schema Section 2, Section 8 rule 8)
# ---------------------------------------------------------------------------


# Env-var conventions accepted for the ``itzg/minecraft-server`` image family.
# See Project_Specs.md and the `itzg/minecraft-server` docs for the canonical
# key names. Both common forms (``VERSION`` and the loader-specific
# ``FORGE_VERSION`` / ``FABRIC_LOADER_VERSION`` / ...) are accepted.
_ENV_MC_VERSION: tuple[str, ...] = ("VERSION", "MINECRAFT_VERSION")
_ENV_LOADER: tuple[str, ...] = ("TYPE",)
_ENV_LOADER_VERSION_BY_LOADER: dict[str, tuple[str, ...]] = {
    "forge": ("FORGE_VERSION", "FORGEVERSION"),
    "neoforge": ("NEOFORGE_VERSION", "NEOFORGEVERSION"),
    "fabric": ("FABRIC_LOADER_VERSION", "FABRICVERSION"),
    "quilt": ("QUILT_LOADER_VERSION", "QUILT_LOADER"),
}


def _env_as_mapping(raw_env: Any) -> dict[str, str]:
    """Normalize a compose service's ``environment`` field to a mapping.

    docker-compose accepts two forms:

      environment:
        VERSION: "1.21.1"          # mapping

      environment:
        - VERSION=1.21.1           # list of "KEY=VALUE"

    Bare list entries (``- VERSION``) that reference the host env are
    dropped: this layer has no business reading host env vars.
    """
    out: dict[str, str] = {}
    if isinstance(raw_env, dict):
        for key, value in raw_env.items():
            out[str(key)] = "" if value is None else str(value)
    elif isinstance(raw_env, list):
        for item in raw_env:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, _, value = item.partition("=")
            out[key] = value
    return out


def _pick_first(env: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty value among ``keys`` in ``env``, or None."""
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return None


def _normalize_loader(value: str) -> str | None:
    """Map the compose ``TYPE`` value to the manifest loader enum."""
    lowered = value.strip().lower()
    if lowered in ("forge", "neoforge", "fabric", "quilt"):
        return lowered
    return None


def _extract_from_env(env: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Return ``(mc_version, loader, loader_version)`` from an env mapping.

    Returns ``None`` when any of the three values is not declared - the
    caller treats that as "cannot verify" per Manifest_Schema Section 8.
    """
    mc = _pick_first(env, _ENV_MC_VERSION)
    loader_raw = _pick_first(env, _ENV_LOADER)
    if not mc or not loader_raw:
        return None
    loader = _normalize_loader(loader_raw)
    if loader is None:
        return None
    loader_version = _pick_first(env, _ENV_LOADER_VERSION_BY_LOADER[loader])
    if not loader_version:
        return None
    return (mc, loader, loader_version)


def _extract_compose_runtime(compose_path: Path) -> tuple[str, str, str] | None:
    """Return ``(minecraft_version, loader, loader_version)`` from Compose.

    Best-effort extraction from the ``itzg/minecraft-server`` env-var
    convention. Returns ``None`` when no minecraft service declares all
    three values - the caller turns that into exit 3, because
    Manifest_Schema Section 8 rule 8 requires an affirmative match.
    """
    try:
        raw = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    services = raw.get("services")
    if not isinstance(services, dict):
        return None
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        image = str(svc.get("image") or "").lower()
        if "minecraft" not in image:
            continue
        extracted = _extract_from_env(_env_as_mapping(svc.get("environment")))
        if extracted is not None:
            return extracted
    return None


def _verify_manifest_matches_compose(manifest: Manifest, compose_path: Path) -> None:
    """Enforce Manifest_Schema Section 2 for ``--release-mode update``.

    ``update`` mode is a same-version update: the manifest MUST describe
    the runtime that is already deployed. A mismatch - or an inability
    to affirmatively confirm equivalence - is exit 3 per Manifest_Schema
    Section 8 rule 8.
    """
    runtime = _extract_compose_runtime(compose_path)
    if runtime is None:
        raise UpgradeCliError(
            3,
            f"--release-mode update requires [minecraft] to match the effective Compose runtime, "
            f"but {compose_path} does not declare VERSION/TYPE/<loader>_VERSION env vars on any "
            f"minecraft service. Either declare them, or pass --release-mode upgrade and reconcile "
            f"at promote.",
        )
    mc, loader, loader_version = runtime
    if manifest.minecraft_version != mc or manifest.loader != loader or manifest.loader_version != loader_version:
        raise UpgradeCliError(
            3,
            f"--release-mode update manifest/Compose mismatch: "
            f"manifest [minecraft] version={manifest.minecraft_version!r} "
            f"loader={manifest.loader!r} loader_version={manifest.loader_version!r}; "
            f"compose version={mc!r} loader={loader!r} loader_version={loader_version!r}. "
            f"Reconcile the manifest or pass --release-mode upgrade.",
        )


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current UTC time as ISO 8601 with Z suffix."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_side_overrides_sha256(path: Path) -> str:
    """Return the SHA-256 of the ``side_overrides.toml`` bytes."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise UpgradeCliError(3, f"could not read side_overrides.toml at {path}: {exc}") from exc


def _load_inputs(
    config_dir: Path,
    project_root: Path,
    release_mode: str,
    env: Mapping[str, str] | None,
    registry: ProviderRegistry | None,
) -> tuple[ResolverInputs, str | None]:
    """Load every input the resolver needs.

    Returns ``(inputs, effective_compose_sha256)``. The compose hash is
    ``None`` for ``upgrade`` mode (which has no compose authority) and
    a real digest for ``update`` mode.

    Raises ``UpgradeCliError(3)`` on any failure, including the
    Manifest_Schema Section 2 ``update``-mode equivalence check.

    Compose-file resolution and hashing are delegated to :mod:`._compose` -
    the single source of truth across all CLI commands.
    """
    manifest_path = project_root / "sync" / "modpack.manifest.toml"
    try:
        manifest = load_manifest(manifest_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    # Manifest_Schema Section 2 / Section 8 rule 8: update mode requires the
    # manifest to describe the currently-deployed runtime. Compute the compose
    # SHA-256 here too, so a broken compose aborts before any report
    # is written (write-order fix).
    effective_compose_sha: str | None = None
    if release_mode == "update":
        compose_path = _compose.resolve_compose_path(config_dir, project_root)
        if not compose_path.is_file():
            raise UpgradeCliError(
                3,
                f"--release-mode update requires a Compose file at {compose_path} to verify manifest/Compose equivalence.",
            )
        _verify_manifest_matches_compose(manifest, compose_path)
        effective_compose_sha = _compose.compute_compose_sha256(config_dir, project_root)

    try:
        in_house = tuple(load_in_house_filenames(config_dir))
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    modpack_dir = project_root / "sync" / "downloads"
    try:
        seed = load_seed(modpack_dir / ".index", modpack_dir, list(in_house))
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    overrides_path = config_dir / "side_overrides.toml"
    if not overrides_path.is_file():
        raise UpgradeCliError(3, f"side_overrides.toml is required: {overrides_path}")
    try:
        overrides = load_side_overrides(overrides_path)
    except Exception as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    side_overrides_sha = _read_side_overrides_sha256(overrides_path)

    try:
        resolution_inputs_sha = compute_resolution_inputs_sha256(
            release_mode=release_mode,
            manifest=manifest,
            in_house_filenames=in_house,
            seed=seed.snapshot_entries,
            side_overrides=overrides,
        )
    except ValueError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    effective_registry = registry
    if effective_registry is None:
        provider_config = load_provider_config(config_dir, env=env)
        effective_registry = build_registry(provider_config)

    inputs = ResolverInputs(
        manifest=manifest,
        release_mode=release_mode,
        in_house_filenames=in_house,
        seed=seed,
        side_overrides=overrides,
        registry=effective_registry,
        manifest_sha256=manifest.raw_sha256,
        resolution_inputs_sha256=resolution_inputs_sha,
        side_overrides_sha256=side_overrides_sha,
    )
    return (inputs, effective_compose_sha)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _write_state(
    *,
    state_dir: Path,
    resolution_inputs_sha256: str,
    status: str,
    run_id: str,
    timestamp: str,
    release_mode: str,
    report_sha256: str,
    manifest_path: Path,
    effective_compose_sha256: str | None,
) -> RunState:
    """Write the run state to ``<state_dir>/runs/<run_id>/state.toml``."""
    state = RunState(
        run_id=run_id,
        status=status,
        release_mode=release_mode,
        paused_at_utc=timestamp,
        report_sha256=report_sha256,
        resolution_inputs_sha256=resolution_inputs_sha256,
        manifest_path=str(manifest_path),
        effective_compose_sha256=effective_compose_sha256,
    )
    save_state(state_dir / "runs" / run_id / "state.toml", state)
    return state


def _resolve_state_dir(config_dir: Path) -> Path:
    """Return the daemon's state directory for the given config dir."""
    daemon_config = load_daemon_config(config_dir)
    return Path(daemon_config.state_dir)


def _print_next_command(state_status: str, run_id: str) -> None:
    """Print the next command for the operator."""
    if state_status == "resolved":
        _log_cli.info(f"next: materialize --run-id {run_id}")
    elif state_status == "awaiting_acknowledgment":
        _log_cli.info(f"next: acknowledge {run_id}")
    elif state_status == "failed":
        _log_cli.error("resolution failed; see the report for details")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    config_dir: Path,
    project_root: Path,
    release_mode: str,
    env: Mapping[str, str] | None = None,
    registry: ProviderRegistry | None = None,
    run_id: str | None = None,
    now: str | None = None,
    state_dir: Path | None = None,
) -> int:
    """Execute ``minecraft-upgrade update --release-mode <mode>``. Returns exit code."""
    if release_mode not in ("update", "upgrade"):
        raise UpgradeCliError(2, f"--release-mode must be 'update' or 'upgrade', got {release_mode!r}")

    inputs, effective_compose_sha = _load_inputs(config_dir, project_root, release_mode, env, registry)
    actual_run_id = run_id or str(uuid.uuid4())
    actual_now = now or _now_iso()

    try:
        report = Resolver(inputs).resolve(run_id=actual_run_id, generated_at_utc=actual_now)
    except CycleError as exc:
        raise UpgradeCliError(3, f"dependency cycle detected: {' -> '.join(exc.chain)}") from exc
    except ProviderError as exc:
        raise UpgradeCliError(1, str(exc)) from exc

    report_path = project_root / "www" / "runs" / actual_run_id / "resolution-report.toml"
    report_sha = write_report(report_path, report)

    effective_state_dir = state_dir if state_dir is not None else _resolve_state_dir(config_dir)
    state_status = _STATE_BY_REPORT_STATUS[report.status]
    exit_code = _EXIT_BY_REPORT_STATUS[report.status]

    _write_state(
        state_dir=effective_state_dir,
        resolution_inputs_sha256=inputs.resolution_inputs_sha256,
        status=state_status,
        run_id=actual_run_id,
        timestamp=actual_now,
        release_mode=release_mode,
        report_sha256=report_sha,
        manifest_path=project_root / "sync" / "modpack.manifest.toml",
        effective_compose_sha256=effective_compose_sha,
    )

    _log_cli.info(f"resolution report: {report_path}")
    _log_cli.info(f"run state: {effective_state_dir / 'runs' / actual_run_id / 'state.toml'}")
    _log_cli.info(f"status: {report.status}")
    if report.lost or report.cascade:
        _log_cli.info(f"losses: {len(report.lost)} lost, {len(report.cascade)} cascaded")

    _print_next_command(state_status, actual_run_id)
    return exit_code
