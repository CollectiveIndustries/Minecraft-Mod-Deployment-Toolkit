# tests/modpack_upgrade/providers/test_base.py

"""Provider ABC, data model, and validation helper tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from minecraft.modpack_upgrade.providers.base import (
    KNOWN_HASH_ALGORITHMS,
    DependencyType,
    HttpResult,
    Provider,
    ProviderDependency,
    ProviderVersion,
    sanitize_filename,
    validate_hash_algorithm,
)


def _version(**overrides: object) -> ProviderVersion:
    """Return a minimal ProviderVersion for validation tests."""
    base: dict[str, object] = {
        "version_id": "v1",
        "filename": "mod.jar",
        "download_url": "https://example.com/mod.jar",
        "hashes": {"sha1": "a" * 40},
        "game_versions": ("1.21.1",),
        "loaders": ("forge",),
        "release_channel": "release",
        "dependencies": (),
        "client_side": None,
        "server_side": None,
    }
    base.update(overrides)
    return ProviderVersion(**base)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_known_hash_algorithms_match_spec() -> None:
    """Section 3.1 restricts slip algorithms to exactly these four."""
    assert frozenset({"sha512", "sha256", "sha1", "md5"}) == KNOWN_HASH_ALGORITHMS


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


def test_sanitize_filename_happy() -> None:
    """A plain filename is returned verbatim."""
    assert sanitize_filename("create-1.20.1-6.0.8.jar") == "create-1.20.1-6.0.8.jar"


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "sub/mod.jar",
        "sub\\mod.jar",
        "..",
        ".",
        ".hidden.jar",
        "mod\x00.jar",
    ],
)
def test_sanitize_filename_rejects(filename: str) -> None:
    """Every documented Section 18.3 rejection is enforced at the boundary."""
    with pytest.raises(ValueError):
        sanitize_filename(filename)


# ---------------------------------------------------------------------------
# validate_hash_algorithm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("algo", ["sha512", "sha256", "sha1", "md5", "SHA512", "SHA1"])
def test_validate_hash_algorithm_accepts_known(algo: str) -> None:
    """Known algorithms (any case) are accepted and lowercased."""
    assert validate_hash_algorithm(algo) == algo.lower()


@pytest.mark.parametrize("algo", ["sha3-512", "crc32", ""])
def test_validate_hash_algorithm_rejects_unknown(algo: str) -> None:
    """Unknown algorithms raise."""
    with pytest.raises(ValueError):
        validate_hash_algorithm(algo)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_provider_dependency_with_project_id() -> None:
    """A Modrinth-shaped dependency has project_id set, mod_id None."""
    dep = ProviderDependency(project_id="create", mod_id=None, version_id=None, dependency_type="required")
    assert dep.project_id == "create"
    assert dep.mod_id is None


def test_provider_dependency_with_mod_id() -> None:
    """A CurseForge-shaped dependency has mod_id set, project_id None."""
    dep = ProviderDependency(project_id=None, mod_id="123", version_id=None, dependency_type="required")
    assert dep.mod_id == "123"
    assert dep.project_id is None


def test_provider_version_is_frozen() -> None:
    """ProviderVersion is immutable."""
    v = _version()
    with pytest.raises(FrozenInstanceError):
        v.filename = "other.jar"  # type: ignore[misc]


def test_provider_version_with_none_download_url() -> None:
    """download_url may be None (CurseForge manual-download files)."""
    v = _version(download_url=None)
    assert v.download_url is None


def test_http_result_default_headers_empty() -> None:
    """HttpResult defaults to empty headers."""
    r = HttpResult(status_code=200, content=b"")
    assert r.headers == {}


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_provider_is_abstract() -> None:
    """Provider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        Provider()  # type: ignore[abstract]


def test_dependency_type_matches_spec() -> None:
    """The DependencyType literal enumerates the six Section A1.3 values."""
    values = {"required", "optional", "incompatible", "embedded", "tool", "include"}
    assert set(DependencyType.__args__) == values  # type: ignore[attr-defined]
