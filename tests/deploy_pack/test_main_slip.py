# tests/deploy_pack/test_main_slip.py

"""Slip-mode CLI wiring tests (spec v1.3 Section 14.1, Section 14.5, Section 14.7)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
import tomli_w

from minecraft.deploy_pack import main
from minecraft.deploy_pack.errors import ConfigError
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import SlipContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _valid_slip_dict(release_id: str = "v1.0.0", jar_contents: dict[str, bytes] | None = None) -> dict:
    """Build a valid slip dict with content_sha256."""
    if jar_contents is None:
        jar_contents = {"mod.jar": b"content"}
    jars = [
        {
            "filename": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "side": "both",
            "provider": "modrinth",
            "pw_toml": f"{name}.pw.toml",
            "pw_toml_sha256": _hex("0"),
            "dependencies": [],
        }
        for name, content in jar_contents.items()
    ]
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
        "jar": jars,
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


def _materialize_release(tmp_path: Path, release_id: str = "v1.0.0", jar_contents: dict[str, bytes] | None = None) -> dict:
    """Create a slip and its bundle on disk; return the slip dict."""
    if jar_contents is None:
        jar_contents = {"mod.jar": b"content"}
    slip_dir = tmp_path / "sync" / "packing_slips"
    slip_dir.mkdir(parents=True)
    body = _valid_slip_dict(release_id, jar_contents)
    (slip_dir / f"{release_id}.toml").write_text(tomli_w.dumps(body), encoding="utf-8")
    bundle_root = tmp_path / "www" / "releases" / release_id
    (bundle_root / "artifacts").mkdir(parents=True)
    (bundle_root / "config").mkdir()
    (bundle_root / "kubejs").mkdir()
    (bundle_root / "runtime").mkdir()
    for name, content in jar_contents.items():
        (bundle_root / "artifacts" / name).write_bytes(content)
    return body


def _materialize_candidate(tmp_path: Path, candidate_id: str = "RC-1", jar_contents: dict[str, bytes] | None = None) -> Path:
    """Create a candidate slip and its artifacts on disk."""
    if jar_contents is None:
        jar_contents = {"mod.jar": b"content"}
    candidate_root = tmp_path / "candidates" / candidate_id
    (candidate_root / "artifacts").mkdir(parents=True)
    (candidate_root / "config").mkdir()
    (candidate_root / "kubejs").mkdir()
    (candidate_root / "runtime").mkdir()
    body = _valid_slip_dict(candidate_id, jar_contents)
    (candidate_root / "packing_slip.toml").write_text(tomli_w.dumps(body), encoding="utf-8")
    for name, content in jar_contents.items():
        (candidate_root / "artifacts" / name).write_bytes(content)
    return candidate_root


def _write_deploy_pack_toml(tmp_path: Path, slip_cfg: dict | None = None) -> Path:
    """Write a minimal deploy_pack.toml with an optional [slip] section."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir(exist_ok=True)
    lines = ["[slip]"]
    if slip_cfg:
        for key, value in slip_cfg.items():
            lines.append(f'{key} = "{value}"')
    (config_dir / "deploy_pack.toml").write_text("\n".join(lines) + "\n")
    return config_dir


def _dummy_logger() -> logging.Logger:
    """Return a logger suitable for helper-level tests."""
    return logging.getLogger("test_main_slip")


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_release_and_pointer_mutually_exclusive():
    """--release and --release-pointer together exit 2."""
    code = main._run(["--server", "--release", "v1.0.0", "--release-pointer", "stable"])
    assert code == 2


def test_slip_flags_require_scope():
    """A slip flag without any deployment scope exits 2."""
    code = main._run(["--release", "v1.0.0"])
    assert code == 2


def test_integration_requires_release_pointer():
    """--integration without --release-pointer exits 2."""
    code = main._run(["--server", "--integration"])
    assert code == 2


def test_integration_requires_latest_slip():
    """--integration with a pointer other than latest-slip exits 2."""
    code = main._run(["--server", "--integration", "--release-pointer", "stable"])
    assert code == 2


def test_latest_slip_pointer_without_integration_rejected():
    """--release-pointer latest-slip without --integration exits 2."""
    code = main._run(["--server", "--release-pointer", "latest-slip"])
    assert code == 2


def test_unknown_pointer_rejected():
    """An unrecognized pointer name exits 2."""
    code = main._run(["--server", "--release-pointer", "bogus"])
    assert code == 2


def test_audit_mods_rejects_slip_flags():
    """--audit-mods is incompatible with slip flags."""
    code = main._run(["--audit-mods", "--release", "v1.0.0"])
    assert code == 2


# ---------------------------------------------------------------------------
# Slip context builder
# ---------------------------------------------------------------------------


def test_build_slip_context_none_without_flags():
    """No slip flags => no context."""
    args = main._Args(server=True)
    assert main._build_slip_context(Path("/nonexistent"), args, _dummy_logger()) is None


def test_build_release_context_direct_tag(tmp_path: Path):
    """--release TAG loads the slip and its bundle into a release context."""
    _materialize_release(tmp_path, "v1.0.0", {"mod.jar": b"content"})
    config_dir = _write_deploy_pack_toml(tmp_path)
    args = main._Args(server=True, release="v1.0.0")

    ctx = main._build_slip_context(config_dir, args, _dummy_logger())

    assert isinstance(ctx, SlipContext)
    assert ctx.mode == "release"
    assert ctx.slip.release_id == "v1.0.0"
    assert "mod.jar" in ctx.jar_sources


def test_build_release_context_stable_pointer(tmp_path: Path):
    """--release-pointer stable reads the pointer file and loads the referenced release."""
    _materialize_release(tmp_path, "v1.0.0", {"mod.jar": b"content"})
    (tmp_path / "sync" / "packing_slips" / "stable").write_text("v1.0.0.toml\n")
    config_dir = _write_deploy_pack_toml(tmp_path)
    args = main._Args(server=True, release_pointer="stable")

    ctx = main._build_slip_context(config_dir, args, _dummy_logger())

    assert ctx is not None
    assert ctx.mode == "release"
    assert ctx.slip.release_id == "v1.0.0"


def test_build_release_context_missing_slip(tmp_path: Path):
    """A missing slip file raises ConfigError with a stable message."""
    config_dir = _write_deploy_pack_toml(tmp_path)
    args = main._Args(server=True, release="v9.9.9")
    with pytest.raises(ConfigError, match="slip load failed"):
        main._build_slip_context(config_dir, args, _dummy_logger())


def test_build_release_context_missing_bundle(tmp_path: Path):
    """A missing bundle raises ConfigError."""
    _materialize_release(tmp_path, "v1.0.0")
    (tmp_path / "www" / "releases" / "v1.0.0").rename(tmp_path / "www" / "releases" / "moved-away")
    config_dir = _write_deploy_pack_toml(tmp_path)
    args = main._Args(server=True, release="v1.0.0")
    with pytest.raises(ConfigError, match="slip load failed"):
        main._build_slip_context(config_dir, args, _dummy_logger())


def test_build_integration_context_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Integration context is built with a captured HEAD."""
    _materialize_candidate(tmp_path, "RC-1", {"mod.jar": b"content"})
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    (tmp_path / "sync" / "config" / "x.yml").write_bytes(b"x")
    (tmp_path / "sync" / "kubejs" / "y.js").write_bytes(b"y")
    pointer = tmp_path / "latest-slip"
    pointer.write_text("RC-1\n")
    config_dir = _write_deploy_pack_toml(
        tmp_path,
        {
            "candidate_pointer_path": str(pointer),
            "candidate_store": str(tmp_path / "candidates"),
        },
    )

    def fake_git(args: list[str], _cwd: Path) -> tuple[int, str]:
        """Simulate a clean tree and a stable HEAD."""
        if args == ["status", "--porcelain", "sync/config", "sync/kubejs"]:
            return (0, "")
        if args == ["rev-parse", "HEAD"]:
            return (0, "abc123def456")
        return (128, "")

    monkeypatch.setattr(main, "_run_git", fake_git)
    args = main._Args(server=True, integration=True, release_pointer="latest-slip")

    ctx = main._build_slip_context(config_dir, args, _dummy_logger())

    assert ctx is not None
    assert ctx.mode == "integration"
    assert ctx.captured_head == "abc123def456"
    assert "mod.jar" in ctx.jar_sources


def test_build_integration_context_dirty_tree_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A dirty working tree for sync/config or sync/kubejs is rejected."""
    _materialize_candidate(tmp_path, "RC-1")
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    pointer = tmp_path / "latest-slip"
    pointer.write_text("RC-1\n")
    config_dir = _write_deploy_pack_toml(
        tmp_path,
        {
            "candidate_pointer_path": str(pointer),
            "candidate_store": str(tmp_path / "candidates"),
        },
    )

    def fake_git(args: list[str], _cwd: Path) -> tuple[int, str]:
        """Simulate a dirty sync/config file."""
        if args == ["status", "--porcelain", "sync/config", "sync/kubejs"]:
            return (0, " M sync/config/x.yml")
        return (128, "")

    monkeypatch.setattr(main, "_run_git", fake_git)
    args = main._Args(server=True, integration=True, release_pointer="latest-slip")
    with pytest.raises(ConfigError, match="dirty working tree"):
        main._build_slip_context(config_dir, args, _dummy_logger())


def test_build_integration_context_git_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When git is missing, integration context construction fails."""
    _materialize_candidate(tmp_path, "RC-1")
    (tmp_path / "sync" / "config").mkdir(parents=True)
    (tmp_path / "sync" / "kubejs").mkdir(parents=True)
    pointer = tmp_path / "latest-slip"
    pointer.write_text("RC-1\n")
    config_dir = _write_deploy_pack_toml(
        tmp_path,
        {
            "candidate_pointer_path": str(pointer),
            "candidate_store": str(tmp_path / "candidates"),
        },
    )

    def fake_git(_args: list[str], _cwd: Path) -> tuple[int, str]:
        """Simulate missing git."""
        return (127, "")

    monkeypatch.setattr(main, "_run_git", fake_git)
    args = main._Args(server=True, integration=True, release_pointer="latest-slip")
    with pytest.raises(ConfigError, match="git is not available"):
        main._build_slip_context(config_dir, args, _dummy_logger())


# ---------------------------------------------------------------------------
# Wiring: slip reaches load_deployment_config
# ---------------------------------------------------------------------------


def test_load_config_passes_slip_and_requested_instances(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_load_config forwards slip, requested_instances, and deploy_mode_override to load_deployment_config."""
    captured: dict = {}

    def fake_load(
        config_dir: Path,
        requested_instances=None,
        cli_remaining=None,
        logger=None,
        slip=None,
        deploy_mode_override=None,
    ):
        """Capture kwargs and return a sentinel."""
        captured["slip"] = slip
        captured["requested_instances"] = requested_instances
        captured["deploy_mode_override"] = deploy_mode_override
        return "fake-config"

    monkeypatch.setattr(main, "load_deployment_config", fake_load)
    sentinel = object()
    result = main._load_config(tmp_path, [], slip=sentinel, requested_instances={"survival"})
    assert result == "fake-config"
    assert captured["slip"] is sentinel
    assert captured["requested_instances"] == {"survival"}
    # Default from the caller (None) reaches the target unmodified.
    assert captured["deploy_mode_override"] is None


def test_load_config_passes_deploy_mode_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_load_config forwards deploy_mode_override (Slice 17) to load_deployment_config."""
    captured: dict = {}

    def fake_load(
        config_dir: Path,
        requested_instances=None,
        cli_remaining=None,
        logger=None,
        slip=None,
        deploy_mode_override=None,
    ):
        """Capture the deploy_mode_override kwarg and return a sentinel."""
        captured["deploy_mode_override"] = deploy_mode_override
        return "fake-config"

    monkeypatch.setattr(main, "load_deployment_config", fake_load)
    result = main._load_config(tmp_path, [], deploy_mode_override="dev")
    assert result == "fake-config"
    assert captured["deploy_mode_override"] == "dev"
