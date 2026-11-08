# tests/unit/common/test_cli_logging.py

"""Tests for minecraft.common.cli_logging and the LoggingCore contracts it relies on.

The critical regression: ``configure(debug=False)`` must suppress DEBUG
records at the source. Prior to this fix, ``_patch_sync_emit`` bypassed
``LoggingCore.emit``'s level filter, so ``pdm run install-dev`` (no
``--debug``) emitted ``[DEBUG] minecraft.deploy_pack.config_model:`` lines
that should have been dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from LoggingCore import get_logger
from LoggingCore import reset as core_reset

from minecraft.common import cli_logging


@pytest.fixture(autouse=True)
def _clean_core() -> Iterator[None]:
    """Reset LoggingCore around every test so level/cache state does not leak."""
    core_reset()
    yield
    core_reset()


# ---------------------------------------------------------------------------
# Bug regression: debug=False suppresses DEBUG
# ---------------------------------------------------------------------------


def test_configure_false_drops_debug_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A diagnostic logger's DEBUG line must not appear with debug=False."""
    cli_logging.configure(debug=False)
    log = get_logger("minecraft.deploy_pack.config_model")
    log.debug("should not appear")
    captured = capsys.readouterr()
    assert "should not appear" not in captured.out
    assert "should not appear" not in captured.err


def test_configure_true_shows_debug_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A diagnostic logger's DEBUG line must appear with debug=True."""
    cli_logging.configure(debug=True)
    log = get_logger("minecraft.deploy_pack.config_model")
    log.debug("should appear")
    captured = capsys.readouterr()
    assert "should appear" in captured.err


def test_configure_false_drops_debug_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A CLI logger's DEBUG line must not appear with debug=False."""
    cli_logging.configure(debug=False)
    log = get_logger("minecraft.deploy_pack.cli.main")
    log.debug("cli debug not shown")
    captured = capsys.readouterr()
    assert "cli debug not shown" not in captured.out
    assert "cli debug not shown" not in captured.err


# ---------------------------------------------------------------------------
# Routing: CLI vs diagnostic
# ---------------------------------------------------------------------------


def test_cli_logger_routes_info_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI INFO goes to stdout, verbatim."""
    cli_logging.configure(debug=False)
    log = get_logger("minecraft.deploy_pack.cli.main")
    log.info("cli message")
    captured = capsys.readouterr()
    assert "cli message" in captured.out
    assert "cli message" not in captured.err


def test_cli_logger_routes_error_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ERROR goes to stderr (errors are diagnostics, even from CLI loggers)."""
    cli_logging.configure(debug=False)
    log = get_logger("minecraft.deploy_pack.cli.main")
    log.error("cli error")
    captured = capsys.readouterr()
    assert "cli error" in captured.err
    assert "cli error" not in captured.out


def test_diagnostic_logger_routes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-CLI loggers always write to stderr with the [LEVEL] prefix."""
    cli_logging.configure(debug=False)
    log = get_logger("minecraft.deploy_pack.config_model")
    log.info("diag message")
    captured = capsys.readouterr()
    assert "diag message" in captured.err
    assert "[INFO]" in captured.err
    assert "diag message" not in captured.out


# ---------------------------------------------------------------------------
# Reconfiguration
# ---------------------------------------------------------------------------


def test_reconfigure_debug_false_after_true(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --debug run followed by a --no-debug run drops DEBUG on the second."""
    cli_logging.configure(debug=True)
    log = get_logger("minecraft.deploy_pack.config_model")
    log.debug("first debug")
    capsys.readouterr()

    cli_logging.configure(debug=False)
    log2 = get_logger("minecraft.deploy_pack.config_model")
    log2.debug("second debug")
    captured = capsys.readouterr()
    assert "second debug" not in captured.err


def test_configure_clears_logger_cache() -> None:
    """A reconfigure invalidates cached loggers so the new level applies."""
    cli_logging.configure(debug=False)
    pre = get_logger("test.cache_probe")
    cli_logging.configure(debug=True)
    post = get_logger("test.cache_probe")
    assert post is not pre


# ---------------------------------------------------------------------------
# Stdlib interop
# ---------------------------------------------------------------------------


def test_configure_sets_stdlib_root_level() -> None:
    """configure() keeps logging.getLogger().level in sync for test fixtures."""
    cli_logging.configure(debug=False)
    assert logging.getLogger().level == logging.INFO
    cli_logging.configure(debug=True)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_survives_repeated_calls() -> None:
    """Repeated configure() calls do not raise or add duplicate handlers."""
    cli_logging.configure(debug=False)
    cli_logging.configure(debug=False)
    cli_logging.configure(debug=True)
    cli_logging.configure(debug=False)
