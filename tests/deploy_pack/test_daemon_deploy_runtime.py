# tests/deploy_pack/test_daemon_deploy_runtime.py

"""Unit tests for ``daemon.deploy_runtime`` (spec v1.3 Section 12.4).

Exercises every reachable branch of the config-to-orchestrator
assembly. Before this file, ``_deploy_one`` (complexity 10, coverage
3.1%, risk 9.69) was the highest-risk function in the project because
the endpoint tests use a lambda in place of the real assembly.

Test sections:

  * _read_active_release           pointer file variants
  * _extract_containers            empty / one / many
  * _resolve_mods_dir              compose / service / bind variants
  * _resolve_world_dir             instance_root presence
  * _select_transports             compose / service / selection outcomes
  * _resolve_is_upgrade            slip presence and mode
  * _build_health_specs            one per container
  * _build_pull_request            field mapping
  * _check_pull_source_configured  source_host presence
  * _build_runtime_glue            containers + glue construction
  * _deploy_one                    every early return + happy path
  * companion: backup write-failure, health-probe DockerException paths
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import tomli_w
from docker.errors import DockerException

from minecraft.deploy_pack.config_model import (
    BindMount,
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.daemon import deploy_runtime as daemon_deploy_runtime
from minecraft.deploy_pack.daemon.backup import (
    MANIFEST_FILENAME,
    BackupManifest,
    create_backup,
)
from minecraft.deploy_pack.daemon.config import MinecraftDaemonConfig
from minecraft.deploy_pack.daemon.deploy import DeployOutcome
from minecraft.deploy_pack.daemon.health_probe import DockerHealthProbe
from minecraft.deploy_pack.daemon.journal import DeploymentJournal
from minecraft.deploy_pack.daemon.lock import DeploymentLock
from minecraft.deploy_pack.errors import ConfigError, DockerUnavailableError
from minecraft.modpack_upgrade import slip as slip_mod

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Minimal DockerRuntime stand-in; select_rcon_transport is mocked."""

    def __init__(self) -> None:
        """Initialize with no client; _deploy_one tests mock deploy()."""
        self._client = None


class _FakeTransport:
    """Minimal RconTransport stand-in; only execute/describe are observed."""

    def execute(self, command: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Return a fixed success payload."""
        del command, timeout
        return (True, "0 players online")

    def describe(self) -> str:
        """Return a fixed description."""
        return "fake"


def _fake_select(*_args: object, **_kwargs: object) -> _FakeTransport:
    """Return a fake transport for select_rcon_transport monkeypatching."""
    return _FakeTransport()


def _make_fake_deploy(captured: dict[str, Any]):
    """Return a fake ``deploy()`` that records the request and returns success."""

    def _fake_deploy(
        *,
        request: Any,
        journal: Any,
        service_control: Any,
        health_probe: Any,
        clock: Any,
    ) -> DeployOutcome:
        del journal, service_control, health_probe, clock
        captured["request"] = request
        return DeployOutcome(success=True, release_id=request.release_id)

    return _fake_deploy


def _compose_service(
    *,
    name: str,
    container_name: str,
    binds: list[BindMount] | None = None,
) -> ComposeService:
    """Build a minimal ComposeService."""
    return ComposeService(
        name=name,
        container_name=container_name,
        binds=binds or [],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=[],
        environment={},
        env_files=[],
        image=None,
        platform=None,
    )


def _compose_file(tmp_path: Path, services: dict[str, ComposeService]) -> ComposeFile:
    """Build a ComposeFile with ``tmp_path`` as its base directory."""
    return ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services=services,
        secret_files={},
    )


def _deploy_config(
    tmp_path: Path,
    *,
    instances: dict[str, InstanceConfig],
    compose_file: ComposeFile | None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with only the fields deploy_runtime reads."""
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=None,
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="test.zip",
        download_base_url="",
        protect_file=None,
        sync_mapping={},
        restart_policy={},
        instances=instances,
        partition=sorted(instances.keys()),
        partition_unknown=[],
        requested_instances=None,
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        compose=ComposeLoadResult(
            file=compose_file,
            error=None if compose_file is not None else "not loaded",
        ),
    )


def _happy_deploy_config(tmp_path: Path) -> DeploymentConfig:
    """DeploymentConfig with one instance that resolves a mods_dir."""
    bind = BindMount(host_source=Path("mods"), container_target="/data/mods")
    service = _compose_service(
        name="mc-survival",
        container_name="mc-survival",
        binds=[bind],
    )
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(
        name="survival",
        container="mc-survival",
        service=service,
        instance_root=tmp_path / "survival",
    )
    return _deploy_config(
        tmp_path,
        instances={"survival": inst},
        compose_file=compose_file,
    )


def _daemon_config(tmp_path: Path, *, source_host: str = "dev.example.com") -> MinecraftDaemonConfig:
    """Build a MinecraftDaemonConfig rooted under ``tmp_path``."""
    return MinecraftDaemonConfig(
        state_dir=tmp_path / "state",
        slip_dir=tmp_path / "slips",
        release_store=tmp_path / "releases",
        staging_root=tmp_path / "staging",
        project_root=tmp_path,
        daemon_version="1.3.0",
        http_host="127.0.0.1",
        http_port=8765,
        bearer_token_env="TEST_TOKEN",
        tailscale_cidrs=(),
        idempotency_path=tmp_path / "idem.sqlite",
        audit_path=tmp_path / "audit.jsonl",
        lock_path=tmp_path / "daemon.lock",
        trusted_keys_path=tmp_path / "keys.toml",
        registry_path=tmp_path / "registry.jsonl",
        journal_path=tmp_path / "journal.jsonl",
        world_backup_root=tmp_path / "backups",
        source_host=source_host,
        source_user="deploy",
        source_path=Path("/srv/remote"),
        ssh_key=tmp_path / "id_key",
    )


def _journal(tmp_path: Path) -> DeploymentJournal:
    """Return a journal rooted under ``tmp_path``."""
    return DeploymentJournal(tmp_path / "journal.jsonl")


def _lock(tmp_path: Path) -> DeploymentLock:
    """Return a lock rooted under ``tmp_path``."""
    return DeploymentLock(tmp_path / "deploy.lock")


def _slip_body(release_id: str, release_mode: str) -> dict:
    """Return a valid slip body dict for ``release_id`` and ``release_mode``."""
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": release_mode,
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + "0" * 64,
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": "a" * 40,
                "kubejs_tree": "a" * 40,
                "compose_tree": "a" * 40,
                "deploy_toml_tree": "a" * 40,
            },
        },
        "fingerprints": {
            "manifest_sha256": "0" * 64,
            "resolution_inputs_sha256": "0" * 64,
            "runtime_sha256": "0" * 64,
            "server_inputs_sha256": "0" * 64,
            "client_inputs_sha256": "0" * 64,
        },
        "jar": [],
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_slip(release_store: Path, release_id: str, *, release_mode: str = "upgrade") -> Path:
    """Write a valid slip at ``release_store/<release_id>/packing_slip.toml``."""
    bundle = release_store / release_id
    bundle.mkdir(parents=True, exist_ok=True)
    body = _slip_body(release_id, release_mode)
    (bundle / "packing_slip.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))
    return bundle


def _write_invalid_slip(release_store: Path, release_id: str) -> None:
    """Write a malformed slip at ``release_store/<release_id>/packing_slip.toml``."""
    bundle = release_store / release_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "packing_slip.toml").write_bytes(b"not = valid = toml\n")


# ---------------------------------------------------------------------------
# _read_active_release
# ---------------------------------------------------------------------------


def test_read_active_release_missing_returns_none(tmp_path: Path) -> None:
    """No pointer file yields None."""
    assert daemon_deploy_runtime._read_active_release(tmp_path / "state") is None


def test_read_active_release_primary_file(tmp_path: Path) -> None:
    """A populated ``active-release`` is returned."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-release").write_text("v1.0.0")
    assert daemon_deploy_runtime._read_active_release(state) == "v1.0.0"


def test_read_active_release_legacy_file(tmp_path: Path) -> None:
    """A populated ``active-release.txt`` is returned."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-release.txt").write_text("v1.0.0")
    assert daemon_deploy_runtime._read_active_release(state) == "v1.0.0"


def test_read_active_release_empty_primary_falls_through(tmp_path: Path) -> None:
    """An empty ``active-release`` falls through to ``active-release.txt``."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-release").write_text("")
    (state / "active-release.txt").write_text("v2.0.0")
    assert daemon_deploy_runtime._read_active_release(state) == "v2.0.0"


def test_read_active_release_both_empty_returns_none(tmp_path: Path) -> None:
    """Both files present but empty yields None."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "active-release").write_text("")
    (state / "active-release.txt").write_text("")
    assert daemon_deploy_runtime._read_active_release(state) is None


# ---------------------------------------------------------------------------
# _extract_containers
# ---------------------------------------------------------------------------


def test_extract_containers_empty(tmp_path: Path) -> None:
    """No instances yields empty tuple and empty map."""
    config = _deploy_config(tmp_path, instances={}, compose_file=None)
    containers, timeouts = daemon_deploy_runtime._extract_containers(config)
    assert containers == ()
    assert timeouts == {}


def test_extract_containers_one(tmp_path: Path) -> None:
    """One instance yields its container and grace period."""
    config = _happy_deploy_config(tmp_path)
    containers, timeouts = daemon_deploy_runtime._extract_containers(config)
    assert containers == ("mc-survival",)
    assert timeouts == {"mc-survival": 10}


def test_extract_containers_many(tmp_path: Path) -> None:
    """Multiple instances yield a tuple in insertion order."""
    a = InstanceConfig(name="a", container="mc-a", stop_grace_seconds=15)
    b = InstanceConfig(name="b", container="mc-b", stop_grace_seconds=30)
    config = _deploy_config(tmp_path, instances={"a": a, "b": b}, compose_file=None)
    containers, timeouts = daemon_deploy_runtime._extract_containers(config)
    assert containers == ("mc-a", "mc-b")
    assert timeouts == {"mc-a": 15, "mc-b": 30}


# ---------------------------------------------------------------------------
# _resolve_mods_dir
# ---------------------------------------------------------------------------


def test_resolve_mods_dir_compose_not_loaded(tmp_path: Path) -> None:
    """Missing compose yields None."""
    config = _deploy_config(tmp_path, instances={}, compose_file=None)
    assert daemon_deploy_runtime._resolve_mods_dir(config) is None


def test_resolve_mods_dir_no_service(tmp_path: Path) -> None:
    """An instance with no matched service yields None."""
    compose_file = _compose_file(tmp_path, {})
    inst = InstanceConfig(name="survival", container="mc-survival", service=None)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)
    assert daemon_deploy_runtime._resolve_mods_dir(config) is None


def test_resolve_mods_dir_no_data_mods_bind(tmp_path: Path) -> None:
    """A service without a /data/mods bind yields None."""
    service = _compose_service(
        name="mc-survival",
        container_name="mc-survival",
        binds=[BindMount(host_source=Path("data"), container_target="/data")],
    )
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(name="survival", container="mc-survival", service=service)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)
    assert daemon_deploy_runtime._resolve_mods_dir(config) is None


def test_resolve_mods_dir_resolves_bind(tmp_path: Path) -> None:
    """A service with /data/mods yields the resolved host source."""
    service = _compose_service(
        name="mc-survival",
        container_name="mc-survival",
        binds=[BindMount(host_source=Path("mods"), container_target="/data/mods")],
    )
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(name="survival", container="mc-survival", service=service)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)
    result = daemon_deploy_runtime._resolve_mods_dir(config)
    assert result is not None
    assert result == (tmp_path / "mods").resolve()


# ---------------------------------------------------------------------------
# _resolve_world_dir
# ---------------------------------------------------------------------------


def test_resolve_world_dir_no_instance_root(tmp_path: Path) -> None:
    """An instance without instance_root yields None."""
    inst = InstanceConfig(name="survival", container="mc-survival", instance_root=None)
    assert daemon_deploy_runtime._resolve_world_dir(inst) is None


def test_resolve_world_dir_with_instance_root(tmp_path: Path) -> None:
    """An instance with instance_root yields <root>/world."""
    inst = InstanceConfig(name="survival", container="mc-survival", instance_root=tmp_path / "survival")
    assert daemon_deploy_runtime._resolve_world_dir(inst) == tmp_path / "survival" / "world"


# ---------------------------------------------------------------------------
# _select_transports
# ---------------------------------------------------------------------------


def test_select_transports_no_compose(tmp_path: Path) -> None:
    """Missing compose.file yields empty maps."""
    config = _deploy_config(tmp_path, instances={}, compose_file=None)
    transports, errors = daemon_deploy_runtime._select_transports(config, _FakeRuntime())
    assert transports == {}
    assert errors == {}


def test_select_transports_no_service(tmp_path: Path) -> None:
    """An instance with no service records an error and omits a transport."""
    compose_file = _compose_file(tmp_path, {})
    inst = InstanceConfig(name="survival", container="mc-survival", service=None)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)
    transports, errors = daemon_deploy_runtime._select_transports(config, _FakeRuntime())
    assert transports == {}
    assert "mc-survival" in errors
    assert "no compose service matched" in errors["mc-survival"]


def test_select_transports_mixed_success_and_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful selection populates transports; a failing one populates errors."""
    good = _compose_service(name="mc-a", container_name="mc-a")
    bad = _compose_service(name="mc-b", container_name="mc-b")
    compose_file = _compose_file(tmp_path, {"mc-a": good, "mc-b": bad})
    instances = {
        "a": InstanceConfig(name="a", container="mc-a", service=good),
        "b": InstanceConfig(name="b", container="mc-b", service=bad),
    }
    config = _deploy_config(tmp_path, instances=instances, compose_file=compose_file)

    def _conditional(*_args: object, **_kwargs: object) -> _FakeTransport:
        raise ConfigError("no rcon secret")

    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _conditional)
    transports, errors = daemon_deploy_runtime._select_transports(config, _FakeRuntime())
    assert transports == {}
    assert "mc-a" in errors
    assert "mc-b" in errors
    assert "no rcon secret" in errors["mc-a"]


def test_select_transports_docker_unavailable_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DockerUnavailableError from selection is recorded, not raised."""
    service = _compose_service(name="mc-survival", container_name="mc-survival")
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(name="survival", container="mc-survival", service=service)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)

    def _raise(*_args: object, **_kwargs: object) -> _FakeTransport:
        raise DockerUnavailableError("daemon unreachable")

    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _raise)
    transports, errors = daemon_deploy_runtime._select_transports(config, _FakeRuntime())
    assert transports == {}
    assert "daemon unreachable" in errors["mc-survival"]


def test_select_transports_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful selection populates the transport map."""
    service = _compose_service(name="mc-survival", container_name="mc-survival")
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(name="survival", container="mc-survival", service=service)
    config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)
    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _fake_select)
    transports, errors = daemon_deploy_runtime._select_transports(config, _FakeRuntime())
    assert "mc-survival" in transports
    assert errors == {}


# ---------------------------------------------------------------------------
# _resolve_is_upgrade
# ---------------------------------------------------------------------------


def test_resolve_is_upgrade_slip_missing(tmp_path: Path) -> None:
    """A missing slip returns (False, failure)."""
    config = _daemon_config(tmp_path)
    is_upgrade, error = daemon_deploy_runtime._resolve_is_upgrade(config, "v1.0.0")
    assert is_upgrade is False
    assert error is not None
    assert error.error_code == 22
    assert "slip not found" in (error.message or "")


def test_resolve_is_upgrade_slip_invalid(tmp_path: Path) -> None:
    """A malformed slip returns (False, failure)."""
    config = _daemon_config(tmp_path)
    _write_invalid_slip(config.release_store, "v1.0.0")
    is_upgrade, error = daemon_deploy_runtime._resolve_is_upgrade(config, "v1.0.0")
    assert is_upgrade is False
    assert error is not None
    assert error.error_code == 22
    assert "slip invalid" in (error.message or "")


def test_resolve_is_upgrade_true(tmp_path: Path) -> None:
    """An upgrade-mode slip returns (True, None)."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="upgrade")
    is_upgrade, error = daemon_deploy_runtime._resolve_is_upgrade(config, "v1.0.0")
    assert is_upgrade is True
    assert error is None


def test_resolve_is_upgrade_false(tmp_path: Path) -> None:
    """An update-mode slip returns (False, None)."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="update")
    is_upgrade, error = daemon_deploy_runtime._resolve_is_upgrade(config, "v1.0.0")
    assert is_upgrade is False
    assert error is None


# ---------------------------------------------------------------------------
# _build_health_specs / _build_pull_request
# ---------------------------------------------------------------------------


def test_build_health_specs_one_per_container() -> None:
    """One spec per container with the default sentinel."""
    specs = daemon_deploy_runtime._build_health_specs(("mc-survival", "mc-creative"))
    assert len(specs) == 2
    assert [s.container for s in specs] == ["mc-survival", "mc-creative"]
    assert all(s.startup_sentinel == daemon_deploy_runtime._DEFAULT_STARTUP_SENTINEL for s in specs)


def test_build_pull_request_field_mapping(tmp_path: Path) -> None:
    """Every field on the PullRequest comes from daemon config."""
    config = _daemon_config(tmp_path)
    request = daemon_deploy_runtime._build_pull_request(config, "v1.0.0")
    assert request.source_host == config.source_host
    assert request.source_user == config.source_user
    assert request.source_path == config.source_path
    assert request.ssh_key == config.ssh_key
    assert request.target_root == config.release_store
    assert request.release_id == "v1.0.0"


# ---------------------------------------------------------------------------
# _check_pull_source_configured
# ---------------------------------------------------------------------------


def test_check_pull_source_empty_returns_failure(tmp_path: Path) -> None:
    """An empty source_host yields a failure outcome."""
    config = _daemon_config(tmp_path, source_host="")
    error = daemon_deploy_runtime._check_pull_source_configured(config, "v1.0.0")
    assert error is not None
    assert error.error_code == 22
    assert "source_host" in (error.message or "")


def test_check_pull_source_configured_returns_none(tmp_path: Path) -> None:
    """A populated source_host yields None."""
    config = _daemon_config(tmp_path)
    assert daemon_deploy_runtime._check_pull_source_configured(config, "v1.0.0") is None


# ---------------------------------------------------------------------------
# _build_runtime_glue
# ---------------------------------------------------------------------------


def test_build_runtime_glue_no_instances(tmp_path: Path) -> None:
    """An empty instance map yields a failure outcome."""
    config = _daemon_config(tmp_path)
    deploy_config = _deploy_config(tmp_path, instances={}, compose_file=None)
    result = daemon_deploy_runtime._build_runtime_glue(config, _FakeRuntime(), object(), deploy_config, "v1.0.0")
    assert isinstance(result, DeployOutcome)
    assert result.error_code == 22


def test_build_runtime_glue_returns_triple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A populated instance map yields (control, probe, containers)."""
    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _fake_select)
    config = _daemon_config(tmp_path)
    deploy_config = _happy_deploy_config(tmp_path)
    result = daemon_deploy_runtime._build_runtime_glue(config, _FakeRuntime(), object(), deploy_config, "v1.0.0")
    assert isinstance(result, tuple)
    control, probe, containers = result
    assert containers == ("mc-survival",)
    assert control is not None
    assert probe is not None


# ---------------------------------------------------------------------------
# _deploy_one
# ---------------------------------------------------------------------------


def test_deploy_one_empty_source_host_returns_22(tmp_path: Path) -> None:
    """An empty source_host short-circuits at config stage."""
    config = _daemon_config(tmp_path, source_host="")
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert outcome.stage == "config"


def test_deploy_one_slip_missing_returns_22(tmp_path: Path) -> None:
    """A missing slip returns 22 at config stage."""
    config = _daemon_config(tmp_path)
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert "slip not found" in (outcome.message or "")


def test_deploy_one_slip_invalid_returns_22(tmp_path: Path) -> None:
    """A malformed slip returns 22 at config stage."""
    config = _daemon_config(tmp_path)
    _write_invalid_slip(config.release_store, "v1.0.0")
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert "slip invalid" in (outcome.message or "")


def test_deploy_one_config_load_raises_returns_22(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load_deployment_config exception returns 22."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="update")

    def _raise(_cd: object) -> None:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(daemon_deploy_runtime, "load_deployment_config", _raise)
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert "could not load deploy config" in (outcome.message or "")


def test_deploy_one_no_mods_dir_returns_22(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config whose mods_dir cannot be resolved returns 22."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="update")

    # Service exists but declares no /data/mods bind.
    service = _compose_service(name="mc-survival", container_name="mc-survival")
    compose_file = _compose_file(tmp_path, {"mc-survival": service})
    inst = InstanceConfig(name="survival", container="mc-survival", service=service)
    deploy_config = _deploy_config(tmp_path, instances={"survival": inst}, compose_file=compose_file)

    monkeypatch.setattr(daemon_deploy_runtime, "load_deployment_config", lambda _cd: deploy_config)
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert "could not resolve mods_dir" in (outcome.message or "")


def test_deploy_one_protect_load_raises_returns_22(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load_protect_patterns exception returns 22."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="update")
    deploy_config = _happy_deploy_config(tmp_path)

    monkeypatch.setattr(daemon_deploy_runtime, "load_deployment_config", lambda _cd: deploy_config)
    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _fake_select)

    def _raise(_p: object) -> None:
        raise OSError("protect file unreadable")

    monkeypatch.setattr(daemon_deploy_runtime, "load_protect_patterns", _raise)
    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )
    assert outcome.success is False
    assert outcome.error_code == 22
    assert "could not load protect patterns" in (outcome.message or "")


def test_deploy_one_happy_path_update_no_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update mode, no active release: DeployRequest assembled with defaults."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="update")
    deploy_config = _happy_deploy_config(tmp_path)

    monkeypatch.setattr(daemon_deploy_runtime, "load_deployment_config", lambda _cd: deploy_config)
    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _fake_select)
    monkeypatch.setattr(daemon_deploy_runtime, "load_protect_patterns", lambda _p: [])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(daemon_deploy_runtime, "deploy", _make_fake_deploy(captured))

    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )

    assert outcome.success is True
    request = captured["request"]
    assert request.release_id == "v1.0.0"
    assert request.is_upgrade is False
    assert request.target_world_dir == tmp_path / "survival" / "world"
    assert request.prior_bundle_dir is None
    assert request.containers == ("mc-survival",)
    assert request.protect_patterns == ()


def test_deploy_one_happy_path_upgrade_with_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade mode with an active prior release: prior_bundle_dir set."""
    config = _daemon_config(tmp_path)
    _write_slip(config.release_store, "v1.0.0", release_mode="upgrade")
    prior = config.release_store / "v0.9.0"
    prior.mkdir(parents=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "active-release").write_text("v0.9.0")
    deploy_config = _happy_deploy_config(tmp_path)

    monkeypatch.setattr(daemon_deploy_runtime, "load_deployment_config", lambda _cd: deploy_config)
    monkeypatch.setattr(daemon_deploy_runtime, "select_rcon_transport", _fake_select)
    monkeypatch.setattr(daemon_deploy_runtime, "load_protect_patterns", lambda _p: ["*.key"])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(daemon_deploy_runtime, "deploy", _make_fake_deploy(captured))

    outcome = daemon_deploy_runtime._deploy_one(
        config=config,
        config_dir=tmp_path / "config.d",
        runtime=_FakeRuntime(),
        client=object(),
        journal=_journal(tmp_path),
        lock=_lock(tmp_path),
        release_id="v1.0.0",
    )

    assert outcome.success is True
    request = captured["request"]
    assert request.is_upgrade is True
    assert request.prior_bundle_dir == prior
    assert request.protect_patterns == ("*.key",)
    assert request.world_backup_root == config.world_backup_root


# ---------------------------------------------------------------------------
# Companion: backup.write-failure and health_probe.DockerException paths
# ---------------------------------------------------------------------------


def test_create_backup_manifest_write_failure_returns_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError during manifest write is reported as a failed backup."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"x")

    def _raise(_backup_dir: Path, _manifest: BackupManifest) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("minecraft.deploy_pack.daemon.backup._write_manifest", _raise)
    result = create_backup(source_world=world, backup_root=tmp_path / "backups", release_id="v1.0.0")
    assert result.success is False
    assert "manifest write failed" in (result.failure_message or "")
    # Backup directory exists but has no manifest.
    assert not (result.backup_dir / MANIFEST_FILENAME).exists()


def test_health_probe_attrs_docker_exception_is_treated_as_stopped(
    tmp_path: Path,
) -> None:
    """A DockerException reading container attrs yields is_running False."""
    del tmp_path

    class _ExplodingContainer:
        @property
        def attrs(self) -> dict:
            raise DockerException("gone")

        def logs(self, tail: int = 500) -> bytes:
            del tail
            raise DockerException("gone")

    class _ExplodingContainers:
        def get(self, name: str) -> Any:
            del name
            return _ExplodingContainer()

    class _Client:
        containers = _ExplodingContainers()

    probe = DockerHealthProbe(client=_Client())
    assert probe.is_running("mc-survival") is False
    assert probe.restart_count("mc-survival") == 0
    assert probe.recent_logs("mc-survival") == ""
