# tests/deploy_pack/test_cli_commands.py

"""Tests for the implemented CLI commands (verify, status, runtime, manifest)."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w
import yaml

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.main import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _valid_slip_body(release_id: str = "v1.0.2") -> dict:
    """Build a valid slip body."""
    body = {
        "packing_slip_version": 1,
        "release_id": release_id,
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
        "jar": [],
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_slip(slip_dir: Path, body: dict) -> None:
    """Write ``body`` as ``<release_id>.toml`` under ``slip_dir``."""
    slip_dir.mkdir(parents=True, exist_ok=True)
    (slip_dir / f"{body['release_id']}.toml").write_text(tomli_w.dumps(body), encoding="utf-8")


def _setup_project(tmp_path: Path) -> Path:
    """Create ``<tmp>/config.d/`` with an empty ``deploy_pack.toml``."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text("", encoding="utf-8")
    return config_dir


def _setup_slip_in_project(tmp_path: Path, release_id: str) -> Path:
    """Write a valid slip in the default project-relative slip dir."""
    slip_dir = tmp_path / "sync" / "packing_slips"
    _write_slip(slip_dir, _valid_slip_body(release_id))
    return slip_dir


def _extract_section(output: str, header: str) -> list[str]:
    """Return the lines following ``header`` up to the next blank line or EOF."""
    lines = output.splitlines()
    try:
        idx = lines.index(header)
    except ValueError:
        return []
    result: list[str] = []
    for line in lines[idx + 1 :]:
        if not line.strip():
            break
        result.append(line)
    return result


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A valid slip verifies and prints its fields."""
    config_dir = _setup_project(tmp_path)
    _setup_slip_in_project(tmp_path, "v1.0.2")
    code = main(
        [
            "--config-dir",
            str(config_dir),
            "verify",
            "--tag",
            "v1.0.2",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "release_id: v1.0.2" in captured.out
    assert "status: ok" in captured.out
    assert "jars: 0" in captured.out


def test_verify_missing_slip(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A missing slip file exits 3."""
    config_dir = _setup_project(tmp_path)
    code = main(
        [
            "--config-dir",
            str(config_dir),
            "verify",
            "--tag",
            "v9.9.9",
        ]
    )
    assert code == 3
    captured = capsys.readouterr()
    assert "slip unreadable" in captured.err


def test_verify_hash_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A slip with a wrong content_sha256 exits 3."""
    config_dir = _setup_project(tmp_path)
    slip_dir = tmp_path / "sync" / "packing_slips"
    slip_dir.mkdir(parents=True)
    body = _valid_slip_body("v1.0.2")
    body["content_sha256"] = _hex("0")
    (slip_dir / "v1.0.2.toml").write_text(tomli_w.dumps(body), encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "verify", "--tag", "v1.0.2"])
    assert code == 3
    captured = capsys.readouterr()
    assert "hash mismatch" in captured.err


def test_verify_schema_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A slip with a bad schema exits 3."""
    config_dir = _setup_project(tmp_path)
    slip_dir = tmp_path / "sync" / "packing_slips"
    slip_dir.mkdir(parents=True)
    (slip_dir / "v1.0.2.toml").write_text("packing_slip_version = 999\n", encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "verify", "--tag", "v1.0.2"])
    assert code == 3
    captured = capsys.readouterr()
    assert "schema error" in captured.err


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_empty_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status on a fresh project shows empty state."""
    config_dir = _setup_project(tmp_path)
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "active_release: (none)" in captured.out
    assert "last_registry_head: (none)" in captured.out
    assert "last_verification: (none)" in captured.out
    assert "stable: (not present)" in captured.out


def test_status_with_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status reflects state files that exist."""
    config_dir = _setup_project(tmp_path)
    state_dir = tmp_path / "var" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "active-release.txt").write_text("v1.0.2\n")
    (state_dir / "last-registry-head.txt").write_text("abc123\n")
    (state_dir / "last-verification.json").write_text('{"release_id": "v1.0.2", "status": "ok", "at_utc": "2026-10-17T00:00:00Z"}')
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nstate_dir = "{state_dir}"\n', encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "active_release: v1.0.2" in captured.out
    assert "last_registry_head: abc123" in captured.out
    assert "last_verification: v1.0.2 (ok)" in captured.out


def test_status_with_slips(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status lists slips present on disk."""
    config_dir = _setup_project(tmp_path)
    _setup_slip_in_project(tmp_path, "v1.0.2")
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "v1.0.2.toml" in captured.out


def test_status_stable_pointer(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status prints the stable pointer target when present."""
    config_dir = _setup_project(tmp_path)
    slip_dir = _setup_slip_in_project(tmp_path, "v1.0.2")
    (slip_dir / "stable").write_text("v1.0.2.toml\n")
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "stable: v1.0.2.toml" in captured.out


def test_status_with_bundle_store_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status reports a missing bundle store."""
    config_dir = _setup_project(tmp_path)
    missing = tmp_path / "does-not-exist"
    (config_dir / "deploy_pack.toml").write_text(f'[daemon]\nrelease_store = "{missing}"\n', encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert _extract_section(out, "bundles:") == ["  (bundle store missing)"]


def test_status_with_empty_bundle_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status reports an empty bundle store."""
    config_dir = _setup_project(tmp_path)
    release_store = tmp_path / "releases"
    release_store.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nrelease_store = "{release_store}"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert _extract_section(out, "bundles:") == ["  (none)"]


def test_status_with_populated_bundle_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Status lists bundle directories and skips regular files."""
    config_dir = _setup_project(tmp_path)
    release_store = tmp_path / "releases"
    (release_store / "v1.0.2" / "artifacts").mkdir(parents=True)
    (release_store / "v1.0.3" / "artifacts").mkdir(parents=True)
    # A regular file in the store must not be listed as a bundle
    (release_store / "not-a-bundle.txt").write_text("x", encoding="utf-8")
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nrelease_store = "{release_store}"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert _extract_section(out, "bundles:") == [
        "  v1.0.2",
        "  v1.0.3",
    ]


# ---------------------------------------------------------------------------
# runtime inspect
# ---------------------------------------------------------------------------


def test_runtime_inspect_missing_compose(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A missing compose file exits 3."""
    config_dir = _setup_project(tmp_path)
    code = main(["--config-dir", str(config_dir), "runtime", "inspect"])
    assert code == 3
    captured = capsys.readouterr()
    assert "compose file not found" in captured.err


def test_runtime_inspect_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A valid compose file is parsed and printed."""
    config_dir = _setup_project(tmp_path)
    compose_data = {
        "services": {
            "survival": {
                "image": "itzg/minecraft-server:java25",
                "platform": "linux/amd64",
                "container_name": "mc-survival",
            }
        }
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose_data), encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "runtime", "inspect"])
    assert code == 0
    captured = capsys.readouterr()
    assert "services: 1" in captured.out
    assert "itzg/minecraft-server:java25" in captured.out
    assert "mc-survival" in captured.out


def test_runtime_inspect_custom_compose_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """[docker].compose_file in deploy_pack.toml overrides the default."""
    config_dir = _setup_project(tmp_path)
    custom = tmp_path / "custom" / "compose.yml"
    custom.parent.mkdir()
    custom.write_text(
        yaml.safe_dump({"services": {"survival": {"image": "myimage:latest"}}}),
        encoding="utf-8",
    )
    (config_dir / "deploy_pack.toml").write_text(f'[docker]\ncompose_file = "{custom}"\n', encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "runtime", "inspect"])
    assert code == 0
    captured = capsys.readouterr()
    assert "myimage:latest" in captured.out


# ---------------------------------------------------------------------------
# manifest check
# ---------------------------------------------------------------------------


def test_manifest_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A missing manifest exits 3."""
    config_dir = _setup_project(tmp_path)
    code = main(["--config-dir", str(config_dir), "manifest", "check"])
    assert code == 3
    captured = capsys.readouterr()
    assert "manifest not found" in captured.err


def test_manifest_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A valid Section 8 manifest parses and reports its fields."""
    config_dir = _setup_project(tmp_path)
    manifest = tmp_path / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "52.0.1"\n\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "manifest", "check"])
    assert code == 0
    captured = capsys.readouterr()
    assert "status: valid" in captured.out
    assert "[minecraft].version: 1.21.1" in captured.out
    assert "[minecraft].loader: forge" in captured.out
    assert "[policy].selection: release_then_beta" in captured.out
    assert "[policy].no_version: ask" in captured.out


def test_manifest_invalid_toml(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A malformed manifest exits 3."""
    config_dir = _setup_project(tmp_path)
    manifest = tmp_path / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("this is not = valid = toml\n", encoding="utf-8")
    code = main(["--config-dir", str(config_dir), "manifest", "check"])
    assert code == 3
    captured = capsys.readouterr()
    assert "invalid TOML" in captured.err


def test_manifest_explicit_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """An explicit manifest path overrides the default."""
    config_dir = _setup_project(tmp_path)
    custom = tmp_path / "custom-manifest.toml"
    custom.write_text(
        '[minecraft]\nversion = "1.20.1"\nloader = "forge"\nloader_version = "47.4.10"\n\n[policy]\nselection = "release_only"\nno_version = "drop"\n',
        encoding="utf-8",
    )
    code = main(
        [
            "--config-dir",
            str(config_dir),
            "manifest",
            "check",
            str(custom),
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "status: valid" in captured.out
    assert "[minecraft].version: 1.20.1" in captured.out
    assert "[policy].selection: release_only" in captured.out
    assert "[policy].no_version: drop" in captured.out
