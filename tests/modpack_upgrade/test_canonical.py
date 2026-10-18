# tests/modpack_upgrade/test_canonical.py

"""Canonical hashing tests (spec Section 5.4)."""

from __future__ import annotations

import datetime

import pytest

from minecraft.modpack_upgrade import canonical

_SAFE_INT_MAX = 2**53 - 1
_SAFE_INT_MIN = -(2**53 - 1)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_scalar_digest_is_stable_across_calls():
    """Repeated hashing of the same input produces the same digest."""
    assert canonical.digest({"x": 1, "y": "z"}) == canonical.digest({"x": 1, "y": "z"})


def test_dict_key_order_does_not_affect_digest():
    """Dict declaration order does not affect the RFC 8785 digest."""
    assert canonical.digest({"a": 1, "b": 2}) == canonical.digest({"b": 2, "a": 1})


def test_input_is_not_mutated():
    """Canonicalization does not modify the input."""
    original = {"a": [3, 1, 2], "b": {"c": "x"}}
    snapshot = {"a": [3, 1, 2], "b": {"c": "x"}}
    canonical.canonicalize(original)
    assert original == snapshot


def test_absent_vs_empty_distinguished():
    """Absent key and empty-list value are different documents."""
    assert canonical.digest({"x": []}) != canonical.digest({})


# ---------------------------------------------------------------------------
# NFC normalization and collision rejection
# ---------------------------------------------------------------------------


def test_nfc_normalization_is_applied():
    """NFC-composed and -decomposed strings hash identically."""
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    assert composed != decomposed
    assert canonical.digest({"x": composed}) == canonical.digest({"x": decomposed})


def test_duplicate_keys_after_normalization_are_rejected():
    """Two keys that normalize to the same NFC string are rejected."""
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    with pytest.raises(ValueError, match="duplicate key"):
        canonical.canonicalize({composed: 1, decomposed: 2})


def test_keys_are_case_sensitive():
    """'Side' and 'side' are distinct keys, not collisions."""
    digest_upper = canonical.digest({"Side": 1})
    digest_lower = canonical.digest({"side": 1})
    assert digest_upper != digest_lower


# ---------------------------------------------------------------------------
# Type rejection
# ---------------------------------------------------------------------------


def test_floats_are_rejected():
    """Floats are rejected (RFC 8785 has no interoperable float semantics)."""
    with pytest.raises(TypeError, match="floats"):
        canonical.canonicalize({"x": 1.5})


def test_none_is_rejected():
    """Bare ``None`` is rejected at the canonical layer."""
    with pytest.raises(TypeError, match="None"):
        canonical.canonicalize({"x": None})


def test_none_in_array_rejected():
    """``None`` array elements are rejected; arrays are not silently shrunk."""
    with pytest.raises(TypeError, match="None"):
        canonical.canonicalize([None])


def test_none_in_nested_array_rejected():
    """``None`` inside a nested array is rejected."""
    with pytest.raises(TypeError, match="None"):
        canonical.canonicalize({"x": [[None]]})


def test_toml_datetime_is_rejected():
    """TOML datetime values are rejected in the canonical body."""
    dt = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC)
    with pytest.raises(TypeError):
        canonical.canonicalize({"created_at_utc": dt})


def test_bool_is_not_treated_as_int():
    """``True`` and ``1`` are different canonical values."""
    assert canonical.digest({"x": True}) != canonical.digest({"x": 1})


# ---------------------------------------------------------------------------
# Safe-integer bounds (RFC 8785 Section 3.2.2.3)
# ---------------------------------------------------------------------------


def test_safe_int_max_accepted():
    """The maximum safe integer (2^53 - 1) is accepted."""
    assert canonical.digest({"x": _SAFE_INT_MAX}) is not None


def test_safe_int_min_accepted():
    """The minimum safe integer -(2^53 - 1) is accepted."""
    assert canonical.digest({"x": _SAFE_INT_MIN}) is not None


def test_int_above_safe_max_rejected():
    """An integer one above the safe max is rejected."""
    with pytest.raises(TypeError, match="safe range"):
        canonical.canonicalize({"x": _SAFE_INT_MAX + 1})


def test_int_below_safe_min_rejected():
    """An integer one below the safe min is rejected."""
    with pytest.raises(TypeError, match="safe range"):
        canonical.canonicalize({"x": _SAFE_INT_MIN - 1})


def test_large_python_int_rejected():
    """A very large arbitrary-precision int is rejected."""
    with pytest.raises(TypeError, match="safe range"):
        canonical.canonicalize({"x": 2**200})
