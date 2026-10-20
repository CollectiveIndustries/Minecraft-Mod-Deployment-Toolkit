# tests/discord_bot/test_cli_invoker.py

"""CLI invoker seam tests (T3a)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minecraft.discord_bot.cli_invoker import (
    CliResult,
    FakeCliInvoker,
    SubprocessCliInvoker,
)
from minecraft.discord_bot.errors import DiscordBotCliError

# ---------------------------------------------------------------------------
# FakeCliInvoker
# ---------------------------------------------------------------------------


async def test_fake_records_calls() -> None:
    """Every invocation is recorded in ``calls``, in order."""
    fake = FakeCliInvoker()
    fake.expect(["status"], CliResult(exit_code=0, stdout="ok", stderr=""))
    fake.expect(["approve", "run-1"], CliResult(exit_code=0, stdout="approved", stderr=""))
    await fake.invoke(["status"])
    await fake.invoke(["approve", "run-1"])
    assert fake.calls == [["status"], ["approve", "run-1"]]


async def test_fake_returns_queued_result() -> None:
    """The queued result is returned to the caller."""
    fake = FakeCliInvoker()
    expected = CliResult(exit_code=0, stdout="hello", stderr="")
    fake.expect(["status"], expected)
    result = await fake.invoke(["status"])
    assert result is expected


async def test_fake_mismatched_args_raises() -> None:
    """An invocation with unexpected args raises AssertionError."""
    fake = FakeCliInvoker()
    fake.expect(["status"], CliResult(exit_code=0, stdout="", stderr=""))
    with pytest.raises(AssertionError, match="expected args"):
        await fake.invoke(["approve", "run-1"])


async def test_fake_no_expectation_raises() -> None:
    """An invocation with no queued expectation raises AssertionError."""
    fake = FakeCliInvoker()
    with pytest.raises(AssertionError, match="no expectation"):
        await fake.invoke(["status"])


async def test_fake_expect_raise() -> None:
    """A queued exception is raised by the matching invocation."""
    fake = FakeCliInvoker()
    fake.expect_raise(["status"], DiscordBotCliError("simulated"))
    with pytest.raises(DiscordBotCliError, match="simulated"):
        await fake.invoke(["status"])


async def test_fake_fifo_ordering() -> None:
    """Queued expectations are consumed in FIFO order."""
    fake = FakeCliInvoker()
    fake.expect(["a"], CliResult(exit_code=0, stdout="1", stderr=""))
    fake.expect(["b"], CliResult(exit_code=0, stdout="2", stderr=""))
    assert (await fake.invoke(["a"])).stdout == "1"
    assert (await fake.invoke(["b"])).stdout == "2"


# ---------------------------------------------------------------------------
# SubprocessCliInvoker
# ---------------------------------------------------------------------------


async def test_subprocess_returns_stdout(tmp_path: Path) -> None:
    """A successful subprocess returns the captured stdout."""
    invoker = SubprocessCliInvoker(binary=sys.executable, timeout_seconds=10, project_root=tmp_path)
    result = await invoker.invoke(["-c", "print('hello-subprocess')"])
    assert result.exit_code == 0
    assert "hello-subprocess" in result.stdout


async def test_subprocess_returns_nonzero_exit(tmp_path: Path) -> None:
    """A non-zero exit code is reported in the result, not raised."""
    invoker = SubprocessCliInvoker(binary=sys.executable, timeout_seconds=10, project_root=tmp_path)
    result = await invoker.invoke(["-c", "import sys; sys.exit(7)"])
    assert result.exit_code == 7


async def test_subprocess_captures_stderr(tmp_path: Path) -> None:
    """The subprocess stderr is captured."""
    invoker = SubprocessCliInvoker(binary=sys.executable, timeout_seconds=10, project_root=tmp_path)
    result = await invoker.invoke(["-c", "import sys; sys.stderr.write('oops\\n')"])
    assert "oops" in result.stderr


async def test_subprocess_prepends_config_dir(tmp_path: Path) -> None:
    """When config_dir is set, --config-dir is prepended to argv."""
    invoker = SubprocessCliInvoker(
        binary="/bin/echo",
        config_dir=tmp_path,
        timeout_seconds=10,
    )
    result = await invoker.invoke(["status"])
    assert result.exit_code == 0
    assert "--config-dir" in result.stdout
    assert str(tmp_path) in result.stdout
    assert "status" in result.stdout


async def test_subprocess_omits_config_dir_when_none(tmp_path: Path) -> None:
    """When config_dir is None, no --config-dir prefix is added."""
    invoker = SubprocessCliInvoker(binary="/bin/echo", config_dir=None, timeout_seconds=10, project_root=tmp_path)
    result = await invoker.invoke(["hello"])
    assert result.exit_code == 0
    assert "--config-dir" not in result.stdout
    assert "hello" in result.stdout


async def test_subprocess_missing_binary_raises(tmp_path: Path) -> None:
    """A missing binary raises DiscordBotCliError."""
    invoker = SubprocessCliInvoker(binary="/nonexistent/binary-xyz", timeout_seconds=5, project_root=tmp_path)
    with pytest.raises(DiscordBotCliError, match="not found"):
        await invoker.invoke(["status"])


async def test_subprocess_timeout_raises(tmp_path: Path) -> None:
    """A subprocess that exceeds the timeout raises DiscordBotCliError."""
    invoker = SubprocessCliInvoker(binary=sys.executable, timeout_seconds=1, project_root=tmp_path)
    with pytest.raises(DiscordBotCliError, match="timed out"):
        await invoker.invoke(["-c", "import time; time.sleep(10)"])


async def test_subprocess_uses_project_root_as_cwd(tmp_path: Path) -> None:
    """The subprocess runs with project_root as its cwd."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    invoker = SubprocessCliInvoker(binary=sys.executable, timeout_seconds=10, project_root=project_root)
    result = await invoker.invoke(["-c", "import os; print(os.getcwd())"])
    assert result.exit_code == 0
    assert str(project_root) in result.stdout
