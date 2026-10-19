# src/minecraft/deploy_pack/daemon/pull.py

"""Pull a release bundle from DEV via rsync-over-SSH (spec v1.3 Section 12.5).

Prod initiates the pull. DEV exposes its publication directory as a
read-only rsync target; the SSH key is restricted to a forced command
running rsync, rooted at the publication directory. DEV never initiates
a transfer to prod.

Pull lifecycle
--------------
1. Clean any stale staging directory from a previous failed attempt.
2. Journal ``PULL_STARTED``.
3. Invoke rsync, staging to ``<target_root>/<release>.partial/``.
4. Verify the staged bundle against its own ``bundle-manifest.json``:
   every listed file exists with matching size and SHA-256, no extra
   files exist, and (when the caller supplies one) the manifest bytes
   hash to the caller-expected value (typically the registry entry's
   ``bundle_manifest_sha256``).
5. Atomic rename ``<release>.partial/`` -> ``<release>/``.
6. Journal ``PULL_COMPLETED``.

Any failure short-circuits: staging is cleaned up and the journal
records ``FAILED`` so recovery knows the pull never completed.

Rsync transport seam
--------------------
The rsync invocation is behind an injectable
:data:`RsyncTransport` callable. Tests script success, failure, and
side effects without spawning subprocesses. Production uses
:func:`_default_transport`, which shells out to rsync and captures
stdout/stderr/return code.

Manifest immutability
---------------------
If ``<release>/`` already exists, the pull refuses rather than
overwriting. Spec Section 15 requires release bundles to be immutable once
published; a re-pull of an existing release is an operator error, not a
recovery scenario.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from LoggingCore import get_logger

from .journal import DeploymentJournal, Stage

__all__ = ["PullRequest", "PullResult", "RsyncTransport", "pull_release"]


_log = get_logger(__name__)


#: Callable signature for the rsync transport seam.
#: Receives ``(args, timeout)``; returns ``(returncode, stdout, stderr)``.
RsyncTransport = Callable[[list[str], float], tuple[int, str, str]]


#: Filenames exempt from the "extra files not in manifest" check.
#: ``bundle-manifest.json`` is the manifest itself; ``bundle-manifest.sig``
#: is the sidecar signature. Neither can honestly appear in the manifest.
_MANIFEST_EXEMPT: frozenset[str] = frozenset({"bundle-manifest.json", "bundle-manifest.sig"})


@dataclass(frozen=True)
class PullRequest:
    """Everything needed to pull one release bundle from DEV."""

    source_host: str
    source_user: str
    source_path: Path
    ssh_key: Path
    target_root: Path
    release_id: str
    timeout_seconds: int = 1800
    expected_manifest_sha256: str | None = None
    ssh_options: tuple[str, ...] = ()

    @property
    def staging_dir(self) -> Path:
        """Return the transient staging directory for this release."""
        return self.target_root / f"{self.release_id}.partial"

    @property
    def final_dir(self) -> Path:
        """Return the final release directory."""
        return self.target_root / self.release_id

    @property
    def rsync_source(self) -> str:
        """Return the rsync source argument.

        The remote forced command prepends ``source_path`` to this
        argument, so the client sends only the release-relative portion.
        """
        return f"{self.source_user}@{self.source_host}:{self.release_id}/"


@dataclass
class PullResult:
    """Outcome of :func:`pull_release`."""

    success: bool
    release_id: str
    final_dir: Path
    staging_dir: Path
    failure_message: str | None = None
    manifest_verified: bool = False


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _default_transport(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run ``args`` as a subprocess; return ``(rc, stdout, stderr)``.

    Missing binary yields rc 127; timeout yields rc 124. Both mimic the
    shell convention so the caller's failure classification is
    symmetric with a failed rsync.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return (127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return (124, "", str(exc))
    return (result.returncode, result.stdout, result.stderr)


def _build_ssh_command(request: PullRequest) -> str:
    """Return the ``-e`` argument for rsync.

    Quoted so a key path containing spaces would still work; the daemon's
    default layout does not require it, but quoting is correct.
    """
    parts = [
        "ssh",
        "-i",
        str(request.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        *request.ssh_options,
    ]
    return " ".join(shlex.quote(part) for part in parts)


def _build_rsync_args(request: PullRequest) -> list[str]:
    """Return the full rsync argv for one pull."""
    return [
        "rsync",
        "-az",
        "--delete",
        "--checksum",
        "-e",
        _build_ssh_command(request),
        request.rsync_source,
        str(request.staging_dir) + "/",
    ]


# ---------------------------------------------------------------------------
# Bundle verification
# ---------------------------------------------------------------------------


def _cleanup(path: Path) -> None:
    """Remove ``path`` recursively; silent on errors or absence."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _verify_manifest_bytes(manifest_bytes: bytes, expected_sha256: str | None) -> str | None:
    """Return an error string if the manifest bytes hash to the wrong value."""
    if expected_sha256 is None:
        return None
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if actual != expected_sha256:
        return f"bundle-manifest.json sha256 mismatch: expected {expected_sha256} actual {actual}"
    return None


def _verify_one_entry(staging_dir: Path, entry: object) -> str | None:
    """Verify one manifest ``files`` entry against the staged file."""
    if not isinstance(entry, dict):
        return f"bundle-manifest.json files entry is not an object: {entry!r}"
    rel = entry.get("path")
    expected_sha = entry.get("sha256")
    expected_size = entry.get("size_bytes")
    if not isinstance(rel, str) or not isinstance(expected_sha, str) or not isinstance(expected_size, int):
        return f"bundle-manifest.json malformed entry: {entry!r}"
    file_path = staging_dir / rel
    if not file_path.is_file():
        return f"staged file missing: {rel}"
    actual_size = file_path.stat().st_size
    if actual_size != expected_size:
        return f"size mismatch for {rel}: expected {expected_size} actual {actual_size}"
    actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        return f"sha256 mismatch for {rel}: expected {expected_sha} actual {actual_sha}"
    return None


def _verify_no_extras(staging_dir: Path, expected_paths: set[str]) -> str | None:
    """Return an error string if any staged file is not in the manifest."""
    for entry in staging_dir.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(staging_dir).as_posix()
        if rel in _MANIFEST_EXEMPT:
            continue
        if rel not in expected_paths:
            return f"staged file not listed in manifest: {rel}"
    return None


def _verify_bundle(staging_dir: Path, expected_manifest_sha256: str | None) -> str | None:
    """Verify the staged bundle against its manifest.

    Returns ``None`` on success, a human-readable error string otherwise.
    """
    manifest_path = staging_dir / "bundle-manifest.json"
    if not manifest_path.is_file():
        return "bundle-manifest.json missing from staged bundle"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        return f"bundle-manifest.json unreadable: {exc}"

    bytes_error = _verify_manifest_bytes(manifest_bytes, expected_manifest_sha256)
    if bytes_error is not None:
        return bytes_error

    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        return f"bundle-manifest.json malformed: {exc}"
    if not isinstance(manifest, dict):
        return "bundle-manifest.json is not an object"
    files = manifest.get("files")
    if not isinstance(files, list):
        return "bundle-manifest.json has no files list"

    expected_paths: set[str] = set()
    for entry in files:
        error = _verify_one_entry(staging_dir, entry)
        if error is not None:
            return error
        assert isinstance(entry, dict)
        expected_paths.add(str(entry["path"]))

    return _verify_no_extras(staging_dir, expected_paths)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _fail(
    request: PullRequest,
    journal: DeploymentJournal,
    run_id: str,
    message: str,
) -> PullResult:
    """Record a terminal failure, clean staging, and return the failed result."""
    _cleanup(request.staging_dir)
    journal.append_stage(
        run_id=run_id,
        release_id=request.release_id,
        stage=Stage.FAILED,
        detail=message,
    )
    _log.error(f"pull: {request.release_id!r} failed: {message}")
    return PullResult(
        success=False,
        release_id=request.release_id,
        final_dir=request.final_dir,
        staging_dir=request.staging_dir,
        failure_message=message,
    )


def pull_release(
    *,
    request: PullRequest,
    journal: DeploymentJournal,
    run_id: str,
    transport: RsyncTransport | None = None,
) -> PullResult:
    """Pull a release bundle from DEV, verify it, and atomically commit it.

    Never raises on operational failure: the result carries the outcome
    and the journal records the terminal stage. Exceptions from the
    transport are captured and returned as failures.

    Refuses if ``<target_root>/<release>/`` already exists (spec Section 15,
    bundles are immutable).
    """
    effective_transport = transport if transport is not None else _default_transport

    if request.final_dir.exists():
        return _fail(
            request,
            journal,
            run_id,
            f"release {request.release_id!r} already present at {request.final_dir}",
        )

    _cleanup(request.staging_dir)

    journal.append_stage(
        run_id=run_id,
        release_id=request.release_id,
        stage=Stage.PULL_STARTED,
        detail=f"source={request.source_user}@{request.source_host}:{request.release_id}",
    )

    args = _build_rsync_args(request)
    _log.info(f"pull: {request.release_id!r} from {request.source_user}@{request.source_host}")
    try:
        rc, _stdout, stderr = effective_transport(args, float(request.timeout_seconds))
    except Exception as exc:
        return _fail(request, journal, run_id, f"rsync invocation raised: {exc}")

    if rc != 0:
        return _fail(request, journal, run_id, f"rsync failed with exit {rc}: {stderr.strip()[:300]}")

    verify_error = _verify_bundle(request.staging_dir, request.expected_manifest_sha256)
    if verify_error is not None:
        return _fail(request, journal, run_id, verify_error)

    try:
        request.staging_dir.replace(request.final_dir)
    except OSError as exc:
        return _fail(request, journal, run_id, f"could not finalize pull: {exc}")

    journal.append_stage(
        run_id=run_id,
        release_id=request.release_id,
        stage=Stage.PULL_COMPLETED,
        detail=f"final={request.final_dir}",
    )
    _log.info(f"pull: {request.release_id!r} committed to {request.final_dir}")
    return PullResult(
        success=True,
        release_id=request.release_id,
        final_dir=request.final_dir,
        staging_dir=request.staging_dir,
        manifest_verified=True,
    )
