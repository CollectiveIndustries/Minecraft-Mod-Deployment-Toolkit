# tests/deploy_pack/test_scope_server_slip.py

"""Slip-mode server-scope tests (spec v1.3 Section 5.8, Section 14.4)."""

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
from minecraft.deploy_pack.scope_server import deploy_server_scope
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_release_context,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _hex(ch: str) -> str:
    """Return a 64-character hex string composed of the repeated character ``ch``."""
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
    instance_overrides: dict | None = None,
) -> dict:
    """Build a valid slip dict; content_sha256 computed."""
    survival_spec = {
        "targeted": True,
        "required_sides": [],
        "config_mode": "merge",
        "kubejs_mode": "delete",
        "config_deletions": [],
        "protect_patterns": [],
    }
    if instance_overrides:
        survival_spec.update(instance_overrides)
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
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {"survival": survival_spec},
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _build_ctx(
    tmp_path: Path,
    jar_contents: dict[str, bytes],
    jar_sides: dict[str, str] | None = None,
    instance_overrides: dict | None = None,
) -> SlipContext:
    """Materialize a bundle and return a verified SlipContext."""
    sides = jar_sides or {}
    entries = [_jar_entry(name, content, sides.get(name, "both")) for name, content in jar_contents.items()]
    parsed = slip_mod.parse_slip(_slip_dict(entries, instance_overrides))
    root = tmp_path / "bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
    for name, content in jar_contents.items():
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
    instance_root: Path | None,
    slip_ctx: SlipContext | None,
    mods_dir: Path | None = None,
) -> DeploymentConfig:
    """Build a minimal DeploymentConfig with one survival instance."""
    inst = InstanceConfig(
        name="survival",
        container="mc-survival",
        config_mode="merge",
        kubejs_mode="delete",
    )
    inst.service = _make_service()
    inst.instance_root = instance_root
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


def _make_plan(
    mods_dir: Path | None,
    slip_ctx: SlipContext | None,
    targeted: bool = False,
) -> PreflightPlan:
    """Build a minimal PreflightPlan for scope_server tests."""
    return PreflightPlan(
        scopes=ScopeSet(server=True),
        partition=["survival"],
        member_plans={"survival": MemberPlan(member="survival", container="mc-survival")},
        none_set=[],
        reload_set=[],
        restart_set=[],
        pack_required=False,
        container_states={},
        mods_dir=mods_dir,
        targeted=targeted,
        slip_context=slip_ctx,
    )


# ===========================================================================
# Mods deployment from slip
# ===========================================================================


def test_slip_mods_only_server_and_both(tmp_path: Path) -> None:
    """Server scope ships ``server`` and ``both`` jars, not ``client``."""
    ctx = _build_ctx(
        tmp_path,
        {"server.jar": b"s", "both.jar": b"b", "client.jar": b"c"},
        jar_sides={"server.jar": "server", "both.jar": "both", "client.jar": "client"},
    )
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(mods_dir, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (mods_dir / "server.jar").exists()
    assert (mods_dir / "both.jar").exists()
    assert not (mods_dir / "client.jar").exists()


def test_slip_mods_targeted_skips_mods(tmp_path: Path) -> None:
    """A targeted deploy does not touch mods_dir, in slip mode either."""
    ctx = _build_ctx(tmp_path, {"a.jar": b"a"})
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "stale.jar").write_bytes(b"old")
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(mods_dir, ctx, targeted=True)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert result.mods_skipped_reason is not None
    assert (mods_dir / "stale.jar").exists()


def test_slip_mods_missing_mods_dir(tmp_path: Path) -> None:
    """mods_dir=None skips mods deployment without failure."""
    ctx = _build_ctx(tmp_path, {"a.jar": b"a"})
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(None, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert result.mods_skipped_reason == "mods_dir could not be determined"


# ===========================================================================
# Config and kubejs deployment from slip
# ===========================================================================


def test_slip_config_deployed_from_context(tmp_path: Path) -> None:
    """Config files come from ctx.config_source, not sync_root."""
    ctx = _build_ctx(tmp_path, {})
    (ctx.config_source / "server.properties").write_bytes(b"motd=new")
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "config").mkdir()
    (instance_root / "config" / "server.properties").write_bytes(b"motd=old")
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(None, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    member = result.member_results["survival"]
    assert member.config_result is not None
    assert (instance_root / "config" / "server.properties").read_bytes() == b"motd=new"


def test_slip_kubejs_deleted_and_restored(tmp_path: Path) -> None:
    """kubejs_mode = delete wipes stale scripts and deploys slip's source tree."""
    ctx = _build_ctx(tmp_path, {})
    (ctx.kubejs_source / "script.js").write_bytes(b"new")
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "kubejs").mkdir()
    (instance_root / "kubejs" / "old.js").write_bytes(b"old")
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(None, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (instance_root / "kubejs" / "script.js").read_bytes() == b"new"
    assert not (instance_root / "kubejs" / "old.js").exists()


def test_slip_config_deletions_applied(tmp_path: Path) -> None:
    """config_deletions removes the named file before merge."""
    ctx = _build_ctx(
        tmp_path,
        {},
        instance_overrides={"config_deletions": ["remove-me.yml"]},
    )
    (ctx.config_source / "keep.yml").write_bytes(b"keep")
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "config").mkdir()
    (instance_root / "config" / "keep.yml").write_bytes(b"old")
    (instance_root / "config" / "remove-me.yml").write_bytes(b"old")
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(None, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (instance_root / "config" / "keep.yml").read_bytes() == b"keep"
    assert not (instance_root / "config" / "remove-me.yml").exists()


def test_slip_config_deletions_protected_skipped(tmp_path: Path) -> None:
    """A protected deletion is skipped with a warning."""
    ctx = _build_ctx(
        tmp_path,
        {},
        instance_overrides={
            "config_deletions": ["protected.yml"],
            "protect_patterns": ["protected.yml"],
        },
    )
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "config").mkdir()
    (instance_root / "config" / "protected.yml").write_bytes(b"keep-me")
    config = _make_config(tmp_path, instance_root, ctx)
    plan = _make_plan(None, ctx)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (instance_root / "config" / "protected.yml").read_bytes() == b"keep-me"


def test_slip_instance_not_in_slip_is_skipped(tmp_path: Path) -> None:
    """An instance absent from slip.instances is skipped (spec rule 10)."""
    ctx = _build_ctx(tmp_path, {})
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "config").mkdir()
    (instance_root / "config" / "sentinel.yml").write_bytes(b"keep")
    config = _make_config(tmp_path, instance_root, ctx)
    config.instances["other"] = config.instances["survival"]
    config.partition = ["other"]
    plan = _make_plan(None, ctx)
    plan.partition = ["other"]
    plan.member_plans = {}

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (instance_root / "config" / "sentinel.yml").read_bytes() == b"keep"


# ===========================================================================
# Legacy regression
# ===========================================================================


def test_legacy_path_unchanged_when_no_slip(tmp_path: Path) -> None:
    """Without a slip, deploy_server_scope dispatches to the legacy path."""
    instance_root = tmp_path / "inst"
    instance_root.mkdir()
    (instance_root / "config").mkdir()
    (instance_root / "config" / "sentinel.yml").write_bytes(b"old")
    config = _make_config(tmp_path, instance_root, slip_ctx=None)
    config.sync_mapping = {"config": "config"}
    (config.sync_root / "config").mkdir(parents=True)
    (config.sync_root / "config" / "sentinel.yml").write_bytes(b"new")
    plan = _make_plan(None, None)

    result = deploy_server_scope(config, plan, protect_patterns=[])

    assert result.success
    assert (instance_root / "config" / "sentinel.yml").read_bytes() == b"new"
