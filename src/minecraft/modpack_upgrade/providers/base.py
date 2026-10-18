# src/minecraft/modpack_upgrade/providers/base.py

"""Provider ABC, data model, and the HTTP transport protocol.

The provider layer is a pure I/O boundary. Adapters report what a
provider said; the resolver decides what to install. See
``Upgrade_System_specifications.md`` Section 5.6 (release mode) and
``Resolution_Report.md`` Section 3 for the data model this feeds.

Attacker-controlled fields (``filename``, ``download_url``) are
validated at the boundary per Section 18.3. A provider that returns a path
traversal, NUL byte, or empty filename is rejected, not silently
rewritten - silent rewriting would hide a malicious or buggy provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Protocol, get_args

__all__ = [
    "KNOWN_HASH_ALGORITHMS",
    "DependencyType",
    "HttpResult",
    "HttpTransport",
    "Provider",
    "ProviderDependency",
    "ProviderVersion",
    "ReleaseChannel",
    "sanitize_filename",
    "validate_hash_algorithm",
]


# Per Resolution_Report Section 3.1, algorithm values are exactly these four.
KNOWN_HASH_ALGORITHMS: frozenset[str] = frozenset({"sha512", "sha256", "sha1", "md5"})

# Per Section 5.3 (Amendments A1.3) the type enum is exactly these six values.
DependencyType = Literal["required", "optional", "incompatible", "embedded", "tool", "include"]
_DEPENDENCY_TYPES: frozenset[str] = frozenset(get_args(DependencyType))

# The provider's declared release channel. ``unknown`` is a fallback for
# providers that return a value not in the documented set (new channels
# would appear here first).
ReleaseChannel = Literal["release", "beta", "alpha", "unknown"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResult:
    """One HTTP response, reduced to what adapters need.

    ``content`` is the full body as bytes. ``headers`` holds lowercased
    header names for case-insensitive lookup; ``Content-Length`` is the
    field adapters rely on and it is preserved verbatim under
    ``"content-length"`` when the server sent it.
    """

    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    """Minimal HTTP GET interface used by provider adapters.

    Implementations MUST:
      * follow redirects;
      * raise :class:`ProviderError` on network failure, timeout, or
        ``max_bytes`` overflow;
      * return :class:`HttpResult` for any HTTP response (including
        4xx and 5xx) so the caller decides whether the status is
        retriable.
    """

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_bytes: int | None = None,
    ) -> HttpResult:
        """Perform a GET and return the result.

        ``max_bytes=None`` disables the size guard.
        """
        ...


# ---------------------------------------------------------------------------
# Provider data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDependency:
    """One dependency edge as declared by a provider.

    ``project_id`` and ``mod_id`` are the two namespaces from Section 5.3.
    Modrinth reports ``project_id``; CurseForge reports numeric
    ``mod_id``. Exactly one is non-None, mirroring the provider's own
    namespace. ``version_id`` may be present (pinned) or absent (any
    version satisfies).

    CurseForge ``Tool`` / ``Include`` edges surface here with
    ``dependency_type`` set accordingly; they are not written to
    ``.pw.toml`` (Amendments A1.2, Section 5.7).
    """

    project_id: str | None
    mod_id: str | None
    version_id: str | None
    dependency_type: str


@dataclass(frozen=True)
class ProviderVersion:
    """One version of a project that a provider returned.

    ``download_url`` is ``None`` when the provider declines to expose a
    direct download (CurseForge allows authors to disable API downloads;
    such versions require manual retrieval). The resolver treats
    ``download_url=None`` versions as "exists but not installable" and
    reports the situation explicitly.

    ``hashes`` contains only digests whose algorithm is in
    :data:`KNOWN_HASH_ALGORITHMS`; unknown algorithms are dropped by
    the adapter. ``sha256`` is not required - providers rarely declare
    it - but ``materialize`` always computes SHA-256 locally.

    ``client_side`` / ``server_side`` are the provider's raw strings
    (Modrinth only; CurseForge returns ``None`` for both). The
    provisional-side computation in ``update`` interprets them.
    """

    version_id: str
    filename: str
    download_url: str | None
    hashes: dict[str, str]
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    release_channel: str
    dependencies: tuple[ProviderDependency, ...]
    client_side: str | None
    server_side: str | None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def sanitize_filename(filename: str) -> str:
    """Validate and return ``filename``, or raise :class:`ValueError`.

    Per Section 18.3, downloaded filenames MUST be sanitized. This is the
    boundary at which that guarantee is enforced. A provider-supplied
    string is rejected when it could escape the download directory or
    hide bytes from the operator. Rejection - not silent rewriting - is
    correct: a malicious or buggy provider must be reported loudly, not
    silently normalised into something that happens to work.
    """
    if not filename:
        raise ValueError("provider filename must be non-empty")
    if "\x00" in filename:
        raise ValueError(f"provider filename contains NUL byte: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"provider filename contains path separator: {filename!r}")
    if filename in (".", ".."):
        raise ValueError(f"provider filename is {filename!r}")
    if filename.startswith("."):
        raise ValueError(f"provider filename starts with '.': {filename!r}")
    return filename


def validate_hash_algorithm(algorithm: str) -> str:
    """Return ``algorithm`` lowercased, or raise :class:`ValueError`.

    A provider that returns an algorithm outside
    :data:`KNOWN_HASH_ALGORITHMS` has that digest dropped from
    ``ProviderVersion.hashes`` - the adapter filters, not raises. This
    helper exists for tests and for the adapter's own diagnostic logging
    (``"dropped unknown hash algorithm: sha3-512"``).
    """
    algo = algorithm.lower()
    if algo not in KNOWN_HASH_ALGORITHMS:
        raise ValueError(f"unknown hash algorithm: {algorithm!r}")
    return algo


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class Provider(ABC):
    """Abstract base for provider adapters.

    An adapter answers two questions and nothing else:

      1. Which versions of this project are compatible with the target
         runtime? (:meth:`query_versions`)
      2. Give me the bytes of a specific version. (:meth:`fetch_artifact`)

    Neither method decides which version to install. That belongs to
    the resolver's policy layer (Section 5.3 ``selection``). The provider is a
    pure I/O boundary; it can be cached, mocked, or swapped without the
    resolver noticing.
    """

    name: str = ""  # subclasses must override

    @abstractmethod
    def query_versions(
        self,
        project_id: str,
        game_version: str,
        loader: str,
    ) -> list[ProviderVersion]:
        """Return every version of ``project_id`` compatible with the
        given game version and loader.

        The list is unordered; the resolver applies selection policy.

        Raises :class:`ProviderError` on network or API failure. An
        empty list is not an error - it means no version satisfied the
        runtime filter.
        """
        ...

    @abstractmethod
    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        """Download and verify the artifact bytes for ``version``.

        Verifies the provider's declared hashes before returning
        (Section 3.3). Raises :class:`ProviderError` on network failure,
        size-limit overflow, or a hash mismatch. ``version.download_url``
        must be non-None; passing a ``None`` URL raises immediately.
        """
        ...
