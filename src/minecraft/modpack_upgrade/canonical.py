# src/minecraft/modpack_upgrade/canonical.py

"""RFC 8785 canonical JSON for packing-slip identity (spec Section 5.4).

Pipeline:

    TOML parse -> NFC normalization and collision checks -> ``None``
    normalization -> slip-specific array ordering (applied by
    ``slip.py``) -> RFC 8785 canonical JSON -> UTF-8 -> SHA-256.

Three normative rules the implementation enforces:

1. **NFC before RFC 8785.** All strings and keys are NFC-normalized
   before RFC 8785 serialization. RFC 8785 requires string data to be
   preserved as supplied and does not itself normalize Unicode; NFC is a
   slip-specific preprocessing step. An implementation that applied raw
   RFC 8785 to unnormalized input would produce different hashes for
   equivalent slips and would fail to catch NFC-collision keys.
2. **Keys are case-sensitive.** ``"Side"`` and ``"side"`` are distinct
   keys. Two differently-encoded keys that normalize to the same NFC
   string are rejected as duplicates.
3. **Safe integers are bounded.** RFC 8785 defines the interoperable
   integer range as ``-(2^53 - 1) <= n <= 2^53 - 1``. Python's ``int``
   is arbitrary-precision, but canonical JSON is a cross-language
   interchange format; integers outside that range are rejected.

``None`` normalization (mapping vs. array)
------------------------------------------

``None`` mapping values are stripped before canonicalization by
``slip._strip_none``, so a programmatically-constructed body with
``{"provider": None}`` hashes identically to a TOML-round-tripped body
with the key absent. ``None`` array elements are rejected here: removing
an element from an array silently changes its cardinality and, for
ordered sequences, its meaning. Failing validation is the frozen rule.
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

# RFC 8785 Section 3.2.2.3: interoperable integers are bounded by 2^53 - 1.
_SAFE_INT_MAX: int = 2**53 - 1
_SAFE_INT_MIN: int = -(2**53 - 1)


def nfc(value: str) -> str:
    """Return ``value`` in Unicode NFC."""
    return unicodedata.normalize("NFC", value)


def _canonicalize_dict(value: dict) -> dict:
    """Return an NFC-normalized copy of ``value`` with duplicate-key detection.

    Dict key ordering is preserved as declared; ``serialize`` performs
    the RFC 8785 sort.
    """
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


def _check_int_range(value: int) -> int:
    """Return ``value`` if it lies in the RFC 8785 safe integer range."""
    if not _SAFE_INT_MIN <= value <= _SAFE_INT_MAX:
        raise TypeError(f"integer outside RFC 8785 safe range [{_SAFE_INT_MIN}, {_SAFE_INT_MAX}]: {value!r}")
    return value


def canonicalize(value: Any) -> Any:
    """Return a canonical, input-preserving copy of ``value``.

    Permitted: ``str``, ``bool``, safe ``int``, ``list``, ``dict[str, ...]``.

    Raises:
        TypeError: For floats, ``None``, TOML date/time values, integers
            outside the RFC 8785 safe range, and any other unsupported
            type. A bare ``None`` array element is rejected here; ``None``
            mapping values are stripped earlier by ``slip._strip_none``.
        ValueError: For duplicate dict keys after NFC normalization.

    The input is never mutated.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _check_int_range(value)
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


def serialize(value: Any) -> bytes:
    """RFC 8785 serialization. Dict keys are sorted by the library."""
    return rfc8785.dumps(value)


def digest(value: Any) -> str:
    """SHA-256 of the canonical serialization, lowercase hex."""
    return hashlib.sha256(serialize(canonicalize(value))).hexdigest()
