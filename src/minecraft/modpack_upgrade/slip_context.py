# src/minecraft/modpack_upgrade/slip_context.py

"""SlipContext - the bridge between verified slips and the deploy_pack pipeline.

In slip modes (`release` or `integration`), `deploy_pack` takes its source
sets from a ``SlipContext``, not from ``deps.resolve_mod_sources`` or
``overrides.py``. The context is validated at construction:

  * Every jar in ``slip.jars`` exists under ``bundle_root/artifacts`` and
    hashes to its slip-recorded SHA-256.
  * Every resource pack in ``slip.resource_packs`` exists under
    ``bundle_root/artifacts`` and hashes to its slip-recorded SHA-256.
  * ``config_source``, ``kubejs_source``, and ``runtime_payload`` are
    directories.
  * Integration mode requires a non-empty ``captured_head``.

Construction is the *only* place a slip's source paths are interpreted.
Scope modules consume ``SlipContext`` fields directly; they MUST NOT
independently resolve pointers or reinterpret mutable paths.

``SlipContext`` is frozen. ``jar_sources`` and ``rp_sources`` are
``MappingProxyType`` so callers cannot accidentally mutate the mappings.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .errors import UpgradeError
from .slip import PackingSlip

__all__ = [
    "SlipContext",
    "build_integration_context",
    "build_release_context",
]


@dataclass(frozen=True)
class SlipContext:
    """Immutable bridge between a verified slip and the deploy_pack pipeline.

    Attributes:
        slip: The verified packing slip.
        mode: ``"release"`` for immutable bundles, ``"integration"`` for
            DEV-local candidates.
        bundle_root: Root of the release bundle or candidate workspace.
        config_source: Directory containing the config snapshot to deploy.
        kubejs_source: Directory containing the KubeJS snapshot to deploy.
        jar_sources: Filename -> path for every jar in the slip.
        rp_sources: Filename -> path for every resource pack in the slip.
        runtime_payload: Directory containing the staged runtime payload.
        captured_head: Committed Git revision captured once at construction
            (integration mode only; ``None`` in release mode).
    """

    slip: PackingSlip
    mode: Literal["release", "integration"]
    bundle_root: Path
    config_source: Path
    kubejs_source: Path
    jar_sources: Mapping[str, Path]
    rp_sources: Mapping[str, Path]
    runtime_payload: Path
    captured_head: str | None


def _hash_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of ``path``."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_artifact_list(
    pairs: Iterable[tuple[str, str]],
    artifacts_root: Path,
    label: str,
) -> dict[str, Path]:
    """Return ``{filename: path}`` for every pair, verified against recorded hashes.

    Raises :class:`UpgradeError` on a missing file or a hash mismatch. The
    hash is recomputed here even though the release bundle was already
    verified at pull time, because the gap between pull and context
    construction is exactly the window in which accidental corruption or
    tampering must be caught.
    """
    out: dict[str, Path] = {}
    for filename, expected in pairs:
        path = artifacts_root / filename
        if not path.is_file():
            raise UpgradeError(f"slip_context: {label} not found: {path} (expected {filename})")
        actual = _hash_file(path)
        if actual != expected:
            raise UpgradeError(f"slip_context: {label} hash mismatch: {path} (expected {expected}, got {actual})")
        out[filename] = path
    return out


def _require_dir(path: Path, label: str) -> Path:
    """Return ``path`` if it is a directory; raise :class:`UpgradeError` otherwise."""
    if not path.is_dir():
        raise UpgradeError(f"slip_context: {label} directory not found: {path}")
    return path


def _build_artifact_maps(slip: PackingSlip, artifacts_root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Return ``(jar_sources, rp_sources)`` verified against the slip."""
    jar_sources = _verify_artifact_list(
        ((jar.filename, jar.sha256) for jar in slip.jars),
        artifacts_root,
        "jar",
    )
    rp_sources = _verify_artifact_list(
        ((rp.filename, rp.sha256) for rp in slip.resource_packs),
        artifacts_root,
        "resource pack",
    )
    return jar_sources, rp_sources


def build_release_context(
    slip: PackingSlip,
    bundle_root: Path,
) -> SlipContext:
    """Build a :class:`SlipContext` from a verified release bundle.

    Sources are taken from the bundle's own snapshots:
    ``bundle_root/config``, ``bundle_root/kubejs``,
    ``bundle_root/artifacts``, and ``bundle_root/runtime``.

    Raises :class:`UpgradeError` on any missing directory, missing artifact,
    or hash mismatch.
    """
    if not bundle_root.is_dir():
        raise UpgradeError(f"slip_context: bundle root not found: {bundle_root}")
    artifacts_root = _require_dir(bundle_root / "artifacts", "artifacts")
    config_source = _require_dir(bundle_root / "config", "config")
    kubejs_source = _require_dir(bundle_root / "kubejs", "kubejs")
    runtime_payload = _require_dir(bundle_root / "runtime", "runtime")
    jar_sources, rp_sources = _build_artifact_maps(slip, artifacts_root)
    return SlipContext(
        slip=slip,
        mode="release",
        bundle_root=bundle_root,
        config_source=config_source,
        kubejs_source=kubejs_source,
        jar_sources=MappingProxyType(jar_sources),
        rp_sources=MappingProxyType(rp_sources),
        runtime_payload=runtime_payload,
        captured_head=None,
    )


def build_integration_context(
    slip: PackingSlip,
    candidate_root: Path,
    config_source: Path,
    kubejs_source: Path,
    captured_head: str,
) -> SlipContext:
    """Build a :class:`SlipContext` from a DEV-local candidate and a captured HEAD.

    The JAR set and resource-pack set are frozen from
    ``candidate_root/artifacts``. Config and KubeJS come from the
    caller-provided ``config_source`` and ``kubejs_source``, which MUST be
    rooted at the clean committed Git revision identified by
    ``captured_head``. The caller is responsible for capturing
    ``captured_head`` (a ``git rev-parse HEAD`` result) exactly once, before
    construction, and for ensuring the config and kubejs working trees are
    clean at that revision.

    Raises :class:`UpgradeError` on any missing directory, missing artifact,
    hash mismatch, or empty ``captured_head``.
    """
    if not candidate_root.is_dir():
        raise UpgradeError(f"slip_context: candidate root not found: {candidate_root}")
    if not captured_head:
        raise UpgradeError("slip_context: captured_head is required in integration mode")
    artifacts_root = _require_dir(candidate_root / "artifacts", "artifacts")
    _require_dir(config_source, "config")
    _require_dir(kubejs_source, "kubejs")
    runtime_payload = _require_dir(candidate_root / "runtime", "runtime")
    jar_sources, rp_sources = _build_artifact_maps(slip, artifacts_root)
    return SlipContext(
        slip=slip,
        mode="integration",
        bundle_root=candidate_root,
        config_source=config_source,
        kubejs_source=kubejs_source,
        jar_sources=MappingProxyType(jar_sources),
        rp_sources=MappingProxyType(rp_sources),
        runtime_payload=runtime_payload,
        captured_head=captured_head,
    )
