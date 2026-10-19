# src/minecraft/deploy_pack/daemon/deploy_runtime.py

"""Assemble the deploy callable from daemon config (spec v1.3 Section 12.4).

The daemon's ``/deploy`` endpoint is served by a callable that:

1. Loads ``deploy_pack.toml`` via ``config_model.load_deployment_config``.
2. Extracts the runtime facts the orchestrator needs (containers,
   stop timeouts, mods_dir, world_dir, protect patterns).
3. Selects RCON transports per container.
4. Reads the prior active release (for recovery).
5. Assembles :class:`deploy.DeployRequest` and invokes :func:`deploy.deploy`.

Every failure path returns a :class:`DeployOutcome` -- the endpoint
translates it into a typed exception via
:func:`plugins.http._outcome_to_exception`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import (
    DeploymentConfig,
    InstanceConfig,
    derive_mods_dir,
    load_deployment_config,
    resolve_compose_path,
)
from minecraft.deploy_pack.docker_runtime import (
    DockerRuntime,
    RconTransport,
    select_rcon_transport,
)
from minecraft.deploy_pack.errors import ConfigError, DockerUnavailableError
from minecraft.deploy_pack.files import load_protect_patterns
from minecraft.modpack_upgrade.slip import load_slip

from .config import MinecraftDaemonConfig
from .deploy import DeployOutcome, DeployRequest, deploy
from .health import HealthCheckSpec
from .health_probe import DockerHealthProbe
from .journal import DeploymentJournal
from .lock import DeploymentLock
from .pull import PullRequest
from .service_control import DockerServiceControl

__all__ = ["build_deploy_callable"]


_log = get_logger(__name__)


_ACTIVE_RELEASE_FILENAMES = ("active-release", "active-release.txt")
_DEFAULT_STARTUP_SENTINEL = "Done ("


class _SystemClock:
    """Wall clock backed by :func:`time.monotonic` and :func:`time.sleep`."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds``."""
        if seconds > 0:
            time.sleep(seconds)


_SYSTEM_CLOCK = _SystemClock()


# ---------------------------------------------------------------------------
# Config resolution helpers
# ---------------------------------------------------------------------------


def _read_active_release(state_dir: Path) -> str | None:
    """Return the currently active release ID, or None.

    Scans both ``active-release`` (FileStateStore convention) and
    ``active-release.txt`` (legacy CLI convention) so that the recovery
    path works regardless of which producer wrote the pointer.
    """
    for name in _ACTIVE_RELEASE_FILENAMES:
        path = state_dir / name
        if not path.is_file():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _extract_containers(deploy_config: DeploymentConfig) -> tuple[tuple[str, ...], dict[str, int]]:
    """Return the container names and per-container stop timeouts."""
    containers: list[str] = []
    timeouts: dict[str, int] = {}
    for inst in deploy_config.instances.values():
        containers.append(inst.container)
        timeouts[inst.container] = inst.stop_grace_seconds
    return (tuple(containers), timeouts)


def _resolve_mods_dir(deploy_config: DeploymentConfig) -> Path | None:
    """Return the shared mods_dir from the first instance's compose service."""
    if not deploy_config.compose.ok or deploy_config.compose.file is None:
        return None
    for inst in deploy_config.instances.values():
        if inst.service is None:
            continue
        compose_mods = derive_mods_dir(inst.service)
        if compose_mods is None:
            continue
        return resolve_compose_path(compose_mods, deploy_config.compose.file.base_dir)
    return None


def _resolve_world_dir(first_inst: InstanceConfig) -> Path | None:
    """Return the first instance's world directory, or None."""
    if first_inst.instance_root is None:
        return None
    return first_inst.instance_root / "world"


def _select_transports(
    deploy_config: DeploymentConfig,
    runtime: DockerRuntime,
) -> tuple[dict[str, RconTransport], dict[str, str]]:
    """Select RCON transports per container; return (transports, errors).

    Containers whose transport selection fails (missing service,
    missing secret, port publishing ambiguity) are omitted from the
    transport map; the error is recorded so the health probe can
    surface a specific reason if the check reaches the RCON step.
    """
    transports: dict[str, RconTransport] = {}
    errors: dict[str, str] = {}
    if deploy_config.compose.file is None:
        return transports, errors
    for name, inst in deploy_config.instances.items():
        if inst.service is None:
            errors[inst.container] = f"no compose service matched for instance {name!r}"
            continue
        try:
            transport = select_rcon_transport(
                runtime,
                inst.service,
                deploy_config.compose.file,
                inst.container,
                deploy_config.docker.rcon_host,
            )
        except (ConfigError, DockerUnavailableError) as exc:
            errors[inst.container] = str(exc)
            _log.warning(f"deploy_runtime: RCON selection failed for {inst.container!r}: {exc}")
            continue
        transports[inst.container] = transport
    return transports, errors


def _resolve_is_upgrade(config: MinecraftDaemonConfig, release_id: str) -> tuple[bool, DeployOutcome | None]:
    """Return (is_upgrade, None) or (False, failure_outcome)."""
    slip_path = config.release_store / release_id / "packing_slip.toml"
    if not slip_path.is_file():
        return (
            False,
            _fail_outcome(release_id, "config", f"slip not found: {slip_path}", 22),
        )
    try:
        slip = load_slip(slip_path)
    except Exception as exc:
        return (
            False,
            _fail_outcome(release_id, "config", f"slip invalid: {exc}", 22),
        )
    return (slip.release_mode == "upgrade", None)


# ---------------------------------------------------------------------------
# Request assembly
# ---------------------------------------------------------------------------


def _build_health_specs(containers: tuple[str, ...]) -> tuple[HealthCheckSpec, ...]:
    """Return one :class:`HealthCheckSpec` per container with defaults."""
    return tuple(HealthCheckSpec(container=c, startup_sentinel=_DEFAULT_STARTUP_SENTINEL) for c in containers)


def _build_pull_request(config: MinecraftDaemonConfig, release_id: str) -> PullRequest:
    """Assemble the pull request from daemon config."""
    return PullRequest(
        source_host=config.source_host,
        source_user=config.source_user,
        source_path=config.source_path,
        ssh_key=config.ssh_key,
        target_root=config.release_store,
        release_id=release_id,
    )


def _fail_outcome(release_id: str, stage: str, message: str, code: int = 22) -> DeployOutcome:
    """Return a failure outcome with the given stage and code."""
    _log.error(f"deploy_runtime: {release_id!r} failed at {stage!r}: {message}")
    return DeployOutcome(
        success=False,
        release_id=release_id,
        stage=stage,
        message=message,
        error_code=code,
    )


def _check_pull_source_configured(config: MinecraftDaemonConfig, release_id: str) -> DeployOutcome | None:
    """Return a failure outcome if the pull source is not configured."""
    if not config.source_host:
        return _fail_outcome(release_id, "config", "[daemon].source_host is not configured", 22)
    return None


def _build_runtime_glue(
    config: MinecraftDaemonConfig,
    runtime: DockerRuntime,
    client: Any,
    deploy_config: DeploymentConfig,
    release_id: str,
) -> tuple[DockerServiceControl, DockerHealthProbe, tuple[str, ...]] | DeployOutcome:
    """Build the service control and health probe.

    Returns ``(control, probe, containers)`` on success or a failure
    :class:`DeployOutcome` otherwise.
    """
    containers, stop_timeouts = _extract_containers(deploy_config)
    if not containers:
        return _fail_outcome(release_id, "config", "no instances configured", 22)
    transports, rcon_errors = _select_transports(deploy_config, runtime)
    control = DockerServiceControl(runtime=runtime, stop_timeouts=stop_timeouts)
    probe = DockerHealthProbe(
        client=client,
        rcon_transports=transports,
        rcon_errors=rcon_errors,
    )
    return (control, probe, containers)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _deploy_one(
    config: MinecraftDaemonConfig,
    config_dir: Path,
    runtime: DockerRuntime,
    client: Any,
    journal: DeploymentJournal,
    lock: DeploymentLock,
    release_id: str,
) -> DeployOutcome:
    """Run one deployment; return the outcome. Never raises."""
    if (source_error := _check_pull_source_configured(config, release_id)) is not None:
        return source_error

    is_upgrade, upgrade_error = _resolve_is_upgrade(config, release_id)
    if upgrade_error is not None:
        return upgrade_error

    try:
        deploy_config = load_deployment_config(config_dir)
    except Exception as exc:
        return _fail_outcome(release_id, "config", f"could not load deploy config: {exc}", 22)

    mods_dir = _resolve_mods_dir(deploy_config)
    if mods_dir is None:
        return _fail_outcome(release_id, "config", "could not resolve mods_dir from compose", 22)

    first_inst = next(iter(deploy_config.instances.values()), None)
    if first_inst is None:
        return _fail_outcome(release_id, "config", "no instances configured", 22)

    glue = _build_runtime_glue(config, runtime, client, deploy_config, release_id)
    if isinstance(glue, DeployOutcome):
        return glue
    control, probe, containers = glue

    try:
        protect_patterns = tuple(load_protect_patterns(deploy_config.protect_file))
    except Exception as exc:
        return _fail_outcome(release_id, "config", f"could not load protect patterns: {exc}", 22)

    active = _read_active_release(config.state_dir)
    prior_bundle_dir = None
    if active:
        candidate = config.release_store / active
        if candidate.is_dir():
            prior_bundle_dir = candidate

    request = DeployRequest(
        run_id=release_id,
        release_id=release_id,
        pull=_build_pull_request(config, release_id),
        target_mods_dir=mods_dir,
        target_world_dir=_resolve_world_dir(first_inst),
        world_backup_root=config.world_backup_root,
        prior_bundle_dir=prior_bundle_dir,
        containers=containers,
        protect_patterns=protect_patterns,
        lock=lock,
        health_specs=_build_health_specs(containers),
        is_upgrade=is_upgrade,
    )

    return deploy(
        request=request,
        journal=journal,
        service_control=control,
        health_probe=probe,
        clock=_SYSTEM_CLOCK,
    )


def build_deploy_callable(
    *,
    config: MinecraftDaemonConfig,
    config_dir: Path,
    runtime: DockerRuntime,
) -> Callable[[str], DeployOutcome]:
    """Return a callable that deploys a verified release by ID.

    The callable loads ``deploy_pack.toml`` on every invocation so config
    edits take effect without a daemon restart. Journal and lock are
    shared across invocations -- the lock is a process-wide singleton
    and the journal must accumulate across runs. The Docker SDK client
    is captured once so every deployment uses the same connection.
    """
    client = runtime._client
    journal = DeploymentJournal(config.journal_path)
    lock = DeploymentLock(config.lock_path)

    def deploy_release(release_id: str) -> DeployOutcome:
        return _deploy_one(config, config_dir, runtime, client, journal, lock, release_id)

    return deploy_release
