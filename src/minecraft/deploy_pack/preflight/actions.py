# src/minecraft/deploy_pack/preflight/actions.py

"""Restart-policy adapter: per-path action, sticky-max, reasons (§4.6.1-4.6.3).

Logging
-------

Module logger is ``minecraft.deploy_pack.preflight.actions``.
``resolve_action`` is a hot loop (one call per changed path) and emits
no events; ``resolve_paths_action`` logs at DEBUG with one line per
contributing pattern and the aggregate count, then a final DEBUG line
with the sticky-max outcome that becomes the plan's effective action.
``sticky_max`` logs its input and the chosen action at DEBUG so a
``+pack`` escalation can be reconstructed. ``is_pack_action`` is a pure
predicate and emits nothing. There are no ERROR or WARN sites: this
module has no failure modes - unmatched paths default to ``restart``,
empty inputs return ``none``, and no path validation happens here.
"""

from __future__ import annotations

from minecraft.deploy_pack.logging_setup import get_logger

from .types import ReasonEntry

_log = get_logger(__name__)

ACTION_ORDER: dict[str, int] = {
    "none": 0,
    "none+pack": 1,
    "reload": 2,
    "reload+pack": 3,
    "restart": 4,
    "restart+pack": 5,
}


def resolve_action(path: str, policy: dict[str, str]) -> tuple[str, str]:
    """Return ``(action, pattern)`` for a changed path.

    Longest literal prefix (substring before the first ``*``) wins;
    ties broken by total pattern length, then lexicographically.
    Unlisted paths default to ``"restart"`` / ``"(default)"``.

    No logging: called once per changed path by
    :func:`resolve_paths_action`, where aggregate counts are logged
    instead of per-path decisions.
    """
    best_pattern: str | None = None
    best_key: tuple[int, int, str] | None = None
    for pattern in policy:
        literal = pattern.split("*", 1)[0]
        if not path.startswith(literal):
            continue
        key = (-len(literal), -len(pattern), pattern)
        if best_key is None or key < best_key:
            best_key = key
            best_pattern = pattern
    if best_pattern is None:
        return ("restart", "(default)")
    return (policy[best_pattern], best_pattern)


def is_pack_action(action: str) -> bool:
    """Return True if the action ends in ``+pack``.

    No logging: pure predicate called from :func:`sticky_max` and from
    :func:`run_preflight`'s per-member plan assembly.
    """
    return action.endswith("+pack")


def sticky_max(actions: list[str]) -> str:
    """Return the §4.6.2 sticky-max over ``actions``.

    ``+pack`` is a property of the batch, not of any single path: if any
    input carries it, the result carries it even when the maximum-ranked
    input did not.
    """
    if not actions:
        _log.debug("sticky_max: empty input -> 'none'")
        return "none"
    base = max(actions, key=lambda a: ACTION_ORDER.get(a, -1))
    if any(is_pack_action(a) for a in actions) and not is_pack_action(base):
        result = base + "+pack"
        _log.debug(f"sticky_max: {actions} -> {result} (base {base!r} escalated to +pack)")
        return result
    _log.debug(f"sticky_max: {actions} -> {base}")
    return base


def resolve_paths_action(changed_paths: list[str], policy: dict[str, str]) -> tuple[str, list[ReasonEntry]]:
    """Compute the effective action and its contributing reasons (§4.6.3).

    Logs one DEBUG line per contributing pattern and one final DEBUG
    line with the effective action and reason count. Per-path decisions
    (the output of :func:`resolve_action`) are not logged - the
    aggregate is what the operator sees in the plan and notification.
    """
    if not changed_paths:
        _log.debug("resolve_paths_action: no changed paths -> ('none', [])")
        return ("none", [])
    by_pattern: dict[str, list[str]] = {}
    pattern_action: dict[str, str] = {}
    for path in changed_paths:
        action, pattern = resolve_action(path, policy)
        by_pattern.setdefault(pattern, []).append(path)
        pattern_action[pattern] = action
    effective = sticky_max(list(pattern_action.values()))
    reasons = [
        ReasonEntry(path_prefix=pattern, action=pattern_action[pattern], changed_paths=sorted(by_pattern[pattern]))
        for pattern in sorted(by_pattern)
        if pattern_action[pattern] == effective or pattern_action[pattern] + "+pack" == effective
    ]
    _log.debug(f"resolve_paths_action: {len(changed_paths)} path(s) across {len(by_pattern)} pattern(s); effective={effective!r}")
    for pattern in sorted(by_pattern):
        _log.debug(f"  pattern {pattern!r}: action={pattern_action[pattern]!r} paths={len(by_pattern[pattern])}")
    return (effective, reasons)
