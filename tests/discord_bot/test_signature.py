# tests/discord_bot/test_signature.py

"""Ed25519 signature verification tests (T3a)."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from minecraft.discord_bot.errors import DiscordBotSignatureError
from minecraft.discord_bot.signature import verify_discord_signature


def _keypair() -> tuple[str, Ed25519PrivateKey]:
    """Return ``(public_key_hex, private_key)`` for a fresh Ed25519 key.

    The private key is returned as an object for signing test payloads.
    """
    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (public_bytes.hex(), private)


def _sign(private_key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    """Sign ``timestamp + body`` and return the hex signature."""
    return private_key.sign(timestamp.encode("utf-8") + body).hex()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_signature_returns_true() -> None:
    """A correctly-signed request verifies."""
    public_hex, private = _keypair()
    body = b'{"type": 1}'
    timestamp = "1700000000"
    signature = _sign(private, timestamp, body)
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=signature,
            timestamp=timestamp,
            body=body,
        )
        is True
    )


def test_empty_body_still_verifies() -> None:
    """An empty body is a valid signed payload."""
    public_hex, private = _keypair()
    timestamp = "1700000000"
    signature = _sign(private, timestamp, b"")
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=signature,
            timestamp=timestamp,
            body=b"",
        )
        is True
    )


# ---------------------------------------------------------------------------
# Signature failures -- return False
# ---------------------------------------------------------------------------


def test_tampered_body_returns_false() -> None:
    """Changing the body after signing yields False."""
    public_hex, private = _keypair()
    timestamp = "1700000000"
    signature = _sign(private, timestamp, b'{"type": 1}')
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=signature,
            timestamp=timestamp,
            body=b'{"type": 2}',
        )
        is False
    )


def test_tampered_timestamp_returns_false() -> None:
    """Changing the timestamp after signing yields False."""
    public_hex, private = _keypair()
    signature = _sign(private, "1700000000", b"body")
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=signature,
            timestamp="1700000001",
            body=b"body",
        )
        is False
    )


def test_wrong_signature_returns_false() -> None:
    """A signature from a different key yields False."""
    public_hex, _private_a = _keypair()
    _public_b, private_b = _keypair()
    signature = _sign(private_b, "1700000000", b"body")
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=signature,
            timestamp="1700000000",
            body=b"body",
        )
        is False
    )


def test_signature_not_hex_returns_false() -> None:
    """A non-hex signature header yields False."""
    public_hex, _private = _keypair()
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex="not-hex",
            timestamp="1700000000",
            body=b"body",
        )
        is False
    )


def test_signature_wrong_length_returns_false() -> None:
    """A signature that decodes to 32 bytes (not 64) yields False."""
    public_hex, _private = _keypair()
    short_hex = "00" * 32
    assert (
        verify_discord_signature(
            public_key_hex=public_hex,
            signature_hex=short_hex,
            timestamp="1700000000",
            body=b"body",
        )
        is False
    )


# ---------------------------------------------------------------------------
# Config failures -- raise
# ---------------------------------------------------------------------------


def test_public_key_wrong_length_raises() -> None:
    """A public_key_hex that is not 64 chars raises."""
    with pytest.raises(DiscordBotSignatureError, match="64 hex characters"):
        verify_discord_signature(
            public_key_hex="abc",
            signature_hex="00" * 64,
            timestamp="1700000000",
            body=b"body",
        )


def test_public_key_not_hex_raises() -> None:
    """A public_key_hex with non-hex characters raises."""
    with pytest.raises(DiscordBotSignatureError, match="not valid hex"):
        verify_discord_signature(
            public_key_hex="z" * 64,
            signature_hex="00" * 64,
            timestamp="1700000000",
            body=b"body",
        )


def test_public_key_all_zeros_returns_false() -> None:
    """A syntactically valid 32-byte all-zero public key parses but never verifies.

    ``cryptography``'s :meth:`Ed25519PublicKey.from_public_bytes` accepts
    any 32-byte input without validating that it is a valid curve point,
    so a hex string of 64 ``"0"`` characters decodes to a syntactically
    valid key. Verification against any signature therefore runs to the
    underlying ``verify`` call and returns ``False`` (bad signature),
    not :class:`DiscordBotSignatureError`. That is the correct
    distinction: a bad request signature is a rejection, not a config
    error. Only ``public_key_hex`` that fails the length or hex-decode
    checks is treated as an operator-setup bug.
    """
    with_zeros = "00" * 32
    assert len(with_zeros) == 64
    assert (
        verify_discord_signature(
            public_key_hex=with_zeros,
            signature_hex="00" * 64,
            timestamp="1700000000",
            body=b"body",
        )
        is False
    )
