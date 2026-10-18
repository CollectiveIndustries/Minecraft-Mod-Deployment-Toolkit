# src/minecraft/modpack_upgrade/providers/__init__.py

"""Provider adapters for artifact resolution (slice 7b).

Public surface:

  * :class:`Provider` - the ABC every adapter implements.
  * :class:`ProviderVersion` / :class:`ProviderDependency` - the
    data model the resolver consumes.
  * :class:`HttpTransport` - the injectable I/O boundary.
  * :class:`RequestsTransport` - the production HTTP implementation.
  * :class:`ModrinthProvider` / :class:`CurseForgeProvider` - the two
    adapters.
  * :class:`ProviderRegistry` - a name -> provider lookup.
  * :func:`load_provider_config` - read ``[providers]`` from
    ``config.d/upgrade.toml`` and resolve the CurseForge key from the
    environment.
  * :func:`build_registry` - construct a registry from a config.

The resolver and the ``update`` command consume this package. Nothing
here makes a selection decision; adapters return candidates.
"""

from __future__ import annotations

from .base import (
    KNOWN_HASH_ALGORITHMS,
    DependencyType,
    HttpResult,
    HttpTransport,
    Provider,
    ProviderDependency,
    ProviderVersion,
    ReleaseChannel,
    sanitize_filename,
    validate_hash_algorithm,
)
from .config import ProviderConfig, build_registry, load_provider_config
from .curseforge import CurseForgeProvider
from .http import RequestsTransport
from .modrinth import ModrinthProvider
from .registry import ProviderRegistry

__all__ = [
    "KNOWN_HASH_ALGORITHMS",
    "CurseForgeProvider",
    "DependencyType",
    "HttpResult",
    "HttpTransport",
    "ModrinthProvider",
    "Provider",
    "ProviderConfig",
    "ProviderDependency",
    "ProviderRegistry",
    "ProviderVersion",
    "ReleaseChannel",
    "RequestsTransport",
    "build_registry",
    "load_provider_config",
    "sanitize_filename",
    "validate_hash_algorithm",
]
