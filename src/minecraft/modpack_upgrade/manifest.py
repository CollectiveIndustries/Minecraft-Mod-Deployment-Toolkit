# src/minecraft/modpack_upgrade/manifest.py

"""Manifest schema, validation, and hashing (Manifest_Schema.md v1.3.1 rev e).

Implements Section 1 (`[minecraft]`), Section 3 (`[policy]`), and Section 8 (validation).
The manifest declares *target runtime intent and resolver policy only* -
it never lists mods, versions, sides, or release mode (Section "Purpose").

This module is the single source of truth for "what is a valid manifest".
`update` consumes it. `manifest check` renders every violation it produces.

The `--release-mode update` manifest-vs-Compose equivalence (Section 2) is NOT
enforced here; the caller computes the effective Compose runtime and passes
it into `check_matches_compose`.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import SchemaError

__all__ = [
    "LOADERS",
    "NO_VERSION_VALUES",
    "SELECTION_VALUES",
    "Manifest",
    "load_manifest",
    "validate_manifest_bytes",
]

LOADERS = ("forge", "neoforge", "fabric", "quilt")
SELECTION_VALUES = ("release_then_beta", "release_only", "release_then_beta_then_alpha")
NO_VERSION_VALUES = ("ask", "drop", "fail")

_TOP_LEVEL_KEYS = frozenset({"minecraft", "policy"})


@dataclass(frozen=True)
class Manifest:
    """A validated manifest, ready to feed the resolver."""

    minecraft_version: str
    loader: str
    loader_version: str
    selection: str
    no_version: str
    raw_sha256: str


def _collect_violations(data: object) -> list[str]:
    """Return every Section 8 violation, in document order. Empty list = valid."""
    failures: list[str] = []

    if not isinstance(data, dict):
        return ["top-level TOML must be a table"]

    # Section 8 rule 2 - exactly `[minecraft]` and `[policy]`
    keys = set(data.keys())
    extra = sorted(keys - _TOP_LEVEL_KEYS)
    missing = sorted(_TOP_LEVEL_KEYS - keys)
    if extra:
        failures.append(f"unexpected top-level key(s): {extra} (allowed: {sorted(_TOP_LEVEL_KEYS)})")
    if missing:
        failures.append(f"missing top-level key(s): {missing}")

    # Section 8 rules 3-7
    minecraft = data.get("minecraft")
    if not isinstance(minecraft, dict):
        failures.append("[minecraft] must be a table")
    else:
        version = minecraft.get("version")
        if not isinstance(version, str) or not version:
            failures.append("[minecraft].version must be a non-empty string")
        loader = minecraft.get("loader")
        if not isinstance(loader, str) or loader not in LOADERS:
            failures.append(f"[minecraft].loader must be one of {list(LOADERS)}, got {loader!r}")
        loader_version = minecraft.get("loader_version")
        if not isinstance(loader_version, str) or not loader_version:
            failures.append("[minecraft].loader_version must be a non-empty string")

    policy = data.get("policy")
    if not isinstance(policy, dict):
        failures.append("[policy] must be a table")
    else:
        selection = policy.get("selection")
        if not isinstance(selection, str) or selection not in SELECTION_VALUES:
            failures.append(f"[policy].selection must be one of {list(SELECTION_VALUES)}, got {selection!r}")
        no_version = policy.get("no_version")
        if not isinstance(no_version, str) or no_version not in NO_VERSION_VALUES:
            failures.append(f"[policy].no_version must be one of {list(NO_VERSION_VALUES)}, got {no_version!r}")

    return failures


def validate_manifest_bytes(raw: bytes) -> tuple[dict | None, list[str]]:
    """Parse and validate. Returns `(data_or_None, violations)`.

    A TOML parse error yields a single violation and `data=None`.
    """
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return (None, [f"invalid TOML: {exc}"])
    return (data, _collect_violations(data))


def load_manifest(path: Path) -> Manifest:
    """Parse, validate, and hash the manifest at `path`.

    Raises `SchemaError` with all violations joined if the manifest is
    invalid. Missing file is also a schema error (per Section 8 rule 1).
    """
    if not path.is_file():
        raise SchemaError(f"manifest not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SchemaError(f"could not read {path}: {exc}") from exc

    data, violations = validate_manifest_bytes(raw)
    if violations:
        raise SchemaError("manifest schema violation(s):\n  - " + "\n  - ".join(violations))
    assert data is not None

    minecraft = data["minecraft"]
    policy = data["policy"]
    return Manifest(
        minecraft_version=minecraft["version"],
        loader=minecraft["loader"],
        loader_version=minecraft["loader_version"],
        selection=policy["selection"],
        no_version=policy["no_version"],
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
