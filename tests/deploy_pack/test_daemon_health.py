# tests/deploy_pack/test_daemon_health.py

"""Unit tests for ``daemon.health`` (spec v1.3 Section 12.9).

Scripts a fake :class:`HealthProbe` and a fake clock to exercise every
check and every short-circuit path deterministically. The real Docker
+ RCON probe is wired in ``daemon/deploy.py`` (Turn 3).
"""

from __future__ import annotations

from pathlib import Path

from minecraft.deploy_pack.daemon.health import (
    HealthCheckSpec,
    HealthProbe,
    run_health_checks,
)
from minecraft.deploy_pack.daemon.journal import DeploymentJournal

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock; advances only on sleep."""

    def __init__(self, start: float = 0.0) -> None:
        """Initialize at ``start``."""
        self._now = start

    def now(self) -> float:
        """Return the current fake time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by ``seconds``."""
        if seconds > 0:
            self._now += seconds


class _FakeProbe:
    """Scriptable HealthProbe: per-container state, logs, and RCON script."""

    def __init__(self) -> None:
        """Initialize empty per-container scripts."""
        self._running: dict[str, bool] = {}
        self._restart_counts: dict[str, int] = {}
        self._logs: dict[str, str] = {}
        self._rcon_results: dict[str, list[tuple[bool, str]]] = {}
        self._rcon_cursor: dict[str, int] = {}

    def set_running(self, container: str, running: bool) -> None:
        """Set the running state for ``container``."""
        self._running[container] = running

    def set_restart_count(self, container: str, count: int) -> None:
        """Set the restart count for ``container``."""
        self._restart_counts[container] = count

    def set_logs(self, container: str, logs: str) -> None:
        """Set the log snapshot for ``container``."""
        self._logs[container] = logs

    def set_rcon_script(self, container: str, results: list[tuple[bool, str]]) -> None:
        """Set the RCON script for ``container``; the last result repeats."""
        self._rcon_results[container] = list(results)
        self._rcon_cursor[container] = 0

    def is_running(self, container: str) -> bool:
        """Return the scripted running state."""
        return self._running.get(container, False)

    def restart_count(self, container: str) -> int:
        """Return the scripted restart count."""
        return self._restart_counts.get(container, 0)

    def recent_logs(self, container: str) -> str:
        """Return the scripted log snapshot."""
        return self._logs.get(container, "")

    def rcon_list(self, container: str) -> tuple[bool, str]:
        """Pop the next scripted RCON outcome; recycle the last on overflow."""
        script = self._rcon_results.get(container, [(True, "3 players online")])
        cursor = self._rcon_cursor.get(container, 0)
        if cursor >= len(script):
            return script[-1]
        self._rcon_cursor[container] = cursor + 1
        return script[cursor]


# Verify the fake satisfies the protocol shape.
_ProbeTypeCheck: type[HealthProbe] = _FakeProbe  # type: ignore[assignment]


def _spec(container: str = "mc-survival", **overrides: object) -> HealthCheckSpec:
    """Return a HealthCheckSpec with sensible test defaults."""
    defaults: dict[str, object] = {
        "container": container,
        "rcon_list_interval_seconds": 15.0,
        "rcon_list_required_successes": 3,
        "timeout_seconds": 600.0,
        "poll_interval_seconds": 5.0,
    }
    defaults.update(overrides)
    return HealthCheckSpec(**defaults)  # type: ignore[arg-type]


def _healthy_probe(container: str = "mc-survival") -> _FakeProbe:
    """Return a probe configured for a happy-path check."""
    probe = _FakeProbe()
    probe.set_running(container, True)
    probe.set_restart_count(container, 0)
    probe.set_logs(container, 'Server started\nDone (12.345s)! For help, type "help"\n')
    probe.set_rcon_script(container, [(True, "3 players online")])
    return probe


def _journal(tmp_path: Path) -> DeploymentJournal:
    """Return a journal rooted under ``tmp_path``."""
    return DeploymentJournal(tmp_path / "journal.jsonl")


def _stages(journal: DeploymentJournal) -> list[str]:
    """Return the stage-value list in file order."""
    return [r.stage.value for r in journal.read_all()]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_all_checks_pass(tmp_path: Path) -> None:
    """A fully healthy container yields a passing report."""
    probe = _healthy_probe()
    clock = _FakeClock()

    report = run_health_checks(
        specs=[_spec()],
        probe=probe,
        clock=clock,
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is True
    assert len(report.results) == 1
    assert report.results[0].container == "mc-survival"
    assert report.results[0].passed is True
    assert report.results[0].failure_reasons == ()


def test_happy_path_journals_started_and_passed(tmp_path: Path) -> None:
    """Journal records HEALTH_CHECK_STARTED then HEALTH_CHECK_PASSED."""
    journal = _journal(tmp_path)
    run_health_checks(
        specs=[_spec()],
        probe=_healthy_probe(),
        clock=_FakeClock(),
        journal=journal,
        run_id="run-1",
        release_id="v1.0.0",
    )
    assert _stages(journal) == ["health_check_started", "health_check_passed"]


def test_delayed_startup_sentinel_succeeds(tmp_path: Path) -> None:
    """A sentinel that appears after several polls still passes."""
    probe = _healthy_probe()
    probe.set_logs("mc-survival", "starting...\n")  # no sentinel yet

    # Script the probe to eventually reveal the sentinel by mutating logs
    # on the third call to recent_logs.
    class _EvolvingProbe(_FakeProbe):
        def __init__(self) -> None:
            super().__init__()
            self._log_calls = 0
            self.set_running("mc-survival", True)
            self.set_rcon_script("mc-survival", [(True, "ok")])

        def recent_logs(self, container: str) -> str:
            self._log_calls += 1
            if self._log_calls >= 3:
                return 'starting...\nDone (12.0s)! For help, type "help"\n'
            return "starting...\n"

    report = run_health_checks(
        specs=[_spec()],
        probe=_EvolvingProbe(),
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )
    assert report.passed is True


def test_multiple_containers_all_pass(tmp_path: Path) -> None:
    """Two healthy containers each yield a passing result."""
    probe = _FakeProbe()
    for name in ("mc-survival", "mc-creative"):
        probe.set_running(name, True)
        probe.set_logs(name, "Done (10s)! For help\n")
        probe.set_rcon_script(name, [(True, "0 players online")])

    report = run_health_checks(
        specs=[_spec("mc-survival"), _spec("mc-creative")],
        probe=probe,
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is True
    assert {r.container for r in report.results} == {"mc-survival", "mc-creative"}


# ---------------------------------------------------------------------------
# Failure short-circuits
# ---------------------------------------------------------------------------


def test_container_not_running_fails(tmp_path: Path) -> None:
    """A stopped container fails at the first check."""
    probe = _FakeProbe()
    probe.set_running("mc-survival", False)

    report = run_health_checks(
        specs=[_spec()],
        probe=probe,
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is False
    assert report.results[0].passed is False
    assert any("not running" in r for r in report.results[0].failure_reasons)


def test_startup_sentinel_timeout_fails(tmp_path: Path) -> None:
    """A sentinel that never appears fails after the timeout."""
    probe = _FakeProbe()
    probe.set_running("mc-survival", True)
    probe.set_logs("mc-survival", "starting...\n")
    probe.set_rcon_script("mc-survival", [(True, "ok")])
    clock = _FakeClock()

    report = run_health_checks(
        specs=[_spec(timeout_seconds=60.0, poll_interval_seconds=10.0)],
        probe=probe,
        clock=clock,
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is False
    assert any("startup sentinel" in r for r in report.results[0].failure_reasons)


def test_restart_count_change_fails(tmp_path: Path) -> None:
    """A restart count that changes during the health window fails."""
    probe = _healthy_probe()
    probe.set_restart_count("mc-survival", 0)

    class _RestartingProbe(_FakeProbe):
        def __init__(self) -> None:
            super().__init__()
            self.set_running("mc-survival", True)
            self.set_logs("mc-survival", "Done (10s)! For help\n")
            self.set_rcon_script("mc-survival", [(True, "ok")])
            self._restart_calls = 0

        def restart_count(self, container: str) -> int:
            self._restart_calls += 1
            # Baseline capture is the first call; subsequent calls
            # observe one restart.
            return 0 if self._restart_calls == 1 else 1

    report = run_health_checks(
        specs=[_spec()],
        probe=_RestartingProbe(),
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )
    assert report.passed is False
    assert any("restarted" in r for r in report.results[0].failure_reasons)


def test_rcon_failure_fails(tmp_path: Path) -> None:
    """A container whose RCON list never succeeds fails."""
    probe = _healthy_probe()
    probe.set_rcon_script("mc-survival", [(False, "authentication rejected")])
    clock = _FakeClock()

    report = run_health_checks(
        specs=[_spec(timeout_seconds=30.0, poll_interval_seconds=10.0)],
        probe=probe,
        clock=clock,
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is False
    assert any("rcon list failed" in r for r in report.results[0].failure_reasons)


def test_fatal_marker_fails(tmp_path: Path) -> None:
    """A fatal marker in the recent logs fails the container."""
    probe = _healthy_probe()
    probe.set_logs(
        "mc-survival",
        "starting...\n---- Minecraft Crash Report ----\njava.lang.RuntimeException: boom\n",
    )

    report = run_health_checks(
        specs=[_spec()],
        probe=probe,
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is False
    assert any("fatal marker" in r for r in report.results[0].failure_reasons)


def test_mixed_pass_and_fail(tmp_path: Path) -> None:
    """One healthy and one broken container: report is overall failed."""
    probe = _FakeProbe()
    probe.set_running("mc-survival", True)
    probe.set_logs("mc-survival", "Done (10s)! For help\n")
    probe.set_rcon_script("mc-survival", [(True, "ok")])
    probe.set_running("mc-creative", False)

    report = run_health_checks(
        specs=[_spec("mc-survival"), _spec("mc-creative")],
        probe=probe,
        clock=_FakeClock(),
        journal=_journal(tmp_path),
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert report.passed is False
    by_container = {r.container: r for r in report.results}
    assert by_container["mc-survival"].passed is True
    assert by_container["mc-creative"].passed is False


def test_failure_journals_health_check_failed(tmp_path: Path) -> None:
    """A failure journals HEALTH_CHECK_STARTED then HEALTH_CHECK_FAILED."""
    journal = _journal(tmp_path)
    probe = _FakeProbe()
    probe.set_running("mc-survival", False)

    run_health_checks(
        specs=[_spec()],
        probe=probe,
        clock=_FakeClock(),
        journal=journal,
        run_id="run-1",
        release_id="v1.0.0",
    )

    assert _stages(journal) == ["health_check_started", "health_check_failed"]
