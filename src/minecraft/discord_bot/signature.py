# src/minecraft/discord_bot/signature.py

"""Discord Ed25519 request signature verification (Slice 15 T3a).

Discord signs every inbound HTTP interaction request with its Ed25519
private key. The bot's HTTP endpoint must verify the signature before
any handler runs; an unsigned or invalidly-signed request must be
rejected with HTTP 401.

Verification contract
---------------------
The signed message is the concatenation ``timestamp_bytes + body_bytes``:

- ``timestamp`` is the value of the ``X-Signature-Timestamp`` header
  (ASCII decimal epoch, e.g. ``"1700000000"``).
- ``body`` is the raw HTTP request body as received on the wire.

The ``X-Signature-Ed25519`` header carries the 64-byte signature,
hex-encoded (128 lowercase hex characters).

The bot's Ed25519 public key is issued by Discord on the application
page and is the same key configured in ``[discord].public_key``.

Failure semantics
-----------------
:func:`verify_discord_signature` returns ``False`` for any
invalid-signature condition on the request:

- Non-hex signature header.
- Signature hex that decodes to something other than 64 bytes.
- Signature that does not verify under the configured public key.

It raises :class:`DiscordBotSignatureError` only for conditions that
are operator-setup bugs, not request-tampering evidence:

- The configured public key is not 64 hex characters.
- The configured public key hex decodes to a non-32-byte value.

A malformed configuration is a bug, not a security event; the caller
wants the exception so it can log the specific misconfiguration rather
than emitting a generic 401 that hides the real cause.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import DiscordBotSignatureError

__all__ = ["verify_discord_signature"]

_SIGNATURE_BYTES = 64
_PUBLIC_KEY_BYTES = 32


def verify_discord_signature(
    *,
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> bool:
    """Return ``True`` iff ``signature_hex`` validates ``timestamp + body``.

    Parameters:

    - ``public_key_hex``: The bot's Ed25519 public key, 64 lowercase
      hex chars. Malformed values raise; the caller is not expected to
      recover from a misconfigured key.
    - ``signature_hex``: The value of the ``X-Signature-Ed25519`` header.
      Malformed values return ``False``; this is the request path, not
      the config path.
    - ``timestamp``: The value of the ``X-Signature-Timestamp`` header,
      verbatim as ASCII string.
    - ``body``: The raw HTTP body bytes as received.

    Raises :class:`DiscordBotSignatureError` on any malformed
    ``public_key_hex``. Returns ``False`` on any malformed
    ``signature_hex`` or any cryptographic verification failure.
    """
    public_key_bytes = _decode_public_key(public_key_hex)
    signature_bytes = _decode_signature(signature_hex)
    if signature_bytes is None:
        return False

    message = timestamp.encode("utf-8") + body
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        public_key.verify(signature_bytes, message)
        return True
    except InvalidSignature:
        return False


def _decode_public_key(public_key_hex: str) -> bytes:
    """Decode and validate a hex-encoded Ed25519 public key.

    Raises :class:`DiscordBotSignatureError` if the input is not 64 hex
    characters or does not decode to a 32-byte value.
    """
    if len(public_key_hex) != _PUBLIC_KEY_BYTES * 2:
        raise DiscordBotSignatureError(f"public_key_hex must be {_PUBLIC_KEY_BYTES * 2} hex characters, got {len(public_key_hex)}")
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise DiscordBotSignatureError(f"public_key_hex is not valid hex: {exc}") from exc
    if len(public_key_bytes) != _PUBLIC_KEY_BYTES:
        raise DiscordBotSignatureError(f"public_key_hex decoded to {len(public_key_bytes)} bytes, expected {_PUBLIC_KEY_BYTES}")
    return public_key_bytes


def _decode_signature(signature_hex: str) -> bytes | None:
    """Decode a hex-encoded Ed25519 signature.

    Returns the 64-byte signature, or ``None`` if ``signature_hex`` is
    not valid hex or does not decode to exactly 64 bytes.
    """
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return None
    if len(signature_bytes) != _SIGNATURE_BYTES:
        return None
    return signature_bytes
