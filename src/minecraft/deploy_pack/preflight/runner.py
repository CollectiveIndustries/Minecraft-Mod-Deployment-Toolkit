# src/minecraft/deploy_pack/preflight/runner.py

"""Preflight orchestration: run every check, aggregate failures, build the plan."""

from __future__ import annotations

from pathlib import Path

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

logger = get_logger(__name__)


def _has_fatal(failures: list[PreflightFailure], *sources: str) -> bool:
    """Return True if any failure came from one of the given sources."""
    wanted = set(sources)
    return any(f.source in wanted for f in failures)


def _inspect_all(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    failures: list[PreflightFailure],
) -> tuple[dict[str, ContainerState], list[str]]:
    """Inspect every partition member's container. Return (states, restarting)."""
    states: dict[str, ContainerState] = {}
    restarting: list[str] = []
    for member in config.partition:
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            continue
        try:
            state = runtime.inspect(inst.container)
        except Exception as exc:
            failures.append(PreflightFailure(f"§4.12 [{member}]", f"inspect failed: {exc}"))
            continue
        states[member] = state
        if state.status == "restarting":
            restarting.append(member)
    return states, restarting


def _settle_restarting(
    runtime: DockerRuntime,
    config: DeploymentConfig,
    states: dict[str, ContainerState],
    restarting: list[str],
) -> None:
    """Bounded wait on restarting containers; update ``states`` in place (§4.12)."""
    wait = config.docker.preflight_restarting_wait_seconds
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


def _classify_states(
    config: DeploymentConfig,
    states: dict[str, ContainerState],
    failures: list[PreflightFailure],
) -> None:
    """Apply §4.12's per-state policy; append failures, log warnings."""
    for member, state in states.items():
        inst = config.instances.get(member)
        if inst is None:
            continue
        msg = classify_state(state, inst.container)
        if msg is not None:
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
) -> None:
    """Verify compose-vs-container mount agreement for every running member (§3.17)."""
    compose_file = config.compose.file
    if compose_file is None:
        return
    for member in states:
        inst = config.instances.get(member)
        if inst is None or inst.service is None:
            continue
        expected = [(resolve_compose_path(b.host_source, compose_file.base_dir), b.container_target) for b in inst.service.binds]
        try:
            check_mount_drift(runtime, inst.container, expected)
        except ConfigError as exc:
            failures.append(PreflightFailure(f"§3.17 [{member}]", str(exc)))


def _check_resource_pack_sources(
    config: DeploymentConfig,
    failures: list[PreflightFailure],
) -> None:
    """Validate resource-pack source files and filenames for the partition (§7.7, §7.8)."""
    resourcepacks = config.sync_mapping.get("resourcepacks") or {}
    client_sub = resourcepacks.get("client") if isinstance(resourcepacks, dict) else None
    if not isinstance(client_sub, str) or not client_sub:
        if any(m in config.resource_packs for m in config.partition):
            failures.append(
                PreflightFailure(
                    "§7.7",
                    "[sync_mapping].resourcepacks.client is required when at least one pack is configured",
                )
            )
        return
    for member in config.partition:
        rp = config.resource_packs.get(member)
        if rp is None:
            continue
        try:
            validate_resource_pack_filename(rp.filename)
        except ConfigError as exc:
            failures.append(PreflightFailure(f"§7.5 [{member}]", str(exc)))
            continue
        source = config.sync_root / client_sub / rp.filename
        if not source.is_file():
            failures.append(PreflightFailure(f"§7.8 [{member}]", f"resource pack source not found: {source}"))


def _build_member_plans(
    config: DeploymentConfig,
    scopes: ScopeSet,
    targeted: bool,
    instance_changes: dict,
    mods_change,
    rp_changes: dict,
) -> dict[str, MemberPlan]:
    """Compute per-member effective action and reasons (§4.6)."""
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
    return plans


def _partition(
    plans: dict[str, MemberPlan],
    partition: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Partition into (none_set, reload_set, restart_set) by effective action."""
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
    return none_set, reload_set, restart_set


def run_preflight(
    config: DeploymentConfig,
    scopes: ScopeSet,
    with_resources: bool,
    notify: bool,
    dry_run: bool,
    runtime: DockerRuntime,
) -> PreflightPlan:
    """Run every preflight check, aggregate failures, return the plan.

    Raises PreflightError (a ConfigError, exit 3) if any check fails.
    """
    failures: list[PreflightFailure] = []
    warnings: list[str] = []

    # --- Top-level checks ------------------------------------------------
    if config.partition_unknown:
        failures.append(PreflightFailure("§2.5", "unknown --instance name(s): " + ", ".join(config.partition_unknown)))
    needs_instances = scopes.server or scopes.resource_pack
    if needs_instances and not config.instances:
        failures.append(PreflightFailure("§2.5", "no instances configured"))

    compose_needed = scopes.server
    if scopes.resource_pack and any(m in config.resource_packs for m in config.partition):
        compose_needed = True
    if compose_needed and not config.compose.ok:
        failures.append(
            PreflightFailure(
                "§3.5",
                f"compose is required for this scope but could not be loaded: {config.compose.error}",
            )
        )
    if (scopes.client or scopes.resource_pack) and config.www_dir is None:
        # §3.19: when www_dir is undeterminable, every candidate is
        # logged at WARN (one host source path per line) before the
        # fatal raise. The candidate list is empty when there were
        # zero candidates; the failure text carries the reason.
        for candidate in getattr(config, "www_dir_candidates", None) or []:
            logger.warning(f"www_dir candidate: {candidate}")
        failures.append(
            PreflightFailure(
                "§3.19",
                f"www_dir could not be determined: {config.www_dir_error or 'unknown reason'}",
            )
        )

    if _has_fatal(failures, "§3.5", "§2.5", "§3.19"):
        raise PreflightError(failures)

    # --- Per-member compose / lifecycle checks ---------------------------
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
                failures.append(PreflightFailure(f"§3.6 [{member}]", inst.service_match_error))
                continue
            if inst.stop_grace_parse_error is not None:
                failures.append(
                    PreflightFailure(
                        f"§3.2 [{member}]",
                        f"stop_grace_period parse failed: {inst.stop_grace_parse_error}",
                    )
                )
            if inst.service is None or inst.instance_root is None:
                failures.append(
                    PreflightFailure(
                        f"§3.6 [{member}]",
                        f"no /data bind found for container {inst.container!r}",
                    )
                )
                continue
            if not inst.service.has_healthcheck:
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
                failures.append(
                    PreflightFailure(
                        "§3.7",
                        "partition member(s) missing /data/mods bind: " + ", ".join(missing),
                    )
                )
            else:
                sources = {p for _n, p in mods_dir_candidates}
                if len(sources) != 1:
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
            container_states, restarting = _inspect_all(runtime, config, failures)
        if restarting:
            _settle_restarting(runtime, config, container_states, restarting)
        _classify_states(config, container_states, failures)
        _check_drift(runtime, config, container_states, failures)

    # --- Resource-pack source validation (§7.7 / §7.8) -------------------
    if scopes.resource_pack:
        _check_resource_pack_sources(config, failures)

    # --- Reachable Discord template validation ---------------------------
    if notify:
        template_failures = notifications.validate_live_and_failure(
            config.discord,
            notify=notify,
            dry_run=dry_run,
            has_scope=scopes.any(),
        )
        failures.extend(PreflightFailure(src, msg) for src, msg in template_failures)

    if failures:
        raise PreflightError(failures)

    # --- Plan assembly ---------------------------------------------------
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

    member_plans = _build_member_plans(config, scopes, targeted, instance_changes, mods_change, rp_changes)
    none_set, reload_set, restart_set = _partition(member_plans, config.partition)

    if needs_lifecycle and restart_set and not dry_run:
        rcon_failures = check_rcon_available(config, runtime, restart_set, container_states)
        if rcon_failures:
            raise PreflightError(rcon_failures)

    pack_required = any(p.pack_required for p in member_plans.values())
    pack_required_warning = None
    if pack_required and not scopes.client:
        pack_required_warning = "client pack content changed; the current ZIP is stale. Run --client to rebuild."
        warnings.append(pack_required_warning)

    if notify and not dry_run and restart_set:
        any_running_restart = any(container_states.get(m) is not None and container_states[m].is_running for m in restart_set)
        if any_running_restart:
            online_failures = notifications.validate_online(config.discord)
            if online_failures:
                raise PreflightError([PreflightFailure(src, msg) for src, msg in online_failures])

    return PreflightPlan(
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
