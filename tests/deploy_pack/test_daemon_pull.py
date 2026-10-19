# tests/deploy_pack/test_daemon_pull.py

"""Unit tests for ``daemon.pull`` (spec v1.3 Section 12.5).

Every test scripts the rsync transport: either a successful run that
populates the staging directory, or a scripted failure. The pull
verification, atomic rename, and journal integration are exercised
against real files under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from minecraft.deploy_pack.daemon.journal import DeploymentJournal
from minecraft.deploy_pack.daemon.pull import (
    PullRequest,
    RsyncTransport,
    pull_release,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _request(tmp_path: Path, *, release_id: str = "v1.0.0", **overrides: object) -> PullRequest:
    """Build a PullRequest rooted under ``tmp_path``."""
    defaults: dict[str, object] = {
        "source_host": "dev.example.com",
        "source_user": "deploy",
        "source_path": Path("/srv/minecraft-dev/www/releases"),
        "ssh_key": tmp_path / "id_deploy",
        "target_root": tmp_path / "releases",
        "release_id": release_id,
    }
    defaults.update(overrides)
    return PullRequest(**defaults)  # type: ignore[arg-type]


def _slip_content() -> bytes:
    """Return an arbitrary packing-slip byte payload."""
    return b'packing_slip_version = 1\nrelease_id = "v1.0.0"\n'


def _bundle_files(slip_content: bytes | None = None) -> dict[str, bytes]:
    """Return ``{relpath: content}`` for a minimal valid bundle.

    The manifest does not list itself or its sidecar signature -- both
    are exempt from the extra-files check.
    """
    content = slip_content if slip_content is not None else _slip_content()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "slip_content_sha256": "0" * 64,
        "approval_sha256": "0" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": [
            {
                "path": "packing_slip.toml",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return {
        "packing_slip.toml": content,
        "bundle-manifest.json": manifest_bytes,
    }


def _populating_transport(files: dict[str, bytes]) -> RsyncTransport:
    """Return a transport that writes ``files`` into the rsync destination."""

    def _transport(args: list[str], _timeout: float) -> tuple[int, str, str]:
        dest = Path(args[-1].rstrip("/"))
        dest.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return (0, "", "")

    return _transport


def _failing_transport(rc: int = 23, stderr: str = "rsync: [receiver] connection refused") -> RsyncTransport:
    """Return a transport that fails with the given rc and stderr."""

    def _transport(_args: list[str], _timeout: float) -> tuple[int, str, str]:
        return (rc, "", stderr)

    return _transport


def _raising_transport() -> RsyncTransport:
    """Return a transport that raises (simulates a crash in the seam)."""

    def _transport(_args: list[str], _timeout: float) -> tuple[int, str, str]:
        raise RuntimeError("simulated transport crash")

    return _transport


def _journal(tmp_path: Path) -> DeploymentJournal:
    """Return a journal rooted under ``tmp_path``."""
    return DeploymentJournal(tmp_path / "journal.jsonl")


def _stages(journal: DeploymentJournal) -> list[str]:
    """Return the stage-value list in file order."""
    return [r.stage.value for r in journal.read_all()]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_commits_release(tmp_path: Path) -> None:
    """A valid bundle is pulled, verified, and committed to <release>/."""
    files = _bundle_files()
    request = _request(tmp_path)
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is True
    assert result.manifest_verified is True
    assert result.final_dir.is_dir()
    assert (result.final_dir / "packing_slip.toml").is_file()
    assert (result.final_dir / "bundle-manifest.json").is_file()
    assert not result.staging_dir.exists()


def test_happy_path_journals_started_and_completed(tmp_path: Path) -> None:
    """Journal records PULL_STARTED then PULL_COMPLETED on success."""
    files = _bundle_files()
    journal = _journal(tmp_path)
    pull_release(
        request=_request(tmp_path),
        journal=journal,
        run_id="run-1",
        transport=_populating_transport(files),
    )
    assert _stages(journal) == ["pull_started", "pull_completed"]


def test_stale_staging_is_cleaned_before_pull(tmp_path: Path) -> None:
    """Garbage in the staging dir from a prior failed attempt is removed."""
    files = _bundle_files()
    request = _request(tmp_path)
    request.staging_dir.mkdir(parents=True)
    (request.staging_dir / "garbage.txt").write_bytes(b"leftover")
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is True
    assert not (result.final_dir / "garbage.txt").exists()


def test_expected_manifest_sha_matches(tmp_path: Path) -> None:
    """A matching caller-supplied manifest sha passes verification."""
    files = _bundle_files()
    expected = hashlib.sha256(files["bundle-manifest.json"]).hexdigest()
    request = _request(tmp_path, expected_manifest_sha256=expected)
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is True


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


def test_rsync_nonzero_exit_fails_and_cleans_staging(tmp_path: Path) -> None:
    """A non-zero rsync exit yields failure and cleans staging."""
    request = _request(tmp_path)
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_failing_transport(rc=23),
    )

    assert result.success is False
    assert "exit 23" in (result.failure_message or "")
    assert not request.final_dir.exists()
    assert not request.staging_dir.exists()


def test_rsync_nonzero_journals_failed(tmp_path: Path) -> None:
    """A non-zero rsync exit journals PULL_STARTED then FAILED."""
    journal = _journal(tmp_path)
    pull_release(
        request=_request(tmp_path),
        journal=journal,
        run_id="run-1",
        transport=_failing_transport(),
    )
    assert _stages(journal) == ["pull_started", "failed"]


def test_transport_exception_is_captured(tmp_path: Path) -> None:
    """An exception from the transport is captured as failure."""
    request = _request(tmp_path)
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_raising_transport(),
    )

    assert result.success is False
    assert "raised" in (result.failure_message or "")


# ---------------------------------------------------------------------------
# Verification failures
# ---------------------------------------------------------------------------


def test_missing_manifest_fails(tmp_path: Path) -> None:
    """A staged bundle without a manifest fails verification."""
    files = {"packing_slip.toml": _slip_content()}  # no manifest
    request = _request(tmp_path)
    journal = _journal(tmp_path)

    result = pull_release(
        request=request,
        journal=journal,
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "bundle-manifest.json missing" in (result.failure_message or "")


def test_malformed_manifest_fails(tmp_path: Path) -> None:
    """A staged bundle with a non-JSON manifest fails."""
    files = {"bundle-manifest.json": b"not json"}
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "malformed" in (result.failure_message or "")


def test_missing_listed_file_fails(tmp_path: Path) -> None:
    """A manifest entry whose file is absent fails verification."""
    content = _slip_content()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "slip_content_sha256": "0" * 64,
        "approval_sha256": "0" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": [
            {
                "path": "absent.toml",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    files = {
        "packing_slip.toml": content,
        "bundle-manifest.json": (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    }
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "staged file missing: absent.toml" in (result.failure_message or "")


def test_size_mismatch_fails(tmp_path: Path) -> None:
    """A manifest entry whose size does not match the file fails."""
    content = _slip_content()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "slip_content_sha256": "0" * 64,
        "approval_sha256": "0" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": [
            {
                "path": "packing_slip.toml",
                "size_bytes": len(content) + 999,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    files = {
        "packing_slip.toml": content,
        "bundle-manifest.json": (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    }
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "size mismatch" in (result.failure_message or "")


def test_sha_mismatch_fails(tmp_path: Path) -> None:
    """A manifest entry whose SHA-256 does not match the file fails."""
    content = _slip_content()
    manifest = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "slip_content_sha256": "0" * 64,
        "approval_sha256": "0" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": [
            {
                "path": "packing_slip.toml",
                "size_bytes": len(content),
                "sha256": "0" * 64,  # wrong
            }
        ],
    }
    files = {
        "packing_slip.toml": content,
        "bundle-manifest.json": (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    }
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "sha256 mismatch" in (result.failure_message or "")


def test_extra_file_fails(tmp_path: Path) -> None:
    """A staged file not listed in the manifest fails verification."""
    files = _bundle_files()
    files["unexpected.txt"] = b"should not be here"
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "not listed in manifest" in (result.failure_message or "")


def test_expected_manifest_sha_mismatch_fails(tmp_path: Path) -> None:
    """A caller-supplied manifest sha that does not match fails."""
    files = _bundle_files()
    request = _request(tmp_path, expected_manifest_sha256="f" * 64)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is False
    assert "bundle-manifest.json sha256 mismatch" in (result.failure_message or "")


def test_sig_sidecar_is_exempt_from_extras_check(tmp_path: Path) -> None:
    """``bundle-manifest.sig`` is allowed even though it is not in the manifest."""
    files = _bundle_files()
    files["bundle-manifest.sig"] = b"base64signature\n"
    request = _request(tmp_path)

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(files),
    )

    assert result.success is True


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_existing_release_dir_is_refused(tmp_path: Path) -> None:
    """A pre-existing <release>/ directory is refused, not overwritten."""
    request = _request(tmp_path)
    request.final_dir.mkdir(parents=True)
    (request.final_dir / "existing.txt").write_bytes(b"pre-existing")

    result = pull_release(
        request=request,
        journal=_journal(tmp_path),
        run_id="run-1",
        transport=_populating_transport(_bundle_files()),
    )

    assert result.success is False
    assert "already present" in (result.failure_message or "")
    assert (request.final_dir / "existing.txt").read_bytes() == b"pre-existing"
