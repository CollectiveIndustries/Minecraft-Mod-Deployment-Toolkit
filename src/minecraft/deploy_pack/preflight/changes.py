# src/minecraft/deploy_pack/preflight/changes.py

"""Effective-change computation (§4.4, §4.6.6). Pure read-only I/O.

Logging
-------

Module logger is ``minecraft.deploy_pack.preflight.changes``. Every
function here is a read-only diff against the current filesystem and
has no failure modes - each branch is a safe early return. There are
therefore no ERROR or WARN sites; the operator's actionable view comes
from ``run_preflight``'s aggregated failures, not from this module.

``compute_mods_change`` is called once per run and logs its aggregate
at INFO (added / updated / removed counts, and the source-set size).
``compute_instance_server_change`` runs once per partition member and
logs at DEBUG: one line per non-shared sync-mapping key with the
per-key change counts, and one final line with the member totals.
``compute_resource_pack_change`` runs once per partition member with a
configured pack and logs each early-return branch at DEBUG with the
reason, then a final DEBUG line with the action and whether a publish
is needed.

Calls into other modules (``load_side_overrides``,
``deps.resolve_mod_sources``, ``hash_tree``, ``hash_flat_dir``,
``build_resource_pack_url``, ``compute_diff``) do not thread a logger:
each sub-module emits under its own module name, so the operator's
``--debug`` output shows which file produced which line. This module's
own ``_log`` covers its own messages.
"""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack import deps
from minecraft.deploy_pack.config_model import DeploymentConfig
from minecraft.deploy_pack.files import (
    build_resource_pack_url,
    compute_sha1,
    compute_sha256,
    hash_flat_dir,
    hash_tree,
    is_shared_dest,
    resolve_mapping_for_side,
)
from minecraft.deploy_pack.logging_setup import get_logger
from minecraft.deploy_pack.overrides import load_side_overrides
from minecraft.deploy_pack.properties import PropertyEdit, compute_diff

from .types import InstanceServerChange, ModsChange, ResourcePackChange

_log = get_logger(__name__)


def compute_mods_change(config: DeploymentConfig, mods_dir: Path | None) -> ModsChange | None:
    """Diff the server-side mod set against the current mods_dir (flat, §4.11)."""
    if mods_dir is None:
        _log.debug("compute_mods_change: mods_dir is None; no diff computed")
        return None
    _log.debug(f"compute_mods_change: mods_dir={mods_dir}")
    overrides = load_side_overrides(config.config_dir / "side_overrides.toml")
    sources = deps.resolve_mod_sources(config.modpack_dir, "server", overrides)
    src: dict[str, str] = {}
    for filename, path in sources.items():
        try:
            src[filename] = compute_sha256(path)
        except OSError:
            continue
    dst = hash_flat_dir(mods_dir)
    change = ModsChange(
        added=sorted(set(src) - set(dst)),
        updated=sorted(f for f in set(src) & set(dst) if src[f] != dst[f]),
        removed=sorted(set(dst) - set(src)),
    )
    _log.info(
        f"compute_mods_change: {mods_dir} -> +{len(change.added)} ~{len(change.updated)} -{len(change.removed)} (source set {len(src)}, dest set {len(dst)})"
    )
    return change


def compute_instance_server_change(config: DeploymentConfig, member: str) -> InstanceServerChange:
    """Diff the sync-mapping sources against a member's config/kubejs tree."""
    inst = config.instances.get(member)
    change = InstanceServerChange(member=member)
    if inst is None or inst.instance_root is None:
        _log.debug(f"compute_instance_server_change: [{member}] no instance or instance_root; empty change")
        return change
    _log.debug(f"compute_instance_server_change: [{member}] instance_root={inst.instance_root}")
    for key, mapping_value in config.sync_mapping.items():
        dest_rel = resolve_mapping_for_side(mapping_value, "server")
        if dest_rel is None:
            _log.debug(f"compute_instance_server_change: [{member}] {key}: excluded on server side")
            continue
        if is_shared_dest(dest_rel):
            _log.debug(f"compute_instance_server_change: [{member}] {key}: shared dest {dest_rel!r} (client scope's concern)")
            continue
        src = config.sync_root / key
        dst = inst.instance_root / dest_rel
        if not src.is_dir():
            _log.debug(f"compute_instance_server_change: [{member}] {key}: source {src} absent")
            continue
        src_map = hash_tree(src)
        dst_map = hash_tree(dst)
        added = [f"{dest_rel}/{rel}" for rel in sorted(set(src_map) - set(dst_map))]
        removed = [f"{dest_rel}/{rel}" for rel in sorted(set(dst_map) - set(src_map))]
        updated = [f"{dest_rel}/{rel}" for rel in sorted(set(src_map) & set(dst_map)) if src_map[rel] != dst_map[rel]]
        change.added.extend(added)
        change.removed.extend(removed)
        change.updated.extend(updated)
        _log.debug(f"compute_instance_server_change: [{member}] {key} -> +{len(added)} ~{len(updated)} -{len(removed)}")
    change.changed_paths = change.added + change.updated + change.removed
    _log.debug(
        f"compute_instance_server_change: [{member}] total +{len(change.added)} ~{len(change.updated)} -{len(change.removed)} "
        f"({len(change.changed_paths)} path(s))"
    )
    return change


def compute_resource_pack_change(config: DeploymentConfig, member: str) -> ResourcePackChange:
    """Server.properties target values and RP publish decision (§4.6.6, §7.5)."""
    change = ResourcePackChange(member=member)
    rp = config.resource_packs.get(member)
    if rp is None:
        _log.debug(f"compute_resource_pack_change: [{member}] no [resource_pack.{member}]; nothing to do")
        return change
    inst = config.instances.get(member)
    if inst is None or inst.server_properties_path is None:
        _log.debug(f"compute_resource_pack_change: [{member}] no instance or server_properties_path")
        return change
    resourcepacks = config.sync_mapping.get("resourcepacks") or {}
    if not isinstance(resourcepacks, dict):
        _log.debug(f"compute_resource_pack_change: [{member}] [sync_mapping].resourcepacks is not a table")
        return change
    dest_value = resourcepacks.get("resource_pack")
    if not dest_value or not isinstance(dest_value, str):
        _log.debug(f"compute_resource_pack_change: [{member}] [sync_mapping].resourcepacks.resource_pack not set")
        return change
    client_sub = resourcepacks.get("client")
    source_dir = config.sync_root / client_sub if client_sub else None
    source_zip: Path | None = None
    if source_dir is not None:
        candidate = source_dir / rp.filename
        if candidate.is_file():
            source_zip = candidate
    if source_zip is None:
        _log.debug(f"compute_resource_pack_change: [{member}] source ZIP {rp.filename!r} not found under {source_dir}")
        return change
    source_sha1 = compute_sha1(source_zip)
    change.source_sha1 = source_sha1
    change.source_zip = source_zip
    _log.debug(f"compute_resource_pack_change: [{member}] source {source_zip} sha1={source_sha1}")
    url = build_resource_pack_url(config.download_base_url, dest_value, rp.filename)
    targets: dict[str, str] = {
        "require-resource-pack": "true" if rp.required else "false",
        "resource-pack": url,
        "resource-pack-prompt": rp.prompt,
        "resource-pack-sha1": source_sha1,
    }
    edits = [PropertyEdit(k, v) for k, v in targets.items()]
    diff = compute_diff(inst.server_properties_path, edits)
    for c in diff.changes:
        change.properties_changes[c.key] = (c.before, c.after)
    dest_dir = config.www_dir / dest_value[5:] if config.www_dir else None
    if dest_dir is not None:
        dest_zip = dest_dir / rp.filename
        if not dest_zip.is_file():
            change.publish_needed = True
            _log.debug(f"compute_resource_pack_change: [{member}] dest {dest_zip} missing; publish needed")
        else:
            try:
                dest_sha1 = compute_sha1(dest_zip)
                change.publish_needed = dest_sha1 != source_sha1
                if change.publish_needed:
                    _log.debug(f"compute_resource_pack_change: [{member}] dest sha1 {dest_sha1} != source {source_sha1}; publish needed")
                else:
                    _log.debug(f"compute_resource_pack_change: [{member}] dest already matches source sha1")
            except OSError as exc:
                change.publish_needed = True
                _log.debug(f"compute_resource_pack_change: [{member}] could not hash {dest_zip}: {exc}; publish needed")
    restart_keys = {"require-resource-pack", "resource-pack", "resource-pack-sha1"}
    if any(k in restart_keys for k in change.properties_changes):
        change.action = "restart"
    else:
        change.action = "none"
    _log.debug(
        f"compute_resource_pack_change: [{member}] action={change.action!r} publish_needed={change.publish_needed} "
        f"property_changes={sorted(change.properties_changes)}"
    )
    return change
