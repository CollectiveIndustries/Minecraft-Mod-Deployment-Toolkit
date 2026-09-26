# tests/deploy_pack/test_main_seams.py

"""Signature contracts between main.py and the modules it calls.

The unit tests for main.py stub preflight and hooks with ``**kwargs``
lambdas, so a renamed keyword, a missing argument, or a swapped positional
slips through every test and only surfaces on the first live run. That is
exactly what happened twice: first ``run_preflight() got an unexpected
keyword argument 'scope_set'``, then ``execute_post_hook`` receiving
``health_timeout`` where it expected ``preflight_states``.

This module asserts call-site shape statically. Every call into a seam
target must bind to the real signature, and no seam call may pass more
than one positional argument. The first rule catches renames and count
drift. The second catches swapped positionals, which bind fine but put
values in the wrong slots.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from collections.abc import Callable
from typing import Any

import pytest

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.hooks import (
    compute_warned_and_running,
    execute_post_hook,
    execute_pre_hook,
    recover_stopped_containers,
)
from minecraft.deploy_pack.preflight import run_preflight

# Every function main.py calls into another module whose signature has
# caused, or could cause, a seam bug. Extend this map when a new seam
# proves fragile.
_SEAM_TARGETS: dict[str, Callable[..., Any]] = {
    "run_preflight": run_preflight,
    "compute_warned_and_running": compute_warned_and_running,
    "execute_pre_hook": execute_pre_hook,
    "execute_post_hook": execute_post_hook,
    "recover_stopped_containers": recover_stopped_containers,
}


def _main_ast() -> ast.Module:
    """Parse main.py to an AST. Source is read once; call sites are cached below."""
    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    return ast.parse(src)


_CALLS_BY_NAME: dict[str, list[ast.Call]] = {}


def _calls_to(name: str) -> list[ast.Call]:
    """All Call nodes in main.py whose callee resolves to ``name``.

    Matches both ``foo(...)`` and ``mod.foo(...)`` shapes. Result is
    cached per process; the file does not change during a test run.
    """
    if not _CALLS_BY_NAME:
        tree = _main_ast()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            callee = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if callee:
                _CALLS_BY_NAME.setdefault(callee, []).append(node)
    return _CALLS_BY_NAME.get(name, [])


@pytest.mark.parametrize("seam_name", sorted(_SEAM_TARGETS))
def test_seam_is_called_at_least_once(seam_name: str) -> None:
    """Guard: the seam is actually invoked. If this fails the tests below are vacuous."""
    assert _calls_to(seam_name), f"main.py no longer calls {seam_name}; remove it from _SEAM_TARGETS or restore the call site"


@pytest.mark.parametrize("seam_name", sorted(_SEAM_TARGETS))
def test_seam_calls_bind_to_real_signature(seam_name: str) -> None:
    """Every call to this seam binds to the target's real signature.

    Catches: unknown keyword, too many positionals, missing required
    positionals. ``bind_partial`` is used because default and keyword-only
    params may legitimately be omitted at the call site.
    """
    sig = inspect.signature(_SEAM_TARGETS[seam_name])
    for call in _calls_to(seam_name):
        if any(isinstance(a, ast.Starred) for a in call.args):
            continue  # *args at the call site; cannot be checked statically
        if any(kw.arg is None for kw in call.keywords):
            continue  # **kwargs at the call site; cannot be checked statically
        positional = [None] * len(call.args)
        keywords = {kw.arg: None for kw in call.keywords}
        try:
            sig.bind_partial(*positional, **keywords)
        except TypeError as exc:
            pytest.fail(f"{seam_name} call at main.py:{call.lineno} does not bind to signature {sig}: {exc}")


@pytest.mark.parametrize("seam_name", sorted(_SEAM_TARGETS))
def test_seam_calls_use_keywords_past_first_positional(seam_name: str) -> None:
    """No seam call may pass more than one positional argument.

    Rationale. ``execute_post_hook(runtime, to_stop, health_timeout, states)``
    binds cleanly against ``(runtime, stopped_by_deployment, preflight_states,
    health_timeout)``. All four arguments are consumed; two of them are in
    the wrong slots, and the failure only surfaces when ``preflight_states.get``
    runs on an int. A signature bind cannot detect this. Forcing named
    arguments past the first positional eliminates the class statically.
    """
    for call in _calls_to(seam_name):
        if len(call.args) <= 1:
            continue
        pytest.fail(
            f"{seam_name} call at main.py:{call.lineno} passes "
            f"{len(call.args)} positional args; use keyword arguments past "
            "the first positional (see this module's docstring for the failure "
            "mode this prevents)"
        )
