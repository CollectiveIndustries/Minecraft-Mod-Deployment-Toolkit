# src/minecraft/deploy_pack/preflight/actions.py

"""Restart-policy adapter: per-path action, sticky-max, reasons (§4.6.1-4.6.3)."""

from __future__ import annotations

from .types import ReasonEntry

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
    """Return True if the action ends in ``+pack``."""
    return action.endswith("+pack")


def sticky_max(actions: list[str]) -> str:
    """Return the §4.6.2 sticky-max over ``actions``.

    ``+pack`` is a property of the batch, not of any single path: if any
    input carries it, the result carries it even when the maximum-ranked
    input did not.
    """
    if not actions:
        return "none"
    base = max(actions, key=lambda a: ACTION_ORDER.get(a, -1))
    if any(is_pack_action(a) for a in actions) and not is_pack_action(base):
        return base + "+pack"
    return base


def resolve_paths_action(changed_paths: list[str], policy: dict[str, str]) -> tuple[str, list[ReasonEntry]]:
    """Compute the effective action and its contributing reasons (§4.6.3)."""
    if not changed_paths:
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
    return (effective, reasons)
