# tests/deploy_pack/test_preflight_drift.py

"""Unit tests for ``preflight.runner._check_drift``.

Section 3.17's compose-vs-container mount drift check. Every branch of
the per-member loop is exercised here:

  * compose not loaded             -> early return, no failures
  * member not in config.instances -> skipped
  * instance has no service        -> skipped
  * no drift detected              -> no failure appended
  * drift detected (config error)  -> failure appended with Section 3.17 prefix
  * missing container-side mount   -> failure appended

The drift *detection* itself is covered in ``test_docker_runtime.py``;
these tests verify that ``_check_drift`` forwards state correctly and
converts ``ConfigError`` into ``PreflightFailure``.
"""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack.config_model import (
    BindMount,
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.docker_runtime import ContainerState, Mount
from minecraft.deploy_pack.preflight.runner import _check_drift
from minecraft.deploy_pack.preflight.types import PreflightFailure

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """DockerRuntime stand-in: returns scripted mounts per container name."""

    def __init__(self, mounts: dict[str, list[Mount]]) -> None:
        """Store scripted mounts keyed by container name."""
        self._mounts = mounts

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the scripted mount list for ``name`` (or empty)."""
        return list(self._mounts.get(name, []))


def _state(name: str) -> ContainerState:
    """Return a plausible running ContainerState for ``name``."""
    return ContainerState(
        name=name,
        exists=True,
        status="running",
        running=True,
        health="healthy",
        raw={},
    )


def _service(name: str, binds: list[BindMount]) -> ComposeService:
    """Build a minimal ComposeService with the given binds."""
    return ComposeService(
        name=name,
        container_name=name,
        binds=binds,
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=[],
        environment={},
        env_files=[],
    )


def _config(
    *,
    compose_file: ComposeFile | None,
    instances: dict[str, InstanceConfig],
) -> DeploymentConfig:
    """Build a minimal DeploymentConfig with only the fields _check_drift reads."""
    return DeploymentConfig(
        project_root=Path("/tmp"),
        config_dir=Path("/tmp/config.d"),
        sync_root=Path("/tmp/sync"),
        modpack_dir=Path("/tmp/sync/downloads"),
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
        docker=DockerConfig(compose_file=Path("/tmp/docker-compose.yml")),
        compose=ComposeLoadResult(
            file=compose_file,
            error=None if compose_file is not None else "not loaded",
        ),
    )


# ---------------------------------------------------------------------------
# Early returns and skips
# ---------------------------------------------------------------------------


def test_compose_not_loaded_is_noop(tmp_path: Path) -> None:
    """A DeploymentConfig without a loaded compose file makes no runtime calls."""
    runtime = _FakeRuntime({})
    failures: list[PreflightFailure] = []
    instances = {
        "survival": InstanceConfig(
            name="survival",
            container="mc-survival",
            service=_service("mc-survival", []),
        ),
    }
    config = _config(compose_file=None, instances=instances)

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert failures == []


def test_member_not_in_instances_is_skipped(tmp_path: Path) -> None:
    """A state for a member not present in config.instances is skipped."""
    compose_file = ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services={},
        secret_files={},
    )
    runtime = _FakeRuntime({})
    failures: list[PreflightFailure] = []
    config = _config(compose_file=compose_file, instances={})

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert failures == []


def test_instance_without_service_is_skipped(tmp_path: Path) -> None:
    """An instance whose service was never matched is skipped."""
    compose_file = ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services={},
        secret_files={},
    )
    runtime = _FakeRuntime({})
    failures: list[PreflightFailure] = []
    instances = {
        "survival": InstanceConfig(
            name="survival",
            container="mc-survival",
            service=None,
        ),
    }
    config = _config(compose_file=compose_file, instances=instances)

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert failures == []


# ---------------------------------------------------------------------------
# Drift outcomes
# ---------------------------------------------------------------------------


def test_no_drift_appends_no_failure(tmp_path: Path) -> None:
    """Container mount matches compose declaration yields no failure."""
    compose_host = tmp_path / "compose_host"
    compose_host.mkdir()

    compose_file = ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services={},
        secret_files={},
    )
    runtime = _FakeRuntime({"mc-survival": [Mount(source=str(compose_host), destination="/data")]})
    failures: list[PreflightFailure] = []
    instances = {
        "survival": InstanceConfig(
            name="survival",
            container="mc-survival",
            service=_service(
                "mc-survival",
                [BindMount(host_source=compose_host, container_target="/data")],
            ),
        ),
    }
    config = _config(compose_file=compose_file, instances=instances)

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert failures == []


def test_drift_appends_failure_with_section_3_17_prefix(tmp_path: Path) -> None:
    """Container mount drift yields a failure with the Section 3.17 source."""
    compose_host = tmp_path / "compose_host"
    compose_host.mkdir()
    container_host = tmp_path / "container_host"
    container_host.mkdir()

    compose_file = ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services={},
        secret_files={},
    )
    runtime = _FakeRuntime({"mc-survival": [Mount(source=str(container_host), destination="/data")]})
    failures: list[PreflightFailure] = []
    instances = {
        "survival": InstanceConfig(
            name="survival",
            container="mc-survival",
            service=_service(
                "mc-survival",
                [BindMount(host_source=compose_host, container_target="/data")],
            ),
        ),
    }
    config = _config(compose_file=compose_file, instances=instances)

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert len(failures) == 1
    assert failures[0].source == "Section 3.17 [survival]"
    assert "mount drift" in failures[0].message


def test_missing_mount_appends_failure(tmp_path: Path) -> None:
    """A container-side mount missing entirely yields a failure."""
    compose_host = tmp_path / "compose_host"
    compose_host.mkdir()

    compose_file = ComposeFile(
        path=tmp_path / "docker-compose.yml",
        base_dir=tmp_path,
        services={},
        secret_files={},
    )
    runtime = _FakeRuntime({"mc-survival": []})
    failures: list[PreflightFailure] = []
    instances = {
        "survival": InstanceConfig(
            name="survival",
            container="mc-survival",
            service=_service(
                "mc-survival",
                [BindMount(host_source=compose_host, container_target="/data")],
            ),
        ),
    }
    config = _config(compose_file=compose_file, instances=instances)

    _check_drift(runtime, config, {"survival": _state("mc-survival")}, failures)

    assert len(failures) == 1
    assert failures[0].source == "Section 3.17 [survival]"
    assert "no bind mount" in failures[0].message
