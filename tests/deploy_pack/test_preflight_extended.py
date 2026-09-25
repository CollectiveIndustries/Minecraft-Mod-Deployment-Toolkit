# tests/deploy_pack/test_preflight_extended.py

"""Extended tests for deploy_pack.preflight.

Complements test_preflight.py with the branches that file does not
reach: the www_dir candidates warning (§3.19), the pack_required
warning suppression when --client is in scope (§4.6.9), resource-pack
source validation failures (§7.5, §7.7, §7.8), the restarting-container
settle path (§4.12), per-instance compose errors (§3.2, §3.6, §3.8),
the mods_dir/compose mismatch warning (§3.7), the unhealthy and starting
container logs (§4.12), the mods-drift warning on targeted deploys
(§2.9), and the ScopeSet bitmask (§5.3).

Every test drives the public ``run_preflight`` entry point against a
real DeploymentConfig and a fake Docker SDK client.
"""

from __future__ import annotations

import logging
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
from minecraft.deploy_pack.preflight import PreflightError, ScopeSet, run_preflight

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
            binds=[
                BindMount(host_source=root, container_target="/data"),
                BindMount(host_source=mods, container_target="/data/mods"),
            ],
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
    www_dir_error: str | None = None,
    www_dir_candidates: list[Path] | None = None,
    mods_dir_toml: Path | None = None,
    sync_mapping: dict | None = None,
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
    if www_dir is None and www_dir_error is None:
        www_dir = tmp_path / "www"
        www_dir.mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=www_dir,
        www_dir_error=www_dir_error,
        www_dir_candidates=www_dir_candidates or [],
        output_filename="minecraft_client_{date}.zip",
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping=sync_mapping
        if sync_mapping is not None
        else {
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
        mods_dir_toml=mods_dir_toml,
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
    """Write .pw.toml files and placeholder jars for filename -> side."""
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
    """Return a single survival instance rooted under tmp_path/data/survival."""
    inst = _instance("survival", "mc-survival", tmp_path / "data" / "survival")
    return ({"survival": inst}, ["survival"])


# ---------------------------------------------------------------------------
# §5.3: ScopeSet bitmask and names
# ---------------------------------------------------------------------------


def test_scope_set_names_are_empty_when_no_scope() -> None:
    """§5.4: no scope is an empty names list."""
    assert ScopeSet().names() == []


def test_scope_set_names_are_in_fixed_order() -> None:
    """§5.4: names come out server, client, resource-pack."""
    assert ScopeSet(server=True, client=True, resource_pack=True).names() == ["server", "client", "resource-pack"]
    assert ScopeSet(client=True, resource_pack=True).names() == ["client", "resource-pack"]


def test_scope_set_bitmask_follows_the_spec() -> None:
    """§5.3: server=1, client=2, resource-pack=4."""
    assert ScopeSet().bitmask() == 0
    assert ScopeSet(server=True).bitmask() == 1
    assert ScopeSet(client=True).bitmask() == 2
    assert ScopeSet(resource_pack=True).bitmask() == 4
    assert ScopeSet(server=True, client=True, resource_pack=True).bitmask() == 7
    assert ScopeSet(client=True, resource_pack=True).bitmask() == 6


# ---------------------------------------------------------------------------
# §3.19: www_dir candidates are logged at WARN before the fatal raise
# ---------------------------------------------------------------------------


def test_www_dir_candidates_are_warned_before_the_fatal_raise(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§3.19: every candidate is logged at WARN, then the preflight failure is raised."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(
        tmp_path,
        partition=partition,
        instances=instances,
        www_dir=None,
        www_dir_error="multiple candidates",
        www_dir_candidates=[tmp_path / "cand1", tmp_path / "cand2"],
    )
    with caplog.at_level(logging.WARNING), pytest.raises(PreflightError):
        run_preflight(cfg, ScopeSet(client=True), False, False, False, _runtime(cfg))
    assert sum("www_dir candidate" in r.message for r in caplog.records) == 2


# ---------------------------------------------------------------------------
# §4.6.9: pack_required warning suppression
# ---------------------------------------------------------------------------


def test_pack_required_warning_is_suppressed_when_client_scope_is_active(tmp_path: Path) -> None:
    """§4.6.9: pack_required without --client in scope emits a warning; with --client, it does not."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True, client=True), False, False, False, _runtime(cfg))
    assert plan.pack_required is True
    assert plan.pack_required_warning is None


def test_pack_required_warning_is_emitted_when_client_scope_is_absent(tmp_path: Path) -> None:
    """§4.6.9: pack_required without --client in scope emits the staleness warning."""
    instances, partition = _basic_instance(tmp_path)
    _write_sync_files(tmp_path, "kubejs", {"startup_scripts/block.js": "new"})
    cfg = _config(tmp_path, partition=partition, instances=instances)
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.pack_required is True
    assert plan.pack_required_warning is not None


# ---------------------------------------------------------------------------
# §7.5 / §7.7 / §7.8: resource-pack validation failures
# ---------------------------------------------------------------------------


def test_invalid_rp_filename_is_a_preflight_failure(tmp_path: Path) -> None:
    """§7.5: an invalid resource-pack filename is exit 3."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "resourcepacks").mkdir(parents=True)
    (tmp_path / "sync" / "resourcepacks" / "pack.zip").write_bytes(b"x")
    cfg = _config(
        tmp_path,
        partition=partition,
        instances=instances,
        resource_packs={"survival": ResourcePackConfig(filename="pack.tar.gz", required=False, prompt="")},
    )
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§7.5") for f in ei.value.failures)


def test_missing_rp_source_is_a_preflight_failure(tmp_path: Path) -> None:
    """§7.8: a missing resource-pack source is exit 3."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "resourcepacks").mkdir(parents=True)
    cfg = _config(
        tmp_path,
        partition=partition,
        instances=instances,
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=False, prompt="")},
    )
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§7.8") for f in ei.value.failures)


def test_missing_resourcepacks_client_mapping_is_a_preflight_failure(tmp_path: Path) -> None:
    """§7.7: a configured pack without sync_mapping.resourcepacks.client is exit 3."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(
        tmp_path,
        partition=partition,
        instances=instances,
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=False, prompt="")},
        sync_mapping={"resourcepacks": {"resource_pack": "@www/resourcepacks"}},
    )
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§7.7") for f in ei.value.failures)


# ---------------------------------------------------------------------------
# §4.12: restarting container that settles to running passes
# ---------------------------------------------------------------------------


def test_restarting_container_that_settles_to_running_passes_preflight(tmp_path: Path) -> None:
    """§4.12: a container that stops restarting within the bounded wait is eligible."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    restarting = ContainerState(name="mc-survival", exists=True, status="restarting", running=False, health=None, raw={"Mounts": []})
    settled = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="healthy", raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": restarting}, restarting_settled={"mc-survival": settled})
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert plan.none_set == ["survival"]


# ---------------------------------------------------------------------------
# §3.2 / §3.6 / §3.8: per-instance compose errors
# ---------------------------------------------------------------------------


def test_service_match_error_is_reported(tmp_path: Path) -> None:
    """§3.6: a container_name with no matching service is exit 3."""
    inst = _instance("survival", "mc-survival", tmp_path / "survival")
    inst.service_match_error = "no service matches"
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    cfg.instances["survival"].service = None
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§3.6") for f in ei.value.failures)


def test_stop_grace_parse_error_is_reported(tmp_path: Path) -> None:
    """§3.2: an unparseable stop_grace_period is exit 3."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    cfg.instances["survival"].stop_grace_parse_error = "garbage"
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§3.2") for f in ei.value.failures)


def test_no_healthcheck_is_reported(tmp_path: Path) -> None:
    """§3.8: a compose service with no healthcheck is exit 3."""
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    assert cfg.instances["survival"].service is not None
    cfg.instances["survival"].service.has_healthcheck = False
    with pytest.raises(PreflightError) as ei:
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any(f.source.startswith("§3.8") for f in ei.value.failures)


# ---------------------------------------------------------------------------
# §3.7: mods_dir mismatch between TOML and compose logs a warning
# ---------------------------------------------------------------------------


def test_mods_dir_toml_mismatch_logs_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§3.4: compose is authoritative for mods_dir; a TOML disagreement is a WARN."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances, mods_dir_toml=tmp_path / "wrong_mods")
    with caplog.at_level(logging.WARNING):
        run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert any("mods_dir" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# §4.12: unhealthy and starting containers log at the correct levels
# ---------------------------------------------------------------------------


def test_unhealthy_container_logs_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§4.12: an unhealthy running container produces a WARN at preflight."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    unhealthy = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="unhealthy", raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": unhealthy})
    with caplog.at_level(logging.WARNING):
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any("unhealthy" in r.message for r in caplog.records)


def test_starting_container_logs_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§4.12: a starting container produces an INFO log."""
    instances, partition = _basic_instance(tmp_path)
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    cfg = _config(tmp_path, partition=partition, instances=instances)
    starting = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="starting", raw={"Mounts": []})
    runtime = _runtime(cfg, states={"mc-survival": starting})
    with caplog.at_level(logging.INFO):
        run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime)
    assert any("starting" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# §2.9: mods drift on targeted deploys
# ---------------------------------------------------------------------------


def test_targeted_deploy_warns_when_mods_dir_drifts(tmp_path: Path) -> None:
    """§2.9: a targeted server deploy warns when mods_dir diverges from the full source set."""
    _write_index(tmp_path, {"a.jar": "server"})
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    shared_mods = tmp_path / "shared_mods"
    shared_mods.mkdir()
    instances, partition = _basic_instance(tmp_path)
    cfg = _config(
        tmp_path,
        partition=partition,
        instances=instances,
        requested_instances={"survival"},
    )
    plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg))
    assert plan.targeted is True
    assert plan.mods_drift is True
    assert any("mods_dir" in w for w in plan.warnings)
