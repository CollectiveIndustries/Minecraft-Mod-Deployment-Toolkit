# tests/modpack_upgrade/providers/test_curseforge.py

"""CurseForge adapter tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.providers.base import HttpResult
from minecraft.modpack_upgrade.providers.curseforge import CurseForgeProvider


class FakeTransport:
    """Scripted HttpTransport routed by URL substring."""

    def __init__(self) -> None:
        self._scripted: list[tuple[str, HttpResult]] = []
        self.calls: list[dict[str, Any]] = []

    def set(self, url_substring: str, result: HttpResult) -> None:
        """Register a response matched by substring."""
        self._scripted.append((url_substring, result))

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_bytes: int | None = None,
    ) -> HttpResult:
        """Return the first matching scripted response."""
        self.calls.append({"url": url, "headers": headers})
        for sub, result in self._scripted:
            if sub in url:
                return result
        raise AssertionError(f"unexpected GET: {url}")


def _ok(body: Any) -> HttpResult:
    """Return a 200 JSON response for ``body``."""
    return HttpResult(status_code=200, content=json.dumps(body).encode("utf-8"))


def _file_entry(
    file_id: int = 1,
    *,
    file_name: str = "mod-1.0.jar",
    release_type: int = 1,
    download_url: str | None = "https://edge.forgecdn.net/mod-1.0.jar",
    hashes: list[dict] | None = None,
    deps: list[dict] | None = None,
) -> dict:
    """Build one CurseForge file object."""
    return {
        "id": file_id,
        "fileName": file_name,
        "releaseType": release_type,
        "gameVersions": ["1.21.1", "Forge"],
        "downloadUrl": download_url,
        "hashes": hashes or [{"algo": 1, "value": "a" * 40}, {"algo": 2, "value": "b" * 32}],
        "dependencies": deps or [],
    }


# ---------------------------------------------------------------------------
# Headers / API key
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_on_query() -> None:
    """A provider without an API key raises immediately."""
    provider = CurseForgeProvider(FakeTransport(), api_key=None)
    with pytest.raises(ProviderError, match="API key"):
        provider.query_versions("238222", "1.21.1", "forge")


def test_api_key_sent_as_x_api_key_header() -> None:
    """The API key arrives in the ``x-api-key`` header."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": []}))
    CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")
    assert transport.calls[0]["headers"]["x-api-key"] == "secret"


# ---------------------------------------------------------------------------
# query_versions - happy path
# ---------------------------------------------------------------------------


def test_query_versions_parses_files() -> None:
    """A well-formed file list is parsed into ProviderVersion objects."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry()]}))
    provider = CurseForgeProvider(transport, api_key="secret")
    versions = provider.query_versions("238222", "1.21.1", "forge")
    assert len(versions) == 1
    v = versions[0]
    assert v.version_id == "1"
    assert v.filename == "mod-1.0.jar"
    assert v.release_channel == "release"
    assert v.hashes["sha1"] == "a" * 40
    assert v.hashes["md5"] == "b" * 32


@pytest.mark.parametrize(
    "loader,expected_code",
    [("forge", 1), ("fabric", 4), ("quilt", 5), ("neoforge", 6)],
)
def test_loader_mapping(loader: str, expected_code: int) -> None:
    """The loader name maps to CurseForge's numeric modLoaderType."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": []}))
    CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", loader)
    assert f"modLoaderType={expected_code}" in transport.calls[0]["url"]


def test_unknown_loader_raises() -> None:
    """An unknown loader name raises ProviderError."""
    with pytest.raises(ProviderError, match="loader"):
        CurseForgeProvider(FakeTransport(), api_key="secret").query_versions("238222", "1.21.1", "custom")


def test_non_numeric_project_id_raises() -> None:
    """A non-numeric project_id raises - CurseForge uses integer mod IDs."""
    with pytest.raises(ProviderError, match="numeric"):
        CurseForgeProvider(FakeTransport(), api_key="secret").query_versions("create", "1.21.1", "forge")


def test_404_returns_empty() -> None:
    """A 404 on files is treated as 'mod does not exist'."""
    transport = FakeTransport()
    transport.set("/files", HttpResult(status_code=404, content=b""))
    assert CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge") == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_raises(status: int) -> None:
    """401/403 raise with an API-key-specific hint."""
    transport = FakeTransport()
    transport.set("/files", HttpResult(status_code=status, content=b""))
    with pytest.raises(ProviderError, match="API key"):
        CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")


# ---------------------------------------------------------------------------
# Release / hash / relation mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,expected", [(1, "release"), (2, "beta"), (3, "alpha"), (99, "unknown")])
def test_release_type_mapping(code: int, expected: str) -> None:
    """ReleaseType codes map to their slip channels."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(release_type=code)]}))
    v = CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")[0]
    assert v.release_channel == expected


def test_unknown_hash_algo_code_is_dropped() -> None:
    """A hash entry with an unrecognised algo code is dropped."""
    entry = _file_entry(hashes=[{"algo": 1, "value": "a" * 40}, {"algo": 99, "value": "b" * 32}])
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [entry]}))
    v = CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")[0]
    assert set(v.hashes) == {"sha1"}


@pytest.mark.parametrize(
    "relation,expected",
    [(1, "embedded"), (2, "optional"), (3, "required"), (4, "tool"), (5, "incompatible"), (6, "include")],
)
def test_relation_type_mapping(relation: int, expected: str) -> None:
    """The six Section 5.3 relation types map to the slip dependency types."""
    entry = _file_entry(deps=[{"modId": 999, "relationType": relation}])
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [entry]}))
    v = CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")[0]
    assert v.dependencies[0].dependency_type == expected


def test_unknown_relation_type_is_dropped() -> None:
    """An unrecognised relationType drops the edge."""
    entry = _file_entry(deps=[{"modId": 999, "relationType": 99}])
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [entry]}))
    v = CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")[0]
    assert v.dependencies == ()


# ---------------------------------------------------------------------------
# Filename and download URL
# ---------------------------------------------------------------------------


def test_filename_path_traversal_rejected() -> None:
    """A path-traversal filename drops the file."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(file_name="../../etc/passwd")]}))
    assert CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge") == []


def test_null_download_url_becomes_none() -> None:
    """A file with null downloadUrl yields download_url=None."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(download_url=None)]}))
    v = CurseForgeProvider(transport, api_key="secret").query_versions("238222", "1.21.1", "forge")[0]
    assert v.download_url is None


# ---------------------------------------------------------------------------
# fetch_artifact
# ---------------------------------------------------------------------------


def test_fetch_artifact_verifies_hashes() -> None:
    """Matching declared hashes return the bytes."""
    payload = b"artifact"
    sha1 = hashlib.sha1(payload).hexdigest()
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(hashes=[{"algo": 1, "value": sha1}])]}))
    transport.set("https://edge.forgecdn.net", HttpResult(status_code=200, content=payload))
    provider = CurseForgeProvider(transport, api_key="secret")
    v = provider.query_versions("238222", "1.21.1", "forge")[0]
    assert provider.fetch_artifact(v) == payload


def test_fetch_artifact_no_url_raises() -> None:
    """A null download_url cannot be fetched."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(download_url=None)]}))
    provider = CurseForgeProvider(transport, api_key="secret")
    v = provider.query_versions("238222", "1.21.1", "forge")[0]
    with pytest.raises(ProviderError, match="no download URL"):
        provider.fetch_artifact(v)


def test_fetch_artifact_hash_mismatch_raises() -> None:
    """A mismatched declared hash raises."""
    transport = FakeTransport()
    transport.set("/files", _ok({"data": [_file_entry(hashes=[{"algo": 1, "value": "0" * 40}])]}))
    transport.set("https://edge.forgecdn.net", HttpResult(status_code=200, content=b"artifact"))
    provider = CurseForgeProvider(transport, api_key="secret")
    v = provider.query_versions("238222", "1.21.1", "forge")[0]
    with pytest.raises(ProviderError, match="hash mismatch"):
        provider.fetch_artifact(v)
