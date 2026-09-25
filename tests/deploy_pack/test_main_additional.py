# tests/deploy_pack/test_main_additional.py

"""Additional tests for deploy_pack.main targeting full file coverage.

Complements ``test_main.py`` (outer shell) and ``test_main_extended.py``
(runtime sequence). Here we cover the remaining uncovered statements in
``src/minecraft/deploy_pack/main.py``:

  * :func:`_run`'s dispatch body: config load, prompt gate, preflight
    invocation, warning emission, and the ``--audit-mods`` branch
  * :func:`main`'s six-clause ``except`` ladder
  * :func:`_run_debug_deps` early-return and diagnostic-output paths
  * :func:`_run_diagnostic_notify`
  * the ``_prompt_for_unmarked`` gating condition inside ``_run``
  * residual ``_run_deployment`` lines: the ``wait_s > 0`` sleep after a
    delivered notice, the resource-pack write-failure branch, and the
    ``_run_writes`` fallback messages when a scope returns
    ``failure_message=None``.

All collaborators are monkeypatched so that only ``main.py`` executes.
Config objects are :class:`types.SimpleNamespace` shapes carrying the
attributes ``main.py`` actually reads; anything else is out of scope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.errors import ConfigError, DeployPackError, DockerRuntimeError, DockerUnavailableError, RuntimeDeployError, UsageError
from minecraft.deploy_pack.main import _run_debug_deps, _run_deployment, _run_diagnostic_notify, _Scopes
from minecraft.deploy_pack.preflight import PreflightError, ScopeSet


def _logger() -> logging.Logger:
    """Return a module-scoped logger for tests that only need a sink."""
    return logging.getLogger("test_main_additional")


def _stub_config(tmp_path: Path) -> SimpleNamespace:
    """A DeploymentConfig-shaped object carrying only fields main.py reads."""
    return SimpleNamespace(
        config_dir=tmp_path / "config.d",
        modpack_dir=tmp_path / "pack",
        protect_file=tmp_path / "protect.toml",
        webhook_url="http://example.invalid/hook",
        discord=SimpleNamespace(
            player_roles=["player"], operator_roles=["op"], diagnostic_template="diag", live_template="live", failure_template="fail", online_template="online"
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


class _RunHandles(SimpleNamespace):
    """Recorded call sites and stubbed collaborators for a single _run test."""


def _install_run_stubs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: object | None = None,
    preflight_result: object | None = None,
    preflight_error: Exception | None = None,
    deployment_result: int = 0,
    deployment_error: Exception | None = None,
    load_config_error: Exception | None = None,
) -> _RunHandles:
    """Wire every collaborator :func:`_run` touches and return call records.

    The returned namespace carries the config, the plan, and lists
    recording each stub invocation so tests can assert both on the
    return code and on which branches fired.
    """
    cfg = config if config is not None else _stub_config(tmp_path)
    plan = preflight_result if preflight_result is not None else _stub_plan()
    handles = _RunHandles(config=cfg, plan=plan, prompt_calls=[], debug_deps_calls=[], diagnostic_calls=[], preflight_calls=[], deployment_calls=[])

    def _load(**kwargs: object) -> object:
        if load_config_error is not None:
            raise load_config_error
        return cfg

    def _preflight(**kwargs: object) -> object:
        handles.preflight_calls.append(kwargs)
        if preflight_error is not None:
            raise preflight_error
        return plan

    def _deploy(**kwargs: object) -> int:
        handles.deployment_calls.append(kwargs)
        if deployment_error is not None:
            raise deployment_error
        return deployment_result

    monkeypatch.setattr(main_mod, "load_deployment_config", _load)
    monkeypatch.setattr(main_mod, "run_preflight", _preflight)
    monkeypatch.setattr(main_mod, "DockerRuntime", lambda: SimpleNamespace())
    monkeypatch.setattr(main_mod, "load_protect_patterns", lambda *a, **k: [])
    monkeypatch.setattr(main_mod, "_run_deployment", _deploy)
    monkeypatch.setattr(main_mod, "_prompt_for_unmarked", lambda *a, **k: handles.prompt_calls.append((a, k)))
    monkeypatch.setattr(main_mod, "_run_debug_deps", lambda *a, **k: handles.debug_deps_calls.append((a, k)))
    monkeypatch.setattr(main_mod, "_run_diagnostic_notify", lambda *a, **k: handles.diagnostic_calls.append((a, k)))
    return handles


class TestRunDispatch:
    """Tests for :func:`_run`'s dispatch body."""

    def test_server_scope_calls_preflight_and_deployment(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a server-scope run reaches preflight and deployment once each."""
        h = _install_run_stubs(monkeypatch, tmp_path, deployment_result=0)
        rc = main_mod.main(["--server", "--non-interactive"])
        assert rc == 0
        assert len(h.preflight_calls) == 1
        assert len(h.deployment_calls) == 1

    def test_prompt_called_for_server_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that the unmarked-jar prompt fires for a live server scope."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        main_mod.main(["--server"])
        assert len(h.prompt_calls) == 1

    def test_prompt_skipped_for_dry_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --dry-run suppresses the unmarked-jar prompt."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        main_mod.main(["--server", "--dry-run"])
        assert len(h.prompt_calls) == 0

    def test_prompt_skipped_for_non_interactive(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --non-interactive suppresses the unmarked-jar prompt."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        main_mod.main(["--server", "--non-interactive"])
        assert len(h.prompt_calls) == 0

    def test_prompt_skipped_for_resource_pack_only_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that a resource-pack-only scope does not trigger the prompt."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        main_mod.main(["--resource-pack"])
        assert len(h.prompt_calls) == 0


class TestRunNoScopeBranches:
    """Tests for :func:`_run`'s no-scope early-return branches."""

    def test_debug_deps_without_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --debug-deps with no scope prints diagnostics and exits 0."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        rc = main_mod.main(["--debug-deps"])
        assert rc == 0
        assert len(h.debug_deps_calls) == 1
        assert len(h.preflight_calls) == 0

    def test_debug_deps_and_notify_without_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --notify with no scope dispatches the diagnostic message."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        rc = main_mod.main(["--debug-deps", "--notify"])
        assert rc == 0
        assert len(h.debug_deps_calls) == 1
        assert len(h.diagnostic_calls) == 1

    def test_debug_deps_with_scope_continues(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --debug-deps alongside a scope prints diagnostics then continues."""
        h = _install_run_stubs(monkeypatch, tmp_path)
        rc = main_mod.main(["--server", "--non-interactive", "--debug-deps"])
        assert rc == 0
        assert len(h.debug_deps_calls) == 1
        assert len(h.preflight_calls) == 1


class TestRunWarnings:
    """Tests for warning emission from :func:`_run`."""

    def test_warnings_logged_in_normal_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that each plan warning is logged at WARN in a normal run."""
        plan = _stub_plan(warnings=["stale pack required"])
        _install_run_stubs(monkeypatch, tmp_path, preflight_result=plan)
        with caplog.at_level(logging.WARNING):
            main_mod.main(["--server", "--non-interactive"])
        assert any("stale pack required" in record.getMessage() for record in caplog.records)

    def test_warnings_suppressed_in_dry_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that --dry-run suppresses plan warning emission at WARN."""
        plan = _stub_plan(warnings=["stale pack required"])
        _install_run_stubs(monkeypatch, tmp_path, preflight_result=plan)
        with caplog.at_level(logging.WARNING):
            main_mod.main(["--server", "--dry-run"])
        assert not any("stale pack required" in record.getMessage() for record in caplog.records)


class TestRunPreflightError:
    """Tests for the preflight-failure exit path in :func:`_run`."""

    def test_preflight_error_logs_each_line_and_returns_3(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that a multi-line PreflightError logs each line and returns exit 3."""
        failures = [SimpleNamespace(source="member-a", message="first problem"), SimpleNamespace(source="member-b", message="second problem")]
        _install_run_stubs(monkeypatch, tmp_path, preflight_error=PreflightError(failures))
        with caplog.at_level(logging.ERROR):
            rc = main_mod.main(["--server", "--non-interactive"])
        assert rc == 3
        assert "first problem" in caplog.text
        assert "second problem" in caplog.text


class TestRunDeploymentErrors:
    """Tests for the Docker error exits in :func:`_run`."""

    def test_docker_unavailable_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that a DockerUnavailableError from deployment returns exit 1."""
        _install_run_stubs(monkeypatch, tmp_path, deployment_error=DockerUnavailableError("daemon gone"))
        with caplog.at_level(logging.ERROR):
            rc = main_mod.main(["--server", "--non-interactive"])
        assert rc == 1
        assert "daemon gone" in caplog.text

    def test_docker_runtime_error_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that a DockerRuntimeError from deployment returns exit 1."""
        _install_run_stubs(monkeypatch, tmp_path, deployment_error=DockerRuntimeError("socket dropped"))
        with caplog.at_level(logging.ERROR):
            rc = main_mod.main(["--server", "--non-interactive"])
        assert rc == 1
        assert "socket dropped" in caplog.text


class TestRunAuditMods:
    """Tests for the ``--audit-mods`` branch inside :func:`_run`."""

    def test_audit_mods_without_textual_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that --audit-mods without Textual installed exits 1 with a hint."""
        monkeypatch.setattr(main_mod.prompt_ui, "HAS_TEXTUAL", False)
        rc = main_mod.main(["--audit-mods"])
        assert rc == 1
        assert "textual" in capsys.readouterr().err

    def test_audit_mods_dispatches_to_run_audit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Tests that --audit-mods with Textual loads config and dispatches run_audit."""
        monkeypatch.setattr(main_mod.prompt_ui, "HAS_TEXTUAL", True)
        config = _stub_config(tmp_path)
        monkeypatch.setattr(main_mod, "load_deployment_config", lambda **kwargs: config)
        calls: list[object] = []

        def _fake_run_audit(cfg: object, log: object) -> int:
            calls.append(cfg)
            return 0

        monkeypatch.setattr(main_mod.prompt_ui, "run_audit", _fake_run_audit)
        rc = main_mod.main(["--audit-mods"])
        assert rc == 0
        assert calls == [config]

    def test_audit_mods_config_error_returns_3(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a ConfigError during the audit-mods config load exits 3."""
        monkeypatch.setattr(main_mod.prompt_ui, "HAS_TEXTUAL", True)

        def boom(**kwargs: object) -> object:
            raise ConfigError("audit cfg bad")

        monkeypatch.setattr(main_mod, "load_deployment_config", boom)
        rc = main_mod.main(["--audit-mods"])
        assert rc == 3
        assert "audit cfg bad" in capsys.readouterr().err


class TestMainExceptLadder:
    """Tests for :func:`main`'s six-clause except ladder."""

    def test_usage_error_returns_2(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a UsageError raised by _run maps to exit code 2."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(UsageError("bad flag")))
        rc = main_mod.main(["--server"])
        assert rc == 2
        assert "bad flag" in capsys.readouterr().err

    def test_config_error_returns_3(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a ConfigError escaping _run maps to exit code 3."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(ConfigError("cfg bad")))
        rc = main_mod.main(["--server"])
        assert rc == 3
        assert "cfg bad" in capsys.readouterr().err

    def test_runtime_deploy_error_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a RuntimeDeployError escaping _run maps to exit code 1."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeDeployError("rt bad")))
        rc = main_mod.main(["--server"])
        assert rc == 1
        assert "rt bad" in capsys.readouterr().err

    def test_deploy_pack_error_uses_its_exit_code(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a DeployPackError uses its own ``exit_code`` attribute."""
        err = DeployPackError("pack bad")
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(err))
        rc = main_mod.main(["--server"])
        assert rc == err.exit_code
        assert "pack bad" in capsys.readouterr().err

    def test_system_exit_with_int_returns_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that SystemExit with an int payload becomes the process exit code."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(SystemExit(7)))
        assert main_mod.main(["--server"]) == 7

    def test_system_exit_with_none_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that SystemExit with no payload maps to exit code 0."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
        assert main_mod.main(["--server"]) == 0

    def test_system_exit_with_string_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that SystemExit with a non-int payload maps to exit code 0."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(SystemExit("message")))
        assert main_mod.main(["--server"]) == 0

    def test_generic_exception_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that an unexpected exception is traced and mapped to exit code 1."""
        monkeypatch.setattr(main_mod, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unexpected")))
        rc = main_mod.main(["--server"])
        assert rc == 1
        assert "unexpected" in capsys.readouterr().err


class TestRunDebugDeps:
    """Tests for :func:`_run_debug_deps`."""

    def test_missing_index_prints_error(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tests that a missing .index directory prints an error and returns."""
        config = _stub_config(tmp_path)
        _run_debug_deps(config, _logger())
        assert "not found" in capsys.readouterr().err

    def test_empty_index_prints_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that an empty index prints an error and returns."""
        config = _stub_config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: [])
        _run_debug_deps(config, _logger())
        assert "No mod entries" in capsys.readouterr().err

    def test_prints_diagnostic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a populated index prints the formatted diagnostic."""
        config = _stub_config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: [{"file": "a.jar", "side": "server"}])
        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(is_empty=lambda: True))
        monkeypatch.setattr(main_mod.deps, "filter_prism_entries_by_side", lambda entries, side: [])
        monkeypatch.setattr(main_mod.deps, "format_diagnostic", lambda **k: "DIAGNOSTIC")
        _run_debug_deps(config, _logger())
        assert "DIAGNOSTIC" in capsys.readouterr().out

    def test_applies_non_empty_overrides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that non-empty side overrides are applied before diagnostic formatting."""
        config = _stub_config(tmp_path)
        (config.modpack_dir / ".index").mkdir(parents=True)
        original = [{"file": "a.jar"}]
        applied: list[object] = []
        monkeypatch.setattr(main_mod.deps, "load_prism_index", lambda p: original)
        monkeypatch.setattr(main_mod, "load_side_overrides", lambda p: SimpleNamespace(is_empty=lambda: False))
        monkeypatch.setattr(main_mod, "apply_side_overrides", lambda entries, ovr: (applied.append(entries), original)[1])
        monkeypatch.setattr(main_mod.deps, "filter_prism_entries_by_side", lambda entries, side: [])
        monkeypatch.setattr(main_mod.deps, "format_diagnostic", lambda **k: "DIAGNOSTIC")
        _run_debug_deps(config, _logger())
        assert applied == [original]


class TestRunDiagnosticNotify:
    """Tests for :func:`_run_diagnostic_notify`."""

    def test_dispatches_with_expected_arguments(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that the diagnostic notify call receives the config-derived arguments."""
        config = _stub_config(tmp_path)
        monkeypatch.setattr(main_mod, "tool_version", lambda: "1.0")
        monkeypatch.setattr(main_mod.notifications, "render_timestamp_now", lambda: "ts")
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(main_mod.notifications, "notify_diagnostic", lambda *a, **k: calls.append((a, k)))
        _run_diagnostic_notify(config, _logger())
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "diag"
        assert args[2] == "http://example.invalid/hook"
        assert args[3] == ["player", "op"]
        assert kwargs["logger"] is not None


class TestRunDeploymentWait:
    """Tests for the in-game wait period after a delivered notice."""

    def test_wait_sleeps_for_configured_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that a positive restart_wait_seconds sleeps via Clock().sleep."""
        plan = _stub_plan(partition=["s1"], restart_set=["s1"], container_states={"s1": _stub_state()})
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "compute_warned_and_running", lambda *a, **k: ["s1"])
        sleeps: list[float] = []

        class _FakeClock:
            def sleep(self, seconds: float) -> None:
                """Pauses execution for a default duration."""
                sleeps.append(seconds)

        monkeypatch.setattr(main_mod, "Clock", _FakeClock)
        monkeypatch.setattr(main_mod, "execute_pre_hook", lambda *a, **k: SimpleNamespace(stopped=[], exited_before_stop=[], failed=[], recovery=None))
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        config = SimpleNamespace(docker=_docker_stub(restart_wait_seconds=45), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0
        assert sleeps == [45.0]


class TestRunWritesFallbacks:
    """Tests for :func:`_run_deployment`'s scope-specific write-failure messages."""

    def test_server_scope_default_message(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a server scope with a None failure message uses the default text."""
        plan = _stub_plan(partition=["s1"])
        scopes = _Scopes(ScopeSet(server=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message=None))
        config = SimpleNamespace(docker=_docker_stub(), instances={"s1": SimpleNamespace(container="c-s1", stop_grace_seconds=10)})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "server scope failed" in capsys.readouterr().out

    def test_client_scope_default_message(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a client scope with a None failure message uses the default text."""
        plan = _stub_plan(partition=[])
        scopes = _Scopes(ScopeSet(client=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_client_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message=None))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "client scope failed" in capsys.readouterr().out

    def test_resource_pack_scope_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """Tests that a resource-pack write failure returns 1 with the RP default message."""
        plan = _stub_plan(partition=[])
        scopes = _Scopes(ScopeSet(resource_pack=True), with_resources=False)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_resource_pack_scope", lambda *a, **k: SimpleNamespace(success=False, failure_message=None))
        monkeypatch.setattr(main_mod, "recover_stopped_containers", lambda *a, **k: None)
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 1
        assert "resource-pack scope failed" in capsys.readouterr().out


class TestRunWritesAllScopes:
    """Tests for a run that passes through every scope in :func:`_run_writes`."""

    def test_all_three_scopes_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that server + client + resource-pack all succeeding returns 0."""
        plan = _stub_plan(partition=[])
        scopes = _Scopes(ScopeSet(server=True, client=True, resource_pack=True), with_resources=True)
        monkeypatch.setattr(main_mod, "_RconSet", _FakeRconSet)
        monkeypatch.setattr(main_mod, "deploy_server_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        monkeypatch.setattr(main_mod, "deploy_client_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        monkeypatch.setattr(main_mod, "deploy_resource_pack_scope", lambda *a, **k: SimpleNamespace(success=True, failure_message=None))
        config = SimpleNamespace(docker=_docker_stub(), instances={})
        rc = _run_deployment(config, plan, scopes, runtime=None, notify=False, dry_run=False, protect_patterns=[], logger=_logger())
        assert rc == 0
