# tests/deploy_pack/test_preflight.py

"""Tests for deploy_pack.preflight, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * aggregate failures: multiple independent errors reported together
  * halt-before-writes: PreflightError is raised, not a partial plan
  * drift check with symlinks (delegated to docker_runtime, smoke here)
  * missing container
  * partition-scoped vs global checks
  * orphan RP check (config load catches, but preflight sees final state)
  * realpath failure (delegated to docker_runtime)
  * in-game template validation (config load time; smoke check here)
  * [resource_pack.X] required keys
  * restart adapter: longest literal prefix, tie-breaking, default
  * sticky-max over actions
  * packing sets
  * §3.5: broken compose fatal for server scope, warning for client
  * §2.5: partition_unknown
  * §5.11: Discord template validation (via notifications, behaviour
    unchanged at this layer)
  * §8.4: RCON availability for restart_set members
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from minecraft.deploy_pack import preflight
from minecraft.deploy_pack.config_model import (
    BindMount,
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DiscordConfig,
    DockerConfig,
    InstanceConfig,
    ResourcePackConfig,
    ServiceMatchError,
    match_service_by_container,
    parse_go_duration,
)
from minecraft.deploy_pack.docker_runtime import ContainerState, Mount
from minecraft.deploy_pack.preflight import PreflightError, ScopeSet, _resolve_action, _resolve_paths_action, _sticky_max


@dataclass
class FakeRuntime:
    """A fake runtime for testing container orchestration.

    Attributes:
        states: Mapping of container names to their states.
        inspected: Names of containers that have been inspected.
        restarting_settled: Predefined settled states for restarting containers.
        ping_calls: Number of times ping has been called.
        published: Mapping of container names to their published ports.
        compose: The ComposeFile whose service binds are surfaced via
            :meth:`list_mounts`, so the preflight drift check (§3.17)
            sees a mount set matching the instance-root derivations.
    """

    states: dict[str, ContainerState] = field(default_factory=dict)
    inspected: list[str] = field(default_factory=list)
    restarting_settled: dict[str, ContainerState] = field(default_factory=dict)
    ping_calls: int = 0
    published: dict[str, dict] = field(default_factory=dict)
    compose: ComposeFile | None = None

    def ping(self) -> None:
        """Pings the service."""
        self.ping_calls += 1

    def inspect(self, name: str) -> ContainerState:
        """Inspects the target."""
        self.inspected.append(name)
        return self.states.get(name, ContainerState(name=name, exists=True, status="running", running=True, health="healthy", raw={"Mounts": []}))

    def wait_for_restarting_settle(self, names, total_timeout, poll_interval):
        """Waits for restarting containers to settle."""
        return self.restarting_settled or {n: self.inspect(n) for n in names}

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the compose service's binds for ``name``, if any.

        The preflight drift check (§3.17) compares the runtime's mount
        list against the instance-root derivation from compose. Wiring
        the same compose file here means a well-formed fixture sees a
        matching set without each test having to stub the mounts.
        """
        if self.compose is None:
            return []
        for svc in self.compose.services.values():
            if svc.container_name == name:
                return [Mount(source=str(b.host_source), destination=b.container_target) for b in svc.binds]
        return []

    def published_ports(self, name: str) -> dict:
        """Returns the published ports."""
        return self.published.get(name, {})


def _runtime(cfg: DeploymentConfig, **kwargs: Any) -> FakeRuntime:
    """Return a :class:`FakeRuntime` wired to ``cfg``'s compose file.

    The runtime surfaces the compose service binds via ``list_mounts``,
    so the preflight drift check (§3.17) sees a mount set that matches
    the instance-root derivations. Without this the check fails on every
    server-scope test because a bare ``FakeRuntime`` reports no mounts.
    """
    compose = cfg.compose.file if cfg.compose.ok else None
    return FakeRuntime(compose=compose, **kwargs)


def _compose_file(tmp_path: Path, members: dict[str, str] | None = None, *, secret_present: bool = True) -> ComposeFile:
    """Build a ComposeFile with one service per member.

    Each service has /data, /data/mods, and a healthcheck. When
    ``secret_present`` is True the service declares the rcon_password
    secret and the secret file is written to disk.
    """
    members = members or {}
    services: dict[str, ComposeService] = {}
    for name, container in members.items():
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        mods = tmp_path / "shared_mods"
        mods.mkdir(exist_ok=True)
        services[name] = ComposeService(
            name=name,
            container_name=container,
            binds=[BindMount(host_source=root, container_target="/data"), BindMount(host_source=mods, container_target="/data/mods")],
            stop_grace_period="10s",
            stop_signal=None,
            has_healthcheck=True,
            secrets=["rcon_password"] if secret_present else [],
            environment={"RCON_PORT": "25575"},
            env_files=[],
        )
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    services["nginx"] = ComposeService(
        name="nginx",
        container_name="nginx",
        binds=[BindMount(host_source=www, container_target="/usr/share/nginx/html")],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=False,
        secrets=[],
        environment={},
        env_files=[],
    )
    secret_path = tmp_path / "rcon.txt"
    secret_path.write_text("pw\n", encoding="utf-8")
    return ComposeFile(path=tmp_path / "docker-compose.yml", base_dir=tmp_path, services=services, secret_files={"rcon_password": secret_path})


def _config(
    tmp_path: Path,
    *,
    partition: list[str] | None = None,
    instances: dict[str, InstanceConfig] | None = None,
    compose_ok: bool = True,
    requested_instances: set[str] | None = None,
    partition_unknown: list[str] | None = None,
    resource_packs: dict[str, ResourcePackConfig] | None = None,
    restart_policy: dict[str, str] | None = None,
    discord: DiscordConfig | None = None,
    secret_present: bool = True,
    rcon_host: str | None = None,
) -> DeploymentConfig:
    partition = partition if partition is not None else ["survival"]
    instances = instances if instances is not None else {}
    compose: ComposeLoadResult
    if compose_ok:
        cf = _compose_file(tmp_path, {k: v.container for k, v in instances.items()}, secret_present=secret_present)
        compose = ComposeLoadResult(file=cf, error=None)
        # Mirror config_model._build_deployment_config: wire each instance
        # to its compose service and derive the timing fields. Without
        # this step, preflight correctly reports "no /data bind found"
        # and the test never reaches what it is actually asserting.
        for inst in instances.values():
            try:
                svc = match_service_by_container(cf, inst.container)
            except ServiceMatchError as exc:
                inst.service_match_error = str(exc)
                continue
            inst.service = svc
            if svc.stop_grace_period:
                inst.stop_grace_period_raw = svc.stop_grace_period
                try:
                    inst.stop_grace_seconds = parse_go_duration(svc.stop_grace_period)
                except ValueError as exc:
                    inst.stop_grace_parse_error = str(exc)
            inst.stop_signal = svc.stop_signal
    else:
        compose = ComposeLoadResult(file=None, error="compose broken")
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=www,
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="minecraft_client_{date}.zip",
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping={"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}},
        restart_policy=restart_policy
        or {
            "config/*": "restart",
            "kubejs/client_scripts/*": "none",
            "kubejs/server_scripts/*": "reload",
            "kubejs/startup_scripts/*": "restart+pack",
            "kubejs/assets/*": "none+pack",
            "kubejs/data/*": "reload",
            "mods/*": "restart",
        },
        instances=instances,
        partition=partition,
        partition_unknown=partition_unknown or [],
        requested_instances=requested_instances,
        resource_packs=resource_packs or {},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml", health_poll_seconds=1, preflight_restarting_wait_seconds=1, rcon_host=rcon_host),
        discord=discord or DiscordConfig(),
        webhook_url=None,
        compose=compose,
        mods_dir_toml=None,
    )


def test_resolve_action_longest_literal_prefix() -> None:
    """Tests that the longest literal prefix match is used when resolving an action."""
    policy = {"config/*": "restart", "config/special/*": "none"}
    assert _resolve_action("config/foo.toml", policy) == ("restart", "config/*")
    assert _resolve_action("config/special/a", policy) == ("none", "config/special/*")


def test_resolve_action_default() -> None:
    """Tests that _resolve_action returns the default policy action for an unlisted path."""
    policy = {"mods/*": "restart"}
    assert _resolve_action("unlisted/path", policy) == ("restart", "(default)")


def test_resolve_action_tie_break_by_length() -> None:
    """Same literal prefix, longer pattern wins."""
    policy = {"kubejs/*": "restart", "kubejs/assets/*": "none"}
    assert _resolve_action("kubejs/assets/x", policy) == ("none", "kubejs/assets/*")


def test_resolve_action_tie_break_alphabetical() -> None:
    """Same literal prefix and same length: lexicographically smaller wins."""
    policy = {"aa/*": "reload", "ab/*": "none"}
    assert _resolve_action("aa/x", policy) == ("reload", "aa/*")


def test_sticky_max() -> None:
    """Tests _sticky_max returns the strongest sticky option, combining flags when needed.

    Verifies that the maximum sticky value is selected from the input list,
    preserving any "+pack" modifier. An empty list returns "none".
    """
    assert _sticky_max(["none", "reload"]) == "reload"
    assert _sticky_max(["reload+pack", "restart"]) == "restart+pack"
    assert _sticky_max(["none+pack", "reload"]) == "reload+pack"
    assert _sticky_max([]) == "none"


def test_resolve_paths_action_returns_reasons() -> None:
    """Tests that resolving path actions returns the effective action and reasons for matching paths."""
    policy = {"mods/*": "restart", "kubejs/server_scripts/*": "reload"}
    paths = ["mods/a.jar", "mods/b.jar", "kubejs/server_scripts/craft.js"]
    effective, reasons = _resolve_paths_action(paths, policy)
    assert effective == "restart"
    assert len(reasons) == 1
    assert reasons[0].path_prefix == "mods/*"
    assert reasons[0].action == "restart"
    assert sorted(reasons[0].changed_paths) == ["mods/a.jar", "mods/b.jar"]


def test_resolve_paths_action_empty() -> None:
    """Test that an empty path list resolves to the 'none' action with no matched paths."""
    assert _resolve_paths_action([], {"a/*": "restart"}) == ("none", [])


def _instance(name: str, container: str, root: Path) -> InstanceConfig:
    root.mkdir(parents=True, exist_ok=True)
    return InstanceConfig(
        name=name,
        container=container,
        instance_root=root,
        config_path=root / "config",
        kubejs_path=root / "kubejs",
        server_properties_path=root / "server.properties",
    )


def test_preflight_raises_on_partition_unknown(tmp_path: Path) -> None:
    """Tests that preflight raises a PreflightError when an unknown partition is configured.

    Verifies that the resulting failure includes a diagnostic sourced from §2.5.
    """
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, partition_unknown=["nope"])
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any(f.source == "§2.5" for f in ei.value.failures)


def test_preflight_aggregates_multiple_failures(tmp_path: Path) -> None:
    """Two independent problems are both reported."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, partition_unknown=["nope"])
    cfg = DeploymentConfig(**{**cfg.__dict__, "compose": ComposeLoadResult(None, "broken")})
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    sources = {f.source for f in ei.value.failures}
    assert "§2.5" in sources
    assert "§3.5" in sources


def test_preflight_broken_compose_ok_for_client_scope(tmp_path: Path) -> None:
    """§3.5: --client alone tolerates a broken compose if www_dir is set."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, compose_ok=False)
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(client=True), False, False, False, runtime, None)
    assert plan.partition == ["survival"]


def test_preflight_missing_container_is_error(tmp_path: Path) -> None:
    """Tests that preflight raises PreflightError when the container is missing."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    runtime = _runtime(cfg, states={"mc-survival": ContainerState(name="mc-survival", exists=False, status="missing", running=False, health=None, raw=None)})
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any("missing" in f.message for f in ei.value.failures)


def test_preflight_restarting_after_wait_is_error(tmp_path: Path) -> None:
    """Tests that preflight fails when a container remains in the restarting state after waiting."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    stuck = ContainerState(name="mc-survival", exists=True, status="restarting", running=False, health=None, raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": stuck}, restarting_settled={"mc-survival": stuck})
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any("restarting" in f.message for f in ei.value.failures)


def test_preflight_no_instances_configured(tmp_path: Path) -> None:
    """Tests that preflight fails when no instances are configured."""
    cfg = _config(tmp_path, partition=[], instances={})
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any("no instances" in f.message for f in ei.value.failures)


def test_preflight_client_only_does_not_require_instances(tmp_path: Path) -> None:
    """--client never touches instances; empty config is fine."""
    cfg = _config(tmp_path, partition=[], instances={})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(client=True), False, False, False, runtime, None)
    assert plan.partition == []


def test_discord_live_template_missing_is_warning(tmp_path: Path) -> None:
    """Missing template → warn, not exit 3."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, discord=DiscordConfig(live_template=None))
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(client=True), False, True, False, runtime, None)
    assert plan.partition == ["survival"]


def test_discord_live_template_empty_is_error(tmp_path: Path) -> None:
    """Tests that preflight fails when the Discord live template is empty."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, discord=DiscordConfig(live_template=""))
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(client=True), False, True, False, runtime, None)
    assert any("live" in f.source for f in ei.value.failures)


def test_discord_live_template_unknown_placeholder(tmp_path: Path) -> None:
    """Tests that preflight fails when the Discord live template contains an unknown placeholder."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, discord=DiscordConfig(live_template="hello {nonexistent}"))
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(client=True), False, True, False, runtime, None)
    assert any("nonexistent" in f.message for f in ei.value.failures)


def test_discord_live_template_invalid_everywhere(tmp_path: Path) -> None:
    """Tests that preflight fails when the Discord live template is invalid in all contexts."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, discord=DiscordConfig(live_template="hello {sha256sum}"))
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(client=True), False, True, False, runtime, None)
    assert any("invalid everywhere" in f.message for f in ei.value.failures)


def test_discord_live_template_cross_template(tmp_path: Path) -> None:
    """A failure-template placeholder used in live → error."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, discord=DiscordConfig(live_template="hello {failure_stage}"))
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(client=True), False, True, False, runtime, None)
    assert any("cross-template" in f.message or "not valid" in f.message for f in ei.value.failures)


def test_discord_live_template_dry_run_validates_only_live(tmp_path: Path) -> None:
    """Under --dry-run, failure template is not validated (§5.11)."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        discord=DiscordConfig(live_template="ok {tool_version}", failure_template="{not_a_placeholder}"),
    )
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(client=True), False, True, True, runtime, None)
    assert plan.partition == ["survival"]


def test_preflight_none_set_when_no_changes(tmp_path: Path) -> None:
    """Verifies that no-change preflight marks the server as none_set with no reloads or restarts."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert plan.none_set == ["survival"]
    assert plan.reload_set == []
    assert plan.restart_set == []
    assert plan.pack_required is False


def test_preflight_restart_set_on_mods_change(tmp_path: Path) -> None:
    """Verifies that a mod addition triggers a restart and records the mods change."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir(exist_ok=True)
    (mods_dir / "existing.jar").write_bytes(b"x")
    index = tmp_path / "sync" / "downloads" / ".index"
    index.mkdir(parents=True)
    (index / "a.pw.toml").write_text('filename = "a.jar"\nside = "server"\n', encoding="utf-8")
    (tmp_path / "sync" / "downloads" / "a.jar").write_bytes(b"new content")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert "survival" in plan.restart_set
    assert plan.mods_change is not None
    assert "a.jar" in plan.mods_change.added


def test_preflight_targeted_server_skips_mods(tmp_path: Path) -> None:
    """§2.9: --server --instance does not touch mods_dir."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    mods_dir = tmp_path / "shared_mods"
    mods_dir.mkdir(exist_ok=True)
    (mods_dir / "orphan.jar").write_bytes(b"x")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, requested_instances={"survival"})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert plan.targeted is True
    assert plan.mods_drift or plan.warnings


def test_preflight_reload_set_on_kubejs_server_scripts(tmp_path: Path) -> None:
    """Tests that preflight adds an instance to the reload set when KubeJS server scripts change, without requiring a pack."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "server_scripts").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "server_scripts" / "craft.js").write_text("// new", encoding="utf-8")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert "survival" in plan.reload_set
    assert plan.pack_required is False


def test_preflight_pack_required_on_startup_scripts(tmp_path: Path) -> None:
    """Tests that preflight marks a pack as required when startup scripts change in server scope, and emits the pack-required warning."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts" / "new_block.js").write_text("// new", encoding="utf-8")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert "survival" in plan.restart_set
    assert plan.pack_required is True
    assert plan.pack_required_warning is not None


def test_preflight_rcon_missing_secret_when_port_published(tmp_path: Path) -> None:
    """§8.4: a running restart_set member on Option B needs the secret.

    The service is configured without the rcon_password secret and the
    published port forces Option B, so load_rcon_password fails and
    preflight must raise §8.4.
    """
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts" / "x.js").write_text("// new", encoding="utf-8")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, secret_present=False)
    runtime = _runtime(cfg, published={"mc-survival": {"25575/tcp": [("0.0.0.0", 25575)]}})
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any("§8.4" in f.source for f in ei.value.failures)
    assert any("rcon_password" in f.message for f in ei.value.failures)


def test_preflight_rcon_remote_host_without_published_port(tmp_path: Path) -> None:
    """§8.4: rcon_host requires exactly one published mapping."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts" / "x.js").write_text("// new", encoding="utf-8")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, rcon_host="10.0.0.5")
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert any("§8.4" in f.source for f in ei.value.failures)


def test_preflight_rcon_skipped_when_no_restart_set(tmp_path: Path) -> None:
    """§8.4: the check only fires when a restart_set member is running.

    With no changes, restart_set is empty and no RCON check runs, even
    though the secret file is missing.
    """
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, secret_present=False, rcon_host="10.0.0.5")
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, None)
    assert plan.restart_set == []


def test_preflight_rcon_skipped_in_dry_run(tmp_path: Path) -> None:
    """§2.6: --dry-run performs no docker operations, including no RCON."""
    root = tmp_path / "survival"
    inst = _instance("survival", "mc-survival", root)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs" / "startup_scripts" / "x.js").write_text("// new", encoding="utf-8")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, secret_present=False, rcon_host="10.0.0.5")
    runtime = _runtime(cfg)
    plan = preflight.run_preflight(cfg, ScopeSet(server=True), False, False, True, runtime, None)
    assert "survival" in plan.restart_set
