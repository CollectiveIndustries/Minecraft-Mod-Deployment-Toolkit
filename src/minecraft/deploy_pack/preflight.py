# src/minecraft/deploy_pack/preflight.py

"""Validation aggregation and deployment planning (Project_Specs.md §4.1, §4.3).

Responsibilities (§9.2):
  * aggregate all independently-detectable failures before any write (§4.3)
  * validate reachable Discord templates when --notify (§5.11, via
    notifications.py)
  * verify RCON availability for any restart_set member that will receive
    a restart notice (§8.4)
  * compute effective changes per partition member (§4.4, §4.6)
  * resolve per-path action via the restart adapter (§4.6.1-4.6.3)
  * merge actions into per-instance effective_action (§4.6.6)
  * partition into none_set / reload_set / restart_set (§4.6.4)
  * inspect running containers and classify state (§4.12)
  * bounded wait on restarting containers (§4.12)
  * verify compose-vs-container mount drift (§3.17)

Non-responsibilities:
  * Scope execution (write phase). That's scope_server, scope_client,
    scope_resource_pack.
  * Docker lifecycle. hooks.py.
  * Notification rendering and template validation rules. Those live in
    notifications.py; this module only routes preflight failures out of
    the notifications API and into the aggregation model.

Exit-code contract (§2.4):
  * Configuration / preflight failures → exit 3
  * Docker daemon unavailable at preflight → exit 3
  * Usage errors (exit 2) are main.py's job, evaluated before this runs.

Aggregation model (§4.3):
  Failures are accumulated, not raised on first detection. If any
  failure remains after every check has run, a single PreflightError
  carrying all of them is raised. Checks that depend on an earlier
  failed check are skipped (e.g. instance-root validation cannot run
  if compose failed to load).

Unmarked entries (§6.3)
-----------------------

The mods-change computation (§4.4) must diff the set of jars the server
scope *intends* to place in ``mods_dir`` against what is actually there.
An unmarked entry - one whose ``.pw.toml`` declares an explicit ``side``
outside ``{client, server, both}``, or which is not overridden - is not
part of that intent set. Including it would generate a spurious "removed"
entry on every run, because the scope never writes it in the first place.

The filter here mirrors ``scope_server._resolve_mods_source``: drop
unmarked entries unless an override in any section of
``side_overrides.toml`` marks them. Overrides are then applied on the
marked set so the side filter and closure see the override's value.

Mods drift for targeted deploys (§2.9)
--------------------------------------

``--server --instance X`` does not write to ``mods_dir``, but §2.9 still
requires a drift warning when the shared directory diverges from the
full source set. The drift computation therefore runs for **both**
targeted and non-targeted server deploys: ``mods_dir`` is derived from
compose in both cases, and the diff always uses the full source set (the
set a non-targeted deploy would place). Only the *warning* is
targeted-specific - the informational line in the live message renders
iff the drift exists and the deploy is targeted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import deps, notifications
from .config_model import DeploymentConfig, derive_mods_dir, resolve_compose_path
from .docker_runtime import ContainerState, DockerRuntime, check_mount_drift, select_rcon_transport
from .errors import ConfigError
from .files import (
    build_resource_pack_url,
    compute_sha1,
    compute_sha256,
    is_shared_dest,
    resolve_mapping_for_side,
    validate_resource_pack_filename,
)
from .overrides import apply_side_overrides, load_side_overrides
from .properties import PropertyEdit, compute_diff

__all__ = [
    "InstanceServerChange",
    "MemberPlan",
    "ModsChange",
    "PreflightError",
    "PreflightFailure",
    "PreflightPlan",
    "ReasonEntry",
    "ResourcePackChange",
    "ScopeSet",
    "run_preflight",
]


@dataclass(frozen=True)
class ScopeSet:
    """Which deployment scopes are active for this invocation (§2.1)."""

    server: bool = False
    client: bool = False
    resource_pack: bool = False

    def any(self) -> bool:
        """Checks whether any of the server, client, or resource pack is present."""
        return self.server or self.client or self.resource_pack

    def names(self) -> list[str]:
        """Scope names in §5.4's fixed order."""
        out: list[str] = []
        if self.server:
            out.append("server")
        if self.client:
            out.append("client")
        if self.resource_pack:
            out.append("resource-pack")
        return out

    def bitmask(self) -> int:
        """§5.3: server=1, client=2, resource-pack=4."""
        m = 0
        if self.server:
            m |= 1
        if self.client:
            m |= 2
        if self.resource_pack:
            m |= 4
        return m


@dataclass
class PreflightFailure:
    """Represents a preflight check failure with a source and message.

    Attributes:
        source (str): Identifier of the component or check that failed.
        message (str): Human-readable description of the failure.
    """

    source: str
    message: str


class PreflightError(ConfigError):
    """Raised after every preflight check has run (§4.3). Exit 3."""

    def __init__(self, failures: list[PreflightFailure]) -> None:
        self.failures = list(failures)
        lines = [f"Preflight failed with {len(self.failures)} error(s):"]
        for f in self.failures:
            lines.append(f"  [{f.source}] {f.message}")
        super().__init__("\n".join(lines))


@dataclass
class ModsChange:
    """Diff between the source mod set and the current mods_dir (§4.11)."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        """Checks whether any changes are present."""
        return bool(self.added or self.updated or self.removed)

    @property
    def total(self) -> int:
        """Returns the total number of changes."""
        return len(self.added) + len(self.updated) + len(self.removed)

    def changed_paths(self) -> list[str]:
        """Returns the list of changed paths."""
        out = [f"mods/{f}" for f in self.added]
        out += [f"mods/{f}" for f in self.updated]
        out += [f"mods/{f}" for f in self.removed]
        return out


@dataclass
class InstanceServerChange:
    """Diff for one instance's config and kubejs trees (server scope)."""

    member: str
    changed_paths: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        """Checks whether any changes are present."""
        return bool(self.changed_paths)


@dataclass
class ResourcePackChange:
    """Resource-pack evaluation for one instance (§4.6.6)."""

    member: str
    properties_changes: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    publish_needed: bool = False
    source_sha1: str | None = None
    action: str = "none"

    @property
    def prompt_only(self) -> bool:
        """Returns True if only the resource-pack-prompt property changed and no publish is needed."""
        return set(self.properties_changes) == {"resource-pack-prompt"} and (not self.publish_needed)


@dataclass
class ReasonEntry:
    """One entry of §4.6.3's ``reasons`` list."""

    path_prefix: str
    action: str
    changed_paths: list[str]


@dataclass
class MemberPlan:
    """Preflight plan for a single partition member."""

    member: str
    container: str
    changed_paths: list[str] = field(default_factory=list)
    effective_action: str = "none"
    pack_required: bool = False
    reasons: list[ReasonEntry] = field(default_factory=list)
    server_action: str | None = None
    resource_pack_action: str | None = None
    resource_pack_target: dict[str, str] = field(default_factory=dict)
    resource_pack_publish: bool = False
    resource_pack_source: Path | None = None


@dataclass
class PreflightPlan:
    """Output of :func:`run_preflight`."""

    scopes: ScopeSet
    partition: list[str]
    member_plans: dict[str, MemberPlan]
    none_set: list[str]
    reload_set: list[str]
    restart_set: list[str]
    pack_required: bool
    container_states: dict[str, ContainerState]
    warnings: list[str] = field(default_factory=list)
    mods_change: ModsChange | None = None
    mods_dir: Path | None = None
    targeted: bool = False
    mods_drift: bool = False
    pack_required_warning: str | None = None


_ACTION_ORDER: dict[str, int] = {"none": 0, "none+pack": 1, "reload": 2, "reload+pack": 3, "restart": 4, "restart+pack": 5}


def _resolve_action(path: str, policy: dict[str, str]) -> tuple[str, str]:
    """Return ``(action, pattern)`` for a changed path.

    Matching rules (§4.6.1): longest literal prefix (substring before
    the first ``*``); ties broken by total pattern length (longer wins),
    then lexicographically (smaller wins). Unlisted paths default to
    ``"restart"`` and are attributed to the sentinel pattern
    ``"(default)"``.
    """
    best_pattern: str | None = None
    best_key: tuple[int, int, str] | None = None
    for pattern, _action in policy.items():
        literal = pattern.split("*", 1)[0]
        if not path.startswith(literal):
            continue
        key = (-len(literal), -len(pattern), pattern)
        if best_key is None or key < best_key:
            best_key = key
            best_pattern = pattern
    if best_pattern is None:
        return ("restart", "(default)")
    return (policy[best_pattern], best_pattern)


def _sticky_max(actions: list[str]) -> str:
    """Return the §4.6.2 sticky-max over ``actions``."""
    if not actions:
        return "none"
    return max(actions, key=lambda a: _ACTION_ORDER.get(a, -1))


def _is_pack_action(action: str) -> bool:
    return action.endswith("+pack")


def _resolve_paths_action(changed_paths: list[str], policy: dict[str, str]) -> tuple[str, list[ReasonEntry]]:
    """Compute the effective action and its contributing reasons (§4.6.3)."""
    if not changed_paths:
        return ("none", [])
    by_pattern: dict[str, list[str]] = {}
    pattern_action: dict[str, str] = {}
    for path in changed_paths:
        action, pattern = _resolve_action(path, policy)
        by_pattern.setdefault(pattern, []).append(path)
        pattern_action[pattern] = action
    effective = _sticky_max(list(pattern_action.values()))
    reasons: list[ReasonEntry] = []
    for pattern in sorted(by_pattern):
        if pattern_action[pattern] == effective:
            reasons.append(ReasonEntry(path_prefix=pattern, action=pattern_action[pattern], changed_paths=sorted(by_pattern[pattern])))
    return (effective, reasons)


def _hash_flat_dir(root: Path) -> dict[str, str]:
    """Return {filename: sha256} for the ``.jar`` files in a flat dir."""
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix != ".jar":
            continue
        try:
            out[entry.name] = compute_sha256(entry)
        except OSError:
            continue
    return out


def _hash_tree(root: Path) -> dict[str, str]:
    """Return {rel_path: sha256} for every file under a tree."""
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(root)
            rel_str = str(rel).replace(os.sep, "/")
            try:
                out[rel_str] = compute_sha256(full)
            except OSError:
                continue
    return out


def _is_unmarked(entry: dict) -> bool:
    """§6.3: an entry whose declared ``side`` is outside ``{client, server, both}`` is unmarked.

    ``side_raw is None`` means the ``.pw.toml`` had no ``side`` key at
    all, which the parser defaults to ``"both"`` - that is *marked*.
    Only an explicit out-of-set value counts as unmarked.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in ("client", "server", "both")


def _load_server_mod_entries(config: DeploymentConfig, logger: Any) -> list[dict]:
    """Load and filter the server-side mod entries, with closure (§6, §6.3).

    Pipeline, matching ``scope_server._resolve_mods_source``:

      1. Load every ``.pw.toml`` entry from the Prism index.
      2. Drop unmarked entries (§6.3) unless an override marks them.
      3. Apply overrides.
      4. Run the server side filter on the marked set.
      5. Expand the seed with the dependency closure.

    This is the "intent set" - the jars ``scope_server`` would place in
    ``mods_dir`` for a full server deploy. ``_compute_mods_change`` diffs
    the current ``mods_dir`` against it.
    """
    index_dir = config.modpack_dir / ".index"
    if not index_dir.is_dir():
        return []
    entries = deps.load_prism_index(index_dir)
    if not entries:
        return []
    overrides_path = config.config_dir / "side_overrides.toml"
    overrides = load_side_overrides(overrides_path)

    def _has_override(entry: dict) -> bool:
        mid = str(entry.get("id", ""))
        fname = str(entry.get("file", ""))
        if mid and mid in overrides.by_id:
            return True
        if fname and fname in overrides.by_filename:
            return True
        return bool(fname and fname in overrides.deployment_tool_review)

    marked = [e for e in entries if not _is_unmarked(e) or _has_override(e)]
    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)
    side_entries = deps.filter_prism_entries_by_side(marked, "server")
    result = deps.expand_with_required(all_entries=marked, seed_entries=side_entries, target_side="server", modpack_dir=config.modpack_dir, logger=logger)
    return result.entries


def _compute_mods_change(config: DeploymentConfig, mods_dir: Path | None, logger: Any) -> ModsChange | None:
    """Diff the server-side mod set against the current mods_dir.

    Returns None when ``mods_dir`` could not be determined (broken
    compose, no partition, no ``/data/mods`` bind on any member).
    Treats ``mods_dir`` as flat (§4.11).
    """
    if mods_dir is None:
        return None
    entries = _load_server_mod_entries(config, logger)
    src: dict[str, str] = {}
    for e in entries:
        filename = e.get("file")
        if not filename:
            continue
        path = config.modpack_dir / filename
        if not path.is_file():
            continue
        try:
            src[filename] = compute_sha256(path)
        except OSError:
            continue
    dst = _hash_flat_dir(mods_dir)
    added = sorted(set(src) - set(dst))
    removed = sorted(set(dst) - set(src))
    updated = sorted(f for f in set(src) & set(dst) if src[f] != dst[f])
    return ModsChange(added=added, updated=updated, removed=removed)


def _compute_instance_server_change(config: DeploymentConfig, member: str, logger: Any) -> InstanceServerChange:
    """Diff the sync-mapping sources against a member's config/kubejs.

    Only non-shared destinations are considered (``@www/...`` items are
    handled by the RP scope). The mapping key is the source directory
    under ``sync_root``; the mapping value (per-side) is the destination
    path relative to ``<instance_root>``.
    """
    inst = config.instances.get(member)
    change = InstanceServerChange(member=member)
    if inst is None or inst.instance_root is None:
        return change
    for key, mapping_value in config.sync_mapping.items():
        dest_rel = _resolve_mapping_value(mapping_value, "server")
        if dest_rel is None or is_shared_dest(dest_rel):
            continue
        src = config.sync_root / key
        dst = inst.instance_root / dest_rel
        if not src.is_dir():
            continue
        src_map = _hash_tree(src)
        dst_map = _hash_tree(dst)
        for rel in sorted(set(src_map) - set(dst_map)):
            change.added.append(f"{dest_rel}/{rel}")
        for rel in sorted(set(dst_map) - set(src_map)):
            change.removed.append(f"{dest_rel}/{rel}")
        for rel in sorted(set(src_map) & set(dst_map)):
            if src_map[rel] != dst_map[rel]:
                change.updated.append(f"{dest_rel}/{rel}")
    change.changed_paths = change.added + change.updated + change.removed
    return change


def _resolve_mapping_value(mapping_value: Any, side: str) -> str | None:
    """Resolve a per-side sync mapping value to a destination path.

    Thin wrapper over :func:`files.resolve_mapping_for_side` so the
    call site reads symmetrically with the rest of this module.
    """
    return resolve_mapping_for_side(mapping_value, side)


def _compute_resource_pack_change(config: DeploymentConfig, member: str, logger: Any) -> ResourcePackChange:
    """Compute server.properties target values and RP action (§4.6.6, §7.5).

    Only the four managed keys are compared. A key whose current value
    already matches the target is not a change.
    """
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
    diff = compute_diff(inst.server_properties_path, edits, logger)
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


def _classify_state(state: ContainerState, container: str) -> str | None:
    """Return a failure message if this state is fatal (§4.12), else None.

    The caller aggregates messages. ``restarting`` is handled by the
    bounded wait before this runs, so a ``restarting`` state reaching
    here is fatal.
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


def _check_rcon_available(
    config: DeploymentConfig, runtime: DockerRuntime, restart_set: list[str], container_states: dict[str, ContainerState], logger: Any
) -> list[PreflightFailure]:
    """§8.4: if any restart_set member is running, RCON must be selectable.

    A running member of restart_set will receive a restart notice, which
    requires an RCON transport. This is the "path requires RCON" trigger
    from §8.4; preflight fails with exit 3 if the secret is missing, the
    port isn't published, or the transport is ambiguous.

    Read-only: `select_rcon_transport` only inspects container state and
    reads the secret file. It never connects or sends a command.
    """
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
            out.append(PreflightFailure(f"§8.4 [{member}]", f"RCON required for restart notice but unavailable: {exc}"))
    return out


def run_preflight(
    config: DeploymentConfig, scopes: ScopeSet, with_resources: bool, notify: bool, dry_run: bool, runtime: DockerRuntime, logger: Any
) -> PreflightPlan:
    """Run every preflight check, aggregate failures, return the plan.

    Raises PreflightError (a ConfigError, exit 3) if any check fails.

    The execution order follows §4.1's sequence, with checks reordered
    so that failures which cascade (e.g. instance-root derivation needs
    compose) are only attempted when their prerequisites succeeded.
    """
    failures: list[PreflightFailure] = []
    warnings: list[str] = []
    if config.partition_unknown:
        failures.append(PreflightFailure("§2.5", "unknown --instance name(s): " + ", ".join(config.partition_unknown)))
    needs_instances = scopes.server or scopes.resource_pack
    if needs_instances and (not config.instances):
        failures.append(PreflightFailure("§2.5", "no instances configured"))
    compose_needed = scopes.server
    if scopes.resource_pack:
        any_rp_for_partition = any(member in config.resource_packs for member in config.partition)
        if any_rp_for_partition:
            compose_needed = True
    if compose_needed and (not config.compose.ok):
        failures.append(PreflightFailure("§3.5", f"compose is required for this scope but could not be loaded: {config.compose.error}"))
    if (scopes.client or scopes.resource_pack) and config.www_dir is None:
        failures.append(PreflightFailure("§3.19", f"www_dir could not be determined: {config.www_dir_error or 'unknown reason'}"))
    if scopes.client and config.www_dir is None and config.www_dir_candidates:
        for cand in config.www_dir_candidates:
            if logger is not None:
                logger.warning(f"www_dir candidate: {cand}")
    if _has_fatal(failures, "§3.5", "§2.5", "§3.19"):
        raise PreflightError(failures)
    needs_lifecycle = scopes.server
    if scopes.resource_pack:
        needs_lifecycle = True
    container_states: dict[str, ContainerState] = {}
    restarting: list[str] = []
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
                failures.append(PreflightFailure(f"§3.2 [{member}]", f"stop_grace_period parse failed: {inst.stop_grace_parse_error}"))
            if inst.service is None or inst.instance_root is None:
                failures.append(PreflightFailure(f"§3.6 [{member}]", f"no /data bind found for container {inst.container!r}"))
                continue
            if not inst.service.has_healthcheck:
                failures.append(PreflightFailure(f"§3.8 [{member}]", f"compose service {inst.service.name!r} has no healthcheck"))
            if scopes.server:
                m = derive_mods_dir(inst.service)
                if m is not None:
                    resolved = resolve_compose_path(m, config.compose.file.base_dir)
                    mods_dir_candidates.append((member, resolved))
        targeted = config.requested_instances is not None
        derived_mods_dir: Path | None = None
        if scopes.server and (not targeted) and config.partition:
            if len(mods_dir_candidates) != len(config.partition):
                missing = [m for m in config.partition if m not in {n for n, _ in mods_dir_candidates}]
                failures.append(PreflightFailure("§3.7", "partition member(s) missing /data/mods bind: " + ", ".join(missing)))
            else:
                sources = {p for _n, p in mods_dir_candidates}
                if len(sources) != 1:
                    failures.append(
                        PreflightFailure("§3.7", "mods_dir bind sources disagree across partition members: " + ", ".join(str(p) for p in sorted(sources)))
                    )
                else:
                    derived_mods_dir = next(iter(sources))
                    if config.mods_dir_toml is not None and config.mods_dir_toml != derived_mods_dir and (logger is not None):
                        logger.warning(f"mods_dir: TOML={config.mods_dir_toml} compose={derived_mods_dir} (compose wins)")
        if needs_lifecycle:
            runtime.ping()
            for member in config.partition:
                inst = config.instances.get(member)
                if inst is None or inst.service is None:
                    continue
                try:
                    state = runtime.inspect(inst.container)
                except Exception as exc:
                    failures.append(PreflightFailure(f"§4.12 [{member}]", f"inspect failed: {exc}"))
                    continue
                container_states[member] = state
                if state.status == "restarting":
                    restarting.append(member)
        if restarting:
            wait = config.docker.preflight_restarting_wait_seconds
            settled = runtime.wait_for_restarting_settle(
                [config.instances[m].container for m in restarting], total_timeout=wait, poll_interval=config.docker.health_poll_seconds
            )
            for member in restarting:
                inst = config.instances.get(member)
                if inst is None:
                    continue
                new_state = settled.get(inst.container)
                if new_state is not None:
                    container_states[member] = new_state
        for member, state in container_states.items():
            inst = config.instances.get(member)
            if inst is None:
                continue
            msg = _classify_state(state, inst.container)
            if msg is not None:
                failures.append(PreflightFailure(f"§4.12 [{member}]", msg))
            elif state.running and state.health == "unhealthy" and (logger is not None):
                logger.warning(f"[{member}] container {inst.container} is unhealthy at preflight")
            elif state.running and state.health == "starting" and (logger is not None):
                logger.info(f"[{member}] container {inst.container} is starting")
        touched_mods = scopes.server and (not targeted)
        for member, _state in container_states.items():
            inst = config.instances.get(member)
            if inst is None or inst.instance_root is None:
                continue
            expected: list[tuple[Path, str]] = [(inst.instance_root, "/data")]
            if touched_mods and derived_mods_dir is not None:
                expected.append((derived_mods_dir, "/data/mods"))
            try:
                check_mount_drift(runtime, inst.container, expected)
            except ConfigError as exc:
                failures.append(PreflightFailure(f"§3.17 [{member}]", str(exc)))
    if scopes.resource_pack:
        resourcepacks = config.sync_mapping.get("resourcepacks") or {}
        client_sub = resourcepacks.get("client") if isinstance(resourcepacks, dict) else None
        if not isinstance(client_sub, str) or not client_sub:
            if any(member in config.resource_packs for member in config.partition):
                failures.append(PreflightFailure("§7.7", "[sync_mapping].resourcepacks.client is required when at least one pack is configured"))
        else:
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
    if notify:
        template_failures = notifications.validate_live_and_failure(config.discord, notify=notify, dry_run=dry_run, has_scope=scopes.any(), logger=logger)
        failures.extend((PreflightFailure(src, msg) for src, msg in template_failures))
    if failures:
        raise PreflightError(failures)
    targeted = config.requested_instances is not None
    mods_change: ModsChange | None = None
    mods_dir: Path | None = None
    if scopes.server:
        if config.compose.ok and config.partition:
            for member in config.partition:
                inst = config.instances.get(member)
                if inst is None or inst.service is None:
                    continue
                m = derive_mods_dir(inst.service)
                if m is not None:
                    mods_dir = resolve_compose_path(m, config.compose.file.base_dir)
                    break
        mods_change = _compute_mods_change(config, mods_dir, logger)
    instance_changes: dict[str, InstanceServerChange] = {}
    if scopes.server:
        for member in config.partition:
            instance_changes[member] = _compute_instance_server_change(config, member, logger)
    rp_changes: dict[str, ResourcePackChange] = {}
    if scopes.resource_pack:
        for member in config.partition:
            if member in config.resource_packs:
                rp_changes[member] = _compute_resource_pack_change(config, member, logger)
    mods_drift = False
    if scopes.server and targeted and (mods_change is not None) and mods_change.any:
        mods_drift = True
        warnings.append("mods_dir differs from the full source set; a targeted deploy does not touch shared mods. Run a non-targeted --server to update mods.")
    member_plans: dict[str, MemberPlan] = {}
    policy = config.restart_policy
    for member in config.partition:
        inst = config.instances.get(member)
        container_name = inst.container if inst else member
        plan = MemberPlan(member=member, container=container_name)
        if scopes.server:
            paths: list[str] = []
            sc = instance_changes.get(member)
            if sc is not None:
                paths.extend(sc.changed_paths)
            if not targeted and mods_change is not None:
                paths.extend(mods_change.changed_paths())
            plan.changed_paths.extend(paths)
            effective, reasons = _resolve_paths_action(paths, policy)
            plan.server_action = effective if paths else "none"
            plan.reasons.extend(reasons)
        rp = rp_changes.get(member)
        if rp is not None:
            plan.resource_pack_action = rp.action
            plan.resource_pack_publish = rp.publish_needed
            if rp.source_sha1 is not None and hasattr(rp, "source_zip"):
                plan.resource_pack_source = rp.source_zip
            if config.compose.ok:
                resourcepacks = config.sync_mapping.get("resourcepacks") or {}
                dest_value = resourcepacks.get("resource_pack") if isinstance(resourcepacks, dict) else None
                rpc = config.resource_packs.get(member)
                if isinstance(dest_value, str) and dest_value and (rpc is not None) and (rp.source_sha1 is not None):
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
        plan.effective_action = _sticky_max(candidates)
        plan.pack_required = _is_pack_action(plan.effective_action)
        if plan.resource_pack_action == plan.effective_action and plan.resource_pack_action != "none" and (rp is not None):
            changed = sorted(rp.properties_changes)
            plan.reasons.append(ReasonEntry(path_prefix="resource-pack", action=plan.resource_pack_action, changed_paths=changed))
        member_plans[member] = plan
    none_set: list[str] = []
    reload_set: list[str] = []
    restart_set: list[str] = []
    for member in config.partition:
        plan = member_plans[member]
        if plan.effective_action in ("none", "none+pack"):
            none_set.append(member)
        elif plan.effective_action in ("reload", "reload+pack"):
            reload_set.append(member)
        else:
            restart_set.append(member)
    if needs_lifecycle and restart_set and (not dry_run):
        rcon_failures = _check_rcon_available(config, runtime, restart_set, container_states, logger)
        if rcon_failures:
            raise PreflightError(rcon_failures)
    pack_required = any(p.pack_required for p in member_plans.values())
    pack_required_warning: str | None = None
    if pack_required and (not scopes.client):
        pack_required_warning = "client pack content changed; the current ZIP is stale. Run --client to rebuild."
        warnings.append(pack_required_warning)
    if notify and (not dry_run) and restart_set:
        any_running = any(container_states.get(m) is not None and container_states[m].is_running for m in restart_set)
        if any_running:
            online_failures = notifications.validate_online(config.discord, logger=logger)
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


def _has_fatal(failures: list[PreflightFailure], *sources: str) -> bool:
    """True if any failure came from one of the given sources."""
    wanted = set(sources)
    return any(f.source in wanted for f in failures)
