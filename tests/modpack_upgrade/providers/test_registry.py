# tests/modpack_upgrade/providers/test_registry.py

"""ProviderRegistry tests."""

from __future__ import annotations

import pytest

from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.providers.base import Provider, ProviderVersion
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry


class _FakeProvider(Provider):
    """A minimal provider for registry tests."""

    def __init__(self, name: str) -> None:
        """Set the provider name."""
        self.name = name

    def query_versions(self, project_id: str, game_version: str, loader: str) -> list[ProviderVersion]:
        """Return an empty list."""
        return []

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        """Return empty bytes."""
        return b""


def test_empty_registry_names() -> None:
    """A new registry has no providers."""
    assert ProviderRegistry().names() == []


def test_register_and_get() -> None:
    """A registered provider is retrievable by name."""
    r = ProviderRegistry()
    p = _FakeProvider("modrinth")
    r.register(p)
    assert r.get("modrinth") is p


def test_register_empty_name_raises() -> None:
    """A provider with an empty name cannot be registered."""
    r = ProviderRegistry()
    with pytest.raises(ProviderError, match="empty name"):
        r.register(_FakeProvider(""))


def test_get_missing_raises() -> None:
    """Getting a name that was never registered raises ProviderError."""
    with pytest.raises(ProviderError, match="no provider registered"):
        ProviderRegistry().get("bogus")


def test_names_sorted() -> None:
    """names() returns provider names sorted lexicographically."""
    r = ProviderRegistry()
    r.register(_FakeProvider("modrinth"))
    r.register(_FakeProvider("curseforge"))
    assert r.names() == ["curseforge", "modrinth"]


def test_contains() -> None:
    """In operator reports registration."""
    r = ProviderRegistry()
    r.register(_FakeProvider("modrinth"))
    assert "modrinth" in r
    assert "curseforge" not in r
    assert 42 not in r
