# src/minecraft/modpack_upgrade/cli/commands/publish.py

"""``minecraft-upgrade publish <release-id>`` -- immutable release bundle.

Section 15.1.8 / Section 6 / Section 7 of Upgrade_System_specifications.
Takes the operator-chosen tag (which is the release slip's
``release_id``), verifies the release slip, assembles a bundle from the
warehouse + sync trees, writes the three manifests plus ``approval.json``,
records git provenance, atomically commits the bundle to
``www/releases/<release-id>/``, and appends one entry to the release
registry at ``sync/packing_slips/release-index.jsonl``.

Preconditions and pipeline:

  * **T1** -- slip exists; slip's ``content_sha256`` verifies; slip's
    ``release_id`` matches the argument; no bundle directory already
    exists at ``www/releases/<release-id>/`` (Section 6.2).
  * **T2** -- every ``[[jar]]``, ``[[resource_pack]]``, and
    ``[[velocity_plugin]]`` exists in the warehouse with matching
    SHA-256 (Section 6.3 #4). Filenames validated against path
    traversal (defense-in-depth).
  * **T3** -- ``sync/config/`` and ``sync/kubejs/`` are walked (refusing
    symlinks); artifacts copied; full staging tree assembled in
    ``www/releases/<release-id>.staging/``.
  * **T4** -- the three manifest JSON files written into staging;
    staging verified against the manifest; staging atomically committed
    to ``www/releases/<release-id>/`` via ``Path.replace``.
  * **T5** -- ``approval.json`` sourced from the materialized run's
    ``acknowledgment.json`` (or auto-generated for loss-free runs);
    ``bundle-manifest.json`` extended with ``approval_sha256``,
    ``release_tag_object_sha``, ``release_commit_sha``; git tag object
    and commit SHAs resolved via ``git rev-parse``; one entry appended
    to ``release-index.jsonl`` with a chained ``entry_hash``.

Run discovery
-------------

``publish`` receives a tag, not a run-id. The slip does not record its
originating run; ``promote`` overwrites ``release_id`` to the tag and
does not store the run-id elsewhere. To find the approval source,
``publish`` scans ``<state_dir>/runs/*/state.toml`` for the run whose
``resolution_inputs_sha256`` matches
``slip.fingerprints.resolution_inputs_sha256`` **and** whose
``status == "materialized"``.

Under normal usage this is unique: one update+materialize cycle per
release. If two cycles ran with identical resolver inputs (same
manifest, same seed, same overrides) they share
``resolution_inputs_sha256``, and publish refuses with exit 3 rather
than picking arbitrarily. A ``--run-id`` override or a promote-sidecar
file lands in a later slice if this becomes common in practice.

Approval sources
----------------

Two cases:

  * **Lossy run** (``no_version="ask"`` with losses): the run went
    through ``acknowledge``, which wrote ``acknowledgment.json``.
    Publish copies that file verbatim into ``approval.json``.
  * **Loss-free run** (no losses, or ``no_version="drop"``): no
    ``acknowledgment.json`` exists. Publish auto-generates an
    ``approval.json`` with ``acknowledged_losses: []``,
    ``acknowledged_by: "auto"``, and a note explaining why no explicit
    acknowledgment was required.

Both cases yield a valid ``approval.json``; the operator-facing
difference is the ``acknowledged_by`` value.

Registry and chain
------------------

One JSON line appended to ``sync/packing_slips/release-index.jsonl``.
The entry's ``previous_entry_hash`` is the last line's ``entry_hash``
(or 64 zeroes for the first entry). ``entry_hash`` is the SHA-256 of a
canonical JSON projection over the chain-relevant fields -- the same
computation a future chain verifier will perform (Section 7.3).

Signing is deferred: ``signature`` and ``key_id`` are both
``"UNSIGNED"`` in this build. The spec's signed-manifest model
(Section 6.4) writes ``bundle-manifest.sig`` alongside
``bundle-manifest.json``; this build keeps the placeholder as a field
inside ``bundle-manifest.json`` for symmetry with the current test
surface. A future signing slice will switch to the sidecar file.

Deferred until the underlying features exist:

  * Runtime payload (Section 6.3 #6) -- no runtime payload producer yet.
  * Client ZIP verification (Section 6.3 #5) -- no client ZIP yet;
    ``client-distribution-manifest.json`` is a placeholder.
  * ``[inputs.git]`` tree enforcement (Section 6.3 #7) -- prod-deploy
    concern (Section 5.3 rule 4).
  * Signature and key_id real values (Section 6.4, Section 7.5) -- crypto
    signing is a separate slice.

Exit codes:

  * ``0`` -- publish succeeded end-to-end: bundle committed, registry
    appended.
  * ``3`` -- a precondition, assembly, manifest, git-ref, or
    verification failure. Staging is cleaned up; no bundle is committed.
  * ``7`` -- registry append failure *after* a bundle was committed.
    The bundle is rolled back on a best-effort basis; if rollback
    fails too, the operator must remove ``www/releases/<id>/``
    manually.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from LoggingCore import get_logger
from PlatformCore.subprocess import run_git

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.deploy_pack.files import compute_sha256
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.slip import PackingSlip, load_slip
from minecraft.modpack_upgrade.state import RunState, load_state

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.publish")


# Bundle filenames. Kept as constants because both writers and any future
# reader (prod daemon, registry verifier) must agree on the exact names.
_BUNDLE_MANIFEST = "bundle-manifest.json"
_ARTIFACT_MANIFEST = "artifact-manifest.json"
_CLIENT_DISTRIBUTION_MANIFEST = "client-distribution-manifest.json"
_APPROVAL = "approval.json"
_MANIFEST_NAMES: frozenset[str] = frozenset({_BUNDLE_MANIFEST, _ARTIFACT_MANIFEST, _CLIENT_DISTRIBUTION_MANIFEST})

# Registry chain genesis sentinel. Matches the ``"0" * 64`` convention
# used by every other "absent" SHA-256 in this codebase.
_GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Timestamp
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _state_dir(config_dir: Path) -> Path:
    """Return the daemon state directory."""
    return Path(load_daemon_config(config_dir).state_dir)


def _slip_path(config_dir: Path, release_id: str) -> Path:
    """Return the release slip path for ``release_id``."""
    daemon_config = load_daemon_config(config_dir)
    return Path(daemon_config.slip_dir) / f"{release_id}.toml"


def _bundle_dir(project_root: Path, release_id: str) -> Path:
    """Return the immutable bundle destination for ``release_id``."""
    return project_root / "www" / "releases" / release_id


def _staging_dir(project_root: Path, release_id: str) -> Path:
    """Return the transient staging directory for ``release_id``."""
    return project_root / "www" / "releases" / f"{release_id}.staging"


def _warehouse_dir(project_root: Path) -> Path:
    """Return the warehouse directory (``sync/downloads/``)."""
    return project_root / "sync" / "downloads"


def _config_source(project_root: Path) -> Path:
    """Return the source config snapshot directory (``sync/config/``)."""
    return project_root / "sync" / "config"


def _kubejs_source(project_root: Path) -> Path:
    """Return the source kubejs snapshot directory (``sync/kubejs/``)."""
    return project_root / "sync" / "kubejs"


def _registry_path(project_root: Path) -> Path:
    """Return the release registry path."""
    return project_root / "sync" / "packing_slips" / "release-index.jsonl"


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def _require_slip_verifies(slip_path: Path) -> PackingSlip:
    """Return the parsed slip; raise ``UpgradeCliError(3)`` on any problem."""
    if not slip_path.is_file():
        raise UpgradeCliError(3, f"slip not found: {slip_path}")
    try:
        return load_slip(slip_path)
    except Exception as exc:
        raise UpgradeCliError(3, f"slip is not verifiable: {slip_path}: {exc}") from exc


def _require_release_id_matches(slip: PackingSlip, release_id: str, slip_path: Path) -> None:
    """Raise ``UpgradeCliError(3)`` when the slip's ``release_id`` != argument."""
    if slip.release_id != release_id:
        raise UpgradeCliError(
            3,
            f"slip {slip_path} declares release_id={slip.release_id!r}, "
            f"but publish was called with release_id={release_id!r}. "
            f"Release slips must have release_id == tag "
            f"(Upgrade_System_specifications Section 5.2, Section 5.3 rule 1).",
        )


def _require_safe_filename(filename: str) -> None:
    """Raise ``UpgradeCliError(3)`` on a filename that could escape its directory."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", "..") or "\x00" in filename:
        raise UpgradeCliError(
            3,
            f"unsafe artifact filename: {filename!r}. Filenames must be plain basenames with no path separators, no '.' or '..', and no NUL bytes.",
        )


def _verify_artifact(
    warehouse_dir: Path,
    filename: str,
    expected_sha256: str,
    *,
    kind: str,
) -> None:
    """Verify one artifact exists with the expected SHA-256 (Section 6.3 #4)."""
    artifact_path = warehouse_dir / filename
    if not artifact_path.is_file():
        raise UpgradeCliError(
            3,
            f"artifact missing: {kind} {filename!r} not found at {artifact_path}. Section 6.3 #4 requires every slip artifact in the warehouse.",
        )
    actual = compute_sha256(artifact_path)
    if actual != expected_sha256:
        raise UpgradeCliError(
            3,
            f"artifact hash mismatch: {kind} {filename!r} slip={expected_sha256} actual={actual}. Section 6.3 #4 requires exact artifact identity.",
        )


def _require_artifacts_present(slip: PackingSlip, warehouse_dir: Path) -> None:
    """Verify every artifact in ``slip`` exists with its recorded SHA-256."""
    for jar in slip.jars:
        _require_safe_filename(jar.filename)
        _verify_artifact(warehouse_dir, jar.filename, jar.sha256, kind="jar")
    for rp in slip.resource_packs:
        _require_safe_filename(rp.filename)
        _verify_artifact(warehouse_dir, rp.filename, rp.sha256, kind="resource pack")
    for vp in slip.velocity_plugins:
        _require_safe_filename(vp.filename)
        _verify_artifact(warehouse_dir, vp.filename, vp.sha256, kind="velocity plugin")


def _require_bundle_absent(bundle_dir: Path, release_id: str) -> None:
    """Raise ``UpgradeCliError(3)`` when ``bundle_dir`` already exists (Section 6.2)."""
    if bundle_dir.exists():
        raise UpgradeCliError(
            3,
            f"release bundle already exists at {bundle_dir}. "
            f"Bundles are immutable; re-publication of {release_id!r} is refused "
            f"(Upgrade_System_specifications Section 6.2). Remove the directory "
            f"manually if this is intentional.",
        )


# ---------------------------------------------------------------------------
# Run discovery (T5)
# ---------------------------------------------------------------------------


def _find_materialized_run_for_slip(slip: PackingSlip, state_dir: Path) -> tuple[Path, RunState]:
    """Return ``(run_dir, state)`` for the materialized run matching ``slip``.

    Scans ``state_dir/runs/*/state.toml`` for entries whose
    ``resolution_inputs_sha256`` matches the slip's own fingerprint AND
    whose ``status`` is ``"materialized"``. Raises ``UpgradeCliError(3)``
    on zero or multiple matches.
    """
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        raise UpgradeCliError(
            3,
            f"no runs directory at {runs_dir}; cannot locate approval source. Was the state directory preserved through promote?",
        )
    expected = slip.fingerprints.resolution_inputs_sha256
    matches: list[tuple[Path, RunState]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        state_path = run_dir / "state.toml"
        if not state_path.is_file():
            continue
        try:
            state = load_state(state_path)
        except Exception:
            continue
        if state.status != "materialized":
            continue
        if state.resolution_inputs_sha256 != expected:
            continue
        matches.append((run_dir, state))

    if not matches:
        raise UpgradeCliError(
            3,
            f"no materialized run found whose resolution_inputs_sha256 matches "
            f"this slip ({expected}). Was the slip promoted from a run whose "
            f"state has since been deleted or moved?",
        )
    if len(matches) > 1:
        run_ids = [s.run_id for _, s in matches]
        raise UpgradeCliError(
            3,
            f"ambiguous: {len(matches)} materialized runs match this slip "
            f"(run_ids: {run_ids}). Multiple update+materialize cycles with "
            f"identical resolver inputs produced multiple matching runs. "
            f"Disambiguate by regenerating with a semantically different input, "
            f"or wait for a --run-id override in a later slice.",
        )
    return matches[0]


def _build_approval(run_dir: Path, state: RunState, timestamp: str) -> dict:
    """Return the approval.json body for this run.

    Lossy runs: use the ``acknowledgment.json`` written by
    ``acknowledge`` verbatim. Loss-free runs: auto-generate a minimal
    record so ``approval.json`` always exists in the bundle (Section 6.2
    requires an approval record; Section 6.3 #3 requires it to be bound
    to candidate identity).
    """
    ack_path = run_dir / "acknowledgment.json"
    if ack_path.is_file():
        try:
            ack = json.loads(ack_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise UpgradeCliError(3, f"malformed acknowledgment.json at {ack_path}: {exc}") from exc
        if not isinstance(ack, dict):
            raise UpgradeCliError(3, f"acknowledgment.json at {ack_path} is not a JSON object")
        return ack
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "acknowledged_losses": [],
        "acknowledged_by": "auto",
        "acknowledged_at_utc": timestamp,
        "note": "no losses required acknowledgment (no_losses or drop policy)",
    }


def _resolve_git_refs(project_root: Path, release_id: str) -> tuple[str, str, str]:
    """Return ``(tag_object_sha, tag_object_format, commit_sha)`` for ``release_id``.

    A lightweight tag produces ``tag_object_sha == commit_sha``; this is
    legal but warns because retargeting is not detectable from the
    recorded SHA alone. ``promote`` prints ``git tag -a``, which produces
    an annotated tag by default.
    """
    try:
        rc, tag_out, _stderr = run_git(
            ["rev-parse", release_id],
            repo_path=str(project_root),
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpgradeCliError(3, f"git rev-parse for tag {release_id!r} failed: {exc}") from exc
    if rc != 0:
        raise UpgradeCliError(
            3,
            f"tag {release_id!r} not found in {project_root}. "
            f"Publish requires the release to be tagged; promote prints "
            f"the git tag command to run before publishing.",
        )
    tag_object_sha = tag_out.strip()
    if len(tag_object_sha) == 40:
        tag_object_format = "sha1"
    elif len(tag_object_sha) == 64:
        tag_object_format = "sha256"
    else:
        raise UpgradeCliError(3, f"unexpected tag SHA length from git: {tag_object_sha!r}")

    try:
        rc, commit_out, stderr = run_git(
            ["rev-parse", f"{release_id}^{{commit}}"],
            repo_path=str(project_root),
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise UpgradeCliError(3, f"git rev-parse for tag commit failed: {exc}") from exc
    if rc != 0:
        raise UpgradeCliError(3, f"could not resolve commit for tag {release_id!r}: {stderr.strip()}")
    commit_sha = commit_out.strip()

    if tag_object_sha == commit_sha:
        _log_cli.warning(
            f"tag {release_id!r} is lightweight; registry will record the "
            f"commit SHA as tag_object_sha. Annotated tags (git tag -a) are "
            f"recommended for stronger provenance (per promote's printed "
            f"instructions)."
        )
    return tag_object_sha, tag_object_format, commit_sha


# ---------------------------------------------------------------------------
# Staging (T3)
# ---------------------------------------------------------------------------


def _assert_no_symlinks(root: Path) -> None:
    """Raise ``UpgradeCliError(3)`` if any entry under ``root`` is a symlink."""
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise UpgradeCliError(
                3,
                f"symlink in source tree: {entry}. Bundle contents must be regular files and directories only (Section 13.2 forbids traversing symlinks).",
            )


def _copy_source_tree(src: Path, dst: Path, *, kind: str) -> None:
    """Copy ``src`` -> ``dst`` after refusing symlinks; skip + warn if ``src`` missing."""
    if not src.is_dir():
        _log_cli.warning(f"source {kind} directory missing: {src}; staging empty {kind}/ directory")
        dst.mkdir(parents=True, exist_ok=False)
        return
    _assert_no_symlinks(src)
    try:
        shutil.copytree(src, dst, symlinks=False)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not copy {kind} {src} -> {dst}: {exc}") from exc


def _copy_artifact(warehouse_dir: Path, artifacts_dir: Path, filename: str, *, kind: str) -> None:
    """Copy one artifact from warehouse -> bundle artifacts dir."""
    src = warehouse_dir / filename
    dst = artifacts_dir / filename
    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not copy {kind} {filename!r} from warehouse: {exc}") from exc


def _copy_artifacts(slip: PackingSlip, warehouse_dir: Path, artifacts_dir: Path) -> None:
    """Copy every slip artifact from warehouse -> ``artifacts_dir``."""
    artifacts_dir.mkdir(parents=True, exist_ok=False)
    for jar in slip.jars:
        _copy_artifact(warehouse_dir, artifacts_dir, jar.filename, kind="jar")
    for rp in slip.resource_packs:
        _copy_artifact(warehouse_dir, artifacts_dir, rp.filename, kind="resource pack")
    for vp in slip.velocity_plugins:
        _copy_artifact(warehouse_dir, artifacts_dir, vp.filename, kind="velocity plugin")


def _stage_bundle(
    staging_dir: Path,
    *,
    slip: PackingSlip,
    slip_path: Path,
    config_src: Path,
    kubejs_src: Path,
    warehouse_dir: Path,
) -> None:
    """Assemble the staging tree (slip + config + kubejs + artifacts).

    Cleanup runs on every failure path so a retry starts from a clean
    state. Manifest writing and the final atomic rename happen in
    :func:`_finalize_bundle`.
    """
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not create staging directory {staging_dir}: {exc}") from exc

    try:
        shutil.copy2(slip_path, staging_dir / "packing_slip.toml")
        _copy_source_tree(config_src, staging_dir / "config", kind="config")
        _copy_source_tree(kubejs_src, staging_dir / "kubejs", kind="kubejs")
        _copy_artifacts(slip, warehouse_dir, staging_dir / "artifacts")
    except UpgradeCliError:
        _cleanup_dir(staging_dir)
        raise
    except OSError as exc:
        _cleanup_dir(staging_dir)
        raise UpgradeCliError(3, f"staging assembly failed at {staging_dir}: {exc}") from exc
    except Exception:
        _cleanup_dir(staging_dir)
        raise


def _cleanup_dir(path: Path) -> None:
    """Best-effort removal of a directory tree; ignore errors."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Manifest construction and atomic commit (T4 + T5)
# ---------------------------------------------------------------------------


def _describe_file(path: Path, relpath: str) -> dict:
    """Return ``{path, size_bytes, sha256}`` for one file."""
    return {
        "path": relpath,
        "size_bytes": path.stat().st_size,
        "sha256": compute_sha256(path),
    }


def _walk_bundle_files(root: Path, *, exclude: frozenset[str]) -> list[dict]:
    """Walk ``root``; return content-file descriptors excluding ``exclude``."""
    out: list[dict] = []
    for entry in sorted(root.rglob("*")):
        if not entry.is_file():
            continue
        rel = entry.relative_to(root).as_posix()
        if rel in exclude:
            continue
        out.append(_describe_file(entry, rel))
    return out


def _build_artifact_entries(staging_dir: Path, slip: PackingSlip) -> list[dict]:
    """Return ``[{kind, path, size_bytes, sha256}]`` for every slip artifact."""
    out: list[dict] = []
    for kind, entries in (
        ("jar", slip.jars),
        ("resource_pack", slip.resource_packs),
        ("velocity_plugin", slip.velocity_plugins),
    ):
        for entry in entries:
            file_path = staging_dir / "artifacts" / entry.filename
            out.append(
                {
                    "kind": kind,
                    "path": f"artifacts/{entry.filename}",
                    "size_bytes": file_path.stat().st_size,
                    "sha256": compute_sha256(file_path),
                }
            )
    return out


def _write_json_atomic(path: Path, body: dict) -> None:
    """Write ``body`` as deterministic pretty JSON to ``path`` atomically."""
    text = json.dumps(body, sort_keys=True, indent=2) + "\n"
    data = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise UpgradeCliError(3, f"could not write {path}: {exc}") from exc


def _verify_manifest_hashes(root: Path, manifest: dict) -> None:
    """Verify every ``manifest['files']`` entry against the on-disk file."""
    for entry in manifest["files"]:
        file_path = root / entry["path"]
        if not file_path.is_file():
            raise UpgradeCliError(3, f"manifest entry missing from staging: {entry['path']}")
        actual_sha = compute_sha256(file_path)
        if actual_sha != entry["sha256"]:
            raise UpgradeCliError(
                3,
                f"manifest hash drift during finalize: {entry['path']} recorded={entry['sha256']} actual={actual_sha}",
            )
        actual_size = file_path.stat().st_size
        if actual_size != entry["size_bytes"]:
            raise UpgradeCliError(
                3,
                f"manifest size drift during finalize: {entry['path']} recorded={entry['size_bytes']} actual={actual_size}",
            )


def _commit_bundle(staging_dir: Path, bundle_dir: Path) -> None:
    """Atomically rename ``staging_dir`` -> ``bundle_dir``."""
    if bundle_dir.exists():
        raise UpgradeCliError(
            3,
            f"bundle destination appeared during staging: {bundle_dir}. Refusing to overwrite (Section 6.2: bundles are immutable).",
        )
    try:
        staging_dir.replace(bundle_dir)
    except OSError as exc:
        raise UpgradeCliError(3, f"could not commit bundle {staging_dir} -> {bundle_dir}: {exc}") from exc


def _finalize_bundle(
    staging_dir: Path,
    bundle_dir: Path,
    *,
    slip: PackingSlip,
    approval_sha256: str,
    release_tag_object_sha: str,
    release_commit_sha: str,
) -> str:
    """Write manifests, verify, atomically commit. Return bundle-manifest SHA-256.

    ``approval.json`` is written by the caller before this function runs,
    so it lands in ``staging_dir`` and appears in the bundle manifest's
    ``files`` list. Order of manifest writes:

      1. ``artifact-manifest.json`` -- structured artifact view.
      2. ``client-distribution-manifest.json`` -- placeholder.
      3. ``bundle-manifest.json`` -- content files + both prior
         manifests + audit fields; excludes itself (self-reference).

    The returned SHA-256 is of ``bundle-manifest.json`` bytes, computed
    before the rename (rename doesn't change bytes).
    """
    content_files = _walk_bundle_files(staging_dir, exclude=_MANIFEST_NAMES)

    artifact_manifest = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "artifacts": _build_artifact_entries(staging_dir, slip),
    }
    _write_json_atomic(staging_dir / _ARTIFACT_MANIFEST, artifact_manifest)

    client_manifest = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "members": [],
        "note": "client distribution not yet implemented (Section 6.3 #5)",
    }
    _write_json_atomic(staging_dir / _CLIENT_DISTRIBUTION_MANIFEST, client_manifest)

    manifest_files = [
        _describe_file(staging_dir / _ARTIFACT_MANIFEST, _ARTIFACT_MANIFEST),
        _describe_file(staging_dir / _CLIENT_DISTRIBUTION_MANIFEST, _CLIENT_DISTRIBUTION_MANIFEST),
    ]
    bundle_manifest = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "slip_content_sha256": slip.content_sha256,
        "approval_sha256": approval_sha256,
        "release_tag_object_sha": release_tag_object_sha,
        "release_commit_sha": release_commit_sha,
        "signature": "UNSIGNED",
        "files": sorted(content_files + manifest_files, key=lambda entry: entry["path"]),
    }
    _write_json_atomic(staging_dir / _BUNDLE_MANIFEST, bundle_manifest)

    manifest_sha = compute_sha256(staging_dir / _BUNDLE_MANIFEST)
    _verify_manifest_hashes(staging_dir, bundle_manifest)
    _commit_bundle(staging_dir, bundle_dir)
    return manifest_sha


# ---------------------------------------------------------------------------
# Registry append (T5)
# ---------------------------------------------------------------------------


def _compute_entry_hash(entry: dict) -> str:
    """Deterministic SHA-256 over the chain-relevant fields of one entry.

    Deliberately excludes ``signature`` and ``key_id`` so that a future
    signing slice can add them without invalidating existing
    ``entry_hash`` values for already-recorded entries.
    """
    chain_fields = {
        "schema_version": entry["schema_version"],
        "release_id": entry["release_id"],
        "tag_object_sha": entry["tag_object_sha"],
        "commit_sha": entry["commit_sha"],
        "slip_content_sha256": entry["slip_content_sha256"],
        "bundle_manifest_sha256": entry["bundle_manifest_sha256"],
        "previous_entry_hash": entry["previous_entry_hash"],
    }
    text = json.dumps(chain_fields, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _last_entry_hash(registry_path: Path) -> str:
    """Return the ``entry_hash`` of the last registry entry, or genesis zeroes."""
    if not registry_path.is_file():
        return _GENESIS_HASH
    last_line = ""
    try:
        with open(registry_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line
    except OSError as exc:
        raise UpgradeCliError(7, f"could not read registry {registry_path}: {exc}") from exc
    if not last_line:
        return _GENESIS_HASH
    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise UpgradeCliError(7, f"registry {registry_path} last line is malformed JSON: {exc}") from exc
    if not isinstance(entry, dict):
        raise UpgradeCliError(7, f"registry {registry_path} last line is not a JSON object")
    entry_hash = entry.get("entry_hash")
    if not isinstance(entry_hash, str) or len(entry_hash) != 64:
        raise UpgradeCliError(
            7,
            f"registry {registry_path} last entry has invalid entry_hash: {entry_hash!r}",
        )
    return entry_hash


def _append_registry_entry(
    registry_path: Path,
    *,
    slip: PackingSlip,
    tag_object_sha: str,
    tag_object_format: str,
    commit_sha: str,
    bundle_manifest_sha256: str,
) -> str:
    """Append one entry to ``release-index.jsonl``; return its ``entry_hash``.

    Raises :class:`UpgradeCliError(7)` on any I/O failure. The caller is
    responsible for rolling back the committed bundle if this raises.
    """
    previous = _last_entry_hash(registry_path)
    entry = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "tag_object_sha": tag_object_sha,
        "tag_object_format": tag_object_format,
        "commit_sha": commit_sha,
        "slip_content_sha256": slip.content_sha256,
        "bundle_manifest_sha256": bundle_manifest_sha256,
        "previous_entry_hash": previous,
        "signature": "UNSIGNED",
        "key_id": "UNSIGNED",
    }
    entry["entry_hash"] = _compute_entry_hash(entry)
    line = json.dumps(entry, sort_keys=True) + "\n"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(registry_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        raise UpgradeCliError(7, f"could not append to registry {registry_path}: {exc}") from exc
    return entry["entry_hash"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_preconditions_summary(slip: PackingSlip, slip_path: Path, bundle_dir: Path) -> None:
    """Print what was verified and where the bundle would go."""
    runtime = slip.runtime
    _log_cli.info(f"slip verified: {slip_path}")
    _log_cli.info(f"  release_id: {slip.release_id}")
    _log_cli.info(f"  release_mode: {slip.release_mode}")
    _log_cli.info(f"  runtime: MC {runtime.minecraft_version} / {runtime.loader_type} {runtime.loader_version}")
    _log_cli.info(
        f"  artifacts verified: {len(slip.jars)} jar(s), {len(slip.resource_packs)} resource pack(s), {len(slip.velocity_plugins)} velocity plugin(s)"
    )
    _log_cli.info("")
    _log_cli.info(f"bundle destination: {bundle_dir}")
    _log_cli.info("")


def _print_staging_summary(staging_dir: Path) -> None:
    """Print what was staged."""
    config_files = sum(1 for f in (staging_dir / "config").rglob("*") if f.is_file())
    kubejs_files = sum(1 for f in (staging_dir / "kubejs").rglob("*") if f.is_file())
    artifact_files = sum(1 for f in (staging_dir / "artifacts").iterdir() if f.is_file())
    _log_cli.info(f"bundle staged: {staging_dir}")
    _log_cli.info("  packing_slip.toml: 1 file")
    _log_cli.info("  approval.json: 1 file")
    _log_cli.info(f"  config/: {config_files} file(s)")
    _log_cli.info(f"  kubejs/: {kubejs_files} file(s)")
    _log_cli.info(f"  artifacts/: {artifact_files} file(s)")
    _log_cli.info("")


def _print_committed_summary(bundle_dir: Path, registry_path: Path, entry_hash: str) -> None:
    """Print the committed bundle location, file count, and registry entry."""
    file_count = sum(1 for f in bundle_dir.rglob("*") if f.is_file())
    _log_cli.info(f"bundle committed: {bundle_dir}")
    _log_cli.info(f"  files: {file_count}")
    _log_cli.info(f"  manifest: {bundle_dir / _BUNDLE_MANIFEST}")
    _log_cli.info("")
    _log_cli.info(f"registry entry appended: {registry_path}")
    _log_cli.info(f"  entry_hash: {entry_hash}")
    _log_cli.info("")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(*, config_dir: Path, project_root: Path, release_id: str) -> int:
    """Execute ``minecraft-upgrade publish``. Returns 0 on success, 3, or 7."""
    slip_path = _slip_path(config_dir, release_id)
    bundle_dir = _bundle_dir(project_root, release_id)
    staging_dir = _staging_dir(project_root, release_id)
    warehouse_dir = _warehouse_dir(project_root)
    config_src = _config_source(project_root)
    kubejs_src = _kubejs_source(project_root)
    state_dir = _state_dir(config_dir)
    registry_path = _registry_path(project_root)

    slip = _require_slip_verifies(slip_path)
    _require_release_id_matches(slip, release_id, slip_path)
    _require_artifacts_present(slip, warehouse_dir)
    _require_bundle_absent(bundle_dir, release_id)

    _print_preconditions_summary(slip, slip_path, bundle_dir)

    _cleanup_dir(staging_dir)

    try:
        _stage_bundle(
            staging_dir,
            slip=slip,
            slip_path=slip_path,
            config_src=config_src,
            kubejs_src=kubejs_src,
            warehouse_dir=warehouse_dir,
        )

        # T5: locate the materialized run, write approval.json.
        run_dir, run_state = _find_materialized_run_for_slip(slip, state_dir)
        approval_body = _build_approval(run_dir, run_state, _now_utc())
        _write_json_atomic(staging_dir / _APPROVAL, approval_body)
        approval_sha = compute_sha256(staging_dir / _APPROVAL)

        # T5: resolve git provenance.
        tag_object_sha, tag_object_format, commit_sha = _resolve_git_refs(project_root, release_id)

        _print_staging_summary(staging_dir)
        bundle_manifest_sha = _finalize_bundle(
            staging_dir,
            bundle_dir,
            slip=slip,
            approval_sha256=approval_sha,
            release_tag_object_sha=tag_object_sha,
            release_commit_sha=commit_sha,
        )
    finally:
        _cleanup_dir(staging_dir)

    # Registry append last; roll back the committed bundle on failure
    # so a retry starts clean.
    try:
        entry_hash = _append_registry_entry(
            registry_path,
            slip=slip,
            tag_object_sha=tag_object_sha,
            tag_object_format=tag_object_format,
            commit_sha=commit_sha,
            bundle_manifest_sha256=bundle_manifest_sha,
        )
    except UpgradeCliError:
        try:
            shutil.rmtree(bundle_dir)
            _log_cli.warning(f"rolled back bundle commit at {bundle_dir} after registry failure")
        except OSError as rollback_exc:
            _log_cli.error(
                f"registry append failed AND rollback failed: bundle remains at "
                f"{bundle_dir} but is unregistered. Manual cleanup required. "
                f"Rollback error: {rollback_exc}"
            )
        raise

    _print_committed_summary(bundle_dir, registry_path, entry_hash)
    return 0
