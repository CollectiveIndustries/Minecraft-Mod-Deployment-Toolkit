# src/minecraft/deploy_pack/preflight/runner.py

"""Preflight orchestration: run every check, aggregate failures, build the plan.

Logging
-------

Module logger is ``minecraft.deploy_pack.preflight.runner``. The
function logs at INFO on entry (scopes, dry_run, notify), at DEBUG
per-section as each check block runs, and at INFO at the end with a
one-line summary of the plan (partition size, none/reload/restart
split, pack_required). Every branch that appends a
``PreflightFailure`` logs at WARN with the specific reason and (where
applicable) the member name; without those, a ``--debug`` run that
fails at the top level would show only the trailing summary and the
operator would have to parse the multi-line ``PreflightError`` text to
find the cause. The four ``raise PreflightError`` sites - the early
bail on Section 3.5/Section 2.5/Section 3.19, the post-per-member aggregation, the
RCON-unavailable bail, and the online-template bail - each log at
ERROR with the failure count and source labels before raising, so the
aggregated diagnostic lands in the sink regardless of how ``main.py``
renders it to stderr.

Per-member helpers (``_inspect_all``, ``_settle_restarting``,
``_classify_states``, ``_check_drift``, ``_check_resource_pack_sources``,
``_build_member_plans``, ``_partition``) accept a trailing
``logger: Any = None`` defaulting to the module logger and are threaded
from :func:`run_preflight`. ``_has_fatal`` is a pure predicate and
emits nothing.

Calls into other modules (``resolve_compose_path``,
``check_mount_drift``, ``notifications.validate_*``,
``check_rcon_available``, ``build_resource_pack_url``,
``validate_resource_pack_filename``) do not thread ``logger``: each
sub-module emits under its own module name, which is more informative
than re-attributing its events here. Only ``check_rcon_available`` and
the ``notifications.validate_*`` helpers accept a caller ``logger``;
those are passed this module's logger where the caller's context
matters. ``resolve_compose_path`` is a two-argument function whose
single DEBUG line uses the ``config_model`` module logger.

Structure
---------

:func:`run_preflight` is a thin orchestrator that walks the Section 4.1
sequence in order. Each check phase is a small function with a single
responsibility, so a failure at any point can be traced to a specific
phase without reading the entire file. The two ``raise`` points
(early bail after top-level checks, late bail after per-member and
template checks) are separate helpers so the ordering contract is
visible at a glance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from minecraft.deploy_pack import notifications
from minecraft.deploy_pack.config_model import DeploymentConfig, derive_mods_dir, resolve_compose_path
from minecraft.deploy_pack.docker_runtime import ContainerState, DockerRuntime, check_mount_drift
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.files import (
    build_resource_pack_url,
    validate_resource_pack_filename,
)
from minecraft.deploy_pack.logging_setup import get_logger

from .actions import is_pack_action, resolve_paths_action, sticky_max
from .changes import (
    compute_instance_server_change,
    compute_mods_change,
    compute_resource_pack_change,
)
from .states import check_rcon_available, classify_state
from .types import (
    MemberPlan,
    PreflightError,
    PreflightFailure,
    PreflightPlan,
    ReasonEntry,
    ScopeSet,
)

_log = get_logger(__name__)

_EARLY_BAIL_SOURCES = ("Section 3.5", "Section 2.5", "Section 3.19")
_MODS_DRIFT_WARNING = "mods_dir differs from the full source set; a targeted deploy does not touch shared mods. Run a non-targeted --server to update mods."
_PACK_REQUIRED_WARNING = "client pack content changed; the current ZIP is stale. Run --client to rebuild."


# ---------------------------------------------------------------------------
# Entry logging and pure predicates
# ---------------------------------------------------------------------------


def _log_preflight_entry(
    config: DeploymentConfig,
    scopes: ScopeSet,
    with_resources: bool,
    notify: bool,
    dry_run: bool,
    logger: Any,
) -> None:
    """Emit the single INFO line that opens every preflight run."""
    logger.info(f"preflight: scopes={scopes.names()} with_resources={with_resources} notify={notify} dry_run={dry_run} partition={list(config.partition)}")


def _has_fatal(failures: list[PreflightFailure], *sources: str) -> bool:
    """Return True if any failure came from one of the given sources.

    No logging: pure predicate, called twice per run.
    """
    wanted = set(sources)
    return any(f.source in wanted for f in failures)


def _needs_lifecycle(scopes: ScopeSet, config: DeploymentConfig) -> bool:
    """Return True when the scope requires container inspection (Section 4.12).

    Server always does. Resource-pack does only when at least one partition member has a ``[resource_pack.X]`` section (Section 7.7).
    """
    if scopes.server:
        return True
    return scopes.resource_pack and any(m in config.resource_packs for m in config.partition)


def _compose_needed_for_scopes(scopes: ScopeSet, config: DeploymentConfig) -> bool:
    """Return True when a broken compose is fatal for this invocation (Section 3.5)."""
    if scopes.server:
        return True
    return scopes.resource_pack and any(m in config.resource_packs for m in config.partition)


# ---------------------------------------------------------------------------
# Top-level checks
# ---------------------------------------------------------------------------


def _check_unknown_instances(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    logger: Any,
) -> None:
    """Section 2.5: requested --instance names that are not configured."""
    if not config.partition_unknown:
        return
    logger.warning(f"preflight: unknown --instance name(s): {config.partition_unknown}")
    failures.append(PreflightFailure("Section 2.5", "unknown --instance name(s): " + ", ".join(config.partition_unknown)))


def _check_instances_configured(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> None:
    """Section 2.5: server and resource-pack scopes require at least one instance."""
    if not (scopes.server or scopes.resource_pack):
        return
    if config.instances:
        return
    logger.warning("preflight: no instances configured")
    failures.append(PreflightFailure("Section 2.5", "no instances configured"))


def _check_compose_available(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> None:
    """Section 3.5: compose must load when a scope requires it."""
    if not _compose_needed_for_scopes(scopes, config):
        return
    if config.compose.ok:
        return
    logger.warning(f"preflight: compose required for this scope but could not be loaded: {config.compose.error}")
    failures.append(
        PreflightFailure(
            "Section 3.5",
            f"compose is required for this scope but could not be loaded: {config.compose.error}",
        )
    )


def _check_www_dir_available(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> None:
    """Section 3.19: client and resource-pack scopes require a resolvable www_dir."""
    if not (scopes.client or scopes.resource_pack):
        return
    if config.www_dir is not None:
        return
    for candidate in getattr(config, "www_dir_candidates", None) or []:
        logger.warning(f"www_dir candidate: {candidate}")
    logger.warning(f"preflight: www_dir could not be determined: {config.www_dir_error or 'unknown reason'}")
    failures.append(
        PreflightFailure(
            "Section 3.19",
            f"www_dir could not be determined: {config.www_dir_error or 'unknown reason'}",
        )
    )


def _run_top_level_checks(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> None:
    """Run the four checks that gate the rest of preflight."""
    logger.debug("preflight: top-level checks")
    _check_unknown_instances(failures, config, logger)
    _check_instances_configured(failures, config, scopes, logger)
    _check_compose_available(failures, config, scopes, logger)
    _check_www_dir_available(failures, config, scopes, logger)


def _raise_if_early_bail(failures: list[PreflightFailure], logger: Any) -> None:
    """Raise when a Section 3.5/Section 2.5/Section 3.19 failure makes further checks meaningless."""
    if not _has_fatal(failures, *_EARLY_BAIL_SOURCES):
        return
    logger.error(f"preflight: aborting early with {len(failures)} top-level failure(s): {[f.source for f in failures]}")
    raise PreflightError(failures)


def _raise_if_failures(failures: list[PreflightFailure], logger: Any) -> None:
    """Raise after all independently detectable failures have been collected (Section 4.3)."""
    if not failures:
        return
    logger.error(f"preflight: {len(failures)} failure(s) after per-member checks: {[f.source for f in failures]}")
    raise PreflightError(failures)


# ---------------------------------------------------------------------------
# Per-member compose checks
# ---------------------------------------------------------------------------


def _check_one_member_compose(
    config: DeploymentConfig,
    member: str,
    scopes: ScopeSet,
    logger: Any,
) -> tuple[list[PreflightFailure], tuple[str, Path] | None]:
    """Section 3.2/Section 3.6/Section 3.8 checks for a single partition member.

    Returns ``(failures, mods_dir_candidate)``. The candidate is ``(member, resolved_mods_dir)`` when the server scope is active and the member's
    service declares a ``/data/mods`` bind; ``None`` otherwise.
    """
    inst = config.instances.get(member)
    if inst is None:
        return [], None

    failures: list[PreflightFailure] = []

    if inst.service_match_error is not None:
        logger.warning(f"preflight: [{member}] compose match failed: {inst.service_match_error}")
        failures.append(PreflightFailure(f"Section 3.6 [{member}]", inst.service_match_error))
        return failures, None

    if inst.stop_grace_parse_error is not None:
        logger.warning(f"preflight: [{member}] stop_grace_period parse failed: {inst.stop_grace_parse_error}")
        failures.append(
            PreflightFailure(
                f"Section 3.2 [{member}]",
                f"stop_grace_period parse failed: {inst.stop_grace_parse_error}",
            )
        )

    if inst.service is None or inst.instance_root is None:
        logger.warning(f"preflight: [{member}] no /data bind found for container {inst.container!r}")
        failures.append(
            PreflightFailure(
                f"Section 3.6 [{member}]",
                f"no /data bind found for container {inst.container!r}",
            )
        )
        return failures, None

    if not inst.service.has_healthcheck:
        logger.warning(f"preflight: [{member}] compose service {inst.service.name!r} has no healthcheck")
        failures.append(
            PreflightFailure(
                f"Section 3.8 [{member}]",
                f"compose service {inst.service.name!r} has no healthcheck",
            )
        )

    candidate: tuple[str, Path] | None = None
    if scopes.server:
        m = derive_mods_dir(inst.service)
        if m is not None:
            candidate = (member, resolve_compose_path(m, config.compose.file.base_dir))

    return failures, candidate


def _check_all_member_compose(
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> tuple[list[PreflightFailure], list[tuple[str, Path]]]:
    """Run :func:`_check_one_member_compose` for every partition member."""
    failures: list[PreflightFailure] = []
    candidates: list[tuple[str, Path]] = []
    for member in config.partition:
        member_failures, candidate = _check_one_member_compose(config, member, scopes, logger)
        failures.extend(member_failures)
        if candidate is not None:
            candidates.append(candidate)
    return failures, candidates


def _should_check_mods_dir_agreement(config: DeploymentConfig, scopes: ScopeSet) -> bool:
    """Section 3.7 gate: server scope, not a targeted deploy, and a non-empty partition."""
    return scopes.server and config.requested_instances is None and bool(config.partition)


def _find_missing_mods_dir_members(
    config: DeploymentConfig,
    mods_dir_candidates: list[tuple[str, Path]],
) -> list[str]:
    """Return the partition members that did not declare a /data/mods bind."""
    have = {name for name, _ in mods_dir_candidates}
    return [m for m in config.partition if m not in have]


def _find_disagreeing_mods_dir_sources(
    mods_dir_candidates: list[tuple[str, Path]],
) -> set[Path] | None:
    """Return the distinct host sources when >1 is present, else None.

    None means "all candidates agree on the same source" (the healthy case); a returned set means the partition disagrees and preflight should fail
    with the sorted source list.
    """
    sources = {path for _name, path in mods_dir_candidates}
    if len(sources) <= 1:
        return None
    return sources


def _warn_mods_dir_toml_mismatch(
    config: DeploymentConfig,
    mods_dir_candidates: list[tuple[str, Path]],
    logger: Any,
) -> None:
    """Section 3.4: log when TOML disagrees with the (authoritative) compose source."""
    if config.mods_dir_toml is None:
        return
    compose_source = next(iter({path for _name, path in mods_dir_candidates}))
    if config.mods_dir_toml != compose_source:
        logger.warning(f"mods_dir: TOML={config.mods_dir_toml} compose={compose_source} (compose wins)")


def _check_mods_dir_agreement(
    config: DeploymentConfig,
    scopes: ScopeSet,
    mods_dir_candidates: list[tuple[str, Path]],
    logger: Any,
) -> list[PreflightFailure]:
    """Section 3.7: every non-targeted partition member must share one mods_dir bind.

    Three checks in order, first failure wins:

      1. Every partition member must declare a /data/mods bind.
      2. All members' binds must resolve to the same host source.
      3. (Informational only) A TOML-vs-compose disagreement is warned.

    Skipped entirely for a targeted deploy (Section 2.9: --server --instance X
    does not touch mods_dir).
    """
    if not _should_check_mods_dir_agreement(config, scopes):
        return []

    missing = _find_missing_mods_dir_members(config, mods_dir_candidates)
    if missing:
        logger.warning(f"preflight: partition member(s) missing /data/mods bind: {missing}")
        return [
            PreflightFailure(
                "Section 3.7",
                "partition member(s) missing /data/mods bind: " + ", ".join(missing),
            )
        ]

    disagreeing = _find_disagreeing_mods_dir_sources(mods_dir_candidates)
    if disagreeing is not None:
        sorted_sources = sorted(disagreeing)
        logger.warning(f"preflight: mods_dir bind sources disagree across partition members: {sorted(str(p) for p in sorted_sources)}")
        return [
            PreflightFailure(
                "Section 3.7",
                "mods_dir bind sources disagree across partition members: " + ", ".join(str(p) for p in sorted_sources),
            )
        ]

    _warn_mods_dir_toml_mismatch(config, mods_dir_candidates, logger)
    return []


# ---------------------------------------------------------------------------
# Lifecycle checks (inspect / settle / classify / drift)
# ---------------------------------------------------------------------------


def _inspect_all(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    failures: list[PreflightFailure],
    logger: Any = None,
) -> tuple[dict[str, ContainerState], list[str]]:
    """Inspect every partition member's container.

    Return (states, restarting).
    """
    if logger is None:
        logger = _log
    logger.debug(f"_inspect_all: inspecting {len(config.partition)} member(s)")
    states: dict[str, ContainerState] = {}
    restarting: list[str] = []
    for member in config.partition:
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            logger.debug(f"_inspect_all: [{member}] no instance or service; skipped")
            continue
        try:
            state = runtime.inspect(inst.container)
        except Exception as exc:
            logger.warning(f"_inspect_all: [{member}] inspect of {inst.container!r} failed: {exc}")
            failures.append(PreflightFailure(f"Section 4.12 [{member}]", f"inspect failed: {exc}"))
            continue
        states[member] = state
        logger.debug(f"_inspect_all: [{member}] {inst.container} status={state.status!r} running={state.running} health={state.health!r}")
        if state.status == "restarting":
            restarting.append(member)
    if restarting:
        logger.debug(f"_inspect_all: restarting members = {restarting}")
    return states, restarting


def _settle_restarting(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    states: dict[str, ContainerState],
    restarting: list[str],
    logger: Any = None,
) -> None:
    """Bounded wait on restarting containers; update ``states`` in place (Section 4.12)."""
    if logger is None:
        logger = _log
    wait = config.docker.preflight_restarting_wait_seconds
    logger.debug(f"_settle_restarting: waiting up to {wait}s for {restarting}")
    settled = runtime.wait_for_restarting_settle(
        [config.instances[m].container for m in restarting],
        total_timeout=wait,
        poll_interval=config.docker.health_poll_seconds,
    )
    for member in restarting:
        inst = config.instances.get(member)
        if inst is None:
            continue
        new_state = settled.get(inst.container)
        if new_state is not None:
            states[member] = new_state
            logger.debug(f"_settle_restarting: [{member}] settled status={new_state.status!r}")


def _classify_states(
    config: DeploymentConfig,
    states: dict[str, ContainerState],
    failures: list[PreflightFailure],
    logger: Any = None,
) -> None:
    """Apply Section 4.12's per-state policy; append failures, log warnings."""
    if logger is None:
        logger = _log
    for member, state in states.items():
        inst = config.instances.get(member)
        if inst is None:
            continue
        msg = classify_state(state, inst.container)
        if msg is not None:
            logger.warning(f"_classify_states: [{member}] {msg}")
            failures.append(PreflightFailure(f"Section 4.12 [{member}]", msg))
        elif state.running and state.health == "unhealthy":
            logger.warning(f"[{member}] container {inst.container} is unhealthy at preflight")
        elif state.running and state.health == "starting":
            logger.info(f"[{member}] container {inst.container} is starting")


def _check_drift(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    states: dict[str, ContainerState],
    failures: list[PreflightFailure],
    logger: Any = None,
) -> None:
    """Verify compose-vs-container mount agreement for every running member (Section 3.17)."""
    if logger is None:
        logger = _log
    compose_file = config.compose.file
    if compose_file is None:
        logger.debug("_check_drift: compose not loaded; skipping")
        return
    logger.debug(f"_check_drift: checking {len(states)} member(s)")
    for member in states:
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            continue
        expected = [(resolve_compose_path(b.host_source, compose_file.base_dir), b.container_target) for b in inst.service.binds]
        try:
            check_mount_drift(runtime, inst.container, expected, logger)
        except ConfigError as exc:
            logger.warning(f"_check_drift: [{member}] drift detected on {inst.container!r}: {exc}")
            failures.append(PreflightFailure(f"Section 3.17 [{member}]", str(exc)))


def _run_member_checks(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    needs_lifecycle: bool,
    runtime: DockerRuntime,
    logger: Any,
) -> dict[str, ContainerState]:
    """Run the per-member compose/lifecycle phase.

    Returns the container-state map. Populates ``failures`` with everything detected. When lifecycle checks are not required, or the compose file
    could not be loaded, returns an empty map without touching Docker.
    """
    if not (needs_lifecycle and config.compose.ok):
        return {}

    logger.debug("preflight: per-member compose / lifecycle checks")

    compose_failures, mods_dir_candidates = _check_all_member_compose(config, scopes, logger)
    failures.extend(compose_failures)
    failures.extend(_check_mods_dir_agreement(config, scopes, mods_dir_candidates, logger))

    runtime.ping()
    container_states, restarting = _inspect_all(runtime, config, failures, logger)
    if restarting:
        _settle_restarting(runtime, config, container_states, restarting, logger)
    _classify_states(config, container_states, failures, logger)
    _check_drift(runtime, config, container_states, failures, logger)

    return container_states


# ---------------------------------------------------------------------------
# Resource-pack source validation (Section 7.7 / Section 7.8)
# ---------------------------------------------------------------------------


def _check_resource_pack_sources(
    config: DeploymentConfig,
    failures: list[PreflightFailure],
    logger: Any = None,
) -> None:
    """Validate resource-pack source files and filenames for the partition (Section 7.7, Section 7.8)."""
    if logger is None:
        logger = _log
    logger.debug("_check_resource_pack_sources: validating RP sources for partition")
    resourcepacks = config.sync_mapping.get("resourcepacks") or {}
    client_sub = resourcepacks.get("client") if isinstance(resourcepacks, dict) else None
    if not isinstance(client_sub, str) or not client_sub:
        if any(m in config.resource_packs for m in config.partition):
            logger.warning("_check_resource_pack_sources: [sync_mapping].resourcepacks.client is required when at least one pack is configured")
            failures.append(
                PreflightFailure(
                    "Section 7.7",
                    "[sync_mapping].resourcepacks.client is required when at least one pack is configured",
                )
            )
        else:
            logger.debug("_check_resource_pack_sources: no packs configured for partition; nothing to validate")
        return
    for member in config.partition:
        rp = config.resource_packs.get(member)
        if rp is None:
            logger.debug(f"_check_resource_pack_sources: [{member}] no [resource_pack.{member}]; skipped")
            continue
        try:
            validate_resource_pack_filename(rp.filename)
        except ConfigError as exc:
            logger.warning(f"_check_resource_pack_sources: [{member}] filename invalid: {exc}")
            failures.append(PreflightFailure(f"Section 7.5 [{member}]", str(exc)))
            continue
        source = config.sync_root / client_sub / rp.filename
        if not source.is_file():
            logger.warning(f"_check_resource_pack_sources: [{member}] source not found: {source}")
            failures.append(PreflightFailure(f"Section 7.8 [{member}]", f"resource pack source not found: {source}"))
        else:
            logger.debug(f"_check_resource_pack_sources: [{member}] {rp.filename} validated at {source}")


def _check_resource_pack_sources_if_needed(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    logger: Any,
) -> None:
    """Run Section 7.7/Section 7.8 source validation only when the RP scope is active."""
    if not scopes.resource_pack:
        return
    logger.debug("preflight: resource-pack source validation")
    _check_resource_pack_sources(config, failures, logger)


# ---------------------------------------------------------------------------
# Discord template validation (Section 5.11)
# ---------------------------------------------------------------------------


def _check_template_availability_if_notify(
    failures: list[PreflightFailure],
    config: DeploymentConfig,
    scopes: ScopeSet,
    notify: bool,
    dry_run: bool,
    logger: Any,
) -> None:
    """Section 5.11: validate the reachable Discord templates before any write."""
    if not notify:
        return
    logger.debug("preflight: discord template validation")
    template_failures = notifications.validate_live_and_failure(
        config.discord,
        notify=notify,
        dry_run=dry_run,
        has_scope=scopes.any(),
        logger=logger,
    )
    failures.extend(PreflightFailure(src, msg) for src, msg in template_failures)


# ---------------------------------------------------------------------------
# Plan assembly (Section 4.6)
# ---------------------------------------------------------------------------


def _first_mods_dir(config: DeploymentConfig) -> Path | None:
    """Return the first partition member's mods_dir, or None.

    Compose is authoritative for ``mods_dir`` (Section 3.4); the first member that resolves a ``/data/mods`` bind wins. Consistency across the partition is
    enforced earlier by :func:`_check_mods_dir_agreement`.
    """
    if not (config.compose.ok and config.partition):
        return None
    for member in config.partition:
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            continue
        m = derive_mods_dir(inst.service)
        if m is not None:
            return resolve_compose_path(m, config.compose.file.base_dir)
    return None


def _compute_mods_change_for_scopes(
    config: DeploymentConfig,
    scopes: ScopeSet,
):
    """Compute the mods diff, or (None, None) when the server scope is off."""
    if not scopes.server:
        return (None, None)
    mods_dir = _first_mods_dir(config)
    mods_change = compute_mods_change(config, mods_dir)
    return (mods_change, mods_dir)


def _compute_instance_changes_for_scopes(
    config: DeploymentConfig,
    scopes: ScopeSet,
) -> dict:
    """Compute per-member config/kubejs diffs when the server scope is active."""
    if not scopes.server:
        return {}
    return {m: compute_instance_server_change(config, m) for m in config.partition}


def _compute_rp_changes_for_scopes(
    config: DeploymentConfig,
    scopes: ScopeSet,
) -> dict:
    """Compute per-member resource-pack evaluations when the RP scope is active."""
    if not scopes.resource_pack:
        return {}
    return {m: compute_resource_pack_change(config, m) for m in config.partition if m in config.resource_packs}


def _detect_mods_drift(
    scopes: ScopeSet,
    targeted: bool,
    mods_change,
    logger: Any,
) -> tuple[bool, str | None]:
    """Section 2.9: a targeted server deploy warns when mods_dir differs from source.

    Returns ``(drift_detected, warning_message)``. The warning is None when no drift is detected.
    """
    if not (scopes.server and targeted and mods_change is not None and mods_change.any):
        return (False, None)
    logger.warning("preflight: mods_drift detected on targeted deploy")
    return (True, _MODS_DRIFT_WARNING)


def _build_member_plans(
    config: DeploymentConfig,
    scopes: ScopeSet,
    targeted: bool,
    instance_changes: dict,
    mods_change,
    rp_changes: dict,
    logger: Any = None,
) -> dict[str, MemberPlan]:
    """Compute per-member effective action and reasons (Section 4.6)."""
    if logger is None:
        logger = _log
    policy = config.restart_policy
    plans: dict[str, MemberPlan] = {}
    for member in config.partition:
        inst = config.instances.get(member)
        plan = MemberPlan(member=member, container=inst.container if inst else member)
        if scopes.server:
            paths: list[str] = []
            sc = instance_changes.get(member)
            if sc is not None:
                paths.extend(sc.changed_paths)
            if not targeted and mods_change is not None:
                paths.extend(mods_change.changed_paths())
            plan.changed_paths.extend(paths)
            effective, reasons = resolve_paths_action(paths, policy)
            plan.server_action = effective if paths else "none"
            plan.reasons.extend(reasons)
        rp = rp_changes.get(member)
        if rp is not None:
            plan.resource_pack_action = rp.action
            plan.resource_pack_publish = rp.publish_needed
            if rp.source_sha1 is not None and rp.source_zip is not None:
                plan.resource_pack_source = rp.source_zip
            if config.compose.ok:
                resourcepacks = config.sync_mapping.get("resourcepacks") or {}
                dest_value = resourcepacks.get("resource_pack") if isinstance(resourcepacks, dict) else None
                rpc = config.resource_packs.get(member)
                if isinstance(dest_value, str) and dest_value and rpc is not None and rp.source_sha1 is not None:
                    url = build_resource_pack_url(config.download_base_url, dest_value, rpc.filename)
                    plan.resource_pack_target = {
                        "require-resource-pack": "true" if rpc.required else "false",
                        "resource-pack": url,
                        "resource-pack-prompt": rpc.prompt,
                        "resource-pack-sha1": rp.source_sha1,
                    }
        candidates: list[str] = []
        if plan.server_action is not None:
            candidates.append(plan.server_action)
        if plan.resource_pack_action is not None:
            candidates.append(plan.resource_pack_action)
        plan.effective_action = sticky_max(candidates)
        plan.pack_required = is_pack_action(plan.effective_action)
        if plan.resource_pack_action == plan.effective_action and plan.resource_pack_action != "none" and rp is not None:
            changed = sorted(rp.properties_changes)
            plan.reasons.append(
                ReasonEntry(
                    path_prefix="resource-pack",
                    action=plan.resource_pack_action,
                    changed_paths=changed,
                )
            )
        plans[member] = plan
        logger.debug(
            f"_build_member_plans: [{member}] server={plan.server_action!r} rp={plan.resource_pack_action!r} "
            f"effective={plan.effective_action!r} pack_required={plan.pack_required} "
            f"changed_paths={len(plan.changed_paths)} reasons={len(plan.reasons)}"
        )
    return plans


def _partition(
    plans: dict[str, MemberPlan],
    partition: list[str],
    logger: Any = None,
) -> tuple[list[str], list[str], list[str]]:
    """Partition into (none_set, reload_set, restart_set) by effective action."""
    if logger is None:
        logger = _log
    none_set: list[str] = []
    reload_set: list[str] = []
    restart_set: list[str] = []
    for member in partition:
        action = plans[member].effective_action
        if action in ("none", "none+pack"):
            none_set.append(member)
        elif action in ("reload", "reload+pack"):
            reload_set.append(member)
        else:
            restart_set.append(member)
    logger.debug(f"_partition: none={none_set} reload={reload_set} restart={restart_set}")
    return none_set, reload_set, restart_set


def _check_rcon_if_needed(
    config: DeploymentConfig,
    runtime: DockerRuntime,
    needs_lifecycle: bool,
    restart_set: list[str],
    container_states: dict[str, ContainerState],
    dry_run: bool,
    logger: Any,
) -> None:
    """Section 8.4: every running restart_set member must have a selectable RCON transport.

    Skipped under ``--dry-run`` (Section 2.6) and when no restart will happen. Raises ``PreflightError`` on any failure.
    """
    if not (needs_lifecycle and restart_set and not dry_run):
        return
    logger.debug(f"preflight: RCON availability check for restart_set={restart_set}")
    rcon_failures = check_rcon_available(config, runtime, restart_set, container_states, logger)
    if rcon_failures:
        logger.error(f"preflight: RCON unavailable for {len(rcon_failures)} member(s): {[f.source for f in rcon_failures]}")
        raise PreflightError(rcon_failures)


def _compute_pack_required(member_plans: dict[str, MemberPlan]) -> bool:
    """Return True if any member's effective action ends in ``+pack``."""
    return any(p.pack_required for p in member_plans.values())


def _compute_pack_required_warning(
    pack_required: bool,
    scopes: ScopeSet,
    warnings: list[str],
    logger: Any,
) -> str | None:
    """Section 4.6.9: emit the staleness warning when --client is not in scope.

    Appends to ``warnings`` so the CLI prints it, and returns the text so the live notification can render it. Returns None when no warning applies.
    """
    if not (pack_required and not scopes.client):
        return None
    warnings.append(_PACK_REQUIRED_WARNING)
    logger.debug(f"preflight: pack_required_warning set ({_PACK_REQUIRED_WARNING!r})")
    return _PACK_REQUIRED_WARNING


def _validate_online_template_if_needed(
    config: DeploymentConfig,
    notify: bool,
    dry_run: bool,
    restart_set: list[str],
    container_states: dict[str, ContainerState],
    logger: Any,
) -> None:
    """Section 5.11 second pass: online template, only when a running restart will fire.

    Raises ``PreflightError`` on any template failure.
    """
    if not (notify and not dry_run and restart_set):
        return
    any_running = any(container_states.get(m) is not None and container_states[m].is_running for m in restart_set)
    if not any_running:
        return
    logger.debug("preflight: validating online template (Section 5.11 second pass)")
    online_failures = notifications.validate_online(config.discord, logger)
    if online_failures:
        logger.error(f"preflight: online template validation failed with {len(online_failures)} failure(s)")
        raise PreflightError([PreflightFailure(src, msg) for src, msg in online_failures])


def _run_plan_assembly(
    config: DeploymentConfig,
    scopes: ScopeSet,
    notify: bool,
    dry_run: bool,
    runtime: DockerRuntime,
    needs_lifecycle: bool,
    container_states: dict[str, ContainerState],
    warnings: list[str],
    logger: Any,
) -> PreflightPlan:
    """Assemble the final plan after every check has passed."""
    logger.debug("preflight: plan assembly")
    targeted = config.requested_instances is not None

    mods_change, mods_dir = _compute_mods_change_for_scopes(config, scopes)
    instance_changes = _compute_instance_changes_for_scopes(config, scopes)
    rp_changes = _compute_rp_changes_for_scopes(config, scopes)

    mods_drift, drift_warning = _detect_mods_drift(scopes, targeted, mods_change, logger)
    if drift_warning is not None:
        warnings.append(drift_warning)

    member_plans = _build_member_plans(config, scopes, targeted, instance_changes, mods_change, rp_changes, logger)
    none_set, reload_set, restart_set = _partition(member_plans, config.partition, logger)

    _check_rcon_if_needed(config, runtime, needs_lifecycle, restart_set, container_states, dry_run, logger)

    pack_required = _compute_pack_required(member_plans)
    pack_required_warning = _compute_pack_required_warning(pack_required, scopes, warnings, logger)

    _validate_online_template_if_needed(config, notify, dry_run, restart_set, container_states, logger)

    plan = PreflightPlan(
        scopes=scopes,
        partition=list(config.partition),
        member_plans=member_plans,
        none_set=none_set,
        reload_set=reload_set,
        restart_set=restart_set,
        pack_required=pack_required,
        container_states=container_states,
        warnings=warnings,
        mods_change=mods_change,
        mods_dir=mods_dir,
        targeted=targeted,
        mods_drift=mods_drift,
        pack_required_warning=pack_required_warning,
    )
    logger.info(
        f"preflight: plan built; partition={plan.partition} "
        f"none={len(none_set)} reload={len(reload_set)} restart={len(restart_set)} "
        f"pack_required={pack_required} warnings={len(warnings)}"
    )
    return plan


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_preflight(
    config: DeploymentConfig,
    scopes: ScopeSet,
    with_resources: bool,
    notify: bool,
    dry_run: bool,
    runtime: DockerRuntime,
    logger: Any = None,
) -> PreflightPlan:
    """Run every preflight check, aggregate failures, return the plan.

    Raises PreflightError (a ConfigError, exit 3) if any check fails.

    The sequence mirrors Section 4.1:

      1. Top-level prerequisites (Section 2.5, Section 3.5, Section 3.19). Bail early on
         failure - nothing downstream can be trusted.
      2. Per-member compose checks and lifecycle inspection (Section 3.2,
         Section 3.6, Section 3.7, Section 3.8, Section 4.12, Section 3.17).
      3. Resource-pack source validation (Section 7.7, Section 7.8) and Discord
         template validation (Section 5.11, first pass).
      4. Bail if any failures were collected (Section 4.3).
      5. Plan assembly (Section 4.6): effective actions, partition, RCON
         availability (Section 8.4), pack-required warning (Section 4.6.9), online
         template (Section 5.11, second pass).

    ``logger`` is optional; when omitted, the module logger
    ``minecraft.deploy_pack.preflight.runner`` is used.
    """
    if logger is None:
        logger = _log

    _log_preflight_entry(config, scopes, with_resources, notify, dry_run, logger)

    failures: list[PreflightFailure] = []
    warnings: list[str] = []

    _run_top_level_checks(failures, config, scopes, logger)
    _raise_if_early_bail(failures, logger)

    needs_lifecycle = _needs_lifecycle(scopes, config)
    container_states = _run_member_checks(failures, config, scopes, needs_lifecycle, runtime, logger)

    _check_resource_pack_sources_if_needed(failures, config, scopes, logger)
    _check_template_availability_if_notify(failures, config, scopes, notify, dry_run, logger)

    _raise_if_failures(failures, logger)

    return _run_plan_assembly(
        config,
        scopes,
        notify,
        dry_run,
        runtime,
        needs_lifecycle,
        container_states,
        warnings,
        logger,
    )
