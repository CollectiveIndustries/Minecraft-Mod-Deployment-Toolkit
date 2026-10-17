# tests/deploy_pack/test_scope_client_slip.py

"""Slip-mode client-scope tests (spec v1.3 Section 5.8, Section 14.4)."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from minecraft.deploy_pack.config_model import (
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.scope_client import deploy_client_scope
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_release_context,
)


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _jar_entry(filename: str, content: bytes, side: str = "both") -> dict:
    """Build a valid [[jar]] entry whose sha256 matches ``content``."""
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "side": side,
        "provider": "modrinth",
        "pw_toml": f"{filename}.pw.toml",
        "pw_toml_sha256": _hex("0"),
        "dependencies": [],
    }


def _slip_dict(
    jars: list[dict],
    rps: list[dict] | None = None,
    client_output: str = "minecraft_client_20261012.zip",
) -> dict:
    """Build a valid slip dict with content_sha256."""
    body = {
        "packing_slip_version": 1,
        "release_id": "v1.0.0",
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
        "resource_pack": rps or [],
        "velocity_plugin": [],
        "client_distribution": {
            "output_filename": client_output,
            "sha256": _hex("a"),
            "with_resources": True,
            "member_manifest_sha256": _hex("b"),
        },
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


def _build_ctx(
    tmp_path: Path,
    jar_contents: dict[str, bytes],
    jar_sides: dict[str, str] | None = None,
    rp_contents: dict[str, bytes] | None = None,
    rp_instances: dict[str, str] | None = None,
    client_output: str = "minecraft_client_20261012.zip",
) -> SlipContext:
    """Materialize a bundle and return a verified SlipContext."""
    sides = jar_sides or {}
    entries = [_jar_entry(name, content, sides.get(name, "both")) for name, content in jar_contents.items()]
    rps: list[dict] = []
    rp_data = rp_contents or {}
    rp_inst = rp_instances or {}
    for name, content in rp_data.items():
        rps.append(
            {
                "filename": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "instance": rp_inst.get(name, "survival"),
                "required": True,
                "prompt": "",
            }
        )
    parsed = slip_mod.parse_slip(_slip_dict(entries, rps, client_output))
    root = tmp_path / "bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
    for name, content in jar_contents.items():
        (root / "artifacts" / name).write_bytes(content)
    for name, content in rp_data.items():
        (root / "artifacts" / name).write_bytes(content)
    return build_release_context(parsed, root)


def _make_service() -> ComposeService:
    """Return a minimal ComposeService."""
    return ComposeService(
        name="survival",
        container_name="mc-survival",
        binds=[],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=True,
        secrets=[],
        environment={},
        env_files=[],
    )


def _make_compose() -> ComposeLoadResult:
    """Wrap a single survival service in a ComposeLoadResult."""
    return ComposeLoadResult(
        file=ComposeFile(
            path=Path("/dev/null"),
            base_dir=Path("/"),
            services={"survival": _make_service()},
            secret_files={},
        ),
        error=None,
    )


def _make_config(
    tmp_path: Path,
    slip_ctx: SlipContext | None,
) -> DeploymentConfig:
    """Build a minimal DeploymentConfig with a single survival instance."""
    inst = InstanceConfig(
        name="survival",
        container="mc-survival",
        config_mode="merge",
        kubejs_mode="delete",
    )
    inst.service = _make_service()
    inst.instance_root = tmp_path / "inst"
    (tmp_path / "inst").mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=tmp_path / "www",
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="minecraft_client_{date}.zip",
        download_base_url="https://example.com",
        protect_file=None,
        sync_mapping={},
        restart_policy={},
        instances={"survival": inst},
        partition=["survival"],
        partition_unknown=[],
        requested_instances=None,
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        compose=_make_compose(),
        slip=slip_ctx,
    )


def _read_zip_members(path: Path) -> dict[str, bytes]:
    """Return ``{arcname: content}`` for every member of the ZIP."""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name)
    return out


# ===========================================================================
# Slip-mode client staging
# ===========================================================================


def test_slip_client_only_client_and_both(tmp_path: Path) -> None:
    """Client ZIP contains ``client`` and ``both`` jars only."""
    ctx = _build_ctx(
        tmp_path,
        {"srv.jar": b"s", "cli.jar": b"c", "both.jar": b"b"},
        jar_sides={"srv.jar": "server", "cli.jar": "client", "both.jar": "both"},
    )
    config = _make_config(tmp_path, ctx)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=False, protect_patterns=[])

    assert result.success, result.failure_message
    members = _read_zip_members(result.zip_path)
    assert "mods/cli.jar" in members
    assert "mods/both.jar" in members
    assert "mods/srv.jar" not in members
    assert members["mods/cli.jar"] == b"c"


def test_slip_client_config_and_kubejs_included(tmp_path: Path) -> None:
    """Config and KubeJS come from the slip's source roots."""
    ctx = _build_ctx(tmp_path, {})
    (ctx.config_source / "server.properties").write_bytes(b"motd=new")
    (ctx.kubejs_source / "script.js").write_bytes(b"line1")
    config = _make_config(tmp_path, ctx)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=False, protect_patterns=[])

    assert result.success, result.failure_message
    members = _read_zip_members(result.zip_path)
    assert "config/server.properties" in members
    assert members["config/server.properties"] == b"motd=new"
    assert "kubejs/script.js" in members
    assert members["kubejs/script.js"] == b"line1"


def test_slip_client_output_filename_from_slip(tmp_path: Path) -> None:
    """The ZIP filename comes from slip.client_distribution."""
    ctx = _build_ctx(tmp_path, {}, client_output="release-42-client.zip")
    config = _make_config(tmp_path, ctx)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=False, protect_patterns=[])

    assert result.success, result.failure_message
    assert result.resolved_output_filename == "release-42-client.zip"
    assert result.zip_path is not None
    assert result.zip_path.name == "release-42-client.zip"
    assert result.zip_path.is_file()
    assert result.changelog_path is not None
    assert result.changelog_path.name == "release-42-client.html"


def test_slip_client_with_resources_stages_rps(tmp_path: Path) -> None:
    """With --with-resources, slip-declared RPs are included in the ZIP."""
    ctx = _build_ctx(
        tmp_path,
        {},
        rp_contents={"survival-pack.zip": b"pk1", "extras.zip": b"pk2"},
        rp_instances={"survival-pack.zip": "survival", "extras.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=True, protect_patterns=[])

    assert result.success, result.failure_message
    members = _read_zip_members(result.zip_path)
    assert "resourcepacks/survival-pack.zip" in members
    assert members["resourcepacks/survival-pack.zip"] == b"pk1"
    assert "resourcepacks/extras.zip" in members
    assert members["resourcepacks/extras.zip"] == b"pk2"


def test_slip_client_without_resources_skips_rps(tmp_path: Path) -> None:
    """Without --with-resources, RPs are not staged."""
    ctx = _build_ctx(
        tmp_path,
        {},
        rp_contents={"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=False, protect_patterns=[])

    assert result.success, result.failure_message
    members = _read_zip_members(result.zip_path)
    assert "resourcepacks/survival-pack.zip" not in members


def test_slip_client_no_client_distribution_fails(tmp_path: Path) -> None:
    """A slip without [client_distribution] causes client scope to fail."""
    body = _slip_dict([], None, "x.zip")
    del body["client_distribution"]
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    parsed = slip_mod.parse_slip(body)
    root = tmp_path / "no-cd-bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
    ctx_no_cd = build_release_context(parsed, root)
    config = _make_config(tmp_path, ctx_no_cd)
    config.www_dir.mkdir(parents=True, exist_ok=True)

    result = deploy_client_scope(config, with_resources=False, protect_patterns=[])

    assert not result.success
    assert "client_distribution" in (result.failure_message or "")
