# src/minecraft/modpack_upgrade/providers/registry.py

"""Name -> :class:`Provider` lookup.

The resolver asks the registry for ``registry.get("modrinth")`` rather
than importing the adapter directly. This keeps the resolver testable
with fake providers and lets slice 8+ add adapters (e.g., a hypothetical
provider) without touching the resolver.
"""

from __future__ import annotations

from minecraft.modpack_upgrade.errors import ProviderError

from .base import Provider

__all__ = ["ProviderRegistry"]


class ProviderRegistry:
    """A mutable mapping from provider name to adapter."""

    def __init__(self) -> None:
        """Construct an empty registry."""
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        """Register ``provider`` under its ``name``.

        Re-registering a name overwrites the previous adapter - tests
        use this to substitute fakes. Production code registers exactly
        once per provider.
        """
        if not provider.name:
            raise ProviderError("cannot register a provider with an empty name")
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        """Return the provider registered under ``name``.

        Raises :class:`ProviderError` if ``name`` was never registered -
        a caller asking for a missing provider is a wiring bug, not a
        runtime condition.
        """
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderError(f"no provider registered under name {name!r} (known: {sorted(self._providers)})")
        return provider

    def names(self) -> list[str]:
        """Return the registered provider names, sorted."""
        return sorted(self._providers)

    def __contains__(self, name: object) -> bool:
        """Support ``"modrinth" in registry``."""
        return isinstance(name, str) and name in self._providers
