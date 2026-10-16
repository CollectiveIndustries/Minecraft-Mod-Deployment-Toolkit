# tests/modpack_upgrade/test_canonical.py

"""Tests for canonical JSON serialization and digest helpers."""

from __future__ import annotations

import datetime

import pytest

from minecraft.modpack_upgrade import canonical


def test_scalar_digest_is_stable_across_calls():
    """Verifies that digest output is identical across repeated calls with the same scalar input."""
    assert canonical.digest({"x": 1, "y": "z"}) == canonical.digest({"x": 1, "y": "z"})


def test_dict_key_order_does_not_affect_digest():
    """Verifies that dictionary key insertion order does not change the digest."""
    assert canonical.digest({"a": 1, "b": 2}) == canonical.digest({"b": 2, "a": 1})


def test_nfc_normalization_is_applied():
    """Verifies that NFC normalization makes composed and decomposed Unicode strings produce the same digest."""
    composed = "caf\u00e9"  # é as one code point
    decomposed = "cafe\u0301"  # e + combining acute
    assert composed != decomposed
    assert canonical.digest({"x": composed}) == canonical.digest({"x": decomposed})


def test_duplicate_keys_after_normalization_are_rejected():
    """Verifies that keys colliding after Unicode normalization raise ValueError."""
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    with pytest.raises(ValueError):
        canonical.canonicalize({composed: 1, decomposed: 2})


def test_floats_are_rejected():
    """Verifies that floating-point values raise TypeError during canonicalization."""
    with pytest.raises(TypeError):
        canonical.canonicalize({"x": 1.5})


def test_none_is_rejected():
    """Tests that canonicalization raises TypeError when given a None value."""
    with pytest.raises(TypeError):
        canonical.canonicalize({"x": None})


def test_toml_datetime_is_rejected():
    """Tests that canonicalization raises TypeError for TOML datetime values."""
    dt = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC)
    with pytest.raises(TypeError):
        canonical.canonicalize({"created_at_utc": dt})


def test_bool_is_not_treated_as_int():
    """Tests that boolean values are not coerced to integers during canonicalization or digesting."""
    # If bool were coerced to int, True would serialize as 1 and collide with int 1.
    assert canonical.digest({"x": True}) != canonical.digest({"x": 1})


def test_input_is_not_mutated():
    """Tests that canonicalization does not mutate the input mapping."""
    original = {"a": [3, 1, 2], "b": {"c": "x"}}
    snapshot = {"a": [3, 1, 2], "b": {"c": "x"}}
    canonical.canonicalize(original)
    assert original == snapshot
