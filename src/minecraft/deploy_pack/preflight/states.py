# src/minecraft/deploy_pack/preflight/states.py

"""Container-state classification and RCON availability checks (Section 4.12, Section 8.4).

Structure
---------

    Section 1  Imports
    Section 2  State classification        (Section 4.12)
    Section 3  RCON availability check     (Section 8.4)

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

from dataclasses import dataclass
from typing import Any

from minecraft.deploy_pack.config_model import DeploymentConfig
from minecraft.deploy_pack.docker_runtime import ContainerState, DockerRuntime, select_rcon_transport
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.logging_setup import get_logger

from .types import PreflightFailure

_log = get_logger(__name__)


# ===========================================================================
# Section 2  State classification (Section 4.12)
# ===========================================================================


def classify_state(state: ContainerState, container: str) -> str | None:
    """Return a failure message if this state is fatal (Section 4.12), else None.

    No logging: called once per partition member by
    ``runner._classify_states``, which logs the non-fatal branches at
    WARN/INFO and aggregates the fatal ones into a single
    ``PreflightError``. Adding per-call logging here would double-report
    every fatal classification.
    """
    if not state.exists:
        return f"container {container!r} is missing (Section 8.9)"
    status = state.status
    if status == "running":
        if state.health is None:
            return f"container {container!r}: running without .State.Health; the healthcheck may not have been created with the container (Section 3.17, Section 4.12)"
        return None
    if status in ("exited", "created", "stopped"):
        return None
    if status in ("paused", "removing", "dead"):
        return f"container {container!r} is in state {status!r} (Section 4.12)"
    if status == "restarting":
        return f"container {container!r} is still restarting after the bounded wait (Section 4.12)"
    return f"container {container!r} has unexpected status {status!r}"


# ===========================================================================
# Section 3  RCON availability check (Section 8.4)
# ===========================================================================


@dataclass
class _RconCheckTally:
    """Running counters for the per-member RCON probe.

    Counts the three outcomes that matter for the closing summary:
    members whose transport was selected (``checked``), members skipped
    because they were not running, and members skipped because they had
    no instance / service to inspect.
    """

    checked: int = 0
    skipped_not_running: int = 0
    skipped_no_service: int = 0


def _is_running_or_none(state: ContainerState | None) -> bool:
    """Return True when the state is present and the container is running.

    No logging: pure predicate used by the per-member loop's first gate.
    """
    return state is not None and state.is_running


def _check_one_member_rcon(
    config: DeploymentConfig,
    runtime: DockerRuntime,
    compose: Any,
    member: str,
    container_states: dict[str, ContainerState],
    tally: _RconCheckTally,
    failures: list[PreflightFailure],
    logger: Any,
) -> None:
    """Probe one restart_set member; record a skip, a success, or a failure.

    Three mutually exclusive outcomes:

      * Not running at preflight -> ``tally.skipped_not_running++``, return.
      * No instance / service    -> ``tally.skipped_no_service++``, return.
      * ``select_rcon_transport`` raises ``ConfigError`` -> append a
        ``PreflightFailure`` to ``failures``, return.
      * Transport selected       -> ``tally.checked++``, return.

    A ``DockerUnavailableError`` from ``select_rcon_transport`` is not
    caught here: preflight lets it propagate per Section 2.4.
    """
    state = container_states.get(member)
    if not _is_running_or_none(state):
        tally.skipped_not_running += 1
        logger.debug(f"check_rcon_available: [{member}] not running at preflight; RCON not required")
        return

    inst = config.instances.get(member)
    if inst is None or inst.service is None:
        tally.skipped_no_service += 1
        logger.debug(f"check_rcon_available: [{member}] no instance or service; cannot check RCON")
        return

    try:
        transport = select_rcon_transport(runtime, inst.service, compose, inst.container, config.docker.rcon_host, logger)
    except ConfigError as exc:
        logger.debug(f"check_rcon_available: [{member}] transport selection failed: {exc}")
        failures.append(
            PreflightFailure(
                f"Section 8.4 [{member}]",
                f"RCON required for restart notice but unavailable: {exc}",
            )
        )
        return

    tally.checked += 1
    logger.debug(f"check_rcon_available: [{member}] -> {transport.describe()}")


def _log_rcon_summary(tally: _RconCheckTally, failures: list[PreflightFailure], logger: Any) -> None:
    """Emit the closing INFO/WARN line for the RCON check.

    WARN when at least one member lacked RCON; INFO otherwise. The checked denominator counts only members that cleared the two skip gates (running
    + has service), so it matches the set of members a transport was actually attempted for.
    """
    if failures:
        affected = [f.source for f in failures]
        logger.warning(f"check_rcon_available: {len(failures)} of {tally.checked + len(failures)} running restart_set member(s) lack RCON: {affected}")
        return
    logger.info(
        f"check_rcon_available: {tally.checked} running restart_set member(s) have RCON "
        f"(skipped: {tally.skipped_not_running} not running, {tally.skipped_no_service} no service)"
    )


def check_rcon_available(
    config: DeploymentConfig,
    runtime: DockerRuntime,
    restart_set: list[str],
    container_states: dict[str, ContainerState],
    logger: Any = None,
) -> list[PreflightFailure]:
    """Section 8.4: any running restart_set member must have a selectable RCON transport.

    The per-member failure reason is logged at ERROR by :func:`select_rcon_transport`; this function logs at DEBUG per member and emits a single
    INFO or WARN summary so the operator sees the aggregate count without counting ERROR lines.
    """
    if logger is None:
        logger = _log
    logger.debug(f"check_rcon_available: restart_set={restart_set} container_states={list(container_states.keys())}")
    compose = config.compose.file if config.compose.ok else None
    if compose is None:
        logger.debug("check_rcon_available: compose not loaded; skipping (per Section 8.4 scope)")
        return []

    tally = _RconCheckTally()
    failures: list[PreflightFailure] = []
    for member in restart_set:
        _check_one_member_rcon(config, runtime, compose, member, container_states, tally, failures, logger)

    _log_rcon_summary(tally, failures, logger)
    return failures
