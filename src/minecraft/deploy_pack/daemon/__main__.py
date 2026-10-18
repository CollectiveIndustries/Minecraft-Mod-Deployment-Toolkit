# src/minecraft/deploy_pack/daemon/__main__.py

"""Support ``python -m minecraft.deploy_pack.daemon``."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
