# tests/modpack_upgrade/providers/test_modrinth.py

"""Modrinth adapter tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import unquote

import pytest

from minecraft.modpack_upgrade.errors import ProviderError
from minecraft.modpack_upgrade.providers.base import HttpResult
from minecraft.modpack_upgrade.providers.modrinth import ModrinthProvider


class FakeTransport:
    """Scripted HttpTransport: routes by URL substring, records every call."""

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
        """Return the first scripted response whose substring matches ``url``."""
        self.calls.append({"url": url, "headers": headers, "timeout": timeout, "max_bytes": max_bytes})
        for sub, result in self._scripted:
            if sub in url:
                return result
        raise AssertionError(f"unexpected GET: {url}")


def _ok(body: Any) -> HttpResult:
    """Return a 200 JSON response for ``body``."""
    return HttpResult(status_code=200, content=json.dumps(body).encode("utf-8"))


def _version_entry(
    version_id: str = "AABB",
    *,
    version_type: str = "release",
    filename: str = "mod-1.0.jar",
    hashes: dict[str, str] | None = None,
    primary: bool = True,
    url: str | None = "https://cdn.modrinth.com/mod-1.0.jar",
    deps: list[dict] | None = None,
    game_versions: list[str] | None = None,
    loaders: list[str] | None = None,
) -> dict:
    """Build one Modrinth version object."""
    return {
        "id": version_id,
        "version_type": version_type,
        "game_versions": game_versions or ["1.21.1"],
        "loaders": loaders or ["forge"],
        "files": [
            {
                "primary": primary,
                "filename": filename,
                "url": url,
                "hashes": hashes or {"sha512": "a" * 128, "sha1": "b" * 40},
            }
        ],
        "dependencies": deps or [],
    }


# ---------------------------------------------------------------------------
# query_versions - happy path
# ---------------------------------------------------------------------------


def test_query_versions_returns_parsed_versions() -> None:
    """A well-formed list of version objects is parsed."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry()]))
    transport.set("/project/create", _ok({"client_side": "required", "server_side": "required"}))
    provider = ModrinthProvider(transport, user_agent="test/1.0")

    versions = provider.query_versions("create", "1.21.1", "forge")

    assert len(versions) == 1
    v = versions[0]
    assert v.version_id == "AABB"
    assert v.filename == "mod-1.0.jar"
    assert v.release_channel == "release"
    assert v.hashes["sha512"] == "a" * 128
    assert v.hashes["sha1"] == "b" * 40


def test_query_versions_sends_user_agent() -> None:
    """Every call carries the configured User-Agent."""
    transport = FakeTransport()
    transport.set("/version", _ok([]))
    ModrinthProvider(transport, user_agent="minecraft-upgrade/test").query_versions("create", "1.21.1", "forge")
    assert transport.calls[0]["headers"]["User-Agent"] == "minecraft-upgrade/test"


def test_query_versions_url_encodes_filters() -> None:
    """game_versions and loaders are JSON-arrays encoded into the query."""
    transport = FakeTransport()
    transport.set("/version", _ok([]))
    ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")
    url = transport.calls[0]["url"]
    # The JSON array is percent-encoded; decode to inspect.
    assert unquote(url).find('game_versions=["1.21.1"]') >= 0
    assert unquote(url).find('loaders=["forge"]') >= 0


def test_query_versions_404_returns_empty() -> None:
    """A 404 on versions is treated as 'project does not exist', not an error."""
    transport = FakeTransport()
    transport.set("/version", HttpResult(status_code=404, content=b"not found"))
    assert ModrinthProvider(transport).query_versions("missing", "1.21.1", "forge") == []


def test_query_versions_500_raises() -> None:
    """A non-404 non-200 status raises ProviderError."""
    transport = FakeTransport()
    transport.set("/version", HttpResult(status_code=500, content=b"oops"))
    with pytest.raises(ProviderError):
        ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")


def test_query_versions_bad_json_raises() -> None:
    """Malformed JSON raises ProviderError."""
    transport = FakeTransport()
    transport.set("/version", HttpResult(status_code=200, content=b"not json"))
    with pytest.raises(ProviderError, match="malformed JSON"):
        ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")


def test_query_versions_non_list_body_raises() -> None:
    """A non-list body raises ProviderError."""
    transport = FakeTransport()
    transport.set("/version", _ok({"not": "a list"}))
    with pytest.raises(ProviderError, match="non-list"):
        ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")


# ---------------------------------------------------------------------------
# Per-version parsing
# ---------------------------------------------------------------------------


def test_release_type_beta_maps_to_beta_channel() -> None:
    """version_type 'beta' maps to release_channel 'beta'."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry(version_type="beta")]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.release_channel == "beta"


def test_release_type_unknown_maps_to_unknown_channel() -> None:
    """An unrecognised version_type falls back to 'unknown'."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry(version_type="experimental")]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.release_channel == "unknown"


def test_unknown_hash_algorithm_is_dropped() -> None:
    """Only known hash algorithms survive into ProviderVersion.hashes."""
    entry = _version_entry(hashes={"sha512": "a" * 128, "sha3-512": "b" * 128, "sha1": "c" * 40})
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert set(v.hashes) == {"sha512", "sha1"}


def test_missing_download_url_becomes_none() -> None:
    """A file with a null url yields download_url=None."""
    entry = _version_entry(url=None)
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.download_url is None


def test_filename_path_traversal_rejected() -> None:
    """A path-traversal filename drops the version."""
    entry = _version_entry(filename="../../etc/passwd")
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    assert ModrinthProvider(transport).query_versions("create", "1.21.1", "forge") == []


def test_no_primary_file_falls_back_to_first_jar() -> None:
    """When no file is marked primary, the first .jar is chosen."""
    entry = {
        "id": "AABB",
        "version_type": "release",
        "game_versions": ["1.21.1"],
        "loaders": ["forge"],
        "files": [
            {"primary": False, "filename": "src.zip", "url": "https://x/src.zip", "hashes": {}},
            {"primary": False, "filename": "mod.jar", "url": "https://x/mod.jar", "hashes": {"sha1": "a" * 40}},
        ],
        "dependencies": [],
    }
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.filename == "mod.jar"


def test_no_jar_file_drops_version() -> None:
    """A version with no .jar file is dropped with a WARN."""
    entry = {
        "id": "AABB",
        "version_type": "release",
        "game_versions": ["1.21.1"],
        "loaders": ["forge"],
        "files": [{"primary": False, "filename": "src.zip", "url": "https://x/src.zip", "hashes": {}}],
        "dependencies": [],
    }
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    assert ModrinthProvider(transport).query_versions("create", "1.21.1", "forge") == []


def test_dependencies_parsed() -> None:
    """Dependency edges map into ProviderDependency with project_id set."""
    entry = _version_entry(deps=[{"project_id": "flywheel", "dependency_type": "required", "version_id": "ABC"}])
    transport = FakeTransport()
    transport.set("/version", _ok([entry]))
    transport.set("/project/create", _ok({}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert len(v.dependencies) == 1
    dep = v.dependencies[0]
    assert dep.project_id == "flywheel"
    assert dep.mod_id is None
    assert dep.version_id == "ABC"
    assert dep.dependency_type == "required"


# ---------------------------------------------------------------------------
# Side hints
# ---------------------------------------------------------------------------


def test_side_hints_from_project_endpoint() -> None:
    """The project endpoint's client_side / server_side populate every version."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry()]))
    transport.set("/project/create", _ok({"client_side": "required", "server_side": "optional"}))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.client_side == "required"
    assert v.server_side == "optional"


def test_side_hints_missing_when_project_404() -> None:
    """A 404 on the project endpoint yields versions with no side hints."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry()]))
    transport.set("/project/create", HttpResult(status_code=404, content=b""))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.client_side is None
    assert v.server_side is None


def test_side_hints_missing_when_project_bad_json() -> None:
    """A malformed project response yields versions with no side hints."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry()]))
    transport.set("/project/create", HttpResult(status_code=200, content=b"not json"))
    v = ModrinthProvider(transport).query_versions("create", "1.21.1", "forge")[0]
    assert v.client_side is None
    assert v.server_side is None


# ---------------------------------------------------------------------------
# fetch_artifact
# ---------------------------------------------------------------------------


def test_fetch_artifact_verifies_hashes() -> None:
    """A matching declared hash returns the bytes unchanged."""
    payload = b"artifact"
    sha1 = hashlib.sha1(payload).hexdigest()
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry(hashes={"sha1": sha1})]))
    transport.set("/project/create", _ok({}))
    transport.set("https://cdn.modrinth.com", HttpResult(status_code=200, content=payload))
    provider = ModrinthProvider(transport)
    v = provider.query_versions("create", "1.21.1", "forge")[0]
    assert provider.fetch_artifact(v) == payload


def test_fetch_artifact_hash_mismatch_raises() -> None:
    """A mismatched declared hash raises ProviderError."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry(hashes={"sha1": "0" * 40})]))
    transport.set("/project/create", _ok({}))
    transport.set("https://cdn.modrinth.com", HttpResult(status_code=200, content=b"artifact"))
    provider = ModrinthProvider(transport)
    v = provider.query_versions("create", "1.21.1", "forge")[0]
    with pytest.raises(ProviderError, match="hash mismatch"):
        provider.fetch_artifact(v)


def test_fetch_artifact_no_url_raises() -> None:
    """A version with no download_url cannot be fetched."""
    transport = FakeTransport()
    transport.set("/version", _ok([_version_entry(url=None)]))
    transport.set("/project/create", _ok({}))
    provider = ModrinthProvider(transport)
    v = provider.query_versions("create", "1.21.1", "forge")[0]
    with pytest.raises(ProviderError, match="no download URL"):
        provider.fetch_artifact(v)
