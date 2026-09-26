# src/minecraft/deploy_pack/notifications.py

"""Discord notifications: template validation, rendering, and posting (Project_Specs.md §5).

Responsibilities (§9.2):
  * template validation per §5.11 - placeholder-set checking, empty
    template, unknown placeholder, invalid-everywhere, cross-template
  * rendering the four templates (§5.4-§5.7) with their placeholders
  * the §5.8 container-status vocabulary and its rendering
  * the §5.14 allowed_mentions payload
  * the §5.13 2000-char guard
  * posting via HTTP, best-effort, never authoritative over the
    deployment outcome (§5.12)

Dependency direction
--------------------

This module does not import preflight, hooks, or any scope. Those
modules call into notifications; the reverse would be circular (preflight
imports notifications for validation). Template validation therefore
returns ``list[tuple[str, str]]`` - (source-label, message) - and the
caller wraps them into its own diagnostic type. This is why
``PreflightFailure`` is not used here.

State vocabulary (§5.8)
-----------------------

Container states are represented as the literal strings §5.8 defines.
The deploy pipeline produces *facts* (StopOutcome, StartResult.success,
HealthResult.healthy); ``main`` maps those facts into these strings when
it builds a notification context. Keeping the strings here rather than
in hooks.py means hooks has no dependency on notification formatting.

Internal states ``cancelled`` and ``exited before stop`` are rendered
in CLI output but are dropped from Discord messages by
:func:`render_container_status`.

Pack-required warning in the server section (§4.6.9)
----------------------------------------------------

When ``pack_required`` is true and ``--client`` is not in scope, the
entrypoint collects a warning ("client pack content changed; the current
ZIP is stale. Run --client to rebuild.") and hands it to the live
notification. :func:`build_server_section` renders it as an informational
line in the server section, immediately after the ``Pack required`` line.
The parameter is optional; ``None`` suppresses the line.

Logging
-------

Module logger is ``minecraft.deploy_pack.notifications``. The module is
best-effort per §5.12, so it emits no ERROR: a failed post is a WARN,
a successful post is an INFO, and every intermediate step is a DEBUG.
Rendered message bodies are never logged - the templates carry player
role IDs and deployment details that should not land in a log sink.
Every DEBUG and INFO line records the *length* of the rendered content
or a category, not the content itself. ``_post`` is the single INFO
site for the post outcome, so exactly one "posted" or "skipped" line
appears per notification kind regardless of which helper dispatched
it. ``_default_poster`` is the HTTP boundary and logs transport-level
diagnostics at WARN (RequestException, HTTP >= 400). ``_check`` logs
its template-validation outcome at DEBUG and leaves severity to the
caller, which already aggregates failures before logging them.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.exceptions import RequestException

from .config_model import DiscordConfig
from .logging_setup import get_logger

_log = get_logger(__name__)
__all__ = [
    "DISCORD_CONTENT_LIMIT",
    "STATE_CANCELLED",
    "STATE_EXITED_BEFORE_STOP",
    "STATE_HEALTH_TIMEOUT",
    "STATE_ONLINE_HEALTHY",
    "STATE_ONLINE_STARTING",
    "STATE_ONLINE_UNHEALTHY",
    "STATE_RECOVERY_START_FAILED",
    "STATE_RECOVERY_START_SUCCEEDED",
    "STATE_RELOADED",
    "STATE_RELOAD_FAILED",
    "STATE_START_FAILED",
    "STATE_STOPPED",
    "STATE_STOPPED_BY_DEPLOYMENT",
    "DiagnosticContext",
    "FailureContext",
    "LiveContext",
    "NotifyKind",
    "NotifyResult",
    "OnlineContext",
    "build_client_section",
    "build_resource_pack_section",
    "build_server_section",
    "notify_diagnostic",
    "notify_failure",
    "notify_live",
    "notify_online",
    "render_container_status",
    "render_player_tags",
    "render_timestamp_now",
    "validate_diagnostic",
    "validate_live_and_failure",
    "validate_online",
]
DISCORD_CONTENT_LIMIT = 2000
STATE_ONLINE_HEALTHY = "online, healthy"
STATE_ONLINE_STARTING = "online, starting"
STATE_ONLINE_UNHEALTHY = "online, unhealthy"
STATE_STOPPED = "stopped"
STATE_STOPPED_BY_DEPLOYMENT = "stopped by deployment"
STATE_RECOVERY_START_FAILED = "recovery start failed"
STATE_RECOVERY_START_SUCCEEDED = "recovery start succeeded"
STATE_RELOADED = "reloaded"
STATE_RELOAD_FAILED = "reload failed"
STATE_START_FAILED = "start failed"
STATE_HEALTH_TIMEOUT = "health timeout"
STATE_CANCELLED = "cancelled"
STATE_EXITED_BEFORE_STOP = "exited before stop"
_INTERNAL_STATES = frozenset({STATE_CANCELLED, STATE_EXITED_BEFORE_STOP})
_LIVE_PLACEHOLDERS = frozenset(
    {
        "tool_version",
        "timestamp",
        "requested_scopes",
        "instance_count",
        "instance_list",
        "dry_run_marker",
        "player_tags",
        "operator_tags",
        "section_server",
        "section_client",
        "section_resource_pack",
    }
)
_ONLINE_PLACEHOLDERS = frozenset({"tool_version", "timestamp", "player_tags", "container_status"})
_FAILURE_PLACEHOLDERS = frozenset({"tool_version", "timestamp", "operator_tags", "failure_stage", "error", "container_status"})
_DIAGNOSTIC_PLACEHOLDERS = frozenset({"tool_version", "timestamp"})
_INVALID_EVERYWHERE = frozenset(
    {"debug_deps", "url", "sha1", "sha256sum", "pack_filename", "changelog_url", "summary", "deployment_status", "header", "footer", "note"}
)
_ALL_VALID = _LIVE_PLACEHOLDERS | _ONLINE_PLACEHOLDERS | _FAILURE_PLACEHOLDERS | _DIAGNOSTIC_PLACEHOLDERS
_PLACEHOLDER_RE = re.compile("\\{([^{}]*)\\}")


def _check(label: str, text: str | None, allowed: frozenset[str], logger: Any) -> list[tuple[str, str]]:
    """Validate one template. Returns (source, message) tuples.

    Missing template → warn, no failure.
    Empty template → failure.
    Malformed template → failure.

    "Malformed" per §5.11 means a placeholder that is invalid everywhere,
    unknown, or cross-template.
    """
    if logger is None:
        logger = _log
    if text is None:
        logger.warning(f"{label} is not configured; the notification will be skipped when it would fire")
        return []
    if not text:
        logger.debug(f"_check: {label} is present but empty")
        return [(label, "must be non-empty")]
    failures: list[tuple[str, str]] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        ph = m.group(1)
        if ph in _INVALID_EVERYWHERE:
            failures.append((label, f"placeholder {{{ph}}} is invalid everywhere (§11.3)"))
        elif ph not in _ALL_VALID:
            failures.append((label, f"unknown placeholder {{{ph}}}"))
        elif ph not in allowed:
            failures.append((label, f"placeholder {{{ph}}} is not valid in this template (cross-template use)"))
    if failures:
        logger.debug(f"_check: {label} produced {len(failures)} validation failure(s)")
    else:
        logger.debug(f"_check: {label} valid ({len(text)} chars)")
    return failures


def validate_live_and_failure(discord: DiscordConfig, *, notify: bool, dry_run: bool, has_scope: bool, logger: Any = None) -> list[tuple[str, str]]:
    """Validate templates reachable before restart_set is known (§5.11).

    Under ``--dry-run`` only the live template is validated. Otherwise
    live and failure are validated. With no scope, diagnostic is
    validated instead.
    """
    if logger is None:
        logger = _log
    if not notify:
        return []
    logger.debug(f"validate_live_and_failure: notify=True dry_run={dry_run} has_scope={has_scope}")
    if not has_scope:
        return _check("[discord.messages.diagnostic].template", discord.diagnostic_template, _DIAGNOSTIC_PLACEHOLDERS, logger)
    out = _check("[discord.messages.live].template", discord.live_template, _LIVE_PLACEHOLDERS, logger)
    if not dry_run:
        out.extend(_check("[discord.messages.failure].template", discord.failure_template, _FAILURE_PLACEHOLDERS, logger))
    if out:
        logger.debug(f"validate_live_and_failure: {len(out)} validation failure(s) total")
    return out


def validate_online(discord: DiscordConfig, logger: Any = None) -> list[tuple[str, str]]:
    """Validate the online template (§5.11, second pass)."""
    if logger is None:
        logger = _log
    logger.debug("validate_online: validating online template")
    return _check("[discord.messages.online].template", discord.online_template, _ONLINE_PLACEHOLDERS, logger)


def validate_diagnostic(discord: DiscordConfig, logger: Any = None) -> list[tuple[str, str]]:
    """Validate the diagnostic template (§5.11, no-scope path)."""
    if logger is None:
        logger = _log
    logger.debug("validate_diagnostic: validating diagnostic template")
    return _check("[discord.messages.diagnostic].template", discord.diagnostic_template, _DIAGNOSTIC_PLACEHOLDERS, logger)


def render_timestamp_now() -> str:
    """RFC 3339 with Z suffix (§5.4)."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_player_tags(role_ids: list[str], logger: Any = None) -> str:
    """Render role IDs as Discord role mentions, space-separated (§5.16)."""
    if logger is None:
        logger = _log
    logger.debug(f"render_player_tags: {len(role_ids)} role id(s)")
    return " ".join(f"<@&{rid}>" for rid in role_ids)


def render_container_status(statuses: dict[str, str], scope: str, logger: Any = None) -> str:
    """Render ``<instance>: <state>`` lines per §5.8.

    ``scope`` is ``"online"`` or ``"failure"``.

    Online: keeps only ``online, healthy`` entries. The caller is
    expected to pass only members of ``to_stop`` that came up healthy,
    but this is enforced here too.

    Failure: drops the internal states ``cancelled`` and
    ``exited before stop``, which are CLI-only.

    An empty result renders as ``- (none)`` so the section header is
    not left dangling.
    """
    if logger is None:
        logger = _log
    if scope == "online":
        filtered = [(name, state) for name, state in statuses.items() if state == STATE_ONLINE_HEALTHY]
    elif scope == "failure":
        filtered = [(name, state) for name, state in statuses.items() if state not in _INTERNAL_STATES]
    else:
        logger.error(f"render_container_status: invalid scope {scope!r}")
        raise ValueError(f"render_container_status: scope must be 'online' or 'failure', got {scope!r}")
    logger.debug(f"render_container_status [{scope}]: {len(statuses)} input -> {len(filtered)} rendered")
    if not filtered:
        return "- (none)"
    lines = [f"- {name}: {state}" for name, state in filtered]
    return "\n".join(lines)


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute every known placeholder; unknown placeholders are left as-is (validation should have caught them)."""
    merged = dict.fromkeys(_ALL_VALID, "")
    merged.update(values)
    return template.format_map(_SafeDict(merged))


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def build_server_section(
    *,
    targeted: bool,
    targeted_members: list[str] | None,
    mods_deployed: int | None,
    mods_skipped: bool,
    mods_drift: bool,
    config_kubejs_members: list[str],
    config_kubejs_changed: int,
    effective_action: str,
    pack_required: bool,
    reasons: list[tuple[str, int]],
    no_restart_performed: bool = False,
    pack_required_warning: str | None = None,
    logger: Any = None,
) -> str:
    """Render the server section (§4.6.10).

    Standard form:

        ### Server
        - Mods deployed: <n>
        - Config/KubeJS deployed: a, b
        - Restart action: <action>
        - Pack required: yes|no
        - Note: <pack_required_warning>          (only when supplied)
        - Reasons:
          - `<pattern>` changed (<n> path(s))

    Targeted form (``targeted=True``):

        ### Server
        - Targeted deploy to: a, b
        - Mods: not updated (targeted deploy does not touch shared mods)
        - Config/KubeJS: <n> files changed
        - Restart action: <action>
        - Pack required: yes|no
        - Note: <pack_required_warning>          (only when supplied)
        - Reasons:
          - `<pattern>` changed (<n> path(s))
        - No container restart performed

    ``pack_required_warning`` is populated by preflight when
    ``pack_required`` is true and ``--client`` is not in scope (§4.6.9).
    It is rendered as an informational ``- Note:`` line immediately
    after ``- Pack required:``. Passing ``None`` suppresses the line.
    """
    if logger is None:
        logger = _log
    logger.debug(
        f"build_server_section: targeted={targeted} mods_deployed={mods_deployed} "
        f"config_kubejs_members={len(config_kubejs_members)} reasons={len(reasons)} "
        f"pack_required={pack_required} pack_required_warning={pack_required_warning is not None}"
    )
    lines: list[str] = ["### Server"]
    if targeted:
        members = ", ".join(targeted_members or [])
        if members:
            lines.append(f"- Targeted deploy to: {members}")
        if mods_drift or mods_skipped:
            lines.append("- Mods: not updated (targeted deploy does not touch shared mods)")
        lines.append(f"- Config/KubeJS: {config_kubejs_changed} file(s) changed")
    else:
        if mods_deployed is not None:
            lines.append(f"- Mods deployed: {mods_deployed}")
        if config_kubejs_members:
            lines.append("- Config/KubeJS deployed: " + ", ".join(config_kubejs_members))
    lines.append(f"- Restart action: {effective_action}")
    lines.append(f"- Pack required: {('yes' if pack_required else 'no')}")
    if pack_required_warning is not None:
        lines.append(f"- Note: {pack_required_warning}")
    if reasons:
        lines.append("- Reasons:")
        for pattern, count in reasons:
            lines.append(f"  - `{pattern}` changed ({count} path(s))")
    if targeted and no_restart_performed:
        lines.append("- No container restart performed")
    logger.debug(f"build_server_section: rendered {len(lines)} line(s)")
    return "\n".join(lines)


def build_client_section(*, zip_filename: str | None, sha256: str | None, changelog_url: str | None, dry_run: bool, logger: Any = None) -> str:
    """Render the client section.

    §5.4 does not spell out the exact shape; this follows the same list
    style as the server and resource-pack sections.
    """
    if logger is None:
        logger = _log
    logger.debug(
        f"build_client_section: dry_run={dry_run} zip={'y' if zip_filename else 'n'} sha256={'y' if sha256 else 'n'} changelog={'y' if changelog_url else 'n'}"
    )
    lines: list[str] = ["### Client"]
    if dry_run:
        lines.append("- Status: dry-run (no ZIP written)")
        logger.debug(f"build_client_section: rendered {len(lines)} line(s) (dry-run)")
        return "\n".join(lines)
    if zip_filename:
        lines.append(f"- ZIP: {zip_filename}")
    if sha256:
        lines.append(f"- SHA-256: `{sha256}`")
    if changelog_url:
        lines.append(f"- Changelog: {changelog_url}")
    logger.debug(f"build_client_section: rendered {len(lines)} line(s)")
    return "\n".join(lines)


def build_resource_pack_section(
    *, configured: bool, published_filename: str | None, members: list[str], effective_action: str | None, logger: Any = None
) -> str:
    """Render the resource-pack section (§7.7)."""
    if logger is None:
        logger = _log
    logger.debug(f"build_resource_pack_section: configured={configured} published={'y' if published_filename else 'n'} members={len(members)}")
    lines: list[str] = ["### Resource Pack"]
    if not configured:
        lines.append("- Status: NOT CONFIGURED")
        logger.debug(f"build_resource_pack_section: rendered {len(lines)} line(s) (unconfigured)")
        return "\n".join(lines)
    if published_filename:
        lines.append(f"- Published: {published_filename}")
    if members:
        lines.append(f"- Instances: {', '.join(members)}")
    if effective_action:
        lines.append(f"- Restart action: {effective_action}")
    logger.debug(f"build_resource_pack_section: rendered {len(lines)} line(s)")
    return "\n".join(lines)


@dataclass
class LiveContext:
    """Represents the runtime context for a live operation, including tool version, timestamp, requested scopes, instance list, dry-run flag, roles, and section identifiers."""

    tool_version: str
    timestamp: str
    requested_scopes: list[str]
    instance_list: list[str]
    dry_run: bool = False
    player_roles: list[str] = field(default_factory=list)
    operator_roles: list[str] = field(default_factory=list)
    section_server: str = ""
    section_client: str = ""
    section_resource_pack: str = ""


@dataclass
class OnlineContext:
    """Holds contextual information for an online notification.

    Attributes:
        tool_version: Version of the tool reporting the online event.
        timestamp: Time at which the online event occurred.
        player_roles: Roles of the online players.
        container_status: Mapping of container names to their statuses.
    """

    tool_version: str
    timestamp: str
    player_roles: list[str] = field(default_factory=list)
    container_status: dict[str, str] = field(default_factory=dict)


@dataclass
class FailureContext:
    """Holds contextual information for a failure notification.

    Attributes:
        tool_version: Version of the tool reporting the failure.
        timestamp: Time at which the failure occurred.
        operator_roles: Roles of the operators involved.
        failure_stage: Stage in which the failure occurred.
        error: Error message describing the failure.
        container_status: Mapping of container names to their statuses.
    """

    tool_version: str
    timestamp: str
    operator_roles: list[str] = field(default_factory=list)
    failure_stage: str = ""
    error: str = ""
    container_status: dict[str, str] = field(default_factory=dict)


@dataclass
class DiagnosticContext:
    """Represents a diagnostic context containing tool version and timestamp."""

    tool_version: str
    timestamp: str


def _live_values(ctx: LiveContext) -> dict[str, str]:
    return {
        "tool_version": ctx.tool_version,
        "timestamp": ctx.timestamp,
        "requested_scopes": ", ".join(ctx.requested_scopes),
        "instance_list": ", ".join(ctx.instance_list),
        "instance_count": str(len(ctx.instance_list)),
        "dry_run_marker": "DRY RUN - no changes applied" if ctx.dry_run else "",
        "player_tags": render_player_tags(ctx.player_roles),
        "operator_tags": render_player_tags(ctx.operator_roles),
        "section_server": ctx.section_server,
        "section_client": ctx.section_client,
        "section_resource_pack": ctx.section_resource_pack,
    }


def render_live(template: str, ctx: LiveContext, logger: Any = None) -> str:
    """Renders a live template using the provided live context."""
    if logger is None:
        logger = _log
    content = _fill(template, _live_values(ctx))
    logger.debug(f"render_live: rendered {len(content)} chars; scopes={ctx.requested_scopes} instances={len(ctx.instance_list)} dry_run={ctx.dry_run}")
    return content


def render_online(template: str, ctx: OnlineContext, logger: Any = None) -> str:
    """Renders an online template with tool version, timestamp, player tags, and container status."""
    if logger is None:
        logger = _log
    content = _fill(
        template,
        {
            "tool_version": ctx.tool_version,
            "timestamp": ctx.timestamp,
            "player_tags": render_player_tags(ctx.player_roles),
            "container_status": render_container_status(ctx.container_status, "online"),
        },
    )
    logger.debug(f"render_online: rendered {len(content)} chars; containers={len(ctx.container_status)}")
    return content


def render_failure(template: str, ctx: FailureContext, logger: Any = None) -> str:
    """Renders a failure template with diagnostic details, error information, and container status."""
    if logger is None:
        logger = _log
    content = _fill(
        template,
        {
            "tool_version": ctx.tool_version,
            "timestamp": ctx.timestamp,
            "operator_tags": render_player_tags(ctx.operator_roles),
            "failure_stage": ctx.failure_stage,
            "error": ctx.error,
            "container_status": render_container_status(ctx.container_status, "failure"),
        },
    )
    logger.debug(f"render_failure: rendered {len(content)} chars; stage={ctx.failure_stage!r} containers={len(ctx.container_status)}")
    return content


def render_diagnostic(template: str, ctx: DiagnosticContext, logger: Any = None) -> str:
    """Renders a diagnostic template using the given diagnostic context."""
    if logger is None:
        logger = _log
    content = _fill(template, {"tool_version": ctx.tool_version, "timestamp": ctx.timestamp})
    logger.debug(f"render_diagnostic: rendered {len(content)} chars")
    return content


class NotifyKind:
    """Defines string constants for notification kinds."""

    LIVE = "live"
    ONLINE = "online"
    FAILURE = "failure"
    DIAGNOSTIC = "diagnostic"


@dataclass
class NotifyResult:
    """Represents the outcome of a notification operation.

    Attributes:
        kind: The notification kind.
        success: Whether the notification succeeded.
        error: Optional error message if the notification failed.
        skipped: Whether the notification was skipped.
        rendered_length: Length of the rendered notification content.
    """

    kind: str
    success: bool
    error: str | None = None
    skipped: bool = False
    rendered_length: int = 0


Poster = Callable[[str, str, dict[str, Any], float], tuple[bool, str | None]]


def _default_poster(webhook_url: str, content: str, allowed_mentions: dict[str, Any], timeout: float) -> tuple[bool, str | None]:
    """POST the payload to ``webhook_url`` via requests. Transport boundary.

    Logs the attempt at DEBUG with the payload length only; the payload
    itself carries player role IDs and deployment details and must not
    land in a log sink. Failure modes log at WARN because they are
    actionable to the operator running with ``--notify``.
    """
    payload = {"content": content, "allowed_mentions": allowed_mentions}
    _log.debug(f"_default_poster: POST timeout={timeout:g}s content_len={len(content)}")
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
    except RequestException as exc:
        _log.warning(f"_default_poster: request failed: {exc}")
        return (False, str(exc))
    if response.status_code >= 400:
        _log.warning(f"_default_poster: HTTP {response.status_code} ({response.text[:200]!r})")
        return (False, f"HTTP {response.status_code}: {response.text[:200]}")
    _log.debug(f"_default_poster: HTTP {response.status_code}")
    return (True, None)


def _post(
    webhook_url: str | None, content: str, allowed_mentions: dict[str, Any], kind: str, poster: Poster, logger: Any, timeout: float = 10.0
) -> NotifyResult:
    """Shared posting logic: guard, post, translate failure (§5.12, §5.13).

    Emits the single INFO line per notification outcome. ``kind`` is the
    NotifyKind constant; the content body is never logged, only its
    length.
    """
    if logger is None:
        logger = _log
    logger.debug(f"_post: kind={kind} content_len={len(content)} webhook_set={webhook_url is not None}")
    if len(content) > DISCORD_CONTENT_LIMIT:
        msg = f"rendered content is {len(content)} chars, exceeding the {DISCORD_CONTENT_LIMIT}-char limit (§5.13)"
        logger.warning(msg)
        return NotifyResult(kind=kind, success=False, error=msg, rendered_length=len(content))
    if not webhook_url:
        logger.warning(f"webhook_url is not configured; {kind} notification skipped")
        return NotifyResult(kind=kind, success=False, skipped=True)
    try:
        ok, err = poster(webhook_url, content, allowed_mentions, timeout)
    except Exception as exc:
        ok, err = (False, f"poster raised: {exc}")
        logger.debug(f"_post: {kind} poster raised: {exc}")
    if not ok:
        logger.warning(f"{kind} notification failed: {err}")
    else:
        logger.info(f"{kind} notification posted ({len(content)} chars)")
    return NotifyResult(kind=kind, success=ok, error=err, rendered_length=len(content))


def _allowed_mentions(ctx_roles: list[str]) -> dict[str, Any]:
    """§5.14: parse=[]; roles = union of all configured role IDs."""
    return {"parse": [], "roles": ctx_roles, "users": []}


def notify_live(
    template: str | None, ctx: LiveContext, webhook_url: str | None, all_role_ids: list[str], logger: Any = None, poster: Poster | None = None
) -> NotifyResult:
    """Render and post the live message (§5.4)."""
    if logger is None:
        logger = _log
    logger.debug(f"notify_live: template_set={template is not None} webhook_set={webhook_url is not None} roles={len(all_role_ids)}")
    if not template:
        logger.warning("live template is not configured; notification skipped")
        return NotifyResult(kind=NotifyKind.LIVE, success=False, skipped=True)
    content = render_live(template, ctx, logger)
    return _post(webhook_url, content, _allowed_mentions(all_role_ids), NotifyKind.LIVE, poster or _default_poster, logger)


def notify_online(
    template: str | None, ctx: OnlineContext, webhook_url: str | None, all_role_ids: list[str], logger: Any = None, poster: Poster | None = None
) -> NotifyResult:
    """Render and post the online message (§5.5)."""
    if logger is None:
        logger = _log
    logger.debug(f"notify_online: template_set={template is not None} webhook_set={webhook_url is not None} roles={len(all_role_ids)}")
    if not template:
        logger.warning("online template is not configured; notification skipped")
        return NotifyResult(kind=NotifyKind.ONLINE, success=False, skipped=True)
    content = render_online(template, ctx, logger)
    return _post(webhook_url, content, _allowed_mentions(all_role_ids), NotifyKind.ONLINE, poster or _default_poster, logger)


def notify_failure(
    template: str | None, ctx: FailureContext, webhook_url: str | None, all_role_ids: list[str], logger: Any = None, poster: Poster | None = None
) -> NotifyResult:
    """Render and post the failure message (§5.6)."""
    if logger is None:
        logger = _log
    logger.debug(
        f"notify_failure: template_set={template is not None} webhook_set={webhook_url is not None} roles={len(all_role_ids)} stage={ctx.failure_stage!r}"
    )
    if not template:
        logger.warning("failure template is not configured; notification skipped")
        return NotifyResult(kind=NotifyKind.FAILURE, success=False, skipped=True)
    content = render_failure(template, ctx, logger)
    return _post(webhook_url, content, _allowed_mentions(all_role_ids), NotifyKind.FAILURE, poster or _default_poster, logger)


def notify_diagnostic(
    template: str | None, ctx: DiagnosticContext, webhook_url: str | None, all_role_ids: list[str], logger: Any = None, poster: Poster | None = None
) -> NotifyResult:
    """Render and post the diagnostic message (§5.7)."""
    if logger is None:
        logger = _log
    logger.debug(f"notify_diagnostic: template_set={template is not None} webhook_set={webhook_url is not None} roles={len(all_role_ids)}")
    if not template:
        logger.warning("diagnostic template is not configured; notification skipped")
        return NotifyResult(kind=NotifyKind.DIAGNOSTIC, success=False, skipped=True)
    content = render_diagnostic(template, ctx, logger)
    return _post(webhook_url, content, _allowed_mentions(all_role_ids), NotifyKind.DIAGNOSTIC, poster or _default_poster, logger)
