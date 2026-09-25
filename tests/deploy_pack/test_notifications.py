# tests/deploy_pack/test_notifications.py

"""Tests for deploy_pack.notifications, Project_Specs.md §5.4-§5.16.

Coverage, in spec-section order:

  * §5.4  - Live placeholders
  * §5.5  - Online placeholders
  * §5.6  - Failure placeholders
  * §5.7  - Diagnostic placeholders
  * §5.8  - the container_status vocabulary and its rendering scope
  * §5.11 - template validation timing and the four failure modes
  * §5.12 - notification is best-effort, never authoritative
  * §5.13 - the 2000-character content guard
  * §5.14 - allowed_mentions payload
  * §5.16 - role-mention rendering
  * §4.6.10 - server section adapter reasons
  * §4.6.9  - pack_required informational note
  * §7.7   - the resource-pack section's NOT CONFIGURED form

Rendering and validation are pure; notification dispatch is tested
against a fake poster that stands in for the requests HTTP call at
the boundary.
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
    STATE_ONLINE_STARTING,
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
    render_timestamp_now,
    validate_diagnostic,
    validate_live_and_failure,
    validate_online,
)


def _live_ctx(**overrides: Any) -> LiveContext:
    """Build a LiveContext with sensible defaults for rendering tests."""
    base: dict[str, Any] = {
        "tool_version": "2.0.0",
        "timestamp": "2026-09-23T12:00:00Z",
        "requested_scopes": ["server", "client"],
        "instance_list": ["creative", "survival"],
        "dry_run": False,
        "player_roles": ["111"],
        "operator_roles": ["222"],
        "section_server": "### Server\n- Mods deployed: 5",
        "section_client": "### Client\n- ZIP: x.zip",
        "section_resource_pack": "### Resource Pack\n- Status: NOT CONFIGURED",
    }
    base.update(overrides)
    return LiveContext(**base)


# ---------------------------------------------------------------------------
# §5.8: the container-status vocabulary
# ---------------------------------------------------------------------------


def test_state_constants_match_the_spec_strings() -> None:
    """§5.8: each state constant is the literal string the spec defines."""
    assert STATE_ONLINE_HEALTHY == "online, healthy"
    assert STATE_ONLINE_STARTING == "online, starting"
    assert STATE_ONLINE_UNHEALTHY == "online, unhealthy"
    assert STATE_STOPPED == "stopped"
    assert STATE_STOPPED_BY_DEPLOYMENT == "stopped by deployment"
    assert STATE_RECOVERY_START_FAILED == "recovery start failed"
    assert STATE_RECOVERY_START_SUCCEEDED == "recovery start succeeded"
    assert STATE_RELOADED == "reloaded"
    assert STATE_RELOAD_FAILED == "reload failed"
    assert STATE_START_FAILED == "start failed"
    assert STATE_HEALTH_TIMEOUT == "health timeout"
    assert STATE_CANCELLED == "cancelled"
    assert STATE_EXITED_BEFORE_STOP == "exited before stop"


def test_render_container_status_online_keeps_only_healthy() -> None:
    """§5.8: the online scope renders only online, healthy entries."""
    statuses = {"a": STATE_ONLINE_HEALTHY, "b": STATE_ONLINE_UNHEALTHY, "c": STATE_CANCELLED}
    out = render_container_status(statuses, "online")
    assert "a: online, healthy" in out
    assert "b" not in out
    assert "c" not in out


def test_render_container_status_failure_excludes_internal_states() -> None:
    """§5.8: cancelled and exited-before-stop are CLI-only."""
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
    assert not any(line.startswith("- d:") for line in out.splitlines())
    assert not any(line.startswith("- e:") for line in out.splitlines())


def test_render_container_status_empty_renders_none_placeholder() -> None:
    """§5.8: an empty status renders '- (none)' so the header is not left dangling."""
    assert render_container_status({}, "online") == "- (none)"
    assert render_container_status({}, "failure") == "- (none)"


def test_render_container_status_rejects_unknown_scope() -> None:
    """§5.8: scope is 'online' or 'failure'."""
    with pytest.raises(ValueError):
        render_container_status({}, "invalid")


def test_render_container_status_failure_covers_every_state() -> None:
    """§5.8: the failure scope includes every non-internal state."""
    statuses = {
        "a": STATE_STOPPED,
        "b": STATE_STOPPED_BY_DEPLOYMENT,
        "c": STATE_RECOVERY_START_SUCCEEDED,
        "d": STATE_RELOADED,
        "e": STATE_RELOAD_FAILED,
        "f": STATE_START_FAILED,
        "g": STATE_HEALTH_TIMEOUT,
        "h": STATE_ONLINE_HEALTHY,
    }
    out = render_container_status(statuses, "failure")
    for name in ("a", "b", "c", "d", "e", "f", "g", "h"):
        assert name in out


# ---------------------------------------------------------------------------
# §5.16: role-mention rendering
# ---------------------------------------------------------------------------


def test_render_player_tags_empty() -> None:
    """§5.16: no roles renders an empty string."""
    assert render_player_tags([]) == ""


def test_render_player_tags_single() -> None:
    """§5.16: one role renders as a Discord role mention."""
    assert render_player_tags(["1"]) == "<@&1>"


def test_render_player_tags_multiple_space_separated() -> None:
    """§5.16: multiple roles are space-separated."""
    assert render_player_tags(["1", "2"]) == "<@&1> <@&2>"


# ---------------------------------------------------------------------------
# §5.11: template validation
# ---------------------------------------------------------------------------


def test_validate_returns_nothing_when_notify_disabled() -> None:
    """§5.11: validation runs only when --notify is active."""
    discord = DiscordConfig(live_template=None)
    assert validate_live_and_failure(discord, notify=False, dry_run=False, has_scope=True) == []


def test_validate_missing_live_template_is_a_warning_not_a_failure() -> None:
    """§5.11: a missing template warns and skips; it does not exit 3."""
    assert validate_live_and_failure(DiscordConfig(live_template=None), notify=True, dry_run=False, has_scope=True) == []


def test_validate_empty_live_template_is_a_failure() -> None:
    """§5.11: an empty template is exit 3."""
    failures = validate_live_and_failure(DiscordConfig(live_template=""), notify=True, dry_run=False, has_scope=True)
    assert len(failures) == 1
    assert "non-empty" in failures[0][1]


def test_validate_unknown_placeholder_is_a_failure() -> None:
    """§5.11: a placeholder in no template's set is malformed."""
    failures = validate_live_and_failure(DiscordConfig(live_template="hello {nonexistent}"), notify=True, dry_run=False, has_scope=True)
    assert any("nonexistent" in m for _s, m in failures)


def test_validate_invalid_everywhere_placeholder_is_a_failure() -> None:
    """§5.11: a placeholder from the §11.3 'invalid everywhere' set is exit 3."""
    failures = validate_live_and_failure(DiscordConfig(live_template="hello {sha256sum}"), notify=True, dry_run=False, has_scope=True)
    assert any("invalid everywhere" in m for _s, m in failures)


def test_validate_cross_template_placeholder_is_a_failure() -> None:
    """§5.11: a placeholder valid in another template but not this one is exit 3."""
    failures = validate_live_and_failure(DiscordConfig(live_template="hello {failure_stage}"), notify=True, dry_run=False, has_scope=True)
    assert any("cross-template" in m for _s, m in failures)


def test_validate_dry_run_validates_only_live() -> None:
    """§5.11: under --dry-run, only the live template is reachable."""
    discord = DiscordConfig(live_template="ok {tool_version}", failure_template="{not_a_placeholder}")
    assert validate_live_and_failure(discord, notify=True, dry_run=True, has_scope=True) == []


def test_validate_no_scope_validates_diagnostic() -> None:
    """§5.11: with no scope, the diagnostic template is validated and live/failure are not."""
    discord = DiscordConfig(diagnostic_template="ok {tool_version}", live_template="{bad}")
    assert validate_live_and_failure(discord, notify=True, dry_run=False, has_scope=False) == []


def test_validate_no_scope_missing_diagnostic_is_skip() -> None:
    """§5.11: a missing diagnostic template warns and skips."""
    assert validate_live_and_failure(DiscordConfig(diagnostic_template=None), notify=True, dry_run=False, has_scope=False) == []


def test_validate_online_missing_is_skip() -> None:
    """§5.11: a missing online template warns and skips."""
    assert validate_online(DiscordConfig(online_template=None)) == []


def test_validate_online_valid() -> None:
    """§5.11: the online placeholder set is accepted."""
    assert validate_online(DiscordConfig(online_template="{tool_version} {timestamp} {container_status}")) == []


def test_validate_online_cross_template() -> None:
    """§5.11: a failure-only placeholder in the online template is exit 3."""
    failures = validate_online(DiscordConfig(online_template="{failure_stage}"))
    assert any("cross-template" in m for _s, m in failures)


def test_validate_online_empty_is_error() -> None:
    """§5.11: an empty online template is exit 3."""
    failures = validate_online(DiscordConfig(online_template=""))
    assert any("non-empty" in m for _s, m in failures)


def test_validate_diagnostic_accepts_only_its_own_placeholders() -> None:
    """§5.7: only {tool_version} and {timestamp} are valid in the diagnostic template."""
    assert validate_diagnostic(DiscordConfig(diagnostic_template="test {tool_version} {timestamp}")) == []


# ---------------------------------------------------------------------------
# §5.4: render_live
# ---------------------------------------------------------------------------


def test_render_live_substitutes_every_placeholder() -> None:
    """§5.4: every live placeholder is substituted."""
    template = (
        "{player_tags} **Minecraft Deployment**\n\n"
        "{section_server}\n{section_client}\n{section_resource_pack}\n\n"
        "{dry_run_marker}\nCompleted {timestamp}\n\n"
        "Built by deploy_pack {tool_version}"
    )
    out = render_live(template, _live_ctx())
    assert "<@&111>" in out
    assert "### Server" in out
    assert "### Client" in out
    assert "### Resource Pack" in out
    assert "2.0.0" in out
    assert "2026-09-23T12:00:00Z" in out


def test_render_live_dry_run_marker_is_the_spec_string_when_true() -> None:
    """§5.4: the dry-run marker is 'DRY RUN - no changes applied' under --dry-run."""
    assert render_live("[{dry_run_marker}]", _live_ctx(dry_run=True)) == "[DRY RUN - no changes applied]"


def test_render_live_dry_run_marker_is_empty_when_false() -> None:
    """§5.4: the dry-run marker is empty under a normal deploy."""
    assert render_live("[{dry_run_marker}]", _live_ctx(dry_run=False)) == "[]"


def test_render_live_instance_count_and_list() -> None:
    """§5.4: instance_count is the integer count; instance_list is comma-and-space separated."""
    assert render_live("{instance_count}: {instance_list}", _live_ctx()) == "2: creative, survival"


def test_render_live_requested_scopes_is_comma_and_space_separated() -> None:
    """§5.4: requested_scopes follows the fixed order server, client, resource-pack."""
    assert render_live("{requested_scopes}", _live_ctx()) == "server, client"
    assert render_live("{requested_scopes}", _live_ctx(requested_scopes=["server", "client", "resource-pack"])) == "server, client, resource-pack"


def test_render_live_operator_tags_uses_operator_roles() -> None:
    """§5.16: operator_tags is the operator role mention list."""
    assert render_live("{operator_tags}", _live_ctx(operator_roles=["9", "8"])) == "<@&9> <@&8>"


# ---------------------------------------------------------------------------
# §5.5 / §5.6 / §5.7: render_online / render_failure / render_diagnostic
# ---------------------------------------------------------------------------


def test_render_online_substitutes_its_placeholders() -> None:
    """§5.5: online renders player_tags, container_status, tool_version, timestamp."""
    ctx = OnlineContext(
        tool_version="2.0.0",
        timestamp="2026-09-23T12:00:00Z",
        player_roles=["1"],
        container_status={"a": STATE_ONLINE_HEALTHY},
    )
    out = render_online("{player_tags}\n{container_status}\n{tool_version}", ctx)
    assert "<@&1>" in out
    assert "a: online, healthy" in out


def test_render_failure_substitutes_its_placeholders() -> None:
    """§5.6: failure renders failure_stage, error, container_status, operator_tags."""
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


def test_render_diagnostic_substitutes_its_placeholders() -> None:
    """§5.7: diagnostic renders tool_version and timestamp."""
    ctx = DiagnosticContext(tool_version="2.0.0", timestamp="2026-09-23T12:00:00Z")
    out = render_diagnostic("test {tool_version} {timestamp}", ctx)
    assert "2.0.0" in out
    assert "2026-09-23T12:00:00Z" in out


# ---------------------------------------------------------------------------
# §5.4 / §5.13: timestamp format
# ---------------------------------------------------------------------------


def test_render_timestamp_now_uses_iso_z_format() -> None:
    """§5.4: the timestamp is RFC 3339 with a Z suffix: %Y-%m-%dT%H:%M:%SZ."""
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", render_timestamp_now())


# ---------------------------------------------------------------------------
# §4.6.10: build_server_section
# ---------------------------------------------------------------------------


def test_build_server_section_standard_form() -> None:
    """§4.6.10: the standard form lists mods, config/kubejs members, action, pack, and reasons."""
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


def test_build_server_section_targeted_form() -> None:
    """§4.6.10: the targeted form lists the target, notes mods are untouched, and counts config files."""
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


def test_build_server_section_pack_required_warning_renders_as_note() -> None:
    """§4.6.9: the pack_required_warning renders as a '- Note:' line after '- Pack required:'."""
    out = build_server_section(
        targeted=False,
        targeted_members=None,
        mods_deployed=1,
        mods_skipped=False,
        mods_drift=False,
        config_kubejs_members=[],
        config_kubejs_changed=0,
        effective_action="restart+pack",
        pack_required=True,
        reasons=[],
        pack_required_warning="client pack content changed; the current ZIP is stale.",
    )
    assert "- Pack required: yes" in out
    assert "- Note: client pack content changed; the current ZIP is stale." in out


def test_build_server_section_pack_required_warning_is_suppressed_by_default() -> None:
    """§4.6.9: pack_required_warning=None suppresses the '- Note:' line."""
    out = build_server_section(
        targeted=False,
        targeted_members=None,
        mods_deployed=1,
        mods_skipped=False,
        mods_drift=False,
        config_kubejs_members=[],
        config_kubejs_changed=0,
        effective_action="restart+pack",
        pack_required=True,
        reasons=[],
    )
    assert "- Note:" not in out


# ---------------------------------------------------------------------------
# §5.4: build_client_section
# ---------------------------------------------------------------------------


def test_build_client_section_renders_zip_sha_and_changelog() -> None:
    """§5.4: the client section carries the ZIP filename, hash, and changelog URL."""
    out = build_client_section(
        zip_filename="minecraft_client_20260923.zip",
        sha256="abc123",
        changelog_url="http://minecraft/downloads/changelog.html",
        dry_run=False,
    )
    assert "### Client" in out
    assert "minecraft_client_20260923.zip" in out
    assert "abc123" in out


def test_build_client_section_dry_run_marks_status() -> None:
    """§5.4 / §4.17: a dry-run client section indicates no ZIP was written."""
    out = build_client_section(zip_filename=None, sha256=None, changelog_url=None, dry_run=True)
    assert "dry-run" in out


# ---------------------------------------------------------------------------
# §7.7: build_resource_pack_section
# ---------------------------------------------------------------------------


def test_build_resource_pack_section_not_configured() -> None:
    """§7.7: the zero-pack form renders '- Status: NOT CONFIGURED'."""
    out = build_resource_pack_section(configured=False, published_filename=None, members=[], effective_action=None)
    assert "NOT CONFIGURED" in out


def test_build_resource_pack_section_configured() -> None:
    """§7.7: the configured form carries the filename, members, and effective action."""
    out = build_resource_pack_section(configured=True, published_filename="pack.zip", members=["survival"], effective_action="restart")
    assert "Published: pack.zip" in out
    assert "Instances: survival" in out
    assert "Restart action: restart" in out


# ---------------------------------------------------------------------------
# §5.12 / §5.13 / §5.14: notification dispatch
# ---------------------------------------------------------------------------


class _FakePoster:
    """A callable stand-in for the requests.post call at the HTTP boundary."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        self.ok = ok
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, content: str, allowed_mentions: dict, timeout: float) -> tuple[bool, str | None]:
        """Record the call and return the configured (ok, error) result."""
        self.calls.append({"url": url, "content": content, "allowed_mentions": allowed_mentions, "timeout": timeout})
        return (self.ok, self.error)


def test_notify_live_posts_the_rendered_content() -> None:
    """§5.4 + §5.12: notify_live posts the rendered content to the webhook."""
    poster = _FakePoster()
    result = notify_live("{tool_version}", _live_ctx(), webhook_url="https://discord.example/webhook", all_role_ids=["111", "222"], poster=poster)
    assert result.success
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"] == "https://discord.example/webhook"
    assert "2.0.0" in poster.calls[0]["content"]


def test_notify_live_allowed_mentions_shape() -> None:
    """§5.14: allowed_mentions is {'parse': [], 'roles': [...], 'users': []}."""
    poster = _FakePoster()
    notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=["a", "b"], poster=poster)
    am = poster.calls[0]["allowed_mentions"]
    assert am["parse"] == []
    assert am["roles"] == ["a", "b"]
    assert am["users"] == []


def test_notify_live_missing_template_skips() -> None:
    """§5.11: a missing template skips the notification without posting."""
    poster = _FakePoster()
    result = notify_live(None, _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=poster)
    assert result.skipped
    assert not result.success
    assert poster.calls == []


def test_notify_live_no_webhook_skips() -> None:
    """§3.12: a missing webhook URL skips the notification without posting."""
    poster = _FakePoster()
    result = notify_live("x", _live_ctx(), webhook_url=None, all_role_ids=[], poster=poster)
    assert result.skipped
    assert not result.success


def test_notify_live_over_2000_chars_fails_without_posting() -> None:
    """§5.13: content longer than 2000 characters is not posted."""
    poster = _FakePoster()
    ctx = _live_ctx(tool_version="X" * (DISCORD_CONTENT_LIMIT + 1))
    result = notify_live("{tool_version}", ctx, webhook_url="https://x", all_role_ids=[], poster=poster)
    assert not result.success
    assert "2000" in (result.error or "")
    assert poster.calls == []


def test_notify_live_at_exactly_2000_chars_is_posted() -> None:
    """§5.13: content of exactly 2000 characters is within the limit."""
    poster = _FakePoster()
    ctx = _live_ctx(tool_version="X" * DISCORD_CONTENT_LIMIT)
    result = notify_live("{tool_version}", ctx, webhook_url="https://x", all_role_ids=[], poster=poster)
    assert result.success
    assert len(poster.calls) == 1


def test_notify_live_poster_failure_is_reported() -> None:
    """§5.12: a poster failure returns a NotifyResult with the error, not a raise."""
    poster = _FakePoster(ok=False, error="HTTP 500")
    result = notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=poster)
    assert not result.success
    assert "500" in (result.error or "")


def test_notify_live_poster_raising_is_caught() -> None:
    """§5.12: a raising poster is caught and returned as a failed result."""

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    result = notify_live("x", _live_ctx(), webhook_url="https://x", all_role_ids=[], poster=boom)
    assert not result.success
    assert "boom" in (result.error or "")


def test_notify_online_failure_and_diagnostic_round_trip() -> None:
    """§5.5-§5.7: each notify_* composes its context and dispatches through the poster."""
    poster = _FakePoster()
    online = notify_online(
        "up {container_status}",
        OnlineContext(tool_version="2.0.0", timestamp="ts", container_status={"a": STATE_ONLINE_HEALTHY}),
        "https://x",
        [],
        poster=poster,
    )
    assert online.success
    failure = notify_failure(
        "boom {error} {failure_stage}",
        FailureContext(
            tool_version="2.0.0",
            timestamp="ts",
            failure_stage="mid_scope_write",
            error="disk full",
            container_status={"a": STATE_STOPPED_BY_DEPLOYMENT},
        ),
        "https://x",
        [],
        poster=poster,
    )
    assert failure.success
    diagnostic = notify_diagnostic(
        "diag {tool_version}",
        DiagnosticContext(tool_version="2.0.0", timestamp="ts"),
        "https://x",
        [],
        poster=poster,
    )
    assert diagnostic.success


# ---------------------------------------------------------------------------
# NotifyKind constants
# ---------------------------------------------------------------------------


def test_notify_kind_constants_match_their_strings() -> None:
    """The four NotifyKind constants are the spec's kind names."""
    assert NotifyKind.LIVE == "live"
    assert NotifyKind.ONLINE == "online"
    assert NotifyKind.FAILURE == "failure"
    assert NotifyKind.DIAGNOSTIC == "diagnostic"
