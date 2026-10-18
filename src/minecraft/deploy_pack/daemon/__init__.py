# src/minecraft/deploy_pack/daemon/__init__.py

"""Minecraft deploy daemon (spec v1.3 Section 12), implemented on DaemonCore.

DaemonCore provides lifecycle, logging, and resources. This package
provides the Minecraft-specific business logic: config loading, release
verification, error codes, and the HTTP admin plugin that exposes
verify/publish/deploy/status.

The HTTP plugin is Minecraft's own code. It is not shipped by DaemonCore
and it is not available to any other application. If another application
wants equivalent HTTP behavior, its author decides whether to copy it,
import it, or extract it into a shared package.
"""

from .config import MinecraftDaemonConfig, load_config

__all__ = ["MinecraftDaemonConfig", "load_config"]
