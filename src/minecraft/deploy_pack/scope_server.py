# src/minecraft/deploy_pack/scope_server.py

"""Server scope: write phase (Project_Specs.md Section 4.2, Section 4.7, Section 4.11, Section 6.3, Section 7.2, Section 9.2).

Responsibilities:
  * deploy shared mods into ``mods_dir`` (skip when targeted, Section 2.9)
  * deploy per-instance ``config`` and ``kubejs`` per Section 7.2
  * deploy any other non-shared sync-mapping key with merge semantics
  * collect a structured result so the caller can apply Section 4.7's failure
    handling (which needs to know what was and wasn't written)

Non-responsibilities:
  * Section 4.7's failure handling itself. That decision requires knowledge of
    which containers were stopped, which is main's concern.
  * Lifecycle (Section 8). hooks.py.
  * Client ZIP assembly. scope_client.
  * Resource-pack publication. scope_resource_pack.
  * Building the deploy plan. preflight.

Write ordering (Section 4.2, "halt on first runtime failure"):

    1. mods_dir (shared, once)
    2. per-instance config/kubejs, in partition order

If a step fails, subsequent steps do not run. Completed steps are not
rolled back. The result carries enough information for the caller to
distinguish "mods failed" from "member X's config failed."

Mods are computed from the Prism index, not from a directory. The
source-set logic mirrors preflight's and scope_client's: overrides are
applied, unmarked entries (Section 6.3) are dropped unless an override marks
them, the server side filter runs, and the dependency closure expands
the seed.

Slip mode (spec v1.3 Section 5.8, Section 14.4)
---------------------------------

When ``plan.slip_context`` is not ``None``, the module dispatches to
``_deploy_server_scope_slip``. In slip mode:

  * Mods come from ``slip.jar_sources``, filtered by the slip-declared
    per-jar side (``server`` or ``both``). The Prism index and
    side-overrides file are NOT consulted (Section 14.4).
  * Config and KubeJS come from ``slip.context.{config,kubejs}_source``,
    deployed into each instance's ``config/`` and ``kubejs/`` using the
    slip's per-instance ``config_mode``, ``kubejs_mode``,
    ``config_deletions``, and ``protect_patterns``.
  * ``config_deletions`` is applied as an explicit remove pass *before*
    ``copy_tree`` and honors ``protect_patterns``.

Unmarked handling (Section 6.3)
------------------------

An entry is unmarked when its ``.pw.toml`` declares a ``side`` outside
``{client, server, both}``. The parser coerces such values to ``"both"``
and preserves the original in ``side_raw``, so the filter cannot rely on
the coerced field. Entries whose ``side_raw`` is ``None`` (no ``side``
key present) are treated as marked, matching the parser default.

An unmarked entry is dropped from the deploy set unless an override
(``by_id``, ``by_filename``, or ``deployment_tool_review``) marks it.
Overrides are applied *after* the unmarked filter, so the override's
value is what the side filter sees.

Logging
-------

Entry and each step log at DEBUG with the inputs. Per-key decisions
(skipped shared destinations, missing sources, protected files kept)
log at DEBUG. Changes and skipped-mods summary log at INFO. Warnings
that the operator needs to act on (missing source file, unprotected
skip of an unmarked jar) log at WARN. Failures log at ERROR with the
failing phase and member. The module logger is
``minecraft.deploy_pack.scope_server``; callers may inject an override
via ``logger=`` for a single call.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import deps
from .config_model import DeploymentConfig, InstanceConfig
from .files import (
    CopyResult,
    copy_tree,
    deploy_flat_files,
    is_protected_path,
    is_shared_dest,
    resolve_mapping_for_side,
)
from .logging_setup import get_logger
from .overrides import apply_side_overrides, load_side_overrides
from .preflight import PreflightPlan

if TYPE_CHECKING:
    from minecraft.modpack_upgrade.slip_context import SlipContext

_log = get_logger(__name__)

__all__ = ["MemberWriteResult", "ServerScopeResult", "deploy_server_scope"]


@dataclass
class MemberWriteResult:
    """Per-member write outcome for one partition member."""

    member: str
    config_result: CopyResult | None = None
    kubejs_result: CopyResult | None = None
    other_results: dict[str, CopyResult] = field(default_factory=dict)

    def all_results(self) -> list[CopyResult]:
        """Collects all copy results.

        Returns:
            list[CopyResult]: All copy results, including the config
            result, kubejs result, and other results.
        """
        out: list[CopyResult] = []
        if self.config_result is not None:
            out.append(self.config_result)
        if self.kubejs_result is not None:
            out.append(self.kubejs_result)
        out.extend(self.other_results.values())
        return out


@dataclass
class ServerScopeResult:
    """Outcome of the server scope write phase.

    On failure, ``success`` is False, ``failure_phase`` identifies the stage that failed (``"mods"`` or ``"config"`` or the sync-mapping key), and
    ``failure_member`` is set when the failure was per-member. ``member_results`` contains only members processed before the halt.
    """

    success: bool = True
    failure_message: str | None = None
    failure_phase: str | None = None
    failure_member: str | None = None
    mods_dir: Path | None = None
    mods_result: CopyResult | None = None
    mods_skipped_reason: str | None = None
    member_results: dict[str, MemberWriteResult] = field(default_factory=dict)

    @property
    def protected_kept(self) -> list[str]:
        """Collects the protected entries that were kept.

        Returns:
            list[str]: The protected entries that were kept.
        """
        out: list[str] = []
        if self.mods_result is not None:
            out.extend(self.mods_result.protected_kept)
        for r in self.member_results.values():
            for cr in r.all_results():
                out.extend(cr.protected_kept)
        return out

    def changed_count(self) -> int:
        """Counts the total number of changed entries.

        Returns:
            int: The total number of changed entries.
        """
        n = 0
        if self.mods_result is not None:
            n += self.mods_result.changed_count
        for r in self.member_results.values():
            for cr in r.all_results():
                n += cr.changed_count
        return n


def _is_unmarked(entry: dict) -> bool:
    """Section 6.3: an entry whose declared ``side`` is outside ``{client, server, both}`` is unmarked.

    ``side_raw is None`` means the ``.pw.toml`` had no ``side`` key at
    all, which the parser defaults to ``"both"`` - that is *marked*.
    Only an explicit out-of-set value counts as unmarked.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in ("client", "server", "both")


def _resolve_mods_source(
    config: DeploymentConfig,
    logger: Any,
) -> dict[str, Path]:
    """Return ``{filename: source_path}`` for the server-side mod set.

    Pipeline (Section 3.11, Section 6.3, Section 6):

      1. Load every ``.pw.toml`` entry from the Prism index.
      2. Drop unmarked entries (Section 6.3) unless an override marks them.
      3. Apply overrides. An override's value replaces ``side`` and is
         what the subsequent filter sees.
      4. Run the server side filter on the marked set.
      5. Expand the seed with the dependency closure.

    Files missing from disk are logged at WARN and skipped.

    An empty index or a missing index directory returns ``{}``.
    """
    index_dir = config.modpack_dir / ".index"
    if not index_dir.is_dir():
        logger.debug(f"_resolve_mods_source: index dir missing: {index_dir}")
        return {}
    entries = deps.load_prism_index(index_dir)
    if not entries:
        logger.debug(f"_resolve_mods_source: no Prism entries under {index_dir}")
        return {}

    overrides_path = config.config_dir / "side_overrides.toml"
    overrides = load_side_overrides(overrides_path)

    marked = [e for e in entries if not _is_unmarked(e) or overrides.matches(e)]
    dropped_unmarked = len(entries) - len(marked)
    logger.debug(f"_resolve_mods_source: {len(entries)} entr(ies); {dropped_unmarked} dropped as unmarked (no override)")

    if not overrides.is_empty():
        marked = apply_side_overrides(marked, overrides)
        logger.debug("_resolve_mods_source: side overrides applied")

    side_entries = deps.filter_prism_entries_by_side(marked, "server")
    logger.debug(f"_resolve_mods_source: server side filter -> {len(side_entries)} seed entr(ies)")

    closure = deps.expand_with_required(
        all_entries=marked,
        seed_entries=side_entries,
        target_side="server",
        modpack_dir=config.modpack_dir,
        logger=logger,
    )

    out: dict[str, Path] = {}
    for entry in closure.entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = config.modpack_dir / filename
        if not path.is_file():
            logger.warning(f"mod source missing from disk, skipping: {filename}")
            continue
        out[str(filename)] = path

    logger.info(f"_resolve_mods_source: {len(out)} server-side mod source(s) ({len(closure.entries)} in closure)")
    return out


def _resolve_mods_source_from_context(ctx: SlipContext, logger: Any) -> dict[str, Path]:
    """Return ``{filename: path}`` for slip jars with side ∈ {server, both}.

    Spec v1.3 Section 14.4: in slip mode the slip-derived source set is the
    authority. The Prism index and side-overrides file are not consulted.
    """
    out: dict[str, Path] = {}
    for jar in ctx.slip.jars:
        if jar.side not in ("server", "both"):
            continue
        path = ctx.jar_sources.get(jar.filename)
        if path is None:
            logger.warning(f"_resolve_mods_source_from_context: slip jar {jar.filename!r} not in jar_sources")
            continue
        out[jar.filename] = path
    logger.info(f"_resolve_mods_source_from_context: {len(out)} server-side jar(s) from {len(ctx.slip.jars)} slip jar(s)")
    return out


def _mode_for_key(key: str, inst: InstanceConfig) -> str:
    """Return the Section 7.2 clean mode for a sync-mapping key on this instance.

    ``config`` -> the instance's ``config_mode``. ``kubejs`` -> the instance's ``kubejs_mode`` (always ``"delete"``). Anything else ->
    ``"merge"``.
    """
    if key == "config":
        return inst.config_mode
    if key == "kubejs":
        return inst.kubejs_mode
    return "merge"


def _deploy_member(
    config: DeploymentConfig,
    inst: InstanceConfig,
    member_result: MemberWriteResult,
    protect_patterns: list[str],
    logger: Any,
) -> None:
    """Deploy every non-shared sync-mapping key for one member.

    Shared destinations (``@www/...``) are skipped; the client scope publishes those. Keys whose source directory is absent are skipped.
    """
    assert inst.instance_root is not None
    logger.debug(f"[{inst.name}] deploying sync-mapping keys to {inst.instance_root}")

    for key, mapping_value in config.sync_mapping.items():
        dest_rel = resolve_mapping_for_side(mapping_value, "server")
        if dest_rel is None:
            logger.debug(f"[{inst.name}] {key}: excluded on server side; skipping")
            continue
        if is_shared_dest(dest_rel):
            logger.debug(f"[{inst.name}] {key}: shared dest {dest_rel!r} handled by client scope")
            continue
        if dest_rel.startswith("@"):
            raise ValueError(f"[{inst.name}] {key}: unsupported @-prefixed destination {dest_rel!r}")

        src = config.sync_root / key
        if not src.is_dir():
            logger.debug(f"[{inst.name}] {key}: source directory absent ({src}); skipping")
            continue

        dst = inst.instance_root / dest_rel
        mode = _mode_for_key(key, inst)
        logger.debug(f"[{inst.name}] {key}: copy_tree mode={mode} {src} -> {dst}")
        result = copy_tree(src, dst, mode=mode, protect_patterns=protect_patterns, logger=logger)

        if result.any_change:
            logger.info(f"[{inst.name}] {key}: {result.changed_count} change(s) (+{len(result.added)} ~{len(result.updated)} -{len(result.removed)})")
        else:
            logger.debug(f"[{inst.name}] {key}: no changes")

        if result.protected_kept:
            logger.debug(f"[{inst.name}] {key}: {len(result.protected_kept)} protected file(s) kept")

        if key == "config":
            member_result.config_result = result
        elif key == "kubejs":
            member_result.kubejs_result = result
        else:
            member_result.other_results[key] = result


def _apply_config_deletions(
    root: Path,
    deletions: tuple[str, ...],
    protect: tuple[str, ...],
    logger: Any,
) -> None:
    """Explicitly remove ``deletions`` under ``root``, honoring ``protect``.

    Used in slip mode's ``config_mode = "merge"`` path: the slip declares
    an explicit deletion list that must be applied even though merge mode
    otherwise retains target-only files. A protected entry is skipped
    with a warning (spec v1.3 Section 5.3 rule 11).
    """
    if not deletions:
        return
    if not root.is_dir():
        logger.debug(f"_apply_config_deletions: {root} is not a directory; skipping")
        return
    for rel in deletions:
        target = root / rel
        if not target.exists():
            logger.debug(f"_apply_config_deletions: {rel} absent under {root}; skipping")
            continue
        if is_protected_path(rel, list(protect)):
            logger.warning(f"_apply_config_deletions: {rel!r} is protected; skipping delete")
            continue
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                logger.warning(f"_apply_config_deletions: {rel!r} is not a file or directory; skipping")
                continue
            logger.info(f"_apply_config_deletions: deleted {rel}")
        except OSError as exc:
            logger.warning(f"_apply_config_deletions: could not delete {rel}: {exc}")


def _deploy_member_from_context(
    inst: InstanceConfig,
    member_result: MemberWriteResult,
    ctx: SlipContext,
    logger: Any,
) -> None:
    """Slip-mode per-member deploy: config and kubejs from the context sources.

    Source roots come from ``ctx.config_source`` and ``ctx.kubejs_source``.
    Modes, deletions, and protections come from the slip's per-instance
    spec (spec v1.3 Section 5.3 rule 11).
    """
    assert inst.instance_root is not None
    slip_inst = ctx.slip.instances.get(inst.name)
    if slip_inst is None:
        logger.warning(f"scope_server: [{inst.name}] not in slip.instances; MUST NOT deploy (spec v1.3 Section 5.3 rule 10)")
        return

    config_dst = inst.instance_root / "config"
    _apply_config_deletions(config_dst, slip_inst.config_deletions, slip_inst.protect_patterns, logger)
    logger.debug(f"[{inst.name}] slip config: mode={slip_inst.config_mode} {ctx.config_source} -> {config_dst}")
    config_result = copy_tree(
        ctx.config_source,
        config_dst,
        mode=slip_inst.config_mode,
        protect_patterns=list(slip_inst.protect_patterns),
        logger=logger,
    )
    member_result.config_result = config_result
    if config_result.any_change:
        logger.info(
            f"[{inst.name}] config: {config_result.changed_count} change(s) "
            f"(+{len(config_result.added)} ~{len(config_result.updated)} "
            f"-{len(config_result.removed)})"
        )

    kubejs_dst = inst.instance_root / "kubejs"
    logger.debug(f"[{inst.name}] slip kubejs: mode={slip_inst.kubejs_mode} {ctx.kubejs_source} -> {kubejs_dst}")
    kubejs_result = copy_tree(
        ctx.kubejs_source,
        kubejs_dst,
        mode=slip_inst.kubejs_mode,
        protect_patterns=list(slip_inst.protect_patterns),
        logger=logger,
    )
    member_result.kubejs_result = kubejs_result
    if kubejs_result.any_change:
        logger.info(
            f"[{inst.name}] kubejs: {kubejs_result.changed_count} change(s) "
            f"(+{len(kubejs_result.added)} ~{len(kubejs_result.updated)} "
            f"-{len(kubejs_result.removed)})"
        )


def _deploy_mods_from_source(
    source_map: dict[str, Path],
    plan: PreflightPlan,
    protect_patterns: list[str],
    result: ServerScopeResult,
    logger: Any,
) -> bool:
    """Shared mods deployment body. Returns True on success, False on failure.

    Both legacy and slip paths funnel through this so the failure shape is
    identical.
    """
    assert plan.mods_dir is not None
    try:
        logger.debug(f"server scope: mods_dir={plan.mods_dir} with {len(source_map)} source(s)")
        result.mods_dir = plan.mods_dir
        result.mods_result = deploy_flat_files(
            src_files=source_map,
            dst_dir=plan.mods_dir,
            protect_patterns=protect_patterns,
            logger=logger,
        )
        logger.info(f"server scope: mods_dir deploy complete; {result.mods_result.changed_count} change(s)")
        return True
    except Exception as exc:
        result.success = False
        result.failure_phase = "mods"
        result.failure_message = str(exc)
        logger.error(f"server scope: mods deploy failed: {exc}")
        return False


def _deploy_members(
    config: DeploymentConfig,
    plan: PreflightPlan,
    protect_patterns: list[str],
    result: ServerScopeResult,
    logger: Any,
) -> int | None:
    """Walk the partition, deploying each member. Returns an exit-style code or None.

    Uses the legacy ``_deploy_member`` when ``plan.slip_context`` is None,
    otherwise ``_deploy_member_from_context``. Both share the outer
    error-handling shape.
    """
    for member in config.partition:
        inst = config.instances.get(member)
        if inst is None:
            logger.warning(f"server scope: partition member {member!r} not in config.instances; skipping")
            continue
        if inst.instance_root is None:
            logger.warning(f"server scope: [{member}] has no instance_root; skipping member writes")
            continue

        logger.debug(f"server scope: deploying member {member}")
        member_result = MemberWriteResult(member=member)
        try:
            if plan.slip_context is not None:
                _deploy_member_from_context(inst, member_result, plan.slip_context, logger)
            else:
                _deploy_member(config, inst, member_result, protect_patterns, logger)
        except Exception as exc:
            result.success = False
            result.failure_phase = "config"
            result.failure_member = member
            result.failure_message = str(exc)
            result.member_results[member] = member_result
            logger.error(f"server scope: member {member} deploy failed: {exc}")
            return None
        result.member_results[member] = member_result
    return 0


def deploy_server_scope(
    config: DeploymentConfig,
    plan: PreflightPlan,
    protect_patterns: list[str],
    logger: Any = None,
) -> ServerScopeResult:
    """Execute the server scope write phase (Section 4.2, Section 7.2).

    ``protect_patterns`` is loaded by the caller once via
    ``files.load_protect_patterns`` - main owns that read so it can be
    logged once and shared across scopes. In slip mode, ``protect_patterns``
    is ignored for config/kubejs: the slip's per-instance
    ``protect_patterns`` take priority (spec v1.3 Section 5.3 rule 11). It is still
    used for the shared mods directory, which no slip entry governs.

    Halts on first runtime failure (Section 4.2). Returns a result describing
    what was written and, on failure, what was and wasn't attempted.
    Never raises on a write failure.

    Read-only with respect to Docker: no container operations happen
    here.
    """
    if logger is None:
        logger = _log
    mode = "slip" if plan.slip_context is not None else "legacy"
    logger.info(f"server scope: partition={list(config.partition)} targeted={plan.targeted} mods_dir={plan.mods_dir} mode={mode}")

    result = ServerScopeResult()

    if plan.targeted:
        result.mods_skipped_reason = "targeted deploy (--instance) does not touch mods_dir"
        logger.info(f"server scope: {result.mods_skipped_reason}")
    elif plan.mods_dir is None:
        result.mods_skipped_reason = "mods_dir could not be determined"
        logger.warning(f"server scope: {result.mods_skipped_reason}")
    else:
        if plan.slip_context is not None:
            source_map = _resolve_mods_source_from_context(plan.slip_context, logger)
        else:
            source_map = _resolve_mods_source(config, logger)
        if not _deploy_mods_from_source(source_map, plan, protect_patterns, result, logger):
            return result

    _deploy_members(config, plan, protect_patterns, result, logger)

    if result.success:
        logger.info(f"server scope: complete; {result.changed_count()} change(s) across {len(result.member_results)} member(s)")
        if result.protected_kept:
            logger.debug(f"server scope: {len(result.protected_kept)} protected file(s) preserved")
    return result
