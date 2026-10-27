# tests/modpack_upgrade/test_iterate_subprocess_client.py

"""Tests for ``iterate._DeployPackSubprocessClient`` (spec Section 15.1.6).

The production ``_BootDriver`` is ``_DockerBootDriver`` with a
``_DeployPackSubprocessClient`` as its client. The driver itself is
covered in ``test_iterate.py`` (which injects ``_FakeDockerClient``);
this file closes the client gap:

  * ``start_candidate`` returns an opaque handle without side effects
  * ``wait_for_exit`` launches ``deploy-pack --integration`` with the
    correct argv, honours the deadline budget, and maps subprocess
    outcomes to the driver's expectations:
      - success -> rc returned as-is (0 -> driver classifies success)
      - non-zero -> rc returned as-is (driver classifies ambiguous)
      - timeout -> TimeoutError (driver converts to ambiguous)
      - FileNotFoundError -> UpgradeCliError(1)
  * ``capture_logs`` concatenates stdout + stderr
  * ``stop_and_remove`` is a no-op

``subprocess.run`` is monkeypatched at the module level, so the fake
controls argv inspection and never spawns a real process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from minecraft.modpack_upgrade.cli.commands import iterate as it
from minecraft.modpack_upgrade.cli.commands.iterate import _DeployPackSubprocessClient
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError


class _FixedClock:
    """Deterministic monotonic clock; advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt


def _client(
    *,
    config_dir: Path | None = None,
    project_root: Path | None = None,
    clock: _FixedClock | None = None,
) -> _DeployPackSubprocessClient:
    """Return a client pointed at ``tmp_path``-style paths."""
    return _DeployPackSubprocessClient(
        config_dir=config_dir or Path("/cfg"),
        project_root=project_root or Path("/proj"),
        clock=clock if clock is not None else _FixedClock(),
    )


def _fake_run_success(record: list[list[str]]):
    """Return a ``subprocess.run`` replacement that succeeds and records argv."""

    def _run(argv: list[str], **_kw) -> SimpleNamespace:
        record.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="stdout line\n", stderr="")

    return _run


def _fake_run_returning(rc: int, stdout: str = "", stderr: str = ""):
    """Return a ``subprocess.run`` replacement that returns ``rc``."""

    def _run(_argv: list[str], **_kw) -> SimpleNamespace:
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    return _run


# ---------------------------------------------------------------------------
# start_candidate / stop_and_remove (trivial contract)
# ---------------------------------------------------------------------------


def test_start_candidate_returns_opaque_handle() -> None:
    """``start_candidate`` returns a marker without touching the filesystem."""
    client = _client()
    handle = client.start_candidate(Path("/candidate"))
    assert isinstance(handle, str)
    assert handle == "deploy-pack-integration"


def test_stop_and_remove_is_noop() -> None:
    """``stop_and_remove`` performs no work and doesn't raise."""
    client = _client()
    client.stop_and_remove("whatever-handle")


# ---------------------------------------------------------------------------
# wait_for_exit -- deadline handling
# ---------------------------------------------------------------------------


def test_wait_for_exit_deadline_passed_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the deadline has already passed, no subprocess runs; TimeoutError raised."""
    clock = _FixedClock(start=1000.0)
    client = _client(clock=clock)

    launched: list[list[str]] = []
    monkeypatch.setattr(it.subprocess, "run", lambda argv, **_kw: launched.append(list(argv)))

    with pytest.raises(TimeoutError) as ei:
        client.wait_for_exit("handle", deadline=500.0)
    assert "deadline already passed" in str(ei.value)
    assert launched == [], "subprocess must not launch if deadline has passed"


def test_wait_for_exit_passes_remaining_time_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout passed to ``subprocess.run`` equals ``deadline - clock()``."""
    clock = _FixedClock(start=100.0)
    client = _client(clock=clock)

    seen_timeout: list[float] = []

    def _run(_argv: list[str], **kw: object) -> SimpleNamespace:
        seen_timeout.append(kw.get("timeout"))  # type: ignore[arg-type]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(it.subprocess, "run", _run)

    client.wait_for_exit("handle", deadline=250.5)
    assert seen_timeout == [150.5]


# ---------------------------------------------------------------------------
# wait_for_exit -- subprocess invocation
# ---------------------------------------------------------------------------


def test_wait_for_exit_builds_expected_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subprocess argv includes the integration deploy flags."""
    client = _client(
        config_dir=Path("/some/cfg"),
        project_root=Path("/some/proj"),
    )
    launched: list[list[str]] = []
    monkeypatch.setattr(it.subprocess, "run", _fake_run_success(launched))

    client.wait_for_exit("handle", deadline=1000.0)

    assert len(launched) == 1
    argv = launched[0]
    assert argv[0] == "deploy-pack"
    assert "--integration" in argv
    assert "--release-pointer" in argv
    assert "latest-slip" in argv
    assert "--server" in argv
    assert "--config-dir" in argv
    assert "/some/cfg" in argv


def test_wait_for_exit_returns_zero_rc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful deploy-pack run returns rc=0."""
    client = _client()
    monkeypatch.setattr(it.subprocess, "run", _fake_run_returning(0))
    assert client.wait_for_exit("handle", deadline=1000.0) == 0


def test_wait_for_exit_returns_nonzero_rc_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero rc from deploy-pack is returned as-is (driver will classify ambiguous)."""
    client = _client()
    monkeypatch.setattr(it.subprocess, "run", _fake_run_returning(23))
    assert client.wait_for_exit("handle", deadline=1000.0) == 23


def test_wait_for_exit_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``subprocess.TimeoutExpired`` is translated to ``TimeoutError``."""
    client = _client()

    def _timeout(_argv: list[str], **_kw) -> None:
        raise subprocess.TimeoutExpired(cmd=["deploy-pack"], timeout=100.0)

    monkeypatch.setattr(it.subprocess, "run", _timeout)

    with pytest.raises(TimeoutError) as ei:
        client.wait_for_exit("handle", deadline=1000.0)
    assert "exceeded" in str(ei.value).lower() or "timeout" in str(ei.value).lower()


def test_wait_for_exit_deploy_pack_missing_raises_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing deploy-pack binary -> ``UpgradeCliError(1)``."""
    client = _client()

    def _not_found(_argv: list[str], **_kw) -> None:
        raise FileNotFoundError("deploy-pack not on PATH")

    monkeypatch.setattr(it.subprocess, "run", _not_found)

    with pytest.raises(UpgradeCliError) as ei:
        client.wait_for_exit("handle", deadline=1000.0)
    assert ei.value.exit_code == 1
    assert "deploy-pack" in str(ei.value)
    assert "not on PATH" in str(ei.value)


def test_wait_for_exit_oserror_raises_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` at launch -> ``UpgradeCliError(1)``."""
    client = _client()

    def _oserror(_argv: list[str], **_kw) -> None:
        raise OSError("simulated launch failure")

    monkeypatch.setattr(it.subprocess, "run", _oserror)

    with pytest.raises(UpgradeCliError) as ei:
        client.wait_for_exit("handle", deadline=1000.0)
    assert ei.value.exit_code == 1
    assert "could not launch" in str(ei.value)


# ---------------------------------------------------------------------------
# capture_logs
# ---------------------------------------------------------------------------


def test_capture_logs_returns_empty_before_any_run() -> None:
    """Before ``wait_for_exit`` has run, ``capture_logs`` returns ''."""
    client = _client()
    assert client.capture_logs("handle") == ""


def test_capture_logs_concatenates_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a run, ``capture_logs`` returns stdout + stderr joined by newline."""
    client = _client()

    def _run(_argv: list[str], **_kw) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="stdout line\n", stderr="stderr line\n")

    monkeypatch.setattr(it.subprocess, "run", _run)
    client.wait_for_exit("handle", deadline=1000.0)

    logs = client.capture_logs("handle")
    assert "stdout line" in logs
    assert "stderr line" in logs


def test_capture_logs_skips_empty_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the populated stream appears (no trailing newlines from empty streams)."""
    client = _client()
    monkeypatch.setattr(it.subprocess, "run", _fake_run_returning(0, stdout="only stdout\n", stderr=""))
    client.wait_for_exit("handle", deadline=1000.0)

    logs = client.capture_logs("handle")
    assert logs == "only stdout\n"
