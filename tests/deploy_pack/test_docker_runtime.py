# tests/deploy_pack/test_docker_runtime.py

"""Tests for deploy_pack.docker_runtime, per Project_Specs.md v3.0 §10.1.

Coverage areas (§10.1 required classes):
  * running / stopped / starting / healthy / unhealthy
  * paused / restarting / removing / dead / missing
  * restarting bounded wait (global, parallel, poll interval)
  * zero-value disables the wait
  * health timeout
  * start failure
  * exec failure
  * Option A/B selection
  * RCON_PORT resolution (environment mapping / list / env_file / default)
  * multi-port ambiguity
  * remote rcon_host + zero/multiple matches
  * stop timeout plumbing
  * missing .State.Health on running container
  * missing .State.Health post-start
  * missing rcon_password secret
  * short-form and long-form secret declarations
  * absolute vs relative secret path
  * pre-stop race (no-op stop → EXITED_BEFORE_STOP)
  * realpath failure → ConfigError
  * Docker daemon unavailable at preflight → DockerUnavailableError
  * Docker daemon unavailable at runtime → DockerUnavailableError from ops
    (hooks re-raises as DockerRuntimeError)
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import docker
import pytest

from minecraft.deploy_pack.config_model import ComposeFile, ComposeService, load_compose
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
    """A fake clock for testing time-dependent behavior."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        """Returns the current time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Pauses execution for a default duration."""
        self.sleeps.append(seconds)
        self._now += seconds


class FakeExecResult:
    """A fake result object representing the output of an exec call."""

    def __init__(self, exit_code: int, output: bytes = b"") -> None:
        self.exit_code = exit_code
        self.output = output


class FakeContainer:
    """A fake container object for testing container lifecycle operations."""

    def __init__(
        self,
        name: str,
        status: str = "running",
        health: str | None = "healthy",
        mounts: list[dict] | None = None,
        ports: dict | None = None,
        rcon_port: int | None = None,
    ) -> None:
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
        self.poll_callback = None

    @property
    def attrs(self) -> dict:
        """Returns the object's attributes."""
        if self.poll_callback is not None:
            self.poll_callback(self)
        return self._attrs

    def reload(self) -> None:
        """Reloads the object's state."""
        pass

    def stop(self, timeout: int | None = None) -> Any:
        """Stop the container."""
        self.stop_calls.append(timeout)
        if self.stop_raises is not None:
            raise self.stop_raises
        if self.stop_return is not None:
            return self.stop_return
        self._attrs["State"]["Status"] = "exited"
        self._attrs["State"]["Running"] = False
        return None

    def start(self) -> None:
        """Start the container."""
        self.start_calls += 1
        if self.start_raises is not None:
            raise self.start_raises
        self._attrs["State"]["Status"] = "running"
        self._attrs["State"]["Running"] = True
        if "Health" not in self._attrs["State"]:
            self._attrs["State"]["Health"] = {"Status": "starting"}

    def exec_run(self, args: list[str]) -> FakeExecResult:
        """Execute a command inside the container."""
        self.exec_calls.append(list(args))
        if self.exec_raises is not None:
            raise self.exec_raises
        if self.exec_results:
            return self.exec_results.pop(0)
        return FakeExecResult(0, b"")

    def set_state(self, status: str | None = None, health: str | None = "healthy", running: bool | None = None) -> None:
        """Set the container's state attributes."""
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
    """A fake collection of Docker containers keyed by name."""

    def __init__(self, containers: dict[str, FakeContainer]) -> None:
        self._containers = containers

    def get(self, name: str) -> FakeContainer:
        """Retrieve a container by name."""
        if name not in self._containers:
            raise docker.errors.NotFound(name)
        return self._containers[name]


class FakeDockerClient:
    """A fake Docker client for testing.

    Attributes:
        containers (FakeContainersCollection): Collection of fake containers.
        ping_raises (BaseException | None): Exception to raise on ping, if any.
        ping_calls (int): Number of times ping has been called.
    """

    def __init__(self, containers: dict[str, FakeContainer]) -> None:
        self.containers = FakeContainersCollection(containers)
        self.ping_raises: BaseException | None = None
        self.ping_calls = 0

    def ping(self) -> bool:
        """Checks connectivity and returns a successful response."""
        self.ping_calls += 1
        if self.ping_raises is not None:
            raise self.ping_raises
        return True


def _runtime(containers: dict[str, FakeContainer] | None = None, clock: Clock | None = None) -> tuple[DockerRuntime, FakeDockerClient]:
    client = FakeDockerClient(containers or {})
    runtime = DockerRuntime(client=client, clock=clock or FakeClock())
    return (runtime, client)


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
def test_inspect_state(status: str, health: str | None, running: bool) -> None:
    """Test that inspect reports container existence, status, running, and health."""
    c = FakeContainer("mc", status=status, health=health)
    runtime, _ = _runtime({"mc": c})
    state = runtime.inspect("mc")
    assert state.exists
    assert state.status == status
    assert state.running == running
    assert state.health == health


def test_inspect_missing_returns_missing_state() -> None:
    """Tests that inspecting a missing container returns a missing state."""
    runtime, _ = _runtime({})
    state = runtime.inspect("nope")
    assert not state.exists
    assert state.status == "missing"
    assert state.running is False
    assert state.raw is None


def test_inspect_daemon_unavailable() -> None:
    """Tests that inspect raises DockerUnavailableError when the Docker daemon cannot be reached."""
    runtime, client = _runtime({})
    client.containers._containers["mc"] = FakeContainer("mc")

    def boom(name):
        raise docker.errors.DockerException("Cannot connect to the Docker daemon")

    client.containers.get = boom
    with pytest.raises(DockerUnavailableError):
        runtime.inspect("mc")


def test_ping_ok() -> None:
    """Tests that ping delegates to the client exactly once and succeeds."""
    runtime, client = _runtime({})
    runtime.ping()
    assert client.ping_calls == 1


def test_ping_daemon_unavailable() -> None:
    """Tests that DockerUnavailableError is raised when the Docker daemon is unreachable."""
    runtime, client = _runtime({})
    client.ping_raises = docker.errors.DockerException("Cannot connect to the Docker daemon")
    with pytest.raises(DockerUnavailableError):
        runtime.ping()


def test_restarting_wait_settles() -> None:
    """Tests that wait_for_restarting_settle returns once a restarting container becomes running within the timeout."""
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


def test_restarting_wait_times_out() -> None:
    """Tests that wait_for_restarting_settle returns after exhausting the timeout when a container never stabilizes."""
    c = FakeContainer("mc", status="restarting", health=None)
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    result = runtime.wait_for_restarting_settle(["mc"], 5, 1)
    assert result["mc"].status == "restarting"
    assert len(clock.sleeps) == 5


def test_restarting_wait_zero_disables() -> None:
    """Tests that a zero timeout disables waiting and returns immediately without sleeping."""
    c = FakeContainer("mc", status="restarting", health=None)
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    result = runtime.wait_for_restarting_settle(["mc"], 0, 1)
    assert result["mc"].status == "restarting"
    assert clock.sleeps == []


def test_restarting_wait_parallel_across_containers() -> None:
    """Tests that wait_for_restarting_settle polls multiple containers in parallel within a single sleep cycle."""
    a = FakeContainer("a", status="restarting", health=None)
    b = FakeContainer("b", status="restarting", health=None)
    for c in (a, b):
        count = [0]

        def make_cb(_c: FakeContainer, count: list[int] = count):
            """Create a callback that advances the count and flips the container to running."""

            def cb(_c2: FakeContainer, count: list[int] = count) -> None:
                count[0] += 1
                if count[0] >= 2:
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


def test_wait_healthy_immediate_success() -> None:
    """Tests that wait_healthy returns immediately when the container is already healthy."""
    c = FakeContainer("mc", status="running", health="healthy")
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert r.healthy
    assert r.final_health == "healthy"


def test_wait_healthy_transitions() -> None:
    """Tests that wait_healthy reports healthy after the container's health status transitions to healthy."""
    c = FakeContainer("mc", status="running", health="starting")
    count = [0]

    def cb(_c: FakeContainer) -> None:
        count[0] += 1
        if count[0] >= 3:
            _c._attrs["State"]["Health"] = {"Status": "healthy"}

    c.poll_callback = cb
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    r = runtime.wait_healthy("mc", 30, 2)
    assert r.healthy


def test_wait_healthy_times_out() -> None:
    """Tests that waiting for a healthy container times out, reports the final unhealthy state, and returns a timeout error."""
    c = FakeContainer("mc", status="running", health="unhealthy")
    clock = FakeClock()
    runtime, _ = _runtime({"mc": c}, clock=clock)
    r = runtime.wait_healthy("mc", 5, 1)
    assert not r.healthy
    assert r.final_health == "unhealthy"
    assert r.error is not None
    assert "timed out" in r.error


def test_wait_healthy_preexisting_unhealthy_note() -> None:
    """Tests that waiting for a healthy container fails with a pre-existing unhealthy note when it was unhealthy before deployment."""
    c = FakeContainer("mc", status="running", health="unhealthy")
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 0, 1, preexisting_unhealthy=True)
    assert not r.healthy
    assert r.error is not None
    assert "unhealthy before the deployment" in r.error


def test_wait_healthy_missing_health_block() -> None:
    """Tests that waiting for a healthy container fails when the running container has no .State.Health block."""
    c = FakeContainer("mc", status="running", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert not r.healthy
    assert r.error is not None
    assert ".State.Health" in r.error


def test_wait_healthy_container_not_running() -> None:
    """Tests that waiting for a healthy container fails with a 'not running' error when the container is exited."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.wait_healthy("mc", 10, 1)
    assert not r.healthy
    assert r.error is not None
    assert "not running" in r.error


def test_start_ok() -> None:
    """Tests that starting a stopped container succeeds, records a start call, and marks the container as running."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.start("mc")
    assert r.success
    assert c.start_calls == 1
    assert c.attrs["State"]["Running"] is True


def test_start_failure() -> None:
    """Tests that a container start failure returns an unsuccessful result with the underlying error."""
    c = FakeContainer("mc", status="exited", health=None)
    c.start_raises = docker.errors.DockerException("boom")
    runtime, _ = _runtime({"mc": c})
    r = runtime.start("mc")
    assert not r.success
    assert r.error is not None


def test_start_missing() -> None:
    """Tests that starting a missing container returns an unsuccessful result with a 'not found' error."""
    runtime, _ = _runtime({})
    r = runtime.start("mc")
    assert not r.success
    assert r.error is not None
    assert "not found" in r.error


def test_stop_ok() -> None:
    """Tests that stopping a running container succeeds and records the timeout value."""
    c = FakeContainer("mc", status="running")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 30)
    assert r.outcome == StopOutcome.STOPPED
    assert c.stop_calls == [30]


def test_stop_timeout_passed_through() -> None:
    """Tests that the timeout argument is forwarded to the container's stop method."""
    c = FakeContainer("mc", status="running")
    runtime, _ = _runtime({"mc": c})
    runtime.stop("mc", 42)
    assert c.stop_calls == [42]


def test_stop_already_exited_is_noop() -> None:
    """Tests that stopping a container already in exited status does not call stop and reports it as already exited."""
    c = FakeContainer("mc", status="exited", health=None)
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.EXITED_BEFORE_STOP
    assert c.stop_calls == []


def test_stop_pre_stop_race_container_not_running_error() -> None:
    """Container was running at inspect, exited before the API call."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("container is not running")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_pre_stop_race_container_not_running_class() -> None:
    """Tests that stopping a container which raises ContainerNotRunning during the pre-stop race is reported as already exited before stop."""

    class ContainerNotRunning(docker.errors.DockerException):
        pass

    c = FakeContainer("mc", status="running")
    c.stop_raises = ContainerNotRunning("x")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_returns_304() -> None:
    """Tests that a 304 response from stop is treated as the container already being exited."""
    c = FakeContainer("mc", status="running")
    c.stop_return = 304
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.EXITED_BEFORE_STOP


def test_stop_failure() -> None:
    """Tests that stopping a container fails when the Docker API raises an exception."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("permission denied")
    runtime, _ = _runtime({"mc": c})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.FAILED
    assert r.error is not None


def test_stop_daemon_unavailable() -> None:
    """Tests that stopping a container raises DockerUnavailableError when the daemon is unavailable."""
    c = FakeContainer("mc", status="running")
    c.stop_raises = docker.errors.DockerException("Cannot connect to the Docker daemon")
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(DockerUnavailableError):
        runtime.stop("mc", 10)


def test_stop_missing_container_is_failed() -> None:
    """Tests that stopping a missing container results in a FAILED outcome with an error."""
    runtime, _ = _runtime({})
    r = runtime.stop("mc", 10)
    assert r.outcome == StopOutcome.FAILED
    assert r.error is not None


def test_exec_run_ok() -> None:
    """Tests that exec_run returns a zero exit code, the command output, and records the exec call."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b"hello"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["rcon-cli", "list"])
    assert code == 0
    assert out == "hello"
    assert c.exec_calls == [["rcon-cli", "list"]]


def test_exec_run_failure_exit_code() -> None:
    """Tests that exec_run returns the failing exit code and output when a command fails."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(1, b"bad command"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["rcon-cli", "list"])
    assert code == 1
    assert "bad command" in out


def test_exec_run_non_utf8_output() -> None:
    """Tests that exec_run handles non-UTF-8 output bytes without failing."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b"\xff\xfe"))
    runtime, _ = _runtime({"mc": c})
    code, out = runtime.exec_run("mc", ["x"])
    assert code == 0
    assert out


def test_exec_run_missing_container() -> None:
    """Tests that exec_run raises DockerUnavailableError for a missing container."""
    runtime, _ = _runtime({})
    with pytest.raises(DockerUnavailableError):
        runtime.exec_run("mc", ["x"])


def test_list_mounts_binds_only() -> None:
    """Tests that list_mounts returns only bind mounts with their source and destination."""
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


def test_published_ports() -> None:
    """Tests that published_ports returns only ports with host bindings, excluding unpublished ones."""
    c = FakeContainer("mc", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}], "25565/tcp": None})
    runtime, _ = _runtime({"mc": c})
    ports = runtime.published_ports("mc")
    assert ports == {"25575/tcp": [("0.0.0.0", 25575)]}


def test_drift_ok(tmp_path: Path) -> None:
    """Verifies that no mount drift is detected when container mounts match expected mounts."""
    data = tmp_path / "data"
    data.mkdir()
    c = FakeContainer("mc", mounts=[{"Type": "bind", "Source": str(data), "Destination": "/data"}])
    runtime, _ = _runtime({"mc": c})
    check_mount_drift(runtime, "mc", [(data, "/data")])


def test_drift_missing_mount() -> None:
    """Tests that check_mount_drift raises ConfigError when a container has no bind mount."""
    c = FakeContainer("mc", mounts=[])
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(ConfigError) as ei:
        check_mount_drift(runtime, "mc", [(Path("/x"), "/data")])
    assert "no bind mount" in str(ei.value)


def test_drift_mismatch(tmp_path: Path) -> None:
    """Tests that a ConfigError is raised when a container's mount source does not match the expected source."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    c = FakeContainer("mc", mounts=[{"Type": "bind", "Source": str(b), "Destination": "/data"}])
    runtime, _ = _runtime({"mc": c})
    with pytest.raises(ConfigError) as ei:
        check_mount_drift(runtime, "mc", [(a, "/data")])
    assert "drift" in str(ei.value)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_drift_realpath_failure_broken_symlink(tmp_path: Path) -> None:
    """Tests that check_mount_drift raises ConfigError when a mount source is a broken symlink."""
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


def _service(env=None, env_files=None, secrets=None, name="mc") -> ComposeService:
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


def test_resolve_rcon_port_default() -> None:
    """Tests that resolve_rcon_port returns the default port when not otherwise specified."""
    svc = _service()
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 25575


def test_resolve_rcon_port_from_environment() -> None:
    """Tests that resolve_rcon_port reads RCON_PORT from the service environment."""
    svc = _service(env={"RCON_PORT": "30000"})
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 30000


def test_resolve_rcon_port_from_env_file(tmp_path: Path) -> None:
    """Tests that resolve_rcon_port reads RCON_PORT from an environment file."""
    f = tmp_path / "svc.env"
    f.write_text("# comment\nRCON_PORT=40000\n", encoding="utf-8")
    svc = _service(env_files=[f])
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 40000


def test_resolve_rcon_port_env_file_last_wins(tmp_path: Path) -> None:
    """Tests that when multiple env files define RCON_PORT, the last file's value wins."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=1\n", encoding="utf-8")
    b = tmp_path / "b.env"
    b.write_text("RCON_PORT=2\n", encoding="utf-8")
    svc = _service(env_files=[a, b])
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 2


def test_resolve_rcon_port_environment_beats_env_file(tmp_path: Path) -> None:
    """Tests that the environment variable RCON_PORT takes precedence over values in env files."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=99\n", encoding="utf-8")
    svc = _service(env={"RCON_PORT": "5"}, env_files=[a])
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 5


def test_resolve_rcon_port_env_file_missing_when_needed(tmp_path: Path) -> None:
    """Tests that a missing env file required for RCON_PORT resolution raises ConfigError mentioning the missing file."""
    svc = _service(env_files=[tmp_path / "missing.env"])
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    with pytest.raises(ConfigError) as ei:
        resolve_rcon_port(svc, compose)
    assert "missing" in str(ei.value)


def test_resolve_rcon_port_env_file_missing_when_not_needed(tmp_path: Path) -> None:
    """A missing env_file is ignored when RCON_PORT is already known."""
    a = tmp_path / "a.env"
    a.write_text("RCON_PORT=7\n", encoding="utf-8")
    svc = _service(env_files=[a, tmp_path / "missing.env"])
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    assert resolve_rcon_port(svc, compose) == 7


def test_resolve_rcon_port_bad_value() -> None:
    """Tests that a non-integer RCON_PORT value raises ConfigError."""
    svc = _service(env={"RCON_PORT": "notanint"})
    compose = ComposeFile(Path("/c.yml"), Path("/"), {}, {})
    with pytest.raises(ConfigError):
        resolve_rcon_port(svc, compose)


def _compose_with_secret(tmp_path: Path, content: str = "hunter2\n") -> ComposeFile:
    f = tmp_path / "rcon.txt"
    f.write_text(content, encoding="utf-8")
    return ComposeFile(path=Path("/c.yml"), base_dir=Path("/"), services={}, secret_files={"rcon_password": f})


def test_load_password_ok(tmp_path: Path) -> None:
    """Tests that the RCON password is loaded successfully from a Docker secret."""
    compose = _compose_with_secret(tmp_path, "hunter2\n")
    svc = _service(secrets=["rcon_password"])
    assert load_rcon_password(compose, svc) == "hunter2"


def test_load_password_strips_trailing_whitespace(tmp_path: Path) -> None:
    """Tests that the loaded rcon password has trailing whitespace stripped."""
    compose = _compose_with_secret(tmp_path, "hunter2  \n\t\n")
    svc = _service(secrets=["rcon_password"])
    assert load_rcon_password(compose, svc) == "hunter2"


def test_load_password_missing_secret_declaration(tmp_path: Path) -> None:
    """Tests that loading the rcon password raises ConfigError mentioning the secret name when the service declares no secrets."""
    compose = _compose_with_secret(tmp_path)
    svc = _service(secrets=[])
    with pytest.raises(ConfigError) as ei:
        load_rcon_password(compose, svc)
    assert "rcon_password" in str(ei.value)


def test_load_password_missing_secret_file_entry(tmp_path: Path) -> None:
    """Tests that loading the rcon password raises ConfigError when no secret file entry exists for the secret."""
    compose = ComposeFile(path=Path("/c.yml"), base_dir=Path("/"), services={}, secret_files={})
    svc = _service(secrets=["rcon_password"])
    with pytest.raises(ConfigError):
        load_rcon_password(compose, svc)


def test_load_password_missing_secret_file_on_disk(tmp_path: Path) -> None:
    """Tests that loading the rcon password raises ConfigError when the secret's file does not exist on disk."""
    compose = ComposeFile(path=Path("/c.yml"), base_dir=Path("/"), services={}, secret_files={"rcon_password": tmp_path / "nope.txt"})
    svc = _service(secrets=["rcon_password"])
    with pytest.raises(ConfigError):
        load_rcon_password(compose, svc)


def test_compose_long_form_secret_declaration_parsed(tmp_path: Path) -> None:
    """Tests that a long-form secret declaration in a compose file is parsed and the service's secrets list contains the source name."""
    p = tmp_path / "docker-compose.yml"
    p.write_text(
        "\nservices:\n  mc:\n    container_name: mc\n    secrets:\n      - source: rcon_password\n        target: /run/secrets/rcon\nsecrets:\n  rcon_password:\n    file: ./rcon.txt\n",
        encoding="utf-8",
    )
    result = load_compose(p)
    assert result.ok
    svc = result.file.services["mc"]
    assert svc.secrets == ["rcon_password"]


def test_compose_relative_secret_path_resolved(tmp_path: Path) -> None:
    """Tests that a relative secret file path in a compose file is resolved relative to the compose file."""
    p = tmp_path / "docker-compose.yml"
    p.write_text(
        "\nservices:\n  mc:\n    container_name: mc\n    secrets: [rcon_password]\nsecrets:\n  rcon_password:\n    file: ./secrets/rcon.txt\n", encoding="utf-8"
    )
    result = load_compose(p)
    assert result.file.secret_files["rcon_password"] == tmp_path / "secrets" / "rcon.txt"


def test_compose_absolute_secret_path_preserved(tmp_path: Path) -> None:
    """Tests that an absolute secret file path in a compose file is preserved."""
    p = tmp_path / "docker-compose.yml"
    p.write_text(
        "\nservices:\n  mc:\n    container_name: mc\n    secrets: [rcon_password]\nsecrets:\n  rcon_password:\n    file: /abs/rcon.txt\n", encoding="utf-8"
    )
    result = load_compose(p)
    assert result.file.secret_files["rcon_password"] == Path("/abs/rcon.txt")


def _compose_and_service(tmp_path: Path, *, rcon_port: int = 25575, secrets_present: bool = True) -> tuple[ComposeFile, ComposeService]:
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


def test_select_option_b_same_host(tmp_path: Path) -> None:
    """Tests that TcpRconTransport is selected with the expected host and port when a matching published port exists."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    t = select_rcon_transport(runtime, svc, compose, "mc-survival", None)
    assert isinstance(t, TcpRconTransport)
    assert t.host == "127.0.0.1"
    assert t.port == 25575


def test_select_option_a_no_published_port(tmp_path: Path) -> None:
    """Tests that ExecRconTransport is selected when no ports are published."""
    c = FakeContainer("mc-survival", ports={})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    t = select_rcon_transport(runtime, svc, compose, "mc-survival", None)
    assert isinstance(t, ExecRconTransport)


def test_select_multiple_mappings_is_error(tmp_path: Path) -> None:
    """Tests that selecting an RCON transport raises a ConfigError when multiple port mappings exist."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}, {"HostIp": "::", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError) as ei:
        select_rcon_transport(runtime, svc, compose, "mc-survival", None)
    assert "multiple" in str(ei.value).lower() or "2" in str(ei.value)


def test_select_remote_rcon_host_requires_published(tmp_path: Path) -> None:
    """Verifies that selecting a remote RCON transport raises ConfigError when the service port is not published."""
    c = FakeContainer("mc-survival", ports={})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError) as ei:
        select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")
    assert "not published" in str(ei.value)


def test_select_remote_rcon_host_multiple_mappings(tmp_path: Path) -> None:
    """Tests that selecting a remote RCON transport raises ConfigError when the container exposes multiple host mappings for the RCON port."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}, {"HostIp": "::", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    with pytest.raises(ConfigError):
        select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")


def test_select_remote_rcon_host_ok(tmp_path: Path) -> None:
    """Tests that selecting a remote RCON transport succeeds and returns a TcpRconTransport with the expected host and port."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path)
    t = select_rcon_transport(runtime, svc, compose, "mc-survival", "10.0.0.5")
    assert isinstance(t, TcpRconTransport)
    assert t.host == "10.0.0.5"
    assert t.port == 25575


def test_select_option_b_missing_secret(tmp_path: Path) -> None:
    """Tests that selecting the option B transport raises ConfigError when the required secret is missing."""
    c = FakeContainer("mc-survival", ports={"25575/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25575"}]})
    runtime, _ = _runtime({"mc-survival": c})
    compose, svc = _compose_and_service(tmp_path, secrets_present=False)
    with pytest.raises(ConfigError):
        select_rcon_transport(runtime, svc, compose, "mc-survival", None)


def test_exec_transport_list() -> None:
    """Tests that ExecRconTransport.execute runs the list command via rcon-cli and reports success."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b""))
    runtime, _ = _runtime({"mc": c})
    t = ExecRconTransport(runtime, "mc")
    ok, _ = t.execute("list")
    assert ok
    assert c.exec_calls == [["rcon-cli", "list"]]


def test_exec_transport_say_with_spaces() -> None:
    """Tests that ExecRconTransport.execute correctly passes a say command with spaces as separate arguments."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(0, b""))
    runtime, _ = _runtime({"mc": c})
    t = ExecRconTransport(runtime, "mc")
    ok, _ = t.execute("say hello world")
    assert ok
    assert c.exec_calls == [["rcon-cli", "say", "hello world"]]


def test_exec_transport_failure_exit_code() -> None:
    """Tests that ExecRconTransport reports failure when the container command exits with a non-zero code."""
    c = FakeContainer("mc")
    c.exec_results.append(FakeExecResult(127, b"not found"))
    runtime, _ = _runtime({"mc": c})
    t = ExecRconTransport(runtime, "mc")
    ok, out = t.execute("list")
    assert not ok
    assert "not found" in out


def test_exec_transport_container_missing() -> None:
    """Tests that ExecRconTransport raises DockerUnavailableError when the target container is missing."""
    runtime, _ = _runtime({})
    t = ExecRconTransport(runtime, "mc")
    with pytest.raises(DockerUnavailableError):
        t.execute("list")


def _encode(request_id: int, type_: int, payload: str) -> bytes:
    payload_b = payload.encode("utf-8")
    body = struct.pack("<ii", request_id, type_) + payload_b + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class FakeSocket:
    """A fake socket implementation for testing TCP transport behavior."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def sendall(self, data: bytes) -> None:
        """Sends data over the connection."""
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        """Receives data from the connection."""
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= n:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]

    def settimeout(self, t: float) -> None:
        """Sets the socket timeout."""
        self.timeout = t

    def close(self) -> None:
        """Closes the connection."""
        self.closed = True


def test_tcp_transport_round_trip() -> None:
    """Tests a successful TCP RCON authentication and command round trip using a fake socket."""
    sock = FakeSocket([_encode(1, 2, ""), _encode(2, 0, "There are 0 of a max of 20 players online")])
    t = TcpRconTransport(host="127.0.0.1", port=25575, password="hunter2", sock_factory=lambda h, p, to: sock)
    ok, out = t.execute("list")
    assert ok
    assert "0 of a max of 20" in out
    assert len(sock.sent) == 2
    assert sock.closed


def test_tcp_transport_auth_failure() -> None:
    """Tests that TCP RCON transport reports failure when authentication fails."""
    sock = FakeSocket([_encode(-1, 2, "")])
    t = TcpRconTransport(host="127.0.0.1", port=25575, password="wrong", sock_factory=lambda h, p, to: sock)
    ok, out = t.execute("list")
    assert not ok
    assert "login failed" in out


def test_tcp_transport_connect_failure() -> None:
    """Verifies that a TCP transport connection failure returns an unsuccessful result with a connect failed message."""

    def factory(h: str, p: int, to: float) -> Any:
        raise OSError("connection refused")

    t = TcpRconTransport(host="127.0.0.1", port=25575, password="x", sock_factory=factory)
    ok, out = t.execute("list")
    assert not ok
    assert "connect failed" in out


def test_tcp_transport_packet_layout() -> None:
    """Verify the bytes we send match the Minecraft RCON framing."""
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
