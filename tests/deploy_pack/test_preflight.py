# tests/deploy_pack/test_preflight.py

"""Tests for deploy_pack.preflight, Project_Specs.md §2.5, §3.5, §3.6, §3.7, §3.8, §4.6, §4.12, §8.4.

Coverage:

  * §2.5  - unknown --instance name -> exit 3
  * §3.5  - broken compose is fatal for --server, tolerated for --client
            when www_dir is set
  * §4.6.1 - restart-policy matching: longest literal prefix; ties by
             total pattern length then lexicographic; unlisted paths
             default to restart
  * §4.6.2 - sticky-max: +pack sticks through the max
  * §4.6.3 - reasons carry the contributing pattern and changed paths
  * §4.6.4 - partitioning into none_set / reload_set / restart_set
  * §4.12  - container state policy: missing, restarting past the
             bounded wait, running without .State.Health
  * §8.4   - RCON availability required for a running restart_set
             member

Every check is exercised through the public ``run_preflight`` entry
point against a real ``DeploymentConfig`` and a fake Docker SDK client.
The restart adapter is spec-defined behavior (§4.6) that currently
lives behind underscore-prefixed helpers; the tests below drive it
through ``run_preflight`` so they pin the plan the operator's pipeline
actually consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
from minecraft.deploy_pack.preflight import PreflightError, PreflightPlan, ScopeSet, run_preflight

# ---------------------------------------------------------------------------
# Fake Docker SDK surface
# ---------------------------------------------------------------------------


@dataclass
class _FakeRuntime:
    """Minimal stand-in for DockerRuntime.

    ``list_mounts`` mirrors the compose service's binds so the §3.17
    drift check sees a matching set when the compose file is well-formed.
    """

    states: dict[str, ContainerState] = field(default_factory=dict)
    restarting_settled: dict[str, ContainerState] = field(default_factory=dict)
    published: dict[str, dict] = field(default_factory=dict)
    compose: ComposeFile | None = None
    ping_calls: int = 0

    def ping(self) -> None:
        """Record a ping call and succeed."""
        self.ping_calls += 1

    def inspect(self, name: str) -> ContainerState:
        """Return the configured state or a healthy running default."""
        return self.states.get(
            name,
            ContainerState(name=name, exists=True, status="running", running=True, health="healthy", raw={"Mounts": []}),
        )

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the compose service's binds for ``name``, or empty."""
        if self.compose is None:
            return []
        for svc in self.compose.services.values():
            if svc.container_name == name:
                return [Mount(source=str(b.host_source), destination=b.container_target) for b in svc.binds]
        return []

    def published_ports(self, name: str) -> dict:
        """Return the container's published ports, if configured."""
        return self.published.get(name, {})

    def wait_for_restarting_settle(self, names: Any, total_timeout: Any, poll_interval: Any) -> dict[str, ContainerState]:
        """Return the pre-configured settled states, or re-inspect each name."""
        return self.restarting_settled or {n: self.inspect(n) for n in names}


# ---------------------------------------------------------------------------
# Config / runtime builders
# ---------------------------------------------------------------------------


def _compose_file(tmp_path: Path, members: dict[str, str], *, secret_present: bool = True) -> ComposeFile:
    """Build a ComposeFile with one service per member plus nginx."""
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
    return ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services=services,
        secret_files={"rcon_password": secret_path},
    )


def _instance(name: str, container: str, root: Path) -> InstanceConfig:
    """Return an InstanceConfig rooted at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    return InstanceConfig(
        name=name,
        container=container,
        instance_root=root,
        config_path=root / "config",
        kubejs_path=root / "kubejs",
        server_properties_path=root / "server.properties",
    )


def _config(
    tmp_path: Path,
    *,
    partition: list[str] | None = None,
    instances: dict[str, InstanceConfig] | None = None,
    compose_ok: bool = True,
    partition_unknown: list[str] | None = None,
    requested_instances: set[str] | None = None,
    resource_packs: dict[str, ResourcePackConfig] | None = None,
    restart_policy: dict[str, str] | None = None,
    rcon_host: str | None = None,
    secret_present: bool = True,
    www_dir: Path | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with sane defaults and the given overrides."""
    partition = partition if partition is not None else ["survival"]
    instances = instances if instances is not None else {}
    compose: ComposeLoadResult
    if compose_ok:
        cf = _compose_file(tmp_path, {k: v.container for k, v in instances.items()}, secret_present=secret_present)
        compose = ComposeLoadResult(file=cf, error=None)
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
    www = www_dir if www_dir is not None else tmp_path / "www"
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
        sync_mapping={
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
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
        docker=DockerConfig(
            compose_file=tmp_path / "docker-compose.yml",
            health_poll_seconds=1,
            preflight_restarting_wait_seconds=1,
            rcon_host=rcon_host,
        ),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=compose,
        mods_dir_toml=None,
    )


def _runtime(cfg: DeploymentConfig, **kwargs: Any) -> _FakeRuntime:
    """Return a _FakeRuntime wired to ``cfg``'s compose file."""
    compose = cfg.compose.file if cfg.compose.ok else None
    return _FakeRuntime(compose=compose, **kwargs)


def _write_sync_files(tmp_path: Path, mapping_key: str, files: dict[str, str]) -> None:
    """Write content under sync_root/<mapping_key>/<rel> for every entry."""
    root = tmp_path / "sync" / mapping_key
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _write_index(tmp_path: Path, entries: dict[str, str]) -> None:
    """Write .pw.toml files and placeholder jars for a filename -> side mapping."""
    idx = tmp_path / "sync" / "downloads" / ".index"
    idx.mkdir(parents=True, exist_ok=True)
    for filename, side in entries.items():
        stem = filename.replace(".jar", "")
        (idx / f"{stem}.pw.toml").write_text(
            f'filename = "{filename}"\nside = "{side}"\n',
            encoding="utf-8",
        )
        (tmp_path / "sync" / "downloads" / filename).write_bytes(b"x")


def _basic_instance(tmp_path: Path) -> tuple[dict[str, InstanceConfig], list[str]]:
    """One survival instance rooted under tmp_path/data/survival."""
    inst = _instance("survival", "mc-survival", tmp_path / "data" / "survival")
    return ({"survival": inst}, ["survival"])


# ---------------------------------------------------------------------------
# §2.5: unknown --instance names
# ---------------------------------------------------------------------------


def test_unknown_instance_name_is_exit_3(tmp_path: Path) -> None:
    """§2.5: --instance X where X is not configured -> preflight error."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(tmp_path, partition=partition, instances=instances, partition_unknown=["nope"])
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any(f.source == "§2.5" for f in ei.value.failures)


# ---------------------------------------------------------------------------
# §3.5: broken compose scoping
# ---------------------------------------------------------------------------


def test_broken_compose_is_fatal_for_server_scope(tmp_path: Path) -> None:
    """§3.5: a broken compose is exit 3 when --server is in scope."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(tmp_path, partition=partition, instances=instances, compose_ok=False)
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any(f.source == "§3.5" for f in ei.value.failures)


def test_broken_compose_is_tolerated_for_client_scope_when_www_dir_is_set(tmp_path: Path) -> None:
    """§3.5: --client alone tolerates a broken compose as long as www_dir is known."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(tmp_path, partition=partition, instances=instances, compose_ok=False)
    plan = run_preflight(cfg, ScopeSet(client=True), False, False, False, _runtime(cfg))
    assert isinstance(plan, PreflightPlan)
    assert plan.partition == partition


# ---------------------------------------------------------------------------
# §4.6.1: restart-policy matching
# ---------------------------------------------------------------------------


def test_restart_policy_longest_literal_prefix_wins(tmp_path: Path) -> None:
    """§4.6.1: config/special/* beats config/* for a config/special path."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "config", {"special/foo.toml": "new"})
    policy = {"config/*": "restart", "config/special/*": "none"}
    cfg = _config(tmp_path, partition=partition, instances=instances, restart_policy=policy)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.member_plans["survival"].server_action == "none"


def test_restart_policy_tie_broken_by_total_pattern_length(tmp_path: Path) -> None:
    """§4.6.1: same literal prefix -> longer total pattern wins."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"assets/x.txt": "new"})
    policy = {"kubejs/*": "restart", "kubejs/assets/*": "none"}
    cfg = _config(tmp_path, partition=partition, instances=instances, restart_policy=policy)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.member_plans["survival"].server_action == "none"


def test_restart_policy_unlisted_path_defaults_to_restart(tmp_path: Path) -> None:
    """§4.6.1: an unlisted path defaults to restart."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "config", {"unknown/thing.txt": "new"})
    policy = {"mods/*": "restart"}
    cfg = _config(tmp_path, partition=partition, instances=instances, restart_policy=policy)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.member_plans["survival"].server_action == "restart"


# ---------------------------------------------------------------------------
# §4.6.2: sticky max
# ---------------------------------------------------------------------------


def test_sticky_max_promotes_reload_to_reload_pack_when_any_input_has_pack(tmp_path: Path) -> None:
    """§4.6.2: +pack sticks through the max even when the max-ranked input does not carry it."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(
        tmp_path,
        "kubejs",
        {
            "server_scripts/craft.js": "new",
            "startup_scripts/block.js": "new",
        },
    )
    policy = {"kubejs/server_scripts/*": "reload", "kubejs/startup_scripts/*": "reload+pack"}
    cfg = _config(tmp_path, partition=partition, instances=instances, restart_policy=policy)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.member_plans["survival"].effective_action == "reload+pack"


def test_sticky_max_restart_beats_reload(tmp_path: Path) -> None:
    """§4.6.2: restart outranks reload when both fire on the same instance."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(
        tmp_path,
        "kubejs",
        {
            "server_scripts/craft.js": "new",
            "startup_scripts/block.js": "new",
        },
    )
    policy = {"kubejs/server_scripts/*": "reload", "kubejs/startup_scripts/*": "restart"}
    cfg = _config(tmp_path, partition=partition, instances=instances, restart_policy=policy)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.member_plans["survival"].effective_action == "restart"


# ---------------------------------------------------------------------------
# §4.6.4: partitioning into none_set / reload_set / restart_set
# ---------------------------------------------------------------------------


def test_no_changes_puts_member_into_none_set(tmp_path: Path) -> None:
    """§4.6.4: with nothing to do, the member is in none_set."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.none_set == ["survival"]
    assert plan.reload_set == []
    assert plan.restart_set == []


def test_reload_path_puts_member_into_reload_set(tmp_path: Path) -> None:
    """§4.6.4: kubejs/server_scripts/* -> reload -> reload_set."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"server_scripts/craft.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.reload_set == ["survival"]
    assert plan.pack_required is False


def test_restart_path_puts_member_into_restart_set(tmp_path: Path) -> None:
    """§4.6.4: a mods change -> restart -> restart_set."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "config", {"file.toml": "new"})
    _write_index(tmp_path, {"a.jar": "server"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert "survival" in plan.restart_set


def test_pack_required_is_true_when_action_ends_in_pack(tmp_path: Path) -> None:
    """§4.6.2: pack_required is derived from the effective action."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.pack_required is True


# ---------------------------------------------------------------------------
# §4.6.3: reasons
# ---------------------------------------------------------------------------


def test_reasons_carry_the_contributing_pattern(tmp_path: Path) -> None:
    """§4.6.3: the reason entry names the pattern that produced the effective action."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"server_scripts/craft.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    reasons = plan.member_plans["survival"].reasons
    assert any(r.path_prefix == "kubejs/server_scripts/*" for r in reasons)


# ---------------------------------------------------------------------------
# §4.12: container state policy
# ---------------------------------------------------------------------------


def test_missing_container_is_exit_3(tmp_path: Path) -> None:
    """§4.12 / §8.9: a missing container is a fatal state."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    missing = ContainerState(name="mc-survival", exists=False, status="missing", running=False, health=None, raw=None)
    runtime = _runtime(cfg, states={"mc-survival": missing})
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any("missing" in f.message for f in ei.value.failures)


def test_restarting_past_the_bounded_wait_is_exit_3(tmp_path: Path) -> None:
    """§4.12: a container still restarting after the bounded wait is fatal."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    stuck = ContainerState(name="mc-survival", exists=True, status="restarting", running=False, health=None, raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": stuck}, restarting_settled={"mc-survival": stuck})
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any("restarting" in f.message for f in ei.value.failures)


def test_running_without_health_block_is_exit_3(tmp_path: Path) -> None:
    """§4.12: a running container without .State.Health is fatal."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    no_health = ContainerState(name="mc-survival", exists=True, status="running", running=True, health=None, raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": no_health})
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any(".State.Health" in f.message for f in ei.value.failures)


def test_paused_container_is_exit_3(tmp_path: Path) -> None:
    """§4.12: paused is fatal."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    paused = ContainerState(name="mc-survival", exists=True, status="paused", running=False, health=None, raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": paused})
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any("paused" in f.message for f in ei.value.failures)


def test_exited_container_is_treated_as_stopped(tmp_path: Path) -> None:
    """§4.12: exited containers pass preflight."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    exited = ContainerState(name="mc-survival", exists=True, status="exited", running=False, health=None, raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": exited})
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert plan.partition == partition


# ---------------------------------------------------------------------------
# §8.4: RCON availability for running restart_set members
# ---------------------------------------------------------------------------


def test_rcon_missing_secret_is_exit_3_when_restart_member_is_running(tmp_path: Path) -> None:
    """§8.4: a running restart_set member must have a selectable RCON transport."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances, secret_present=False)
    runtime = _runtime(cfg, published={"mc-survival": {"25575/tcp": [("0.0.0.0", 25575)]}})
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any(f.source.startswith("§8.4") for f in ei.value.failures)


def test_rcon_remote_host_without_published_port_is_exit_3(tmp_path: Path) -> None:
    """§8.4: rcon_host set but the RCON port is not published -> exit 3."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances, rcon_host="10.0.0.5")
    runtime = _runtime(cfg)
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any(f.source.startswith("§8.4") for f in ei.value.failures)


def test_rcon_check_is_skipped_when_restart_set_is_empty(tmp_path: Path) -> None:
    """§8.4: no restart_set members means no RCON check is required."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances, secret_present=False, rcon_host="10.0.0.5")
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.restart_set == []


def test_rcon_check_is_skipped_under_dry_run(tmp_path: Path) -> None:
    """§2.6: --dry-run performs no docker operations, including RCON selection."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances, secret_present=False, rcon_host="10.0.0.5")
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, True, _runtime(cfg))
    assert "survival" in plan.restart_set
