# src/minecraft/deploy_pack/main.py

"""Entry point and runtime orchestration (Project_Specs.md §2, §4.1, §6.2, §9.2).

Responsibilities (§9.2):
  * argument parsing and §2.5's exit-2 matrix
  * scope resolution
  * partition resolution (delegated to config_model)
  * interactive prompt for unmarked jars (§6.2), when the operator has
    not passed --non-interactive
  * preflight invocation (delegated to preflight)
  * the §4.1 runtime sequence: notices, stop, writes, reload, live, start,
    health, online
  * notification dispatch via notifications.py
  * exit-code mapping per §2.4

Exit codes (§2.4):
  0 success
  1 runtime failure
  2 CLI usage error
  3 configuration / preflight failure

Error precedence: exit-2 checks run before config load; config load
before the interactive prompt; the prompt before preflight; preflight
before runtime. See :func:`_validate_args`, :func:`_validate_passthrough_flags`,
:func:`_run`, and the try/except hierarchy in :func:`main`.

Interactive prompt (§6.2, §6.3)
-------------------------------

Jars whose ``.pw.toml`` declares a ``side`` outside ``{client, server,
both}`` are "unmarked" (§6.3). When a server or client scope is active
and the operator has not passed ``--non-interactive``, the entrypoint
prompts for each such jar and records the answer as a
``[deployment_tool_review]`` entry in ``side_overrides.toml``. The
prompt runs before preflight so that preflight's mods-change computation
(§4.4) sees the newly-marked entries; it is skipped under ``--dry-run``
because §2.6 forbids filesystem writes.

Jars with *no* ``.pw.toml`` at all are also unmarked by §6.3, but they
cannot be fixed by writing a review entry - the index has nothing to
key against. The prompt warns about them and continues.

CLI failure output (§4.7, §4.8, §4.13)
---------------------------------------

The runtime failure paths emit structured diagnostics to stdout
regardless of ``--notify``:

  * **Failure summary line:** ``<failure_stage>: <error>`` on stdout.
  * **Per-container status:** one ``<instance_name>: <state>`` line per
    lifecycle-touched container, per §5.8's vocabulary. Rendered for
    non-server-scope write failures, reload failures, and post-phase
    failures. The server-scope write failure path emits the recovery
    block instead (which carries the affected containers and the reason).

Both are in addition to the logged diagnostics; the spec's "print ... to
CLI (always)" clauses are satisfied by stdout.

CLI warnings (§4.6.9)
---------------------

``preflight`` collects warnings (the pack-required staleness notice,
mods-drift on targeted server deploys, etc.) on ``PreflightPlan.warnings``.
The entrypoint emits each at WARN before starting the runtime sequence,
so the operator sees them in normal runs; ``--dry-run`` suppresses this
emission (warnings are surfaced as part of the dry-run plan body instead).

Logging
-------

Every application module emits exclusively through LoggingCore (§9.2's
"pluggable sinks" contract). The entrypoint wires LoggingCore with a
stderr sink and, separately, sets the stdlib root logger's level so
that tooling which reads ``logging.getLogger().level`` sees the
operator's intent. The two concerns are independent: LoggingCore owns
emission, stdlib owns level visibility.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import deps, notifications, prompt_ui, tool_version
from .config_model import DeploymentConfig, load_deployment_config
from .docker_runtime import Clock, DockerRuntime, RconTransport, select_rcon_transport
from .errors import ConfigError, DeployPackError, DockerRuntimeError, DockerUnavailableError, RuntimeDeployError, UsageError
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

# ---------------------------------------------------------------------------
# Config-override flag allowlist (§2.2, §3.1)
# ---------------------------------------------------------------------------

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
    """Build the argparse parser for deploy_pack's CLI (§2.2, §2.5)."""
    parser = argparse.ArgumentParser(prog="deploy_pack", description="Deploy Minecraft server + client pack.", add_help=True)
    parser.add_argument("--server", action="store_true", help="Server scope (§2.1)")
    parser.add_argument("--client", action="store_true", help="Client scope (§2.1)")
    parser.add_argument("--resource-pack", action="store_true", help="Resource-pack scope (§2.1)")
    parser.add_argument("--full", action="store_true", help="Server + client + resource-pack (§2.1)")
    parser.add_argument("--notify", action="store_true", help="Opt-in Discord notify (§2.2)")
    parser.add_argument("--dry-run", action="store_true", help="No writes, no lifecycle (§2.2, §2.6)")
    parser.add_argument("--with-resources", action="store_true", help="Include RP files in client ZIP (§2.2)")
    parser.add_argument("--instance", action="append", default=None, help="Target instances; repeatable / comma-separated (§2.9)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--debug-deps", action="store_true", help="Print dependency closure and exit / continue")
    parser.add_argument("--audit-mods", action="store_true", help="Standalone Textual UI (§6.1)")
    parser.add_argument("--non-interactive", action="store_true", help="Skip prompts; unmarked mods not deployed (§6.2)")
    parser.add_argument("--config-dir", type=str, default=None, help="Config directory (§2.2)")
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
    matching ``[deploy_pack.toml]``'s naming convention (§2.8). Unknown
    flags are caught earlier by :func:`_validate_passthrough_flags`; this
    function only runs on tokens that already passed that gate.

    Args:
        remaining: The leftover tokens from ``parse_known_args``.

    Returns:
        A mapping of snake_case key to string value.
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


def _validate_args(args: _Args, parser: argparse.ArgumentParser) -> None:
    """Raise UsageError for every §2.5 exit-2 condition."""
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
    """Resolve the _Scopes bundle from the parsed CLI surface (§2.1)."""
    if args.full:
        return _Scopes(ScopeSet(server=True, client=True, resource_pack=True), with_resources=True)
    return _Scopes(ScopeSet(server=args.server, client=args.client, resource_pack=args.resource_pack), with_resources=args.with_resources)


def _setup_logging(debug: bool) -> Any:
    """Configure logging and return the module logger."""
    level = logging.DEBUG if debug else logging.INFO
    logging.getLogger().setLevel(level)
    _configure_logging(debug=debug)
    return _get_loggingcore_logger("deploy_pack")


def _sticky_max(actions: list[str]) -> str:
    """Return the §4.6.2 sticky-max over ``actions``."""
    return sticky_max(actions)


def _resolve_config_dir(raw: str | None) -> Path:
    """Return the config directory as a filesystem path."""
    if raw:
        p = Path(raw)
        if p.is_file():
            p = p.parent
        return p
    return Path("config.d")


def _load_config(config_dir: Path, overrides: list[str]) -> DeploymentConfig:
    """Load the deployment config, forwarding CLI overrides as kwargs.

    Args:
        config_dir: The resolved config directory.
        overrides: The leftover ``--flag value`` tokens from the CLI.

    Returns:
        The resolved deployment configuration.

    Raises:
        ConfigError: On any config-loading failure.
    """
    parsed = _parse_overrides(overrides)
    return load_deployment_config(config_dir=config_dir, **parsed)


def _prompt_for_unmarked(config: DeploymentConfig, logger: Any) -> None:
    """Walk unmarked jars, prompt for a side, write review entries."""
    index_dir = Path(config.modpack_dir) / ".index"
    if not index_dir.is_dir():
        return
    entries = deps.load_prism_index(index_dir)
    unmarked = deps.find_unmarked(config.modpack_dir, entries)
    if not unmarked:
        return
    indexed_files = {str(e.get("file", "")) for e in entries if e.get("file")}
    fixable = [u for u in unmarked if u.filename in indexed_files]
    unfixable = [u for u in unmarked if u.filename not in indexed_files]
    if unfixable:
        names = ", ".join(u.filename for u in unfixable)
        logger.warning(f"{len(unfixable)} unmarked jar(s) have no .pw.toml and cannot be fixed here; they will not be deployed. Files: {names}")
    if not fixable:
        return
    if not sys.stdin.isatty():
        logger.warning(
            f"{len(fixable)} unmarked jar(s) need review but stdin is not a TTY; skipping the interactive prompt. Pass --non-interactive to acknowledge, or run in a terminal."
        )
        return
    overrides_path = Path(config.config_dir) / "side_overrides.toml"
    existing = load_side_overrides(overrides_path)
    review = dict(existing.deployment_tool_review)
    logger.info(f"Prompting for {len(fixable)} unmarked jar(s). Answers are written to {overrides_path}.")
    print()
    for jar in fixable:
        print(f"Unmarked: {jar.filename}")
        print(f"  reason: {jar.reason}")
        while True:
            try:
                answer = input("  side? [s]erver / [c]lient / [b]oth / [k]skip / [d]efer: ")
            except EOFError:
                answer = "d"
            answer = answer.strip().lower()
            if answer == "s":
                review[jar.filename] = "server"
                break
            if answer == "c":
                review[jar.filename] = "client"
                break
            if answer == "b":
                review[jar.filename] = "both"
                break
            if answer == "k":
                review[jar.filename] = "skipped"
                break
            if answer in ("d", ""):
                break
            print("  Invalid answer. Try again.")
    save_side_overrides(overrides_path, review, logger=logger)
    logger.info(f"Wrote {len(review)} review entr(ies) to {overrides_path}")


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


def _run_debug_deps(config: DeploymentConfig, logger: Any) -> None:
    """Print dependency closure for both sides (§2.5)."""
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
    """Attempt the diagnostic Discord message (§5.2)."""
    ctx = notifications.DiagnosticContext(tool_version=tool_version(), timestamp=notifications.render_timestamp_now())
    all_roles = config.discord.player_roles + config.discord.operator_roles
    notifications.notify_diagnostic(config.discord.diagnostic_template, ctx, config.webhook_url, all_roles, logger=logger)


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


def _build_server_section(plan: PreflightPlan) -> str:
    """Render the live notification's server section (§4.6.10)."""
    targeted = plan.targeted
    mods_deployed = None
    if plan.mods_change is not None and (not targeted):
        mods_deployed = len(plan.mods_change.added) + len(plan.mods_change.updated)
    config_members: list[str] = []
    config_changed_count = 0
    for member in plan.partition:
        mp = plan.member_plans.get(member)
        if mp is None:
            continue
        ck = [p for p in mp.changed_paths if not p.startswith("mods/")]
        if ck:
            config_members.append(member)
            config_changed_count += len(ck)
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
    """Render the live notification's client section (§5.4)."""
    if dry_run or client_result is None:
        return notifications.build_client_section(zip_filename=None, sha256=None, changelog_url=None, dry_run=True)
    return notifications.build_client_section(
        zip_filename=client_result.resolved_output_filename, sha256=client_result.zip_sha256, changelog_url=client_result.changelog_url, dry_run=False
    )


def _build_rp_section(plan: PreflightPlan, rp_result: ResourcePackScopeResult | None) -> str:
    """Render the live notification's resource-pack section (§7.7)."""
    configured = any(member in plan.member_plans and plan.member_plans[member].resource_pack_target for member in plan.partition) or bool(
        plan.partition and rp_result and rp_result.publish_results
    )
    if not configured:
        return notifications.build_resource_pack_section(configured=False, published_filename=None, members=[], effective_action=None)
    filename = None
    if rp_result and rp_result.publish_results:
        filename = rp_result.publish_results[0].filename
    return notifications.build_resource_pack_section(configured=True, published_filename=filename, members=list(plan.partition), effective_action=None)


def _send_live(
    config: DeploymentConfig,
    plan: PreflightPlan,
    scopes: _Scopes,
    client_result: ClientScopeResult | None,
    rp_result: ResourcePackScopeResult | None,
    dry_run: bool,
    logger: Any,
) -> notifications.NotifyResult:
    """Render and post the live notification (§5.4)."""
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


def _send_failure(config: DeploymentConfig, failure_stage: str, error: str, container_status: dict[str, str], logger: Any) -> notifications.NotifyResult:
    """Render and post the failure notification (§5.6)."""
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
    """Render and post the online notification (§5.5)."""
    ctx = notifications.OnlineContext(
        tool_version=tool_version(), timestamp=notifications.render_timestamp_now(), player_roles=config.discord.player_roles, container_status=container_status
    )
    all_roles = config.discord.player_roles + config.discord.operator_roles
    return notifications.notify_online(config.discord.online_template, ctx, config.webhook_url, all_roles, logger=logger)


def _build_recovery_ctx(config: DeploymentConfig, rcon_set: _RconSet, cancel_message: str, clock: Clock | None = None) -> RecoveryContext:
    """Assemble a RecoveryContext around the given RCON set."""
    probe: ReachabilityProbeFn = rcon_set.probe

    def cancel(names: list[str]) -> None:
        rcon_set.dispatch_cancel(names, cancel_message)

    return RecoveryContext(
        reachability_probe=probe,
        cancel_notice_fn=cancel,
        cancel_ready_timeout=float(config.docker.cancel_notice_ready_timeout_seconds),
        poll_interval=float(config.docker.health_poll_seconds),
        clock=clock if clock is not None else Clock(),
    )


@dataclass
class _NoticeOutcome:
    attempted: list[str]
    delivered: list[str]
    failed: list[str]

    @property
    def any_delivered(self) -> bool:
        """Return True if at least one notice was delivered."""
        return bool(self.delivered)


def _dispatch_restart_notices(config: DeploymentConfig, warned_and_running: list[str], rcon_set: _RconSet, logger: Any) -> _NoticeOutcome:
    """Dispatch the in-game restart notice to warned-and-running members (§4.14 step 4)."""
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
    """Dispatch the in-game cancel notice to the given recipients (§8.7)."""
    if not recipients:
        return
    msg = config.docker.restart_cancel_notice_template
    rcon_set.dispatch_cancel([config.instances[m].container for m in recipients if m in config.instances], msg)


def _online_container_status(plan: PreflightPlan, to_stop: list[str], post: PostHookResult) -> dict[str, str]:
    """§5.8 online: containers successfully stopped and brought up healthy."""
    out: dict[str, str] = {}
    for member in to_stop:
        if member in post.started and member not in post.start_failed and member not in post.health_failed:
            out[member] = notifications.STATE_ONLINE_HEALTHY
    return out


def _failure_container_status(
    plan: PreflightPlan, pre: PreHookResult | None, post: PostHookResult | None, reloaded: list[str], reload_failed: list[str]
) -> dict[str, str]:
    """§5.8 failure: all lifecycle-touched containers, plus internal states."""
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
    """Print per-container status to CLI, format per §5.8."""
    for member in sorted(status):
        print(f"{member}: {status[member]}")


def _run_writes(
    config: DeploymentConfig, plan: PreflightPlan, scopes: _Scopes, protect_patterns: list[str], logger: Any
) -> tuple[bool, ServerScopeResult | None, ClientScopeResult | None, ResourcePackScopeResult | None, str | None]:
    """Perform scopes in §4.2's order, halting on first failure."""
    server_result: ServerScopeResult | None = None
    client_result: ClientScopeResult | None = None
    rp_result: ResourcePackScopeResult | None = None
    if scopes.scope_set.server:
        server_result = deploy_server_scope(config, plan, protect_patterns, logger)
        if not server_result.success:
            return (False, server_result, None, None, server_result.failure_message or "server scope failed")
    if scopes.scope_set.client:
        client_result = deploy_client_scope(config, scopes.with_resources, protect_patterns, logger)
        if not client_result.success:
            return (False, server_result, client_result, None, client_result.failure_message or "client scope failed")
    if scopes.scope_set.resource_pack:
        rp_result = deploy_resource_pack_scope(config, plan, protect_patterns, logger)
        if not rp_result.success:
            return (False, server_result, client_result, rp_result, rp_result.failure_message or "resource-pack scope failed")
    return (True, server_result, client_result, rp_result, None)


def _print_server_write_failure_recovery(config: DeploymentConfig, to_stop: list[str], failure_summary: str) -> None:
    """Print the §4.7 server-scope write-failure recovery block to CLI."""
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


def _print_recovery_block(config: DeploymentConfig, recovery: RecoveryResult, container_states: dict[str, Any], reason: str) -> None:
    """Print the §8.8 recovery block after a failed pre-hook stop."""
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
    """Execute the §4.1 runtime sequence. Returns the exit code."""
    if not scopes.scope_set.any():
        return 0
    if dry_run:
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
        for w in plan.warnings:
            logger.warning(f"  {w}")
        logger.info("=" * 72)
        if notify:
            _send_live(config, plan, scopes, None, None, True, logger)
        return 0
    rcon_set = _RconSet(config, runtime, logger)
    recovery_ctx = _build_recovery_ctx(config, rcon_set, config.docker.restart_cancel_notice_template)
    warned_and_running: list[str] = []
    if plan.restart_set:
        warned_and_running = compute_warned_and_running(runtime, plan.restart_set, plan.container_states, logger=logger)
    pre: PreHookResult | None = None
    to_stop: list[str] = []
    if warned_and_running:
        outcome = _dispatch_restart_notices(config, warned_and_running, rcon_set, logger)
        if outcome.failed and config.docker.in_game_notice_required:
            _dispatch_cancel_notice(config, outcome.attempted, rcon_set, logger)
            print("in_game_notice: RCON notice failed for: " + ", ".join(outcome.failed))
            logger.error("restart notice dispatch failed; aborting before any writes")
            return 1
        if outcome.any_delivered:
            wait_s = config.docker.restart_wait_seconds
            if wait_s > 0:
                logger.info(f"waiting {wait_s}s for in-game restart window")
                Clock().sleep(float(wait_s))
    if plan.restart_set:
        stop_timeouts: dict[str, int] = {}
        for member in plan.restart_set:
            inst = config.instances.get(member)
            if inst is not None:
                stop_timeouts[member] = inst.stop_grace_seconds
        pre = execute_pre_hook(runtime, warned_and_running, stop_timeouts, recovery_ctx, logger=logger)
        to_stop = list(pre.stopped)
        if pre.failed:
            logger.error(f"stop phase failed for: {', '.join(pre.failed)}")
            print("pre_hook: docker stop failed for: " + ", ".join(pre.failed))
            if pre.recovery is not None and pre.recovery.any_failure:
                _print_recovery_block(config, pre.recovery, plan.container_states, "stop phase failure")
            return 1
    write_ok, _server_result, client_result, rp_result, write_msg = _run_writes(config, plan, scopes, protect_patterns, logger)
    if not write_ok:
        logger.error(f"write failed: {write_msg}")
        if scopes.scope_set.server:
            if notify:
                status = _failure_container_status(plan, pre, None, [], [])
                _send_failure(config, "mid_scope_write", write_msg, status, logger)
            if to_stop:
                _print_server_write_failure_recovery(config, to_stop, write_msg)
            else:
                print(f"mid_scope_write: {write_msg}")
            return 1
        recovery = recover_stopped_containers(
            runtime,
            stopped_by_deployment=to_stop,
            warned_and_running=warned_and_running,
            ctx=recovery_ctx,
            logger=logger,
        )
        if pre is not None:
            pre.recovery = recovery
        status = _failure_container_status(plan, pre, None, [], [])
        if notify:
            _send_failure(config, "mid_scope_write", write_msg, status, logger)
        print(f"mid_scope_write: {write_msg}")
        _print_cli_container_status(status)
        return 1

    # Reload phase (§4.6.8, §4.8).
    reloaded: list[str] = []
    reload_failed: list[str] = []
    if plan.reload_set:
        for member in sorted(plan.reload_set):
            inst = config.instances.get(member)
            if inst is None:
                continue
            state = plan.container_states.get(member)
            if state is not None and not state.is_running:
                continue
            if rcon_set.send(inst.container, "reload"):
                reloaded.append(member)
            else:
                reload_failed.append(member)
        if reload_failed:
            msg = "RCON reload failed for: " + ", ".join(reload_failed)
            logger.error(msg)
            recovery = recover_stopped_containers(
                runtime,
                stopped_by_deployment=to_stop,
                warned_and_running=warned_and_running,
                ctx=recovery_ctx,
                logger=logger,
            )
            if pre is not None:
                pre.recovery = recovery
            status = _failure_container_status(plan, pre, None, reloaded, reload_failed)
            if notify:
                _send_failure(config, "reload", msg, status, logger)
            print(f"reload: {msg}")
            _print_cli_container_status(status)
            return 1

    # Live notification (§5.4) before the post-phase start/health cycle.
    if notify:
        _send_live(config, plan, scopes, client_result, rp_result, False, logger)

    # Post phase: start stopped containers, wait for health (§4.13, §4.14).
    if plan.restart_set:
        post = execute_post_hook(
            runtime,
            to_stop,
            config.docker.health_timeout_seconds,
            plan.container_states,
            poll_interval=config.docker.health_poll_seconds,
            logger=logger,
        )
        if post.start_failed or post.health_failed:
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
            logger.error(err)
            status = _failure_container_status(plan, pre, post, reloaded, reload_failed)
            if notify:
                _send_failure(config, stage, err, status, logger)
            print(f"{stage}: {err}")
            _print_cli_container_status(status)
            return 1
        online_status = _online_container_status(plan, to_stop, post)
        if notify and online_status:
            _send_online(config, online_status, logger)
        return 0
    return 0


def _run_with_scope(args: _Args, remaining: list[str], logger: Any) -> int:
    """Config load -> prompt -> preflight -> runtime, in spec order (§2.4)."""
    config_dir = _resolve_config_dir(args.config_dir)
    try:
        config = _load_config(config_dir, remaining)
    except Exception as exc:
        logger.error(f"config error: {exc}")
        return 3

    scopes = _resolve_scopes(args)

    if args.debug_deps:
        try:
            _run_debug_deps(config, logger)
        except Exception as exc:
            logger.error(f"debug-deps failed: {exc}")

    if (scopes.scope_set.server or scopes.scope_set.client) and not args.non_interactive and not args.dry_run:
        try:
            _prompt_for_unmarked(config, logger)
        except Exception as exc:
            logger.error(f"config error: {exc}")
            return 3

    try:
        runtime = DockerRuntime()
    except Exception as exc:
        logger.error(f"docker unavailable: {exc}")
        return 3

    try:
        protect_patterns = load_protect_patterns(config.protect_file)
    except Exception as exc:
        logger.error(f"config error: {exc}")
        return 3

    try:
        plan = run_preflight(
            config=config,
            scopes=scopes.scope_set,
            with_resources=scopes.with_resources,
            notify=args.notify,
            dry_run=args.dry_run,
            runtime=runtime,
        )
    except Exception as exc:
        logger.error(f"preflight failed: {exc}")
        return 3

    # §4.6.9: warnings go to the CLI at WARN during normal runs; the
    # dry-run plan body carries them under the DRY RUN header instead.
    if not args.dry_run:
        for w in plan.warnings:
            logger.warning(w)

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


def _run(argv: list[str]) -> int:
    """Top-level entrypoint. Returns the process exit code."""
    parser = _build_parser()
    if not argv:
        parser.print_help()
        return 0

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
    )

    if args.non_interactive and not (args.server or args.client or args.resource_pack or args.full or args.audit_mods or args.debug_deps):
        parser.print_help()
        return 0

    try:
        _validate_args(args, parser)
        _validate_passthrough_flags(remaining)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2

    logger = _setup_logging(args.debug)
    config_dir = _resolve_config_dir(args.config_dir)

    if args.audit_mods:
        if not getattr(prompt_ui, "HAS_TEXTUAL", False):
            print("error: --audit-mods requires the textual package", file=sys.stderr)
            return 1
        try:
            config = _load_config(config_dir, remaining)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        return prompt_ui.run_audit(config, logger)

    has_scope = args.server or args.client or args.resource_pack or args.full

    if args.debug_deps and not has_scope:
        try:
            config = _load_config(config_dir, remaining)
        except Exception as exc:
            logger.error(f"config error: {exc}")
            return 3
        try:
            _run_debug_deps(config, logger)
        except Exception as exc:
            logger.error(f"debug-deps failed: {exc}")
        if args.notify:
            try:
                _run_diagnostic_notify(config, logger)
            except Exception as exc:
                logger.error(f"diagnostic notify failed: {exc}")
        return 0

    if args.notify and not has_scope:
        try:
            config = _load_config(config_dir, remaining)
        except Exception as exc:
            logger.error(f"config error: {exc}")
            return 3
        try:
            _run_diagnostic_notify(config, logger)
        except Exception as exc:
            logger.error(f"diagnostic notify failed: {exc}")
        return 0

    if not has_scope:
        parser.print_help()
        return 0

    return _run_with_scope(args, remaining, logger)


def main(argv: list[str] | None = None) -> int:
    """Process entrypoint. Maps exceptions to §2.4's exit codes.

    Ordering of the ``except`` clauses matters: ``DockerUnavailableError``
    and its siblings are caught before ``ConfigError`` because the docker
    exception types may inherit from the config hierarchy (or share a base
    with it), and a docker-availability failure is exit 1 regardless.

    Docker failures are emitted through the stdlib logger so that
    ``caplog`` sees them; the CLI also writes a short summary to stderr.
    Config failures are stderr-only, matching the config-load path's
    ``print`` style.

    ``SystemExit`` is caught so argparse's ``--help`` path (``sys.exit(0)``)
    does not propagate; an int payload becomes the return code, ``None``
    and string payloads map to 0.
    """
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
