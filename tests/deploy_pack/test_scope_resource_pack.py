# tests/deploy_pack/test_scope_resource_pack.py

"""Tests for deploy_pack.scope_resource_pack, per Project_Specs.md v3.0 §10.1.

Coverage areas:
  * action merge (via preflight; the scope only writes)
  * destination-missing triggers publication
  * SHA-1 case-insensitive comparison
  * zero-pack no-op (§7.7)
  * @www grammar validation surfaces from resolve_resource_pack_dest
  * properties vs publication independence (§4.4)
  * [resource_pack.X] key validation (filename required, required
    boolean required, prompt optional)
  * §4.9 phase ordering: publish all before touching properties
  * §4.15 prompt-only path
  * §4.10 atomic publication
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.deploy_pack.config_model import DeploymentConfig, DiscordConfig, DockerConfig, InstanceConfig, ResourcePackConfig
from minecraft.deploy_pack.files import compute_sha1
from minecraft.deploy_pack.preflight import PreflightPlan, ScopeSet
from minecraft.deploy_pack.scope_resource_pack import _client_source_dir, _needs_publish, _resource_pack_mapping, deploy_resource_pack_scope


def _config(
    tmp_path: Path,
    *,
    partition: list[str],
    instances: dict[str, InstanceConfig],
    resource_packs: dict[str, ResourcePackConfig] | None = None,
    sync_mapping: dict | None = None,
    www_dir: Path | None = None,
) -> DeploymentConfig:
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
        else {"config": "config", "kubejs": "kubejs", "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"}},
        restart_policy={},
        instances=instances,
        partition=partition,
        partition_unknown=[],
        requested_instances=None,
        resource_packs=resource_packs or {},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        discord=DiscordConfig(),
        webhook_url=None,
        compose=None,
        mods_dir_toml=None,
    )


def _instance(name: str, root: Path, *, props: bool = True) -> InstanceConfig:
    root.mkdir(parents=True, exist_ok=True)
    inst = InstanceConfig(name=name, container=f"mc-{name}", instance_root=root, config_path=root / "config", kubejs_path=root / "kubejs")
    if props:
        inst.server_properties_path = root / "server.properties"
    return inst


def _plan(partition: list[str]) -> PreflightPlan:
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


def _source_rp(tmp_path: Path, name: str = "pack.zip", data: bytes = b"ZIPDATA") -> Path:
    src_dir = tmp_path / "sync" / "resourcepacks"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / name
    src.write_bytes(data)
    return src


def test_client_source_dir_returns_path(tmp_path: Path) -> None:
    """Verifies that the client source directory resolves to the expected resourcepacks path."""
    cfg = _config(tmp_path, partition=[], instances={})
    assert _client_source_dir(cfg) == tmp_path / "sync" / "resourcepacks"


def test_client_source_dir_none_when_absent(tmp_path: Path) -> None:
    """Tests that _client_source_dir returns None when no client source directory is configured."""
    cfg = _config(tmp_path, partition=[], instances={}, sync_mapping={"config": "config", "kubejs": "kubejs"})
    assert _client_source_dir(cfg) is None


def test_resource_pack_mapping_returns_value(tmp_path: Path) -> None:
    """Tests that _resource_pack_mapping returns the configured resource pack mapping value."""
    cfg = _config(tmp_path, partition=[], instances={})
    assert _resource_pack_mapping(cfg) == "@www/resourcepacks"


def test_resource_pack_mapping_none_when_absent(tmp_path: Path) -> None:
    """Tests that _resource_pack_mapping returns None when the mapping is absent."""
    cfg = _config(tmp_path, partition=[], instances={}, sync_mapping={"config": "config"})
    assert _resource_pack_mapping(cfg) is None


def test_needs_publish_missing_destination(tmp_path: Path) -> None:
    """Tests that _needs_publish returns True when the destination file does not exist."""
    dest = tmp_path / "missing.zip"
    assert _needs_publish(dest, "abc123")


def test_needs_publish_sha1_match(tmp_path: Path) -> None:
    """Tests that _needs_publish returns False when the destination SHA1 matches the expected value."""
    dest = tmp_path / "existing.zip"
    dest.write_bytes(b"content")
    sha = compute_sha1(dest)
    assert not _needs_publish(dest, sha)


def test_needs_publish_sha1_case_insensitive(tmp_path: Path) -> None:
    """Verifies that SHA-1 comparison for publishing is case-insensitive."""
    dest = tmp_path / "existing.zip"
    dest.write_bytes(b"content")
    sha = compute_sha1(dest).upper()
    assert not _needs_publish(dest, sha)


def test_needs_publish_sha1_differs(tmp_path: Path) -> None:
    """Verifies that publish is needed when the destination file's SHA-1 differs from the expected value."""
    dest = tmp_path / "existing.zip"
    dest.write_bytes(b"content")
    assert _needs_publish(dest, "0000000000000000000000000000000000000000")


def test_zero_pack_members_is_noop(tmp_path: Path) -> None:
    """Tests that a resource pack scope deployment with no pack members for a partition returns a successful no-op result."""
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, resource_packs={})
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert result.no_pack_members == ["survival"]
    assert result.publish_results == []
    assert result.properties_results == []


def test_zero_pack_whole_partition_no_source_dir(tmp_path: Path) -> None:
    """When no pack is configured, missing sync_mapping keys are fine."""
    inst = _instance("survival", tmp_path / "survival")
    cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, sync_mapping={"config": "config"})
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    assert result.no_pack_members == ["survival"]


def test_missing_source_is_error(tmp_path: Path) -> None:
    """Tests that a required resource pack with a missing source file causes validation failure with the expected error details."""
    inst = _instance("survival", tmp_path / "survival")
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
    assert "not found" in result.failure_message


def test_missing_client_mapping_is_error(tmp_path: Path) -> None:
    """Tests that a missing client mapping for a required resource pack results in an error mentioning the client is required."""
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
    assert "client is required" in result.failure_message


def test_missing_resource_pack_mapping_is_error(tmp_path: Path) -> None:
    """Tests that a missing resource pack mapping produces an error."""
    _source_rp(tmp_path)
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
    assert "resource_pack is required" in result.failure_message


def test_bad_filename_is_error(tmp_path: Path) -> None:
    """Tests that a resource pack with a disallowed filename extension causes a validation failure."""
    _source_rp(tmp_path, "pack.zip")
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


def test_publishes_missing_destination(tmp_path: Path) -> None:
    """Tests that a required resource pack is published to its destination when the destination is initially missing."""
    _source_rp(tmp_path, "pack.zip", b"CONTENT")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("", encoding="utf-8")
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
    assert pub.published is True
    assert pub.destination is not None
    assert pub.destination.read_bytes() == b"CONTENT"
    assert pub.destination == tmp_path / "www" / "resourcepacks" / "pack.zip"


def test_skips_publication_when_destination_matches(tmp_path: Path) -> None:
    """Tests that publication is skipped when the destination file already matches."""
    src = _source_rp(tmp_path, "pack.zip", b"CONTENT")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"CONTENT")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("", encoding="utf-8")
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


def test_republishes_when_source_changes(tmp_path: Path) -> None:
    """Tests that a resource pack is republished when its source content changes."""
    _source_rp(tmp_path, "pack.zip", b"NEW")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"OLD")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("", encoding="utf-8")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.any_published
    assert (dest_dir / "pack.zip").read_bytes() == b"NEW"


def test_writes_all_four_keys(tmp_path: Path) -> None:
    """Tests that deploying a resource pack writes all four server.properties keys."""
    _source_rp(tmp_path, "pack.zip", b"ZIPDATA")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("motd=hi\n", encoding="utf-8")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="Please accept")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert result.success
    props = inst.server_properties_path.read_text(encoding="utf-8")
    assert "motd=hi" in props
    assert "require-resource-pack=true" in props
    assert "resource-pack=http://minecraft/downloads/resourcepacks/pack.zip" in props
    assert "resource-pack-prompt=Please accept" in props
    sha1 = compute_sha1(tmp_path / "sync" / "resourcepacks" / "pack.zip")
    assert f"resource-pack-sha1={sha1}" in props


def test_empty_prompt_written_as_empty(tmp_path: Path) -> None:
    """§4.15: empty prompt is written as `resource-pack-prompt=`."""
    _source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("", encoding="utf-8")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    props = inst.server_properties_path.read_text(encoding="utf-8")
    assert "resource-pack-prompt=\n" in props


def test_required_false_writes_lowercase(tmp_path: Path) -> None:
    """Tests that a non-required resource pack writes require-resource-pack=false."""
    _source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("", encoding="utf-8")
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=False, prompt="")},
    )
    deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    props = inst.server_properties_path.read_text(encoding="utf-8")
    assert "require-resource-pack=false" in props


def test_prompt_only_marks_prompt_only(tmp_path: Path) -> None:
    """§4.15: only the prompt differs → properties write, no publish."""
    src = _source_rp(tmp_path, "pack.zip", b"ZIPDATA")
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True)
    (dest_dir / "pack.zip").write_bytes(b"ZIPDATA")
    sha1 = compute_sha1(src)
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text(
        f"require-resource-pack=true\nresource-pack=http://minecraft/downloads/resourcepacks/pack.zip\nresource-pack-prompt=OLD\nresource-pack-sha1={sha1}\n",
        encoding="utf-8",
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
    assert props.changes == {"resource-pack-prompt": ("OLD", "NEW")}
    assert props.prompt_only


def test_publish_failure_stops_properties_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish failure must not touch server.properties."""
    _source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    original = "motd=hi\n"
    inst.server_properties_path.write_text(original, encoding="utf-8")
    from minecraft.deploy_pack import scope_resource_pack as rps

    def boom(*a, **kw):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(rps, "atomic_copy", boom)
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
    assert inst.server_properties_path.read_text(encoding="utf-8") == original


def test_properties_failure_after_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A properties failure occurs after publish; the publish stands (§4.2)."""
    _source_rp(tmp_path, "pack.zip")
    inst = _instance("survival", tmp_path / "survival")
    inst.server_properties_path.write_text("motd=hi\n", encoding="utf-8")
    from minecraft.deploy_pack import scope_resource_pack as rps

    def boom(*a, **kw):
        raise ConfigError("simulated properties failure")

    from minecraft.deploy_pack.errors import ConfigError

    monkeypatch.setattr(rps, "apply_edits", boom)
    cfg = _config(
        tmp_path,
        partition=["survival"],
        instances={"survival": inst},
        resource_packs={"survival": ResourcePackConfig(filename="pack.zip", required=True, prompt="")},
    )
    result = deploy_resource_pack_scope(cfg, _plan(["survival"]), [], None)
    assert not result.success
    assert result.failure_stage == "properties"
    assert (tmp_path / "www" / "resourcepacks" / "pack.zip").is_file()


def test_two_members_mixed_packs(tmp_path: Path) -> None:
    """One member with a pack, one without."""
    _source_rp(tmp_path, "pack.zip")
    a = _instance("a", tmp_path / "a")
    a.server_properties_path.write_text("", encoding="utf-8")
    b = _instance("b", tmp_path / "b")
    b.server_properties_path.write_text("", encoding="utf-8")
    cfg = _config(
        tmp_path, partition=["a", "b"], instances={"a": a, "b": b}, resource_packs={"a": ResourcePackConfig(filename="pack.zip", required=True, prompt="")}
    )
    result = deploy_resource_pack_scope(cfg, _plan(["a", "b"]), [], None)
    assert result.success
    assert result.no_pack_members == ["b"]
    assert len(result.publish_results) == 1
    assert result.publish_results[0].member == "a"
    assert len(result.properties_results) == 1
