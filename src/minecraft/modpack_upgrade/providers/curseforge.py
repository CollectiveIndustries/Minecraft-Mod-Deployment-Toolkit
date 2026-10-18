# src/minecraft/modpack_upgrade/providers/curseforge.py

"""CurseForge provider adapter (Section 5.3, Section 5.7, Side_Determination Section 4.2).

Requires an API key supplied via the ``x-api-key`` header. The key is
read from the environment by :func:`load_provider_config` and stored on
:class:`ProviderConfig`; it is never written to disk, logged, or
included in any artifact.

CurseForge returns numeric codes for loaders, release types, relation
types, and hash algorithms. The adapter translates them via the tables
in :data:`_LOADER_TO_CF` etc. (see Section 5.3 for the relation mapping).

CurseForge has no ``clientSide`` / ``serverSide`` fields
(Side_Determination Section 4.2), so both side hints are always ``None``; the
side determination chain falls through to ``.pw.toml`` / overrides.

``downloadUrl`` may be ``null`` - CurseForge lets authors disable
direct API downloads. Such versions are returned with
``download_url=None``; the resolver treats them as "exists but not
installable" and reports the situation explicitly, rather than
dropping them silently.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlencode

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

__all__ = ["CurseForgeProvider"]

_log = get_logger(__name__)

_CURSEFORGE_API = "https://api.curseforge.com/v1"

_LOADER_TO_CF: dict[str, int] = {
    "forge": 1,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}

_RELEASE_TYPE_TO_CHANNEL: dict[int, str] = {
    1: "release",
    2: "beta",
    3: "alpha",
}

_RELATION_TYPE_TO_DEP: dict[int, str] = {
    1: "embedded",
    2: "optional",
    3: "required",
    4: "tool",
    5: "incompatible",
    6: "include",
}

_HASH_ALGO_TO_NAME: dict[int, str] = {
    1: "sha1",
    2: "md5",
}

_PAGE_SIZE = 50
_MAX_PAGES = 5


def _build_files_url(mod_id: int, game_version: str, cf_loader: int) -> str:
    """Build the files endpoint URL for one page."""
    query = urlencode(
        {
            "gameVersion": game_version,
            "modLoaderType": cf_loader,
            "pageSize": _PAGE_SIZE,
        }
    )
    return f"{_CURSEFORGE_API}/mods/{mod_id}/files?{query}"


def _parse_hashes(raw: Any) -> dict[str, str]:
    """Return the known-algorithm hashes from a ``hashes`` array."""
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        algo_code = entry.get("algo")
        value = entry.get("value")
        if not isinstance(value, str) or not value:
            continue
        algo = _HASH_ALGO_TO_NAME.get(algo_code) if isinstance(algo_code, int) else None
        if algo is None or algo not in KNOWN_HASH_ALGORITHMS:
            _log.debug(f"curseforge: dropping unknown hash algorithm code {algo_code!r}")
            continue
        out[algo] = value.lower()
    return out


def _parse_dependency(raw: Any) -> ProviderDependency | None:
    """Return one parsed dependency edge, or ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    rel = raw.get("relationType")
    if not isinstance(rel, int):
        return None
    dep_type = _RELATION_TYPE_TO_DEP.get(rel)
    if dep_type is None:
        _log.debug(f"curseforge: unknown relationType {rel!r}; dropping dependency")
        return None
    mod_id = raw.get("modId")
    return ProviderDependency(
        project_id=None,
        mod_id=(str(mod_id) if isinstance(mod_id, int) else None),
        version_id=None,
        dependency_type=dep_type,
    )


def _parse_version_entry(raw: Any) -> ProviderVersion | None:
    """Parse one CurseForge file object; return ``None`` if it must be dropped."""
    if not isinstance(raw, dict):
        return None
    file_id = raw.get("id")
    if not isinstance(file_id, int):
        return None
    try:
        filename = sanitize_filename(str(raw.get("fileName", "")))
    except ValueError as exc:
        _log.warning(f"curseforge: file {file_id} filename rejected: {exc}")
        return None

    url_raw = raw.get("downloadUrl")
    download_url = str(url_raw) if isinstance(url_raw, str) and url_raw else None

    release_type = raw.get("releaseType")
    release_channel = _RELEASE_TYPE_TO_CHANNEL.get(release_type, "unknown") if isinstance(release_type, int) else "unknown"

    gv_raw = raw.get("gameVersions")
    game_versions = tuple(str(v) for v in gv_raw) if isinstance(gv_raw, list) else ()
    # CurseForge mixes game versions ("1.20.1") and loader names ("Forge")
    # in one array; the loader field is not separately exposed. Report the
    # array verbatim; the resolver filters by game version, and loader
    # compatibility is already enforced by the API filter.
    loaders: tuple[str, ...] = ()

    deps_raw = raw.get("dependencies")
    dependencies: list[ProviderDependency] = []
    if isinstance(deps_raw, list):
        for d in deps_raw:
            parsed = _parse_dependency(d)
            if parsed is not None:
                dependencies.append(parsed)

    return ProviderVersion(
        version_id=str(file_id),
        filename=filename,
        download_url=download_url,
        hashes=_parse_hashes(raw.get("hashes")),
        game_versions=game_versions,
        loaders=loaders,
        release_channel=release_channel,
        dependencies=tuple(dependencies),
        client_side=None,
        server_side=None,
    )


class CurseForgeProvider(Provider):
    """CurseForge adapter.

    Construction is lenient: ``api_key=None`` is allowed so a
    Modrinth-only workflow does not fail when CurseForge is unused.
    Every network method raises :class:`ProviderError` immediately if
    the key is missing.
    """

    name = "curseforge"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_key: str | None = None,
    ) -> None:
        """Store the transport and optional API key."""
        self._transport = transport
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        """Return the headers required by the CurseForge API."""
        if not self._api_key:
            raise ProviderError("curseforge: no API key configured (set [providers].curseforge_api_key_env and provide the env var)")
        return {"x-api-key": self._api_key, "Accept": "application/json"}

    def _query_one_page(self, mod_id: int, game_version: str, cf_loader: int) -> list[dict[str, Any]]:
        """Return one page of the files endpoint."""
        result = self._transport.get(_build_files_url(mod_id, game_version, cf_loader), headers=self._headers())
        if result.status_code == 404:
            _log.info(f"curseforge: mod {mod_id} not found")
            return []
        if result.status_code in (401, 403):
            raise ProviderError(f"curseforge: HTTP {result.status_code} from files endpoint - check the API key")
        if result.status_code != 200:
            raise ProviderError(f"curseforge: query for mod {mod_id} returned HTTP {result.status_code}: {result.content[:200]!r}")
        try:
            body = json.loads(result.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"curseforge: query for mod {mod_id} returned malformed JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ProviderError(f"curseforge: query for mod {mod_id} returned a non-object body")
        data = body.get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ProviderError(f"curseforge: query for mod {mod_id} returned a non-list 'data' field")
        return data

    def query_versions(
        self,
        project_id: str,
        game_version: str,
        loader: str,
    ) -> list[ProviderVersion]:
        """Return every compatible CurseForge file for ``project_id``.

        ``project_id`` is the numeric CurseForge mod ID as a string.
        """
        try:
            mod_id = int(project_id)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"curseforge: project_id {project_id!r} is not a numeric mod ID") from exc

        cf_loader = _LOADER_TO_CF.get(loader)
        if cf_loader is None:
            raise ProviderError(f"curseforge: loader {loader!r} is not supported (expected one of {sorted(_LOADER_TO_CF)})")

        versions: list[ProviderVersion] = []
        for _page in range(_MAX_PAGES):
            data = self._query_one_page(mod_id, game_version, cf_loader)
            if not data:
                break
            for entry in data:
                parsed = _parse_version_entry(entry)
                if parsed is not None:
                    versions.append(parsed)
            if len(data) < _PAGE_SIZE:
                break
        return versions

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        """Download and verify the CurseForge artifact bytes."""
        if version.download_url is None:
            raise ProviderError(f"curseforge: version {version.version_id} has no download URL (author disabled API downloads)")
        result = self._transport.get(version.download_url)
        if result.status_code != 200:
            raise ProviderError(f"curseforge: artifact download for {version.version_id} returned HTTP {result.status_code}")
        for algo, expected in version.hashes.items():
            actual = hashlib.new(algo, result.content).hexdigest()
            if actual.lower() != expected.lower():
                raise ProviderError(f"curseforge: hash mismatch for {version.version_id} ({algo}): expected {expected}, got {actual}")
        return result.content
