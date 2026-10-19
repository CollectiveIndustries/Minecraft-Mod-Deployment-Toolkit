# src/minecraft/modpack_upgrade/registry_verify.py

"""Chain-walk verification for the release registry and bundle manifests.

Upgrade_System_specifications Section 7.2 (chain structure), Section 7.3
(prod rejection rules), Section 6.4 (bundle manifest signature). This
module is the *consumer* side of the primitives that ``publish`` writes
with -- it must agree with ``publish`` on three things, and both sides
import them from here so they cannot drift:

  1. **Chain field set** (``CHAIN_FIELDS``). The exact fields that the
     entry hash and the entry signature cover.
  2. **Entry hash computation** (``compute_entry_hash``). SHA-256 of
     canonical JSON over ``CHAIN_FIELDS``, matching what ``publish``
     stores in ``entry_hash``.
  3. **Signature projection** (``extract_chain_fields``). The exact same
     dict that ``publish`` passes to ``sign_json``.

What this module does
---------------------

  * ``verify_chain(registry_path, trusted)`` -- walk
    ``release-index.jsonl`` from genesis, checking every link:
      - line parses as a JSON object
      - every chain field is present
      - ``schema_version == 1``
      - ``entry_hash`` is 64-char lowercase hex
      - recomputed ``sha256(chain_fields)`` equals ``entry_hash``
      - ``previous_entry_hash`` equals the prior entry's ``entry_hash``
        (or genesis zeroes for the first entry)
      - ``key_id`` is present in the trusted key set
      - ``signature`` base64-decodes to 64 bytes and validates under
        ``key_id``'s public key against the canonical projection
    Stops at the first failure. A partial chain is not "mostly ok" --
    everything after a break is unverifiable.

  * ``verify_release(...)`` -- verify the chain, then verify that a
    specific ``release_id`` is present in the chain, then verify its
    bundle manifest (hash matches the entry's recorded value + signature
    validates against the trusted key).

What this module does NOT do
----------------------------

  * Repair or report every failure in a broken chain. First-failure is
    enough; anything beyond a break is meaningless.
  * Load trusted keys. Callers pass a :class:`TrustedKeys` (from
    :func:`minecraft.modpack_upgrade.signing.load_trusted_keys`).
  * Decide exit codes. ``publish --verify-only`` maps ``ok`` to 0/3;
    prod daemon (Slice 12) will map it to its own exit codes.
  * Verify the bundle's *contents* against the manifest. That's a
    separate concern (does every file in ``files`` actually exist with
    the recorded hash); the bundle manifest signature + its hash
    matching the registry entry is the identity check that belongs here.

Bundle verification is split across helpers
-------------------------------------------

``_verify_bundle`` orchestrates four single-purpose helpers:

  * ``_load_bundle_manifest`` -- existence, read, parse, dict-shape.
  * ``_check_manifest_hash`` -- SHA-256 of manifest bytes equals
    ``entry["bundle_manifest_sha256"]``.
  * ``_extract_signature_and_key`` -- signature field presence, base64
    validity, length, key lookup against the trusted set.
  * ``_check_manifest_signature`` -- the actual Ed25519 verification.

The split keeps ``_verify_bundle`` at a cyclomatic complexity of 4
(orchestration + 3 short-circuits) while preserving the exact failure
strings that callers depend on. This was a Slice 11 T4 refactor target:
the original 17-complexity single function had untested branches for
defensive failure modes that only fire on contrived inputs.

Consistency with publish
------------------------

The ``tag_object_format`` field was, prior to Slice 11 T3, in the
*signed* projection but NOT in the ``entry_hash`` computation. That
meant a tampered ``tag_object_format`` would fail the signature check
but pass the hash check. Post-T3 both cover the same field set; the
inconsistency is closed. There are no production releases predating this
change, so the change is safe.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from LoggingCore import get_logger

from .errors import SigningError
from .signing import TrustedKeys, verify_json

__all__ = [
    "CHAIN_FIELDS",
    "GENESIS_HASH",
    "ChainFailure",
    "ChainVerification",
    "ReleaseVerification",
    "compute_entry_hash",
    "extract_chain_fields",
    "verify_chain",
    "verify_release",
]

_log = get_logger(__name__)

#: Fields that both the entry hash and the entry signature cover.
#: Order is canonical (used for documentation and error messages only;
#: the actual serialization sorts keys, per RFC 8785).
CHAIN_FIELDS: tuple[str, ...] = (
    "schema_version",
    "release_id",
    "tag_object_sha",
    "tag_object_format",
    "commit_sha",
    "slip_content_sha256",
    "bundle_manifest_sha256",
    "previous_entry_hash",
)

#: Genesis sentinel: the ``previous_entry_hash`` recorded by the first
#: entry in the chain. ``"0" * 64`` matches the codebase-wide convention
#: for "absent" SHA-256.
GENESIS_HASH = "0" * 64

# Failure reason vocabulary. Kept as plain string literals rather than an
# enum because tests assert on the exact string and future slices may
# want to add reasons without touching an enum class.
_REASON_MALFORMED_JSON = "malformed_json"
_REASON_NOT_AN_OBJECT = "not_an_object"
_REASON_MISSING_FIELD = "missing_field"
_REASON_BAD_SCHEMA_VERSION = "bad_schema_version"
_REASON_BAD_ENTRY_HASH = "bad_entry_hash"
_REASON_ENTRY_HASH_MISMATCH = "entry_hash_mismatch"
_REASON_PREVIOUS_HASH_MISMATCH = "previous_hash_mismatch"
_REASON_BAD_SIGNATURE_ENCODING = "bad_signature_encoding"
_REASON_BAD_SIGNATURE_LENGTH = "bad_signature_length"
_REASON_SIGNATURE_INVALID = "signature_invalid"
_REASON_UNKNOWN_KEY = "unknown_key"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainFailure:
    """One chain-break, with enough context to report and dispatch on.

    ``index`` is the 0-based line number in ``release-index.jsonl``.
    ``release_id`` is populated when the entry parsed far enough to have
    one, otherwise ``None`` -- an unparseable line has no release_id to
    blame. ``reason`` is one of the ``_REASON_*`` string constants.
    """

    index: int
    release_id: str | None
    reason: str
    detail: str


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking ``release-index.jsonl`` from genesis.

    ``last_entry_hash`` is the ``entry_hash`` of the last successfully
    verified entry, or ``GENESIS_HASH`` if the chain is empty or the very
    first entry failed. Callers that want to compare against a "known
    head" use this field.

    ``failures`` is empty when ``ok`` is True. Non-empty means the chain
    is broken at ``failures[0].index`` and everything after is unverified.
    """

    ok: bool
    entry_count: int
    last_entry_hash: str
    failures: tuple[ChainFailure, ...]


@dataclass(frozen=True)
class ReleaseVerification:
    """Result of verifying one release's chain + bundle.

    ``chain`` is the full chain-verification result. ``release_present``
    is True iff the chain is valid AND contains an entry with matching
    ``release_id``. ``bundle_failures`` are human-readable strings (not
    structured failures like chain ones) because the bundle-verification
    surface is narrower and doesn't need per-reason dispatch.

    ``ok`` is True iff the chain is valid, the release is present, the
    bundle directory exists, and there are no bundle failures. A chain
    that's valid but doesn't contain the release is a failure (the
    caller asked for a specific release and it wasn't there).
    """

    ok: bool
    release_id: str
    chain: ChainVerification
    release_present: bool
    bundle_present: bool
    bundle_failures: tuple[str, ...]


# ---------------------------------------------------------------------------
# Shared primitives (used by publish AND verify)
# ---------------------------------------------------------------------------


def extract_chain_fields(entry: dict) -> dict:
    """Return the chain-fields subset of ``entry``.

    Raises :class:`SigningError` if any field in :data:`CHAIN_FIELDS` is
    missing. This is what :func:`compute_entry_hash` hashes and what
    :func:`publish._append_registry_entry` signs, so the two sides agree
    by construction.
    """
    missing = [name for name in CHAIN_FIELDS if name not in entry]
    if missing:
        raise SigningError(f"chain entry missing required field(s): {', '.join(missing)}")
    return {name: entry[name] for name in CHAIN_FIELDS}


def compute_entry_hash(chain_fields: dict) -> str:
    """Deterministic SHA-256 over ``CHAIN_FIELDS`` as extracted from ``chain_fields``.

    Uses ``json.dumps(..., sort_keys=True)`` -- NOT RFC 8785 canonical JSON
    -- to remain consistent with the values that ``publish`` has already
    written and that verifiers must reproduce. The two diverge only for
    edge-case inputs (very large floats, unusual Unicode) that never
    appear in practice; switching to RFC 8785 here would require
    re-signing every existing entry and is deferred.

    Fields are extracted via :func:`extract_chain_fields` so the caller
    can pass a full entry (with ``entry_hash``, ``signature``, ``key_id``
    present) or a minimal dict of just the chain fields. Either works.
    """
    fields = extract_chain_fields(chain_fields)
    text = json.dumps(fields, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def _parse_line(index: int, line: str) -> dict:
    """Parse one registry line. Raises :class:`ChainFailure` (as an exception) on error."""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _ChainEntryError(
            _REASON_MALFORMED_JSON,
            f"line {index}: not valid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise _ChainEntryError(
            _REASON_NOT_AN_OBJECT,
            f"line {index}: parsed JSON is {type(parsed).__name__}, not an object",
        )
    return parsed


class _ChainEntryError(Exception):
    """Internal: a single chain entry failed verification."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _verify_one_entry(
    entry: dict,
    *,
    expected_previous_hash: str,
    trusted: TrustedKeys,
) -> None:
    """Verify one parsed entry; raise ``_ChainEntryError`` on any problem."""
    try:
        fields = extract_chain_fields(entry)
    except SigningError as exc:
        raise _ChainEntryError(_REASON_MISSING_FIELD, str(exc)) from exc

    if fields["schema_version"] != 1:
        raise _ChainEntryError(
            _REASON_BAD_SCHEMA_VERSION,
            f"unsupported schema_version {fields['schema_version']!r}",
        )

    recorded_hash = entry.get("entry_hash")
    if not isinstance(recorded_hash, str) or len(recorded_hash) != 64:
        raise _ChainEntryError(
            _REASON_BAD_ENTRY_HASH,
            f"entry_hash is not a 64-char string: {recorded_hash!r}",
        )

    recomputed = compute_entry_hash(fields)
    if recomputed != recorded_hash:
        raise _ChainEntryError(
            _REASON_ENTRY_HASH_MISMATCH,
            f"entry_hash mismatch: recorded={recorded_hash} recomputed={recomputed}",
        )

    if fields["previous_entry_hash"] != expected_previous_hash:
        raise _ChainEntryError(
            _REASON_PREVIOUS_HASH_MISMATCH,
            f"previous_entry_hash={fields['previous_entry_hash']} does not match expected={expected_previous_hash} (chain break or fork)",
        )

    signature_b64 = entry.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise _ChainEntryError(
            _REASON_BAD_SIGNATURE_ENCODING,
            "signature field is missing or empty",
        )
    try:
        signature_bytes = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _ChainEntryError(
            _REASON_BAD_SIGNATURE_ENCODING,
            f"signature is not valid base64: {exc}",
        ) from exc
    if len(signature_bytes) != 64:
        raise _ChainEntryError(
            _REASON_BAD_SIGNATURE_LENGTH,
            f"signature decoded to {len(signature_bytes)} bytes, expected 64",
        )

    key_id = entry.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise _ChainEntryError(
            _REASON_UNKNOWN_KEY,
            "key_id field is missing or empty",
        )
    public_key = trusted.by_id.get(key_id)
    if public_key is None:
        raise _ChainEntryError(
            _REASON_UNKNOWN_KEY,
            f"key_id {key_id[:12]}... is not in the trusted set ({len(trusted.by_id)} pinned key(s))",
        )

    if not verify_json(public_key, fields, signature_bytes):
        raise _ChainEntryError(
            _REASON_SIGNATURE_INVALID,
            "Ed25519 signature does not validate against the trusted key",
        )


def verify_chain(registry_path: Path, trusted: TrustedKeys) -> ChainVerification:
    """Walk ``registry_path`` from genesis. Return structured result.

    A missing or empty file is treated as a valid empty chain
    (``ok=True``, ``entry_count=0``, ``last_entry_hash=GENESIS_HASH``).
    That's the state before the first publish and is not a failure.

    First failure stops the walk. Everything after a break is
    unverifiable, and continuing past a break gives operators a
    false-positive tail of "OK" lines that encourage ignoring the real
    error at the head.
    """
    if not registry_path.is_file():
        return ChainVerification(
            ok=True,
            entry_count=0,
            last_entry_hash=GENESIS_HASH,
            failures=(),
        )

    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        failure = ChainFailure(
            index=0,
            release_id=None,
            reason=_REASON_MALFORMED_JSON,
            detail=f"could not read registry {registry_path}: {exc}",
        )
        return ChainVerification(
            ok=False,
            entry_count=0,
            last_entry_hash=GENESIS_HASH,
            failures=(failure,),
        )

    lines = [line for line in text.splitlines() if line.strip()]
    expected_previous = GENESIS_HASH
    last_verified_hash = GENESIS_HASH
    verified_count = 0

    for index, line in enumerate(lines):
        try:
            entry = _parse_line(index, line)
        except _ChainEntryError as err:
            return ChainVerification(
                ok=False,
                entry_count=verified_count,
                last_entry_hash=last_verified_hash,
                failures=(
                    ChainFailure(
                        index=index,
                        release_id=None,
                        reason=err.reason,
                        detail=err.detail,
                    ),
                ),
            )

        try:
            _verify_one_entry(
                entry,
                expected_previous_hash=expected_previous,
                trusted=trusted,
            )
        except _ChainEntryError as err:
            return ChainVerification(
                ok=False,
                entry_count=verified_count,
                last_entry_hash=last_verified_hash,
                failures=(
                    ChainFailure(
                        index=index,
                        release_id=entry.get("release_id") if isinstance(entry.get("release_id"), str) else None,
                        reason=err.reason,
                        detail=err.detail,
                    ),
                ),
            )

        expected_previous = entry["entry_hash"]
        last_verified_hash = entry["entry_hash"]
        verified_count += 1

    return ChainVerification(
        ok=True,
        entry_count=verified_count,
        last_entry_hash=last_verified_hash,
        failures=(),
    )


# ---------------------------------------------------------------------------
# Release (chain + bundle) verification
# ---------------------------------------------------------------------------


def _find_release_entry(registry_path: Path, release_id: str) -> dict | None:
    """Return the parsed entry whose ``release_id`` matches, or None.

    Only called after :func:`verify_chain` reports ``ok=True``, so every
    line is guaranteed to parse as a JSON object with a ``release_id``
    field. This is a narrow convenience over re-parsing.
    """
    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if parsed.get("release_id") == release_id:
            return parsed
    return None


# -- bundle verification helpers (Slice 11 T4 refactor) ---------------------


def _load_bundle_manifest(bundle_dir: Path) -> tuple[dict, bytes] | str:
    """Load and parse the bundle manifest.

    Returns ``(manifest_dict, raw_bytes)`` on success, or a human-readable
    failure string on any problem. The string-or-tuple shape lets the
    caller distinguish success from failure without a separate bool
    (``isinstance(loaded, str)``).

    Checks, in order: directory exists, manifest file exists, file is
    readable, bytes parse as JSON, parsed value is a dict. Every failure
    path is defensive -- the caller's hash check would already catch the
    "manifest doesn't match what the entry recorded" case, but a
    malformed-bytes-with-matching-hash scenario is possible if the
    attacker controls both sides (unrealistic, but the defensive error
    is cheap).
    """
    if not bundle_dir.is_dir():
        return "bundle directory not found"
    manifest_path = bundle_dir / "bundle-manifest.json"
    if not manifest_path.is_file():
        return "bundle-manifest.json not found"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        return f"could not read bundle-manifest.json: {exc}"
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        return f"bundle-manifest.json malformed: {exc}"
    if not isinstance(manifest, dict):
        return f"bundle-manifest.json is {type(manifest).__name__}, not an object"
    return manifest, manifest_bytes


def _check_manifest_hash(manifest_bytes: bytes, entry: dict) -> str | None:
    """Return a failure string if manifest bytes don't match the entry's hash.

    ``None`` means the hash matched. Split out so ``_verify_bundle`` can
    accumulate failures from independent checks.
    """
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    expected = entry.get("bundle_manifest_sha256")
    if actual != expected:
        return f"bundle-manifest hash mismatch: entry={expected} actual={actual}"
    return None


def _extract_signature_and_key(manifest: dict, trusted: TrustedKeys) -> tuple[bytes, Ed25519PublicKey] | str:
    """Extract the decoded signature and the trusted public key.

    Returns ``(signature_bytes, public_key)`` on success, or a
    human-readable failure string on any problem: signature field missing
    or not a non-empty string, key_id field missing or not a non-empty
    string, base64 decode failure, wrong signature length, or key_id not
    in the trusted set.

    The split lets ``_check_manifest_signature`` stay small (it only does
    the actual crypto) while this helper focuses on the four extraction
    and validation branches.
    """
    signature_b64 = manifest.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        return "bundle-manifest.json signature field is missing or empty"
    key_id = manifest.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        return "bundle-manifest.json key_id field is missing or empty"
    try:
        signature_bytes = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        return f"bundle-manifest signature base64 invalid: {exc}"
    if len(signature_bytes) != 64:
        return f"bundle-manifest signature wrong length: {len(signature_bytes)}"
    public_key = trusted.by_id.get(key_id)
    if public_key is None:
        return f"bundle-manifest key_id {key_id[:12]}... not in trusted set"
    return signature_bytes, public_key


def _check_manifest_signature(manifest: dict, trusted: TrustedKeys) -> str | None:
    """Return a failure string if the manifest signature doesn't validate.

    ``None`` means the signature verified against the trusted public key
    over the projection (``manifest`` minus ``signature`` and ``key_id``).
    """
    extracted = _extract_signature_and_key(manifest, trusted)
    if isinstance(extracted, str):
        return extracted
    signature_bytes, public_key = extracted
    projection = {k: v for k, v in manifest.items() if k not in ("signature", "key_id")}
    if not verify_json(public_key, projection, signature_bytes):
        return "bundle-manifest signature does not validate against the trusted key"
    return None


def _verify_bundle(
    bundle_dir: Path,
    entry: dict,
    trusted: TrustedKeys,
) -> list[str]:
    """Verify the bundle manifest hash + signature; return failure strings.

    Assumes the entry has already passed chain verification, so
    ``entry["bundle_manifest_sha256"]`` and ``entry["key_id"]`` are
    trustworthy in the sense that they're what the chain committed to.

    Two independent checks -- hash and signature -- are accumulated so a
    caller sees both failures at once when both are wrong. That's the
    only reason this isn't a short-circuit chain: the operator benefits
    from seeing all the ways the bundle is broken, not just the first.

    Does NOT verify every file listed in the manifest against disk. That
    is a distinct check (the manifest's ``files`` array is a list of
    ``{path, size_bytes, sha256}``) that belongs to a deployment-time
    verifier, not the publication verifier.
    """
    loaded = _load_bundle_manifest(bundle_dir)
    if isinstance(loaded, str):
        return [loaded]
    manifest, manifest_bytes = loaded

    failures: list[str] = []
    hash_failure = _check_manifest_hash(manifest_bytes, entry)
    if hash_failure is not None:
        failures.append(hash_failure)
    sig_failure = _check_manifest_signature(manifest, trusted)
    if sig_failure is not None:
        failures.append(sig_failure)
    return failures


def verify_release(
    release_id: str,
    *,
    registry_path: Path,
    bundle_dir: Path,
    trusted: TrustedKeys,
) -> ReleaseVerification:
    """Verify the chain, locate ``release_id``, and verify its bundle.

    The three checks are cumulative and short-circuit:
      - chain broken -> release verification fails, bundle not consulted
      - chain ok but release not in chain -> fails, bundle not consulted
      - chain ok, release found, bundle missing or corrupt -> fails

    A "release present but bundle absent" outcome is a failure for
    ``--verify-only`` (the release is not deployable), but the caller may
    choose to interpret it differently if they're auditing. The result
    type carries ``release_present`` and ``bundle_present`` separately so
    downstream consumers can distinguish "chain is intact but the release
    was reaped" from "chain is broken".
    """
    chain = verify_chain(registry_path, trusted)
    if not chain.ok:
        return ReleaseVerification(
            ok=False,
            release_id=release_id,
            chain=chain,
            release_present=False,
            bundle_present=bundle_dir.is_dir(),
            bundle_failures=(),
        )

    entry = _find_release_entry(registry_path, release_id)
    if entry is None:
        return ReleaseVerification(
            ok=False,
            release_id=release_id,
            chain=chain,
            release_present=False,
            bundle_present=bundle_dir.is_dir(),
            bundle_failures=(),
        )

    bundle_present = bundle_dir.is_dir()
    bundle_failures = _verify_bundle(bundle_dir, entry, trusted)
    ok = bundle_present and not bundle_failures

    return ReleaseVerification(
        ok=ok,
        release_id=release_id,
        chain=chain,
        release_present=True,
        bundle_present=bundle_present,
        bundle_failures=tuple(bundle_failures),
    )
