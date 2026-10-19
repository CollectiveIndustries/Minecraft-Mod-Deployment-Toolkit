# tests/modpack_upgrade/test_signing.py

"""Tests for Ed25519 signing and verification (Slice 11 T1).

Covers the three concerns of :mod:`minecraft.modpack_upgrade.signing`:

  * Key loading (env-var path, env-var inline PEM, all failure modes)
  * Trusted-keys loading (single key, multiple keys, all failure modes)
  * Sign/verify primitives (deterministic signing, canonical JSON,
    unknown-key soft rejection, wrong-length hard rejection)

Content-addressing of ``key_id`` is validated end-to-end: a trusted-keys
entry whose PEM does not hash to its table key is refused outright, which
is the trust-substitution attack the scheme exists to prevent.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from minecraft.modpack_upgrade.errors import PublicationError, SigningError
from minecraft.modpack_upgrade.signing import (
    SigningKey,
    derive_key_id,
    load_signing_key,
    load_trusted_keys,
    sign_bytes,
    sign_json,
    verify_bytes,
    verify_json,
    verify_with_trusted_keys,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_key() -> SigningKey:
    """Generate a fresh Ed25519 signing key for a test."""
    return SigningKey.from_private_key(Ed25519PrivateKey.generate())


def _private_pem(key: SigningKey) -> bytes:
    """Serialize the private key to unencrypted PKCS8 PEM bytes."""
    return key.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_pem(key: SigningKey) -> str:
    """Serialize the public key to SubjectPublicKeyInfo PEM text."""
    return key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def _raw_pub_bytes(key: SigningKey) -> bytes:
    """Return the raw 32-byte Ed25519 public-key point."""
    return key.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _write_trusted_keys(path: Path, *keys: SigningKey) -> None:
    """Write a trusted-keys TOML file with the given keys."""
    body = {"keys": {k.key_id: _public_pem(k) for k in keys}}
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))


def _setup_signing_env(tmp_path: Path, key: SigningKey) -> dict[str, str]:
    """Write the private key to a file and return env mapping to that path."""
    pem_path = tmp_path / "signing-key.pem"
    pem_path.write_bytes(_private_pem(key))
    return {"RELEASE_SIGNING_KEY": str(pem_path)}


# ---------------------------------------------------------------------------
# derive_key_id
# ---------------------------------------------------------------------------


def test_derive_key_id_returns_64_lowercase_hex() -> None:
    """``key_id`` is a lowercase 64-char SHA-256 hex string."""
    key = _generate_key()
    kid = derive_key_id(key.public_key)
    assert len(kid) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", kid) is not None


def test_derive_key_id_matches_sha256_of_raw_bytes() -> None:
    """``key_id`` equals sha256 of the raw 32-byte Ed25519 point."""
    key = _generate_key()
    raw = _raw_pub_bytes(key)
    assert len(raw) == 32
    assert derive_key_id(key.public_key) == hashlib.sha256(raw).hexdigest()


def test_derive_key_id_stable_across_calls() -> None:
    """Repeated calls with the same public key yield the same ID."""
    key = _generate_key()
    assert derive_key_id(key.public_key) == derive_key_id(key.public_key)


def test_derive_key_id_independent_of_pem_format() -> None:
    """Two PEM serialisations of the same key yield the same ID."""
    key = _generate_key()
    # Round-trip through PEM and back; the raw key bytes are unchanged.
    pem_bytes = key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    reloaded = serialization.load_pem_public_key(pem_bytes)
    assert derive_key_id(reloaded) == derive_key_id(key.public_key)


def test_derive_key_id_differs_for_different_keys() -> None:
    """Two freshly-generated keys yield different IDs (with overwhelming probability)."""
    a = _generate_key()
    b = _generate_key()
    assert derive_key_id(a.public_key) != derive_key_id(b.public_key)


# ---------------------------------------------------------------------------
# load_signing_key
# ---------------------------------------------------------------------------


def test_load_signing_key_from_path_env(tmp_path: Path) -> None:
    """Env var pointing at a PEM file loads correctly."""
    key = _generate_key()
    env = _setup_signing_env(tmp_path, key)
    loaded = load_signing_key(env=env)
    assert loaded.key_id == key.key_id
    assert _raw_pub_bytes(loaded) == _raw_pub_bytes(key)


def test_load_signing_key_from_inline_pem_env() -> None:
    """Env var containing inline PEM text loads correctly."""
    key = _generate_key()
    env = {"RELEASE_SIGNING_KEY": _private_pem(key).decode("utf-8")}
    loaded = load_signing_key(env=env)
    assert loaded.key_id == key.key_id


def test_load_signing_key_custom_env_name(tmp_path: Path) -> None:
    """A custom env-var name is honoured."""
    key = _generate_key()
    pem_path = tmp_path / "custom.pem"
    pem_path.write_bytes(_private_pem(key))
    loaded = load_signing_key("CUSTOM_SIGNING_KEY", env={"CUSTOM_SIGNING_KEY": str(pem_path)})
    assert loaded.key_id == key.key_id


def test_load_signing_key_env_unset_raises() -> None:
    """Unset env var -> SigningError."""
    with pytest.raises(SigningError, match="is unset or empty"):
        load_signing_key(env={})


def test_load_signing_key_env_empty_raises() -> None:
    """Empty env var -> SigningError."""
    with pytest.raises(SigningError, match="is unset or empty"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": ""})


def test_load_signing_key_env_whitespace_raises() -> None:
    """Whitespace-only env var -> SigningError."""
    with pytest.raises(SigningError, match="is unset or empty"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": "   \n  "})


def test_load_signing_key_path_missing_raises(tmp_path: Path) -> None:
    """Path that does not exist -> SigningError."""
    missing = tmp_path / "no-such-key.pem"
    with pytest.raises(SigningError, match="does not exist"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": str(missing)})


def test_load_signing_key_path_unreadable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` on key-file read -> SigningError."""
    pem_path = tmp_path / "signing-key.pem"
    pem_path.write_bytes(b"placeholder")
    real_read = Path.read_bytes

    def _fail(self: Path) -> bytes:
        if self == pem_path:
            raise OSError("permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _fail)
    with pytest.raises(SigningError, match="could not read signing key"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": str(pem_path)})


def test_load_signing_key_invalid_pem_raises(tmp_path: Path) -> None:
    """Garbage that doesn't parse as PEM -> SigningError."""
    pem_path = tmp_path / "garbage.pem"
    pem_path.write_text("not a pem", encoding="utf-8")
    with pytest.raises(SigningError, match="did not parse"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": str(pem_path)})


def test_load_signing_key_non_ed25519_raises(tmp_path: Path) -> None:
    """A valid PEM containing a non-Ed25519 key -> SigningError."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = tmp_path / "rsa.pem"
    pem_path.write_bytes(pem_bytes)
    with pytest.raises(SigningError, match="must be Ed25519"):
        load_signing_key(env={"RELEASE_SIGNING_KEY": str(pem_path)})


# ---------------------------------------------------------------------------
# load_trusted_keys
# ---------------------------------------------------------------------------


def test_load_trusted_keys_single(tmp_path: Path) -> None:
    """A single pinned key loads and maps to the expected key_id."""
    key = _generate_key()
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, key)
    trusted = load_trusted_keys(path)
    assert key.key_id in trusted.by_id
    assert _raw_pub_bytes_for(trusted.by_id[key.key_id]) == _raw_pub_bytes(key)


def test_load_trusted_keys_multiple(tmp_path: Path) -> None:
    """Multiple pinned keys all load into the mapping."""
    keys = [_generate_key() for _ in range(3)]
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, *keys)
    trusted = load_trusted_keys(path)
    assert len(trusted.by_id) == 3
    for k in keys:
        assert k.key_id in trusted.by_id


def test_load_trusted_keys_missing_file_raises(tmp_path: Path) -> None:
    """Missing trusted-keys file -> SigningError."""
    with pytest.raises(SigningError, match="not found"):
        load_trusted_keys(tmp_path / "no-such.toml")


def test_load_trusted_keys_malformed_toml_raises(tmp_path: Path) -> None:
    """Malformed TOML -> SigningError."""
    path = tmp_path / "trusted.toml"
    path.write_text("not = valid = toml\n", encoding="utf-8")
    with pytest.raises(SigningError, match="not valid TOML"):
        load_trusted_keys(path)


def test_load_trusted_keys_missing_keys_table_raises(tmp_path: Path) -> None:
    """TOML without a ``[keys]`` table -> SigningError."""
    path = tmp_path / "trusted.toml"
    path.write_text('other = "value"\n', encoding="utf-8")
    with pytest.raises(SigningError) as ei:
        load_trusted_keys(path)
    assert "keys" in str(ei.value)


def test_load_trusted_keys_empty_keys_table_raises(tmp_path: Path) -> None:
    """An empty ``[keys]`` table -> SigningError (at least one key must be pinned)."""
    path = tmp_path / "trusted.toml"
    path.write_text("[keys]\n", encoding="utf-8")
    with pytest.raises(SigningError, match="at least one public key must be pinned"):
        load_trusted_keys(path)


def test_load_trusted_keys_invalid_key_id_raises(tmp_path: Path) -> None:
    """A key_id that isn't 64-char hex -> SigningError."""
    key = _generate_key()
    pem = _public_pem(key)
    body = {"keys": {"not-a-valid-hash": pem}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="64-char"):
        load_trusted_keys(path)


def test_load_trusted_keys_non_string_pem_raises(tmp_path: Path) -> None:
    """A non-string PEM value -> SigningError."""
    body = {"keys": {"a" * 64: 42}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="must be a PEM string"):
        load_trusted_keys(path)


def test_load_trusted_keys_bad_pem_raises(tmp_path: Path) -> None:
    """A PEM-shaped string that doesn't parse -> SigningError."""
    body = {"keys": {"a" * 64: "-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----\n"}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="did not parse"):
        load_trusted_keys(path)


def test_load_trusted_keys_content_hash_mismatch_raises(tmp_path: Path) -> None:
    """A PEM whose content-hash doesn't match its table key -> SigningError.

    This is the trust-substitution attack: an attacker who can write the
    trusted-keys file but cannot forge the sha256 of the real key can't
    sneak their own key in under the wrong name.
    """
    key = _generate_key()
    other = _generate_key()
    # Table key is `key.key_id`, but the PEM is `other`'s -- content mismatch.
    body = {"keys": {key.key_id: _public_pem(other)}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="content-hash mismatch"):
        load_trusted_keys(path)


def test_load_trusted_keys_non_ed25519_raises(tmp_path: Path) -> None:
    """A valid non-Ed25519 PEM -> SigningError."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pub_pem = (
        rsa_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    body = {"keys": {"a" * 64: rsa_pub_pem}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="must be Ed25519"):
        load_trusted_keys(path)


def test_load_trusted_keys_ec_key_raises(tmp_path: Path) -> None:
    """An EC key -> SigningError (distinct from RSA to cover both non-Ed25519 branches)."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pub_pem = (
        ec_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    body = {"keys": {"a" * 64: ec_pub_pem}}
    path = tmp_path / "trusted.toml"
    path.write_bytes(tomli_w.dumps(body).encode("utf-8"))
    with pytest.raises(SigningError, match="must be Ed25519"):
        load_trusted_keys(path)


# ---------------------------------------------------------------------------
# sign_bytes / verify_bytes
# ---------------------------------------------------------------------------


def test_sign_bytes_returns_64_bytes() -> None:
    """Ed25519 signatures are exactly 64 bytes."""
    key = _generate_key()
    sig = sign_bytes(key, b"payload")
    assert isinstance(sig, bytes)
    assert len(sig) == 64


def test_sign_bytes_deterministic() -> None:
    """Same key + same payload -> identical signature (Ed25519 is deterministic)."""
    key = _generate_key()
    assert sign_bytes(key, b"payload") == sign_bytes(key, b"payload")


def test_sign_bytes_different_payloads_differ() -> None:
    """Different payloads yield different signatures."""
    key = _generate_key()
    assert sign_bytes(key, b"one") != sign_bytes(key, b"two")


def test_verify_bytes_valid_returns_true() -> None:
    """A signature from the correct key verifies."""
    key = _generate_key()
    payload = b"the real payload"
    sig = sign_bytes(key, payload)
    assert verify_bytes(key.public_key, payload, sig) is True


def test_verify_bytes_wrong_payload_returns_false() -> None:
    """A signature over different data does not verify."""
    key = _generate_key()
    sig = sign_bytes(key, b"original")
    assert verify_bytes(key.public_key, b"tampered", sig) is False


def test_verify_bytes_wrong_key_returns_false() -> None:
    """A signature from one key does not verify under another."""
    a = _generate_key()
    b = _generate_key()
    sig = sign_bytes(a, b"payload")
    assert verify_bytes(b.public_key, b"payload", sig) is False


def test_verify_bytes_corrupted_sig_returns_false() -> None:
    """A single-bit flip in the signature makes it invalid."""
    key = _generate_key()
    sig = bytearray(sign_bytes(key, b"payload"))
    sig[0] ^= 0x01
    assert verify_bytes(key.public_key, b"payload", bytes(sig)) is False


def test_verify_bytes_wrong_length_raises() -> None:
    """A non-64-byte signature is a caller bug, not a rejection -> PublicationError."""
    key = _generate_key()
    with pytest.raises(PublicationError, match="must be 64 bytes"):
        verify_bytes(key.public_key, b"payload", b"\x00" * 32)


# ---------------------------------------------------------------------------
# sign_json / verify_json (canonicalisation)
# ---------------------------------------------------------------------------


def test_sign_json_round_trips_with_verify_json() -> None:
    """Sign a JSON-shaped dict, verify it back."""
    key = _generate_key()
    obj = {"release_id": "v1.0.0", "files": [{"path": "a.jar", "sha256": "c" * 64}]}
    sig = sign_json(key, obj)
    assert verify_json(key.public_key, obj, sig) is True


def test_sign_json_canonical_independent_of_dict_order() -> None:
    """Two dicts with different key order sign identically (RFC 8785 sorts keys)."""
    key = _generate_key()
    a = {"alpha": 1, "beta": 2}
    b = {"beta": 2, "alpha": 1}
    assert sign_json(key, a) == sign_json(key, b)


def test_verify_json_wrong_payload_returns_false() -> None:
    """A sign/verify mismatch on the payload -> False."""
    key = _generate_key()
    sig = sign_json(key, {"release_id": "v1.0.0"})
    assert verify_json(key.public_key, {"release_id": "v1.0.1"}, sig) is False


def test_sign_json_rejects_float() -> None:
    """Canonicalization rejects floats; the TypeError propagates."""
    key = _generate_key()
    with pytest.raises(TypeError, match="floats"):
        sign_json(key, {"x": 1.5})


# ---------------------------------------------------------------------------
# verify_with_trusted_keys
# ---------------------------------------------------------------------------


def test_verify_with_trusted_keys_valid(tmp_path: Path) -> None:
    """A signature from a pinned key verifies."""
    key = _generate_key()
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, key)
    trusted = load_trusted_keys(path)
    payload = b"release payload"
    sig = sign_bytes(key, payload)
    assert verify_with_trusted_keys(trusted, key.key_id, payload, sig) is True


def test_verify_with_trusted_keys_unknown_key_returns_false(tmp_path: Path) -> None:
    """A key_id not in the trusted set -> False (soft rejection, not raise)."""
    key = _generate_key()
    other = _generate_key()
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, key)  # only `key` is pinned
    trusted = load_trusted_keys(path)
    payload = b"release payload"
    sig = sign_bytes(other, payload)  # signed with `other`
    assert verify_with_trusted_keys(trusted, other.key_id, payload, sig) is False


def test_verify_with_trusted_keys_wrong_payload_returns_false(tmp_path: Path) -> None:
    """Signed by a pinned key but over different data -> False."""
    key = _generate_key()
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, key)
    trusted = load_trusted_keys(path)
    sig = sign_bytes(key, b"original")
    assert verify_with_trusted_keys(trusted, key.key_id, b"tampered", sig) is False


def test_verify_with_trusted_keys_wrong_length_raises(tmp_path: Path) -> None:
    """A wrong-length signature still raises (caller bug, not a rejection)."""
    key = _generate_key()
    path = tmp_path / "trusted.toml"
    _write_trusted_keys(path, key)
    trusted = load_trusted_keys(path)
    with pytest.raises(PublicationError, match="must be 64 bytes"):
        verify_with_trusted_keys(trusted, key.key_id, b"payload", b"\x00" * 32)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_end_to_end_sign_then_load_and_verify(tmp_path: Path) -> None:
    """Full happy path: sign with one key, verify with a trusted-keys file."""
    signing_key = _generate_key()
    env = _setup_signing_env(tmp_path, signing_key)
    loaded_signing_key = load_signing_key(env=env)

    trusted_path = tmp_path / "trusted.toml"
    _write_trusted_keys(trusted_path, signing_key)
    trusted = load_trusted_keys(trusted_path)

    payload = b"the canonical manifest bytes"
    sig = sign_bytes(loaded_signing_key, payload)

    assert verify_with_trusted_keys(trusted, loaded_signing_key.key_id, payload, sig) is True
    # And any tampering breaks it:
    assert verify_with_trusted_keys(trusted, loaded_signing_key.key_id, payload + b"!", sig) is False


# ---------------------------------------------------------------------------
# Small helpers only used by one or two tests
# ---------------------------------------------------------------------------


def _raw_pub_bytes_for(public_key: object) -> bytes:
    """Return the raw Ed25519 point for a public key recovered from trusted-keys."""
    return public_key.public_bytes(  # type: ignore[attr-defined]
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
