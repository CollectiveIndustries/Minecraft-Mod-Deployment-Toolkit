# src/minecraft/modpack_upgrade/signing.py

"""Ed25519 signing and verification for release manifests and registry entries.

Upgrade_System_specifications Section 6.4 (signed manifest) and Section 7.5
(key management). Replaces the ``"signature": "UNSIGNED"`` and
``"key_id": "UNSIGNED"`` placeholders that publish wrote in Slice 10.

Three concerns, kept separate:

  * **Key loading** -- from the DEV signing key env var (release-time) and
    from a pinned trusted-keys TOML (verify-time). Both raise
    :class:`SigningError` on config problems.
  * **Signing** -- Ed25519 over a caller-supplied payload. Raises
    :class:`PublicationError` on crypto-operation failures.
  * **Verification** -- returns ``bool`` for "signature validates against
    key + payload". A ``False`` is a legitimate answer, not an error;
    callers decide whether "untrusted" is a hard stop (chain walk) or a
    soft signal (self-check).

Key ID derivation
-----------------

``key_id`` is ``sha256(raw_ed25519_public_key_bytes).hexdigest()``.
Content-addressed, not a name. Two consequences:

  1. Signing needs only the private key; the key ID falls out
     automatically.
  2. Verification looks up the public key by content-hash, so an
     attacker cannot substitute a different key under the same
     ``key_id`` unless they also control the trusted-keys file (a
     separate trust event).

Trade-off: key rotation always changes ``key_id``. Spec Section 7.5
already requires this ("Key rotation requires a new ``key_id``"), so the
two designs agree.

Env-var convention
------------------

``RELEASE_SIGNING_KEY`` (name configurable via
``[publication].signing_key_env`` in ``config.d/upgrade.toml``) may
contain either:

  * A filesystem path to a PEM-encoded Ed25519 private key.
  * Inline PEM text (value starts with ``-----BEGIN``).

Detection is by prefix; there is no ambiguity because paths cannot
legally start with five hyphens.

Trusted-keys file format
------------------------

``trusted_keys.toml`` (path configurable via
``[registry].trusted_keys_path``):

    [keys]
    "<sha256-of-raw-pubkey>" = \"\"\"-----BEGIN PUBLIC KEY-----
    MCowBQYDK2VwAyEA...
    -----END PUBLIC KEY-----
    \"\"\"

The loader validates every value parses as an Ed25519 public key AND
that its derived ``key_id`` matches the table key. A mismatch (PEM
content doesn't match its name) is refused outright -- exactly the
trust-substitution attack the content-addressing scheme is designed to
prevent.

Bytes only
----------

This module operates on raw bytes. Encoding to base64 for JSON
serialisation (registry entries) or to a ``.sig`` sidecar file is the
caller's job, keeping the signing primitive independent of any storage
format.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from LoggingCore import get_logger

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError("cryptography is required by minecraft.modpack_upgrade.signing; install it with `pdm add cryptography`") from exc

from . import canonical
from .errors import PublicationError, SigningError

__all__ = [
    "SigningKey",
    "TrustedKeys",
    "derive_key_id",
    "load_signing_key",
    "load_trusted_keys",
    "sign_bytes",
    "sign_json",
    "verify_bytes",
    "verify_json",
    "verify_with_trusted_keys",
]

_log = get_logger(__name__)

_PEM_BEGIN = "-----BEGIN"
_SIGNATURE_LEN = 64
_KEY_ID_RE = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """A loaded signing key with its derived ``key_id``.

    Construct via :meth:`from_private_key` (or :func:`load_signing_key`).
    Direct construction is possible but the fields must be mutually
    consistent; the classmethod enforces that.
    """

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    key_id: str

    @classmethod
    def from_private_key(cls, private_key: Ed25519PrivateKey) -> SigningKey:
        """Build a :class:`SigningKey` from a private key, deriving the rest."""
        public_key = private_key.public_key()
        return cls(
            private_key=private_key,
            public_key=public_key,
            key_id=derive_key_id(public_key),
        )


@dataclass(frozen=True)
class TrustedKeys:
    """A set of pinned public keys, indexed by content-hash key ID.

    Constructed by :func:`load_trusted_keys`. Immutable: callers receive
    a read-only mapping; keys cannot be added at runtime.
    """

    by_id: Mapping[str, Ed25519PublicKey]


# ---------------------------------------------------------------------------
# Key ID derivation
# ---------------------------------------------------------------------------


def derive_key_id(public_key: Ed25519PublicKey) -> str:
    """Return the content-addressed key ID for ``public_key``.

    ``sha256(raw_ed25519_pubkey_bytes).hexdigest()``. The raw form is the
    32-byte Ed25519 point; PEM encoding is deliberately excluded so that
    re-serialising the same key with different whitespace or headers
    produces the same ID.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Loading the signing key
# ---------------------------------------------------------------------------


def _load_pem_private_key(pem_bytes: bytes) -> Ed25519PrivateKey:
    """Parse PEM bytes as an Ed25519 private key; raise SigningError on failure."""
    try:
        loaded = serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError) as exc:
        raise SigningError(f"signing key PEM did not parse as a private key: {exc}") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise SigningError(f"signing key must be Ed25519; loaded PEM contains {type(loaded).__name__}")
    return loaded


def _resolve_env_value_to_pem_bytes(value: str, env_name: str) -> bytes:
    """Return PEM bytes from either an inline PEM value or a filesystem path.

    Detection: a valid PEM always starts with ``-----BEGIN``; the env var
    either contains that literal prefix (inline) or a path (which is
    read). No ambiguity -- paths cannot legally start with five hyphens
    in any real filesystem layout.
    """
    if value.startswith(_PEM_BEGIN):
        return value.encode("utf-8")
    path = Path(value)
    if not path.is_file():
        raise SigningError(f"signing key path from env var {env_name} does not exist: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SigningError(f"could not read signing key from {path}: {exc}") from exc


def load_signing_key(
    env_name: str = "RELEASE_SIGNING_KEY",
    *,
    env: Mapping[str, str] | None = None,
) -> SigningKey:
    """Load the DEV signing key from the environment.

    ``env_name`` is the env-var name (per
    ``[publication].signing_key_env`` in ``config.d/upgrade.toml``). The
    env-var value must be either a filesystem path to a PEM-encoded
    Ed25519 private key or inline PEM text.

    Raises :class:`SigningError` when:

      * the env var is unset or empty;
      * the referenced path does not exist or cannot be read;
      * the PEM does not parse;
      * the parsed key is not Ed25519.
    """
    effective_env = env if env is not None else os.environ
    value = effective_env.get(env_name, "").strip()
    if not value:
        raise SigningError(f"signing key env var {env_name} is unset or empty; set it to a PEM path or inline PEM text before publishing")
    pem_bytes = _resolve_env_value_to_pem_bytes(value, env_name)
    private_key = _load_pem_private_key(pem_bytes)
    signing_key = SigningKey.from_private_key(private_key)
    _log.debug(f"signing: loaded Ed25519 key from env var {env_name} (key_id={signing_key.key_id[:12]}...)")
    return signing_key


# ---------------------------------------------------------------------------
# Loading the trusted keys
# ---------------------------------------------------------------------------


def _load_pem_public_key(pem_bytes: bytes, *, key_id: str) -> Ed25519PublicKey:
    """Parse PEM bytes as an Ed25519 public key; raise SigningError on failure.

    Also validates that the key ID matches the table key under which the
    PEM was found. A mismatch is the trust-substitution attack this
    scheme is designed to prevent -- the operator recorded a specific
    hash, so a PEM that hashes to something else is refused, not
    silently accepted under the wrong name.
    """
    try:
        loaded = serialization.load_pem_public_key(pem_bytes)
    except (ValueError, TypeError) as exc:
        raise SigningError(f"trusted key {key_id[:12]}...: PEM did not parse as a public key: {exc}") from exc
    if not isinstance(loaded, Ed25519PublicKey):
        raise SigningError(f"trusted key {key_id[:12]}...: must be Ed25519; loaded PEM contains {type(loaded).__name__}")
    derived = derive_key_id(loaded)
    if derived != key_id:
        raise SigningError(
            f"trusted key {key_id[:12]}...: content-hash mismatch "
            f"(file says {key_id}, PEM hashes to {derived}). The trust "
            f"file is internally inconsistent; refusing to load."
        )
    return loaded


def load_trusted_keys(path: Path) -> TrustedKeys:
    """Load the pinned trusted public keys from a TOML file.

    File format::

        [keys]
        "<sha256-of-raw-pubkey>" = \"\"\"-----BEGIN PUBLIC KEY-----
        ...
        -----END PUBLIC KEY-----
        \"\"\"

    Raises :class:`SigningError` when:

      * the file is missing or cannot be read;
      * the file is not valid TOML;
      * the ``[keys]`` table is missing or empty;
      * any key_id is not a 64-char lowercase hex string;
      * any PEM does not parse, is not Ed25519, or its content-hash does
        not match its table key.
    """
    if not path.is_file():
        raise SigningError(f"trusted keys file not found: {path}")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise SigningError(f"could not read trusted keys from {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SigningError(f"trusted keys file {path} is not valid TOML: {exc}") from exc

    keys_section = data.get("keys")
    if not isinstance(keys_section, dict):
        raise SigningError(f"trusted keys file {path}: missing [keys] table")
    if not keys_section:
        raise SigningError(f"trusted keys file {path}: [keys] table is empty; at least one public key must be pinned")

    by_id: dict[str, Ed25519PublicKey] = {}
    for key_id, pem_value in keys_section.items():
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise SigningError(f"trusted keys file {path}: {key_id!r} is not a 64-char lowercase hex SHA-256")
        if not isinstance(pem_value, str):
            raise SigningError(f"trusted keys file {path}: key {key_id[:12]}... value must be a PEM string, got {type(pem_value).__name__}")
        by_id[key_id] = _load_pem_public_key(pem_value.encode("utf-8"), key_id=key_id)

    _log.debug(f"signing: loaded {len(by_id)} trusted key(s) from {path}")
    return TrustedKeys(by_id=MappingProxyType(by_id))


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_bytes(signing_key: SigningKey, payload: bytes) -> bytes:
    """Return the 64-byte Ed25519 signature over ``payload``.

    Deterministic: same key + payload yields the same signature, per
    Ed25519's design (no per-message randomness at sign time).
    """
    try:
        return signing_key.private_key.sign(payload)
    except Exception as exc:  # pragma: no cover - defensive; valid key shouldn't fail
        raise PublicationError(f"Ed25519 signing failed unexpectedly: {exc}") from exc


def sign_json(signing_key: SigningKey, obj: object) -> bytes:
    """Canonicalize ``obj`` per RFC 8785, then sign the canonical bytes.

    Convenience wrapper around :func:`sign_bytes` and
    :func:`canonical.serialize`. The object must be canonical-serializable
    (strings, bools, safe ints, lists, string-keyed dicts); floats and
    ``None`` are rejected by :mod:`canonical` and will raise
    :class:`TypeError`.
    """
    canonical_bytes = canonical.serialize(canonical.canonicalize(obj))
    return sign_bytes(signing_key, canonical_bytes)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_bytes(public_key: Ed25519PublicKey, payload: bytes, signature: bytes) -> bool:
    """Return True iff ``signature`` is valid for ``payload`` under ``public_key``.

    Raises :class:`PublicationError` if ``signature`` is not 64 bytes --
    that is a caller bug (wrong data flow), not a verification rejection.
    """
    if len(signature) != _SIGNATURE_LEN:
        raise PublicationError(f"Ed25519 signature must be {_SIGNATURE_LEN} bytes, got {len(signature)}")
    try:
        public_key.verify(signature, payload)
        return True
    except InvalidSignature:
        return False


def verify_json(public_key: Ed25519PublicKey, obj: object, signature: bytes) -> bool:
    """Canonicalize ``obj`` then verify ``signature`` against its bytes."""
    canonical_bytes = canonical.serialize(canonical.canonicalize(obj))
    return verify_bytes(public_key, canonical_bytes, signature)


def verify_with_trusted_keys(
    trusted: TrustedKeys,
    key_id: str,
    payload: bytes,
    signature: bytes,
) -> bool:
    """Look up ``key_id`` in ``trusted`` and verify ``signature``.

    Returns ``False`` (not raise) when ``key_id`` is unknown: an unknown
    key is a verification *outcome* ("this release was signed by a key
    the operator has not pinned"), not an exception. The caller decides
    whether that is a hard stop (chain walk) or informational (audit).

    Raises :class:`PublicationError` on wrong-length signature, same as
    :func:`verify_bytes`.
    """
    public_key = trusted.by_id.get(key_id)
    if public_key is None:
        _log.warning(f"verify: key_id {key_id[:12]}... is not in the trusted set ({len(trusted.by_id)} pinned key(s))")
        return False
    return verify_bytes(public_key, payload, signature)
