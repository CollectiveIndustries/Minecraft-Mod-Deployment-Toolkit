# tests/modpack_upgrade/test_slip.py

"""Schema, canonical hashing, and content-hash verification tests for packing slips (spec Section 5)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade import slip
from minecraft.modpack_upgrade.errors import HashMismatchError, SchemaError


def _hex(ch: str) -> str:
    """Returns a 64-character hex string composed of the repeated character ``ch``."""
    return ch * 64


def _base_slip() -> dict:
    """Builds a valid baseline packing-slip body with a matching content hash."""
    body = {
        "packing_slip_version": 1,
        "release_id": "v1.0.2",
        "release_mode": "update",
        "meta": {
            "created_at_utc": "2026-10-12T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.0",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.20.1",
            "loader_type": "forge",
            "loader_version": "47.4.10",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + _hex("1"),
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": _hex("2"),
                "kubejs_tree": _hex("3"),
                "compose_tree": _hex("4"),
                "deploy_toml_tree": _hex("5"),
            },
        },
        "fingerprints": {
            "manifest_sha256": _hex("6"),
            "runtime_sha256": _hex("7"),
            "server_inputs_sha256": _hex("8"),
            "client_inputs_sha256": _hex("9"),
        },
        "tested": {"world": "survival", "snapshot_id": _hex("a")},
        "jar": [
            {
                "filename": "create-1.20.1-6.0.8.jar",
                "sha256": _hex("b"),
                "side": "both",
                "provider": "modrinth",
                "provider_endpoint": "https://api.modrinth.com/v2",
                "project_id": "create",
                "version_id": "abc123",
                "pw_toml": "create-1.20.1-6.0.8.pw.toml",
                "pw_toml_sha256": _hex("c"),
                "dependencies": [
                    {
                        "namespace": "project",
                        "provider": "modrinth",
                        "project_id": "flywheel",
                        "version_id": "def456",
                        "type": "required",
                        "source": "pw_toml",
                    },
                ],
            },
            {
                "filename": "aaa-other-1.0.jar",
                "sha256": _hex("d"),
                "side": "server",
                "provider": "modrinth",
                "provider_endpoint": "https://api.modrinth.com/v2",
                "project_id": "aaa",
                "version_id": "xyz",
                "pw_toml": "aaa-other-1.0.pw.toml",
                "pw_toml_sha256": _hex("e"),
                "dependencies": [],
            },
        ],
        "resource_pack": [],
        "velocity_plugin": [],
        "client_distribution": {
            "output_filename": "minecraft_client_20261012.zip",
            "sha256": _hex("f"),
            "with_resources": True,
            "member_manifest_sha256": _hex("a"),
        },
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": ["both", "server"],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip.compute_content_sha256(body)
    return body


def test_parse_slip_round_trips():
    """Parses the baseline slip and verifies the typed fields preserve document order."""
    body = _base_slip()
    parsed = slip.parse_slip(body)
    assert parsed.release_id == "v1.0.2"
    assert parsed.release_mode == "update"
    assert len(parsed.jars) == 2
    # parse_slip preserves document order; canonical sorting is hash-only.
    assert parsed.jars[0].filename == "create-1.20.1-6.0.8.jar"
    assert parsed.jars[1].filename == "aaa-other-1.0.jar"
    assert parsed.jars[0].is_in_house is False
    assert parsed.instances["survival"].config_mode == "merge"
    assert parsed.instances["survival"].kubejs_mode == "delete"


def test_jar_array_order_does_not_change_hash():
    """Reverses the [[jar]] array and asserts the canonical content hash is unchanged."""
    body = _base_slip()
    shuffled = copy.deepcopy(body)
    shuffled["jar"].reverse()
    shuffled["content_sha256"] = slip.compute_content_sha256(shuffled)
    assert shuffled["content_sha256"] == body["content_sha256"]


def test_required_sides_order_does_not_change_hash():
    """Reorders required_sides and asserts the canonical content hash is unchanged."""
    body = _base_slip()
    swapped = copy.deepcopy(body)
    swapped["instances"]["survival"]["required_sides"] = ["server", "both"]
    swapped["content_sha256"] = slip.compute_content_sha256(swapped)
    assert swapped["content_sha256"] == body["content_sha256"]


def test_dependency_order_does_not_change_hash():
    """Reverses the dependency list and asserts the canonical content hash is unchanged."""
    body = _base_slip()
    body["jar"][0]["dependencies"] = []
    body["jar"][1]["dependencies"] = [
        {
            "namespace": "modid",
            "type": "required",
            "source": "jar_manifest",
        },
        {
            "namespace": "project",
            "provider": "modrinth",
            "project_id": "x",
            "version_id": "1",
            "type": "required",
            "source": "pw_toml",
        },
    ]
    body["content_sha256"] = slip.compute_content_sha256(body)

    reordered = copy.deepcopy(body)
    reordered["jar"][1]["dependencies"] = list(reversed(body["jar"][1]["dependencies"]))
    reordered["content_sha256"] = slip.compute_content_sha256(reordered)
    assert reordered["content_sha256"] == body["content_sha256"]


def test_hash_verification_round_trip(tmp_path: Path):
    """Writes a valid slip to disk and asserts load_slip accepts it."""
    body = _base_slip()
    path = tmp_path / "v1.0.2.toml"
    path.write_text(tomli_w.dumps(body), encoding="utf-8")
    parsed = slip.load_slip(path)
    assert parsed.content_sha256 == body["content_sha256"]


def test_hash_mismatch_is_rejected(tmp_path: Path):
    """Writes a tampered slip and asserts load_slip raises HashMismatchError."""
    body = _base_slip()
    body["release_id"] = "v1.0.2-tampered"
    path = tmp_path / "v1.0.2.toml"
    path.write_text(tomli_w.dumps(body), encoding="utf-8")
    with pytest.raises(HashMismatchError):
        slip.load_slip(path)


def test_bad_release_mode_is_rejected():
    """Asserts an invalid release_mode value raises SchemaError."""
    body = _base_slip()
    body["release_mode"] = "invalid"
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_bad_config_mode_is_rejected():
    """Asserts an unsupported config_mode value raises SchemaError."""
    body = _base_slip()
    body["instances"]["survival"]["config_mode"] = "replace"
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_bad_kubejs_mode_is_rejected():
    """Asserts an unsupported kubejs_mode value raises SchemaError."""
    body = _base_slip()
    body["instances"]["survival"]["kubejs_mode"] = "merge"
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_duplicate_jar_filename_is_rejected():
    """Asserts two [[jar]] entries sharing a filename raise SchemaError."""
    body = _base_slip()
    body["jar"][1]["filename"] = body["jar"][0]["filename"]
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_missing_content_sha256_is_rejected():
    """Asserts a slip without content_sha256 raises SchemaError."""
    body = _base_slip()
    del body["content_sha256"]
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_bad_sha256_format_is_rejected():
    """Asserts a non-lowercase hex SHA-256 field raises SchemaError."""
    body = _base_slip()
    body["jar"][0]["sha256"] = "ABC"
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)


def test_float_in_body_is_rejected():
    """Asserts a float in the slip body raises TypeError during canonicalization."""
    body = _base_slip()
    body["java_float"] = 1.5
    with pytest.raises(TypeError):
        slip.compute_content_sha256(body)


def test_toml_datetime_value_is_rejected():
    """Tests that computing the content hash raises a TypeError when the body contains a TOML datetime value."""
    text = "packing_slip_version = 1\nrelease_id = 'v1'\nrelease_mode = 'update'\ncontent_sha256 = '" + _hex("0") + "'\ncreated_at = 2026-10-12T00:00:00Z\n"
    raw = tomllib.loads(text)
    with pytest.raises(TypeError):
        slip.compute_content_sha256(raw)


def test_missing_runtime_is_rejected():
    """Asserts a slip without [runtime] raises SchemaError."""
    body = _base_slip()
    del body["runtime"]
    body["content_sha256"] = slip.compute_content_sha256(body)
    with pytest.raises(SchemaError):
        slip.parse_slip(body)
