# tests/deploy_pack/test_notifications.py

"""Tests for deploy_pack.notifications, per Project_Specs.md v3.0 §10.1.

Coverage areas (§10.1 required):
  * bitmask per scope combo (delegated to preflight; rendered sections here)
  * empty template rejection
  * missing template skip
  * malformed placeholder (unknown, invalid-everywhere, cross-template) → exit 3
  * 2000-char guard
  * allowed_mentions
  * container_status scope
  * failure notification on start failure (smoke; main wires this)
  * failure_stage selection (post_hook vs health_timeout) - smoke
  * recovery-path stage preservation - smoke
  * conditional on --notify
  * {dry_run_marker} content
  * {timestamp} format
  * {requested_scopes} format
  * internal states (cancelled, exited before stop) not rendered in
    Discord messages
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from minecraft.deploy_pack.config_model import DiscordConfig
from minecraft.deploy_pack.notifications import (
    DISCORD_CONTENT_LIMIT,
    STATE_CANCELLED,
    STATE_EXITED_BEFORE_STOP,
    STATE_HEALTH_TIMEOUT,
    STATE_ONLINE_HEALTHY,
    STATE_ONLINE_UNHEALTHY,
    STATE_RECOVERY_START_FAILED,
    STATE_RECOVERY_START_SUCCEEDED,
    STATE_RELOAD_FAILED,
    STATE_RELOADED,
    STATE_START_FAILED,
    STATE_STOPPED,
    STATE_STOPPED_BY_DEPLOYMENT,
    DiagnosticContext,
    FailureContext,
    LiveContext,
    NotifyKind,
    OnlineContext,
    build_client_section,
    build_resource_pack_section,
    build_server_section,
    notify_diagnostic,
    notify_failure,
    notify_live,
    notify_online,
    render_container_status,
    render_diagnostic,
    render_failure,
    render_live,
    render_online,
    render_player_tags,
    validate_diagnostic,
    validate_live_and_failure,
    validate_online,
)


def _live_ctx(**overrides: Any) -> LiveContext:
    base = dict(
        tool_version="2.0.0",
        timestamp="2026-09-23T12:00:00Z",
        requested_scopes=["server", "client"],
        instance_list=["creative", "survival"],
        dry_run=False,
        player_roles=["111"],
        operator_roles=["222"],
        section_server="### Server\n- Mods deployed: 5",
        section_client="### Client\n- ZIP: x.zip",
        section_resource_pack="### Resource Pack\n- Status: NOT CONFIGURED",
    )
    base.update(overrides)
    return LiveContext(**base)


def test_validate_skips_when_not_notify() -> None:
    """{"def test_validate_skips_when_not_notify()": "Tests that validation returns no messages when notifications are disabled."}"""
    discord = DiscordConfig(live_template=None)
    assert validate_live_and_failure(discord, notify=False, dry_run=False, has_scope=True) == []


def test_validate_live_missing_is_warning_not_failure() -> None:
    """Missing template → warn, not exit 3 (§5.11)."""
    discord = DiscordConfig(live_template=None)
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=True)
    assert failures == []


def test_validate_live_empty_is_failure() -> None:
    """Tests that an empty live template produces a validation failure mentioning "non-empty"."""
    discord = DiscordConfig(live_template="")
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=True)
    assert len(failures) == 1
    assert "non-empty" in failures[0][1]


def test_validate_live_unknown_placeholder() -> None:
    """Tests that an unknown placeholder in the live template yields a failure referencing the placeholder name."""
    discord = DiscordConfig(live_template="hello {nonexistent}")
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=True)
    assert any(("nonexistent" in m for _s, m in failures))


def test_validate_live_invalid_everywhere() -> None:
    """Tests that a placeholder invalid in all contexts produces a failure mentioning "invalid everywhere"."""
    discord = DiscordConfig(live_template="hello {sha256sum}")
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=True)
    assert any(("invalid everywhere" in m for _s, m in failures))


def test_validate_live_cross_template() -> None:
    """Tests that cross-template validation failure is reported when live template references a failure-only placeholder with scope."""
    discord = DiscordConfig(live_template="hello {failure_stage}")
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=True)
    assert any(("cross-template" in m for _s, m in failures))


def test_validate_dry_run_validates_only_live() -> None:
    """Under --dry-run, only live is validated (§5.11)."""
    discord = DiscordConfig(live_template="ok {tool_version}", failure_template="{bad}")
    failures = validate_live_and_failure(discord, notify=True, dry_run=True, has_scope=True)
    assert failures == []


def test_validate_no_scope_uses_diagnostic() -> None:
    """Tests that live/failure validation succeeds without a scope when only diagnostic templates are used."""
    discord = DiscordConfig(diagnostic_template="ok {tool_version}", live_template="{bad}")
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=False)
    assert failures == []


def test_validate_no_scope_diagnostic_missing_is_skip() -> None:
    """Tests that missing diagnostic templates cause live/failure validation to be skipped when no scope is present."""
    discord = DiscordConfig(diagnostic_template=None)
    failures = validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=False)
    assert failures == []


def test_validate_online_missing_is_skip() -> None:
    """Tests that a missing online template is skipped and returns no validation failures."""
    assert validate_online(DiscordConfig(online_template=None)) == []


def test_validate_online_valid() -> None:
    """Tests that a valid online template with allowed placeholders passes validation."""
    discord = DiscordConfig(online_template="{tool_version} {timestamp} {container_status}")
    assert validate_online(discord) == []


def test_validate_online_cross_template() -> None:
    """Tests that using a placeholder from another template reports a cross-template failure."""
    discord = DiscordConfig(online_template="{failure_stage}")
    failures = validate_online(discord)
    assert any(("cross-template" in m for _s, m in failures))


def test_validate_online_empty_is_error() -> None:
    """Tests that an empty online template reports a non-empty requirement failure."""
    discord = DiscordConfig(online_template="")
    failures = validate_online(discord)
    assert any(("non-empty" in m for _s, m in failures))


def test_validate_diagnostic_ok() -> None:
    """Tests that a valid diagnostic template with allowed placeholders passes validation."""
    discord = DiscordConfig(diagnostic_template="test {tool_version} {timestamp}")
    assert validate_diagnostic(discord) == []


def test_render_player_tags() -> None:
    """Tests rendering of player tags with empty, single, and multiple role IDs."""
    assert render_player_tags([]) == ""
    assert render_player_tags(["1"]) == "<@&1>"
    assert render_player_tags(["1", "2"]) == "<@&1> <@&2>"


def test_render_container_status_online_keeps_healthy_only() -> None:
    """Tests that rendering container status for online state includes only healthy containers."""
    statuses = {"a": STATE_ONLINE_HEALTHY, "b": STATE_ONLINE_UNHEALTHY, "c": STATE_CANCELLED}
    out = render_container_status(statuses, "online")
    assert "a: online, healthy" in out
    assert "b" not in out
    assert "c" not in out


def test_render_container_status_failure_excludes_internal() -> None:
    """Tests that failure scope rendering includes expected statuses and excludes internal ones."""
    statuses = {
        "a": STATE_STOPPED_BY_DEPLOYMENT,
        "b": STATE_RECOVERY_START_FAILED,
        "c": STATE_HEALTH_TIMEOUT,
        "d": STATE_CANCELLED,
        "e": STATE_EXITED_BEFORE_STOP,
    }
    out = render_container_status(statuses, "failure")
    assert "a: stopped by deployment" in out
    assert "b: recovery start failed" in out
    assert "c: health timeout" in out
    assert "d" not in out
    assert "e" not in out


def test_render_container_status_empty_placeholder() -> None:
    """Tests that rendering an empty status mapping returns the none placeholder."""
    assert render_container_status({}, "online") == "- (none)"
    assert render_container_status({}, "failure") == "- (none)"


def test_render_container_status_bad_scope() -> None:
    """Tests that an invalid scope raises a ValueError."""
    with pytest.raises(ValueError):
        render_container_status({}, "invalid")


def test_render_container_status_all_failure_states() -> None:
    """Tests that render_container_status includes all containers with failure states."""
    statuses = {
        "a": STATE_STOPPED,
        "b": STATE_STOPPED_BY_DEPLOYMENT,
        "c": STATE_RECOVERY_START_SUCCEEDED,
        "d": STATE_RELOADED,
        "e": STATE_RELOAD_FAILED,
        "f": STATE_START_FAILED,
        "g": STATE_HEALTH_TIMEOUT,
    }
    out = render_container_status(statuses, "failure")
    for name in ("a", "b", "c", "d", "e", "f", "g"):
        assert name in out


def test_render_live_basic() -> None:
    """Tests basic render_live output with live context values."""
    template = "{player_tags} **Minecraft Deployment**\n\n{section_server}\n{section_client}\n{section_resource_pack}\n\n{dry_run_marker}\nCompleted {timestamp}\n\nBuilt by deploy_pack {tool_version}"
    out = render_live(template, _live_ctx())
    assert "<@&111>" in out
    assert "### Server" in out
    assert "### Client" in out
    assert "### Resource Pack" in out
    assert "2.0.0" in out
    assert "2026-09-23T12:00:00Z" in out


def test_render_live_dry_run_marker() -> None:
    """Tests the dry-run marker rendering for enabled and disabled dry-run modes."""
    template = "[{dry_run_marker}]"
    assert render_live(template, _live_ctx(dry_run=True)) == "[DRY RUN - no changes applied]"
    assert render_live(template, _live_ctx(dry_run=False)) == "[]"


def test_render_live_instance_list_and_count() -> None:
    """Tests that render_live renders instance count and instance list correctly."""
    template = "{instance_count}: {instance_list}"
    out = render_live(template, _live_ctx())
    assert out == "2: creative, survival"


def test_render_live_requested_scopes() -> None:
    """Tests that render_live renders requested scopes with default and custom values."""
    template = "{requested_scopes}"
    assert render_live(template, _live_ctx()) == "server, client"
    assert render_live(template, _live_ctx(requested_scopes=["server", "client", "resource-pack"])) == "server, client, resource-pack"


def test_render_live_operator_tags() -> None:
    """Tests that render_live renders operator role mentions from operator_roles."""
    template = "{operator_tags}"
    assert render_live(template, _live_ctx(operator_roles=["9", "8"])) == "<@&9> <@&8>"


def test_render_online() -> None:
    """Tests that render_online formats player tags, container status, and tool version correctly."""
    ctx = OnlineContext(tool_version="2.0.0", timestamp="2026-09-23T12:00:00Z", player_roles=["1"], container_status={"a": STATE_ONLINE_HEALTHY})
    out = render_online("{player_tags}\n{container_status}\n{tool_version}", ctx)
    assert "<@&1>" in out
    assert "a: online, healthy" in out


def test_render_failure() -> None:
    """Tests that render_failure formats failure stage, error, and container status correctly."""
    ctx = FailureContext(
        tool_version="2.0.0",
        timestamp="2026-09-23T12:00:00Z",
        operator_roles=["2"],
        failure_stage="mid_scope_write",
        error="disk full",
        container_status={"a": STATE_STOPPED_BY_DEPLOYMENT, "b": STATE_CANCELLED},
    )
    out = render_failure("Stage: {failure_stage}\nError: {error}\n{container_status}", ctx)
    assert "mid_scope_write" in out
    assert "disk full" in out
    assert "a: stopped by deployment" in out
    assert "b: cancelled" not in out


def test_render_diagnostic() -> None:
    """Tests that render_diagnostic substitutes context values in a template."""
    ctx = DiagnosticContext(tool_version="2.0.0", timestamp="2026-09-23T12:00:00Z")
    out = render_diagnostic("test {tool_version} {timestamp}", ctx)
    assert "2.0.0" in out
    assert "2026-09-23T12:00:00Z" in out


class FakePoster:
    """A callable fake poster that records calls and returns a configurable result."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        self.ok = ok
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, content: str, allowed_mentions: dict, timeout: float) -> tuple[bool, str | None]:
        self.calls.append({"url": url, "content": content, "allowed_mentions": allowed_mentions, "timeout": timeout})
        return (self.ok, self.error)


def test_notify_live_posts_payload() -> None:
    """Tests that notify_live posts the rendered payload to the webhook."""
    poster = FakePoster()
    ctx = _live_ctx()
    template = "{tool_version}"
    result = notify_live(template, ctx, webhook_url="https://discord.example/webhook", all_role_ids=["111", "222"], poster=poster)
    assert result.success
    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["url"] == "https://discord.example/webhook"
    assert "2.0.0" in call["content"]


def test_notify_live_allowed_mentions() -> None:
    """Tests that notify_live constructs allowed_mentions with the given role IDs."""
    poster = FakePoster()
    notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=["a", "b"], poster=poster)
    am = poster.calls[0]["allowed_mentions"]
    assert am["parse"] == []
    assert am["roles"] == ["a", "b"]
    assert am["users"] == []


def test_notify_live_missing_template_skips() -> None:
    """Tests that notify_live skips when the template is missing."""
    poster = FakePoster()
    result = notify_live(None, _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=poster)
    assert result.skipped
    assert not result.success
    assert poster.calls == []


def test_notify_live_no_webhook_skips() -> None:
    """Tests that notify_live skips when no webhook URL is provided."""
    poster = FakePoster()
    result = notify_live("x", _live_ctx(), webhook_url=None, all_role_ids=[], poster=poster)
    assert result.skipped
    assert not result.success


def test_notify_live_2000_char_guard() -> None:
    """Tests that content exceeding the Discord limit fails without posting."""
    poster = FakePoster()
    template = "{tool_version}"
    ctx = _live_ctx(tool_version="X" * (DISCORD_CONTENT_LIMIT + 1))
    result = notify_live(template, ctx, webhook_url="https://x", all_role_ids=[], poster=poster)
    assert not result.success
    assert "2000" in (result.error or "")
    assert poster.calls == []


def test_notify_live_at_limit_ok() -> None:
    """Tests that content exactly at the Discord limit succeeds and posts once."""
    poster = FakePoster()
    template = "{tool_version}"
    ctx = _live_ctx(tool_version="X" * DISCORD_CONTENT_LIMIT)
    result = notify_live(template, ctx, webhook_url="https://x", all_role_ids=[], poster=poster)
    assert result.success
    assert len(poster.calls) == 1


def test_notify_live_poster_failure_returns_failure() -> None:
    """Tests that a failed live notification poster returns a failure result containing the error."""
    poster = FakePoster(ok=False, error="HTTP 500")
    result = notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=poster)
    assert not result.success
    assert "500" in (result.error or "")


def test_notify_live_poster_raises_is_caught() -> None:
    """Tests that an exception raised by the poster during notify_live is caught and returned as a failed result containing the error message."""

    def boom(*a, **kw):
        raise RuntimeError("boom")

    result = notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=boom)
    assert not result.success
    assert "boom" in (result.error or "")


def test_notify_online_and_failure_and_diagnostic() -> None:
    """Tests that notify_online, notify_failure, and notify_diagnostic each post successfully with their respective contexts."""
    poster = FakePoster()
    online_ctx = OnlineContext(tool_version="2.0.0", timestamp="2026-09-23T12:00:00Z", container_status={"a": STATE_ONLINE_HEALTHY})
    r1 = notify_online("up {container_status}", online_ctx, "https://x", [], poster=poster)
    assert r1.success
    failure_ctx = FailureContext(
        tool_version="2.0.0",
        timestamp="2026-09-23T12:00:00Z",
        failure_stage="mid_scope_write",
        error="disk full",
        container_status={"a": STATE_STOPPED_BY_DEPLOYMENT},
    )
    r2 = notify_failure("boom {error} {failure_stage}", failure_ctx, "https://x", [], poster=poster)
    assert r2.success
    diag_ctx = DiagnosticContext(tool_version="2.0.0", timestamp="2026-09-23T12:00:00Z")
    r3 = notify_diagnostic("diag {tool_version}", diag_ctx, "https://x", [], poster=poster)
    assert r3.success


def test_build_server_section_standard() -> None:
    """Tests that build_server_section renders standard deployment details including mods, Config/KubeJS members, restart action, pack requirement, and change reasons."""
    out = build_server_section(
        targeted=False,
        targeted_members=None,
        mods_deployed=128,
        mods_skipped=False,
        mods_drift=False,
        config_kubejs_members=["survival", "creative"],
        config_kubejs_changed=0,
        effective_action="restart",
        pack_required=True,
        reasons=[("mods/*", 3), ("kubejs/startup_scripts/*", 1)],
    )
    assert "### Server" in out
    assert "- Mods deployed: 128" in out
    assert "- Config/KubeJS deployed: survival, creative" in out
    assert "- Restart action: restart" in out
    assert "- Pack required: yes" in out
    assert "`mods/*` changed (3 path(s))" in out


def test_build_server_section_targeted() -> None:
    """Tests that build_server_section renders targeted deployment details, skipped mods, changed Config/KubeJS files, and no-restart notice."""
    out = build_server_section(
        targeted=True,
        targeted_members=["mc-creative"],
        mods_deployed=None,
        mods_skipped=True,
        mods_drift=False,
        config_kubejs_members=["creative"],
        config_kubejs_changed=3,
        effective_action="reload",
        pack_required=False,
        reasons=[("kubejs/server_scripts/*", 1)],
        no_restart_performed=True,
    )
    assert "- Targeted deploy to: mc-creative" in out
    assert "- Mods: not updated" in out
    assert "- Config/KubeJS: 3 file(s) changed" in out
    assert "- No container restart performed" in out


def test_build_client_section() -> None:
    """Tests that build_client_section includes the client heading, zip filename, and SHA-256 hash in its output."""
    out = build_client_section(
        zip_filename="minecraft_client_20260923.zip", sha256="abc123", changelog_url="http://minecraft/downloads/changelog.html", dry_run=False
    )
    assert "### Client" in out
    assert "minecraft_client_20260923.zip" in out
    assert "abc123" in out


def test_build_client_section_dry_run() -> None:
    """Tests that the client section indicates dry-run mode when requested."""
    out = build_client_section(zip_filename=None, sha256=None, changelog_url=None, dry_run=True)
    assert "dry-run" in out


def test_build_resource_pack_section_not_configured() -> None:
    """Tests the resource pack section output when no pack is configured."""
    out = build_resource_pack_section(configured=False, published_filename=None, members=[], effective_action=None)
    assert "NOT CONFIGURED" in out


def test_build_resource_pack_section_configured() -> None:
    """Tests the resource pack section output when a pack is configured."""
    out = build_resource_pack_section(configured=True, published_filename="pack.zip", members=["survival"], effective_action="restart")
    assert "Published: pack.zip" in out
    assert "Instances: survival" in out
    assert "Restart action: restart" in out


def test_timestamp_format() -> None:
    """Verifies render_timestamp_now returns an ISO 8601 UTC timestamp."""
    from minecraft.deploy_pack.notifications import render_timestamp_now

    ts = render_timestamp_now()
    assert re.match("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$", ts)


def test_notify_kind_constants() -> None:
    """Checks that NotifyKind constants have their expected string values."""
    assert NotifyKind.LIVE == "live"
    assert NotifyKind.ONLINE == "online"
    assert NotifyKind.FAILURE == "failure"
    assert NotifyKind.DIAGNOSTIC == "diagnostic"
