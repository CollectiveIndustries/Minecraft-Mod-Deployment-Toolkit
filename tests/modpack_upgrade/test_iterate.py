# tests/modpack_upgrade/test_iterate.py

"""Iterate command unit tests (Section 15.1.6).

Turn 1 covered the state machine and both caps. Turn 2 adds the
production driver architecture: :class:`_DockerBootDriver`,
:class:`_DockerClient`, :class:`_LogClassifier`, and the conservative
:class:`_MinimalLogClassifier`. All tests use a fake client and a
deterministic clock; real Docker orchestration and the full log
classifier are deferred to a later slice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands.iterate import (
    _BOOT_ITERATION_CAP,
    _BOOT_WALLCLOCK_CAP_SECONDS,
    _MINIMAL_FATAL_MARKERS,
    BootOutcome,
    _DockerBootDriver,
    _MinimalLogClassifier,
    _NullDockerClient,
    run,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.state import (
    STATUS_FAILED,
    STATUS_MATERIALIZED,
    STATUS_RESOLVED,
    RunState,
    load_state,
    save_state,
)

# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock; advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


class _FakeDriver:
    """Scripted ``_BootDriver``: pops one outcome per attempt."""

    def __init__(self, outcomes: list[BootOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[Path, float]] = []

    def run_attempt(self, candidate_dir: Path, *, deadline: float) -> BootOutcome:
        self.calls.append((candidate_dir, deadline))
        if not self._outcomes:
            raise AssertionError("scripted outcomes exhausted")
        return self._outcomes.pop(0)


class _FakeDockerClient:
    """Scriptable ``_DockerClient`` for driver tests.

    Each method records its call and can be configured to raise or return
    a scripted value. The script persists across calls (no per-attempt
    queue) because driver tests always run exactly one attempt.
    """

    def __init__(self) -> None:
        self.start_calls: list[Path] = []
        self.wait_calls: list[tuple[str, float]] = []
        self.capture_calls: list[str] = []
        self.stop_calls: list[str] = []

        self._container_id = "container-1"
        self._exit_code: int = 0
        self._logs: str = ""
        self._start_error: Exception | None = None
        self._wait_error: Exception | None = None
        self._capture_error: Exception | None = None
        self._stop_error: Exception | None = None

    def script_start_error(self, exc: Exception) -> None:
        self._start_error = exc

    def script_exit(self, code: int) -> None:
        self._exit_code = code

    def script_logs(self, logs: str) -> None:
        self._logs = logs

    def script_wait_error(self, exc: Exception) -> None:
        self._wait_error = exc

    def script_capture_error(self, exc: Exception) -> None:
        self._capture_error = exc

    def script_stop_error(self, exc: Exception) -> None:
        self._stop_error = exc

    def start_candidate(self, candidate_dir: Path) -> str:
        self.start_calls.append(candidate_dir)
        if self._start_error is not None:
            raise self._start_error
        return self._container_id

    def wait_for_exit(self, container_id: str, *, deadline: float) -> int:
        self.wait_calls.append((container_id, deadline))
        if self._wait_error is not None:
            raise self._wait_error
        return self._exit_code

    def capture_logs(self, container_id: str) -> str:
        self.capture_calls.append(container_id)
        if self._capture_error is not None:
            raise self._capture_error
        return self._logs

    def stop_and_remove(self, container_id: str) -> None:
        self.stop_calls.append(container_id)
        if self._stop_error is not None:
            raise self._stop_error


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _minimal_state(**overrides: object) -> RunState:
    """Build a minimal ``RunState`` with sensible defaults."""
    base: dict[str, object] = {
        "run_id": "run-1",
        "status": STATUS_MATERIALIZED,
        "release_mode": "upgrade",
        "paused_at_utc": "2026-01-01T00:00:00Z",
        "report_sha256": _hex("a"),
        "resolution_inputs_sha256": _hex("b"),
        "manifest_path": "/m",
    }
    base.update(overrides)
    return RunState(**base)


def _setup_run(
    tmp_path: Path,
    *,
    state: RunState | None = None,
    write_slip: bool = True,
) -> tuple[Path, Path, Path]:
    """Create state and (optionally) a placeholder candidate slip.

    Returns ``(state_dir, state_path, candidate_dir)``.
    """
    state_dir = tmp_path / "state"
    state_path = state_dir / "runs" / "run-1" / "state.toml"
    state_path.parent.mkdir(parents=True)
    save_state(state_path, state or _minimal_state())
    candidate_dir = state_dir / "candidates" / "run-1"
    candidate_dir.mkdir(parents=True)
    if write_slip:
        (candidate_dir / "packing_slip.toml").write_text("placeholder")
    return state_dir, state_path, candidate_dir


# ---------------------------------------------------------------------------
# Turn 1: precondition failures (unchanged)
# ---------------------------------------------------------------------------


def test_missing_state_raises_3(tmp_path: Path) -> None:
    """A missing state.toml -> exit 3."""
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=_FakeDriver([]),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "state.toml not found" in str(ei.value)


def test_wrong_status_raises_3(tmp_path: Path) -> None:
    """A run that is not ``materialized`` -> exit 3."""
    _setup_run(tmp_path, state=_minimal_state(status=STATUS_RESOLVED))
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=_FakeDriver([]),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "iterate requires" in str(ei.value)


def test_missing_slip_raises_3(tmp_path: Path) -> None:
    """A materialized run with no candidate slip -> exit 3."""
    _setup_run(tmp_path, write_slip=False)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=_FakeDriver([]),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "candidate slip not found" in str(ei.value)


# ---------------------------------------------------------------------------
# Turn 1: unimplemented driver (production default, unchanged in Turn 2)
# ---------------------------------------------------------------------------


def test_unimplemented_driver_raises_1_state_untouched(tmp_path: Path) -> None:
    """The default driver exits 1 without touching state."""
    _, state_path, _ = _setup_run(tmp_path)
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 1
    assert "boot driver not wired" in str(ei.value)
    reloaded = load_state(state_path)
    assert reloaded.status == STATUS_MATERIALIZED
    assert reloaded.reason is None


# ---------------------------------------------------------------------------
# Turn 1: loop control flow (unchanged)
# ---------------------------------------------------------------------------


def test_success_on_first_attempt_exits_0(tmp_path: Path) -> None:
    """Driver returns success -> exit 0, state stays materialized."""
    _, state_path, candidate_dir = _setup_run(tmp_path)
    clock = _FakeClock(start=0.0)
    driver = _FakeDriver([BootOutcome(outcome="success")])

    code = run(
        config_dir=tmp_path,
        project_root=tmp_path,
        run_id="run-1",
        driver=driver,
        clock=clock,
        state_dir=tmp_path / "state",
    )

    assert code == 0
    assert driver.calls == [(candidate_dir, _BOOT_WALLCLOCK_CAP_SECONDS)]
    reloaded = load_state(state_path)
    assert reloaded.status == STATUS_MATERIALIZED
    assert reloaded.reason is None


def test_failure_then_success(tmp_path: Path) -> None:
    """One failure then success -> exit 0; state stays materialized."""
    _, state_path, _ = _setup_run(tmp_path)
    driver = _FakeDriver(
        [
            BootOutcome(outcome="failure", detail="startup crash"),
            BootOutcome(outcome="success"),
        ]
    )
    code = run(
        config_dir=tmp_path,
        project_root=tmp_path,
        run_id="run-1",
        driver=driver,
        clock=_FakeClock(start=0.0),
        state_dir=tmp_path / "state",
    )
    assert code == 0
    assert len(driver.calls) == 2
    assert load_state(state_path).status == STATUS_MATERIALIZED


def test_ambiguous_first_attempt_transitions_to_failed(tmp_path: Path) -> None:
    """Ambiguous outcome -> failed/ambiguous_failure, exit 3."""
    _, state_path, _ = _setup_run(tmp_path)
    driver = _FakeDriver([BootOutcome(outcome="ambiguous", detail="classifier uncertain")])
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "ambiguous_failure" in str(ei.value)
    assert "classifier uncertain" in str(ei.value)
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "ambiguous_failure"


def test_ambiguous_after_failures_transitions_to_failed(tmp_path: Path) -> None:
    """Failure, failure, ambiguous -> failed/ambiguous_failure."""
    _, state_path, _ = _setup_run(tmp_path)
    driver = _FakeDriver(
        [
            BootOutcome(outcome="failure"),
            BootOutcome(outcome="failure"),
            BootOutcome(outcome="ambiguous"),
        ]
    )
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert len(driver.calls) == 3
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "ambiguous_failure"


def test_iteration_cap_exhaustion(tmp_path: Path) -> None:
    """All failures through the iteration cap -> failed/cap_exhausted."""
    _, state_path, _ = _setup_run(tmp_path)
    driver = _FakeDriver([BootOutcome(outcome="failure") for _ in range(_BOOT_ITERATION_CAP)])
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "cap_exhausted" in str(ei.value)
    assert len(driver.calls) == _BOOT_ITERATION_CAP
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "cap_exhausted"


def test_iteration_cap_override(tmp_path: Path) -> None:
    """A custom ``iteration_cap`` is honoured."""
    _setup_run(tmp_path)
    driver = _FakeDriver([BootOutcome(outcome="failure") for _ in range(3)])
    with pytest.raises(UpgradeCliError):
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            iteration_cap=3,
            state_dir=tmp_path / "state",
        )
    assert len(driver.calls) == 3


def test_wallclock_cap_exhaustion(tmp_path: Path) -> None:
    """Clock crosses the deadline before the iteration cap -> cap_exhausted."""
    _, state_path, _ = _setup_run(tmp_path)
    clock = _FakeClock(start=0.0)
    advance_per_attempt = 1000.0
    wallclock_cap = 2500.0

    class _SlowDriver:
        def __init__(self) -> None:
            self.calls = 0

        def run_attempt(self, candidate_dir: Path, *, deadline: float) -> BootOutcome:
            del candidate_dir, deadline
            self.calls += 1
            clock.advance(advance_per_attempt)
            return BootOutcome(outcome="failure")

    driver = _SlowDriver()
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=clock,
            wallclock_cap_seconds=wallclock_cap,
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "cap_exhausted" in str(ei.value)
    # t=0: iter1 runs, t->1000. t=1000: iter2 runs, t->2000.
    # t=2000: iter3 runs, t->3000. t=3000 >= 2500: iter4 exits before driver.
    assert driver.calls == 3
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "cap_exhausted"


def test_wallclock_cap_override(tmp_path: Path) -> None:
    """A custom ``wallclock_cap_seconds`` is honoured."""
    _setup_run(tmp_path)
    clock = _FakeClock(start=0.0)
    advance_per_attempt = 100.0
    wallclock_cap = 150.0

    class _SlowDriver:
        def __init__(self) -> None:
            self.calls = 0

        def run_attempt(self, candidate_dir: Path, *, deadline: float) -> BootOutcome:
            del candidate_dir, deadline
            self.calls += 1
            clock.advance(advance_per_attempt)
            return BootOutcome(outcome="failure")

    driver = _SlowDriver()
    with pytest.raises(UpgradeCliError):
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=clock,
            wallclock_cap_seconds=wallclock_cap,
            state_dir=tmp_path / "state",
        )
    # t=0: iter1 runs, t->100. t=100: iter2 runs, t->200. t=200 >= 150.
    assert driver.calls == 2


def test_deadline_passed_to_driver_matches_wallclock_cap(tmp_path: Path) -> None:
    """The driver receives ``clock_start + wallclock_cap_seconds``."""
    _setup_run(tmp_path)
    start = 1000.0
    clock = _FakeClock(start=start)
    driver = _FakeDriver([BootOutcome(outcome="success")])

    run(
        config_dir=tmp_path,
        project_root=tmp_path,
        run_id="run-1",
        driver=driver,
        clock=clock,
        wallclock_cap_seconds=600.0,
        state_dir=tmp_path / "state",
    )

    _, observed_deadline = driver.calls[0]
    assert observed_deadline == start + 600.0


def test_deadline_stable_across_attempts(tmp_path: Path) -> None:
    """The same absolute deadline reaches every attempt."""
    _setup_run(tmp_path)
    start = 100.0
    clock = _FakeClock(start=start)
    driver = _FakeDriver(
        [
            BootOutcome(outcome="failure"),
            BootOutcome(outcome="failure"),
            BootOutcome(outcome="success"),
        ]
    )

    run(
        config_dir=tmp_path,
        project_root=tmp_path,
        run_id="run-1",
        driver=driver,
        clock=clock,
        wallclock_cap_seconds=900.0,
        state_dir=tmp_path / "state",
    )

    deadlines = [d for _, d in driver.calls]
    assert deadlines == [start + 900.0, start + 900.0, start + 900.0]


# ---------------------------------------------------------------------------
# Turn 2: _MinimalLogClassifier
# ---------------------------------------------------------------------------


def test_classifier_clean_boot_is_success() -> None:
    """Zero exit and no fatal marker -> ``success``."""
    classifier = _MinimalLogClassifier()
    outcome = classifier.classify(exit_code=0, logs="")
    assert outcome.outcome == "success"
    assert outcome.detail is None


def test_classifier_clean_log_with_warnings_is_success() -> None:
    """Zero exit with WARN-level log noise still classifies as ``success``."""
    classifier = _MinimalLogClassifier()
    logs = "[WARN] something deprecated\n[INFO] all good\n"
    outcome = classifier.classify(exit_code=0, logs=logs)
    assert outcome.outcome == "success"


@pytest.mark.parametrize("marker", _MINIMAL_FATAL_MARKERS)
def test_classifier_zero_exit_with_fatal_marker_is_ambiguous(marker: str) -> None:
    """Any fatal marker on a zero exit -> ``ambiguous`` with a log excerpt."""
    classifier = _MinimalLogClassifier()
    logs = f"some prelude\n{marker}\nstack trace follows\n"
    outcome = classifier.classify(exit_code=0, logs=logs)
    assert outcome.outcome == "ambiguous"
    assert outcome.detail is not None
    assert marker in outcome.detail


def test_classifier_nonzero_exit_is_ambiguous() -> None:
    """A non-zero exit without a recognised marker -> ``ambiguous``."""
    classifier = _MinimalLogClassifier()
    outcome = classifier.classify(exit_code=137, logs="")
    assert outcome.outcome == "ambiguous"
    assert outcome.detail is not None
    assert "137" in outcome.detail


def test_classifier_detail_includes_log_excerpt() -> None:
    """The operator-facing detail carries a log excerpt (truncated)."""
    classifier = _MinimalLogClassifier()
    long_log = "x" * 5000
    outcome = classifier.classify(exit_code=1, logs=long_log)
    assert outcome.outcome == "ambiguous"
    assert outcome.detail is not None
    # Excerpt must be bounded (200 chars + "..." tail).
    assert len(outcome.detail) < 500
    assert "..." in outcome.detail


# ---------------------------------------------------------------------------
# Turn 2: _DockerBootDriver orchestration
# ---------------------------------------------------------------------------


def test_driver_happy_path_returns_classified_outcome() -> None:
    """Start -> wait -> capture -> classify; driver returns the classifier's outcome."""
    candidate_dir = Path("/candidate")
    deadline = 12345.0
    client = _FakeDockerClient()
    client.script_exit(0)
    client.script_logs("clean boot\n")
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    outcome = driver.run_attempt(candidate_dir, deadline=deadline)

    assert outcome.outcome == "success"
    assert client.start_calls == [candidate_dir]
    assert client.wait_calls == [("container-1", deadline)]
    assert client.capture_calls == ["container-1"]
    assert client.stop_calls == ["container-1"]


def test_driver_teardown_runs_on_timeout() -> None:
    """A ``TimeoutError`` from ``wait_for_exit`` -> ambiguous, teardown still runs."""
    client = _FakeDockerClient()
    client.script_wait_error(TimeoutError("did not exit"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    outcome = driver.run_attempt(Path("/candidate"), deadline=100.0)

    assert outcome.outcome == "ambiguous"
    assert outcome.detail is not None
    assert "deadline" in outcome.detail.lower()
    # Teardown must still run - container may be partially started.
    assert client.stop_calls == ["container-1"]


def test_driver_wait_error_raises_exit_1_and_teardown_runs() -> None:
    """A non-timeout error from ``wait_for_exit`` -> exit 1, teardown still runs."""
    client = _FakeDockerClient()
    client.script_wait_error(RuntimeError("docker socket gone"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    with pytest.raises(UpgradeCliError) as ei:
        driver.run_attempt(Path("/candidate"), deadline=100.0)
    assert ei.value.exit_code == 1
    assert "wait_for_exit failed" in str(ei.value)
    assert "docker socket gone" in str(ei.value)
    assert client.stop_calls == ["container-1"]


def test_driver_capture_error_raises_exit_1_and_teardown_runs() -> None:
    """A ``capture_logs`` error -> exit 1, teardown still runs."""
    client = _FakeDockerClient()
    client.script_capture_error(RuntimeError("log pipe broken"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    with pytest.raises(UpgradeCliError) as ei:
        driver.run_attempt(Path("/candidate"), deadline=100.0)
    assert ei.value.exit_code == 1
    assert "capture_logs failed" in str(ei.value)
    assert client.stop_calls == ["container-1"]


def test_driver_start_error_propagates() -> None:
    """An error from ``start_candidate`` propagates unchanged.

    The client's contract requires atomic start: either it returns a
    container_id, or it raises after cleaning up any partial state. The
    driver never holds a container_id to tear down.
    """
    client = _FakeDockerClient()
    client.script_start_error(UpgradeCliError(1, "cannot start"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    with pytest.raises(UpgradeCliError) as ei:
        driver.run_attempt(Path("/candidate"), deadline=100.0)
    assert ei.value.exit_code == 1
    assert "cannot start" in str(ei.value)
    # No teardown attempted: no container_id was ever assigned.
    assert client.stop_calls == []


def test_driver_teardown_failure_does_not_mask_success() -> None:
    """A ``stop_and_remove`` failure logs a warning but does not change the outcome."""
    client = _FakeDockerClient()
    client.script_exit(0)
    client.script_logs("clean\n")
    client.script_stop_error(RuntimeError("remove failed"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    outcome = driver.run_attempt(Path("/candidate"), deadline=100.0)

    assert outcome.outcome == "success"


def test_driver_teardown_failure_does_not_mask_ambiguous() -> None:
    """A ``stop_and_remove`` failure does not change an ambiguous outcome."""
    client = _FakeDockerClient()
    client.script_exit(1)
    client.script_stop_error(RuntimeError("remove failed"))
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    outcome = driver.run_attempt(Path("/candidate"), deadline=100.0)

    assert outcome.outcome == "ambiguous"


def test_driver_deadline_passed_to_client_unchanged() -> None:
    """The deadline reaches the client's ``wait_for_exit`` verbatim."""
    client = _FakeDockerClient()
    client.script_exit(0)
    client.script_logs("")
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    driver.run_attempt(Path("/candidate"), deadline=98765.5)

    assert client.wait_calls == [("container-1", 98765.5)]


# ---------------------------------------------------------------------------
# Turn 2: _NullDockerClient
# ---------------------------------------------------------------------------


def test_null_client_start_raises_exit_1() -> None:
    """``_NullDockerClient.start_candidate`` raises ``UpgradeCliError(1)``."""
    client = _NullDockerClient()
    with pytest.raises(UpgradeCliError) as ei:
        client.start_candidate(Path("/candidate"))
    assert ei.value.exit_code == 1
    assert "no _DockerClient wired" in str(ei.value)


@pytest.mark.parametrize(
    "method,args",
    [
        ("wait_for_exit", ("container-1",)),
        ("capture_logs", ("container-1",)),
        ("stop_and_remove", ("container-1",)),
    ],
)
def test_null_client_downstream_methods_are_unreachable(method: str, args: tuple) -> None:
    """Downstream methods raise AssertionError to make the unreachable contract explicit."""
    client = _NullDockerClient()
    if method == "wait_for_exit":
        with pytest.raises(AssertionError):
            client.wait_for_exit(args[0], deadline=0.0)
    elif method == "capture_logs":
        with pytest.raises(AssertionError):
            client.capture_logs(args[0])
    else:
        with pytest.raises(AssertionError):
            client.stop_and_remove(args[0])


# ---------------------------------------------------------------------------
# Turn 2: driver + real classifier integration
# ---------------------------------------------------------------------------


def test_integration_clean_boot_succeeds(tmp_path: Path) -> None:
    """End-to-end: fake client + real classifier -> success on first attempt."""
    _, state_path, candidate_dir = _setup_run(tmp_path)
    client = _FakeDockerClient()
    client.script_exit(0)
    client.script_logs("[INFO] server started\n")
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    code = run(
        config_dir=tmp_path,
        project_root=tmp_path,
        run_id="run-1",
        driver=driver,
        clock=_FakeClock(start=0.0),
        state_dir=tmp_path / "state",
    )

    assert code == 0
    assert client.start_calls == [candidate_dir]
    assert client.stop_calls == ["container-1"]
    reloaded = load_state(state_path)
    assert reloaded.status == STATUS_MATERIALIZED


def test_integration_nonzero_exit_escalates_ambiguous(tmp_path: Path) -> None:
    """End-to-end: non-zero exit -> ambiguous on first attempt, state=failed."""
    _, state_path, _ = _setup_run(tmp_path)
    client = _FakeDockerClient()
    client.script_exit(1)
    client.script_logs("boot failed\n")
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "ambiguous_failure" in str(ei.value)
    # Exactly one attempt: the conservative classifier refuses to loop.
    assert len(client.start_calls) == 1
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "ambiguous_failure"


def test_integration_fatal_marker_on_zero_exit_escalates_ambiguous(tmp_path: Path) -> None:
    """End-to-end: zero exit with a fatal marker -> ambiguous on first attempt."""
    _, state_path, _ = _setup_run(tmp_path)
    client = _FakeDockerClient()
    client.script_exit(0)
    client.script_logs("---- Minecraft Crash Report ----\n[FATAL] ...\n")
    driver = _DockerBootDriver(client, _MinimalLogClassifier())

    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=tmp_path,
            project_root=tmp_path,
            run_id="run-1",
            driver=driver,
            clock=_FakeClock(start=0.0),
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "ambiguous_failure" in str(ei.value)
    assert "Minecraft Crash Report" in str(ei.value)
    final = load_state(state_path)
    assert final.status == STATUS_FAILED
    assert final.reason == "ambiguous_failure"
