# tests/deploy_pack/test_scope_resource_pack.py

"""Tests for deploy_pack.scope_resource_pack, Project_Specs.md §4.4, §4.6.6, §4.9, §4.15, §7.5, §7.7, §7.8.

Coverage:

  * §7.7   - zero-pack no-op: a partition member with no [resource_pack.X]
             has nothing to publish and nothing to write
  * §7.8   - source validation happens before any write
  * §4.9   - the five-step ordering: validate, hash, resolve, publish,
             update server.properties; a publish failure stops the
             properties write
  * §4.4   - SHA-1 comparison is case-insensitive; a destination that
             already matches is not republished
  * §4.6.6 - the resource-pack action merge: prompt-only changes write
             and defer; require-resource-pack, resource-pack, and
             resource-pack-sha1 changes trigger a restart
  * §4.15  - empty prompt written as `resource-pack-prompt=`
  * §4.10  - atomic publication of the ZIP and of server.properties

Every test drives :func:`deploy_resource_pack_scope` against a real
DeploymentConfig and a real PreflightPlan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import minecraft.deploy_pack.scope_resource_pack as rp_module
from minecraft.deploy_pack.config_model import (
    ComposeLoadResult,
    DeploymentConfig,
    DiscordConfig,
    DockerConfig,
    InstanceConfig,
    ResourcePackConfig,
)
from minecraft.deploy_pack.files import compute_sha1
from minecraft.deploy_pack.preflight import PreflightPlan, ScopeSet
from minecraft.deploy_pack.scope_resource_pack import (
    PropertiesWriteResult,
    PublishResult,
    ResourcePackScopeResult,
    deploy_resource_pack_scope,
)

# ---------------------------------------------------------------------------
# Config and plan builders
# ---------------------------------------------------------------------------


def _instance(name: str, root: Path) -> InstanceConfig:
    """Return an InstanceConfig with a server.properties path set."""
    root.mkdir(parents=True, exist_ok=True)
    inst = InstanceConfig(
        name=name,
        container=f"mc-{name}",
        instance_root=root,
        config_path=root / "config",
        kubejs_path=root / "kubejs",
    )
    inst.server_properties_path = root / "server.properties"
    return inst


def _config(
    tmp_path: Path,
    *,
    partition: list[str],
    instances: dict[str, InstanceConfig],
    resource_packs: dict[str, ResourcePackConfig] | None = None,
    sync_mapping: dict | None = None,
    www_dir: Path | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig wired for resource-pack scope tests."""
    if www_dir is None:
        www_dir = tmp_path / "www"
        www_dir.mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=www_dir,
        www_dir_error=None,
        www_dir_candidates=[],
        output_filename="minecraft_client_{date}.zip",
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping=sync_mapping
        if sync_mapping is not None
        else {
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
        restart_policy={},
        instances=instances,
        partition=partition,
        partition_unknown=[],
        requested_instances=None,
        resource_packs=resource_packs or {},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=ComposeLoadResult(file=None, error="not used by these tests"),
        mods_dir_toml=None,
    )


def _plan(partition: list[str]) -> PreflightPlan:
    """Build a minimal PreflightPlan for the resource-pack scope."""
    return PreflightPlan(
        scopes=ScopeSet(resource_pack=True),
        partition=list(partition),
        member_plans={},
        none_set=[],
        reload_set=[],
        restart_set=[],
        pack_required=False,
        container_states={},
    )


def _write_source_rp(tmp_path: Path, name: str = "pack.zip", data: bytes = b"ZIPDATA") -> Path:
    """Write a resource-pack source under sync_root/resourcepacks and return its path."""
    src_dir = tmp_path / "sync" / "resourcepacks"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / name
    src.write_bytes(data)
    return src


def _write_props(inst: InstanceConfig, content: str = "") -> None:
    """Write the instance's server.properties file."""
    assert inst.server_properties_path is not None
    inst.server_properties_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# §7.7: zero-pack no-op
# ---------------------------------------------------------------------------


def test_zero_packs_for_partition_is_a_successful_noop(tmp_path: Path) -> None:
    """§7.7: a member with no [resource_pack.X] is reported and skipped."""
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert isinstance(result, ResourcePackScopeResult)
    assert result.success
    assert result.no_pack_members == ["survival"]
    assert result.publish_results == []
    assert result.properties_results == []


def test_zero_packs_with_no_client_mapping_is_a_noop(tmp_path: Path) -> None:
    """§7.7: with no pack configured, a missing resourcepacks.client is not an error."""
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        sync_mapping={"config": "config"},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert result.no_pack_members == ["survival"]


# ---------------------------------------------------------------------------
# §7.8: source validation happens before any write
# ---------------------------------------------------------------------------


def test_missing_source_zip_is_a_validate_stage_failure(tmp_path: Path) -> None:
    """§7.8: a missing source is exit 3 before any file is touched."""
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst, "motd=hi\n")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="missing.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "validate"
    assert result.failure_member == "survival"
    assert result.failure_message is not None
    assert "not found" in result.failure_message
    assert inst.server_properties_path is not None
    assert inst.server_properties_path.read_text(encoding="utf-8") == "motd=hi\n"


def test_missing_client_mapping_is_a_validate_stage_failure(tmp_path: Path) -> None:
    """§7.7: a configured pack without sync_mapping.resourcepacks.client is exit 3."""
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
        sync_mapping={"config": "config", "resourcepacks": {"resource_pack": "@www/resourcepacks"}},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "validate"
    assert result.failure_message is not None
    assert "client is required" in result.failure_message


def test_missing_resource_pack_mapping_is_a_validate_stage_failure(tmp_path: Path) -> None:
    """§7.5: a configured pack without sync_mapping.resourcepacks.resource_pack is exit 3."""
    _write_source_rp(tmp_path)
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
        sync_mapping={"config": "config", "resourcepacks": {"client": "resourcepacks"}},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "validate"
    assert result.failure_message is not None
    assert "resource_pack is required" in result.failure_message


def test_invalid_filename_is_a_validate_stage_failure(tmp_path: Path) -> None:
    """§7.5: a filename that fails the grammar is exit 3."""
    _write_source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.tar.gz", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "validate"


# ---------------------------------------------------------------------------
# §4.4: SHA-1 comparison drives publication
# ---------------------------------------------------------------------------


def test_publishes_when_destination_missing(tmp_path: Path) -> None:
    """§4.4: a missing destination is a change."""
    _write_source_rp(tmp_path, "pack.zip", b"CONTENT")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert result.any_published
    pub = result.publish_results[0]
    assert isinstance(pub, PublishResult)
    assert pub.published is True
    assert pub.destination is not None
    assert pub.destination.read_bytes() == b"CONTENT"
    assert pub.destination == tmp_path / "www" / "resourcepacks" / "pack.zip"


def test_skips_publication_when_destination_matches(tmp_path: Path) -> None:
    """§4.4: matching SHA-1 means no effective change for publication."""
    _write_source_rp(tmp_path, "pack.zip", b"CONTENT")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"CONTENT")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    pub = result.publish_results[0]
    assert pub.published is False
    assert pub.skipped_reason == "destination already matches"


def test_sha1_comparison_is_case_insensitive(tmp_path: Path) -> None:
    """§4.4: comparison normalizes to lowercase."""
    _write_source_rp(tmp_path, "pack.zip", b"CONTENT")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"CONTENT")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    pub = result.publish_results[0]
    assert pub.published is False
    assert pub.sha1 == compute_sha1(tmp_path / "sync" / "resourcepacks" / "pack.zip")


def test_republishes_when_source_content_changes(tmp_path: Path) -> None:
    """§4.4: differing SHA-1 triggers a republish."""
    _write_source_rp(tmp_path, "pack.zip", b"NEW")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"OLD")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.any_published
    assert (dest_dir / "pack.zip").read_bytes() == b"NEW"


# ---------------------------------------------------------------------------
# §4.9: server.properties is updated after all publishes
# ---------------------------------------------------------------------------


def test_writes_all_four_keys_after_publish(tmp_path: Path) -> None:
    """§4.9: every managed key is written after the ZIP lands."""
    _write_source_rp(tmp_path, "pack.zip", b"ZIPDATA")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst, "motd=hi\n")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="Please accept")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert inst.server_properties_path is not None
    props = inst.server_properties_path.read_text(encoding="utf-8")
    assert "motd=hi" in props
    assert "require-resource-pack=true" in props
    assert "resource-pack=http://minecraft/downloads/resourcepacks/pack.zip" in props
    assert "resource-pack-prompt=Please accept" in props
    sha1 = compute_sha1(tmp_path / "sync" / "resourcepacks" / "pack.zip")
    assert f"resource-pack-sha1={sha1}" in props


def test_required_false_is_written_lowercase(tmp_path: Path) -> None:
    """§7.4: booleans are lowercase."""
    _write_source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=False, prompt="")},
    )
    deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert inst.server_properties_path is not None
    assert "require-resource-pack=false" in inst.server_properties_path.read_text(encoding="utf-8")


def test_empty_prompt_is_written_as_empty_value(tmp_path: Path) -> None:
    """§4.15: an empty prompt is written as `resource-pack-prompt=`."""
    _write_source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    _write_props(inst)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert inst.server_properties_path is not None
    assert "resource-pack-prompt=\n" in inst.server_properties_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §4.6.6 / §4.15: prompt-only changes write and defer
# ---------------------------------------------------------------------------


def test_prompt_only_change_does_not_republish(tmp_path: Path) -> None:
    """§4.15: only the prompt differs -> properties write, no publish."""
    src = _write_source_rp(tmp_path, "pack.zip", b"ZIPDATA")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"ZIPDATA")
    sha1 = compute_sha1(src)
    inst = _instance("survival", tmp_path / "survival")
    _write_props(
        inst,
        f"require-resource-pack=true\nresource-pack=http://minecraft/downloads/resourcepacks/pack.zip\nresource-pack-prompt=OLD\nresource-pack-sha1={sha1}\n",
    )
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="NEW")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert not result.any_published
    props = result.properties_results[0]
    assert isinstance(props, PropertiesWriteResult)
    assert props.changes == {"resource-pack-prompt": ("OLD", "NEW")}
    assert props.prompt_only


# ---------------------------------------------------------------------------
# §4.9 / §4.2: publish failure stops the properties write
# ---------------------------------------------------------------------------


def test_publish_failure_leaves_server_properties_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.9 / §4.2: a publish failure halts before the properties write."""
    _write_source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    original = "motd=hi\n"
    _write_props(inst, original)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(rp_module, "atomic_copy", boom)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "publish"
    assert result.properties_results == []
    assert inst.server_properties_path is not None
    assert inst.server_properties_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Multiple members: only members with packs are touched
# ---------------------------------------------------------------------------


def test_members_without_packs_are_reported_and_not_touched(tmp_path: Path) -> None:
    """§7.7: a member with no pack is listed in no_pack_members and left alone."""
    _write_source_rp(tmp_path, "pack.zip")
    a = _instance("a", tmp_path / "a")
    _write_props(a)
    b = _instance("b", tmp_path / "b")
    _write_props(b)
    cfg = _config(
        tmp_path,
        partition=["a", "b"],
        instances={"a": a, "b": b},
        resource_packs={"a": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["a", "b"]), [], None)
    assert result.success
    assert result.no_pack_members == ["b"]
    assert len(result.publish_results) == 1
    assert result.publish_results[0].member == "a"
    assert len(result.properties_results) == 1
