# tests/deploy_pack/test_integration.py

"""End-to-end integration tests: real modules, real files, fake Docker SDK.

Unit tests stub each module's collaborators. These do the opposite: real
files, real config loading, real preflight, real scope writes. Only the
Docker SDK is faked, at the outermost boundary where it belongs.

The intent is to catch seam bugs that per-module unit tests cannot see:
a renamed keyword between main and preflight, a swapped positional
between main and hooks, a return type that a downstream module reads
with the wrong attribute name, a state machine that only fails when two
modules interleave.

Instance names vs container names
---------------------------------

Deploy-state sets (``restart_set``, ``warned_and_running``,
``stopped_by_deployment``, ``preflight_states``) are keyed by instance
name. The Docker SDK is keyed by container name. Production code passes
``container_of`` at every hooks boundary; these tests do the same so
they exercise the real call shape.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import docker
import pytest

from minecraft.deploy_pack import deps, notifications
from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.config_model import load_deployment_config
from minecraft.deploy_pack.docker_runtime import Clock, DockerRuntime
from minecraft.deploy_pack.hooks import (
    RecoveryContext,
    compute_warned_and_running,
    execute_post_hook,
    execute_pre_hook,
)
from minecraft.deploy_pack.overrides import load_side_overrides, save_side_overrides
from minecraft.deploy_pack.preflight import ScopeSet, run_preflight
from minecraft.deploy_pack.scope_client import deploy_client_scope
from minecraft.deploy_pack.scope_resource_pack import deploy_resource_pack_scope
from minecraft.deploy_pack.scope_server import deploy_server_scope

# ---------------------------------------------------------------------------
# Fake Docker SDK -- the only thing faked in this file
# ---------------------------------------------------------------------------


class _FakeExecResult:
    """Stand-in for the docker SDK's exec_run result."""

    def __init__(self, exit_code: int = 0, output: bytes = b"") -> None:
        """Record the exit code and output bytes."""
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    """Minimal container stand-in with mutable state and call recording."""

    def __init__(
        self,
        name: str,
        *,
        running: bool = True,
        health: str | None = "healthy",
        mounts: list[dict] | None = None,
        ports: dict | None = None,
    ) -> None:
        """Initialise the fake with the given starting state."""
        self.name = name
        self._running = running
        self._health = health
        self._mounts = mounts or []
        self._ports = ports or {}
        self.stop_raises: BaseException | None = None
        self.start_raises: BaseException | None = None
        self.stop_calls: list[int | None] = []
        self.start_calls: int = 0
        self.exec_calls: list[list[str]] = []
        self.exec_results: list[_FakeExecResult] = []

    @property
    def attrs(self) -> dict:
        """Return the container's attributes in the SDK's shape."""
        state: dict[str, Any] = {
            "Status": "running" if self._running else "exited",
            "Running": self._running,
        }
        if self._health is not None:
            state["Health"] = {"Status": self._health}
        return {
            "State": state,
            "Mounts": self._mounts,
            "NetworkSettings": {"Ports": self._ports},
            "Name": f"/{self.name}",
        }

    def stop(self, timeout: int | None = None) -> Any:
        """Record the timeout and flip the running flag on success."""
        self.stop_calls.append(timeout)
        if self.stop_raises is not None:
            raise self.stop_raises
        self._running = False
        return None

    def start(self) -> None:
        """Flip the running flag on success; raise if configured to."""
        self.start_calls += 1
        if self.start_raises is not None:
            raise self.start_raises
        self._running = True

    def exec_run(self, args: list[str]) -> _FakeExecResult:
        """Record the call and return the next queued result."""
        self.exec_calls.append(list(args))
        if self.exec_results:
            return self.exec_results.pop(0)
        return _FakeExecResult(0, b"")


class _FakeContainersCollection:
    """Stand-in for the docker SDK's ``client.containers`` mapping."""

    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        """Store the container map by reference."""
        self._containers = containers

    def get(self, name: str) -> _FakeContainer:
        """Return the named container or raise NotFound."""
        if name not in self._containers:
            raise docker.errors.NotFound(name)
        return self._containers[name]


class _FakeDockerClient:
    """Stand-in for the docker SDK client."""

    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        """Build the containers collection and reset the ping counter."""
        self.containers = _FakeContainersCollection(containers)
        self.ping_calls = 0

    def ping(self) -> bool:
        """Record a ping and return True."""
        self.ping_calls += 1
        return True


def _runtime(containers: dict[str, _FakeContainer]) -> DockerRuntime:
    """Build a DockerRuntime wired to a fake SDK client."""
    return DockerRuntime(client=_FakeDockerClient(containers))


def _container_of(instances: dict[str, str]) -> dict[str, str]:
    """Return instance name -> container name. Mirrors main._run_deployment."""
    return dict(instances)


# ---------------------------------------------------------------------------
# Repo builder
# ---------------------------------------------------------------------------


def _write_repo(
    root: Path,
    *,
    instances: dict[str, str] | None = None,
    mods: dict[str, str] | None = None,
    config_files: dict[str, str] | None = None,
    kubejs_files: dict[str, str] | None = None,
    resource_pack: str | None = None,
) -> Path:
    """Build a minimal but valid deploy_pack repo. Return config.d path.

    ``instances`` maps instance name to container_name. ``mods`` maps
    jar filename to side. ``config_files`` and ``kubejs_files`` map
    relative paths to file contents.

    The docker block sets ``restart_wait_seconds = 0`` so the pipeline
    does not sleep for the in-game restart window when tests exercise a
    live run. The window itself is exercised by unit tests.
    """
    instances = instances or {"survival": "mc-survival"}
    mods = mods or {}
    config_files = config_files or {}
    kubejs_files = kubejs_files or {}

    config_dir = root / "config.d"
    config_dir.mkdir(parents=True, exist_ok=True)

    compose = ["services:"]
    for name, container in instances.items():
        compose += [
            f"  {name}:",
            f"    container_name: {container}",
            "    healthcheck:",
            '      test: ["CMD", "true"]',
            "    volumes:",
            f"      - ./data/{name}:/data",
            "      - ./shared/mods:/data/mods",
            "    stop_grace_period: 10s",
        ]
    compose += [
        "  nginx:",
        "    container_name: nginx",
        "    volumes:",
        "      - ./www:/usr/share/nginx/html",
        "secrets:",
        "  rcon_password:",
        "    file: ./secrets/rcon.txt",
    ]
    (root / "docker-compose.yml").write_text("\n".join(compose) + "\n", encoding="utf-8")
    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "rcon.txt").write_text("pw\n", encoding="utf-8")

    toml = [
        'instance_discovery = "explicit"',
        'sync_root = "./sync"',
        'modpack_dir = "./sync/downloads"',
        'output_filename = "client_{date}.zip"',
        'download_base_url = "http://minecraft/downloads"',
        "",
        "[sync_mapping]",
        'config = "config"',
        'kubejs = "kubejs"',
        'resourcepacks = { resource_pack = "@www/resourcepacks", client = "resourcepacks" }',
        "",
        "[docker]",
        'compose_file = "./docker-compose.yml"',
        "restart_wait_seconds = 0",
        "",
        "[restart_policy]",
        '"config/*" = "restart"',
        '"kubejs/server_scripts/*" = "reload"',
        '"kubejs/startup_scripts/*" = "restart+pack"',
        '"mods/*" = "restart"',
    ]
    for name, container in instances.items():
        toml += [
            "",
            f"[instances.{name}]",
            f'container = "{container}"',
            'config_mode = "merge"',
            'kubejs_mode = "delete"',
        ]
    if resource_pack is not None:
        for name in instances:
            toml += [
                "",
                f"[resource_pack.{name}]",
                f'filename = "{resource_pack}"',
                "required = true",
                'prompt = ""',
            ]
    (config_dir / "deploy_pack.toml").write_text("\n".join(toml) + "\n", encoding="utf-8")

    for sub in ("config", "kubejs", "resourcepacks"):
        (root / "sync" / sub).mkdir(parents=True, exist_ok=True)
    for rel, content in config_files.items():
        p = root / "sync" / "config" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in kubejs_files.items():
        p = root / "sync" / "kubejs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    index_dir = root / "sync" / "downloads" / ".index"
    index_dir.mkdir(parents=True, exist_ok=True)
    for filename, side in mods.items():
        stem = filename.removesuffix(".jar")
        (index_dir / f"{stem}.pw.toml").write_text(
            f'filename = "{filename}"\nside = "{side}"\n',
            encoding="utf-8",
        )
        (root / "sync" / "downloads" / filename).write_bytes(b"jar-content")

    (root / "www").mkdir(exist_ok=True)
    (root / "shared" / "mods").mkdir(parents=True, exist_ok=True)
    for name in instances:
        (root / "data" / name).mkdir(parents=True, exist_ok=True)

    return config_dir


def _running_container(name: str, *, data_dir: Path, mods_dir: Path) -> _FakeContainer:
    """Return a fake container whose mounts satisfy §3.17 for the given dirs."""
    mounts = [
        {"Type": "bind", "Source": str(data_dir.resolve()), "Destination": "/data"},
        {"Type": "bind", "Source": str(mods_dir.resolve()), "Destination": "/data/mods"},
    ]
    return _FakeContainer(name, running=True, health="healthy", mounts=mounts)


def _make_recovery_ctx() -> RecoveryContext:
    """Build a RecoveryContext whose callbacks always succeed."""

    def probe(container_name: str) -> bool:
        """Return True for every container."""
        return True

    def cancel(names: list[str]) -> None:
        """Do nothing; the fake runtime has no in-game client."""
        return None

    return RecoveryContext(
        reachability_probe=probe,
        cancel_notice_fn=cancel,
        cancel_ready_timeout=0.0,
        poll_interval=1.0,
        clock=Clock(),
    )


# ---------------------------------------------------------------------------
# 1. config load -> preflight -> none_set
# ---------------------------------------------------------------------------


def test_config_load_and_preflight_no_changes(tmp_path: Path) -> None:
    """Full config load and preflight on a clean repo yields none_set."""
    config_dir = _write_repo(tmp_path)
    cfg = load_deployment_config(config_dir)
    assert cfg.compose.ok
    assert cfg.partition == ["survival"]

    containers = {
        "mc-survival": _running_container(
            "mc-survival",
            data_dir=tmp_path / "data" / "survival",
            mods_dir=tmp_path / "shared" / "mods",
        )
    }

    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(containers))
    assert plan.none_set == ["survival"]
    assert plan.reload_set == []
    assert plan.restart_set == []
    assert plan.pack_required is False


# ---------------------------------------------------------------------------
# 2. mod change -> preflight sees restart_set -> scope writes it -> next run
# ---------------------------------------------------------------------------


def test_mod_change_produces_restart_set_and_scope_writes_it(tmp_path: Path) -> None:
    """A mod in the index but not on disk is a change -> restart_set -> written."""
    config_dir = _write_repo(tmp_path, mods={"newmod.jar": "server"})
    cfg = load_deployment_config(config_dir)

    containers = {
        "mc-survival": _running_container(
            "mc-survival",
            data_dir=tmp_path / "data" / "survival",
            mods_dir=tmp_path / "shared" / "mods",
        )
    }
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(containers))
    assert "survival" in plan.restart_set
    assert plan.mods_change is not None
    assert "newmod.jar" in plan.mods_change.added

    result = deploy_server_scope(cfg, plan, [], None)
    assert result.success
    assert (tmp_path / "shared" / "mods" / "newmod.jar").is_file()

    plan2 = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(containers))
    assert plan2.none_set == ["survival"]


# ---------------------------------------------------------------------------
# 3. preflight -> hooks: full lifecycle stop, write, start
# ---------------------------------------------------------------------------


def test_preflight_to_hooks_full_lifecycle(tmp_path: Path) -> None:
    """compute_warned_and_running -> stop -> write -> start, end to end."""
    config_dir = _write_repo(tmp_path, mods={"newmod.jar": "server"})
    cfg = load_deployment_config(config_dir)

    container = _running_container(
        "mc-survival",
        data_dir=tmp_path / "data" / "survival",
        mods_dir=tmp_path / "shared" / "mods",
    )
    runtime = _runtime({"mc-survival": container})
    cnames = _container_of({"survival": "mc-survival"})

    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert plan.restart_set == ["survival"]

    warned = compute_warned_and_running(
        runtime=runtime,
        restart_set=plan.restart_set,
        preflight_states=plan.container_states,
        container_of=cnames,
    )
    assert warned == ["survival"]

    recovery_ctx = _make_recovery_ctx()
    pre = execute_pre_hook(
        runtime=runtime,
        warned_and_running=warned,
        stop_timeouts={"survival": 10},
        recovery_ctx=recovery_ctx,
        container_of=cnames,
    )
    assert pre.stopped == ["survival"]
    assert container.stop_calls == [10]
    assert container._running is False

    write_result = deploy_server_scope(cfg, plan, [], None)
    assert write_result.success
    assert (tmp_path / "shared" / "mods" / "newmod.jar").is_file()

    post = execute_post_hook(
        runtime=runtime,
        stopped_by_deployment=list(pre.stopped),
        preflight_states=plan.container_states,
        health_timeout=10,
        poll_interval=1,
        container_of=cnames,
    )
    assert post.started == ["survival"]
    assert post.healthy == ["survival"]
    assert container._running is True
    assert container.start_calls == 1


# ---------------------------------------------------------------------------
# 4. hooks recovery when a stop fails mid-phase
# ---------------------------------------------------------------------------


def test_hooks_stop_failure_triggers_recovery(tmp_path: Path) -> None:
    """One stop fails, another succeeds -> recovery starts the succeeded one."""
    config_dir = _write_repo(
        tmp_path,
        instances={"a": "mc-a", "b": "mc-b"},
        mods={"newmod.jar": "server"},
    )
    cfg = load_deployment_config(config_dir)

    ca = _running_container("mc-a", data_dir=tmp_path / "data" / "a", mods_dir=tmp_path / "shared" / "mods")
    cb = _running_container("mc-b", data_dir=tmp_path / "data" / "b", mods_dir=tmp_path / "shared" / "mods")
    cb.stop_raises = docker.errors.DockerException("simulated stop failure")
    runtime = _runtime({"mc-a": ca, "mc-b": cb})
    cnames = _container_of({"a": "mc-a", "b": "mc-b"})

    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert plan.restart_set == ["a", "b"]

    recovery_ctx = _make_recovery_ctx()
    pre = execute_pre_hook(
        runtime=runtime,
        warned_and_running=["a", "b"],
        stop_timeouts={"a": 10, "b": 10},
        recovery_ctx=recovery_ctx,
        container_of=cnames,
    )
    assert pre.stopped == ["a"]
    assert pre.failed == ["b"]
    assert pre.recovery is not None
    assert pre.recovery.started == ["a"]
    assert ca._running is True
    assert ca.start_calls == 1


# ---------------------------------------------------------------------------
# 5. start failure is reported, not raised
# ---------------------------------------------------------------------------


def test_hooks_post_phase_start_failure_is_reported() -> None:
    """A start failure appears on PostHookResult; no exception."""
    c = _FakeContainer("mc-x", running=False, health=None)
    c.start_raises = docker.errors.DockerException("boom")
    runtime = _runtime({"mc-x": c})

    post = execute_post_hook(
        runtime=runtime,
        stopped_by_deployment=["x"],
        preflight_states={},
        health_timeout=5,
        poll_interval=0.01,
        container_of=_container_of({"x": "mc-x"}),
    )
    assert post.start_failed == ["x"]
    assert post.started == []
    assert post.failure_stage == "post_hook"


# ---------------------------------------------------------------------------
# 6. resource-pack publish -> server.properties update
# ---------------------------------------------------------------------------


def test_resource_pack_publish_and_properties_update(tmp_path: Path) -> None:
    """The RP scope publishes a ZIP and updates all four managed keys."""
    config_dir = _write_repo(tmp_path, resource_pack="pack.zip")
    (tmp_path / "sync" / "resourcepacks" / "pack.zip").write_bytes(b"ZIPDATA")
    cfg = load_deployment_config(config_dir)
    props = tmp_path / "data" / "survival" / "server.properties"
    props.write_text("motd=hi\n", encoding="utf-8")

    container = _running_container(
        "mc-survival",
        data_dir=tmp_path / "data" / "survival",
        mods_dir=tmp_path / "shared" / "mods",
    )
    plan = run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime({"mc-survival": container}))

    result = deploy_resource_pack_scope(cfg, plan, [], None)
    assert result.success
    assert result.any_published
    assert (tmp_path / "www" / "resourcepacks" / "pack.zip").is_file()

    body = props.read_text(encoding="utf-8")
    assert "motd=hi" in body
    assert "require-resource-pack=true" in body
    assert "resource-pack=http://minecraft/downloads/resourcepacks/pack.zip" in body
    assert "resource-pack-prompt=" in body
    assert "resource-pack-sha1=" in body


# ---------------------------------------------------------------------------
# 7. client scope ZIP + changelog baseline
# ---------------------------------------------------------------------------


def test_client_scope_zip_and_initial_changelog(tmp_path: Path) -> None:
    """First run reports an initial build; the ZIP contains the client mod set."""
    config_dir = _write_repo(
        tmp_path,
        mods={"clientonly.jar": "client", "serveronly.jar": "server"},
        config_files={"mod.toml": "value"},
        kubejs_files={"server_scripts/craft.js": "// js"},
    )
    cfg = load_deployment_config(config_dir)
    result = deploy_client_scope(cfg, False, [], None)
    assert result.success
    assert result.initial_build is True
    assert result.zip_path is not None

    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    assert "mods/clientonly.jar" in names
    assert "mods/serveronly.jar" not in names
    assert "config/mod.toml" in names
    assert "kubejs/server_scripts/craft.js" in names


def test_client_scope_second_run_sees_diff(tmp_path: Path) -> None:
    """A second run with a new mod produces a diff (not initial build)."""
    config_dir = _write_repo(tmp_path, mods={"first.jar": "client"})
    cfg = load_deployment_config(config_dir)
    first = deploy_client_scope(cfg, False, [], None)
    assert first.initial_build is True

    index_dir = tmp_path / "sync" / "downloads" / ".index"
    (index_dir / "second.pw.toml").write_text('filename = "second.jar"\nside = "client"\n', encoding="utf-8")
    (tmp_path / "sync" / "downloads" / "second.jar").write_bytes(b"jar-content")

    second = deploy_client_scope(cfg, False, [], None)
    assert second.success
    assert second.initial_build is False
    assert second.baseline_zip == first.zip_path
    assert second.report is not None
    assert "second.jar" in second.report.added_mods


# ---------------------------------------------------------------------------
# 8. overrides save/load round trip with byte preservation
# ---------------------------------------------------------------------------


def test_overrides_round_trip_preserves_unrelated_bytes(tmp_path: Path) -> None:
    """save_side_overrides then load_side_overrides round-trips cleanly."""
    path = tmp_path / "side_overrides.toml"
    path.write_text(
        '# a comment\n\n[by_id]\n"123" = "both"\n',
        encoding="utf-8",
    )
    save_side_overrides(
        path,
        {"newmod.jar": "client", "other.jar": "skipped"},
        timestamp="2026-09-25T12:00:00Z",
    )
    text = path.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert '"123" = "both"' in text
    assert '"newmod.jar" = "client"' in text
    assert '"other.jar" = "skipped"' in text
    assert "# Last generated: 2026-09-25T12:00:00Z" in text

    loaded = load_side_overrides(path)
    assert loaded.by_id == {"123": "both"}
    assert loaded.deployment_tool_review == {"newmod.jar": "client", "other.jar": "skipped"}


# ---------------------------------------------------------------------------
# 9. deps closure with real jars on disk
# ---------------------------------------------------------------------------


def test_deps_closure_pulls_required_dependency(tmp_path: Path) -> None:
    """A jar that requires another modId pulls it into the closure."""
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)

    with zipfile.ZipFile(downloads / "a.jar", "w") as zf:
        zf.writestr(
            "META-INF/mods.toml",
            '[[mods]]\nmodId = "a"\n[[dependencies.a]]\nmodId = "lib"\nmandatory = true\nside = "BOTH"\n',
        )
    with zipfile.ZipFile(downloads / "lib.jar", "w") as zf:
        zf.writestr("META-INF/mods.toml", '[[mods]]\nmodId = "lib"\n')

    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    (index / "lib.pw.toml").write_text('filename = "lib.jar"\nside = "client"\n', encoding="utf-8")

    deps.clear_manifest_cache()
    entries = deps.load_prism_index(index)
    seed = deps.filter_prism_entries_by_side(entries, "server")
    result = deps.expand_with_required(entries, seed, "server", downloads, None)
    names = {e["file"] for e in result.entries}
    assert "a.jar" in names
    assert "lib.jar" in names


# ---------------------------------------------------------------------------
# 10. main() -> full CLI pipeline
# ---------------------------------------------------------------------------


def test_main_server_dry_run_exits_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with --server --dry-run exercises the full preflight seam."""
    config_dir = _write_repo(tmp_path)

    containers = {
        "mc-survival": _running_container(
            "mc-survival",
            data_dir=tmp_path / "data" / "survival",
            mods_dir=tmp_path / "shared" / "mods",
        )
    }
    runtime = _runtime(containers)
    monkeypatch.setattr(main_mod, "DockerRuntime", lambda: runtime)

    code = main_mod.main(["--server", "--dry-run", "--config-dir", str(config_dir)])
    assert code == 0


def test_main_debug_deps_exits_0(tmp_path: Path) -> None:
    """main() with --debug-deps and no scope prints diagnostics and exits 0."""
    config_dir = _write_repo(tmp_path, mods={"a.jar": "client"})
    code = main_mod.main(["--debug-deps", "--config-dir", str(config_dir)])
    assert code == 0


def test_main_missing_config_dir_exits_3(tmp_path: Path) -> None:
    """A nonexistent config tree is exit 3."""
    code = main_mod.main(["--client", "--config-dir", str(tmp_path / "nope")])
    assert code == 3


def test_main_unknown_flag_exits_2() -> None:
    """An unknown --flag is exit 2."""
    code = main_mod.main(["--not-a-real-flag"])
    assert code == 2


def test_main_dry_run_without_scope_exits_2() -> None:
    """--dry-run without a scope is exit 2."""
    code = main_mod.main(["--dry-run"])
    assert code == 2


def test_main_help_exits_0() -> None:
    """--help exits 0."""
    assert main_mod.main(["--help"]) == 0


# ---------------------------------------------------------------------------
# 11. notifications via the fake poster
# ---------------------------------------------------------------------------


def test_notify_live_dispatches_through_fake_poster() -> None:
    """notify_live calls the poster once with rendered content and roles."""
    calls: list[dict] = []

    def poster(url: str, content: str, allowed_mentions: dict, timeout: float) -> tuple[bool, str | None]:
        """Record the call and report success."""
        calls.append({"url": url, "content": content, "mentions": allowed_mentions})
        return (True, None)

    ctx = notifications.LiveContext(
        tool_version="2.0.0",
        timestamp="2026-09-25T12:00:00Z",
        requested_scopes=["server"],
        instance_list=["survival"],
    )
    result = notifications.notify_live(
        "{tool_version} {instance_list}",
        ctx,
        webhook_url="https://example/webhook",
        all_role_ids=["111"],
        poster=poster,
    )
    assert result.success
    assert len(calls) == 1
    assert calls[0]["url"] == "https://example/webhook"
    assert "2.0.0" in calls[0]["content"]
    assert calls[0]["mentions"]["roles"] == ["111"]


# ---------------------------------------------------------------------------
# 12. main() -> hooks seam, with a mod change forcing a real restart
# ---------------------------------------------------------------------------


def test_main_server_with_change_runs_stop_write_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full main run: preflight sees a change -> hooks stop and start the container."""
    config_dir = _write_repo(tmp_path, mods={"newmod.jar": "server"})

    container = _running_container(
        "mc-survival",
        data_dir=tmp_path / "data" / "survival",
        mods_dir=tmp_path / "shared" / "mods",
    )
    runtime = _runtime({"mc-survival": container})
    monkeypatch.setattr(main_mod, "DockerRuntime", lambda: runtime)

    code = main_mod.main(["--server", "--non-interactive", "--config-dir", str(config_dir)])
    assert code == 0
    assert container.stop_calls, "container should have been stopped"
    assert container.start_calls == 1, "container should have been started back up"
    assert (tmp_path / "shared" / "mods" / "newmod.jar").is_file()
