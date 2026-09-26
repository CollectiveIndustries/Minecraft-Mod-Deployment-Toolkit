# tests/deploy_pack/test_docker_runtime.py

"""Tests for deploy_pack.docker_runtime, Project_Specs.md §2.4, §4.12, §8.3, §8.4, §8.10.

Coverage:

  * §4.12 - container state policy: running / exited / created / stopped /
            paused / restarting / removing / dead / missing, plus the
            bounded wait on restarting containers
  * §8.3  - health polling until healthy or timeout; missing
            .State.Health on a running container; pre-existing
            unhealthy note
  * §8.4  - RCON transport selection (Option A via exec, Option B via
            TCP), RCON_PORT resolution (environment, env_file, default),
            secret loading, multi-mapping ambiguity, remote rcon_host
            rules
  * §8.10 - stop semantics: real stop, no-op stop, stop failure
  * §2.4  - daemon unavailable at any operation -> DockerUnavailableError

The Docker SDK is a hard external boundary. A fake client and fake
container are used in place of a real daemon; everything else uses the
real code paths.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import docker
import pytest

from minecraft.deploy_pack.config_model import ComposeFile, ComposeService
from minecraft.deploy_pack.docker_runtime import (
    Clock,
    DockerRuntime,
    ExecRconTransport,
    StopOutcome,
    TcpRconTransport,
    check_mount_drift,
    load_rcon_password,
    resolve_rcon_port,
    select_rcon_transport,
)
from minecraft.deploy_pack.errors import ConfigError, DockerUnavailableError


class FakeClock(Clock):
    """Clock with a controllable monotonic time and recorded sleeps."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        """Return the current fake monotonic time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Record the sleep duration and advance the fake clock."""
        self.sleeps.append(seconds)
        self._now += seconds


class FakeExecResult:
    """Stand-in for the docker SDK's exec_run result."""

    def __init__(self, exit_code: int, output: bytes = b"") -> None:
        self.exit_code = exit_code
        self.output = output


class FakeContainer:
    """Minimal container stand-in with per-test hooks."""

    def __init__(self, name: str, status: str = "running", health: str | None = "healthy", mounts: list[dict] | None = None, ports: dict | None = None) -> None:
        state: dict[str, Any] = {"Status": status, "Running": status == "running"}
        if health is not None:
            state["Health"] = {"Status": health}
        self._attrs: dict[str, Any] = {"State": state, "Mounts": mounts or [], "NetworkSettings": {"Ports": ports or {}}, "Name": f"/{name}"}
        self.name = name
        self.exec_calls: list[list[str]] = []
        self.exec_results: list[FakeExecResult] = []
        self.exec_raises: BaseException | None = None
        self.start_raises: BaseException | None = None
        self.stop_raises: BaseException | None = None
        self.stop_return: Any = None
        self.stop_calls: list[int | None] = []
        self.start_calls: int = 0
        self.poll_callback: Any = None

    @property
    def attrs(self) -> dict:
        """Return the container's attributes, running any registered poll hook."""
        if self.poll_callback is not None:
            self.poll_callback(self)
        return self._attrs

    def stop(self, timeout: int | None = None) -> Any:
        """Record the timeout; mutate state on success."""
        self.stop_calls.append(timeout)
        if self.stop_raises is not None:
            raise self.stop_raises
        if self.stop_return is not None:
            return self.stop_return
        self._attrs["State"]["Status"] = "exited"
        self._attrs["State"]["Running"] = False
        return None

    def start(self) -> None:
        """Start the container; raise if configured to."""
        self.start_calls += 1
        if self.start_raises is not None:
            raise self.start_raises
        self._attrs["State"]["Status"] = "running"
        self._attrs["State"]["Running"] = True
        if "Health" not in self._attrs["State"]:
            self._attrs["State"]["Health"] = {"Status": "starting"}

    def exec_run(self, args: list[str]) -> FakeExecResult:
        """Record an exec call and return the next queued result."""
        self.exec_calls.append(list(args))
        if self.exec_raises is not None:
            raise self.exec_raises
        if self.exec_results:
            return self.exec_results.pop(0)
        return FakeExecResult(0, b"")

    def set_state(self, status: str | None = None, health: str | None = "healthy", running: bool | None = None) -> None:
        """Convenience for tests that mutate the container's state mid-run."""
        if status is not None:
            self._attrs["State"]["Status"] = status
        if running is None:
            running = self._attrs["State"]["Status"] == "running"
        self._attrs["State"]["Running"] = running
        if health is None:
            self._attrs["State"].pop("Health", None)
        else:
            self._attrs["State"]["Health"] = {"Status": health}


class FakeContainersCollection:
    """A dict-backed stand-in for ``client.containers``."""

    def __init__(self, containers: dict[str, FakeContainer]) -> None:
        self._containers = containers

    def get(self, name: str) -> FakeContainer:
        """Return the named container or raise NotFound."""
        if name not in self._containers:
            raise docker.errors.NotFound(name)
        return self._containers[name]


class FakeDockerClient:
    """Stand-in for the docker SDK client."""

    def __init__(self, containers: dict[str, FakeContainer]) -> None:
        self.containers = FakeContainersCollection(containers)
        self.ping_raises: BaseException | None = None
        self.ping_calls = 0

    def ping(self) -> bool:
        """Record a ping call; raise if configured to."""
        self.ping_calls += 1
        if self.ping_raises is not None:
            raise self.ping_raises
        return True


def _runtime(containers: dict[str, FakeContainer] | None = None, clock: Clock | None = None) -> tuple[DockerRuntime, FakeDockerClient]:
    """Build a DockerRuntime wired to a fake SDK client."""
    client = FakeDockerClient(containers or {})
    return (DockerRuntime(client=client, clock=clock or FakeClock()), client)


@pytest.mark.parametrize(
    "status,health,running",
    [
        ("running", "healthy", True),
        ("running", "starting", True),
        ("running", "unhealthy", True),
        ("running", None, True),
        ("exited", None, False),
        ("created", None, False),
        ("paused", None, False),
        ("restarting", None, False),
        ("removing", None, False),
        ("dead", None, False),
    ],
)
def test_inspect_reports_state_faithfully(status: str, health: str | None, running: bool) -> None:
    """§4.12: inspect reports the status, running flag, and health verbatim."""
    c = FakeContainer("mc", status=status, health=health)
    runtime, _ = _runtime({"mc": c})
    state = runtime.inspect("mc")
    assert state.exists
    assert state.status == status
    assert state.running == running
    assert state.health == health


def test_inspect_missing_container_returns_missing_state() -> None:
    """§8.9: a container that does not exist is reported as missing, not raised."""
    runtime, _ = _runtime({})
    state = runtime.inspect("nope")
    assert not state.exists
    assert state.status == "missing"
    assert state.running is False


def test_inspect_daemon_unavailable_raises_docker_unavailable() -> None:
    """§2.4: a daemon connection error is DockerUnavailableError."""
    runtime, client = _runtime({"mc": FakeContainer("mc")})

    def boom(name: str) -> None:
        raise docker.errors.DockerException("Cannot connect to the Docker daemon")

    client.containers.get = boom
    with pytest.raises(DockerUnavailableError):
        runtime.inspect("mc")


def test_ping_delegates_to_client() -> None:
    """§2.4: ping is a thin pass-through to the SDK client's ping."""
    runtime, client = _runtime({})
    runtime.ping()
    assert client.ping_calls == 1


def test_ping_daemon_unavailable_raises() -> None:
    """§2.4: a ping failure is DockerUnavailableError."""
    runtime, client = _runtime({})
    client.ping_raises = docker.errors.DockerException("Cannot connect to the Docker daemon")
    with pytest.raises(DockerUnavailableError):
        runtime.ping()


def test_wait_for_restarting_settle_returns_when_settled() -> None:
    """§4.12: the wait exits as soon as no container is still restarting."""
    c = FakeContainer("mc", status="restarting", health=None)
    count = [0]

    def cb(_c: FakeContainer) -> None:
        count[0] += 1
        if count[0] >= 3:
            _c._attrs["State"]["Status"] = "running"
            _c._attrs["State"]["Running"] = True

    c.poll_callback = cb
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    result = runtime.wait_for_restarting_settle(["mc"], 30, 2)
    assert result["mc"].status == "running"
    assert all(s <= 2 for s in clock.sleeps)


def test_wait_for_restarting_times_out() -> None:
    """§4.12: the wait ends when the total timeout elapses, even if still restarting."""
    c = FakeContainer("mc", status="restarting", health=None)
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    result = runtime.wait_for_restarting_settle(["mc"], 5, 1)
    assert result["mc"].status == "restarting"
    assert len(clock.sleeps) == 5


def test_wait_for_restarting_zero_disables_the_wait() -> None:
    """§4.12: total_timeout=0 captures one snapshot and returns without sleeping."""
    c = FakeContainer("mc", status="restarting", health=None)
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    result = runtime.wait_for_restarting_settle(["mc"], 0, 1)
    assert result["mc"].status == "restarting"
    assert clock.sleeps == []


def test_wait_for_restarting_polls_containers_in_parallel() -> None:
    """§4.12: the bounded wait inspects every name once per iteration."""
    a = FakeContainer("a", status="restarting", health=None)
    b = FakeContainer("b", status="restarting", health=None)
    for c in (a, b):
        count = [0]

        def make_cb(_c: FakeContainer, counter: list[int] = count) -> Any:
            """Creates and returns a callback function."""

            def cb(_c2: FakeContainer) -> None:
                counter[0] += 1
                if counter[0] >= 2:
                    _c2._attrs["State"]["Status"] = "running"
                    _c2._attrs["State"]["Running"] = True

            return cb

        c.poll_callback = make_cb(c)
    clock = FakeClock()
    runtime, _ = _runtime({"a": a, "b": b}, clock=clock)
    result = runtime.wait_for_restarting_settle(["a", "b"], 30, 2)
    assert result["a"].status == "running"
    assert result["b"].status == "running"
    assert len(clock.sleeps) == 1


def test_wait_healthy_returns_immediately_when_already_healthy() -> None:
    """§8.3: a healthy container returns on the first poll."""
    c = FakeContainer("mc", status="running", health="healthy")
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert r.healthy
    assert r.final_health == "healthy"


def test_wait_healthy_transitions_to_healthy() -> None:
    """§8.3: the poll loop returns as soon as the status flips to healthy."""
    c = FakeContainer("mc", status="running", health="starting")
    count = [0]

    def cb(_c: FakeContainer) -> None:
        count[0] += 1
        if count[0] >= 3:
            _c._attrs["State"]["Health"] = {"Status": "healthy"}

    c.poll_callback = cb
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    assert runtime.wait_healthy("mc", 30, 2).healthy


def test_wait_healthy_times_out(tmp_path: Path) -> None:
    """§8.3: a health poll that never succeeds returns a timeout error."""
    c = FakeContainer("mc", status="running", health="unhealthy")
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 5, 1)
    assert not r.healthy
    assert r.error is not None
    assert "timed out" in r.error


def test_wait_healthy_preexisting_unhealthy_is_prefixed() -> None:
    """§8.3: the error message notes that the container was unhealthy before the deployment."""
    c = FakeContainer("mc", status="running", health="unhealthy")
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 0, 1, preexisting_unhealthy=True)
    assert not r.healthy
    assert r.error is not None
    assert "unhealthy before the deployment" in r.error


def test_wait_healthy_running_without_health_block_is_failure() -> None:
    """§8.3: a running container with no .State.Health is a distinct failure."""
    c = FakeContainer("mc", status="running", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert not r.healthy
    assert r.error is not None
    assert ".State.Health" in r.error


def test_wait_healthy_container_not_running_is_failure() -> None:
    """§8.3: a container that exits mid-wait returns a not-running error."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert not r.healthy
    assert r.error is not None
    assert "not running" in r.error


def test_start_ok() -> None:
    """§4.13: a successful start returns success and flips the running flag."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.start("mc")
    assert r.success
    assert c.start_calls == 1
    assert c.attrs["State"]["Running"] is True


def test_start_failure_is_reported_not_raised() -> None:
    """§4.13: start failures are collected, not raised."""
    c = FakeContainer("mc", status="exited", health=None)
    c.start_raises = docker.errors.DockerException("boom")
    runtime, _ = _runtime({"mc": c})
    r = runtime.start("mc")
    assert not r.success
    assert r.error is not None


def test_start_missing_container_is_reported() -> None:
    """§8.9: starting a missing container returns an error result, not a raise."""
    runtime, _ = _runtime({})
    r = runtime.start("mc")
    assert not r.success
    assert r.error is not None
    assert "not found" in r.error


def test_stop_real_stop_returns_stopped() -> None:
    """§8.10: a real stop returns STOPPED and passes the timeout through."""
    c = FakeContainer("mc", status="running")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 30)
    assert r.outcome == StopOutcome.STOPPED
    assert c.stop_calls == [30]


def test_stop_timeout_is_forwarded() -> None:
    """§8.10: the timeout argument is forwarded to the SDK's stop call."""
    c = FakeContainer("mc", status="running")
    runtime, _ = _runtime({"mc": c})
    runtime.stop("mc", 42)
    assert c.stop_calls == [42]


def test_stop_already_exited_is_noop() -> None:
    """§8.10: a container that already exited is not stopped; the SDK is not called."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.EXITED_BEFORE_STOP
    assert c.stop_calls == []


def test_stop_pre_stop_race_not_running_error() -> None:
    """§8.10: a stop that races an external exit is treated as a no-op."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("container is not running")
    runtime, _ = _runtime({"mc": c})
    assert runtime.stop("mc", 10).outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_pre_stop_race_container_not_running_class() -> None:
    """§8.10: the ContainerNotRunning subclass is recognized as a pre-stop race."""

    class ContainerNotRunning(docker.errors.DockerException):
        pass

    c = FakeContainer("mc", status="running")
    c.stop_raises = ContainerNotRunning("x")
    runtime, _ = _runtime({"mc": c})
    assert runtime.stop("mc", 10).outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_returning_304_is_treated_as_noop() -> None:
    """§8.10: the SDK's 304 already-stopped response is a no-op stop."""
    c = FakeContainer("mc", status="running")
    c.stop_return = 304
    runtime, _ = _runtime({"mc": c})
    assert runtime.stop("mc", 10).outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_failure_is_reported() -> None:
    """§8.10: a genuine stop failure is reported as FAILED."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("permission denied")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.FAILED
    assert r.error is not None


def test_stop_daemon_unavailable_raises() -> None:
    """§2.4: a daemon-loss stop raises DockerUnavailableError."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("Cannot connect to the Docker daemon")
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(DockerUnavailableError):
        runtime.stop("mc", 10)


def test_stop_missing_container_is_failed() -> None:
    """§8.9: stopping a missing container returns FAILED with an error."""
    runtime, _ = _runtime({})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.FAILED
    assert r.error is not None


def test_exec_run_returns_decoded_output() -> None:
    """exec_run returns the exit code and UTF-8-decoded output."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b"hello"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["rcon-cli", "list"])
    assert code == 0
    assert out == "hello"
    assert c.exec_calls == [["rcon-cli", "list"]]


def test_exec_run_failure_exit_code_is_returned() -> None:
    """A non-zero exit code is returned verbatim."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(1, b"bad command"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["rcon-cli", "list"])
    assert code == 1
    assert "bad command" in out


def test_exec_run_handles_non_utf8_output() -> None:
    """Output that is not valid UTF-8 is decoded with replacement, not raised."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b"\xff\xfe"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["x"])
    assert code == 0
    assert out


def test_exec_run_missing_container_raises_docker_unavailable() -> None:
    """A missing container is DockerUnavailableError."""
    runtime, _ = _runtime({})
    with pytest.raises(DockerUnavailableError):
        runtime.exec_run("mc", ["x"])


def test_list_mounts_returns_binds_only() -> None:
    """§3.17: only bind mounts are surfaced."""
    c = FakeContainer(
        "mc",
        mounts=[
            {"Type": "bind", "Source": "/host/a", "Destination": "/data"},
            {"Type": "volume", "Source": "/var/lib/docker/vol/x", "Destination": "/vol"},
            {"Type": "bind", "Source": "/host/b", "Destination": "/data/mods"},
        ],
    )
    runtime, _ = _runtime({"mc": c})
    mounts = runtime.list_mounts("mc")
    assert {(m.source, m.destination) for m in mounts} == {("/host/a", "/data"), ("/host/b", "/data/mods")}


def test_published_ports_skips_unpublished_mappings() -> None:
    """§8.4: only ports with at least one published mapping appear."""
    c = FakeContainer("mc", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}], "25565/tcp": None})
    runtime, _ = _runtime({"mc": c})
    assert runtime.published_ports("mc") == {"25575/tcp": [("0.0.0.0", 25575)]}


def test_check_mount_drift_no_drift(tmp_path: Path) -> None:
    """§3.17: matching host sources produce no error."""
    data = tmp_path / "data"
    data.mkdir()
    c = FakeContainer("mc", mounts=[{"Type": "bind", "Source": str(data), "Destination": "/data"}])
    runtime, _ = _runtime({"mc": c})
    check_mount_drift(runtime, "mc", [(data, "/data")])


def test_check_mount_drift_missing_mount_is_error() -> None:
    """§3.17: a compose-declared mount missing from the running container is exit 3."""
    c = FakeContainer("mc", mounts=[])
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(ConfigError) as ei:
        check_mount_drift(runtime, "mc", [(Path("/x"), "/data")])
    assert "no bind mount" in str(ei.value)


def test_check_mount_drift_mismatch_is_error(tmp_path: Path) -> None:
    """§3.17: differing host sources for the same target is exit 3."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    c = FakeContainer("mc", mounts=[{"Type": "bind", "Source": str(b), "Destination": "/data"}])
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(ConfigError) as ei:
        check_mount_drift(runtime, "mc", [(a, "/data")])
    assert "drift" in str(ei.value)


def test_check_mount_drift_realpath_failure_is_error(tmp_path: Path) -> None:
    """§3.17: a broken symlink is exit 3."""
    data = tmp_path / "data"
    data.mkdir()
    broken = tmp_path / "broken"
    try:
        broken.symlink_to(tmp_path / "does-not-exist")
    except OSError:
        pytest.skip("cannot create symlink")
    c = FakeContainer("mc", mounts=[{"Type": "bind", "Source": str(data), "Destination": "/data"}])
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(ConfigError) as ei:
        check_mount_drift(runtime, "mc", [(broken, "/data")])
    assert "realpath" in str(ei.value)


def _service(env: dict[str, str] | None = None, env_files: list[Path] | None = None, secrets: list[str] | None = None, name: str = "mc") -> ComposeService:
    """Return a minimal ComposeService for RCON tests."""
    return ComposeService(
        name=name,
        container_name=f"mc-{name}",
        binds=[],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=secrets or [],
        environment=env or {},
        env_files=env_files or [],
    )


def _empty_compose() -> ComposeFile:
    """Return a minimal empty ComposeFile for RCON tests."""
    return ComposeFile(Path("/c.yml"), Path("/"), {}, {})


def test_resolve_rcon_port_default() -> None:
    """§8.4: no override anywhere yields 25575."""
    assert resolve_rcon_port(_service(), _empty_compose()) == 25575


def test_resolve_rcon_port_from_environment() -> None:
    """§8.4: services.<svc>.environment.RCON_PORT wins."""
    assert resolve_rcon_port(_service(env={"RCON_PORT": "30000"}), _empty_compose()) == 30000


def test_resolve_rcon_port_from_env_file(tmp_path: Path) -> None:
    """§8.4: RCON_PORT is read from env_file entries."""
    f = tmp_path / "svc.env"
    f.write_text("# comment\nRCON_PORT=40000\n", encoding="utf-8")
    assert resolve_rcon_port(_service(env_files=[f]), _empty_compose()) == 40000


def test_resolve_rcon_port_env_file_last_wins(tmp_path: Path) -> None:
    """§8.4: with multiple env_file entries, the last value wins."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=1\n", encoding="utf-8")
    b = tmp_path / "b.env"
    b.write_text("RCON_PORT=2\n", encoding="utf-8")
    assert resolve_rcon_port(_service(env_files=[a, b]), _empty_compose()) == 2


def test_resolve_rcon_port_environment_beats_env_file(tmp_path: Path) -> None:
    """§8.4: environment takes priority over env_file."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=99\n", encoding="utf-8")
    assert resolve_rcon_port(_service(env={"RCON_PORT": "5"}, env_files=[a]), _empty_compose()) == 5


def test_resolve_rcon_port_missing_env_file_is_error_when_needed(tmp_path: Path) -> None:
    """§8.4: a missing env_file needed to resolve RCON_PORT is exit 3."""
    with pytest.raises(ConfigError) as ei:
        resolve_rcon_port(_service(env_files=[tmp_path / "missing.env"]), _empty_compose())
    assert "missing" in str(ei.value)


def test_resolve_rcon_port_missing_env_file_is_ignored_when_not_needed(tmp_path: Path) -> None:
    """§8.4: a missing env_file after RCON_PORT has been found is ignored."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=7\n", encoding="utf-8")
    svc = _service(env_files=[a, tmp_path / "missing.env"])
    assert resolve_rcon_port(svc, _empty_compose()) == 7


def test_resolve_rcon_port_bad_value_is_error() -> None:
    """§8.4: a non-integer RCON_PORT is exit 3."""
    with pytest.raises(ConfigError):
        resolve_rcon_port(_service(env={"RCON_PORT": "notanint"}), _empty_compose())


def _compose_with_secret(tmp_path: Path, content: str = "hunter2\n") -> ComposeFile:
    """Return a ComposeFile whose rcon_password secret points to a real file."""
    f = tmp_path / "rcon.txt"
    f.write_text(content, encoding="utf-8")
    return ComposeFile(path=Path("/c.yml"), base_dir=Path("/"), services={}, secret_files={"rcon_password": f})


def test_load_rcon_password_ok(tmp_path: Path) -> None:
    """§8.4: the password is read from secrets.rcon_password.file."""
    compose = _compose_with_secret(tmp_path, "hunter2\n")
    assert load_rcon_password(compose, _service(secrets=["rcon_password"])) == "hunter2"


def test_load_rcon_password_strips_trailing_whitespace(tmp_path: Path) -> None:
    """§8.4: trailing whitespace is stripped."""
    compose = _compose_with_secret(tmp_path, "hunter2  \n\t\n")
    assert load_rcon_password(compose, _service(secrets=["rcon_password"])) == "hunter2"


def test_load_rcon_password_missing_declaration_is_error(tmp_path: Path) -> None:
    """§8.4: a service that does not declare rcon_password is exit 3."""
    compose = _compose_with_secret(tmp_path)
    with pytest.raises(ConfigError) as ei:
        load_rcon_password(compose, _service(secrets=[]))
    assert "rcon_password" in str(ei.value)


def test_load_rcon_password_missing_secret_file_entry_is_error() -> None:
    """§8.4: a declared secret with no secrets.<name>.file entry is exit 3."""
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    with pytest.raises(ConfigError):
        load_rcon_password(compose, _service(secrets=["rcon_password"]))


def test_load_rcon_password_missing_file_on_disk_is_error(tmp_path: Path) -> None:
    """§8.4: a secrets file that does not exist is exit 3."""
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {"rcon_password": tmp_path / "nope.txt"})
    with pytest.raises(ConfigError):
        load_rcon_password(compose, _service(secrets=["rcon_password"]))


def _compose_and_service(tmp_path: Path, *, rcon_port: int = 25575, secrets_present: bool = True) -> tuple[ComposeFile, ComposeService]:
    """Return a compose + service pair configured for RCON tests."""
    secret_file = tmp_path / "rcon.txt"
    secret_file.write_text("pw\n", encoding="utf-8")
    svc = ComposeService(
        name="mc",
        container_name="mc-survival",
        binds=[],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=["rcon_password"] if secrets_present else [],
        environment={"RCON_PORT": str(rcon_port)},
        env_files=[],
    )
    compose = ComposeFile(path=Path("/c.yml"), base_dir=tmp_path, services={"mc": svc}, secret_files={"rcon_password": secret_file})
    return (compose, svc)


def test_select_transport_option_b_same_host(tmp_path: Path) -> None:
    """§8.4: exactly one published mapping on the same host -> Option B to 127.0.0.1."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    t = select_rcon_transport(runtime, svc, compose, "mc-survival", None)
    assert isinstance(t, TcpRconTransport)
    assert t.host == "127.0.0.1"
    assert t.port == 25575


def test_select_transport_option_a_when_no_published_port(tmp_path: Path) -> None:
    """§8.4: zero published mappings -> Option A via exec."""
    c = FakeContainer("mc-survival", ports={})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    assert isinstance(select_rcon_transport(runtime, svc, compose, "mc-survival", None), ExecRconTransport)


def test_select_transport_multiple_mappings_is_error(tmp_path: Path) -> None:
    """§8.4: multiple published mappings is exit 3."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}, {"HostIp": "::", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError):
        select_rcon_transport(runtime, svc, compose, "mc-survival", None)


def test_select_transport_remote_host_requires_published_port(tmp_path: Path) -> None:
    """§8.4: rcon_host with no published mapping is exit 3."""
    c = FakeContainer("mc-survival", ports={})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError) as ei:
        select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")
    assert "not published" in str(ei.value)


def test_select_transport_remote_host_multiple_mappings_is_error(tmp_path: Path) -> None:
    """§8.4: rcon_host with multiple published mappings is exit 3."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}, {"HostIp": "::", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError):
        select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")


def test_select_transport_remote_host_ok(tmp_path: Path) -> None:
    """§8.4: rcon_host with exactly one published mapping -> Option B to the remote host."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    t = select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")
    assert isinstance(t, TcpRconTransport)
    assert t.host == "10.0.0.5"
    assert t.port == 25575


def test_select_transport_option_b_missing_secret_is_error(tmp_path: Path) -> None:
    """§8.4: Option B requires the rcon_password secret."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path, secrets_present=False)
    with pytest.raises(ConfigError):
        select_rcon_transport(runtime, svc, compose, "mc-survival", None)


def test_exec_transport_list_command() -> None:
    """§8.4 Option A: 'list' becomes rcon-cli list."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b""))
    runtime, _ = _runtime({"mc": c})
    ok, _ = ExecRconTransport(runtime, "mc").execute("list")
    assert ok
    assert c.exec_calls == [["rcon-cli", "list"]]


def test_exec_transport_say_with_spaces() -> None:
    """§8.4 Option A: the message after 'say' is passed as one argument."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b""))
    runtime, _ = _runtime({"mc": c})
    ok, _ = ExecRconTransport(runtime, "mc").execute("say hello world")
    assert ok
    assert c.exec_calls == [["rcon-cli", "say", "hello world"]]


def test_exec_transport_failure_exit_code_is_reported() -> None:
    """§8.4 Option A: a non-zero exit code reports failure."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(127, b"not found"))
    runtime, _ = _runtime({"mc": c})
    ok, out = ExecRconTransport(runtime, "mc").execute("list")
    assert not ok
    assert "not found" in out


def test_exec_transport_missing_container_raises() -> None:
    """§8.4 Option A: a missing container is DockerUnavailableError."""
    runtime, _ = _runtime({})
    with pytest.raises(DockerUnavailableError):
        ExecRconTransport(runtime, "mc").execute("list")


def _encode(request_id: int, type_: int, payload: str) -> bytes:
    """Encode a single RCON packet per the Minecraft RCON framing."""
    payload_b = payload.encode("utf-8")
    body = struct.pack("<ii", request_id, type_) + payload_b + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class FakeSocket:
    """A queue-backed socket stand-in for TCP transport tests."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def sendall(self, data: bytes) -> None:
        """Record the bytes and return."""
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        """Return the next chunk (or the leading n bytes of it)."""
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= n:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]

    def settimeout(self, t: float) -> None:
        """Record the timeout."""
        self.timeout = t

    def close(self) -> None:
        """Mark the socket as closed."""
        self.closed = True


def test_tcp_transport_round_trip() -> None:
    """§8.4 Option B: login then command round-trips through one connection."""
    sock = FakeSocket([_encode(1, 2, ""), _encode(2, 0, "There are 0 of a max of 20 players online")])
    t = TcpRconTransport(host="127.0.0.1", port=25575, password="hunter2", sock_factory=lambda h, p, to: sock)
    ok, out = t.execute("list")
    assert ok
    assert "0 of a max of 20" in out
    assert len(sock.sent) == 2
    assert sock.closed


def test_tcp_transport_auth_failure() -> None:
    """§8.4 Option B: an authentication rejection reports login failure."""
    sock = FakeSocket([_encode(-1, 2, "")])
    t = TcpRconTransport(host="127.0.0.1", port=25575, password="wrong", sock_factory=lambda h, p, to: sock)
    ok, out = t.execute("list")
    assert not ok
    assert "login failed" in out


def test_tcp_transport_connect_failure() -> None:
    """§8.4 Option B: a connect failure reports, does not raise."""

    def factory(h: str, p: int, to: float) -> Any:
        raise OSError("connection refused")

    t = TcpRconTransport(host="127.0.0.1", port=25575, password="x", sock_factory=factory)
    ok, out = t.execute("list")
    assert not ok
    assert "connect failed" in out


def test_tcp_transport_packet_framing() -> None:
    """§8.4 Option B: auth and command packets match the RCON framing."""
    sock = FakeSocket([_encode(1, 2, ""), _encode(2, 0, "ok")])
    t = TcpRconTransport(host="127.0.0.1", port=25575, password="pw", sock_factory=lambda h, p, to: sock)
    t.execute("list")
    auth = sock.sent[0]
    size = struct.unpack("<i", auth[:4])[0]
    assert size == len(auth) - 4
    rid, type_ = struct.unpack("<ii", auth[4:12])
    assert rid == 1
    assert type_ == 3
    assert auth[12:].rstrip(b"\x00") == b"pw"
    cmd = sock.sent[1]
    rid, type_ = struct.unpack("<ii", cmd[4:12])
    assert rid == 2
    assert type_ == 2
    assert cmd[12:].rstrip(b"\x00") == b"list"
