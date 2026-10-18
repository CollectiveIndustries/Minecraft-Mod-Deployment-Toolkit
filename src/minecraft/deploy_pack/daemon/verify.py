# src/minecraft/deploy_pack/daemon/verify.py

"""Release verification for the Minecraft deploy daemon.

Local checks only; registry chain and bundle-manifest signature
verification land in later slices. This module returns a plain
:class:`VerificationResult` describing every check performed and any
reasons the release is blocked. It does not raise on verification
failure -- every failure is a block.

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
    SlipError,
)

__all__ = ["VerificationResult", "verify_release"]


@dataclass
class VerificationResult:
    """Outcome of a verification pass."""

    status: Literal["ok", "blocked"] = "ok"
    release_id: str | None = None
    content_sha256: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        """Record ``reason`` and flip status to blocked."""
        self.reasons.append(reason)
        self.status = "blocked"


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
    """Populate the ``bundle_exists`` check."""
    bundle_root = release_store / release_id
    exists = bundle_root.is_dir() and (bundle_root / "artifacts").is_dir()
    result.checks["bundle_exists"] = exists
    if not exists:
        result.block("bundle_missing")


def _check_bundle_manifest_present(result: VerificationResult, release_store: Path, release_id: str) -> None:
    """Populate the ``bundle_manifest_present`` check (existence only for now)."""
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


def verify_release(
    *,
    release_id: str | None,
    release_pointer: str | None,
    expected_commit_sha: str | None,
    expected_slip_sha256: str | None,
    slip_dir: Path,
    release_store: Path,
) -> VerificationResult:
    """Run the full local verification pass.

    Returns :class:`VerificationResult` with status ``ok`` or ``blocked``.
    Does not raise on verification failure -- every failure is a block.
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
    _check_bundle_exists(result, release_store, result.release_id)
    _check_bundle_manifest_present(result, release_store, result.release_id)
    return result
