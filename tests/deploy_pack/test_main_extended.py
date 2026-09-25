# tests/deploy_pack/test_main_extended.py

"""Extended tests for deploy_pack.main covering the runtime sequence.

The sibling ``test_main.py`` exercises the outer shell: argument
parsing, the §2.5 exit-2 matrix, scope resolution, and the top-level
exit-code mapping. This module covers what that leaves unexercised:

  * pure helpers: :func:`_resolve_config_dir`, :func:`_setup_logging`,
    :func:`_print_cli_container_status`
  * dataclasses: :class:`_NoticeOutcome`, :class:`_Scopes.any`
  * status composition: :func:`_failure_container_status`,
    :func:`_online_container_status`
  * notification section builders: :func:`_aggregate_reasons`,
    :func:`_server_effective_action`, :func:`_build_client_section`,
    :func:`_build_rp_section`
  * recovery printers: :func:`_print_recovery_block`,
    :func:`_print_server_write_failure_recovery`
  * :class:`_RconSet` lazy transport selection, probing, dispatch
  * :func:`_prompt_for_unmarked` interactive prompt paths
  * :func:`_dispatch_restart_notices`, :func:`_dispatch_cancel_notice`
  * :func:`_run_deployment` dry-run output and the §4.1 sequence
    failure exit paths (restart-notice failure, server write failure,
    client write failure) driven end-to-end with monkeypatched
    preflight, Docker runtime, and scope functions.

Stubs use :class:`types.SimpleNamespace` where the real dataclasses
would require a full preflight pass to construct. The goal is to
exercise the control flow in ``main.py``, not to re-test preflight or
the scope modules.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.main import (
    _aggregate_reasons,
    _build_client_section,
    _build_rp_section,
    _dispatch_cancel_notice,
    _dispatch_restart_notices,
    _failure_container_status,
    _NoticeOutcome,
    _online_container_status,
    _print_cli_container_status,
    _print_recovery_block,
    _print_server_write_failure_recovery,
    _prompt_for_unmarked,
    _RconSet,
    _resolve_config_dir,
    _run_deployment,
    _Scopes,
    _server_effective_action,
    _setup_logging,
)
from minecraft.deploy_pack.preflight import ScopeSet


def _logger() -> logging.Logger:
    """Return a module-scoped logger for tests that only need a sink."""
    return logging.getLogger("test_main_extended")


def _stub_plan(**overrides: object) -> SimpleNamespace:
    """Return a stub plan with every field ``_run_deployment`` reads.

    Tests override only the fields they care about; the defaults
    describe the smallest plan that can walk the runtime sequence
    without touching Docker or the scopes.
    """
    base: dict[str, object] = {
        "partition": [],
        "none_set": [],
        "reload_set": [],
        "restart_set": [],
        "pack_required": False,
        "member_plans": {},
        "warnings": [],
        "container_states": {},
        "mods_change": None,
        "mods_drift": None,
        "pack_required_warning": None,
        "targeted": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_state(running: bool = True) -> SimpleNamespace:
    """Return a container-state stub with the ``is_running`` flag."""
    return SimpleNamespace(is_running=running)


def _docker_stub(**overrides: object) -> SimpleNamespace:
    """Return a ``config.docker`` stub with every field the runtime path reads."""
    base: dict[str, object] = {
        "restart_notice_template": "Server restarting in {time}",
        "restart_wait_seconds": 0,
        "restart_cancel_notice_template": "Restart cancelled",
        "in_game_notice_required": False,
        "cancel_notice_ready_timeout_seconds": 5.0,
        "health_poll_seconds": 1.0,
        "health_timeout_seconds": 60,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeRconSet:
    """Stand-in for :class:`_RconSet` used across the ``_run_deployment`` tests.

    ``probe`` defaults to False so the recovery path treats every
    container as unreachable; ``send`` defaults to True so the happy
    path succeeds unless a test overrides ``send_results`` (or
    subclasses to fail).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.probe_results: dict[str, bool] = {}
        self.send_results: dict[str, bool] = {}
        self.sent: list[tuple[str, str]] = []
        self.cancelled: list[tuple[list[str], str]] = []

    def probe(self, name: str) -> bool:
        """Check whether the container is reachable."""
        return self.probe_results.get(name, False)

    def send(self, name: str, cmd: str) -> bool:
        """Record the command and return the configured result (default True)."""
        self.sent.append((name, cmd))
        return self.send_results.get(name, True)

    def dispatch_cancel(self, names: list[str], msg: str) -> None:
        """Record a cancellation dispatch."""
        self.cancelled.append((list(names), msg))


class TestResolveConfigDir:
    """Tests for :func:`_resolve_config_dir`."""

    def test_default_is_relative(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a missing --config-dir falls back to ./config.d."""
        monkeypatch.chdir(tmp_path)
        assert _resolve_config_dir(None) == Path("config.d")

    def test_directory_returned_verbatim(self, tmp_path: Path) -> None:
        """Tests that a directory argument is returned unchanged."""
        d = tmp_path / "cfg"
        d.mkdir()
        assert _resolve_config_dir(str(d)) == d

    def test_file_argument_returns_parent(self, tmp_path: Path) -> None:
        """Tests that a file argument is reduced to its parent directory."""
        f = tmp_path / "deploy_pack.toml"
        f.write_text("")
        assert _resolve_config_dir(str(f)) == tmp_path

    def test_nonexistent_path_returned_as_is(self, tmp_path: Path) -> None:
        """Tests that a nonexistent path is treated as a directory."""
        p = tmp_path / "missing"
        assert _resolve_config_dir(str(p)) == p


class TestSetupLogging:
    """Tests for :func:`_setup_logging`."""

    def test_debug_enables_debug_level(self) -> None:
        """Tests that --debug raises the root logger to DEBUG."""
        logger = _setup_logging(True)
        assert logger.name == "deploy_pack"
        assert logging.getLogger().level == logging.DEBUG

    def test_default_is_info_level(self) -> None:
        """Tests that omitting --debug leaves the root logger at INFO."""
        _setup_logging(False)
        assert logging.getLogger().level == logging.INFO


class TestPrintCliContainerStatus:
    """Tests for :func:`_print_cli_container_status`."""

    def test_emits_one_line_per_member(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that each container is rendered as <name>: <state>."""
        _print_cli_container_status({"alpha": "online, healthy", "beta": "stopped"})
        out = capsys.readouterr().out
        assert "alpha: online, healthy" in out
        assert "beta: stopped" in out

    def test_empty_status_prints_nothing(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that an empty status dict produces no stdout."""
        _print_cli_container_status({})
        assert capsys.readouterr().out == ""

    def test_keys_sorted(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that containers are printed in sorted order."""
        _print_cli_container_status({"b": "stopped", "a": "stopped"})
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["a: stopped", "b: stopped"]


class TestNoticeOutcome:
    """Tests for the :class:`_NoticeOutcome` helper."""

    def test_any_delivered_false_when_empty(self) -> None:
        """Tests that an outcome with no deliveries reports any_delivered False."""
        assert _NoticeOutcome(attempted=[], delivered=[], failed=[]).any_delivered is False

    def test_any_delivered_true_with_one(self) -> None:
        """Tests that a single delivery sets any_delivered True."""
        assert _NoticeOutcome(attempted=["a"], delivered=["a"], failed=[]).any_delivered is True


class TestScopes:
    """Tests for :class:`_Scopes`."""

    def test_any_true_with_server(self) -> None:
        """Tests that any() is True when the server scope is set."""
        assert _Scopes(ScopeSet(server=True), with_resources=False).any() is True

    def test_any_false_when_empty(self) -> None:
        """Tests that any() is False when no scope is set."""
        assert _Scopes(ScopeSet(), with_resources=False).any() is False


class TestOnlineContainerStatus:
    """Tests for :func:`_online_container_status`."""

    def test_healthy_containers_are_marked(self) -> None:
        """Tests that a stopped-and-restarted container is marked online, healthy."""
        post = SimpleNamespace(started=["a", "b"], start_failed=[], health_failed=[])
        status = _online_container_status(None, ["a", "b"], post)
        assert status == {"a": "online, healthy", "b": "online, healthy"}

    def test_failed_start_is_omitted(self) -> None:
        """Tests that containers that failed to start are omitted."""
        post = SimpleNamespace(started=["a", "b"], start_failed=["b"], health_failed=[])
        status = _online_container_status(None, ["a", "b"], post)
        assert status == {"a": "online, healthy"}

    def test_health_failed_is_omitted(self) -> None:
        """Tests that containers whose health check timed out are omitted."""
        post = SimpleNamespace(started=["a", "b"], start_failed=[], health_failed=["b"])
        status = _online_container_status(None, ["a", "b"], post)
        assert status == {"a": "online, healthy"}


class TestFailureContainerStatus:
    """Tests for :func:`_failure_container_status`."""

    def test_empty_inputs_yield_empty_status(self) -> None:
        """Tests that no lifecycle activity and an empty partition yield {}."""
        plan = _stub_plan(partition=[])
        assert _failure_container_status(plan, None, None, [], []) == {}

    def test_reload_outcomes_recorded(self) -> None:
        """Tests that reload outcomes are carried into the status map."""
        plan = _stub_plan(partition=[])
        status = _failure_container_status(plan, None, None, ["a"], ["b"])
        assert status["a"] == "reloaded"
        assert status["b"] == "reload failed"

    def test_pre_hook_stopped_recorded(self) -> None:
        """Tests that containers stopped by the pre-hook are recorded."""
        plan = _stub_plan(partition=[])
        pre = SimpleNamespace(stopped=["a"], exited_before_stop=[], recovery=None, failed=[])
        status = _failure_container_status(plan, pre, None, [], [])
        assert status["a"] == "stopped by deployment"

    def test_pre_hook_exited_before_stop_recorded(self) -> None:
        """Tests that containers that exited before the stop are recorded distinctly."""
        plan = _stub_plan(partition=[])
        pre = SimpleNamespace(stopped=[], exited_before_stop=["b"], recovery=None, failed=[])
        status = _failure_container_status(plan, pre, None, [], [])
        assert status["b"] == "exited before stop"

    def test_recovery_outcomes_recorded(self) -> None:
        """Tests that the pre-hook's recovery outcomes are surfaced per container."""
        plan = _stub_plan(partition=[])
        recovery = SimpleNamespace(start_failed=["a"], started=["b"])
        pre = SimpleNamespace(stopped=[], exited_before_stop=[], recovery=recovery, failed=["a"])
        status = _failure_container_status(plan, pre, None, [], [])
        assert status["a"] == "recovery start failed"
        assert status["b"] == "recovery start succeeded"

    def test_post_phase_outcomes_recorded(self) -> None:
        """Tests that post-phase successes and failures are recorded."""
        plan = _stub_plan(partition=[])
        post = SimpleNamespace(healthy=["a"], start_failed=["b"], health_failed=["c"])
        status = _failure_container_status(plan, None, post, [], [])
        assert status["a"] == "online, healthy"
        assert status["b"] == "start failed"
        assert status["c"] == "health timeout"

    def test_previously_stopped_partition_members(self) -> None:
        """Tests that partition members already down are marked stopped."""
        plan = _stub_plan(partition=["a"], container_states={"a": _stub_state(running=False)})
        status = _failure_container_status(plan, None, None, [], [])
        assert status["a"] == "stopped"

    def test_running_partition_member_not_marked(self) -> None:
        """Tests that still-running partition members are not spuriously marked."""
        plan = _stub_plan(partition=["a"], container_states={"a": _stub_state(running=True)})
        assert _failure_container_status(plan, None, None, [], []) == {}

    def test_later_stage_overrides_earlier(self) -> None:
        """Tests that a post-phase state overwrites an earlier reload state for the same member."""
        plan = _stub_plan(partition=[])
        post = SimpleNamespace(healthy=["a"], start_failed=[], health_failed=[])
        status = _failure_container_status(plan, None, post, ["a"], [])
        assert status["a"] == "online, healthy"


class TestAggregateReasons:
    """Tests for :func:`_aggregate_reasons`."""

    def test_counts_by_prefix(self) -> None:
        """Tests that reason changed paths are counted and grouped by prefix."""
        r1 = SimpleNamespace(path_prefix="mods", changed_paths=["mods/a.jar", "mods/b.jar"])
        r2 = SimpleNamespace(path_prefix="config", changed_paths=["config/x.toml"])
        mp = SimpleNamespace(reasons=[r1, r2])
        plan = _stub_plan(partition=["s1"], member_plans={"s1": mp})
        assert _aggregate_reasons(plan) == [("config", 1), ("mods", 2)]

    def test_empty_plan(self) -> None:
        """Tests that a plan with no member plans yields no reasons."""
        plan = _stub_plan(partition=[])
        assert _aggregate_reasons(plan) == []

    def test_member_without_plan_skipped(self) -> None:
        """Tests that a partition member missing a member plan is skipped."""
        plan = _stub_plan(partition=["s1"], member_plans={})
        assert _aggregate_reasons(plan) == []


class TestServerEffectiveAction:
    """Tests for :func:`_server_effective_action`."""

    def test_empty_plan_returns_none(self) -> None:
        """Tests that a plan with no member plans returns the literal 'none'."""
        plan = _stub_plan(member_plans={})
        assert _server_effective_action(plan) == "none"

    def test_single_action_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the sticky max of a single action is that action."""
        monkeypatch.setattr(main_mod, "_sticky_max", lambda actions: actions[0])
        mp = SimpleNamespace(effective_action="restart")
        plan = _stub_plan(member_plans={"s1": mp})
        assert _server_effective_action(plan) == "restart"


class TestBuildClientSection:
    """Tests for :func:`_build_client_section`."""

    def test_dry_run_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a dry-run client section is built with the dry-run flag set."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "client-section"

        monkeypatch.setattr(main_mod.notifications, "build_client_section", fake_build)
        assert _build_client_section(None, dry_run=True) == "client-section"
        assert captured["dry_run"] is True
        assert captured["zip_filename"] is None

    def test_live_section_uses_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a non-dry-run client section carries the result fields."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "client-section"

        monkeypatch.setattr(main_mod.notifications, "build_client_section", fake_build)
        result = SimpleNamespace(resolved_output_filename="pack.zip", zip_sha256="deadbeef", changelog_url="http://x")
        _build_client_section(result, dry_run=False)
        assert captured["dry_run"] is False
        assert captured["zip_filename"] == "pack.zip"
        assert captured["sha256"] == "deadbeef"

    def test_dry_run_with_result_still_marks_dry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that dry_run=True wins even when a result is supplied."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "client-section"

        monkeypatch.setattr(main_mod.notifications, "build_client_section", fake_build)
        _build_client_section(SimpleNamespace(resolved_output_filename="pack.zip", zip_sha256="x", changelog_url=None), dry_run=True)
        assert captured["dry_run"] is True


class TestBuildRpSection:
    """Tests for :func:`_build_rp_section`."""

    def test_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a plan with no RP members yields an unconfigured section."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "rp-section"

        monkeypatch.setattr(main_mod.notifications, "build_resource_pack_section", fake_build)
        plan = _stub_plan(partition=[])
        _build_rp_section(plan, None)
        assert captured["configured"] is False
        assert captured["members"] == []

    def test_configured_from_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a member plan with an RP target marks the section as configured."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "rp-section"

        monkeypatch.setattr(main_mod.notifications, "build_resource_pack_section", fake_build)
        mp = SimpleNamespace(resource_pack_target="server-resources")
        plan = _stub_plan(partition=["s1"], member_plans={"s1": mp})
        _build_rp_section(plan, None)
        assert captured["configured"] is True
        assert captured["members"] == ["s1"]

    def test_publish_result_supplies_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the first publish result's filename is threaded through."""
        captured: dict[str, object] = {}

        def fake_build(**kwargs: object) -> str:
            captured.update(kwargs)
            return "rp-section"

        monkeypatch.setattr(main_mod.notifications, "build_resource_pack_section", fake_build)
        rp_result = SimpleNamespace(publish_results=[SimpleNamespace(filename="rp.zip")])
        plan = _stub_plan(partition=["s1"], member_plans={"s1": SimpleNamespace(resource_pack_target=None)})
        _build_rp_section(plan, rp_result)
        assert captured["configured"] is True
        assert captured["published_filename"] == "rp.zip"


class TestPrintRecoveryBlock:
    """Tests for :func:`_print_recovery_block`."""

    def test_no_affected_prints_nothing(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that a recovery with no failures produces no output."""
        recovery = SimpleNamespace(start_failed=[], unreachable=[])
        config = SimpleNamespace(instances={})
        _print_recovery_block(config, recovery, {}, "ctx")
        assert capsys.readouterr().out == ""

    def test_affected_members_rendered(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that affected containers are listed with their docker start hint."""
        recovery = SimpleNamespace(start_failed=["a"], unreachable=["b"])
        config = SimpleNamespace(instances={"a": SimpleNamespace(container="c-a"), "b": SimpleNamespace(container="c-b")})
        _print_recovery_block(config, recovery, {}, "stop phase failure")
        out = capsys.readouterr().out
        assert "RECOVERY REQUIRED" in out
        assert "stop phase failure" in out
        assert "c-a: stopped" in out
        assert "c-b: stopped" in out
        assert "docker start c-a c-b" in out

    def test_unknown_member_uses_name(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that a member not present in config.instances falls back to its own name."""
        recovery = SimpleNamespace(start_failed=["ghost"], unreachable=[])
        config = SimpleNamespace(instances={})
        _print_recovery_block(config, recovery, {}, "ctx")
        out = capsys.readouterr().out
        assert "ghost: stopped" in out


class TestPrintServerWriteFailureRecovery:
    """Tests for :func:`_print_server_write_failure_recovery`."""

    def test_renders_block(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that the server-scope recovery block lists stopped containers and the reason."""
        config = SimpleNamespace(instances={"s1": SimpleNamespace(container="mc-s1")})
        _print_server_write_failure_recovery(config, ["s1"], "write failed")
        out = capsys.readouterr().out
        assert "RECOVERY REQUIRED" in out
        assert "mc-s1: stopped" in out
        assert "write failed" in out
        assert "docker start mc-s1" in out

    def test_unknown_member_uses_name(self, capsys: pytest.CaptureFixture) -> None:
        """Tests that a missing instance entry is rendered by name."""
        config = SimpleNamespace(instances={})
        _print_server_write_failure_recovery(config, ["ghost"], "boom")
        assert "ghost: stopped" in capsys.readouterr().out


class TestRconSet:
    """Tests for :class:`_RconSet` lazy transport selection."""

    def _config(self, instances: dict[str, object], compose_ok: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            compose=SimpleNamespace(ok=compose_ok, file="docker-compose.yml"), instances=instances, docker=SimpleNamespace(rcon_host="127.0.0.1")
        )

    def test_skips_instances_without_service(self) -> None:
        """Tests that instances without a service are not given a transport."""
        inst = SimpleNamespace(service=None, container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.probe("c") is False

    def test_skips_when_compose_missing(self) -> None:
        """Tests that when the compose file is unknown, no transports are selected."""
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst}, compose_ok=False)
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.probe("c") is False

    def test_selection_failure_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a select_rcon_transport failure is recorded and the container probes as unreachable."""

        def boom(*args: object, **kwargs: object) -> object:
            raise ConfigError("no rcon")

        monkeypatch.setattr(main_mod, "select_rcon_transport", boom)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert "c" in rs._selection_errors
        assert rs.probe("c") is False

    def test_probe_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a transport whose 'list' command succeeds probes as reachable."""
        transport = SimpleNamespace(execute=lambda cmd: (True, "ok"))
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.probe("c") is True

    def test_probe_command_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a transport whose 'list' command returns (False, ...) probes as unreachable."""
        transport = SimpleNamespace(execute=lambda cmd: (False, "nope"))
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.probe("c") is False

    def test_probe_swallows_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a transport raising on execute probes as unreachable."""

        def boom(cmd: str) -> object:
            raise RuntimeError("socket")

        transport = SimpleNamespace(execute=boom)
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.probe("c") is False

    def test_send_returns_false_for_unknown(self) -> None:
        """Tests that sending to an unregistered container returns False."""
        config = self._config({})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.send("nope", "list") is False

    def test_send_uses_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a registered transport receives the command and reports success."""
        calls: list[str] = []

        def execute(cmd: str) -> tuple[bool, str]:
            calls.append(cmd)
            return (True, "ok")

        transport = SimpleNamespace(execute=execute)
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.send("c", "say hi") is True
        assert calls == ["say hi"]

    def test_send_swallows_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a transport raising on send returns False."""

        def boom(cmd: str) -> object:
            raise RuntimeError("socket")

        transport = SimpleNamespace(execute=boom)
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        assert rs.send("c", "say hi") is False

    def test_dispatch_cancel_fans_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that dispatch_cancel sends a say command to every named container."""
        calls: list[str] = []

        def execute(cmd: str) -> tuple[bool, str]:
            calls.append(cmd)
            return (True, "ok")

        transport = SimpleNamespace(execute=execute)
        monkeypatch.setattr(main_mod, "select_rcon_transport", lambda *a, **k: transport)
        inst = SimpleNamespace(service="svc", container="c")
        config = self._config({"a": inst})
        rs = _RconSet(config, runtime=None, logger=_logger())
        rs.dispatch_cancel(["c", "c"], "restarting")
        assert calls == ["say restarting", "say restarting"]


class TestDispatchRestartNotices:
    """Tests for :func:`_dispatch_restart_notices`."""

    def test_all_delivered(self) -> None:
        """Tests that a successful dispatch records every container as delivered."""
        config = SimpleNamespace(docker=_docker_stub(), instances={"a": SimpleNamespace(container="c-a")})
        rcon = SimpleNamespace(send=lambda c, m: True)
        outcome = _dispatch_restart_notices(config, ["a"], rcon, _logger())
        assert outcome.attempted == ["a"]
        assert outcome.delivered == ["a"]
        assert outcome.failed == []

    def test_failure_recorded(self) -> None:
        """Tests that a send failure is recorded in the failed list."""
        config = SimpleNamespace(docker=_docker_stub(), instances={"a": SimpleNamespace(container="c-a")})
        rcon = SimpleNamespace(send=lambda c, m: False)
        outcome = _dispatch_restart_notices(config, ["a"], rcon, _logger())
        assert outcome.failed == ["a"]

    def test_required_stops_on_first_failure(self) -> None:
        """Tests that a required notice aborts the dispatch on the first failure."""
        config = SimpleNamespace(
            docker=_docker_stub(in_game_notice_required=True), instances={"a": SimpleNamespace(container="c-a"), "b": SimpleNamespace(container="c-b")}
        )
        rcon = SimpleNamespace(send=lambda c, m: False)
        outcome = _dispatch_restart_notices(config, ["a", "b"], rcon, _logger())
        assert outcome.failed == ["a"]

    def test_unknown_member_skipped(self) -> None:
        """Tests that a warned member with no instance entry is not attempted."""
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        rcon = SimpleNamespace(send=lambda c, m: True)
        outcome = _dispatch_restart_notices(config, ["ghost"], rcon, _logger())
        assert outcome.attempted == []

    def test_template_substitutes_time(self) -> None:
        """Tests that the {time} placeholder is replaced with the configured wait."""
        sent: list[str] = []
        config = SimpleNamespace(
            docker=_docker_stub(restart_notice_template="in {time}", restart_wait_seconds=45), instances={"a": SimpleNamespace(container="c-a")}
        )
        rcon = SimpleNamespace(send=lambda c, m: sent.append(m) or True)
        _dispatch_restart_notices(config, ["a"], rcon, _logger())
        assert sent == ["say in 45 seconds"]


class TestDispatchCancelNotice:
    """Tests for :func:`_dispatch_cancel_notice`."""

    def test_empty_recipients_noop(self) -> None:
        """Tests that an empty recipient list does not invoke dispatch_cancel."""
        called: list[object] = []
        rcon = SimpleNamespace(dispatch_cancel=lambda names, msg: called.append((names, msg)))
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        _dispatch_cancel_notice(config, [], rcon, _logger())
        assert called == []

    def test_dispatch_fans_out(self) -> None:
        """Tests that each recipient is resolved to its container and dispatched."""
        called: list[tuple[list[str], str]] = []
        rcon = SimpleNamespace(dispatch_cancel=lambda names, msg: called.append((list(names), msg)))
        config = SimpleNamespace(docker=_docker_stub(restart_cancel_notice_template="cancelled"), instances={"a": SimpleNamespace(container="c-a")})
        _dispatch_cancel_notice(config, ["a"], rcon, _logger())
        assert called == [(["c-a"], "cancelled")]

    def test_unknown_recipient_dropped(self) -> None:
        """Tests that recipients with no instance entry are silently dropped."""
        called: list[tuple[list[str], str]] = []
        rcon = SimpleNamespace(dispatch_cancel=lambda names, msg: called.append((list(names), msg)))
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        _dispatch_cancel_notice(config, ["ghost"], rcon, _logger())
        assert called == [([], "Restart cancelled")]


class TestPromptForUnmarked:
    """Tests for :func:`_prompt_for_unmarked`."""

    def _config(self, tmp_path: Path) -> SimpleNamespace:
        return SimpleNamespace(modpack_dir=tmp_path / "pack", config_dir=tmp_path / "cfg")

    def test_missing_index_returns_early(self, tmp_path: Path) -> None:
        """Tests that a missing .index directory short-circuits the prompt."""
        config = self._config(tmp_path)
        _prompt_for_unmarked(config, _logger())

    def test_no_unmarked_returns_early(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that when no unmarked jars are found, the prompt does nothing."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: [])
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [])
        _prompt_for_unmarked(config, _logger())

    def test_unfixable_only_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that unmarked jars without an index entry are warned about, not prompted."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: [])
        unmarked = SimpleNamespace(filename="x.jar", reason="no pw.toml")
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [unmarked])
        with caplog.at_level(logging.WARNING):
            _prompt_for_unmarked(config, _logger())
        assert any("cannot be fixed" in r.message for r in caplog.records)

    def test_fixable_non_tty_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that fixable jars without a TTY are skipped with a warning."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        entries = [{"file": "y.jar"}]
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: entries)
        unmarked = SimpleNamespace(filename="y.jar", reason="bad side")
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [unmarked])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with caplog.at_level(logging.WARNING):
            _prompt_for_unmarked(config, _logger())
        assert any("not a TTY" in r.message for r in caplog.records)

    def test_fixable_answers_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an interactive answer is written as a review entry."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        entries = [{"file": "y.jar"}]
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: entries)
        unmarked = SimpleNamespace(filename="y.jar", reason="bad side")
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [unmarked])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        saved: dict[str, object] = {}

        def fake_save(path: Path, review: dict, logger: object) -> None:
            saved["path"] = path
            saved["review"] = dict(review)

        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(deployment_tool_review={}))
        monkeypatch.setattr(main_mod, "save_side_overrides", fake_save)
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"y.jar": "server"}

    def test_skip_answer_writes_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the [k] answer records the jar as skipped."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        entries = [{"file": "y.jar"}]
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [SimpleNamespace(filename="y.jar", reason="x")])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "k")
        saved: dict[str, object] = {}

        def fake_save(path: Path, review: dict, logger: object) -> None:
            saved["review"] = dict(review)

        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(deployment_tool_review={}))
        monkeypatch.setattr(main_mod, "save_side_overrides", fake_save)
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"y.jar": "skipped"}

    def test_preserves_existing_review(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an existing review entry is preserved while new ones are added."""
        config = self._config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        entries = [{"file": "y.jar"}]
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [SimpleNamespace(filename="y.jar", reason="x")])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "c")
        saved: dict[str, object] = {}

        def fake_save(path: Path, review: dict, logger: object) -> None:
            saved["review"] = dict(review)

        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(deployment_tool_review={"old.jar": "both"}))
        monkeypatch.setattr(main_mod, "save_side_overrides", fake_save)
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"old.jar": "both", "y.jar": "client"}


class TestRunDeploymentDryRun:
    """Tests for the dry-run branch of :func:`_run_deployment`."""

    def test_empty_scope_returns_immediately(self) -> None:
        """Tests that an empty scope set returns 0 without touching the plan."""
        plan = _stub_plan()
        scopes = _Scopes(ScopeSet(), with_resources=False)
        rc = _run_deployment(None, plan, scopes, None, notify=False, dry_run=True, protect_patterns=[], logger=_logger())
        assert rc == 0

    def test_dry_run_returns_zero_without_runtime(self) -> None:
        """Tests that a dry-run with a scope returns 0 without invoking Docker."""
        plan = _stub_plan(partition=["s1"], reload_set=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        rc = _run_deployment(None, plan, scopes, None, notify=False, dry_run=True, protect_patterns=[], logger=_logger())
        assert rc == 0

    def test_dry_run_with_notify_dispatches_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that dry-run with --notify still calls _send_live."""
        calls: list[object] = []
        monkeypatch.setattr(main_mod, "_send_live", lambda *a, **k: calls.append((a, k)))
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        rc = _run_deployment(None, plan, scopes, None, notify=True, dry_run=True, protect_patterns=[], logger=_logger())
        assert rc == 0
        assert len(calls) == 1


class TestRunDeploymentRestartNoticeFailure:
    """Tests for the in-game-notice failure exit path."""

    def test_required_notice_failure_aborts(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a required in-game notice failure returns exit 1 before any writes."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: ["s1"])

        class _FailingRconSet(_FakeRconSet):
            def send(self, name: str, cmd: str) -> bool:
                """Sends the current payload."""
                self.sent.append((name, cmd))
                return False

        monkeypatch.setattr(main_mod, "_RconSet", _FailingRconSet)
        config = SimpleNamespace(docker=_docker_stub(in_game_notice_required=True), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "in_game_notice" in capsys.readouterr().out


class TestRunDeploymentWriteFailure:
    """Tests for :func:`_run_deployment` write-failure exit paths."""

    def test_server_write_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a server-scope write failure returns 1 and prints the recovery block."""
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message="disk full"))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "disk full" in capsys.readouterr().out

    def test_server_write_failure_with_stopped_containers_prints_recovery(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a server write failure with a prior stop prints the recovery block."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: [])
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=["s1"], exited_before_stop=[], failed=[], recovery=None))
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message="permission denied"))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        out = capsys.readouterr().out
        assert "RECOVERY REQUIRED" in out
        assert "permission denied" in out

    def test_client_write_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a client-scope write failure returns 1 and prints the status block."""
        plan = _stub_plan(partition=[])
        scopes = _Scopes(ScopeSet(client=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_client_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message="zip error"))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "zip error" in capsys.readouterr().out

    def test_server_scope_pre_hook_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a failing pre-hook aborts before any writes."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: [])
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=[], exited_before_stop=[], failed=["s1"], recovery=None))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "pre_hook" in capsys.readouterr().out


class TestRunDeploymentReloadFailure:
    """Tests for the RCON reload failure exit path."""

    def test_reload_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a failing RCON reload returns exit 1 and prints the failure line."""
        plan = _stub_plan(partition=["s1"], reload_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)

        class _ReloadFailRconSet(_FakeRconSet):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.send_results = {"c-s1": False}

        monkeypatch.setattr(main_mod, "_RconSet", _ReloadFailRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "reload" in capsys.readouterr().out


class TestRunDeploymentPostPhaseFailure:
    """Tests for the post-hook failure exit path."""

    def test_post_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a post-phase failure returns exit 1 and prints the stage and summary."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: [])
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=["s1"], exited_before_stop=[], failed=[], recovery=None))
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        post = SimpleNamespace(
            started=["s1"],
            healthy=[],
            start_failed=[],
            health_failed=["s1"],
            any_start_failure=False,
            any_health_failure=True,
            failure_stage="post_hook",
            error_summary=lambda: "health timeout on s1",
        )
        monkeypatch.setattr(main_mod, "execute_post_hook", lambda *a, **k: post)
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        out = capsys.readouterr().out
        assert "post_hook" in out
        assert "health timeout" in out


class TestRunDeploymentSuccess:
    """Tests for the full success path of :func:`_run_deployment`."""

    def test_server_scope_success_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a clean server-scope run returns 0."""
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0

    def test_full_success_with_restart_and_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests a full-success path with restart, post-hook health, and online notification."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: [])
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=["s1"], exited_before_stop=[], failed=[], recovery=None))
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        post = SimpleNamespace(
            started=["s1"],
            healthy=["s1"],
            start_failed=[],
            health_failed=[],
            any_start_failure=False,
            any_health_failure=False,
            failure_stage=None,
            error_summary=lambda: "",
        )
        monkeypatch.setattr(main_mod, "execute_post_hook", lambda *a, **k: post)
        live_calls: list[object] = []
        monkeypatch.setattr(main_mod, "_send_live", lambda *a, **k: live_calls.append(a))
        online_calls: list[object] = []
        monkeypatch.setattr(main_mod, "_send_online", lambda *a, **k: online_calls.append(a))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0
        assert len(live_calls) == 1
        assert len(online_calls) == 1

    def test_full_success_without_notify_skips_online(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that --notify=False skips the online notification even on success."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: [])
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=["s1"], exited_before_stop=[], failed=[], recovery=None))
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        post = SimpleNamespace(
            started=["s1"],
            healthy=["s1"],
            start_failed=[],
            health_failed=[],
            any_start_failure=False,
            any_health_failure=False,
            failure_stage=None,
            error_summary=lambda: "",
        )
        monkeypatch.setattr(main_mod, "execute_post_hook", lambda *a, **k: post)
        online_calls: list[object] = []
        monkeypatch.setattr(main_mod, "_send_online", lambda *a, **k: online_calls.append(a))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0
        assert online_calls == []
