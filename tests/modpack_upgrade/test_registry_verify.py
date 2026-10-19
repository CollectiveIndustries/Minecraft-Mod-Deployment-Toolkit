# tests/modpack_upgrade/test_registry_verify.py

"""Tests for chain-walk verification (Slice 11 T3 + T4 refactor).

Covers :mod:`minecraft.modpack_upgrade.registry_verify` end-to-end:

  * ``extract_chain_fields`` -- required-field enforcement
  * ``compute_entry_hash`` -- determinism and field-set coverage
  * ``verify_chain`` -- every one of the 11 failure reasons, plus
    happy-path single-entry / two-entry / multi-entry chains
  * ``verify_release`` -- chain ok + release present + bundle ok;
    every failure branch of the three-part check

Slice 11 T4 split ``_verify_bundle`` into four helpers
(``_load_bundle_manifest``, ``_check_manifest_hash``,
``_extract_signature_and_key``, ``_check_manifest_signature``). The
new tests below exercise the defensive branches those helpers
introduced, covering failure modes that were previously unreachable
without contrived setups (malformed JSON with a matching hash, non-dict
manifest, missing key_id, unknown key_id at bundle level, etc.).

Fixtures build real Ed25519 signatures via the same primitives that
``publish`` uses, so these tests exercise the actual wire format. If the
writer ever changes shape, these tests fail -- which is the point.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import tomli_w
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from minecraft.modpack_upgrade.errors import SigningError
from minecraft.modpack_upgrade.registry_verify import (
    CHAIN_FIELDS,
    GENESIS_HASH,
    _verify_bundle,
    compute_entry_hash,
    extract_chain_fields,
    verify_chain,
    verify_release,
)
from minecraft.modpack_upgrade.signing import (
    SigningKey,
    TrustedKeys,
    load_trusted_keys,
    sign_json,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> SigningKey:
    """A fresh Ed25519 signing key."""
    return SigningKey.from_private_key(Ed25519PrivateKey.generate())


@pytest.fixture
def trusted(tmp_path: Path, key: SigningKey) -> TrustedKeys:
    """A trusted-keys set containing only ``key``."""
    path = tmp_path / "trusted_keys.toml"
    pem = key.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    path.write_bytes(tomli_w.dumps({"keys": {key.key_id: pem}}).encode("utf-8"))
    return load_trusted_keys(path)


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------


def _chain_fields(
    *,
    release_id: str = "v1.0.0",
    previous: str = GENESIS_HASH,
    tag_object_sha: str = "a" * 40,
    tag_object_format: str = "sha1",
    commit_sha: str = "b" * 40,
    slip_sha: str = "c" * 64,
    manifest_sha: str = "d" * 64,
    schema_version: int = 1,
) -> dict:
    """Build a chain-fields dict with sensible defaults."""
    return {
        "schema_version": schema_version,
        "release_id": release_id,
        "tag_object_sha": tag_object_sha,
        "tag_object_format": tag_object_format,
        "commit_sha": commit_sha,
        "slip_content_sha256": slip_sha,
        "bundle_manifest_sha256": manifest_sha,
        "previous_entry_hash": previous,
    }


def _build_entry(key: SigningKey, **overrides: object) -> dict:
    """Build a well-formed signed registry entry."""
    fields = _chain_fields(**overrides)  # type: ignore[arg-type]
    entry_hash = compute_entry_hash(fields)
    signature_bytes = sign_json(key, fields)
    return {
        **fields,
        "entry_hash": entry_hash,
        "signature": base64.b64encode(signature_bytes).decode("ascii"),
        "key_id": key.key_id,
    }


def _write_registry(path: Path, entries: list[dict]) -> None:
    """Write entries as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not entries:
        path.write_text("", encoding="utf-8")
        return
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_chain(key: SigningKey, n: int, *, prefix: str = "v1.0") -> list[dict]:
    """Build an n-entry chain linked by entry_hash."""
    entries: list[dict] = []
    prev = GENESIS_HASH
    for i in range(n):
        tag = chr(ord("a") + i)
        e = _build_entry(
            key,
            release_id=f"{prefix}.{i}",
            previous=prev,
            tag_object_sha=("a" * 39) + tag,
            commit_sha=("b" * 39) + tag,
            slip_sha=("c" * 63) + tag,
            manifest_sha=("d" * 63) + tag,
        )
        entries.append(e)
        prev = e["entry_hash"]
    return entries


# ---------------------------------------------------------------------------
# extract_chain_fields
# ---------------------------------------------------------------------------


def test_extract_chain_fields_returns_all_fields() -> None:
    """``extract_chain_fields`` returns exactly the ``CHAIN_FIELDS`` set."""
    entry = _chain_fields()
    extracted = extract_chain_fields(entry)
    assert set(extracted.keys()) == set(CHAIN_FIELDS)
    for name in CHAIN_FIELDS:
        assert extracted[name] == entry[name]


def test_extract_chain_fields_tolerates_extra_fields() -> None:
    """Extra fields (entry_hash, signature, key_id) are ignored."""
    entry = {**_chain_fields(), "entry_hash": "x", "signature": "y", "key_id": "z"}
    extracted = extract_chain_fields(entry)
    assert set(extracted.keys()) == set(CHAIN_FIELDS)
    assert "entry_hash" not in extracted


def test_extract_chain_fields_raises_on_missing_field() -> None:
    """A missing chain field -> ``SigningError``."""
    entry = _chain_fields()
    del entry["tag_object_format"]
    with pytest.raises(SigningError, match="missing required field"):
        extract_chain_fields(entry)


def test_extract_chain_fields_raises_on_multiple_missing() -> None:
    """Multiple missing fields are all reported."""
    entry = _chain_fields()
    del entry["release_id"]
    del entry["commit_sha"]
    with pytest.raises(SigningError) as exc_info:
        extract_chain_fields(entry)
    msg = str(exc_info.value)
    assert "release_id" in msg
    assert "commit_sha" in msg


# ---------------------------------------------------------------------------
# compute_entry_hash
# ---------------------------------------------------------------------------


def test_compute_entry_hash_deterministic() -> None:
    """Same chain fields -> same hash across calls."""
    fields = _chain_fields()
    assert compute_entry_hash(fields) == compute_entry_hash(fields)


def test_compute_entry_hash_returns_64_lowercase_hex() -> None:
    """Hash is a lowercase 64-char SHA-256 hex."""
    h = compute_entry_hash(_chain_fields())
    assert len(h) == 64
    assert h == h.lower()
    int(h, 16)


def test_compute_entry_hash_covers_all_chain_fields() -> None:
    """Changing any single field changes the hash."""
    base = _chain_fields()
    baseline = compute_entry_hash(base)
    mutations = {
        "schema_version": 2,
        "release_id": "v9.9.9",
        "tag_object_sha": "f" * 40,
        "tag_object_format": "sha256",
        "commit_sha": "e" * 40,
        "slip_content_sha256": "e" * 64,
        "bundle_manifest_sha256": "e" * 64,
        "previous_entry_hash": "e" * 64,
    }
    for name, new_value in mutations.items():
        mutated = {**base, name: new_value}
        assert compute_entry_hash(mutated) != baseline, f"changing {name} did not change the hash"


# ---------------------------------------------------------------------------
# verify_chain -- happy paths
# ---------------------------------------------------------------------------


def test_verify_chain_missing_file_is_ok_empty(tmp_path: Path, trusted: TrustedKeys) -> None:
    """A missing registry file is a valid empty chain."""
    result = verify_chain(tmp_path / "does-not-exist.jsonl", trusted)
    assert result.ok is True
    assert result.entry_count == 0
    assert result.last_entry_hash == GENESIS_HASH
    assert result.failures == ()


def test_verify_chain_empty_file_is_ok_empty(tmp_path: Path, trusted: TrustedKeys) -> None:
    """An empty (or whitespace-only) registry file is a valid empty chain."""
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n\n", encoding="utf-8")
    result = verify_chain(path, trusted)
    assert result.ok is True
    assert result.entry_count == 0


def test_verify_chain_single_valid_entry(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A single well-formed entry verifies cleanly."""
    entries = _build_chain(key, 1)
    path = tmp_path / "reg.jsonl"
    _write_registry(path, entries)
    result = verify_chain(path, trusted)
    assert result.ok is True
    assert result.entry_count == 1
    assert result.last_entry_hash == entries[0]["entry_hash"]


def test_verify_chain_two_entries(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A two-entry chain verifies."""
    entries = _build_chain(key, 2)
    path = tmp_path / "reg.jsonl"
    _write_registry(path, entries)
    result = verify_chain(path, trusted)
    assert result.ok is True
    assert result.entry_count == 2
    assert result.last_entry_hash == entries[-1]["entry_hash"]


def test_verify_chain_multi_entry(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A five-entry chain verifies."""
    entries = _build_chain(key, 5)
    path = tmp_path / "reg.jsonl"
    _write_registry(path, entries)
    result = verify_chain(path, trusted)
    assert result.ok is True
    assert result.entry_count == 5


def test_verify_chain_ignores_blank_lines(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Blank lines between entries are ignored."""
    entries = _build_chain(key, 2)
    path = tmp_path / "reg.jsonl"
    path.write_text(
        "\n" + json.dumps(entries[0], sort_keys=True) + "\n\n" + json.dumps(entries[1], sort_keys=True) + "\n\n",
        encoding="utf-8",
    )
    result = verify_chain(path, trusted)
    assert result.ok is True
    assert result.entry_count == 2


# ---------------------------------------------------------------------------
# verify_chain -- failure paths
# ---------------------------------------------------------------------------


def test_verify_chain_malformed_json_at_zero(tmp_path: Path, trusted: TrustedKeys) -> None:
    """A malformed first line fails at index 0 with reason malformed_json."""
    path = tmp_path / "reg.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.entry_count == 0
    assert len(result.failures) == 1
    f = result.failures[0]
    assert f.index == 0
    assert f.reason == "malformed_json"


def test_verify_chain_malformed_json_on_later_line(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A malformed second line fails at index 1; first entry verified OK."""
    entries = _build_chain(key, 1)
    path = tmp_path / "reg.jsonl"
    path.write_text(
        json.dumps(entries[0], sort_keys=True) + "\nnot json\n",
        encoding="utf-8",
    )
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.entry_count == 1
    assert result.failures[0].index == 1
    assert result.failures[0].reason == "malformed_json"
    assert result.last_entry_hash == entries[0]["entry_hash"]


def test_verify_chain_not_an_object(tmp_path: Path, trusted: TrustedKeys) -> None:
    """A JSON array on a line fails with reason not_an_object."""
    path = tmp_path / "reg.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "not_an_object"


def test_verify_chain_missing_field(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """An entry missing a chain field fails with reason missing_field."""
    entry = _build_entry(key)
    del entry["commit_sha"]
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "missing_field"
    assert "commit_sha" in result.failures[0].detail


def test_verify_chain_bad_schema_version(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """An entry with schema_version != 1 fails with reason bad_schema_version."""
    entry = _build_entry(key, schema_version=2)
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "bad_schema_version"


def test_verify_chain_bad_entry_hash_format(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """An entry with a wrong-length entry_hash fails with reason bad_entry_hash."""
    entry = _build_entry(key)
    entry["entry_hash"] = "abc"
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "bad_entry_hash"


def test_verify_chain_entry_hash_mismatch(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A tampered chain field fails the recomputed-hash check."""
    entry = _build_entry(key)
    entry["commit_sha"] = "0" * 40
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "entry_hash_mismatch"


def test_verify_chain_previous_hash_mismatch_fork(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Two entries whose chain doesn't link -> reason previous_hash_mismatch at index 1."""
    entries = _build_chain(key, 2)
    forked_fields = {
        **extract_chain_fields(entries[1]),
        "previous_entry_hash": "f" * 64,
    }
    forked_hash = compute_entry_hash(forked_fields)
    forked_sig = sign_json(key, forked_fields)
    forked_entry = {
        **forked_fields,
        "entry_hash": forked_hash,
        "signature": base64.b64encode(forked_sig).decode("ascii"),
        "key_id": key.key_id,
    }
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entries[0], forked_entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].index == 1
    assert result.failures[0].reason == "previous_hash_mismatch"


def test_verify_chain_bad_signature_encoding(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A signature that isn't valid base64 fails with reason bad_signature_encoding."""
    entry = _build_entry(key)
    entry["signature"] = "!!!not-base64!!!"
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "bad_signature_encoding"


def test_verify_chain_bad_signature_length(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A signature that decodes to the wrong length fails with reason bad_signature_length."""
    entry = _build_entry(key)
    entry["signature"] = base64.b64encode(b"\x00" * 32).decode("ascii")
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "bad_signature_length"


def test_verify_chain_signature_invalid(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A signature that decodes to 64 bytes but doesn't validate -> signature_invalid."""
    entry = _build_entry(key)
    sig_bytes = bytearray(base64.b64decode(entry["signature"]))
    sig_bytes[0] ^= 0xFF
    entry["signature"] = base64.b64encode(bytes(sig_bytes)).decode("ascii")
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "signature_invalid"


def test_verify_chain_unknown_key(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """An entry signed by a key not in the trusted set -> unknown_key."""
    other_key = SigningKey.from_private_key(Ed25519PrivateKey.generate())
    entry = _build_entry(other_key)
    path = tmp_path / "reg.jsonl"
    _write_registry(path, [entry])
    result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "unknown_key"


def test_verify_chain_unreadable_registry(tmp_path: Path, trusted: TrustedKeys) -> None:
    """An ``OSError`` on read is reported as malformed_json (read-error family)."""
    path = tmp_path / "reg.jsonl"
    path.write_text("placeholder", encoding="utf-8")
    real_read = Path.read_text

    def _fail(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise OSError("permission denied")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_text", _fail)
        result = verify_chain(path, trusted)
    assert result.ok is False
    assert result.failures[0].reason == "malformed_json"
    assert "could not read" in result.failures[0].detail


# ---------------------------------------------------------------------------
# verify_release -- happy path
# ---------------------------------------------------------------------------


def _setup_bundle(
    bundle_dir: Path,
    key: SigningKey,
    *,
    release_id: str,
    tag_object_sha: str,
    commit_sha: str,
    slip_sha: str,
) -> tuple[dict, dict]:
    """Create a bundle + matching entry. Return ``(entry, manifest)``."""
    projection = {
        "schema_version": 1,
        "release_id": release_id,
        "slip_content_sha256": slip_sha,
        "approval_sha256": "a" * 64,
        "release_tag_object_sha": tag_object_sha,
        "release_commit_sha": commit_sha,
        "files": [],
    }
    sig_bytes = sign_json(key, projection)
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
    manifest = {**projection, "signature": sig_b64, "key_id": key.key_id}

    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (bundle_dir / "bundle-manifest.json").write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    entry = _build_entry(
        key,
        release_id=release_id,
        tag_object_sha=tag_object_sha,
        commit_sha=commit_sha,
        slip_sha=slip_sha,
        manifest_sha=manifest_sha,
    )
    return entry, manifest


def test_verify_release_ok(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Release present, chain intact, bundle manifest signature valid -> all ok."""
    registry = tmp_path / "reg.jsonl"
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    entry, _ = _setup_bundle(
        bundle,
        key,
        release_id="v1.0.0",
        tag_object_sha="a" * 40,
        commit_sha="b" * 40,
        slip_sha="c" * 64,
    )
    _write_registry(registry, [entry])

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is True
    assert result.release_present is True
    assert result.bundle_present is True
    assert result.bundle_failures == ()
    assert result.chain.ok is True


def test_verify_release_chain_broken(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Broken chain -> verify_release fails without inspecting bundle."""
    registry = tmp_path / "reg.jsonl"
    registry.write_text("not json\n", encoding="utf-8")
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    bundle.mkdir(parents=True)

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert result.chain.ok is False
    assert result.release_present is False


def test_verify_release_not_in_chain(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Valid chain, but no entry for the requested release -> release_present=False."""
    registry = tmp_path / "reg.jsonl"
    entries = _build_chain(key, 2)
    _write_registry(registry, entries)
    bundle = tmp_path / "www" / "releases" / "v9.9.9"
    bundle.mkdir(parents=True)

    result = verify_release(
        "v9.9.9",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert result.chain.ok is True
    assert result.release_present is False


# ---------------------------------------------------------------------------
# verify_release -- bundle failure paths
# ---------------------------------------------------------------------------


def test_verify_release_bundle_missing(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Release in chain, but bundle directory absent -> ok=False."""
    registry = tmp_path / "reg.jsonl"
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    entry, _ = _setup_bundle(
        bundle,
        key,
        release_id="v1.0.0",
        tag_object_sha="a" * 40,
        commit_sha="b" * 40,
        slip_sha="c" * 64,
    )
    shutil.rmtree(bundle)
    _write_registry(registry, [entry])

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert result.release_present is True
    assert result.bundle_present is False


def test_verify_release_bundle_manifest_missing(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Bundle directory present but no manifest -> bundle failure."""
    registry = tmp_path / "reg.jsonl"
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    entry, _ = _setup_bundle(
        bundle,
        key,
        release_id="v1.0.0",
        tag_object_sha="a" * 40,
        commit_sha="b" * 40,
        slip_sha="c" * 64,
    )
    (bundle / "bundle-manifest.json").unlink()
    _write_registry(registry, [entry])

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert any("not found" in f for f in result.bundle_failures)


def test_verify_release_bundle_manifest_hash_mismatch(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Manifest bytes changed after publication -> hash mismatch reported."""
    registry = tmp_path / "reg.jsonl"
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    entry, _ = _setup_bundle(
        bundle,
        key,
        release_id="v1.0.0",
        tag_object_sha="a" * 40,
        commit_sha="b" * 40,
        slip_sha="c" * 64,
    )
    (bundle / "bundle-manifest.json").write_text('{"tampered": true}\n', encoding="utf-8")
    _write_registry(registry, [entry])

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert any("hash mismatch" in f for f in result.bundle_failures)


def test_verify_release_bundle_manifest_signature_invalid(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Manifest signature doesn't verify -> failure reported."""
    registry = tmp_path / "reg.jsonl"
    bundle = tmp_path / "www" / "releases" / "v1.0.0"
    entry, manifest = _setup_bundle(
        bundle,
        key,
        release_id="v1.0.0",
        tag_object_sha="a" * 40,
        commit_sha="b" * 40,
        slip_sha="c" * 64,
    )
    manifest["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    tampered_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(tampered_bytes)
    new_sha = hashlib.sha256(tampered_bytes).hexdigest()

    new_entry = _build_entry(
        key,
        release_id=entry["release_id"],
        previous=entry["previous_entry_hash"],
        tag_object_sha=entry["tag_object_sha"],
        commit_sha=entry["commit_sha"],
        slip_sha=entry["slip_content_sha256"],
        manifest_sha=new_sha,
    )
    _write_registry(registry, [new_entry])

    result = verify_release(
        "v1.0.0",
        registry_path=registry,
        bundle_dir=bundle,
        trusted=trusted,
    )
    assert result.ok is False
    assert any("signature does not validate" in f for f in result.bundle_failures)


# ---------------------------------------------------------------------------
# Slice 11 T4: _verify_bundle branch coverage for the extracted helpers
# ---------------------------------------------------------------------------
#
# The following tests exercise _verify_bundle directly so the defensive
# branches introduced by the helper split are reachable without
# contriving an entire valid chain first. Each test builds a manifest
# whose sha256 matches the entry, so the failure surface of the *bundle*
# (not the chain) is the one being exercised.


def _build_entry_for_bundle(
    key: SigningKey,
    *,
    release_id: str = "v1.0.0",
    manifest_bytes: bytes,
) -> dict:
    """Build a signed registry entry whose manifest_sha matches manifest_bytes."""
    return _build_entry(
        key,
        release_id=release_id,
        manifest_sha=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def test_verify_bundle_manifest_read_error(tmp_path: Path, key: SigningKey, trusted: TrustedKeys, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` reading the manifest yields a 'could not read' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    placeholder = b"placeholder"
    manifest_path = bundle / "bundle-manifest.json"
    manifest_path.write_bytes(placeholder)
    entry = _build_entry_for_bundle(key, manifest_bytes=placeholder)

    real_read = Path.read_bytes
    target = manifest_path

    def _fail(self: Path) -> bytes:
        if self == target:
            raise OSError("permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _fail)
    failures = _verify_bundle(bundle, entry, trusted)
    assert any("could not read" in f for f in failures)


def test_verify_bundle_manifest_malformed_json_matching_hash(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Malformed JSON bytes with a matching hash yield a 'malformed' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    malformed = b"not json at all"
    (bundle / "bundle-manifest.json").write_bytes(malformed)
    entry = _build_entry_for_bundle(key, manifest_bytes=malformed)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("malformed" in f for f in failures)


def test_verify_bundle_manifest_non_dict_matching_hash(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """Non-dict JSON with a matching hash yields a 'not an object' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    payload = b"[1, 2, 3]\n"
    (bundle / "bundle-manifest.json").write_bytes(payload)
    entry = _build_entry_for_bundle(key, manifest_bytes=payload)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("not an object" in f for f in failures)


def test_verify_bundle_signature_field_missing(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A manifest without a signature field yields a 'signature ... missing' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "key_id": key.key_id,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)
    entry = _build_entry_for_bundle(key, manifest_bytes=manifest_bytes)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("signature field is missing" in f for f in failures)


def test_verify_bundle_key_id_field_missing(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A manifest without a key_id yields a 'key_id ... missing' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)
    entry = _build_entry_for_bundle(key, manifest_bytes=manifest_bytes)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("key_id field is missing" in f for f in failures)


def test_verify_bundle_signature_bad_base64(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A signature that isn't valid base64 yields a 'base64 invalid' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "signature": "!!!not-base64!!!",
        "key_id": key.key_id,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)
    entry = _build_entry_for_bundle(key, manifest_bytes=manifest_bytes)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("base64 invalid" in f for f in failures)


def test_verify_bundle_signature_wrong_length(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A signature decoding to wrong length yields a 'wrong length' failure."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "signature": base64.b64encode(b"\x00" * 32).decode("ascii"),
        "key_id": key.key_id,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)
    entry = _build_entry_for_bundle(key, manifest_bytes=manifest_bytes)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("wrong length" in f for f in failures)


def test_verify_bundle_unknown_key_at_bundle_level(tmp_path: Path, key: SigningKey, trusted: TrustedKeys) -> None:
    """A manifest whose key_id isn't trusted yields 'not in trusted set' at bundle level.

    Distinct from ``test_verify_chain_unknown_key`` which fails at the
    chain layer. Here the chain would be valid (if we ran it) -- the
    failure is at the bundle's manifest, which is what _verify_bundle
    is responsible for.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    unknown_key_id = "f" * 64  # valid hex, not in trusted set
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
        "key_id": unknown_key_id,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)
    entry = _build_entry_for_bundle(key, manifest_bytes=manifest_bytes)

    failures = _verify_bundle(bundle, entry, trusted)
    assert any("not in trusted set" in f for f in failures)
