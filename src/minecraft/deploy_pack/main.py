# src/minecraft/deploy_pack/main.py

"""Entry point and runtime orchestration (Project_Specs.md Section 2, Section 4.1, Section 6.2, Section 9.2).

Responsibilities (Section 9.2):
  * argument parsing and Section 2.5's exit-2 matrix
  * scope resolution
  * partition resolution (delegated to config_model)
  * interactive prompt for unmarked jars (Section 6.2), when the operator has
    not passed --non-interactive
  * preflight invocation (delegated to preflight)
  * the Section 4.1 runtime sequence: notices, stop, writes, reload, live, start,
    health, online
  * notification dispatch via notifications.py
  * exit-code mapping per Section 2.4

Slip mode (spec v1.3 Section 14.1, Section 14.5, Section 14.7)
------------------------------------------

New CLI flags:

  * ``--release <tag>`` - release mode, direct tag. Loads
    ``sync/packing_slips/<tag>.toml`` and builds ``SlipContext`` from
    ``www/releases/<tag>/``.
  * ``--release-pointer <name>`` - release mode via a pointer file.
    ``stable`` reads ``sync/packing_slips/stable`` (a single line like
    ``v1.0.2.toml``) and loads the referenced release. ``latest-slip``
    reads the DEV-local candidate pointer and loads the candidate.
  * ``--integration`` - integration mode. Requires
    ``--release-pointer latest-slip``. Builds ``SlipContext`` from the
    candidate and captures one clean committed HEAD before construction.

Legacy mode is preserved: without any slip flag, ``deploy_pack`` behaves
exactly as v2 and slip-context construction is skipped.

Exit codes (Section 2.4):
  0 success
  1 runtime failure
  2 CLI usage error
  3 configuration / preflight failure
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tomllib
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.errors import UpgradeError as SlipUpgradeError
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_integration_context,
    build_release_context,
)

from . import deps, notifications, prompt_ui, tool_version
from .config_model import DeploymentConfig, load_deployment_config
from .docker_runtime import Clock, DockerRuntime, RconTransport, select_rcon_transport
from .errors import (
    ConfigError,
    DeployPackError,
    DockerRuntimeError,
    DockerUnavailableError,
    RuntimeDeployError,
    UsageError,
)
from .files import load_protect_patterns
from .hooks import (
    PostHookResult,
    PreHookResult,
    ReachabilityProbeFn,
    RecoveryContext,
    RecoveryResult,
    compute_warned_and_running,
    execute_post_hook,
    execute_pre_hook,
    recover_stopped_containers,
)
from .logging_setup import configure as _configure_logging
from .logging_setup import get_logger as _get_loggingcore_logger
from .overrides import apply_side_overrides, load_side_overrides, save_side_overrides
from .preflight import PreflightError, PreflightPlan, ScopeSet, run_preflight, sticky_max
from .scope_client import ClientScopeResult, deploy_client_scope
from .scope_resource_pack import ResourcePackScopeResult, deploy_resource_pack_scope
from .scope_server import ServerScopeResult, deploy_server_scope

_DEFAULT_SLIP_CONFIG: dict[str, str] = {
    "release_slip_dir": "sync/packing_slips",
    "bundle_store": "www/releases",
    "candidate_pointer_path": "/var/lib/minecraft-upgrade/latest-slip",
    "candidate_store": "/var/lib/minecraft-upgrade/candidates",
}


# ===========================================================================
# Section 2  CLI surface
# ===========================================================================


_KNOWN_OVERRIDE_FLAGS = frozenset(
    {
        "sync_root",
        "modpack_dir",
        "mods_dir",
        "www_dir",
        "output_filename",
        "download_base_url",
        "protect_file",
    }
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for deploy_pack's CLI (Section 2.2, Section 2.5)."""
    parser = argparse.ArgumentParser(
        prog="deploy_pack",
        description="Deploy Minecraft server + client pack.",
        add_help=True,
    )
    parser.add_argument("--server", action="store_true", help="Server scope (Section 2.1)")
    parser.add_argument("--client", action="store_true", help="Client scope (Section 2.1)")
    parser.add_argument("--resource-pack", action="store_true", help="Resource-pack scope (Section 2.1)")
    parser.add_argument("--full", action="store_true", help="Server + client + resource-pack (Section 2.1)")
    parser.add_argument("--notify", action="store_true", help="Opt-in Discord notify (Section 2.2)")
    parser.add_argument("--dry-run", action="store_true", help="No writes, no lifecycle (Section 2.2, Section 2.6)")
    parser.add_argument("--with-resources", action="store_true", help="Include RP files in client ZIP (Section 2.2)")
    parser.add_argument("--instance", action="append", default=None, help="Target instances; repeatable / comma-separated (Section 2.9)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--debug-deps", action="store_true", help="Print dependency closure and exit / continue")
    parser.add_argument("--audit-mods", action="store_true", help="Standalone Textual UI (Section 6.1)")
    parser.add_argument("--non-interactive", action="store_true", help="Skip prompts; unmarked mods not deployed (Section 6.2)")
    parser.add_argument("--config-dir", type=str, default=None, help="Config directory (Section 2.2)")
    parser.add_argument(
        "--release",
        type=str,
        default=None,
        metavar="TAG",
        help="Release mode: load sync/packing_slips/<TAG>.toml and its bundle (spec Section 14.1)",
    )
    parser.add_argument(
        "--release-pointer",
        type=str,
        default=None,
        metavar="NAME",
        help="Release mode via pointer: 'stable' or 'latest-slip' (spec Section 14.5)",
    )
    parser.add_argument(
        "--integration",
        action="store_true",
        help="Integration mode: requires --release-pointer latest-slip (spec Section 14.1)",
    )
    return parser


def _parse_instances(raw: list[str] | None) -> set[str] | None:
    """Normalise --instance values into a set, or None if absent."""
    if raw is None:
        return None
    out: set[str] = set()
    for item in raw:
        for name in item.split(","):
            name = name.strip()
            if name:
                out.add(name)
    return out


def _parse_overrides(remaining: list[str]) -> dict[str, str]:
    """Parse leftover ``--flag value`` / ``--flag=value`` tokens into a kwargs dict.

    The resulting keys are snake_case (``--sync-root`` -> ``sync_root``),
    matching ``[deploy_pack.toml]``'s naming convention (Section 2.8). Unknown
    flags are caught earlier by :func:`_validate_passthrough_flags`; this
    function only runs on tokens that already passed that gate.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(remaining):
        tok = remaining[i]
        if not tok.startswith("--") or len(tok) <= 2:
            i += 1
            continue
        body = tok[2:]
        if "=" in body:
            key, val = body.split("=", 1)
            out[key.replace("-", "_")] = val
            i += 1
        else:
            key = body.replace("-", "_")
            if i + 1 < len(remaining) and not remaining[i + 1].startswith("--"):
                out[key] = remaining[i + 1]
                i += 2
            else:
                i += 1
    return out


@dataclass
class _Args:
    server: bool = False
    client: bool = False
    resource_pack: bool = False
    full: bool = False
    notify: bool = False
    dry_run: bool = False
    with_resources: bool = False
    instance: set[str] | None = None
    debug: bool = False
    debug_deps: bool = False
    audit_mods: bool = False
    non_interactive: bool = False
    config_dir: str | None = None
    release: str | None = None
    release_pointer: str | None = None
    integration: bool = False


def _validate_slip_flags(args: _Args, has_scope: bool) -> None:
    """Validate slip-flag combinations (spec v1.3 Section 14.1, Section 14.5)."""
    if args.release and args.release_pointer:
        raise UsageError("--release and --release-pointer are mutually exclusive")
    if (args.release or args.release_pointer or args.integration) and not has_scope:
        raise UsageError("slip flags require a deployment scope")
    if args.integration:
        if not args.release_pointer:
            raise UsageError("--integration requires --release-pointer")
        if args.release_pointer != "latest-slip":
            raise UsageError(f"--integration requires --release-pointer latest-slip, got {args.release_pointer!r}")
    if args.release_pointer is not None:
        if args.release_pointer not in ("stable", "latest-slip"):
            raise UsageError(f"unknown pointer: {args.release_pointer!r} (expected 'stable' or 'latest-slip')")
        if args.release_pointer == "latest-slip" and not args.integration:
            raise UsageError("--release-pointer latest-slip requires --integration")


def _validate_args(args: _Args, parser: argparse.ArgumentParser) -> None:
    """Raise UsageError for every Section 2.5 exit-2 condition."""
    if args.audit_mods:
        rejected = []
        if args.server:
            rejected.append("--server")
        if args.client:
            rejected.append("--client")
        if args.resource_pack:
            rejected.append("--resource-pack")
        if args.full:
            rejected.append("--full")
        if args.dry_run:
            rejected.append("--dry-run")
        if args.with_resources:
            rejected.append("--with-resources")
        if args.notify:
            rejected.append("--notify")
        if args.debug_deps:
            rejected.append("--debug-deps")
        if args.non_interactive:
            rejected.append("--non-interactive")
        if args.release:
            rejected.append("--release")
        if args.release_pointer:
            rejected.append("--release-pointer")
        if args.integration:
            rejected.append("--integration")
        if rejected:
            raise UsageError("--audit-mods is incompatible with: " + ", ".join(rejected))
        return
    has_scope = args.server or args.client or args.resource_pack or args.full
    if args.dry_run and (not has_scope):
        raise UsageError("--dry-run requires a scope")
    if args.with_resources and (not (args.client or args.full)):
        raise UsageError("--with-resources requires --client or --full")
    if args.full and args.instance is not None:
        raise UsageError("--full and --instance are mutually exclusive")
    if args.instance is not None and (not (args.server or args.resource_pack)):
        raise UsageError("--instance requires --server or --resource-pack")
    _validate_slip_flags(args, has_scope)


def _validate_passthrough_flags(remaining: list[str]) -> None:
    """Reject any unrecognized ``--flag`` in argparse's leftover list."""
    for arg in remaining:
        if not arg.startswith("--"):
            continue
        body = arg[2:]
        key = body.split("=", 1)[0].replace("-", "_")
        if key not in _KNOWN_OVERRIDE_FLAGS:
            raise UsageError(f"unknown flag: {arg}")


@dataclass
class _Scopes:
    scope_set: ScopeSet
    with_resources: bool

    def any(self) -> bool:
        """Return True if at least one deployment scope is active."""
        return self.scope_set.any()


def _resolve_scopes(args: _Args) -> _Scopes:
    """Resolve the _Scopes bundle from the parsed CLI surface (Section 2.1)."""
    if args.full:
        return _Scopes(ScopeSet(server=True, client=True, resource_pack=True), with_resources=True)
    return _Scopes(
        ScopeSet(server=args.server, client=args.client, resource_pack=args.resource_pack),
        with_resources=args.with_resources,
    )


def _resolve_config_dir(raw: str | None) -> Path:
    """Return the config directory as a filesystem path."""
    if raw:
        p = Path(raw)
        if p.is_file():
            p = p.parent
        return p
    return Path("config.d")


# ===========================================================================
# Section 2b  Slip context construction (spec v1.3 Section 14.1, Section 14.5, Section 14.7)
# ===========================================================================


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run ``git`` with ``args`` in ``cwd``; return (returncode, stdout-stripped).

    Returns (127, "") when git is unavailable or times out so the caller
    can distinguish "git missing" from "git failed".
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.returncode, result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (127, "")


def _read_slip_config(config_dir: Path) -> dict[str, str]:
    """Return the ``[slip]`` table from ``deploy_pack.toml`` merged with defaults."""
    toml_path = config_dir / "deploy_pack.toml"
    raw: dict[str, str] = {}
    if toml_path.is_file():
        try:
            with open(toml_path, "rb") as handle:
                data = tomllib.load(handle)
            section = data.get("slip")
            if isinstance(section, dict):
                raw = {str(k): str(v) for k, v in section.items()}
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
    merged = dict(_DEFAULT_SLIP_CONFIG)
    merged.update(raw)
    return merged


def _read_pointer_file(path: Path, logger: Any) -> str:
    """Read a single-line pointer file and return the stripped content."""
    if not path.is_file():
        raise ConfigError(f"pointer file not found: {path}")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"could not read pointer file {path}: {exc}") from exc
    if not content:
        raise ConfigError(f"pointer file is empty: {path}")
    return content


def _strip_toml_suffix(value: str) -> str:
    """Return ``value`` without a trailing ``.toml`` suffix, if present."""
    return value[:-5] if value.endswith(".toml") else value


def _build_release_slip_context(slip_dir: Path, bundle_store: Path, release_id: str, logger: Any) -> SlipContext:
    """Load a release slip and construct a release-mode SlipContext."""
    slip_path = slip_dir / f"{release_id}.toml"
    bundle_root = bundle_store / release_id
    logger.info(f"slip: loading release {release_id!r} from {slip_path}, bundle {bundle_root}")
    slip_obj = slip_mod.load_slip(slip_path)
    return build_release_context(slip_obj, bundle_root)


def _build_integration_slip_context(
    project_root: Path,
    pointer_path: Path,
    candidate_store: Path,
    logger: Any,
) -> SlipContext:
    """Load a candidate and construct an integration-mode SlipContext.

    Rejects dirty working trees for ``sync/config`` and ``sync/kubejs``
    before capturing HEAD (spec Section 14.7). Captures HEAD exactly once.
    """
    candidate_id = _read_pointer_file(pointer_path, logger)
    candidate_root = candidate_store / candidate_id
    slip_path = candidate_root / "packing_slip.toml"
    config_source = project_root / "sync" / "config"
    kubejs_source = project_root / "sync" / "kubejs"

    rc, out = _run_git(
        ["status", "--porcelain", "sync/config", "sync/kubejs"],
        project_root,
    )
    if rc == 127:
        raise ConfigError("integration mode: git is not available; cannot capture HEAD")
    if rc != 0:
        raise ConfigError(f"integration mode: git status failed (rc={rc}); cannot capture HEAD")
    if out:
        raise ConfigError(f"integration mode: dirty working tree for sync/config or sync/kubejs: {out[:200]}")

    rc, head = _run_git(["rev-parse", "HEAD"], project_root)
    if rc != 0 or not head:
        raise ConfigError(f"integration mode: git rev-parse HEAD failed (rc={rc})")

    logger.info(f"slip: loading integration candidate {candidate_id!r} from {slip_path}, HEAD={head[:12]}")
    slip_obj = slip_mod.load_slip(slip_path)
    return build_integration_context(
        slip_obj,
        candidate_root,
        config_source=config_source,
        kubejs_source=kubejs_source,
        captured_head=head,
    )


def _build_slip_context(config_dir: Path, args: _Args, logger: Any) -> SlipContext | None:
    """Build a SlipContext if slip flags are present; None in legacy mode."""
    if not (args.release or args.release_pointer):
        return None
    slip_cfg = _read_slip_config(config_dir)
    project_root = config_dir.parent
    try:
        if args.integration:
            return _build_integration_slip_context(
                project_root,
                Path(slip_cfg["candidate_pointer_path"]),
                Path(slip_cfg["candidate_store"]),
                logger,
            )
        slip_dir = project_root / slip_cfg["release_slip_dir"]
        bundle_store = project_root / slip_cfg["bundle_store"]
        if args.release:
            release_id = args.release
        else:  # args.release_pointer == "stable"
            release_id = _strip_toml_suffix(_read_pointer_file(slip_dir / "stable", logger))
        return _build_release_slip_context(slip_dir, bundle_store, release_id, logger)
    except ConfigError:
        raise
    except SlipUpgradeError as exc:
        raise ConfigError(f"slip load failed: {exc}") from exc
    except Exception as exc:
        raise ConfigError(f"slip context build failed: {exc}") from exc


# ===========================================================================
# Section 3  Config load
# ===========================================================================


def _setup_logging(debug: bool) -> Any:
    """Configure logging and return this module's logger."""
    level = logging.DEBUG if debug else logging.INFO
    logging.getLogger().setLevel(level)
    _configure_logging(debug=debug)
    return _get_loggingcore_logger(__name__)


def _load_config(
    config_dir: Path,
    overrides: list[str],
    slip: SlipContext | None = None,
    requested_instances: set[str] | None = None,
) -> DeploymentConfig:
    """Load the deployment config, forwarding CLI overrides as kwargs."""
    parsed = _parse_overrides(overrides)
    return load_deployment_config(
        config_dir=config_dir,
        requested_instances=requested_instances,
        slip=slip,
        **parsed,
    )


def _sticky_max(actions: list[str]) -> str:
    """Return the Section 4.6.2 sticky-max over ``actions``."""
    return sticky_max(actions)


# ===========================================================================
# Section 4  Interactive prompt (Section 6.2, Section 6.3)
# ===========================================================================


def _classify_unmarked(unmarked: list, entries: list[dict]) -> tuple[list, list]:
    """Split unmarked jars into (fixable, unfixable) by whether an index entry exists."""
    indexed_files = {str(e.get("file", "")) for e in entries if e.get("file")}
    fixable = [u for u in unmarked if u.filename in indexed_files]
    unfixable = [u for u in unmarked if u.filename not in indexed_files]
    return fixable, unfixable


def _warn_unfixable(unfixable: list, logger: Any) -> None:
    """Log the unfixable-unmarked warning when there is anything to warn about."""
    if not unfixable:
        return
    names = ", ".join(u.filename for u in unfixable)
    logger.warning(f"{len(unfixable)} unmarked jar(s) have no .pw.toml and cannot be fixed here; they will not be deployed. Files: {names}")


def _prompt_one_jar(jar: Any) -> str | None:
    """Prompt for one jar; returns the review value or None to defer."""
    print(f"Unmarked: {jar.filename}")
    print(f"  reason: {jar.reason}")
    while True:
        try:
            answer = input("  side? [s]erver / [c]lient / [b]oth / [k]skip / [d]efer: ")
        except EOFError:
            answer = "d"
        answer = answer.strip().lower()
        if answer == "s":
            return "server"
        if answer == "c":
            return "client"
        if answer == "b":
            return "both"
        if answer == "k":
            return "skipped"
        if answer in ("d", ""):
            return None
        print("  Invalid answer. Try again.")


def _prompt_for_unmarked(config: DeploymentConfig, logger: Any) -> None:
    """Walk unmarked jars, prompt for a side, write review entries."""
    index_dir = Path(config.modpack_dir) / ".index"
    if not index_dir.is_dir():
        return
    entries = deps.load_prism_index(index_dir)
    unmarked = deps.find_unmarked(config.modpack_dir, entries)
    if not unmarked:
        return

    fixable, unfixable = _classify_unmarked(unmarked, entries)
    _warn_unfixable(unfixable, logger)
    if not fixable:
        return

    if not sys.stdin.isatty():
        logger.warning(
            f"{len(fixable)} unmarked jar(s) need review but stdin is not a TTY; "
            f"skipping the interactive prompt. Pass --non-interactive to "
            f"acknowledge, or run in a terminal."
        )
        return

    overrides_path = Path(config.config_dir) / "side_overrides.toml"
    existing = load_side_overrides(overrides_path)
    review = dict(existing.deployment_tool_review)
    logger.info(f"Prompting for {len(fixable)} unmarked jar(s). Answers are written to {overrides_path}.")
    print()
    for jar in fixable:
        answer = _prompt_one_jar(jar)
        if answer is not None:
            review[jar.filename] = answer
    save_side_overrides(overrides_path, review, logger=logger)
    logger.info(f"Wrote {len(review)} review entr(ies) to {overrides_path}")


# ===========================================================================
# Section 5  RCON transport set
# ===========================================================================


class _RconSet:
    """Per-member RCON transports, keyed by container name."""

    def __init__(self, config: DeploymentConfig, runtime: DockerRuntime, logger: Any) -> None:
        self._config = config
        self._runtime = runtime
        self._logger = logger
        self._transports: dict[str, RconTransport] = {}
        self._selection_errors: dict[str, str] = {}
        compose = config.compose.file if config.compose.ok else None
        for inst in config.instances.values():
            if inst.service is None or compose is None:
                continue
            try:
                transport = select_rcon_transport(runtime, inst.service, compose, inst.container, config.docker.rcon_host)
            except ConfigError as exc:
                self._selection_errors[inst.container] = str(exc)
                continue
            self._transports[inst.container] = transport

    def probe(self, container_name: str) -> bool:
        """Return True if a container responds to a no-op RCON command."""
        transport = self._transports.get(container_name)
        if transport is None:
            return False
        try:
            ok, _ = transport.execute("list")
            return bool(ok)
        except Exception:
            return False

    def send(self, container_name: str, command: str) -> bool:
        """Send a command to a container over its RCON transport."""
        transport = self._transports.get(container_name)
        if transport is None:
            return False
        try:
            ok, _ = transport.execute(command)
            return bool(ok)
        except Exception:
            return False

    def dispatch_cancel(self, container_names: list[str], message: str) -> None:
        """Send a cancellation ``say`` message to each named container."""
        for name in container_names:
            self.send(name, f"say {message}")


# ===========================================================================
# Section 6  Diagnostics (Section 2.5)
# ===========================================================================


def _run_debug_deps(config: DeploymentConfig, logger: Any) -> None:
    """Print dependency closure for both sides (Section 2.5)."""
    index_dir = Path(config.modpack_dir) / ".index"
    if not index_dir.is_dir():
        print(f"Prism index directory not found: {index_dir}", file=sys.stderr)
        return
    all_mods = deps.load_prism_index(index_dir)
    if not all_mods:
        print(f"No mod entries found in {index_dir}", file=sys.stderr)
        return
    overrides_path = Path(config.config_dir) / "side_overrides.toml"
    ovr = load_side_overrides(overrides_path)
    if not ovr.is_empty():
        all_mods = apply_side_overrides(all_mods, ovr)
    seeds = {side: deps.filter_prism_entries_by_side(all_mods, side) for side in ("client", "server")}
    print(deps.format_diagnostic(all_entries=all_mods, seeds=seeds, modpack_dir=config.modpack_dir, logger=logger))


def _run_diagnostic_notify(config: DeploymentConfig, logger: Any) -> None:
    """Attempt the diagnostic Discord message (Section 5.2)."""
    ctx = notifications.DiagnosticContext(
        tool_version=tool_version(),
        timestamp=notifications.render_timestamp_now(),
    )
    all_roles = config.discord.player_roles + config.discord.operator_roles
    notifications.notify_diagnostic(config.discord.diagnostic_template, ctx, config.webhook_url, all_roles, logger=logger)


# ===========================================================================
# Section 7  Notification composition (Section 4.6.9, Section 4.6.10, Section 5.4-Section 5.6)
# ===========================================================================


def _aggregate_reasons(plan: PreflightPlan) -> list[tuple[str, int]]:
    """Group the plan's reason entries by pattern, counting changed paths."""
    counts: dict[str, int] = {}
    for member in plan.partition:
        mp = plan.member_plans.get(member)
        if mp is None:
            continue
        for r in mp.reasons:
            counts[r.path_prefix] = counts.get(r.path_prefix, 0) + len(r.changed_paths)
    return sorted(counts.items())


def _server_effective_action(plan: PreflightPlan) -> str:
    """Return the sticky-max effective action across the plan's members."""
    actions = [mp.effective_action for mp in plan.member_plans.values()]
    return _sticky_max(actions) if actions else "none"


def _collect_config_members(plan: PreflightPlan) -> tuple[list[str], int]:
    """Return (members_with_config_changes, total_non_mods_changed_paths)."""
    members: list[str] = []
    changed = 0
    for member in plan.partition:
        mp = plan.member_plans.get(member)
        if mp is None:
            continue
        ck = [p for p in mp.changed_paths if not p.startswith("mods/")]
        if ck:
            members.append(member)
            changed += len(ck)
    return members, changed


def _build_server_section(plan: PreflightPlan) -> str:
    """Render the live notification's server section (Section 4.6.10)."""
    targeted = plan.targeted
    mods_deployed = None
    if plan.mods_change is not None and (not targeted):
        mods_deployed = len(plan.mods_change.added) + len(plan.mods_change.updated)
    config_members, config_changed_count = _collect_config_members(plan)
    return notifications.build_server_section(
        targeted=targeted,
        targeted_members=plan.partition if targeted else None,
        mods_deployed=mods_deployed,
        mods_skipped=False,
        mods_drift=plan.mods_drift,
        config_kubejs_members=config_members,
        config_kubejs_changed=config_changed_count,
        effective_action=_server_effective_action(plan),
        pack_required=plan.pack_required,
        reasons=_aggregate_reasons(plan),
        no_restart_performed=targeted and (not plan.restart_set) and (not plan.reload_set),
        pack_required_warning=plan.pack_required_warning,
    )


def _build_client_section(client_result: ClientScopeResult | None, dry_run: bool) -> str:
    """Render the live notification's client section (Section 5.4)."""
    if dry_run or client_result is None:
        return notifications.build_client_section(zip_filename=None, sha256=None, changelog_url=None, dry_run=True)
    return notifications.build_client_section(
        zip_filename=client_result.resolved_output_filename,
        sha256=client_result.zip_sha256,
        changelog_url=client_result.changelog_url,
        dry_run=False,
    )


def _build_rp_section(plan: PreflightPlan, rp_result: ResourcePackScopeResult | None) -> str:
    """Render the live notification's resource-pack section (Section 7.7)."""
    configured = any(member in plan.member_plans and plan.member_plans[member].resource_pack_target for member in plan.partition) or bool(
        plan.partition and rp_result and rp_result.publish_results
    )
    if not configured:
        return notifications.build_resource_pack_section(configured=False, published_filename=None, members=[], effective_action=None)
    filename = None
    if rp_result and rp_result.publish_results:
        filename = rp_result.publish_results[0].filename
    return notifications.build_resource_pack_section(
        configured=True,
        published_filename=filename,
        members=list(plan.partition),
        effective_action=None,
    )


def _send_live(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    client_result: ClientScopeResult | None,
    rp_result: ResourcePackScopeResult | None,
    dry_run: bool,
    logger: Any,
) -> notifications.NotifyResult:
    """Render and post the live notification (Section 5.4)."""
    ctx = notifications.LiveContext(
        tool_version=tool_version(),
        timestamp=notifications.render_timestamp_now(),
        requested_scopes=scopes.scope_set.names(),
        instance_list=sorted(plan.partition),
        dry_run=dry_run,
        player_roles=config.discord.player_roles,
        operator_roles=config.discord.operator_roles,
        section_server=_build_server_section(plan) if scopes.scope_set.server else "",
        section_client=_build_client_section(client_result, dry_run) if scopes.scope_set.client else "",
        section_resource_pack=_build_rp_section(plan, rp_result) if scopes.scope_set.resource_pack else "",
    )
    all_roles = config.discord.player_roles + config.discord.operator_roles
    return notifications.notify_live(config.discord.live_template, ctx, config.webhook_url, all_roles, logger=logger)


def _send_failure(
    config: DeploymentConfig,
    failure_stage: str,
    error: str,
    container_status: dict[str, str],
    logger: Any,
) -> notifications.NotifyResult:
    """Render and post the failure notification (Section 5.6)."""
    ctx = notifications.FailureContext(
        tool_version=tool_version(),
        timestamp=notifications.render_timestamp_now(),
        operator_roles=config.discord.operator_roles,
        failure_stage=failure_stage,
        error=error,
        container_status=container_status,
    )
    all_roles = config.discord.player_roles + config.discord.operator_roles
    return notifications.notify_failure(config.discord.failure_template, ctx, config.webhook_url, all_roles, logger=logger)


def _send_online(config: DeploymentConfig, container_status: dict[str, str], logger: Any) -> notifications.NotifyResult:
    """Render and post the online notification (Section 5.5)."""
    ctx = notifications.OnlineContext(
        tool_version=tool_version(),
        timestamp=notifications.render_timestamp_now(),
        player_roles=config.discord.player_roles,
        container_status=container_status,
    )
    all_roles = config.discord.player_roles + config.discord.operator_roles
    return notifications.notify_online(config.discord.online_template, ctx, config.webhook_url, all_roles, logger=logger)


# ===========================================================================
# Section 8  Recovery / notice helpers (Section 4.5, Section 5.8, Section 8.7, Section 8.8)
# ===========================================================================


@dataclass
class _NoticeOutcome:
    attempted: list[str]
    delivered: list[str]
    failed: list[str]

    @property
    def any_delivered(self) -> bool:
        """Return True if at least one notice was delivered."""
        return bool(self.delivered)


def _build_recovery_ctx(
    config: DeploymentConfig,
    rcon_set: _RconSet,
    cancel_message: str,
    clock: Clock | None = None,
) -> RecoveryContext:
    """Assemble a RecoveryContext around the given RCON set."""
    container_of = {name: inst.container for name, inst in config.instances.items() if inst.container}

    def probe(instance_name: str) -> bool:
        """Return True if the instance is RCON-reachable."""
        return rcon_set.probe(container_of.get(instance_name, instance_name))

    def cancel(instance_names: list[str]) -> None:
        """Send the cancellation notice to each named instance."""
        containers = [container_of.get(m, m) for m in instance_names]
        rcon_set.dispatch_cancel(containers, cancel_message)

    probe_fn: ReachabilityProbeFn = probe
    return RecoveryContext(
        reachability_probe=probe_fn,
        cancel_notice_fn=cancel,
        cancel_ready_timeout=float(config.docker.cancel_notice_ready_timeout_seconds),
        poll_interval=float(config.docker.health_poll_seconds),
        clock=clock if clock is not None else Clock(),
    )


def _dispatch_restart_notices(
    config: DeploymentConfig,
    warned_and_running: list[str],
    rcon_set: _RconSet,
    logger: Any,
) -> _NoticeOutcome:
    """Dispatch the in-game restart notice to warned-and-running members (Section 4.14 step 4)."""
    template = config.docker.restart_notice_template
    wait_seconds = config.docker.restart_wait_seconds
    message = template.replace("{time}", f"{wait_seconds} seconds")
    outcome = _NoticeOutcome(attempted=[], delivered=[], failed=[])
    for member in warned_and_running:
        inst = config.instances.get(member)
        if inst is None:
            continue
        outcome.attempted.append(member)
        ok = rcon_set.send(inst.container, f"say {message}")
        if ok:
            outcome.delivered.append(member)
        else:
            outcome.failed.append(member)
            if config.docker.in_game_notice_required:
                break
    return outcome


def _dispatch_cancel_notice(config: DeploymentConfig, recipients: list[str], rcon_set: _RconSet, logger: Any) -> None:
    """Dispatch the in-game cancel notice to the given recipients (Section 8.7)."""
    if not recipients:
        return
    msg = config.docker.restart_cancel_notice_template
    rcon_set.dispatch_cancel([config.instances[m].container for m in recipients if m in config.instances], msg)


def _online_container_status(plan: PreflightPlan, to_stop: list[str], post: PostHookResult) -> dict[str, str]:
    """Section 5.8 online: containers successfully stopped and brought up healthy."""
    out: dict[str, str] = {}
    for member in to_stop:
        if member in post.started and member not in post.start_failed and member not in post.health_failed:
            out[member] = notifications.STATE_ONLINE_HEALTHY
    return out


def _failure_container_status(
    plan: PreflightPlan,
    pre: PreHookResult | None,
    post: PostHookResult | None,
    reloaded: list[str],
    reload_failed: list[str],
) -> dict[str, str]:
    """Section 5.8 failure: all lifecycle-touched containers, plus internal states."""
    out: dict[str, str] = {}
    if pre is not None:
        for member in pre.stopped:
            out[member] = notifications.STATE_STOPPED_BY_DEPLOYMENT
        for member in pre.exited_before_stop:
            out[member] = notifications.STATE_EXITED_BEFORE_STOP
        if pre.recovery is not None:
            for m in pre.recovery.start_failed:
                out[m] = notifications.STATE_RECOVERY_START_FAILED
            for m in pre.recovery.started:
                out[m] = notifications.STATE_RECOVERY_START_SUCCEEDED
    for member in reloaded:
        out[member] = notifications.STATE_RELOADED
    for member in reload_failed:
        out[member] = notifications.STATE_RELOAD_FAILED
    if post is not None:
        for member in post.healthy:
            out[member] = notifications.STATE_ONLINE_HEALTHY
        for member in post.start_failed:
            out[member] = notifications.STATE_START_FAILED
        for member in post.health_failed:
            out[member] = notifications.STATE_HEALTH_TIMEOUT
    for member in plan.partition:
        state = plan.container_states.get(member)
        if state is None:
            continue
        if not state.is_running and member not in out:
            out[member] = notifications.STATE_STOPPED
    return out


def _print_cli_container_status(status: dict[str, str]) -> None:
    """Print per-container status to CLI, format per Section 5.8."""
    for member in sorted(status):
        print(f"{member}: {status[member]}")


def _print_server_write_failure_recovery(config: DeploymentConfig, to_stop: list[str], failure_summary: str) -> None:
    """Print the Section 4.7 server-scope write-failure recovery block to CLI."""
    print()
    print("RECOVERY REQUIRED")
    print()
    print("A server-scope write failed. Minecraft containers are stopped and")
    print("were not automatically restarted.")
    print()
    print("Affected instances:")
    containers: list[str] = []
    for member in sorted(to_stop):
        inst = config.instances.get(member)
        container = getattr(inst, "container", None) or member
        containers.append(container)
        print(f"  {container}: stopped")
    print()
    print("Reason:")
    print(f"  {failure_summary}")
    print()
    print("Recovery steps:")
    print("  1. Investigate the failure and repair the source.")
    print("  2. Re-run deployment once the source is correct.")
    print("     Note: a same-day re-run overwrites the day's client ZIP if")
    print("     --client is in scope, and the day's resource-pack ZIP if")
    print("     --resource-pack is in scope.")
    print("  3. Or start containers manually with:")
    print("       docker start " + " ".join(containers))


def _print_recovery_block(
    config: DeploymentConfig,
    recovery: RecoveryResult,
    container_states: dict[str, Any],
    reason: str,
) -> None:
    """Print the Section 8.8 recovery block after a failed pre-hook stop."""
    start_failed = list(getattr(recovery, "start_failed", []) or [])
    unreachable = list(getattr(recovery, "unreachable", []) or [])
    affected = sorted(set(start_failed) | set(unreachable))
    if not affected:
        return
    print()
    print("RECOVERY REQUIRED")
    print()
    print(reason)
    print()
    print("Affected instances:")
    containers: list[str] = []
    for member in affected:
        inst = config.instances.get(member)
        container = getattr(inst, "container", None) or member
        containers.append(container)
        print(f"  {container}: stopped")
    print()
    print("Recovery steps:")
    print("  1. Investigate the failure and repair the source.")
    print("  2. Re-run deployment once the source is correct.")
    print("  3. Or start containers manually with:")
    print("       docker start " + " ".join(containers))


# ===========================================================================
# Section 9  Scope writes (Section 4.2)
# ===========================================================================


def _run_writes(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    protect_patterns: list[str],
    logger: Any,
) -> tuple[
    bool,
    ServerScopeResult | None,
    ClientScopeResult | None,
    ResourcePackScopeResult | None,
    str | None,
]:
    """Perform scopes in Section 4.2's order, halting on first failure."""
    server_result: ServerScopeResult | None = None
    client_result: ClientScopeResult | None = None
    rp_result: ResourcePackScopeResult | None = None
    if scopes.scope_set.server:
        server_result = deploy_server_scope(config, plan, protect_patterns)
        if not server_result.success:
            return (False, server_result, None, None, server_result.failure_message or "server scope failed")
    if scopes.scope_set.client:
        client_result = deploy_client_scope(config, scopes.with_resources, protect_patterns)
        if not client_result.success:
            return (False, server_result, client_result, None, client_result.failure_message or "client scope failed")
    if scopes.scope_set.resource_pack:
        rp_result = deploy_resource_pack_scope(config, plan, protect_patterns)
        if not rp_result.success:
            return (False, server_result, client_result, rp_result, rp_result.failure_message or "resource-pack scope failed")
    return (True, server_result, client_result, rp_result, None)


# ===========================================================================
# Section 10  Runtime phases (Section 4.1)
# ===========================================================================


@dataclass
class _RuntimeState:
    """Mutable state threaded through the Section 4.1 runtime phases."""

    config: DeploymentConfig
    plan: PreflightPlan
    scopes: _Scopes
    runtime: DockerRuntime
    notify: bool
    protect_patterns: list[str]
    logger: Any
    container_of: dict[str, str]
    rcon_set: _RconSet
    recovery_ctx: RecoveryContext
    warned_and_running: list[str] = field(default_factory=list)
    to_stop: list[str] = field(default_factory=list)
    pre: PreHookResult | None = None
    reloaded: list[str] = field(default_factory=list)
    reload_failed: list[str] = field(default_factory=list)
    server_result: ServerScopeResult | None = None
    client_result: ClientScopeResult | None = None
    rp_result: ResourcePackScopeResult | None = None


def _make_runtime_state(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    runtime: DockerRuntime,
    notify: bool,
    protect_patterns: list[str],
    logger: Any,
) -> _RuntimeState:
    """Build the state bundle the runtime phases operate on."""
    container_of = {name: inst.container for name, inst in config.instances.items() if inst.container}
    rcon_set = _RconSet(config, runtime, logger)
    recovery_ctx = _build_recovery_ctx(config, rcon_set, config.docker.restart_cancel_notice_template)
    return _RuntimeState(
        config=config,
        plan=plan,
        scopes=scopes,
        runtime=runtime,
        notify=notify,
        protect_patterns=protect_patterns,
        logger=logger,
        container_of=container_of,
        rcon_set=rcon_set,
        recovery_ctx=recovery_ctx,
    )


def _run_dry_run_phase(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    notify: bool,
    logger: Any,
) -> int:
    """Section 2.6: report the plan and return without any write or lifecycle op."""
    logger.info("=" * 72)
    logger.info("DRY RUN - plan")
    logger.info(f"  partition:    {', '.join(plan.partition) or '(none)'}")
    logger.info(f"  none_set:     {', '.join(plan.none_set) or '(none)'}")
    logger.info(f"  reload_set:   {', '.join(plan.reload_set) or '(none)'}")
    logger.info(f"  restart_set:  {', '.join(plan.restart_set) or '(none)'}")
    logger.info(f"  pack_required: {plan.pack_required}")
    for member in plan.partition:
        mp = plan.member_plans.get(member)
        if mp is None:
            continue
        logger.info(f"  [{member}] action={mp.effective_action} changed={len(mp.changed_paths)} paths")
        for path in sorted(mp.changed_paths):
            logger.debug(f"    [{member}] {path}")
    for w in plan.warnings:
        logger.warning(f"  {w}")
    logger.info("=" * 72)
    if notify:
        _send_live(config, plan, scopes, None, None, True, logger)
    return 0


def _run_notice_phase(state: _RuntimeState) -> int | None:
    """Section 4.14 steps 3-5: compute warned_and_running, dispatch notices, wait."""
    if state.plan.restart_set:
        state.warned_and_running = compute_warned_and_running(
            runtime=state.runtime,
            restart_set=state.plan.restart_set,
            preflight_states=state.plan.container_states,
            container_of=state.container_of,
        )
    if not state.warned_and_running:
        return None

    outcome = _dispatch_restart_notices(state.config, state.warned_and_running, state.rcon_set, state.logger)
    if outcome.failed and state.config.docker.in_game_notice_required:
        _dispatch_cancel_notice(state.config, outcome.attempted, state.rcon_set, state.logger)
        print("in_game_notice: RCON notice failed for: " + ", ".join(outcome.failed))
        state.logger.error("restart notice dispatch failed; aborting before any writes")
        return 1
    if outcome.any_delivered:
        wait_s = state.config.docker.restart_wait_seconds
        if wait_s > 0:
            state.logger.info(f"waiting {wait_s}s for in-game restart window")
            Clock().sleep(float(wait_s))
    return None


def _run_stop_phase(state: _RuntimeState) -> int | None:
    """Section 4.14 step 6: execute the pre-hook stop, or abort on failure."""
    if not state.plan.restart_set:
        return None
    stop_timeouts: dict[str, int] = {}
    for member in state.plan.restart_set:
        inst = state.config.instances.get(member)
        if inst is not None:
            stop_timeouts[member] = inst.stop_grace_seconds
    state.pre = execute_pre_hook(
        runtime=state.runtime,
        warned_and_running=state.warned_and_running,
        stop_timeouts=stop_timeouts,
        recovery_ctx=state.recovery_ctx,
        container_of=state.container_of,
    )
    state.to_stop = list(state.pre.stopped)
    if state.pre.failed:
        state.logger.error(f"stop phase failed for: {', '.join(state.pre.failed)}")
        print("pre_hook: docker stop failed for: " + ", ".join(state.pre.failed))
        if state.pre.recovery is not None and state.pre.recovery.any_failure:
            _print_recovery_block(
                state.config,
                state.pre.recovery,
                state.plan.container_states,
                "stop phase failure",
            )
        return 1
    return None


def _run_write_phase(state: _RuntimeState) -> int | None:
    """Section 4.2: run the scopes; handle write failure per Section 4.7."""
    write_ok, server_result, client_result, rp_result, write_msg = _run_writes(state.config, state.plan, state.scopes, state.protect_patterns, state.logger)
    state.server_result = server_result
    state.client_result = client_result
    state.rp_result = rp_result
    if write_ok:
        return None

    state.logger.error(f"write failed: {write_msg}")
    if state.scopes.scope_set.server:
        if state.notify:
            status = _failure_container_status(state.plan, state.pre, None, [], [])
            _send_failure(state.config, "mid_scope_write", write_msg, status, state.logger)
        if state.to_stop:
            _print_server_write_failure_recovery(state.config, state.to_stop, write_msg)
        else:
            print(f"mid_scope_write: {write_msg}")
        return 1

    recovery = recover_stopped_containers(
        runtime=state.runtime,
        stopped_by_deployment=state.to_stop,
        warned_and_running=state.warned_and_running,
        ctx=state.recovery_ctx,
        container_of=state.container_of,
    )
    if state.pre is not None:
        state.pre.recovery = recovery
    status = _failure_container_status(state.plan, state.pre, None, [], [])
    if state.notify:
        _send_failure(state.config, "mid_scope_write", write_msg, status, state.logger)
    print(f"mid_scope_write: {write_msg}")
    _print_cli_container_status(status)
    return 1


def _run_reload_phase(state: _RuntimeState) -> int | None:
    """Section 4.6.8: RCON reload running reload_set members."""
    if not state.plan.reload_set:
        return None
    for member in sorted(state.plan.reload_set):
        inst = state.config.instances.get(member)
        if inst is None:
            continue
        cs = state.plan.container_states.get(member)
        if cs is not None and not cs.is_running:
            continue
        if state.rcon_set.send(inst.container, "reload"):
            state.reloaded.append(member)
        else:
            state.reload_failed.append(member)
    if not state.reload_failed:
        return None

    msg = "RCON reload failed for: " + ", ".join(state.reload_failed)
    state.logger.error(msg)
    recovery = recover_stopped_containers(
        runtime=state.runtime,
        stopped_by_deployment=state.to_stop,
        warned_and_running=state.warned_and_running,
        ctx=state.recovery_ctx,
        container_of=state.container_of,
    )
    if state.pre is not None:
        state.pre.recovery = recovery
    status = _failure_container_status(state.plan, state.pre, None, state.reloaded, state.reload_failed)
    if state.notify:
        _send_failure(state.config, "reload", msg, status, state.logger)
    print(f"reload: {msg}")
    _print_cli_container_status(status)
    return 1


def _send_live_if_notify(state: _RuntimeState) -> None:
    """Post the live notification when --notify is active."""
    if state.notify:
        _send_live(
            state.config,
            state.plan,
            state.scopes,
            state.client_result,
            state.rp_result,
            False,
            state.logger,
        )


def _handle_post_failure(state: _RuntimeState, post: PostHookResult) -> int:
    """Report a post-phase failure and return exit 1."""
    stage = getattr(post, "failure_stage", None)
    if stage is None:
        stage = "post_hook" if post.start_failed else "health_timeout"
    summary_attr = getattr(post, "error_summary", None)
    if callable(summary_attr):
        err = summary_attr()
    elif summary_attr is not None:
        err = str(summary_attr)
    else:
        parts: list[str] = []
        if post.start_failed:
            parts.append("docker start failed for: " + ", ".join(sorted(post.start_failed)))
        if post.health_failed:
            parts.append("health timeout for: " + ", ".join(sorted(post.health_failed)))
        err = "; ".join(parts) if parts else "post-start phase failed"
    state.logger.error(err)
    status = _failure_container_status(state.plan, state.pre, post, state.reloaded, state.reload_failed)
    if state.notify:
        _send_failure(state.config, stage, err, status, state.logger)
    print(f"{stage}: {err}")
    _print_cli_container_status(status)
    return 1


def _run_post_phase(state: _RuntimeState) -> int:
    """Section 4.13: start every container this deployment stopped; wait for health."""
    if not state.plan.restart_set:
        return 0
    post = execute_post_hook(
        runtime=state.runtime,
        stopped_by_deployment=state.to_stop,
        preflight_states=state.plan.container_states,
        health_timeout=state.config.docker.health_timeout_seconds,
        poll_interval=state.config.docker.health_poll_seconds,
        container_of=state.container_of,
    )
    if post.start_failed or post.health_failed:
        return _handle_post_failure(state, post)
    online_status = _online_container_status(state.plan, state.to_stop, post)
    if state.notify and online_status:
        _send_online(state.config, online_status, state.logger)
    return 0


def _run_deployment(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    runtime: DockerRuntime,
    notify: bool,
    dry_run: bool,
    protect_patterns: list[str],
    logger: Any,
) -> int:
    """Execute the Section 4.1 runtime sequence."""
    if not scopes.scope_set.any():
        return 0
    if dry_run:
        return _run_dry_run_phase(config, plan, scopes, notify, logger)

    state = _make_runtime_state(config, plan, scopes, runtime, notify, protect_patterns, logger)

    code = _run_notice_phase(state)
    if code is not None:
        return code
    code = _run_stop_phase(state)
    if code is not None:
        return code
    code = _run_write_phase(state)
    if code is not None:
        return code
    code = _run_reload_phase(state)
    if code is not None:
        return code
    _send_live_if_notify(state)
    return _run_post_phase(state)


# ===========================================================================
# Section 11  CLI dispatch (Section 2.4, Section 2.5, Section 2.7)
# ===========================================================================


def _load_config_or_exit(
    config_dir: Path,
    remaining: list[str],
    logger: Any,
    slip: SlipContext | None = None,
    requested_instances: set[str] | None = None,
) -> DeploymentConfig | None:
    """Load config, logging and swallowing any exception; return None on failure."""
    try:
        return _load_config(
            config_dir,
            remaining,
            slip=slip,
            requested_instances=requested_instances,
        )
    except Exception as exc:
        logger.error(f"config error: {exc}")
        return None


def _run_debug_deps_safe(config: DeploymentConfig, logger: Any) -> None:
    """Run --debug-deps alongside a scoped run; log failures, never raise."""
    try:
        _run_debug_deps(config, logger)
    except Exception as exc:
        logger.error(f"debug-deps failed: {exc}")


def _prompt_if_needed(config: DeploymentConfig, scopes: _Scopes, args: _Args, logger: Any) -> int | None:
    """Section 6.2: prompt for unmarked jars on live server/client runs."""
    if not (scopes.scope_set.server or scopes.scope_set.client):
        return None
    if args.non_interactive or args.dry_run:
        return None
    try:
        _prompt_for_unmarked(config, logger)
    except Exception as exc:
        logger.error(f"config error: {exc}")
        return 3
    return None


def _build_runtime_or_exit(logger: Any) -> DockerRuntime | None:
    """Construct a DockerRuntime; return None on connection failure."""
    try:
        return DockerRuntime()
    except Exception as exc:
        logger.error(f"docker unavailable: {exc}")
        return None


def _load_protect_or_exit(config: DeploymentConfig, logger: Any) -> list[str] | None:
    """Load protect patterns; return None on failure (empty list is valid)."""
    try:
        return load_protect_patterns(config.protect_file)
    except Exception as exc:
        logger.error(f"config error: {exc}")
        return None


def _run_preflight_or_exit(
    config: DeploymentConfig,
    scopes: _Scopes,
    args: _Args,
    runtime: DockerRuntime,
    logger: Any,
) -> PreflightPlan | None:
    """Run preflight; return None on any exception."""
    try:
        return run_preflight(
            config=config,
            scopes=scopes.scope_set,
            with_resources=scopes.with_resources,
            notify=args.notify,
            dry_run=args.dry_run,
            runtime=runtime,
        )
    except Exception as exc:
        logger.error(f"preflight failed: {exc}")
        return None


def _emit_plan_warnings(plan: PreflightPlan, args: _Args, logger: Any) -> None:
    """Emit plan warnings at WARN, except under --dry-run (reported in the plan body)."""
    if args.dry_run:
        return
    for w in plan.warnings:
        logger.warning(w)


def _run_with_scope(args: _Args, remaining: list[str], logger: Any) -> int:
    """Config load -> prompt -> preflight -> runtime, in spec order (Section 2.4).

    Slip context is built before config load (spec Section 14.1). A slip failure
    exits 3, matching config/preflight failures.
    """
    config_dir = _resolve_config_dir(args.config_dir)

    try:
        slip_ctx = _build_slip_context(config_dir, args, logger)
    except ConfigError as exc:
        logger.error(f"slip error: {exc}")
        return 3

    config = _load_config_or_exit(
        config_dir,
        remaining,
        logger,
        slip=slip_ctx,
        requested_instances=args.instance,
    )
    if config is None:
        return 3

    scopes = _resolve_scopes(args)

    if args.debug_deps:
        _run_debug_deps_safe(config, logger)

    code = _prompt_if_needed(config, scopes, args, logger)
    if code is not None:
        return code

    runtime = _build_runtime_or_exit(logger)
    if runtime is None:
        return 3

    protect_patterns = _load_protect_or_exit(config, logger)
    if protect_patterns is None:
        return 3

    plan = _run_preflight_or_exit(config, scopes, args, runtime, logger)
    if plan is None:
        return 3

    _emit_plan_warnings(plan, args, logger)

    return _run_deployment(
        config=config,
        plan=plan,
        scopes=scopes,
        runtime=runtime,
        notify=args.notify,
        dry_run=args.dry_run,
        protect_patterns=protect_patterns,
        logger=logger,
    )


def _parse_cli(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[_Args | None, list[str]]:
    """Parse argv into (_Args, remaining)."""
    ns, remaining = parser.parse_known_args(argv)
    args = _Args(
        server=ns.server,
        client=ns.client,
        resource_pack=ns.resource_pack,
        full=ns.full,
        notify=ns.notify,
        dry_run=ns.dry_run,
        with_resources=ns.with_resources,
        instance=_parse_instances(ns.instance),
        debug=ns.debug,
        debug_deps=ns.debug_deps,
        audit_mods=ns.audit_mods,
        non_interactive=ns.non_interactive,
        config_dir=ns.config_dir,
        release=ns.release,
        release_pointer=ns.release_pointer,
        integration=ns.integration,
    )
    non_interactive_alone = args.non_interactive and not (args.server or args.client or args.resource_pack or args.full or args.audit_mods or args.debug_deps)
    if non_interactive_alone:
        return (None, remaining)
    return (args, remaining)


def _validate_cli(args: _Args, parser: argparse.ArgumentParser, remaining: list[str]) -> int | None:
    """Run the Section 2.5 exit-2 checks; return 2 on failure, None on success."""
    try:
        _validate_args(args, parser)
        _validate_passthrough_flags(remaining)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    return None


def _dispatch_audit_mods(args: _Args, config_dir: Path, remaining: list[str], logger: Any) -> int:
    """Section 2.7: standalone Textual audit UI."""
    if not getattr(prompt_ui, "HAS_TEXTUAL", False):
        print("error: --audit-mods requires the textual package", file=sys.stderr)
        return 1
    try:
        config = _load_config(config_dir, remaining)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    return prompt_ui.run_audit(config, logger)


def _dispatch_debug_deps_only(args: _Args, config_dir: Path, remaining: list[str], logger: Any) -> int:
    """Section 2.5: --debug-deps without a scope."""
    config = _load_config_or_exit(config_dir, remaining, logger)
    if config is None:
        return 3
    _run_debug_deps_safe(config, logger)
    if args.notify:
        try:
            _run_diagnostic_notify(config, logger)
        except Exception as exc:
            logger.error(f"diagnostic notify failed: {exc}")
    return 0


def _dispatch_diagnostic_only(args: _Args, config_dir: Path, remaining: list[str], logger: Any) -> int:
    """Section 2.5: --notify without a scope attempts the diagnostic message."""
    config = _load_config_or_exit(config_dir, remaining, logger)
    if config is None:
        return 3
    try:
        _run_diagnostic_notify(config, logger)
    except Exception as exc:
        logger.error(f"diagnostic notify failed: {exc}")
    return 0


def _dispatch_no_scope(
    args: _Args,
    config_dir: Path,
    remaining: list[str],
    logger: Any,
    parser: argparse.ArgumentParser,
) -> int:
    """Handle the three no-scope cases: debug-deps, notify, or plain help."""
    if args.debug_deps:
        return _dispatch_debug_deps_only(args, config_dir, remaining, logger)
    if args.notify:
        return _dispatch_diagnostic_only(args, config_dir, remaining, logger)
    parser.print_help()
    return 0


def _run(argv: list[str]) -> int:
    """Top-level entrypoint; returns the process exit code."""
    parser = _build_parser()
    if not argv:
        parser.print_help()
        return 0

    args, remaining = _parse_cli(parser, argv)
    if args is None:
        parser.print_help()
        return 0

    usage_code = _validate_cli(args, parser, remaining)
    if usage_code is not None:
        return usage_code

    logger = _setup_logging(args.debug)
    config_dir = _resolve_config_dir(args.config_dir)

    if args.audit_mods:
        return _dispatch_audit_mods(args, config_dir, remaining, logger)

    has_scope = args.server or args.client or args.resource_pack or args.full
    if not has_scope:
        return _dispatch_no_scope(args, config_dir, remaining, logger, parser)

    return _run_with_scope(args, remaining, logger)


# ===========================================================================
# Section 12  Entry point
# ===========================================================================


def main(argv: list[str] | None = None) -> int:
    """Process entrypoint; maps exceptions to Section 2.4's exit codes."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _run(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 0
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except DockerUnavailableError as exc:
        logging.getLogger("deploy_pack").error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (DockerRuntimeError, RuntimeDeployError) as exc:
        logging.getLogger("deploy_pack").error(str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ConfigError, PreflightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except DeployPackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    except Exception:
        traceback.print_exc()
        return 1
