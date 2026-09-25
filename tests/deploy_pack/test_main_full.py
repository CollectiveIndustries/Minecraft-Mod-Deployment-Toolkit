# tests/deploy_pack/test_main_full.py

"""Closes the last coverage gap in deploy_pack.main.

Where ``test_main_extended`` stubs out :func:`_send_live`,
:func:`_send_failure`, and :func:`_send_online` to keep its tests focused
on the runtime flow, this module lets those functions execute so their
bodies are measured. Also covers:

  * the ``if notify:`` branches inside the write-failure, reload-failure,
    and post-phase-failure paths of :func:`_run_deployment`
  * the success-with-notify path that reaches both ``_send_live`` and
    ``_send_online``
  * the reload skip branches (missing instance, container not running)
  * the remaining :func:`_prompt_for_unmarked` interactive answers
    (``c``, ``b``), the ``EOFError`` fallback, and the invalid-input
    retry loop.

Notification dispatch is stubbed at the ``notifications`` module level
(``notify_live`` / ``notify_failure`` / ``notify_online``) so the
composition functions in ``main.py`` run but no I/O happens.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.main import _prompt_for_unmarked, _run_deployment, _Scopes, _send_failure, _send_live, _send_online
from minecraft.deploy_pack.preflight import ScopeSet


def _logger() -> logging.Logger:
    """Return a module-scoped logger for tests that only need a sink."""
    return logging.getLogger("test_main_full")


def _docker_stub(**overrides: object) -> SimpleNamespace:
    """A ``config.docker`` stub with every field the runtime path reads."""
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


def _full_config(tmp_path: Path) -> SimpleNamespace:
    """A config stub carrying every attribute the notify path reads."""
    return SimpleNamespace(
        config_dir=tmp_path / "config.d",
        modpack_dir=tmp_path / "pack",
        protect_file=tmp_path / "protect.toml",
        webhook_url="http://hook.invalid",
        docker=_docker_stub(),
        instances={},
        discord=SimpleNamespace(
            player_roles=["player"],
            operator_roles=["op"],
            diagnostic_template="diag-template",
            live_template="live-template",
            failure_template="fail-template",
            online_template="online-template",
        ),
    )


def _stub_plan(**overrides: object) -> SimpleNamespace:
    """A PreflightPlan-shaped object with every field _run_deployment reads."""
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


class _FakeRconSet:
    """A no-op stand-in for :class:`_RconSet`."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.sent: list[tuple[str, str]] = []
        self.cancelled: list[tuple[list[str], str]] = []

    def probe(self, name: str) -> bool:
        """Return False; recovery treats everything as unreachable."""
        return False

    def send(self, name: str, cmd: str) -> bool:
        """Record the command and report success."""
        self.sent.append((name, cmd))
        return True

    def dispatch_cancel(self, names: list[str], msg: str) -> None:
        """Record a cancellation dispatch."""
        self.cancelled.append((list(names), msg))


def _patch_notify(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[tuple[object, ...], dict[str, object]]]]:
    """Stub the notification dispatchers and return per-kind call logs.

    ``tool_version`` and ``render_timestamp_now`` are patched so the
    ``_send_*`` bodies run without their real dependencies.
    """
    monkeypatch.setattr(main_mod, "tool_version", lambda: "1.0")
    monkeypatch.setattr(main_mod.notifications, "render_timestamp_now", lambda: "ts")
    calls: dict[str, list[tuple[tuple[object, ...], dict[str, object]]]] = {"live": [], "failure": [], "online": []}
    monkeypatch.setattr(main_mod.notifications, "notify_live", lambda *a, **k: (calls["live"].append((a, k)), "ok")[1])
    monkeypatch.setattr(main_mod.notifications, "notify_failure", lambda *a, **k: (calls["failure"].append((a, k)), "ok")[1])
    monkeypatch.setattr(main_mod.notifications, "notify_online", lambda *a, **k: (calls["online"].append((a, k)), "ok")[1])
    return calls


def _patch_server_scope_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``deploy_server_scope`` to succeed without touching the plan.

    The reload-skip tests only need ``_run_deployment`` to reach the
    reload loop; running the real scope would require a fully-populated
    plan (``mods_dir``, ``sync_dir``, etc.) that is orthogonal to the
    branches under test.
    """
    monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))


class TestSendLiveDirect:
    """Tests for :func:`_send_live` composition."""

    def test_composes_context_and_dispatches(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that _send_live composes a LiveContext and dispatches it."""
        calls = _patch_notify(monkeypatch)
        config = _full_config(tmp_path)
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        _send_live(config, plan, scopes, None, None, False, _logger())
        assert len(calls["live"]) == 1
        args, _kwargs = calls["live"][0]
        assert args[0] == "live-template"
        assert args[2] == "http://hook.invalid"
        assert args[3] == ["player", "op"]
        ctx = args[1]
        assert ctx.dry_run is False
        assert ctx.instance_list == ["s1"]

    def test_dry_run_sets_flag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that the dry_run flag is threaded into the LiveContext."""
        calls = _patch_notify(monkeypatch)
        config = _full_config(tmp_path)
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        _send_live(config, plan, scopes, None, None, True, _logger())
        ctx = calls["live"][0][0][1]
        assert ctx.dry_run is True


class TestSendFailureDirect:
    """Tests for :func:`_send_failure` composition."""

    def test_dispatches_with_context(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that _send_failure composes a FailureContext and dispatches it."""
        calls = _patch_notify(monkeypatch)
        config = _full_config(tmp_path)
        _send_failure(config, "mid_scope_write", "disk full", {"s1": "stopped"}, _logger())
        assert len(calls["failure"]) == 1
        args, _kwargs = calls["failure"][0]
        assert args[0] == "fail-template"
        assert args[2] == "http://hook.invalid"
        ctx = args[1]
        assert ctx.failure_stage == "mid_scope_write"
        assert ctx.error == "disk full"
        assert ctx.container_status == {"s1": "stopped"}


class TestSendOnlineDirect:
    """Tests for :func:`_send_online` composition."""

    def test_dispatches_with_context(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that _send_online composes an OnlineContext and dispatches it."""
        calls = _patch_notify(monkeypatch)
        config = _full_config(tmp_path)
        _send_online(config, {"s1": "online, healthy"}, _logger())
        assert len(calls["online"]) == 1
        args, _kwargs = calls["online"][0]
        assert args[0] == "online-template"
        ctx = args[1]
        assert ctx.player_roles == ["player"]
        assert ctx.container_status == {"s1": "online, healthy"}


class TestRunDeploymentNotifyFailures:
    """Tests for the notify=True branches on each failure path."""

    def test_server_write_failure_with_notify(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a server write failure with notify=True dispatches a failure message."""
        calls = _patch_notify(monkeypatch)
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message="disk full"))
        config = _full_config(tmp_path)
        config.instances = {"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert len(calls["failure"]) == 1

    def test_client_write_failure_with_notify(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a client write failure with notify=True dispatches a failure message."""
        calls = _patch_notify(monkeypatch)
        plan = _stub_plan(partition=[])
        scopes = _Scopes(ScopeSet(client=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_client_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message="zip error"))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = _full_config(tmp_path)
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert len(calls["failure"]) == 1

    def test_reload_failure_with_notify(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a reload failure with notify=True dispatches a failure message."""
        calls = _patch_notify(monkeypatch)
        plan = _stub_plan(partition=["s1"], reload_set=["s1"], container_states={"s1": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)

        class _ReloadFailRconSet(_FakeRconSet):
            def send(self, name: str, cmd: str) -> bool:
                """Sends data."""
                self.sent.append((name, cmd))
                return False

        monkeypatch.setattr(main_mod, "_RconSet", _ReloadFailRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = _full_config(tmp_path)
        config.instances = {"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert len(calls["failure"]) == 1

    def test_post_failure_with_notify(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a post-phase failure with notify=True dispatches a failure message."""
        calls = _patch_notify(monkeypatch)
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
            error_summary=lambda: "health timeout",
        )
        monkeypatch.setattr(main_mod, "execute_post_hook", lambda *a, **k: post)
        config = _full_config(tmp_path)
        config.instances = {"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert len(calls["failure"]) == 1


class TestRunDeploymentNotifySuccess:
    """Tests for the notify=True success path."""

    def test_success_fires_live_and_online(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a successful run with notify=True fires both live and online."""
        calls = _patch_notify(monkeypatch)
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
        config = _full_config(tmp_path)
        config.instances = {"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=True, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0
        assert len(calls["live"]) == 1
        assert len(calls["online"]) == 1


class TestRunDeploymentReloadSkips:
    """Tests for the two skip branches inside the reload loop."""

    def test_missing_instance_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a reload_set member without an instance entry is skipped."""
        plan = _stub_plan(partition=[], reload_set=["ghost"], container_states={"ghost": _stub_state(running=True)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        _patch_server_scope_success(monkeypatch)
        config = _full_config(tmp_path)
        config.instances = {}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0

    def test_not_running_container_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a reload_set member whose container is not running is skipped."""
        plan = _stub_plan(partition=["s1"], reload_set=["s1"], container_states={"s1": _stub_state(running=False)})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        _patch_server_scope_success(monkeypatch)
        config = _full_config(tmp_path)
        config.instances = {"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)}
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0


class TestPromptInteractiveAnswers:
    """Tests for the remaining interactive answers in _prompt_for_unmarked."""

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, dict[str, object]]:
        config = SimpleNamespace(modpack_dir=tmp_path / "pack", config_dir=tmp_path / "cfg")
        (config.modpack_dir / ".index").mkdir(parents=True)
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: [{"file": "y.jar"}])
        monkeypatch.setattr(main_mod.deps, "find_unmarked", lambda md, entries: [SimpleNamespace(filename="y.jar", reason="bad side")])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(deployment_tool_review={}))
        saved: dict[str, object] = {}

        def fake_save(path: Path, review: dict, logger: object) -> None:
            saved["review"] = dict(review)

        monkeypatch.setattr(main_mod, "save_side_overrides", fake_save)
        return (config, saved)

    def test_client_answer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the [c] answer records the jar as client."""
        config, saved = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr("builtins.input", lambda prompt="": "c")
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"y.jar": "client"}

    def test_both_answer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the [b] answer records the jar as both."""
        config, saved = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr("builtins.input", lambda prompt="": "b")
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"y.jar": "both"}

    def test_eof_falls_back_to_defer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an EOFError is treated as [d] and no entry is written."""
        config, saved = self._setup(tmp_path, monkeypatch)

        def _raise_eof(prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {}

    def test_invalid_then_valid_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an invalid answer is rejected and the prompt retries."""
        config, saved = self._setup(tmp_path, monkeypatch)
        answers = iter(["zzz", "s"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        _prompt_for_unmarked(config, _logger())
        assert saved["review"] == {"y.jar": "server"}
