# src/minecraft/modpack_upgrade/cli/commands/publish.py

"""``minecraft-upgrade publish <release-id>`` -- immutable signed release bundle.

Section 15.1.8 / Section 6 / Section 7 of Upgrade_System_specifications.
Takes the operator-chosen tag (which is the release slip's
``release_id``), verifies the release slip, assembles a bundle from the
warehouse + sync trees, writes the three manifests plus ``approval.json``,
**signs** the bundle manifest and every registry entry with an Ed25519
key, records git provenance, atomically commits the bundle to
``www/releases/<release-id>/``, and appends one signed entry to the
release registry at ``sync/packing_slips/release-index.jsonl``.

``--verify-only <release-id>`` verifies an already-published release
without republishing: walks the registry chain from genesis, checks the
entry signature against the trusted keys, verifies the bundle manifest's
hash matches the entry's recorded value, and verifies the manifest's
signature. This is the operator-facing entry point for chain-walk
verification; the underlying primitives live in
:mod:`minecraft.modpack_upgrade.registry_verify`.

Preconditions and pipeline (publish, not verify-only):

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
  * **Slice 11 T2** -- Ed25519 signing. The bundle manifest carries a
    real ``signature`` (base64) and ``key_id`` (sha256 of the raw
    Ed25519 point); a ``bundle-manifest.sig`` sidecar carries the same
    base64 signature per Section 6.4. Registry entries carry real
    ``signature`` and ``key_id``.
  * **Slice 11 T3** -- ``--verify-only`` flag; ``entry_hash`` and
    signature both cover all of :data:`registry_verify.CHAIN_FIELDS`
    (previously ``tag_object_format`` was in the signature but not the
    entry hash -- inconsistency closed, no production entries affected).

Precondition ordering (publish, not verify-only)
------------------------------------------------

The preconditions run in this order:

  1. Slip loads and verifies; ``release_id`` matches the argument.
     These are cheap and per-release, so a wrong-tag invocation fails
     with the most actionable message.
  2. Signing key loads. Cheap (env-var or single-file read); the point
     of loading it here is to fail on missing key BEFORE the expensive
     artifact hashing below.
  3. Every artifact verified against its slip-recorded SHA-256.
     This is the only expensive step (hashing every jar).
  4. Bundle directory is absent.

Cheap checks first, expensive checks last. This ordering also means
that a fresh install with no signing key AND a missing slip surfaces
the slip error, not the key error -- the slip is the more fundamental
"this specific tag is wrong" problem, while a missing key is a one-time
setup problem. ``test_cli_main.py::test_publish_missing_slip_exits_3``
pins this ordering: with slip-first, that test sees "slip not found";
if a future refactor moves key-load before slip-check, that test would
see the key error instead.

Signature projection rule
-------------------------

Both the bundle-manifest signature and the registry-entry signature
cover a **projection** of the stored object, not the object itself:

  * **Bundle manifest:** projection = manifest minus ``signature`` and
    ``key_id``. A consumer verifying the manifest strips these two
    fields, canonicalizes, and verifies.
  * **Registry entry:** projection = :data:`registry_verify.CHAIN_FIELDS`
    extracted from the entry. A consumer verifying the entry strips
    ``signature``, ``key_id``, and ``entry_hash``, canonicalizes the
    remaining chain fields, verifies the Ed25519 signature, AND
    confirms ``sha256(chain_fields) == entry_hash``.

The chain-fields extraction and hash computation are shared between
``publish`` (writer) and :mod:`registry_verify` (reader) by importing
from the latter, so the two sides cannot drift.

The self-reference paradox is resolved the same way for the manifest's
``files`` list: it excludes ``bundle-manifest.json`` **and**
``bundle-manifest.sig``. A manifest cannot honestly hash a file whose
contents depend on the manifest's own hash.

Signing key (publish only)
--------------------------

Loaded from ``[publication].signing_key_env`` in ``config.d/upgrade.toml``
(default env-var name: ``RELEASE_SIGNING_KEY``). Env var may contain a
path to a PEM-encoded Ed25519 private key or the PEM text inline. See
:mod:`minecraft.modpack_upgrade.signing`.

**A valid key must be present before publish runs.** Signing is not
optional: an unsigned release is not a release. Missing key = exit 3
before any staging is done.

``--verify-only`` does NOT require the signing key -- verification uses
trusted public keys, not the private signing key. It DOES require
``[registry].trusted_keys_path`` (default ``config.d/trusted_keys.toml``).

Run discovery
-------------

``publish`` receives a tag, not a run-id. The slip does not record its
originating run; ``promote`` overwrites ``release_id`` to the tag and
does not store the run-id elsewhere. To find the approval source,
``publish`` scans ``<state_dir>/runs/*/state.toml`` for the run whose
``resolution_inputs_sha256`` matches
``slip.fingerprints.resolution_inputs_sha256`` **and** whose
``status == "materialized"``.

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

Registry and chain
------------------

One JSON line appended to ``sync/packing_slips/release-index.jsonl``.
The entry's ``previous_entry_hash`` is the last line's ``entry_hash``
(or 64 zeroes for the first entry). ``entry_hash`` and ``signature``
both cover the same :data:`registry_verify.CHAIN_FIELDS` projection --
see :mod:`registry_verify` for the canonical definitions.

Deferred until the underlying features exist:

  * Runtime payload (Section 6.3 #6) -- no runtime payload producer yet.
  * Client ZIP verification (Section 6.3 #5) -- no client ZIP yet;
    ``client-distribution-manifest.json`` is a placeholder.
  * ``[inputs.git]`` tree enforcement (Section 6.3 #7) -- prod-deploy
    concern (Section 5.3 rule 4).

Exit codes:

  * ``0`` -- publish succeeded end-to-end (or ``--verify-only`` passed
    all checks).
  * ``3`` -- a precondition, signing-config, assembly, manifest, git-ref,
    verification, or (for ``--verify-only``) trusted-keys/config failure.
    Staging is cleaned up; no bundle is committed.
  * ``7`` -- registry append failure *after* a bundle was committed.
    The bundle is rolled back on a best-effort basis; if rollback
    fails too, the operator must remove ``www/releases/<id>/`` manually.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from LoggingCore import get_logger
from PlatformCore.subprocess import run_git

from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.deploy_pack.files import compute_sha256
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import SigningError
from minecraft.modpack_upgrade.registry_verify import (
    ChainVerification,
    ReleaseVerification,
    compute_entry_hash,
    verify_release,
)
from minecraft.modpack_upgrade.signing import (
    SigningKey,
    TrustedKeys,
    load_signing_key,
    load_trusted_keys,
    sign_json,
    verify_json,
)
from minecraft.modpack_upgrade.slip import PackingSlip, load_slip
from minecraft.modpack_upgrade.state import RunState, load_state

__all__ = ["run"]

_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.publish")


# Bundle filenames. Kept as constants because both writers and any future
# reader (prod daemon, registry verifier) must agree on the exact names.
_BUNDLE_MANIFEST = "bundle-manifest.json"
_BUNDLE_MANIFEST_SIG = "bundle-manifest.sig"
_ARTIFACT_MANIFEST = "artifact-manifest.json"
_CLIENT_DISTRIBUTION_MANIFEST = "client-distribution-manifest.json"
_APPROVAL = "approval.json"

# Files not listed in the manifest's own `files` array. The manifest
# itself is excluded because it can't honestly hash bytes that depend on
# its own hash; the sidecar is excluded for the same reason (the sig
# depends on the manifest; putting its hash in the manifest would create
# a cycle). The other two manifests ARE listed -- they're referenced by
# `_finalize_bundle` explicitly.
_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        _BUNDLE_MANIFEST,
        _BUNDLE_MANIFEST_SIG,
        _ARTIFACT_MANIFEST,
        _CLIENT_DISTRIBUTION_MANIFEST,
    }
)

# Registry chain genesis sentinel. Matches the ``"0" * 64`` convention
# used by every other "absent" SHA-256 in this codebase.
_GENESIS_HASH = "0" * 64

_DEFAULT_SIGNING_KEY_ENV = "RELEASE_SIGNING_KEY"
_DEFAULT_TRUSTED_KEYS_FILENAME = "trusted_keys.toml"


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
# Config
# ---------------------------------------------------------------------------


def _resolve_signing_key_env_name(config_dir: Path) -> str:
    """Return the env-var name that holds the signing key.

    Reads ``[publication].signing_key_env`` from ``config.d/upgrade.toml``.
    Missing file, missing section, or malformed value all fall back to
    ``RELEASE_SIGNING_KEY`` -- consistent with how other config defaults
    are resolved in this codebase (see ``providers.config``).
    """
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return _DEFAULT_SIGNING_KEY_ENV
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_SIGNING_KEY_ENV
    publication = data.get("publication", {})
    if not isinstance(publication, dict):
        return _DEFAULT_SIGNING_KEY_ENV
    raw = publication.get("signing_key_env")
    if not isinstance(raw, str) or not raw:
        return _DEFAULT_SIGNING_KEY_ENV
    return raw


def _resolve_trusted_keys_path(config_dir: Path, project_root: Path) -> Path:
    """Return the trusted-keys path for ``--verify-only``.

    Reads ``[registry].trusted_keys_path`` from ``config.d/upgrade.toml``.
    Relative paths resolve against ``project_root`` (matching how
    ``[docker].compose_file`` is resolved elsewhere). Missing file,
    missing section, or malformed value all fall back to
    ``<config_dir>/trusted_keys.toml`` -- the documented default.
    """
    default = config_dir / _DEFAULT_TRUSTED_KEYS_FILENAME
    toml_path = config_dir / "upgrade.toml"
    if not toml_path.is_file():
        return default
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return default
    registry = data.get("registry", {})
    if not isinstance(registry, dict):
        return default
    raw = registry.get("trusted_keys_path")
    if not isinstance(raw, str) or not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else project_root / path


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
    state. Manifest writing, signing, and the final atomic rename happen
    in :func:`_finalize_bundle`.
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
# Manifest construction and atomic commit (T4 + T5 + Slice 11 T2)
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


def _write_sig_sidecar(path: Path, signature_b64: str) -> None:
    """Write the base64 signature to the ``bundle-manifest.sig`` sidecar.

    Sidecar format: single base64 line + trailing newline. Matches the
    convention of a ``.sig`` file per Section 6.4; the base64 encoding
    keeps the file text-safe (no binary artifacts in the bundle that git
    would mangle).
    """
    try:
        path.write_text(signature_b64 + "\n", encoding="utf-8")
    except OSError as exc:
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
    signing_key: SigningKey,
) -> str:
    """Write manifests, sign bundle-manifest, verify, atomically commit.

    Returns the SHA-256 of ``bundle-manifest.json`` bytes (computed before
    the rename, which does not change bytes).

    Order of operations:

      1. ``artifact-manifest.json`` -- structured artifact view.
      2. ``client-distribution-manifest.json`` -- placeholder.
      3. Build the *projection* of the bundle manifest: content files +
         both prior manifests + audit fields. Excludes ``signature`` and
         ``key_id`` (chicken-and-egg).
      4. Sign the projection; write ``bundle-manifest.sig`` sidecar with
         the base64 signature (Section 6.4).
      5. Write the full ``bundle-manifest.json`` = projection + signature
         + key_id.
      6. Self-check: verify the signature round-trips against the
         projection. If this fails, signing or canonicalization is broken
         -- catch now, not in prod pull.
      7. Verify every file in the manifest against disk.
      8. Atomic rename.
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

    projection = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "slip_content_sha256": slip.content_sha256,
        "approval_sha256": approval_sha256,
        "release_tag_object_sha": release_tag_object_sha,
        "release_commit_sha": release_commit_sha,
        "files": sorted(content_files + manifest_files, key=lambda entry: entry["path"]),
    }

    signature_bytes = sign_json(signing_key, projection)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")

    _write_sig_sidecar(staging_dir / _BUNDLE_MANIFEST_SIG, signature_b64)

    bundle_manifest = {
        **projection,
        "signature": signature_b64,
        "key_id": signing_key.key_id,
    }
    _write_json_atomic(staging_dir / _BUNDLE_MANIFEST, bundle_manifest)

    if not verify_json(signing_key.public_key, projection, signature_bytes):
        raise UpgradeCliError(
            3,
            "internal: signed bundle manifest failed its own signature "
            "self-check. This indicates a bug in signing.py or canonical.py; "
            "refusing to commit an unverifiable bundle.",
        )

    manifest_sha = compute_sha256(staging_dir / _BUNDLE_MANIFEST)
    _verify_manifest_hashes(staging_dir, bundle_manifest)
    _commit_bundle(staging_dir, bundle_dir)
    return manifest_sha


# ---------------------------------------------------------------------------
# Registry append (T5 + Slice 11 T2 + T3)
# ---------------------------------------------------------------------------


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
    signing_key: SigningKey,
) -> str:
    """Append one signed entry to ``release-index.jsonl``; return its ``entry_hash``.

    The ``entry_hash`` and ``signature`` cover the same
    :data:`registry_verify.CHAIN_FIELDS` projection -- see
    :mod:`registry_verify` for the canonical field set and hash
    computation. This function builds the chain_fields dict explicitly
    (as the *writer*, we know every field), then delegates the hash and
    signature to the shared primitives so verification agrees by
    construction.

    Raises :class:`UpgradeCliError(7)` on any I/O failure. The caller is
    responsible for rolling back the committed bundle if this raises.
    """
    previous = _last_entry_hash(registry_path)
    chain_fields = {
        "schema_version": 1,
        "release_id": slip.release_id,
        "tag_object_sha": tag_object_sha,
        "tag_object_format": tag_object_format,
        "commit_sha": commit_sha,
        "slip_content_sha256": slip.content_sha256,
        "bundle_manifest_sha256": bundle_manifest_sha256,
        "previous_entry_hash": previous,
    }
    entry_hash = compute_entry_hash(chain_fields)
    signature_bytes = sign_json(signing_key, chain_fields)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")
    entry = {
        **chain_fields,
        "entry_hash": entry_hash,
        "signature": signature_b64,
        "key_id": signing_key.key_id,
    }
    line = json.dumps(entry, sort_keys=True) + "\n"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(registry_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        raise UpgradeCliError(7, f"could not append to registry {registry_path}: {exc}") from exc
    return entry_hash


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
    _log_cli.info(f"  signature: {bundle_dir / _BUNDLE_MANIFEST_SIG}")
    _log_cli.info("")
    _log_cli.info(f"registry entry appended: {registry_path}")
    _log_cli.info(f"  entry_hash: {entry_hash}")
    _log_cli.info("")


def _print_chain_summary(chain: ChainVerification) -> None:
    """Print chain-verification results."""
    status = "OK" if chain.ok else "FAILED"
    _log_cli.info(f"  chain: {chain.entry_count} entry/entries, status={status}")
    if chain.ok:
        _log_cli.info(f"    head: {chain.last_entry_hash}")
    else:
        for failure in chain.failures:
            release_ctx = f" release={failure.release_id!r}" if failure.release_id is not None else ""
            _log_cli.info(f"    [{failure.reason}] index={failure.index}{release_ctx}")
            _log_cli.info(f"      {failure.detail}")


def _print_verify_result(result: ReleaseVerification) -> None:
    """Print the results of a ``--verify-only`` run."""
    _log_cli.info(f"verify: release {result.release_id!r}")
    _print_chain_summary(result.chain)
    _log_cli.info(f"  release entry present: {result.release_present}")
    _log_cli.info(f"  bundle present: {result.bundle_present}")
    if result.bundle_failures:
        _log_cli.info("  bundle failures:")
        for failure in result.bundle_failures:
            _log_cli.info(f"    - {failure}")
    _log_cli.info("")
    _log_cli.info(f"overall: {'OK' if result.ok else 'FAILED'}")


# ---------------------------------------------------------------------------
# Verify-only path
# ---------------------------------------------------------------------------


def _load_trusted_keys_for_verify(config_dir: Path, project_root: Path) -> TrustedKeys:
    """Load trusted keys for ``--verify-only``. Raise ``UpgradeCliError(3)`` on failure."""
    path = _resolve_trusted_keys_path(config_dir, project_root)
    try:
        return load_trusted_keys(path)
    except SigningError as exc:
        raise UpgradeCliError(3, str(exc)) from exc


def _run_verify_only(*, config_dir: Path, project_root: Path, release_id: str) -> int:
    """Execute ``publish --verify-only``. Returns 0 (all ok) or 3 (any failure)."""
    trusted = _load_trusted_keys_for_verify(config_dir, project_root)
    registry_path = _registry_path(project_root)
    bundle_dir = _bundle_dir(project_root, release_id)

    result = verify_release(
        release_id,
        registry_path=registry_path,
        bundle_dir=bundle_dir,
        trusted=trusted,
    )
    _print_verify_result(result)
    return 0 if result.ok else 3


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    config_dir: Path,
    project_root: Path,
    release_id: str,
    env: Mapping[str, str] | None = None,
    verify_only: bool = False,
) -> int:
    """Execute ``minecraft-upgrade publish``. Returns 0 on success, 3, or 7.

    See the module docstring for what signing means, how the signature
    projection is constructed, and what ``verify_only=True`` does. The
    two modes are entirely disjoint: ``verify_only=True`` does not touch
    the signing key, staging directory, or bundle.
    """
    if verify_only:
        return _run_verify_only(
            config_dir=config_dir,
            project_root=project_root,
            release_id=release_id,
        )

    slip_path = _slip_path(config_dir, release_id)
    bundle_dir = _bundle_dir(project_root, release_id)
    staging_dir = _staging_dir(project_root, release_id)
    warehouse_dir = _warehouse_dir(project_root)
    config_src = _config_source(project_root)
    kubejs_src = _kubejs_source(project_root)
    state_dir = _state_dir(config_dir)
    registry_path = _registry_path(project_root)

    # Precondition 1: slip (cheap, per-release). A wrong-tag invocation
    # fails here with the most actionable message, before touching the
    # signing key or hashing any artifacts.
    slip = _require_slip_verifies(slip_path)
    _require_release_id_matches(slip, release_id, slip_path)

    # Precondition 2: signing key (cheap, one-time config). Loaded here
    # -- after slip validation but BEFORE artifact hashing -- so a
    # missing key fails fast without 100MB of unnecessary file I/O.
    signing_env_name = _resolve_signing_key_env_name(config_dir)
    try:
        signing_key = load_signing_key(signing_env_name, env=env)
    except SigningError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    # Precondition 3: artifacts (expensive -- hashes every jar).
    _require_artifacts_present(slip, warehouse_dir)

    # Precondition 4: bundle absent (cheap, filesystem).
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

        run_dir, run_state = _find_materialized_run_for_slip(slip, state_dir)
        approval_body = _build_approval(run_dir, run_state, _now_utc())
        _write_json_atomic(staging_dir / _APPROVAL, approval_body)
        approval_sha = compute_sha256(staging_dir / _APPROVAL)

        tag_object_sha, tag_object_format, commit_sha = _resolve_git_refs(project_root, release_id)

        _print_staging_summary(staging_dir)
        bundle_manifest_sha = _finalize_bundle(
            staging_dir,
            bundle_dir,
            slip=slip,
            approval_sha256=approval_sha,
            release_tag_object_sha=tag_object_sha,
            release_commit_sha=commit_sha,
            signing_key=signing_key,
        )
    finally:
        _cleanup_dir(staging_dir)

    try:
        entry_hash = _append_registry_entry(
            registry_path,
            slip=slip,
            tag_object_sha=tag_object_sha,
            tag_object_format=tag_object_format,
            commit_sha=commit_sha,
            bundle_manifest_sha256=bundle_manifest_sha,
            signing_key=signing_key,
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
