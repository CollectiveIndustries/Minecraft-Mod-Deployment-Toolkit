# src/minecraft/deploy_pack/preflight/changes.py

"""Effective-change computation (§4.4, §4.6.6). Pure read-only I/O."""

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
from minecraft.deploy_pack.overrides import load_side_overrides
from minecraft.deploy_pack.properties import PropertyEdit, compute_diff

from .types import InstanceServerChange, ModsChange, ResourcePackChange


def compute_mods_change(config: DeploymentConfig, mods_dir: Path | None) -> ModsChange | None:
    """Diff the server-side mod set against the current mods_dir (flat, §4.11)."""
    if mods_dir is None:
        return None
    overrides = load_side_overrides(config.config_dir / "side_overrides.toml")
    sources = deps.resolve_mod_sources(config.modpack_dir, "server", overrides)
    src: dict[str, str] = {}
    for filename, path in sources.items():
        try:
            src[filename] = compute_sha256(path)
        except OSError:
            continue
    dst = hash_flat_dir(mods_dir)
    return ModsChange(
        added=sorted(set(src) - set(dst)),
        updated=sorted(f for f in set(src) & set(dst) if src[f] != dst[f]),
        removed=sorted(set(dst) - set(src)),
    )


def compute_instance_server_change(config: DeploymentConfig, member: str) -> InstanceServerChange:
    """Diff the sync-mapping sources against a member's config/kubejs tree."""
    inst = config.instances.get(member)
    change = InstanceServerChange(member=member)
    if inst is None or inst.instance_root is None:
        return change
    for key, mapping_value in config.sync_mapping.items():
        dest_rel = resolve_mapping_for_side(mapping_value, "server")
        if dest_rel is None or is_shared_dest(dest_rel):
            continue
        src = config.sync_root / key
        dst = inst.instance_root / dest_rel
        if not src.is_dir():
            continue
        src_map = hash_tree(src)
        dst_map = hash_tree(dst)
        change.added.extend(f"{dest_rel}/{rel}" for rel in sorted(set(src_map) - set(dst_map)))
        change.removed.extend(f"{dest_rel}/{rel}" for rel in sorted(set(dst_map) - set(src_map)))
        change.updated.extend(f"{dest_rel}/{rel}" for rel in sorted(set(src_map) & set(dst_map)) if src_map[rel] != dst_map[rel])
    change.changed_paths = change.added + change.updated + change.removed
    return change


def compute_resource_pack_change(config: DeploymentConfig, member: str) -> ResourcePackChange:
    """Server.properties target values and RP publish decision (§4.6.6, §7.5)."""
    change = ResourcePackChange(member=member)
    rp = config.resource_packs.get(member)
    if rp is None:
        return change
    inst = config.instances.get(member)
    if inst is None or inst.server_properties_path is None:
        return change
    resourcepacks = config.sync_mapping.get("resourcepacks") or {}
    if not isinstance(resourcepacks, dict):
        return change
    dest_value = resourcepacks.get("resource_pack")
    if not dest_value or not isinstance(dest_value, str):
        return change
    client_sub = resourcepacks.get("client")
    source_dir = config.sync_root / client_sub if client_sub else None
    source_zip: Path | None = None
    if source_dir is not None:
        candidate = source_dir / rp.filename
        if candidate.is_file():
            source_zip = candidate
    if source_zip is None:
        return change
    source_sha1 = compute_sha1(source_zip)
    change.source_sha1 = source_sha1
    change.source_zip = source_zip
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
        else:
            try:
                change.publish_needed = compute_sha1(dest_zip) != source_sha1
            except OSError:
                change.publish_needed = True
    restart_keys = {"require-resource-pack", "resource-pack", "resource-pack-sha1"}
    if any(k in restart_keys for k in change.properties_changes):
        change.action = "restart"
    else:
        change.action = "none"
    return change
