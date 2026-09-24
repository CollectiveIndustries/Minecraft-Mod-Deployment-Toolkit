# src/minecraft/deploy_pack/deploy_pack.py

"""Command-line entrypoint shim (Project_Specs.md §9.2).

The real logic lives in :mod:`minecraft.deploy_pack.main`. This module
exists so that:

  * ``pyproject.toml`` can point a console script at a stable path
    (``minecraft.deploy_pack.deploy_pack:cli``) without exposing
    ``main`` as the public symbol, and
  * ``python -m minecraft.deploy_pack`` (via ``__main__.py``) and a
    direct invocation of this file both funnel into the same code.

It deliberately does nothing else. Argument parsing, config load,
preflight, and the runtime sequence are all in ``main``.
"""

from __future__ import annotations

import sys

from .main import main as _main

__all__ = ["cli"]


def cli() -> None:
    """Console-script entry point. Exits with the code from :func:`main`."""
    sys.exit(_main())


if __name__ == "__main__":
    cli()
