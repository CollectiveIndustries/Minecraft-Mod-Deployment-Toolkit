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

Logging
-------

The shim's only unique piece of information is the process exit
boundary. It logs entry to ``cli`` at DEBUG and the return value from
``main`` at DEBUG immediately before ``sys.exit``. Every event that
matters for an operator is emitted by ``main`` or a module it calls -
the shim never adds its own INFO/ERROR traffic. Module logger is
``minecraft.deploy_pack.deploy_pack``.
"""

from __future__ import annotations

import sys

from .logging_setup import get_logger
from .main import main as _main

_log = get_logger(__name__)
__all__ = ["cli"]


def cli() -> None:
    """Console-script entry point. Exits with the code from :func:`main`."""
    _log.debug("cli: invoking main()")
    code = _main()
    _log.debug(f"cli: main() returned exit code {code}; calling sys.exit")
    sys.exit(code)


if __name__ == "__main__":
    cli()
