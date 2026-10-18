# src/minecraft/modpack_upgrade/providers/modrinth.py

"""Modrinth provider adapter (Section 5.3, Section 5.7, Side_Determination Section 4.1).

Reads ``GET /v2/project/{id}/version`` filtered by ``game_versions``
and ``loaders``, plus ``GET /v2/project/{id}`` for the client/server
side hints that Modrinth surfaces at the project level.

Modrinth returns every hash algorithm it has (usually sha512 and
sha1). Only the algorithms in :data:`KNOWN_HASH_ALGORITHMS` survive
into ``ProviderVersion.hashes``; others are logged at DEBUG and
dropped, because Resolution_Report Section 3.1 restricts the slip's
``expected_hashes`` to four algorithms.

Side hints are best-effort: a 404 or malformed response on the project
endpoint yields versions with ``client_side=None`` / ``server_side=None``
and a WARN log; version resolution still succeeds.

The adapter never silently skips a malformed version - it logs WARN
and omits it from the return, so a single bad version does not take
down a whole project's resolution.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote, urlencode

from LoggingCore import get_logger

from minecraft.modpack_upgrade.errors import ProviderError

from .base import (
    KNOWN_HASH_ALGORITHMS,
    HttpTransport,
    Provider,
    ProviderDependency,
    ProviderVersion,
    sanitize_filename,
)

__all__ = ["ModrinthProvider"]

_log = get_logger(__name__)

_MODRINTH_API = "https://api.modrinth.com/v2"
_DEFAULT_USER_AGENT = "minecraft-upgrade/1.3.1"

_RELEASE_TYPE_TO_CHANNEL: dict[str, str] = {
    "release": "release",
    "beta": "beta",
    "alpha": "alpha",
}


def _build_versions_url(project_id: str, game_version: str, loader: str) -> str:
    """Build the versions endpoint URL with URL-encoded filters."""
    query = urlencode(
        {
            "game_versions": json.dumps([game_version]),
            "loaders": json.dumps([loader]),
        }
    )
    return f"{_MODRINTH_API}/project/{quote(project_id, safe='')}/version?{query}"


def _build_project_url(project_id: str) -> str:
    """Build the project metadata URL."""
    return f"{_MODRINTH_API}/project/{quote(project_id, safe='')}"


def _parse_hashes(raw: Any) -> dict[str, str]:
    """Return the known-algorithm hashes from a ``files[i].hashes`` dict."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for algo, value in raw.items():
        algo_lower = str(algo).lower()
        if algo_lower not in KNOWN_HASH_ALGORITHMS:
            _log.debug(f"modrinth: dropping unknown hash algorithm {algo!r}")
            continue
        if isinstance(value, str) and value:
            out[algo_lower] = value.lower()
    return out


def _select_primary_file(files: Any) -> dict[str, Any] | None:
    """Return the primary file dict, or the first ``.jar``, or ``None``."""
    if not isinstance(files, list):
        return None
    for f in files:
        if isinstance(f, dict) and f.get("primary") is True:
            return f
    for f in files:
        if isinstance(f, dict) and str(f.get("filename", "")).endswith(".jar"):
            return f
    return None


def _parse_dependency(raw: Any) -> ProviderDependency | None:
    """Return one parsed dependency edge, or ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    dep_type = raw.get("dependency_type")
    if not isinstance(dep_type, str):
        return None
    return ProviderDependency(
        project_id=(str(raw["project_id"]) if raw.get("project_id") else None),
        mod_id=None,
        version_id=(str(raw["version_id"]) if raw.get("version_id") else None),
        dependency_type=dep_type,
    )


def _parse_version_entry(raw: Any) -> ProviderVersion | None:
    """Parse one version object; return ``None`` if it must be dropped."""
    if not isinstance(raw, dict):
        return None
    version_id = raw.get("id")
    if not isinstance(version_id, str) or not version_id:
        return None

    file_obj = _select_primary_file(raw.get("files"))
    if file_obj is None:
        _log.warning(f"modrinth: version {version_id} has no primary file; skipping")
        return None
    try:
        filename = sanitize_filename(str(file_obj.get("filename", "")))
    except ValueError as exc:
        _log.warning(f"modrinth: version {version_id} filename rejected: {exc}")
        return None

    download_url_raw = file_obj.get("url")
    download_url = str(download_url_raw) if isinstance(download_url_raw, str) and download_url_raw else None

    release_channel = _RELEASE_TYPE_TO_CHANNEL.get(str(raw.get("version_type", "")), "unknown")

    game_versions_raw = raw.get("game_versions")
    game_versions = tuple(str(v) for v in game_versions_raw) if isinstance(game_versions_raw, list) else ()
    loaders_raw = raw.get("loaders")
    loaders = tuple(str(v) for v in loaders_raw) if isinstance(loaders_raw, list) else ()

    deps_raw = raw.get("dependencies")
    dependencies: list[ProviderDependency] = []
    if isinstance(deps_raw, list):
        for d in deps_raw:
            parsed = _parse_dependency(d)
            if parsed is not None:
                dependencies.append(parsed)

    return ProviderVersion(
        version_id=version_id,
        filename=filename,
        download_url=download_url,
        hashes=_parse_hashes(file_obj.get("hashes")),
        game_versions=game_versions,
        loaders=loaders,
        release_channel=release_channel,
        dependencies=tuple(dependencies),
        client_side=None,
        server_side=None,
    )


class ModrinthProvider(Provider):
    """Modrinth adapter.

    ``user_agent`` should identify the operator per Modrinth's API
    terms. The default is a bare tool-and-version string; operators
    are expected to configure a contact via ``[providers].modrinth_user_agent``.
    """

    name = "modrinth"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        """Store the transport and user agent; no network activity yet."""
        self._transport = transport
        self._user_agent = user_agent

    def _headers(self) -> dict[str, str]:
        """Return the headers required on every Modrinth API call."""
        return {"User-Agent": self._user_agent, "Accept": "application/json"}

    def _fetch_project_side(self, project_id: str) -> tuple[str | None, str | None]:
        """Return ``(client_side, server_side)``, or ``(None, None)`` on failure.

        A missing or malformed project response is not fatal - versions
        are still returned. Side determination falls through to
        ``.pw.toml`` / overrides.
        """
        try:
            result = self._transport.get(_build_project_url(project_id), headers=self._headers())
        except ProviderError as exc:
            _log.warning(f"modrinth: project metadata fetch failed for {project_id!r}: {exc}")
            return (None, None)
        if result.status_code != 200:
            _log.warning(f"modrinth: project metadata for {project_id!r} returned HTTP {result.status_code}; side hints unavailable")
            return (None, None)
        try:
            data = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log.warning(f"modrinth: project metadata for {project_id!r} is not valid JSON: {exc}")
            return (None, None)
        if not isinstance(data, dict):
            return (None, None)
        client = data.get("client_side")
        server = data.get("server_side")
        return (
            str(client) if isinstance(client, str) else None,
            str(server) if isinstance(server, str) else None,
        )

    def query_versions(
        self,
        project_id: str,
        game_version: str,
        loader: str,
    ) -> list[ProviderVersion]:
        """Return every compatible Modrinth version for ``project_id``."""
        result = self._transport.get(
            _build_versions_url(project_id, game_version, loader),
            headers=self._headers(),
        )
        if result.status_code == 404:
            _log.info(f"modrinth: project {project_id!r} not found")
            return []
        if result.status_code != 200:
            raise ProviderError(f"modrinth: query for {project_id!r} returned HTTP {result.status_code}: {result.content[:200]!r}")

        try:
            raw = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"modrinth: query for {project_id!r} returned malformed JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ProviderError(f"modrinth: query for {project_id!r} returned a non-list body")

        versions: list[ProviderVersion] = []
        for entry in raw:
            parsed = _parse_version_entry(entry)
            if parsed is not None:
                versions.append(parsed)
        if not versions:
            return versions

        client_side, server_side = self._fetch_project_side(project_id)
        if client_side is not None or server_side is not None:
            versions = [
                ProviderVersion(
                    version_id=v.version_id,
                    filename=v.filename,
                    download_url=v.download_url,
                    hashes=v.hashes,
                    game_versions=v.game_versions,
                    loaders=v.loaders,
                    release_channel=v.release_channel,
                    dependencies=v.dependencies,
                    client_side=client_side,
                    server_side=server_side,
                )
                for v in versions
            ]
        return versions

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        """Download and verify the Modrinth artifact bytes."""
        if version.download_url is None:
            raise ProviderError(f"modrinth: version {version.version_id} has no download URL")
        result = self._transport.get(version.download_url, headers=self._headers())
        if result.status_code != 200:
            raise ProviderError(f"modrinth: artifact download for {version.version_id} returned HTTP {result.status_code}")
        _verify_hashes(version, result.content)
        return result.content


def _verify_hashes(version: ProviderVersion, content: bytes) -> None:
    """Verify every declared hash; raise on any mismatch (Section 3.3)."""
    for algo, expected in version.hashes.items():
        actual = hashlib.new(algo, content).hexdigest()
        if actual.lower() != expected.lower():
            raise ProviderError(f"modrinth: hash mismatch for {version.version_id} ({algo}): expected {expected}, got {actual}")
