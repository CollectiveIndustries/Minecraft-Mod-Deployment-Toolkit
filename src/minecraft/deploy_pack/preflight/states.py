# src/minecraft/deploy_pack/preflight/states.py

"""Container-state classification and RCON availability checks (§4.12, §8.4)."""

from __future__ import annotations

from minecraft.deploy_pack.config_model import DeploymentConfig
from minecraft.deploy_pack.docker_runtime import ContainerState, DockerRuntime, select_rcon_transport
from minecraft.deploy_pack.errors import ConfigError

from .types import PreflightFailure


def classify_state(state: ContainerState, container: str) -> str | None:
    """Return a failure message if this state is fatal (§4.12), else None."""
    if not state.exists:
        return f"container {container!r} is missing (§8.9)"
    status = state.status
    if status == "running":
        if state.health is None:
            return f"container {container!r}: running without .State.Health; the healthcheck may not have been created with the container (§3.17, §4.12)"
        return None
    if status in ("exited", "created", "stopped"):
        return None
    if status in ("paused", "removing", "dead"):
        return f"container {container!r} is in state {status!r} (§4.12)"
    if status == "restarting":
        return f"container {container!r} is still restarting after the bounded wait (§4.12)"
    return f"container {container!r} has unexpected status {status!r}"


def check_rcon_available(
    config: DeploymentConfig,
    runtime: DockerRuntime,
    restart_set: list[str],
    container_states: dict[str, ContainerState],
) -> list[PreflightFailure]:
    """§8.4: any running restart_set member must have a selectable RCON transport."""
    compose = config.compose.file if config.compose.ok else None
    if compose is None:
        return []
    out: list[PreflightFailure] = []
    for member in restart_set:
        state = container_states.get(member)
        if state is None or not state.is_running:
            continue
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            continue
        try:
            select_rcon_transport(runtime, inst.service, compose, inst.container, config.docker.rcon_host)
        except ConfigError as exc:
            out.append(
                PreflightFailure(
                    f"§8.4 [{member}]",
                    f"RCON required for restart notice but unavailable: {exc}",
                )
            )
    return out
