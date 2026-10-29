# src/minecraft/installer/__init__.py

"""First-time installer and secret rotator for the Minecraft deployment system."""

from .cli import install_dev, install_prod, rotate_secrets
from .installer import install, rotate

__all__ = [
    "install",
    "install_dev",
    "install_prod",
    "rotate",
    "rotate_secrets",
]
