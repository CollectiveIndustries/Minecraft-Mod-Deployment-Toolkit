# src/minecraft/deploy_pack/daemon/verify.py

"""Release verification for the Minecraft deploy daemon.

Slice 11 was local-only (slip schema + content hash + file existence).
Slice 12 adds chain-walk and bundle-manifest signature verification via
:mod:`minecraft.modpack_upgrade.registry_verify`. The local checks stay
in place and run first - they give the operator a stable diagnostic on
the most common failure modes (missing slip, malformed schema, hash
drift) before chain verification is attempted.

Which checks run
----------------
When ``registry_path`` and ``trusted_keys_path`` are both provided:
  * local slip checks (unchanged from slice 11)
  * trusted-keys load  (Section 7.5)
  * chain walk         (Section 7.1-7.3)
  * release-in-chain   (Section 7.3)
  * bundle present
  * bundle manifest hash  (Section 6.4)
  * bundle manifest signature  (Section 6.4)

When either is absent: local slip checks only (backward compat with
slice-11 tests and the daemon's pre-slice-12 default).

Contract: ``verify_release`` never raises on verification failure -
every failure is a block. Callers that want a hard stop (`/deploy` per
Section 12.4) translate ``result.chain_ok`` / ``result.status`` into typed
errors at the plugin layer.

Structured result
-----------------
``VerificationResult`` carries:

  * ``status``           - ``"ok"`` or ``"blocked"`` (the wire field)
  * ``checks``           - per-check pass/fail (informational)
  * ``reasons``          - human-readable block reasons
  * ``chain_ok``         - True iff chain walk succeeded (or skipped)
  * ``chain_failures``   - structured reason codes from ChainFailure

``chain_ok`` and ``chain_failures`` are slice-12 additions used by
:class:`HttpAdminPlugin`'s ``/deploy`` handler to dispatch to
``RegistryChainFailedError`` (409/code 26) versus
``VerificationBlockedError`` (409/code 20).

Exception hierarchy note
------------------------
``slip_mod``'s exception hierarchy is::

    UpgradeConfigError
      SchemaError              (schema violations in a readable slip)
        SlipError              (slip file cannot be read at all)
          HashMismatchError    (content_sha256 does not match)

So ``HashMismatchError`` is a subclass of ``SlipError``, which is a
subclass of ``SchemaError``. ``except`` clauses in
:func:`_load_slip_or_block` are ordered narrowest-first so each raised
exception maps to its intended reason string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.errors import (
    HashMismatchError,
    SchemaError,
    SigningError,
    SlipError,
)
from minecraft.modpack_upgrade.registry_verify import (
    ReleaseVerification,
)
from minecraft.modpack_upgrade.registry_verify import (
    verify_release as chain_verify_release,
)
from minecraft.modpack_upgrade.signing import load_trusted_keys

__all__ = ["VerificationResult", "verify_release"]


@dataclass
class VerificationResult:
    """Outcome of a verification pass."""

    status: Literal["ok", "blocked"] = "ok"
    release_id: str | None = None
    content_sha256: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    # Slice 12: structured chain outcome for /deploy dispatch.
    chain_ok: bool = True
    chain_failures: list[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        """Record ``reason`` and flip status to blocked."""
        self.reasons.append(reason)
        self.status = "blocked"


# ---------------------------------------------------------------------------
# Local slip checks (unchanged from slice 11)
# ---------------------------------------------------------------------------


def _resolve_pointer(slip_dir: Path, pointer: str) -> tuple[str | None, str | None]:
    """Return ``(release_id, reason)`` from a pointer file."""
    path = slip_dir / pointer
    if not path.is_file():
        return (None, "pointer_missing")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return (None, "pointer_unreadable")
    if not content:
        return (None, "pointer_malformed")
    if content.endswith(".toml"):
        content = content[: -len(".toml")]
    return (content, None)


def _check_slip_sha(result: VerificationResult, expected: str | None) -> None:
    """Populate the ``expected_slip_sha256`` check."""
    if expected is None:
        return
    if result.content_sha256 is None:
        result.checks["expected_slip_sha256"] = False
        result.block("expected_slip_sha256_unverifiable")
        return
    matched = result.content_sha256 == expected
    result.checks["expected_slip_sha256"] = matched
    if not matched:
        result.block("expected_slip_sha256_mismatch")


def _check_commit_sha(
    result: VerificationResult,
    slip_obj: slip_mod.PackingSlip,
    expected: str | None,
) -> None:
    """Populate the ``expected_commit_sha`` check.

    The slip's ``[meta]`` MAY contain a ``commit_sha`` field; if present,
    it is compared. If absent, the check is marked False with reason
    ``expected_commit_sha_unverifiable`` (blocking).
    """
    if expected is None:
        return
    meta_commit = slip_obj.meta.get("commit_sha") if slip_obj.meta else None
    if not isinstance(meta_commit, str):
        result.checks["expected_commit_sha"] = False
        result.block("expected_commit_sha_unverifiable")
        return
    matched = meta_commit == expected
    result.checks["expected_commit_sha"] = matched
    if not matched:
        result.block("expected_commit_sha_mismatch")


def _check_bundle_exists(result: VerificationResult, release_store: Path, release_id: str) -> None:
    """Populate the ``bundle_exists`` check (local-only mode)."""
    bundle_root = release_store / release_id
    exists = bundle_root.is_dir() and (bundle_root / "artifacts").is_dir()
    result.checks["bundle_exists"] = exists
    if not exists:
        result.block("bundle_missing")


def _check_bundle_manifest_present(result: VerificationResult, release_store: Path, release_id: str) -> None:
    """Populate the ``bundle_manifest_present`` check (existence only, local-only mode)."""
    manifest = release_store / release_id / "bundle-manifest.json"
    result.checks["bundle_manifest_present"] = manifest.is_file()


def _load_slip_or_block(result: VerificationResult, slip_path: Path) -> slip_mod.PackingSlip | None:
    """Load a slip; on failure, record the block reason and return None.

    ``except`` clauses are ordered narrowest-first because
    ``HashMismatchError`` < ``SlipError`` < ``SchemaError``.
    """
    try:
        return slip_mod.load_slip(slip_path)
    except HashMismatchError:
        result.block("slip_hash_mismatch")
        return None
    except SlipError:
        result.block("slip_not_found")
        return None
    except SchemaError:
        result.block("slip_schema_error")
        return None


# ---------------------------------------------------------------------------
# Slice 12: chain walk + bundle-manifest verification
# ---------------------------------------------------------------------------


def _verify_chain_and_bundle(
    result: VerificationResult,
    *,
    release_id: str,
    registry_path: Path,
    bundle_dir: Path,
    trusted_keys_path: Path,
) -> None:
    """Run Section 7.1-Section 7.3 chain walk and Section 6.4 bundle verification.

    Populates ``checks``, ``chain_ok``, ``chain_failures``, and ``reasons``.
    Sets ``status = "blocked"`` for any failure.
    """
    try:
        trusted = load_trusted_keys(trusted_keys_path)
    except SigningError as exc:
        result.checks["trusted_keys"] = False
        result.block(f"trusted_keys_error: {exc}")
        return
    result.checks["trusted_keys"] = True

    verification: ReleaseVerification = chain_verify_release(
        release_id,
        registry_path=registry_path,
        bundle_dir=bundle_dir,
        trusted=trusted,
    )

    result.checks["chain"] = verification.chain.ok
    result.checks["release_present"] = verification.release_present
    result.checks["bundle_present"] = verification.bundle_present
    result.checks["bundle_manifest"] = not verification.bundle_failures

    # Chain break is a hard stop: nothing past it is verifiable.
    if not verification.chain.ok:
        result.chain_ok = False
        for failure in verification.chain.failures:
            result.chain_failures.append(failure.reason)
            result.reasons.append(f"chain_failure[{failure.reason}] index={failure.index} {failure.release_id or '(unknown)'}: {failure.detail}")
        result.status = "blocked"
        return

    if not verification.release_present:
        result.block(f"release {release_id!r} is not in the registry chain")
        return

    if not verification.bundle_present:
        result.block(f"bundle directory missing for {release_id!r}")
        return

    for failure in verification.bundle_failures:
        result.block(failure)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def verify_release(
    *,
    release_id: str | None,
    release_pointer: str | None,
    expected_commit_sha: str | None,
    expected_slip_sha256: str | None,
    slip_dir: Path,
    release_store: Path,
    registry_path: Path | None = None,
    trusted_keys_path: Path | None = None,
) -> VerificationResult:
    """Run the full verification pass.

    Local slip checks always run. When ``registry_path`` and
    ``trusted_keys_path`` are both provided, chain walk and bundle
    verification run on top (slice 12). When either is ``None``, only
    the slice-11 local checks run (backward compat).

    Returns :class:`VerificationResult` with status ``ok`` or ``blocked``.
    Does not raise on verification failure - every failure is a block.
    The caller (`/deploy` in ``http.py``) translates
    ``chain_ok`` / ``status`` into typed errors per Section 12.4.
    """
    result = VerificationResult(status="ok")

    if release_pointer is not None:
        resolved, reason = _resolve_pointer(slip_dir, release_pointer)
        if reason is not None:
            result.block(reason)
            return result
        result.release_id = resolved
    else:
        result.release_id = release_id

    if result.release_id is None:
        result.block("release_id_missing")
        return result

    slip_path = slip_dir / f"{result.release_id}.toml"
    slip_obj = _load_slip_or_block(result, slip_path)
    if slip_obj is None:
        return result

    result.content_sha256 = slip_obj.content_sha256
    result.checks["slip_sha256"] = True

    _check_slip_sha(result, expected_slip_sha256)
    _check_commit_sha(result, slip_obj, expected_commit_sha)

    if registry_path is not None and trusted_keys_path is not None:
        _verify_chain_and_bundle(
            result,
            release_id=result.release_id,
            registry_path=registry_path,
            bundle_dir=release_store / result.release_id,
            trusted_keys_path=trusted_keys_path,
        )
    else:
        # Backward-compatible slice-11 local checks.
        _check_bundle_exists(result, release_store, result.release_id)
        _check_bundle_manifest_present(result, release_store, result.release_id)

    return result
