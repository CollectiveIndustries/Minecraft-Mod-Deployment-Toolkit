# src/minecraft/deploy_pack/__main__.py
"""Support ``python -m minecraft.deploy_pack``."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
