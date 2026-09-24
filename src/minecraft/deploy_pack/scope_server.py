# src/minecraft/deploy_pack/scope_server.py

"""Server scope: write phase (Project_Specs.md §4.2, §4.7, §4.11, §6.3, §7.2, §9.2).

Responsibilities:
  * deploy shared mods into ``mods_dir`` (skip when targeted, §2.9)
  * deploy per-instance ``config`` and ``kubejs`` per §7.2
  * deploy any other non-shared sync-mapping key with merge semantics
  * collect a structured result so the caller can apply §4.7's failure
    handling (which needs to know what was and wasn't written)

Non-responsibilities:
  * §4.7's failure handling itself. That decision requires knowledge of
    which containers were stopped, which is main's concern.
  * Lifecycle (§8). hooks.py.
  * Client ZIP assembly. scope_client.
  * Resource-pack publication. scope_resource_pack.
  * Building the deploy plan. preflight.

Write ordering (§4.2, "halt on first runtime failure"):

    1. mods_dir (shared, once)
    2. per-instance config/kubejs, in partition order

If a step fails, subsequent steps do not run. Completed steps are not
rolled back. The result carries enough information for the caller to
distinguish "mods failed" from "member X's config failed."

Mods are computed from the Prism index, not from a directory. The
source-set logic mirrors preflight's and scope_client's: overrides are
applied, unmarked entries (§6.3) are dropped unless an override marks
them, the server side filter runs, and the dependency closure expands
the seed.

Unmarked handling (§6.3)
------------------------

An entry is unmarked when its ``.pw.toml`` declares a ``side`` outside
``{client, server, both}``. The parser coerces such values to ``"both"``
and preserves the original in ``side_raw``, so the filter cannot rely on
the coerced field. Entries whose ``side_raw`` is ``None`` (no ``side``
key present) are treated as marked, matching the parser default.

An unmarked entry is dropped from the deploy set unless an override
(``by_id``, ``by_filename``, or ``deployment_tool_review``) marks it.
Overrides are applied *after* the unmarked filter, so the override's
value is what the side filter sees - an unmarked entry overridden to
``"skipped"`` is still excluded from the server side, and one overridden
to ``"both"`` or ``"server"`` is included.

Same limitation as scope_client and deps.py: because unmarked entries
are dropped before the closure runs, an unmarked jar that some marked
jar requires via ``mandatory=true`` cannot be pulled in by the closure.
The fix is to add a ``.pw.toml`` side declaration or an override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import deps
from .config_model import DeploymentConfig, InstanceConfig
from .files import CopyResult, copy_tree, deploy_flat_files, is_shared_dest, resolve_mapping_for_side
from .overrides import apply_side_overrides, load_side_overrides
from .preflight import PreflightPlan

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
            list[CopyResult]: All copy results, including the config result, kubejs result, and other results.
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

    On failure, ``success`` is False, ``failure_phase`` identifies the
    stage that failed (``"mods"`` or ``"config"`` or the sync-mapping
    key), and ``failure_member`` is set when the failure was per-member.
    ``member_results`` contains only members processed before the halt.
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
    """§6.3: an entry whose declared ``side`` is outside ``{client, server, both}`` is unmarked.

    ``side_raw is None`` means the ``.pw.toml`` had no ``side`` key at
    all, which the parser defaults to ``"both"`` - that is *marked*.
    Only an explicit out-of-set value counts as unmarked.
    """
    raw = entry.get("side_raw")
    if raw is None:
        return False
    return raw not in ("client", "server", "both")


def _resolve_mods_source(config: DeploymentConfig, logger: Any) -> dict[str, Path]:
    """Return ``{filename: source_path}`` for the server-side mod set.

    Pipeline (§3.11, §6.3, §6):

      1. Load every ``.pw.toml`` entry from the Prism index.
      2. Drop unmarked entries (§6.3) - those whose ``side_raw`` is an
         explicit value outside ``{client, server, both}`` - unless an
         override in any section marks them.
      3. Apply overrides. An override's value replaces ``side`` and is
         what the subsequent filter sees.
      4. Run the server side filter on the marked set.
      5. Expand the seed with the dependency closure.

    Files missing from disk are logged and skipped - preflight's mods
    change computation already ignores them, and there is no download
    step in v3.0.

    An empty index or a missing index directory returns ``{}``.
    """
    index_dir = config.modpack_dir / ".index"
    if not index_dir.is_dir():
        return {}
    entries = deps.load_prism_index(index_dir)
    if not entries:
        return {}
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
    closure = deps.expand_with_required(all_entries=marked, seed_entries=side_entries, target_side="server", modpack_dir=config.modpack_dir, logger=logger)
    out: dict[str, Path] = {}
    for entry in closure.entries:
        filename = entry.get("file")
        if not filename:
            continue
        path = config.modpack_dir / filename
        if not path.is_file():
            if logger is not None:
                logger.warning(f"mod source missing from disk, skipping: {filename}")
            continue
        out[str(filename)] = path
    return out


def _mode_for_key(key: str, inst: InstanceConfig) -> str:
    """Return the §7.2 clean mode for a sync-mapping key on this instance.

    ``config`` → the instance's ``config_mode``.
    ``kubejs`` → the instance's ``kubejs_mode`` (always ``"delete"``).
    Anything else → ``"merge"``.

    The spec defines modes only for config and kubejs. Other mapping
    keys are user extensions; merge is the conservative default because
    it never removes content that isn't in the source.
    """
    if key == "config":
        return inst.config_mode
    if key == "kubejs":
        return inst.kubejs_mode
    return "merge"


def _deploy_member(config: DeploymentConfig, inst: InstanceConfig, member_result: MemberWriteResult, protect_patterns: list[str], logger: Any) -> None:
    """Deploy every non-shared sync-mapping key for one member.

    Shared destinations (``@www/...``) are skipped; the client scope
    publishes those. Keys whose source directory is absent are skipped.
    A key whose mode is misconfigured is a caller bug - the config
    layer validates ``config_mode`` and ``kubejs_mode`` at load.
    """
    assert inst.instance_root is not None
    for key, mapping_value in config.sync_mapping.items():
        dest_rel = resolve_mapping_for_side(mapping_value, "server")
        if dest_rel is None:
            continue
        if is_shared_dest(dest_rel):
            if logger is not None:
                logger.debug(f"[{inst.name}] {key}: shared dest {dest_rel!r} handled by client scope")
            continue
        if dest_rel.startswith("@"):
            raise ValueError(f"[{inst.name}] {key}: unsupported @-prefixed destination {dest_rel!r}")
        src = config.sync_root / key
        if not src.is_dir():
            if logger is not None:
                logger.debug(f"[{inst.name}] {key}: source directory absent ({src}); skipping")
            continue
        dst = inst.instance_root / dest_rel
        mode = _mode_for_key(key, inst)
        result = copy_tree(src, dst, mode=mode, protect_patterns=protect_patterns, logger=logger)
        if key == "config":
            member_result.config_result = result
        elif key == "kubejs":
            member_result.kubejs_result = result
        else:
            member_result.other_results[key] = result


def deploy_server_scope(config: DeploymentConfig, plan: PreflightPlan, protect_patterns: list[str], logger: Any = None) -> ServerScopeResult:
    """Execute the server scope write phase (§4.2, §7.2).

    ``protect_patterns`` is loaded by the caller once via
    ``files.load_protect_patterns`` - main owns that read so it can be
    logged once and shared across scopes.

    Halts on first runtime failure (§4.2). Returns a result describing
    what was written and, on failure, what was and wasn't attempted.
    Never raises on a write failure; the caller decides the exit code
    and whether recovery applies (§4.7).

    Read-only with respect to Docker: no container operations happen
    here.
    """
    result = ServerScopeResult()
    if plan.targeted:
        result.mods_skipped_reason = "targeted deploy (--instance) does not touch mods_dir"
    elif plan.mods_dir is None:
        result.mods_skipped_reason = "mods_dir could not be determined"
    else:
        try:
            src_map = _resolve_mods_source(config, logger)
            result.mods_dir = plan.mods_dir
            result.mods_result = deploy_flat_files(src_files=src_map, dst_dir=plan.mods_dir, protect_patterns=protect_patterns, logger=logger)
        except Exception as exc:
            result.success = False
            result.failure_phase = "mods"
            result.failure_message = str(exc)
            return result
    for member in config.partition:
        inst = config.instances.get(member)
        if inst is None:
            continue
        if inst.instance_root is None:
            continue
        member_result = MemberWriteResult(member=member)
        try:
            _deploy_member(config, inst, member_result, protect_patterns, logger)
        except Exception as exc:
            result.success = False
            result.failure_phase = "config"
            result.failure_member = member
            result.failure_message = str(exc)
            result.member_results[member] = member_result
            return result
        result.member_results[member] = member_result
    return result
