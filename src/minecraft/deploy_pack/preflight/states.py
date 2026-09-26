# src/minecraft/deploy_pack/preflight/states.py

"""Container-state classification and RCON availability checks (§4.12, §8.4).

Logging
-------

Module logger is ``minecraft.deploy_pack.preflight.states``.
``classify_state`` is a pure predicate called once per partition member
by ``runner._classify_states``; it emits no events, because the caller
already logs the non-fatal branches and the fatal branches are rendered
as one aggregated ``PreflightError`` message. ``check_rcon_available``
is a once-per-run reporting function: it logs at DEBUG on entry with
the skip counts, at DEBUG per member for both the "not running, skipped"
and "checked OK" branches, and at DEBUG per failing member with the
message text (the specific reason was already logged at ERROR by
``select_rcon_transport``). The aggregate outcome logs at INFO when the
returned failure list is empty and at WARN when it is not, naming the
count and the affected members so the operator does not have to count
the ERROR lines themselves.
"""

from __future__ import annotations

from typing import Any

from minecraft.deploy_pack.config_model import DeploymentConfig
from minecraft.deploy_pack.docker_runtime import ContainerState, DockerRuntime, select_rcon_transport
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.logging_setup import get_logger

from .types import PreflightFailure

_log = get_logger(__name__)


def classify_state(state: ContainerState, container: str) -> str | None:
    """Return a failure message if this state is fatal (§4.12), else None.

    No logging: called once per partition member by
    ``runner._classify_states``, which logs the non-fatal branches at
    WARN/INFO and aggregates the fatal ones into a single
    ``PreflightError``. Adding per-call logging here would double-report
    every fatal classification.
    """
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
    logger: Any = None,
) -> list[PreflightFailure]:
    """§8.4: any running restart_set member must have a selectable RCON transport.

    The per-member failure reason is logged at ERROR by
    :func:`select_rcon_transport`; this function logs at DEBUG per
    member and emits a single INFO or WARN summary so the operator sees
    the aggregate count without counting ERROR lines.
    """
    if logger is None:
        logger = _log
    logger.debug(f"check_rcon_available: restart_set={restart_set} container_states={list(container_states.keys())}")
    compose = config.compose.file if config.compose.ok else None
    if compose is None:
        logger.debug("check_rcon_available: compose not loaded; skipping (per §8.4 scope)")
        return []
    out: list[PreflightFailure] = []
    skipped_not_running = 0
    skipped_no_service = 0
    checked = 0
    for member in restart_set:
        state = container_states.get(member)
        if state is None or not state.is_running:
            skipped_not_running += 1
            logger.debug(f"check_rcon_available: [{member}] not running at preflight; RCON not required")
            continue
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            skipped_no_service += 1
            logger.debug(f"check_rcon_available: [{member}] no instance or service; cannot check RCON")
            continue
        try:
            transport = select_rcon_transport(runtime, inst.service, compose, inst.container, config.docker.rcon_host, logger)
        except ConfigError as exc:
            logger.debug(f"check_rcon_available: [{member}] transport selection failed: {exc}")
            out.append(
                PreflightFailure(
                    f"§8.4 [{member}]",
                    f"RCON required for restart notice but unavailable: {exc}",
                )
            )
            continue
        checked += 1
        logger.debug(f"check_rcon_available: [{member}] -> {transport.describe()}")
    if out:
        affected = [f.source for f in out]
        logger.warning(f"check_rcon_available: {len(out)} of {checked + len(out)} running restart_set member(s) lack RCON: {affected}")
    else:
        logger.info(
            f"check_rcon_available: {checked} running restart_set member(s) have RCON "
            f"(skipped: {skipped_not_running} not running, {skipped_no_service} no service)"
        )
    return out
