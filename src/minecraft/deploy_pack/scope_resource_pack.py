# src/minecraft/deploy_pack/scope_resource_pack.py

"""Resource-pack scope: ZIP publication and server.properties updates (Project_Specs.md Section 4.6.6, Section 4.9, Section 4.15, Section 7.5, Section 7.7, Section 7.8, Section 9.2).

Responsibilities:
  * validate source ZIPs (Section 4.9 step 1, Section 7.8)
  * compute each source's SHA-1 (Section 4.9 step 2, Section 4.4)
  * resolve destination paths and public URLs (Section 4.9 step 3, Section 7.5)
  * publish each ZIP atomically when the destination differs (Section 4.9
    step 4, Section 4.10)
  * update ``server.properties`` atomically (Section 4.9 step 5, Section 7.4, Section 4.10)

Slip mode (spec v1.3 Section 5.8, Section 14.4)
---------------------------------

When ``plan.slip_context`` is not None, per-member RP config comes from
``slip.resource_packs`` filtered by ``instance == member``; source ZIPs
come from ``ctx.rp_sources``. SHA-1 is computed from the source ZIP
(slip stores SHA-256 for integrity). The publish destination and URL
remain the same (driven by ``[sync_mapping].resourcepacks.resource_pack``
from ``deploy_pack.toml``, which is still loaded in slip mode).

Non-responsibilities:
  * Deciding the per-member effective action. Section 4.6.6's merge is done by
    preflight; this scope reads ``MemberPlan.resource_pack_target``.
  * Restart policy. A prompt-only change writes and defers (Section 4.15); the
    deferral itself is preflight's classification of the member into
    ``none_set``.
  * Notifications. notifications.py.

Structure
---------

The scope walks Section 4.9's five steps in order. Each step is one function:

    Section 1  Result dataclasses (public)
    Section 2  Source / destination helpers (pure)
    Section 3  Pending plan model (internal)
    Section 4  Phases 1-3: validate + plan all publishes
    Section 5  Phase 4: publish each ZIP whose destination differs
    Section 6  Phase 5: update each member's server.properties
    Section 7  Summary + public entry point
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config_model import DeploymentConfig, InstanceConfig, ResourcePackConfig
from .errors import ConfigError
from .files import (
    atomic_copy,
    build_resource_pack_url,
    compute_sha1,
    resolve_resource_pack_dest,
    validate_resource_pack_filename,
)
from .logging_setup import get_logger
from .preflight import PreflightPlan
from .properties import PropertyEdit, apply_edits

if TYPE_CHECKING:
    from minecraft.modpack_upgrade.slip_context import SlipContext

_log = get_logger(__name__)

__all__ = [
    "PropertiesWriteResult",
    "PublishResult",
    "ResourcePackScopeResult",
    "deploy_resource_pack_scope",
]


# ===========================================================================
# Section 1  Result dataclasses
# ===========================================================================


@dataclass
class PublishResult:
    """Outcome of publishing one member's resource-pack ZIP."""

    member: str
    filename: str
    source: Path
    destination: Path | None
    sha1: str
    published: bool
    skipped_reason: str | None = None


@dataclass
class PropertiesWriteResult:
    """Outcome of updating one member's server.properties."""

    member: str
    path: Path
    changes: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    wrote: bool = False

    @property
    def prompt_only(self) -> bool:
        """Checks whether only the resource-pack-prompt change was made and written."""
        return set(self.changes) == {"resource-pack-prompt"} and self.wrote


@dataclass
class ResourcePackScopeResult:
    """Outcome of the resource-pack scope write phase."""

    success: bool = True
    failure_message: str | None = None
    failure_stage: str | None = None
    failure_member: str | None = None
    no_pack_members: list[str] = field(default_factory=list)
    publish_results: list[PublishResult] = field(default_factory=list)
    properties_results: list[PropertiesWriteResult] = field(default_factory=list)

    @property
    def any_published(self) -> bool:
        """Checks whether any results were published."""
        return any(r.published for r in self.publish_results)

    @property
    def any_properties_written(self) -> bool:
        """Checks whether any properties were written."""
        return any(r.wrote for r in self.properties_results)


# ===========================================================================
# Section 2  Source / destination helpers
# ===========================================================================


def _client_source_dir(config: DeploymentConfig, logger: Any) -> Path | None:
    """Return ``sync_root / resourcepacks.client`` or None if unset."""
    rp_mapping = config.sync_mapping.get("resourcepacks")
    if not isinstance(rp_mapping, dict):
        logger.debug("_client_source_dir: [sync_mapping].resourcepacks is not a table")
        return None
    client_sub = rp_mapping.get("client")
    if not isinstance(client_sub, str) or not client_sub:
        logger.debug("_client_source_dir: [sync_mapping].resourcepacks.client not set")
        return None
    resolved = config.sync_root / client_sub
    logger.debug(f"_client_source_dir: {resolved}")
    return resolved


def _resource_pack_mapping(config: DeploymentConfig, logger: Any) -> str | None:
    """Return the ``resource_pack`` mapping value (``@www/...``) or None."""
    rp_mapping = config.sync_mapping.get("resourcepacks")
    if not isinstance(rp_mapping, dict):
        return None
    value = rp_mapping.get("resource_pack")
    if not isinstance(value, str) or not value:
        logger.debug("_resource_pack_mapping: [sync_mapping].resourcepacks.resource_pack not set")
        return None
    logger.debug(f"_resource_pack_mapping: {value!r}")
    return value


def _instance_server_properties(inst: InstanceConfig) -> Path | None:
    """Return the server.properties path for an instance, or None."""
    if inst.server_properties_path is not None:
        return inst.server_properties_path
    if inst.instance_root is not None:
        return inst.instance_root / "server.properties"
    return None


def _needs_publish(destination: Path, source_sha1: str, logger: Any = None) -> bool:
    """Section 4.4: publish when destination missing OR SHA-1 differs."""
    if logger is None:
        logger = _log
    if not destination.is_file():
        logger.debug(f"_needs_publish: {destination} does not exist; will publish")
        return True
    try:
        dest_sha1 = compute_sha1(destination)
    except OSError as exc:
        logger.warning(f"_needs_publish: could not hash {destination}: {exc}; will publish")
        return True
    differs = dest_sha1.lower() != source_sha1.lower()
    if differs:
        logger.debug(f"_needs_publish: {destination} sha1 differs (dest={dest_sha1} source={source_sha1}); will publish")
    else:
        logger.debug(f"_needs_publish: {destination} already matches source sha1")
    return differs


# ===========================================================================
# Section 3  Pending plan model
# ===========================================================================


@dataclass
class _PendingPublish:
    """One member's validated publish plan.

    ``rp`` is duck-typed: both ``ResourcePackConfig`` (legacy) and
    ``slip.ResourcePackEntry`` (slip mode) expose ``filename``,
    ``required``, and ``prompt``.
    """

    member: str
    rp: Any
    source: Path
    sha1: str
    destination: Path
    url: str
    target: dict[str, str]


# ===========================================================================
# Section 4  Phases 1-3: validate + plan
# ===========================================================================


def _partition_by_pack(
    config: DeploymentConfig,
    partition: list[str],
) -> tuple[list[tuple[str, ResourcePackConfig]], list[str]]:
    """Split the partition into (members_with_pack, no_pack_members)."""
    members_with_pack: list[tuple[str, ResourcePackConfig]] = []
    no_pack: list[str] = []
    for member in partition:
        rp = config.resource_packs.get(member)
        if rp is None:
            no_pack.append(member)
        else:
            members_with_pack.append((member, rp))
    return (members_with_pack, no_pack)


def _partition_by_pack_slip(
    ctx: SlipContext,
    partition: list[str],
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Slip-mode: split by slip-declared RPs; first entry wins on duplicates."""
    by_member: dict[str, Any] = {}
    for rp in ctx.slip.resource_packs:
        if rp.instance not in by_member:
            by_member[rp.instance] = rp
    members_with_pack: list[tuple[str, Any]] = []
    no_pack: list[str] = []
    for member in partition:
        if member in by_member:
            members_with_pack.append((member, by_member[member]))
        else:
            no_pack.append(member)
    return (members_with_pack, no_pack)


def _resolve_mapping_paths(
    config: DeploymentConfig,
    logger: Any,
) -> tuple[Path | None, str | None, str | None]:
    """Resolve the source dir and mapping value for RP publication."""
    source_dir = _client_source_dir(config, logger)
    if source_dir is None:
        return (
            None,
            None,
            "[sync_mapping].resourcepacks.client is required when at least one pack is configured",
        )
    dest_value = _resource_pack_mapping(config, logger)
    if dest_value is None:
        return (
            None,
            None,
            "[sync_mapping].resourcepacks.resource_pack is required when at least one pack is configured",
        )
    return (source_dir, dest_value, None)


def _plan_one_publish(
    member: str,
    rp: ResourcePackConfig,
    source_dir: Path,
    dest_value: str,
    www_dir: Path,
    download_base_url: str,
    logger: Any,
) -> tuple[_PendingPublish | None, str | None]:
    """Validate one member's pack and build its pending plan."""
    logger.debug(f"[{member}] validating resource pack {rp.filename!r}")

    try:
        validate_resource_pack_filename(rp.filename)
    except ConfigError as exc:
        return (None, str(exc))

    source = source_dir / rp.filename
    if not source.is_file():
        return (None, f"resource pack source not found: {source}")

    try:
        sha1 = compute_sha1(source)
        logger.debug(f"[{member}] {rp.filename} sha1={sha1}")
    except OSError as exc:
        return (None, f"could not hash {source}: {exc}")

    try:
        dest_dir = resolve_resource_pack_dest(dest_value, www_dir, logger)
    except ConfigError as exc:
        return (None, str(exc))

    destination = dest_dir / rp.filename

    try:
        url = build_resource_pack_url(download_base_url, dest_value, rp.filename, logger)
    except ConfigError as exc:
        return (None, str(exc))

    target = {
        "require-resource-pack": "true" if rp.required else "false",
        "resource-pack": url,
        "resource-pack-prompt": rp.prompt,
        "resource-pack-sha1": sha1,
    }
    pending = _PendingPublish(
        member=member,
        rp=rp,
        source=source,
        sha1=sha1,
        destination=destination,
        url=url,
        target=target,
    )
    logger.debug(f"[{member}] planned publish {source} -> {destination}")
    return (pending, None)


def _plan_one_publish_slip(
    member: str,
    rp: Any,
    ctx: SlipContext,
    dest_value: str,
    www_dir: Path,
    download_base_url: str,
    logger: Any,
) -> tuple[_PendingPublish | None, str | None]:
    """Slip-mode equivalent of :func:`_plan_one_publish`."""
    logger.debug(f"[{member}] validating slip resource pack {rp.filename!r}")

    try:
        validate_resource_pack_filename(rp.filename)
    except ConfigError as exc:
        return (None, str(exc))

    source = ctx.rp_sources.get(rp.filename)
    if source is None or not source.is_file():
        return (None, f"slip RP source not found for {rp.filename!r}")

    try:
        sha1 = compute_sha1(source)
        logger.debug(f"[{member}] {rp.filename} sha1={sha1}")
    except OSError as exc:
        return (None, f"could not hash {source}: {exc}")

    try:
        dest_dir = resolve_resource_pack_dest(dest_value, www_dir, logger)
    except ConfigError as exc:
        return (None, str(exc))

    destination = dest_dir / rp.filename

    try:
        url = build_resource_pack_url(download_base_url, dest_value, rp.filename, logger)
    except ConfigError as exc:
        return (None, str(exc))

    target = {
        "require-resource-pack": "true" if rp.required else "false",
        "resource-pack": url,
        "resource-pack-prompt": rp.prompt,
        "resource-pack-sha1": sha1,
    }
    pending = _PendingPublish(
        member=member,
        rp=rp,
        source=source,
        sha1=sha1,
        destination=destination,
        url=url,
        target=target,
    )
    logger.debug(f"[{member}] planned slip publish {source} -> {destination}")
    return (pending, None)


def _validate_and_plan_publishes(
    config: DeploymentConfig,
    partition: list[str],
    logger: Any,
) -> tuple[list[_PendingPublish], list[str], str | None, str | None]:
    """Phases 1 + 2 + 3 of Section 4.9."""
    members_with_pack, no_pack = _partition_by_pack(config, partition)
    logger.debug(f"_validate_and_plan_publishes: partition={partition} with_pack={[m for m, _ in members_with_pack]} no_pack={no_pack}")

    if not members_with_pack:
        return ([], no_pack, None, None)

    source_dir, dest_value, err = _resolve_mapping_paths(config, logger)
    if err is not None:
        return ([], no_pack, err, None)

    if config.www_dir is None:
        return (
            [],
            no_pack,
            "www_dir is not set; resource-pack scope requires it",
            None,
        )

    assert source_dir is not None and dest_value is not None
    pending: list[_PendingPublish] = []
    for member, rp in members_with_pack:
        p, err = _plan_one_publish(
            member,
            rp,
            source_dir,
            dest_value,
            config.www_dir,
            config.download_base_url,
            logger,
        )
        if err is not None:
            return ([], no_pack, err, member)
        assert p is not None
        pending.append(p)

    return (pending, no_pack, None, None)


def _validate_and_plan_publishes_slip(
    config: DeploymentConfig,
    plan: PreflightPlan,
    logger: Any,
) -> tuple[list[_PendingPublish], list[str], str | None, str | None]:
    """Slip-mode Phases 1 + 2 + 3 of Section 4.9."""
    ctx = plan.slip_context
    assert ctx is not None
    members_with_pack, no_pack = _partition_by_pack_slip(ctx, plan.partition)
    logger.debug(f"_validate_and_plan_publishes_slip: partition={list(plan.partition)} with_pack={[m for m, _ in members_with_pack]} no_pack={no_pack}")

    if not members_with_pack:
        return ([], no_pack, None, None)

    dest_value = _resource_pack_mapping(config, logger)
    if dest_value is None:
        return (
            [],
            no_pack,
            "[sync_mapping].resourcepacks.resource_pack is required when at least one pack is configured",
            None,
        )

    if config.www_dir is None:
        return (
            [],
            no_pack,
            "www_dir is not set; resource-pack scope requires it",
            None,
        )

    pending: list[_PendingPublish] = []
    for member, rp in members_with_pack:
        p, err = _plan_one_publish_slip(
            member,
            rp,
            ctx,
            dest_value,
            config.www_dir,
            config.download_base_url,
            logger,
        )
        if err is not None:
            return ([], no_pack, err, member)
        assert p is not None
        pending.append(p)

    return (pending, no_pack, None, None)


# ===========================================================================
# Section 5  Phase 4: publish
# ===========================================================================


def _publish_one(
    p: _PendingPublish,
    logger: Any,
) -> tuple[PublishResult | None, tuple[str, str] | None]:
    """Attempt a single publish (Section 4.9 phase 4)."""
    try:
        needed = _needs_publish(p.destination, p.sha1, logger)
    except Exception as exc:
        logger.error(f"resource-pack scope: publish check failed for {p.member}: {exc}")
        return (None, (f"could not check destination {p.destination}: {exc}", p.member))

    if not needed:
        result = PublishResult(
            member=p.member,
            filename=p.rp.filename,
            source=p.source,
            destination=p.destination,
            sha1=p.sha1,
            published=False,
            skipped_reason="destination already matches",
        )
        logger.info(f"[{p.member}] resource pack {p.rp.filename} already up to date at {p.destination}")
        return (result, None)

    try:
        atomic_copy(p.source, p.destination, logger=logger)
    except Exception as exc:
        logger.error(f"resource-pack scope: publish failed for {p.member}: {exc}")
        return (
            None,
            (f"could not publish {p.source} -> {p.destination}: {exc}", p.member),
        )

    result = PublishResult(
        member=p.member,
        filename=p.rp.filename,
        source=p.source,
        destination=p.destination,
        sha1=p.sha1,
        published=True,
    )
    logger.info(f"[{p.member}] published {p.rp.filename} -> {p.destination}")
    return (result, None)


def _run_publish_phase(
    pending: list[_PendingPublish],
    logger: Any,
) -> tuple[list[PublishResult], str | None, str | None]:
    """Section 4.9 phase 4: publish every pending ZIP."""
    results: list[PublishResult] = []
    for p in pending:
        pr, err = _publish_one(p, logger)
        if err is not None:
            msg, member = err
            return (results, msg, member)
        assert pr is not None
        results.append(pr)
    return (results, None, None)


# ===========================================================================
# Section 6  Phase 5: server.properties
# ===========================================================================


def _write_properties_one(
    p: _PendingPublish,
    config: DeploymentConfig,
    logger: Any,
) -> tuple[PropertiesWriteResult | None, str | None]:
    """Update one member's server.properties (Section 4.9 phase 5)."""
    inst = config.instances.get(p.member)
    if inst is None:
        logger.warning(f"resource-pack scope: partition member {p.member!r} not in config.instances; skipping properties")
        return (None, None)

    props_path = _instance_server_properties(inst)
    if props_path is None:
        logger.warning(f"[{p.member}] no server.properties path; skipping resource-pack key updates")
        return (None, None)

    edits = [PropertyEdit(k, v) for k, v in p.target.items()]
    logger.debug(f"[{p.member}] applying {len(edits)} key(s) to {props_path}")
    try:
        diff = apply_edits(props_path, edits, logger=logger)
    except Exception as exc:
        logger.error(f"resource-pack scope: properties update failed for {p.member}: {exc}")
        return (None, f"could not update {props_path}: {exc}")

    changes = {c.key: (c.before, c.after) for c in diff.changes}
    write_result = PropertiesWriteResult(member=p.member, path=props_path, changes=changes, wrote=diff.any)

    if diff.any:
        if write_result.prompt_only:
            logger.warning(f"[{p.member}] {props_path}: only resource-pack-prompt changed; write deferred (no restart, Section 4.15)")
        else:
            logger.info(f"[{p.member}] updated {props_path} ({len(changes)} key(s))")
    else:
        logger.debug(f"[{p.member}] {props_path}: no effective change")

    return (write_result, None)


def _run_properties_phase(
    pending: list[_PendingPublish],
    config: DeploymentConfig,
    logger: Any,
) -> tuple[list[PropertiesWriteResult], str | None, str | None]:
    """Section 4.9 phase 5: update every pending member's server.properties."""
    results: list[PropertiesWriteResult] = []
    for p in pending:
        wr, err = _write_properties_one(p, config, logger)
        if err is not None:
            return (results, err, p.member)
        if wr is not None:
            results.append(wr)
    return (results, None, None)


# ===========================================================================
# Section 7  Summary + public entry point
# ===========================================================================


def _log_rp_summary(result: ResourcePackScopeResult, logger: Any) -> None:
    """Emit the closing INFO line with per-phase counts."""
    published_count = sum(1 for r in result.publish_results if r.published)
    skipped_count = sum(1 for r in result.publish_results if not r.published)
    props_written = sum(1 for r in result.properties_results if r.wrote)
    logger.info(
        f"resource-pack scope: complete; {published_count} published, "
        f"{skipped_count} skipped (destination match), "
        f"{props_written}/{len(result.properties_results)} properties file(s) written"
    )


def _fail(
    result: ResourcePackScopeResult,
    stage: str,
    message: str,
    member: str | None,
    logger: Any,
) -> ResourcePackScopeResult:
    """Populate ``result`` with a failure and return it."""
    result.success = False
    result.failure_stage = stage
    result.failure_message = message
    result.failure_member = member
    logger.error(f"resource-pack scope: {stage} failed for {member or '(global)'}: {message}")
    return result


def deploy_resource_pack_scope(
    config: DeploymentConfig,
    plan: PreflightPlan,
    protect_patterns: list[str],
    logger: Any = None,
) -> ResourcePackScopeResult:
    """Execute the resource-pack scope write phase (Section 4.9).

    Dispatches to slip mode when ``plan.slip_context`` is set; legacy
    otherwise. Order (mandatory per Section 4.9):

      1. Validate all source ZIPs, compute all SHA-1s, resolve all
         destinations and URLs.
      2. Publish every ZIP whose destination is missing or differs.
      3. Update every partition member's server.properties.

    Halts on the first failure inside any phase. Completed phases are
    not rolled back (Section 4.2). Returns a structured result; the caller
    applies Section 4.7's failure handling.

    Read-only with respect to Docker.
    """
    if logger is None:
        logger = _log

    mode = "slip" if plan.slip_context is not None else "legacy"
    logger.info(f"resource-pack scope: partition={list(plan.partition)} mode={mode}")

    result = ResourcePackScopeResult()

    if plan.slip_context is not None:
        pending, no_pack, err, err_member = _validate_and_plan_publishes_slip(config, plan, logger)
    else:
        pending, no_pack, err, err_member = _validate_and_plan_publishes(config, plan.partition, logger)
    result.no_pack_members = no_pack

    if err is not None:
        return _fail(result, "validate", err, err_member, logger)

    if not pending:
        logger.info("resource-pack scope: no packs configured for partition; no-op")
        return result

    logger.debug(f"resource-pack scope: {len(pending)} pending publish(es)")

    publish_results, err, err_member = _run_publish_phase(pending, logger)
    result.publish_results = publish_results
    if err is not None:
        return _fail(result, "publish", err, err_member, logger)

    properties_results, err, err_member = _run_properties_phase(pending, config, logger)
    result.properties_results = properties_results
    if err is not None:
        return _fail(result, "properties", err, err_member, logger)

    _log_rp_summary(result, logger)
    if result.no_pack_members:
        logger.debug(f"resource-pack scope: members with no pack: {result.no_pack_members}")
    return result
