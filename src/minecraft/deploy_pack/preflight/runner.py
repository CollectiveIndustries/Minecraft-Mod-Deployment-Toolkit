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
bail on §3.5/§2.5/§3.19, the post-per-member aggregation, the
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


def _has_fatal(failures: list[PreflightFailure], *sources: str) -> bool:
    """Return True if any failure came from one of the given sources.

    No logging: pure predicate, called twice per run.
    """
    wanted = set(sources)
    return any(f.source in wanted for f in failures)


def _inspect_all(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    failures: list[PreflightFailure],
    logger: Any = None,
) -> tuple[dict[str, ContainerState], list[str]]:
    """Inspect every partition member's container. Return (states, restarting)."""
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
            failures.append(PreflightFailure(f"§4.12 [{member}]", f"inspect failed: {exc}"))
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
    """Bounded wait on restarting containers; update ``states`` in place (§4.12)."""
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
    """Apply §4.12's per-state policy; append failures, log warnings."""
    if logger is None:
        logger = _log
    for member, state in states.items():
        inst = config.instances.get(member)
        if inst is None:
            continue
        msg = classify_state(state, inst.container)
        if msg is not None:
            logger.warning(f"_classify_states: [{member}] {msg}")
            failures.append(PreflightFailure(f"§4.12 [{member}]", msg))
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
    """Verify compose-vs-container mount agreement for every running member (§3.17)."""
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
            failures.append(PreflightFailure(f"§3.17 [{member}]", str(exc)))


def _check_resource_pack_sources(
    config: DeploymentConfig,
    failures: list[PreflightFailure],
    logger: Any = None,
) -> None:
    """Validate resource-pack source files and filenames for the partition (§7.7, §7.8)."""
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
                    "§7.7",
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
            failures.append(PreflightFailure(f"§7.5 [{member}]", str(exc)))
            continue
        source = config.sync_root / client_sub / rp.filename
        if not source.is_file():
            logger.warning(f"_check_resource_pack_sources: [{member}] source not found: {source}")
            failures.append(PreflightFailure(f"§7.8 [{member}]", f"resource pack source not found: {source}"))
        else:
            logger.debug(f"_check_resource_pack_sources: [{member}] {rp.filename} validated at {source}")


def _build_member_plans(
    config: DeploymentConfig,
    scopes: ScopeSet,
    targeted: bool,
    instance_changes: dict,
    mods_change,
    rp_changes: dict,
    logger: Any = None,
) -> dict[str, MemberPlan]:
    """Compute per-member effective action and reasons (§4.6)."""
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

    ``logger`` is optional; when omitted, the module logger
    ``minecraft.deploy_pack.preflight.runner`` is used.
    """
    if logger is None:
        logger = _log

    logger.info(f"preflight: scopes={scopes.names()} with_resources={with_resources} notify={notify} dry_run={dry_run} partition={list(config.partition)}")

    failures: list[PreflightFailure] = []
    warnings: list[str] = []

    # --- Top-level checks ------------------------------------------------
    logger.debug("preflight: top-level checks")
    if config.partition_unknown:
        logger.warning(f"preflight: unknown --instance name(s): {config.partition_unknown}")
        failures.append(PreflightFailure("§2.5", "unknown --instance name(s): " + ", ".join(config.partition_unknown)))
    needs_instances = scopes.server or scopes.resource_pack
    if needs_instances and not config.instances:
        logger.warning("preflight: no instances configured")
        failures.append(PreflightFailure("§2.5", "no instances configured"))

    compose_needed = scopes.server
    if scopes.resource_pack and any(m in config.resource_packs for m in config.partition):
        compose_needed = True
    if compose_needed and not config.compose.ok:
        logger.warning(f"preflight: compose required for this scope but could not be loaded: {config.compose.error}")
        failures.append(
            PreflightFailure(
                "§3.5",
                f"compose is required for this scope but could not be loaded: {config.compose.error}",
            )
        )
    if (scopes.client or scopes.resource_pack) and config.www_dir is None:
        for candidate in getattr(config, "www_dir_candidates", None) or []:
            logger.warning(f"www_dir candidate: {candidate}")
        logger.warning(f"preflight: www_dir could not be determined: {config.www_dir_error or 'unknown reason'}")
        failures.append(
            PreflightFailure(
                "§3.19",
                f"www_dir could not be determined: {config.www_dir_error or 'unknown reason'}",
            )
        )

    if _has_fatal(failures, "§3.5", "§2.5", "§3.19"):
        logger.error(f"preflight: aborting early with {len(failures)} top-level failure(s): {[f.source for f in failures]}")
        raise PreflightError(failures)

    # --- Per-member compose / lifecycle checks ---------------------------
    logger.debug("preflight: per-member compose / lifecycle checks")
    container_states: dict[str, ContainerState] = {}
    restarting: list[str] = []
    needs_lifecycle = scopes.server or (scopes.resource_pack and any(m in config.resource_packs for m in config.partition))

    if needs_lifecycle and config.compose.ok:
        mods_dir_candidates: list[tuple[str, Path]] = []
        for member in config.partition:
            inst = config.instances.get(member)
            if inst is None:
                continue
            if inst.service_match_error is not None:
                logger.warning(f"preflight: [{member}] compose match failed: {inst.service_match_error}")
                failures.append(PreflightFailure(f"§3.6 [{member}]", inst.service_match_error))
                continue
            if inst.stop_grace_parse_error is not None:
                logger.warning(f"preflight: [{member}] stop_grace_period parse failed: {inst.stop_grace_parse_error}")
                failures.append(
                    PreflightFailure(
                        f"§3.2 [{member}]",
                        f"stop_grace_period parse failed: {inst.stop_grace_parse_error}",
                    )
                )
            if inst.service is None or inst.instance_root is None:
                logger.warning(f"preflight: [{member}] no /data bind found for container {inst.container!r}")
                failures.append(
                    PreflightFailure(
                        f"§3.6 [{member}]",
                        f"no /data bind found for container {inst.container!r}",
                    )
                )
                continue
            if not inst.service.has_healthcheck:
                logger.warning(f"preflight: [{member}] compose service {inst.service.name!r} has no healthcheck")
                failures.append(
                    PreflightFailure(
                        f"§3.8 [{member}]",
                        f"compose service {inst.service.name!r} has no healthcheck",
                    )
                )
            if scopes.server:
                m = derive_mods_dir(inst.service)
                if m is not None:
                    mods_dir_candidates.append((member, resolve_compose_path(m, config.compose.file.base_dir)))

        targeted = config.requested_instances is not None
        if scopes.server and not targeted and config.partition:
            if len(mods_dir_candidates) != len(config.partition):
                missing = [m for m in config.partition if m not in {n for n, _ in mods_dir_candidates}]
                logger.warning(f"preflight: partition member(s) missing /data/mods bind: {missing}")
                failures.append(
                    PreflightFailure(
                        "§3.7",
                        "partition member(s) missing /data/mods bind: " + ", ".join(missing),
                    )
                )
            else:
                sources = {p for _n, p in mods_dir_candidates}
                if len(sources) != 1:
                    logger.warning(f"preflight: mods_dir bind sources disagree across partition members: {sorted(str(p) for p in sources)}")
                    failures.append(
                        PreflightFailure(
                            "§3.7",
                            "mods_dir bind sources disagree across partition members: " + ", ".join(str(p) for p in sorted(sources)),
                        )
                    )
                elif config.mods_dir_toml is not None and config.mods_dir_toml != next(iter(sources)):
                    logger.warning(f"mods_dir: TOML={config.mods_dir_toml} compose={next(iter(sources))} (compose wins)")

        if needs_lifecycle:
            runtime.ping()
            container_states, restarting = _inspect_all(runtime, config, failures, logger)
        if restarting:
            _settle_restarting(runtime, config, container_states, restarting, logger)
        _classify_states(config, container_states, failures, logger)
        _check_drift(runtime, config, container_states, failures, logger)

    # --- Resource-pack source validation (§7.7 / §7.8) -------------------
    if scopes.resource_pack:
        logger.debug("preflight: resource-pack source validation")
        _check_resource_pack_sources(config, failures, logger)

    # --- Reachable Discord template validation ---------------------------
    if notify:
        logger.debug("preflight: discord template validation")
        template_failures = notifications.validate_live_and_failure(
            config.discord,
            notify=notify,
            dry_run=dry_run,
            has_scope=scopes.any(),
            logger=logger,
        )
        failures.extend(PreflightFailure(src, msg) for src, msg in template_failures)

    if failures:
        logger.error(f"preflight: {len(failures)} failure(s) after per-member checks: {[f.source for f in failures]}")
        raise PreflightError(failures)

    # --- Plan assembly ---------------------------------------------------
    logger.debug("preflight: plan assembly")
    targeted = config.requested_instances is not None
    mods_change = None
    mods_dir = None
    if scopes.server:
        if config.compose.ok and config.partition:
            for member in config.partition:
                inst = config.instances.get(member)
                if inst is not None and inst.service is not None:
                    m = derive_mods_dir(inst.service)
                    if m is not None:
                        mods_dir = resolve_compose_path(m, config.compose.file.base_dir)
                        break
        mods_change = compute_mods_change(config, mods_dir)

    instance_changes = {m: compute_instance_server_change(config, m) for m in config.partition} if scopes.server else {}
    rp_changes = {m: compute_resource_pack_change(config, m) for m in config.partition if m in config.resource_packs} if scopes.resource_pack else {}

    mods_drift = False
    if scopes.server and targeted and mods_change is not None and mods_change.any:
        mods_drift = True
        warnings.append("mods_dir differs from the full source set; a targeted deploy does not touch shared mods. Run a non-targeted --server to update mods.")
        logger.warning("preflight: mods_drift detected on targeted deploy")

    member_plans = _build_member_plans(config, scopes, targeted, instance_changes, mods_change, rp_changes, logger)
    none_set, reload_set, restart_set = _partition(member_plans, config.partition, logger)

    if needs_lifecycle and restart_set and not dry_run:
        logger.debug(f"preflight: RCON availability check for restart_set={restart_set}")
        rcon_failures = check_rcon_available(config, runtime, restart_set, container_states, logger)
        if rcon_failures:
            logger.error(f"preflight: RCON unavailable for {len(rcon_failures)} member(s): {[f.source for f in rcon_failures]}")
            raise PreflightError(rcon_failures)

    pack_required = any(p.pack_required for p in member_plans.values())
    pack_required_warning = None
    if pack_required and not scopes.client:
        pack_required_warning = "client pack content changed; the current ZIP is stale. Run --client to rebuild."
        warnings.append(pack_required_warning)
        logger.debug(f"preflight: pack_required_warning set ({pack_required_warning!r})")

    if notify and not dry_run and restart_set:
        any_running_restart = any(container_states.get(m) is not None and container_states[m].is_running for m in restart_set)
        if any_running_restart:
            logger.debug("preflight: validating online template (§5.11 second pass)")
            online_failures = notifications.validate_online(config.discord, logger)
            if online_failures:
                logger.error(f"preflight: online template validation failed with {len(online_failures)} failure(s)")
                raise PreflightError([PreflightFailure(src, msg) for src, msg in online_failures])

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
