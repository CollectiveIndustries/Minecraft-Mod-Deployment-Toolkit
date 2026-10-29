# tests/deploy_pack/test_preflight_runner_online.py

"""Targeted tests for preflight.runner._validate_online_template_if_needed.

Section 5.11's second-pass validation: the online template is checked only
when --notify is on, --dry-run is off, and at least one restart_set member
is currently running. A failure raises PreflightError (exit 3), which
percolates through run_preflight and is then translated by main.main into
the operator-facing exit code.

Why a dedicated file: the function is only reached from the tail of
run_preflight after every other check has passed. Its low coverage in the
risk report reflects that the integration path never reaches it in tests.
The direct tests here pin every early-return branch and both outcomes of
the validation call.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from minecraft.deploy_pack.docker_runtime import ContainerState
from minecraft.deploy_pack.preflight import runner
from minecraft.deploy_pack.preflight.runner import _validate_online_template_if_needed
from minecraft.deploy_pack.preflight.types import PreflightError

_LOGGER = logging.getLogger("test_preflight_online")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discord_config(**overrides: Any) -> SimpleNamespace:
    """Return a DiscordConfig-shaped stub with a valid-looking online template."""
    base = SimpleNamespace(online_template="{tool_version} @ {timestamp}")
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _config(**overrides: Any) -> SimpleNamespace:
    """Return a DeploymentConfig-shaped stub carrying only ``.discord``."""
    return SimpleNamespace(discord=overrides.get("discord", _discord_config()))


def _running_state(name: str = "s1") -> ContainerState:
    """Return a running ContainerState with a health block."""
    return ContainerState(
        name=name,
        exists=True,
        status="running",
        running=True,
        health="healthy",
        raw={},
    )


def _stopped_state(name: str = "s1") -> ContainerState:
    """Return a not-running ContainerState."""
    return ContainerState(
        name=name,
        exists=True,
        status="exited",
        running=False,
        health=None,
        raw=None,
    )


def _missing_state(name: str = "s1") -> ContainerState:
    """Return the ContainerState the SDK produces for a missing container."""
    return ContainerState(
        name=name,
        exists=False,
        status="missing",
        running=False,
        health=None,
        raw=None,
    )


def _forbid_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``notifications.validate_online`` fail the test if called."""

    def boom(*a: Any, **k: Any) -> list:
        raise AssertionError("validate_online must not be called on this branch")

    monkeypatch.setattr(runner.notifications, "validate_online", boom)


def _return_validate(monkeypatch: pytest.MonkeyPatch, failures: list[tuple[str, str]]) -> list[dict]:
    """Stub ``validate_online`` to return ``failures`` and record its calls."""
    calls: list[dict] = []

    def fake(discord: Any, logger: Any = None) -> list[tuple[str, str]]:
        calls.append({"discord": discord, "logger": logger})
        return list(failures)

    monkeypatch.setattr(runner.notifications, "validate_online", fake)
    return calls


# ---------------------------------------------------------------------------
# Section 5.11: early-return branches
# ---------------------------------------------------------------------------


def test_skips_when_notify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify=False -> no validation (Section 5.11: only when --notify is active)."""
    _forbid_validate(monkeypatch)
    _validate_online_template_if_needed(
        _config(),
        notify=False,
        dry_run=False,
        restart_set=["s1"],
        container_states={"s1": _running_state()},
        logger=_LOGGER,
    )


def test_skips_when_dry_run_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True -> no validation (Section 2.6: no lifecycle, no notify)."""
    _forbid_validate(monkeypatch)
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=True,
        restart_set=["s1"],
        container_states={"s1": _running_state()},
        logger=_LOGGER,
    )


def test_skips_when_restart_set_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty restart_set -> no container will restart, so no online message will fire."""
    _forbid_validate(monkeypatch)
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=False,
        restart_set=[],
        container_states={},
        logger=_LOGGER,
    )


def test_skips_when_no_member_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart_set non-empty but no member is running -> no online message will fire."""
    _forbid_validate(monkeypatch)
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=False,
        restart_set=["s1", "s2"],
        container_states={"s1": _stopped_state("s1"), "s2": _missing_state("s2")},
        logger=_LOGGER,
    )


def test_skips_when_member_missing_from_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart_set member absent from container_states does not count as running."""
    _forbid_validate(monkeypatch)
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=False,
        restart_set=["s1"],
        container_states={},
        logger=_LOGGER,
    )


def test_proceeds_when_at_least_one_member_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single running member among several (rest stopped/missing) is enough to reach validation."""
    calls = _return_validate(monkeypatch, [])
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=False,
        restart_set=["s1", "s2"],
        container_states={"s1": _stopped_state("s1"), "s2": _running_state("s2")},
        logger=_LOGGER,
    )
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Section 5.11: validation outcomes
# ---------------------------------------------------------------------------


def test_valid_template_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid online template returns without raising."""
    calls = _return_validate(monkeypatch, [])
    _validate_online_template_if_needed(
        _config(),
        notify=True,
        dry_run=False,
        restart_set=["s1"],
        container_states={"s1": _running_state()},
        logger=_LOGGER,
    )
    assert len(calls) == 1
    assert calls[0]["logger"] is _LOGGER


def test_invalid_template_raises_preflight_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed online template raises PreflightError with each failure wrapped."""
    failures = [
        ("[discord.messages.online].template", "unknown placeholder {oops}"),
        ("[discord.messages.online].template", "cross-template {failure_stage}"),
    ]
    _return_validate(monkeypatch, failures)
    with pytest.raises(PreflightError) as ei:
        _validate_online_template_if_needed(
            _config(),
            notify=True,
            dry_run=False,
            restart_set=["s1"],
            container_states={"s1": _running_state()},
            logger=_LOGGER,
        )
    assert len(ei.value.failures) == 2
    sources = {f.source for f in ei.value.failures}
    assert sources == {"[discord.messages.online].template"}
    messages = " | ".join(f.message for f in ei.value.failures)
    assert "unknown placeholder" in messages
    assert "cross-template" in messages


def test_discord_config_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DiscordConfig handed to validate_online is config.discord, unmodified."""
    calls = _return_validate(monkeypatch, [])
    discord = _discord_config(online_template="my custom {tool_version}")
    _validate_online_template_if_needed(
        _config(discord=discord),
        notify=True,
        dry_run=False,
        restart_set=["s1"],
        container_states={"s1": _running_state()},
        logger=_LOGGER,
    )
    assert calls[0]["discord"] is discord
