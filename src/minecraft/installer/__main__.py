# src/minecraft/installer/__main__.py

"""Support ``python -m minecraft.installer``."""

from __future__ import annotations

import argparse
import sys

from .cli import install_dev, install_prod, rotate_secrets

_DISPATCH = {
    "install-prod": install_prod,
    "install-dev": install_dev,
    "rotate-secrets": rotate_secrets,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minecraft.installer",
        description="First-time installer and secret rotator for the Minecraft deployment system.",
        add_help=True,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=sorted(_DISPATCH),
        help="Subcommand to run",
    )
    parser.add_argument(
        "remaining",
        nargs=argparse.REMAINDER,
        help="Arguments passed to the subcommand",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch to one of the three installer commands."""
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command is None:
        parser.print_help()
        return 0
    handler = _DISPATCH[ns.command]
    return handler(ns.remaining)


if __name__ == "__main__":
    sys.exit(main())
