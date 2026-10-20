# src/minecraft/discord_bot/cli_invoker.py

"""Subprocess ``minecraft-upgrade`` invocation seam (Slice 15 T3a).

Handlers do not import the resolver, approval, or materialize logic
directly. They invoke the ``minecraft-upgrade`` CLI as a subprocess,
with the toolchain's own config directory passed via ``--config-dir``.
This is the "bot is not the controller" property from spec Section 11.3 rule 8
applied at the process boundary: the CLI owns the state machine, the
bot is a relay.

Why a subprocess, not an in-process import
------------------------------------------
The bot daemon is a peer service, not a subprocess of the CLI. In-process
imports would couple the bot's crash surface to the resolver's and would
require the bot to hold the same file locks the CLI does. The subprocess
boundary keeps the two services independently restartable, gives the bot
a clean per-invocation timeout, and matches the operator's mental model
(the CLI is the documented interface).

Contract
--------
:class:`CliInvoker` is the injection point. Two implementations:

- :class:`SubprocessCliInvoker` runs the configured binary with
  ``asyncio.create_subprocess_exec``. A missing binary, a start failure,
  or an exceeded timeout raises :class:`DiscordBotCliError`. A non-zero
  exit code from a successfully-started subprocess is **not** an
  exception: it is reported in the returned :class:`CliResult` so the
  handler can decide whether to surface it as a command failure.

- :class:`FakeCliInvoker` records calls and returns scripted results for
  tests. Failures are pre-loaded with :meth:`expect_raise`; successful
  runs with :meth:`expect`. Unmatched calls raise ``AssertionError`` so
  a handler that invokes an unexpected command fails loudly rather than
  silently producing a plausible-looking response.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import DiscordBotCliError

__all__ = [
    "CliInvoker",
    "CliResult",
    "FakeCliInvoker",
    "SubprocessCliInvoker",
]


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI invocation.

    ``exit_code`` is ``0`` on success. ``stdout`` and ``stderr`` are
    UTF-8 decoded with replacement so a subprocess that writes arbitrary
    bytes cannot crash the handler. Trailing whitespace is not stripped;
    the handler decides what to display.
    """

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class CliInvoker(Protocol):
    """Injection point for CLI invocation.

    Implementations MUST NOT raise on a non-zero exit code; they return
    it in :class:`CliResult`. They MAY raise :class:`DiscordBotCliError`
    for conditions that prevent the subprocess from running to completion
    (missing binary, start failure, timeout).
    """

    async def invoke(self, args: list[str]) -> CliResult:
        """Run the CLI with ``args`` and return the result.

        ``args`` are the subcommand and its flags (for example
        ``["approve", "run-123"]``). The implementation is responsible
        for prepending any global arguments such as ``--config-dir``.
        """
        ...


class SubprocessCliInvoker:
    """Run ``minecraft-upgrade`` as a subprocess.

    When ``config_dir`` is set, ``--config-dir <config_dir>`` is
    prepended to every invocation's argv. When it is ``None``, no prefix
    is added; this is intended for tests that run an unrelated binary.

    The subprocess runs with ``cwd=project_root`` when ``project_root``
    is set (default: ``config_dir.parent`` or the process CWD). This
    matches the operator-facing convention and makes relative paths in
    CLI diagnostics stable.
    """

    def __init__(
        self,
        *,
        binary: str,
        config_dir: Path | None = None,
        timeout_seconds: int = 60,
        project_root: Path | None = None,
    ) -> None:
        """Store the invocation parameters."""
        self._binary = binary
        self._config_dir = config_dir
        self._timeout_seconds = timeout_seconds
        if project_root is None:
            project_root = config_dir.parent if config_dir is not None else Path.cwd()
        self._project_root = project_root

    async def invoke(self, args: list[str]) -> CliResult:
        """Run the CLI and return its result. Raises :class:`DiscordBotCliError` on start failure or timeout."""
        full_args = self._build_argv(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *full_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )
        except FileNotFoundError as exc:
            raise DiscordBotCliError(f"CLI binary not found: {self._binary}") from exc
        except OSError as exc:
            raise DiscordBotCliError(f"could not start CLI: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DiscordBotCliError(f"CLI timed out after {self._timeout_seconds}s: {' '.join(args)}") from None

        return CliResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    def _build_argv(self, args: list[str]) -> list[str]:
        """Construct the full argv, prepending ``--config-dir`` when set."""
        argv = [self._binary]
        if self._config_dir is not None:
            argv.extend(["--config-dir", str(self._config_dir)])
        argv.extend(args)
        return argv


class FakeCliInvoker:
    """In-memory :class:`CliInvoker` for tests.

    Records every ``invoke`` call in :attr:`calls` (including failed
    ones) and returns queued results in FIFO order. Queued results are
    matched against the exact argument list: an invocation whose args do
    not match the next queued expectation raises ``AssertionError`` so
    handler logic that builds unexpected commands fails loudly.
    """

    def __init__(self) -> None:
        """Initialise an empty call log and expectation queue."""
        self.calls: list[list[str]] = []
        self._expectations: list[tuple[list[str], CliResult | Exception]] = []

    def expect(self, args: list[str], result: CliResult) -> FakeCliInvoker:
        """Queue ``result`` to be returned by the next matching ``invoke`` call.

        Returns ``self`` so expectations can be chained.
        """
        self._expectations.append((list(args), result))
        return self

    def expect_raise(self, args: list[str], exc: Exception) -> FakeCliInvoker:
        """Queue ``exc`` to be raised by the next matching ``invoke`` call.

        Returns ``self`` so expectations can be chained.
        """
        self._expectations.append((list(args), exc))
        return self

    async def invoke(self, args: list[str]) -> CliResult:
        """Match against the next queued expectation, or raise ``AssertionError``."""
        self.calls.append(list(args))
        if not self._expectations:
            raise AssertionError(f"FakeCliInvoker has no expectation for args={args!r}")
        expected_args, expected_outcome = self._expectations.pop(0)
        if list(args) != expected_args:
            raise AssertionError(f"FakeCliInvoker expected args={expected_args!r}, got {list(args)!r}")
        if isinstance(expected_outcome, Exception):
            raise expected_outcome
        return expected_outcome
