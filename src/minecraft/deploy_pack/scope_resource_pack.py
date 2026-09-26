# src/minecraft/deploy_pack/scope_resource_pack.py

"""Resource-pack scope: ZIP publication and server.properties updates (Project_Specs.md §4.6.6, §4.9, §4.15, §7.5, §7.7, §7.8, §9.2).

Responsibilities:
  * validate source ZIPs (§4.9 step 1, §7.8)
  * compute each source's SHA-1 (§4.9 step 2, §4.4)
  * resolve destination paths and public URLs (§4.9 step 3, §7.5)
  * publish each ZIP atomically when the destination differs (§4.9
    step 4, §4.10)
  * update ``server.properties`` atomically (§4.9 step 5, §7.4, §4.10)

Non-responsibilities:
  * Deciding the per-member effective action. §4.6.6's merge is done by
    preflight; this scope reads ``MemberPlan.resource_pack_target``.
  * Restart policy. A prompt-only change writes and defers (§4.15); the
    deferral itself is preflight's classification of the member into
    ``none_set``.
  * Notifications. notifications.py.

Zero-pack path (§7.7)
---------------------

A partition member with no ``[resource_pack.X]`` section has nothing to
publish and no properties to touch. The scope reports it in
``no_pack_members`` and moves on. If the entire partition has zero
packs, the scope is a no-op and returns success - no compose read, no
source validation, no properties write.

Logging
-------

The scope logs at INFO on entry, on each publish, and on each
server.properties update. Per-member validation, SHA-1 computation,
destination resolution, and skip decisions log at DEBUG. Warnings the
operator needs to act on (missing server.properties path, RP
prompt-only deferral) log at WARN. Failures log at ERROR with the
failing stage (``validate``, ``publish``, ``properties``) and the
member name where applicable. The module logger is
``minecraft.deploy_pack.scope_resource_pack``; callers may inject an
override via ``logger=`` for a single call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_model import DeploymentConfig, InstanceConfig, ResourcePackConfig
from .errors import ConfigError
from .files import atomic_copy, build_resource_pack_url, compute_sha1, resolve_resource_pack_dest, validate_resource_pack_filename
from .logging_setup import get_logger
from .preflight import PreflightPlan
from .properties import PropertyEdit, apply_edits

_log = get_logger(__name__)

__all__ = ["PropertiesWriteResult", "PublishResult", "ResourcePackScopeResult", "deploy_resource_pack_scope"]


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


def _client_source_dir(config: DeploymentConfig, logger: Any) -> Path | None:
    """Return ``sync_root / resourcepacks.client`` or None if unset.

    §7.6: the source path resolves from ``[sync_mapping].resourcepacks.client``
    under sync_root. A missing or non-string value means RP publication
    cannot happen; preflight (§7.7 / §7.8) validates this when a pack is
    configured for the partition.
    """
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


@dataclass
class _PendingPublish:
    member: str
    rp: ResourcePackConfig
    source: Path
    sha1: str
    destination: Path
    url: str
    target: dict[str, str]


def _validate_and_plan_publishes(
    config: DeploymentConfig,
    partition: list[str],
    logger: Any,
) -> tuple[list[_PendingPublish], list[str], str | None, str | None]:
    """Phase 1 + 2 + 3 of §4.9.

    Returns ``(pending, no_pack_members, error_message, error_member)``.
    On error, ``pending`` is empty and error_message is set.
    """
    no_pack: list[str] = []
    pending: list[_PendingPublish] = []
    members_with_pack: list[tuple[str, ResourcePackConfig]] = []
    for member in partition:
        rp = config.resource_packs.get(member)
        if rp is None:
            no_pack.append(member)
            continue
        members_with_pack.append((member, rp))

    logger.debug(f"_validate_and_plan_publishes: partition={partition} with_pack={[m for m, _ in members_with_pack]} no_pack={no_pack}")

    if not members_with_pack:
        return (pending, no_pack, None, None)

    source_dir = _client_source_dir(config, logger)
    if source_dir is None:
        return (pending, no_pack, "[sync_mapping].resourcepacks.client is required when at least one pack is configured", None)

    dest_value = _resource_pack_mapping(config, logger)
    if dest_value is None:
        return (pending, no_pack, "[sync_mapping].resourcepacks.resource_pack is required when at least one pack is configured", None)

    if config.www_dir is None:
        return (pending, no_pack, "www_dir is not set; resource-pack scope requires it", None)

    for member, rp in members_with_pack:
        logger.debug(f"[{member}] validating resource pack {rp.filename!r}")
        try:
            validate_resource_pack_filename(rp.filename)
        except ConfigError as exc:
            return (pending, no_pack, str(exc), member)

        source = source_dir / rp.filename
        if not source.is_file():
            return (pending, no_pack, f"resource pack source not found: {source}", member)

        try:
            sha1 = compute_sha1(source)
            logger.debug(f"[{member}] {rp.filename} sha1={sha1}")
        except OSError as exc:
            return (pending, no_pack, f"could not hash {source}: {exc}", member)

        try:
            dest_dir = resolve_resource_pack_dest(dest_value, config.www_dir, logger)
        except ConfigError as exc:
            return (pending, no_pack, str(exc), member)

        destination = dest_dir / rp.filename
        try:
            url = build_resource_pack_url(config.download_base_url, dest_value, rp.filename, logger)
        except ConfigError as exc:
            return (pending, no_pack, str(exc), member)

        target = {
            "require-resource-pack": "true" if rp.required else "false",
            "resource-pack": url,
            "resource-pack-prompt": rp.prompt,
            "resource-pack-sha1": sha1,
        }
        pending.append(
            _PendingPublish(
                member=member,
                rp=rp,
                source=source,
                sha1=sha1,
                destination=destination,
                url=url,
                target=target,
            )
        )
        logger.debug(f"[{member}] planned publish {source} -> {destination}")

    return (pending, no_pack, None, None)


def _needs_publish(destination: Path, source_sha1: str, logger: Any = None) -> bool:
    """§4.4: publish when destination missing OR SHA-1 differs.

    SHA-1 comparison is case-insensitive. This guard is for a
    destination that was written by something else.
    """
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


def deploy_resource_pack_scope(
    config: DeploymentConfig,
    plan: PreflightPlan,
    protect_patterns: list[str],
    logger: Any = None,
) -> ResourcePackScopeResult:
    """Execute the resource-pack scope write phase (§4.9).

    Order (mandatory per §4.9):

      1. Validate all source ZIPs, compute all SHA-1s, resolve all
         destinations and URLs.
      2. Publish every ZIP whose destination is missing or differs.
      3. Update every partition member's server.properties.

    Halts on the first failure inside any phase. Completed phases are
    not rolled back (§4.2). Returns a structured result; the caller
    applies §4.7's failure handling.

    Read-only with respect to Docker.
    """
    if logger is None:
        logger = _log

    logger.info(f"resource-pack scope: partition={list(plan.partition)}")

    result = ResourcePackScopeResult()

    # ------------------------------------------------------------------
    # Phase 1-3: validate + plan
    # ------------------------------------------------------------------
    pending, no_pack, err, err_member = _validate_and_plan_publishes(config, plan.partition, logger)
    result.no_pack_members = no_pack

    if err is not None:
        result.success = False
        result.failure_stage = "validate"
        result.failure_message = err
        result.failure_member = err_member
        logger.error(f"resource-pack scope: validate failed for {err_member or '(global)'}: {err}")
        return result

    if not pending:
        logger.info("resource-pack scope: no packs configured for partition; no-op")
        return result

    logger.debug(f"resource-pack scope: {len(pending)} pending publish(es)")

    # ------------------------------------------------------------------
    # Phase 4: publish
    # ------------------------------------------------------------------
    for p in pending:
        try:
            needed = _needs_publish(p.destination, p.sha1, logger)
        except Exception as exc:
            result.success = False
            result.failure_stage = "publish"
            result.failure_message = f"could not check destination {p.destination}: {exc}"
            result.failure_member = p.member
            logger.error(f"resource-pack scope: publish check failed for {p.member}: {exc}")
            return result

        if not needed:
            result.publish_results.append(
                PublishResult(
                    member=p.member,
                    filename=p.rp.filename,
                    source=p.source,
                    destination=p.destination,
                    sha1=p.sha1,
                    published=False,
                    skipped_reason="destination already matches",
                )
            )
            logger.info(f"[{p.member}] resource pack {p.rp.filename} already up to date at {p.destination}")
            continue

        try:
            atomic_copy(p.source, p.destination, logger=logger)
        except Exception as exc:
            result.success = False
            result.failure_stage = "publish"
            result.failure_message = f"could not publish {p.source} -> {p.destination}: {exc}"
            result.failure_member = p.member
            logger.error(f"resource-pack scope: publish failed for {p.member}: {exc}")
            return result

        result.publish_results.append(
            PublishResult(
                member=p.member,
                filename=p.rp.filename,
                source=p.source,
                destination=p.destination,
                sha1=p.sha1,
                published=True,
            )
        )
        logger.info(f"[{p.member}] published {p.rp.filename} -> {p.destination}")

    # ------------------------------------------------------------------
    # Phase 5: server.properties
    # ------------------------------------------------------------------
    for p in pending:
        inst = config.instances.get(p.member)
        if inst is None:
            logger.warning(f"resource-pack scope: partition member {p.member!r} not in config.instances; skipping properties")
            continue
        props_path = _instance_server_properties(inst)
        if props_path is None:
            logger.warning(f"[{p.member}] no server.properties path; skipping resource-pack key updates")
            continue

        edits = [PropertyEdit(k, v) for k, v in p.target.items()]
        logger.debug(f"[{p.member}] applying {len(edits)} key(s) to {props_path}")
        try:
            diff = apply_edits(props_path, edits, logger=logger)
        except ConfigError as exc:
            result.success = False
            result.failure_stage = "properties"
            result.failure_message = f"could not update {props_path}: {exc}"
            result.failure_member = p.member
            logger.error(f"resource-pack scope: properties update failed for {p.member}: {exc}")
            return result
        except Exception as exc:
            result.success = False
            result.failure_stage = "properties"
            result.failure_message = f"could not update {props_path}: {exc}"
            result.failure_member = p.member
            logger.error(f"resource-pack scope: properties update failed for {p.member}: {exc}")
            return result

        changes = {c.key: (c.before, c.after) for c in diff.changes}
        write_result = PropertiesWriteResult(member=p.member, path=props_path, changes=changes, wrote=diff.any)
        result.properties_results.append(write_result)

        if diff.any:
            if write_result.prompt_only:
                logger.warning(f"[{p.member}] {props_path}: only resource-pack-prompt changed; write deferred (no restart, §4.15)")
            else:
                logger.info(f"[{p.member}] updated {props_path} ({len(changes)} key(s))")
        else:
            logger.debug(f"[{p.member}] {props_path}: no effective change")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    published_count = sum(1 for r in result.publish_results if r.published)
    skipped_count = sum(1 for r in result.publish_results if not r.published)
    props_written = sum(1 for r in result.properties_results if r.wrote)
    logger.info(
        f"resource-pack scope: complete; {published_count} published, "
        f"{skipped_count} skipped (destination match), "
        f"{props_written}/{len(result.properties_results)} properties file(s) written"
    )
    if result.no_pack_members:
        logger.debug(f"resource-pack scope: members with no pack: {result.no_pack_members}")
    return result
