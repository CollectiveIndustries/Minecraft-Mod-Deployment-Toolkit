# tests/deploy_pack/test_scope_resource_pack_slip.py

"""Slip-mode resource-pack scope tests (spec v1.3 Section 5.8, Section 14.4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from minecraft.deploy_pack.config_model import (
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.preflight.types import (
    MemberPlan,
    PreflightPlan,
    ScopeSet,
)
from minecraft.deploy_pack.scope_resource_pack import deploy_resource_pack_scope
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_release_context,
)

_DEFAULT_SYNC_MAPPING: dict = {"resourcepacks": {"resource_pack": "@www/resourcepacks"}}


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _jar_entry(filename: str, content: bytes) -> dict:
    """Build a valid [[jar]] entry whose sha256 matches ``content``."""
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "side": "both",
        "provider": "modrinth",
        "pw_toml": f"{filename}.pw.toml",
        "pw_toml_sha256": _hex("0"),
        "dependencies": [],
    }


def _slip_dict(
    rps: list[dict],
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
        "jar": [_jar_entry("noop.jar", b"x")],
        "resource_pack": rps,
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


def _build_ctx(
    tmp_path: Path,
    rp_contents: dict[str, bytes],
    rp_instances: dict[str, str] | None = None,
) -> SlipContext:
    """Materialize a bundle with the given RPs and return a SlipContext."""
    rp_inst = rp_instances or {}
    rps = [
        {
            "filename": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "instance": rp_inst.get(name, "survival"),
            "required": True,
            "prompt": "",
        }
        for name, content in rp_contents.items()
    ]
    parsed = slip_mod.parse_slip(_slip_dict(rps))
    root = tmp_path / "bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
    (root / "artifacts" / "noop.jar").write_bytes(b"x")
    for name, content in rp_contents.items():
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
    sync_mapping: dict | None = None,
) -> DeploymentConfig:
    """Build a minimal DeploymentConfig with a single survival instance.

    ``sync_mapping=None`` selects the default RP mapping; pass ``{}`` to
    test the missing-mapping path explicitly (an empty dict is not treated
    as "use default").
    """
    inst = InstanceConfig(
        name="survival",
        container="mc-survival",
        config_mode="merge",
        kubejs_mode="delete",
    )
    inst.service = _make_service()
    inst.instance_root = tmp_path / "inst"
    inst.server_properties_path = tmp_path / "inst" / "server.properties"
    (tmp_path / "inst").mkdir(exist_ok=True)
    inst.server_properties_path.write_bytes(b"motd=A Minecraft Server\nresource-pack=\n")
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=tmp_path / "www",
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="client.zip",
        download_base_url="https://example.com",
        protect_file=None,
        sync_mapping=_DEFAULT_SYNC_MAPPING if sync_mapping is None else sync_mapping,
        restart_policy={},
        instances={"survival": inst},
        partition=["survival"],
        partition_unknown=[],
        requested_instances=None,
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        compose=_make_compose(),
        slip=slip_ctx,
    )


def _make_plan(slip_ctx: SlipContext | None) -> PreflightPlan:
    """Build a minimal PreflightPlan for RP scope tests."""
    return PreflightPlan(
        scopes=ScopeSet(resource_pack=True),
        partition=["survival"],
        member_plans={"survival": MemberPlan(member="survival", container="mc-survival")},
        none_set=[],
        reload_set=[],
        restart_set=[],
        pack_required=False,
        container_states={},
        slip_context=slip_ctx,
    )


# ===========================================================================
# Slip-mode RP publication
# ===========================================================================


def test_slip_rp_publishes_to_www(tmp_path: Path) -> None:
    """Slip RP is published to the www_dir destination."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx)
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert result.success, result.failure_message
    assert len(result.publish_results) == 1
    pub = result.publish_results[0]
    assert pub.published
    assert pub.destination is not None
    assert pub.destination.is_file()
    assert pub.destination.read_bytes() == b"pk1"


def test_slip_rp_updates_server_properties(tmp_path: Path) -> None:
    """server.properties is updated with slip-declared required/prompt/url/sha1."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx)
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert result.success, result.failure_message
    assert len(result.properties_results) == 1
    props = (tmp_path / "inst" / "server.properties").read_text()
    assert "require-resource-pack=true" in props
    assert "resource-pack=https://example.com/resourcepacks/survival-pack.zip" in props
    sha1 = hashlib.sha1(b"pk1").hexdigest()
    assert f"resource-pack-sha1={sha1}" in props


def test_slip_rp_skips_when_destination_matches(tmp_path: Path) -> None:
    """If the destination SHA-1 already matches, publish is skipped."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "survival-pack.zip").write_bytes(b"pk1")
    config = _make_config(tmp_path, ctx)
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert result.success, result.failure_message
    assert len(result.publish_results) == 1
    assert not result.publish_results[0].published
    assert result.publish_results[0].skipped_reason == "destination already matches"


def test_slip_rp_no_packs_for_partition_is_noop(tmp_path: Path) -> None:
    """A slip with no RPs for the partition is a successful no-op."""
    ctx = _build_ctx(tmp_path, {})
    config = _make_config(tmp_path, ctx)
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert result.success
    assert result.no_pack_members == ["survival"]
    assert result.publish_results == []


def test_slip_rp_missing_mapping_fails(tmp_path: Path) -> None:
    """Without [sync_mapping].resourcepacks.resource_pack, slip mode fails."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx, sync_mapping={})
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert not result.success
    assert result.failure_stage == "validate"
    assert "resource_pack" in (result.failure_message or "")


def test_slip_rp_missing_www_dir_fails(tmp_path: Path) -> None:
    """Without www_dir, slip mode fails."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_instances={"survival-pack.zip": "survival"},
    )
    config = _make_config(tmp_path, ctx)
    config.www_dir = None
    plan = _make_plan(ctx)

    result = deploy_resource_pack_scope(config, plan, protect_patterns=[])

    assert not result.success
    assert result.failure_stage == "validate"
    assert "www_dir" in (result.failure_message or "")
