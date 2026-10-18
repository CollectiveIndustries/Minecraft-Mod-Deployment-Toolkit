# src/minecraft/modpack_upgrade/cli/commands/stubs.py

"""Placeholder implementations for not-yet-implemented commands."""

from __future__ import annotations

import sys

__all__ = ["not_implemented"]


def not_implemented(name: str, hint: str | None = None) -> int:
    """Print a "not implemented" message and return exit code 1."""
    print(f"error: {name!r} is not yet implemented", file=sys.stderr)
    if hint:
        print(f"  hint: {hint}", file=sys.stderr)
    return 1
