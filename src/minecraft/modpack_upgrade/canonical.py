# src/minecraft/modpack_upgrade/canonical.py

"""RFC 8785 canonical JSON for packing-slip identity (spec Section 5.4).

Flow:
  1. ``canonicalize`` - NFC-normalize every string and key, detect
     post-normalization duplicate keys, reject any value outside
     {str, bool, int, list, dict-with-str-keys}.
  2. ``serialize`` - RFC 8785 serialization via the pinned ``rfc8785``.
  3. ``digest`` - SHA-256 of the UTF-8 canonical bytes, lowercase hex.

Floats, ``None``, and TOML datetime values are rejected: the spec
forbids all three in the canonical body. Absent and empty values stay
distinguished.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

try:
    import rfc8785
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError("rfc8785 is required by minecraft.modpack_upgrade.canonical; install it with `pdm add rfc8785`") from exc

__all__ = ["canonicalize", "digest", "nfc", "serialize"]


def nfc(value: str) -> str:
    """Return ``value`` in Unicode NFC."""
    return unicodedata.normalize("NFC", value)


def canonicalize(value: Any) -> Any:
    """Return a canonical, input-preserving copy of ``value``.

    Permitted: ``str``, ``bool``, ``int``, ``list``, ``dict[str, ...]``.
    Everything else raises ``TypeError`` (including ``float`` and
    ``None``). Duplicate keys after NFC normalization raise
    ``ValueError``. The input is never mutated.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return nfc(value)
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, dict):
        return _canonicalize_dict(value)
    if isinstance(value, float):
        raise TypeError(f"floats are not permitted in the canonical body: {value!r}")
    if value is None:
        raise TypeError("None is not permitted in the canonical body")
    raise TypeError(f"unsupported type in canonical body: {type(value).__name__}")


def _canonicalize_dict(value: dict) -> dict:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"dict keys must be strings, got {type(key).__name__}")
        normalized = nfc(key)
        if normalized in seen:
            raise ValueError(f"duplicate key after NFC normalization: {key!r} -> {normalized!r}")
        seen.add(normalized)
        out[normalized] = canonicalize(item)
    return out


def serialize(value: Any) -> bytes:
    """RFC 8785 serialization. Dict keys are sorted by the library."""
    return rfc8785.dumps(value)


def digest(value: Any) -> str:
    """SHA-256 of the canonical serialization, lowercase hex."""
    return hashlib.sha256(serialize(canonicalize(value))).hexdigest()
