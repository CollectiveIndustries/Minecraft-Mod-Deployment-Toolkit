# tests/deploy_pack/test_main_post_failure.py

"""Targeted tests for main._handle_post_failure.

``_handle_post_failure`` is only reached from ``_run_post_phase`` when
either ``start_failed`` or ``health_failed`` is non-empty. Its branches
are the fallback mechanisms for reporting a post-phase failure:

  * ``failure_stage`` is None -> derive "post_hook" vs "health_timeout"
    from ``start_failed``.
  * ``error_summary`` is callable -> use its return value (the real
    ``PostHookResult`` shape).
  * ``error_summary`` is non-callable but not None -> stringify it (legacy
    callers that set it as an attribute).
  * ``error_summary`` is absent -> assemble the message from
    ``start_failed`` and ``health_failed`` manually.

The risk report flagged ``_handle_post_failure`` at 59.1% coverage; the
manual-assembly branch and the ``stage is None`` fallback were untested.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.main import _handle_post_failure, _RuntimeState

_LOGGER = logging.getLogger("test_main_post_failure")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(*, notify: bool = False) -> _RuntimeState:
    """Return a minimal _RuntimeState with every unused field set to a placeholder.

    ``_handle_post_failure`` reads only ``config``, ``plan``, ``pre``,
    ``reloaded``, ``reload_failed``, ``notify``, and ``logger``. The other
    fields are required by the dataclass but unused on this code path.
    """
    return _RuntimeState(
        config=SimpleNamespace(),
        plan=SimpleNamespace(partition=[], container_states={}),
        scopes=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        notify=notify,
        protect_patterns=[],
        logger=_LOGGER,
        container_of={},
        rcon_set=None,  # type: ignore[arg-type]
        recovery_ctx=None,  # type: ignore[arg-type]
        pre=None,
        reloaded=[],
        reload_failed=[],
    )


def _make_post(**overrides: object) -> SimpleNamespace:
    """Return a PostHookResult-shaped stub.

    ``error_summary`` is absent by default; tests that want a callable or
    non-callable value pass it explicitly.
    """
    base: dict[str, object] = {
        "started": [],
        "start_failed": [],
        "healthy": [],
        "health_failed": [],
        "failure_stage": "post_hook",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _capture_send_failure(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace ``_send_failure`` with a recorder; return the call log."""
    calls: list[dict] = []

    def fake_send(config: object, stage: str, err: str, status: dict, logger: object) -> None:
        calls.append(
            {
                "config": config,
                "stage": stage,
                "err": err,
                "status": status,
                "logger": logger,
            }
        )

    monkeypatch.setattr(main_mod, "_send_failure", fake_send)
    return calls


# ---------------------------------------------------------------------------
# stage is None -> derive from start_failed
# ---------------------------------------------------------------------------


def test_stage_none_with_start_failed_uses_post_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """stage=None + start_failed non-empty -> fallback stage is 'post_hook'."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(
        failure_stage=None,
        start_failed=["s1"],
        error_summary=lambda: "start boom",
    )
    rc = _handle_post_failure(_make_state(notify=True), post)
    assert rc == 1
    assert calls[0]["stage"] == "post_hook"


def test_stage_none_without_start_failed_uses_health_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage=None + start_failed empty -> fallback stage is 'health_timeout'."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(
        failure_stage=None,
        start_failed=[],
        health_failed=["s1"],
        error_summary=lambda: "health boom",
    )
    rc = _handle_post_failure(_make_state(notify=True), post)
    assert rc == 1
    assert calls[0]["stage"] == "health_timeout"


# ---------------------------------------------------------------------------
# error_summary variants
# ---------------------------------------------------------------------------


def test_error_summary_callable_is_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A callable error_summary (the real PostHookResult shape) supplies err verbatim."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook", error_summary=lambda: "called")
    _handle_post_failure(_make_state(notify=True), post)
    assert calls[0]["err"] == "called"


def test_error_summary_string_is_str_cast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-callable error_summary that is not None is stringified."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook", error_summary="raw-text")
    _handle_post_failure(_make_state(notify=True), post)
    assert calls[0]["err"] == "raw-text"


def test_no_error_summary_assembles_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without error_summary, start and health failures are joined with '; '."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(
        failure_stage="post_hook",
        start_failed=["s1"],
        health_failed=["s2"],
    )
    _handle_post_failure(_make_state(notify=True), post)
    err = calls[0]["err"]
    assert "docker start failed for: s1" in err
    assert "health timeout for: s2" in err
    assert "; " in err


def test_no_error_summary_assembles_start_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without error_summary and only start failures, the assembly names them."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook", start_failed=["s1"])
    _handle_post_failure(_make_state(notify=True), post)
    assert calls[0]["err"] == "docker start failed for: s1"


def test_no_error_summary_assembles_health_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without error_summary and only health failures, the assembly names them."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="health_timeout", health_failed=["s1", "s2"])
    _handle_post_failure(_make_state(notify=True), post)
    assert calls[0]["err"] == "health timeout for: s1, s2"


def test_no_error_summary_assembles_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without error_summary and no failures listed, the fallback string is used."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook")
    _handle_post_failure(_make_state(notify=True), post)
    assert calls[0]["err"] == "post-start phase failed"


# ---------------------------------------------------------------------------
# state.notify branches
# ---------------------------------------------------------------------------


def test_notify_false_skips_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify=False -> no Discord dispatch, return code still 1."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook", error_summary=lambda: "boom")
    rc = _handle_post_failure(_make_state(notify=False), post)
    assert rc == 1
    assert calls == []


def test_notify_true_calls_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify=True -> send_failure is invoked exactly once with the computed stage and err."""
    calls = _capture_send_failure(monkeypatch)
    post = _make_post(
        failure_stage="health_timeout",
        health_failed=["s1"],
        error_summary=lambda: "timed out",
    )
    _handle_post_failure(_make_state(notify=True), post)
    assert len(calls) == 1
    assert calls[0]["stage"] == "health_timeout"
    assert calls[0]["err"] == "timed out"


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------


def test_always_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A post-phase failure always exits 1 (runtime failure, Section 2.4)."""
    _capture_send_failure(monkeypatch)
    post = _make_post(failure_stage="post_hook", start_failed=["s1"])
    assert _handle_post_failure(_make_state(notify=False), post) == 1
