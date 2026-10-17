# tests/deploy_pack/test_preflight_changes_slip.py

"""Slip-mode resource-pack change computation tests (spec v1.3 Section 14.4, Section 14.6)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

import pytest

from minecraft.deploy_pack.config_model import (
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DockerConfig,
    InstanceConfig,
)
from minecraft.deploy_pack.preflight import changes as changes_mod
from minecraft.deploy_pack.preflight.changes import (
    _find_slip_rp,
    compute_resource_pack_change_from_context,
)
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.slip_context import (
    SlipContext,
    build_release_context,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _hex(ch: str) -> str:
    """Return a 64-character hex string of repeated ``ch``."""
    return ch * 64


def _sha1_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-1 of ``data``."""
    return hashlib.sha1(data).hexdigest()


def _slip_dict(rps: list[dict]) -> dict:
    """Build a valid slip dict with the given [[resource_pack]] entries."""
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
        "jar": [],
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
    rp_required: dict[str, bool] | None = None,
    rp_prompts: dict[str, str] | None = None,
) -> SlipContext:
    """Build a SlipContext with the given RP ZIPs."""
    rp_inst = rp_instances or {}
    rp_req = rp_required or {}
    rp_prompt = rp_prompts or {}
    rps = [
        {
            "filename": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "instance": rp_inst.get(name, "survival"),
            "required": rp_req.get(name, True),
            "prompt": rp_prompt.get(name, ""),
        }
        for name, content in rp_contents.items()
    ]
    parsed = slip_mod.parse_slip(_slip_dict(rps))
    root = tmp_path / "bundle"
    (root / "artifacts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "kubejs").mkdir()
    (root / "runtime").mkdir()
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
    slip_ctx: SlipContext,
    *,
    server_properties_exists: bool = True,
    sync_mapping: dict | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with a single survival instance."""
    inst = InstanceConfig(
        name="survival",
        container="mc-survival",
        config_mode="merge",
        kubejs_mode="delete",
    )
    inst.service = _make_service()
    inst.instance_root = tmp_path / "inst"
    inst.instance_root.mkdir(parents=True, exist_ok=True)
    if server_properties_exists:
        inst.server_properties_path = inst.instance_root / "server.properties"
        inst.server_properties_path.write_bytes(b"motd=test\n")
    else:
        inst.server_properties_path = None
    if sync_mapping is None:
        sync_mapping = {"resourcepacks": {"resource_pack": "@www/resourcepacks"}}
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
        sync_mapping=sync_mapping,
        restart_policy={},
        instances={"survival": inst},
        partition=["survival"],
        partition_unknown=[],
        requested_instances=None,
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml"),
        compose=_make_compose(),
        slip=slip_ctx,
    )


def _seed_dest_zip(tmp_path: Path, filename: str, content: bytes) -> Path:
    """Create the publish destination file and return its path."""
    dest_dir = tmp_path / "www" / "resourcepacks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(content)
    return dest


def _write_server_properties(
    config: DeploymentConfig,
    *,
    require: str,
    url: str,
    sha1: str,
    prompt: str,
) -> None:
    """Write a server.properties with the given resource-pack key values."""
    props = config.instances["survival"].server_properties_path
    assert props is not None
    props.write_bytes((f"motd=test\nrequire-resource-pack={require}\nresource-pack={url}\nresource-pack-sha1={sha1}\nresource-pack-prompt={prompt}\n").encode())


_HTTP_PACK_URL = "https://example.com/resourcepacks/survival-pack.zip"


# ===========================================================================
# _find_slip_rp
# ===========================================================================


def test_find_slip_rp_no_match(tmp_path: Path) -> None:
    """No slip RP for the member returns None."""
    ctx = _build_ctx(tmp_path, {})
    assert _find_slip_rp(ctx, "survival") is None


def test_find_slip_rp_single_match(tmp_path: Path) -> None:
    """One matching slip RP is returned."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    rp = _find_slip_rp(ctx, "survival")
    assert rp is not None
    assert rp.filename == "survival-pack.zip"


def test_find_slip_rp_multiple_match_returns_first(tmp_path: Path) -> None:
    """Two slip RPs for one instance: the first declared wins."""
    ctx = _build_ctx(tmp_path, {"first.zip": b"a", "second.zip": b"b"})
    rp = _find_slip_rp(ctx, "survival")
    assert rp is not None
    assert rp.filename == "first.zip"


def test_find_slip_rp_only_one_matches(tmp_path: Path) -> None:
    """With RPs on multiple instances, only the matching one is returned."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"a", "creative-pack.zip": b"b"},
        rp_instances={
            "survival-pack.zip": "survival",
            "creative-pack.zip": "creative",
        },
    )
    rp = _find_slip_rp(ctx, "survival")
    assert rp is not None
    assert rp.filename == "survival-pack.zip"


# ===========================================================================
# compute_resource_pack_change_from_context - early returns
# ===========================================================================


def test_rp_change_no_slip_rp(tmp_path: Path) -> None:
    """No slip RP for the member yields an empty change."""
    ctx = _build_ctx(tmp_path, {})
    config = _make_config(tmp_path, ctx)
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.member == "survival"
    assert change.source_sha1 is None
    assert change.source_zip is None
    assert change.publish_needed is False
    assert change.action == "none"
    assert change.properties_changes == {}


def test_rp_change_instance_missing(tmp_path: Path) -> None:
    """An instance absent from config.instances yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx)
    config.instances = {}
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None
    assert change.action == "none"


def test_rp_change_no_server_properties(tmp_path: Path) -> None:
    """An instance without server_properties_path yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx, server_properties_exists=False)
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None
    assert change.action == "none"


def test_rp_change_no_sync_mapping_resourcepacks(tmp_path: Path) -> None:
    """Missing [sync_mapping].resourcepacks yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx, sync_mapping={})
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None


def test_rp_change_resourcepacks_not_dict(tmp_path: Path) -> None:
    """[sync_mapping].resourcepacks being a non-dict yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx, sync_mapping={"resourcepacks": "not-a-dict"})
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None


def test_rp_change_no_resource_pack_mapping(tmp_path: Path) -> None:
    """Missing [sync_mapping].resourcepacks.resource_pack yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx, sync_mapping={"resourcepacks": {"other": "value"}})
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None


def test_rp_change_resource_pack_empty(tmp_path: Path) -> None:
    """Empty resource_pack value yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx, sync_mapping={"resourcepacks": {"resource_pack": ""}})
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None


def test_rp_change_source_missing_from_rp_sources(tmp_path: Path) -> None:
    """An RP in slip.resource_packs but not in rp_sources yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    mismatched = SlipContext(
        slip=ctx.slip,
        mode=ctx.mode,
        bundle_root=ctx.bundle_root,
        config_source=ctx.config_source,
        kubejs_source=ctx.kubejs_source,
        jar_sources=ctx.jar_sources,
        rp_sources=MappingProxyType({}),
        runtime_payload=ctx.runtime_payload,
        captured_head=ctx.captured_head,
    )
    config = _make_config(tmp_path, mismatched)
    change = compute_resource_pack_change_from_context(config, "survival", mismatched)
    assert change.source_sha1 is None


def test_rp_change_source_zip_deleted_after_construction(tmp_path: Path) -> None:
    """Deleting the source ZIP after SlipContext construction yields an empty change."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    (ctx.bundle_root / "artifacts" / "survival-pack.zip").unlink()
    config = _make_config(tmp_path, ctx)
    change = compute_resource_pack_change_from_context(config, "survival", ctx)
    assert change.source_sha1 is None


# ===========================================================================
# compute_resource_pack_change_from_context - happy paths
# ===========================================================================


def test_rp_change_happy_path_fresh_properties(tmp_path: Path) -> None:
    """Fresh server.properties + no dest ZIP => all 4 keys added, publish needed."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.source_sha1 == _sha1_hex(b"pk1")
    assert change.source_zip is not None
    assert change.publish_needed is True
    assert change.action == "restart"
    assert set(change.properties_changes) == {
        "require-resource-pack",
        "resource-pack",
        "resource-pack-prompt",
        "resource-pack-sha1",
    }


def test_rp_change_properties_before_after_values(tmp_path: Path) -> None:
    """The properties_changes map carries (before, after) tuples with correct values."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    for key in (
        "require-resource-pack",
        "resource-pack",
        "resource-pack-prompt",
        "resource-pack-sha1",
    ):
        before, after = change.properties_changes[key]
        assert before is None
        assert isinstance(after, str)
    assert change.properties_changes["require-resource-pack"] == (None, "true")
    assert change.properties_changes["resource-pack"] == (None, _HTTP_PACK_URL)


def test_rp_change_dest_matches_no_publish(tmp_path: Path) -> None:
    """Destination matches source => publish_needed=False, but restart still fires."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    _seed_dest_zip(tmp_path, "survival-pack.zip", b"pk1")
    config = _make_config(tmp_path, ctx)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.publish_needed is False
    assert change.action == "restart"


def test_rp_change_dest_differs_publish_needed(tmp_path: Path) -> None:
    """Destination content differs => publish_needed=True."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    _seed_dest_zip(tmp_path, "survival-pack.zip", b"old")
    config = _make_config(tmp_path, ctx)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.publish_needed is True
    assert change.action == "restart"


def test_rp_change_dest_hash_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If hashing the destination raises OSError, publish_needed=True."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    _seed_dest_zip(tmp_path, "survival-pack.zip", b"pk1")
    config = _make_config(tmp_path, ctx)

    original = changes_mod.compute_sha1

    def fake_sha1(path: Path) -> str:
        """Raise OSError when hashing the destination file."""
        if path.name == "survival-pack.zip" and "resourcepacks" in str(path):
            raise OSError("simulated read error")
        return original(path)

    monkeypatch.setattr(changes_mod, "compute_sha1", fake_sha1)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.publish_needed is True


def test_rp_change_unresolvable_dest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise from resolve_resource_pack_dest is swallowed; publish_needed stays False."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx)

    def fake_resolve(_value: str, _www_dir: Path) -> Path:
        """Simulate a destination resolution failure."""
        raise changes_mod.resolve_resource_pack_dest.__globals__["ConfigError"](  # type: ignore[attr-defined]
            "simulated resolution failure"
        )

    monkeypatch.setattr(changes_mod, "resolve_resource_pack_dest", fake_resolve)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.publish_needed is False
    assert change.source_sha1 is not None
    assert change.action == "restart"


def test_rp_change_prompt_only_action_none(tmp_path: Path) -> None:
    """Only the prompt differs => properties_changes only carries prompt, action='none'."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_prompts={"survival-pack.zip": "Welcome!"},
    )
    _seed_dest_zip(tmp_path, "survival-pack.zip", b"pk1")
    config = _make_config(tmp_path, ctx)
    _write_server_properties(
        config,
        require="true",
        url=_HTTP_PACK_URL,
        sha1=_sha1_hex(b"pk1"),
        prompt="old",
    )

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert set(change.properties_changes) == {"resource-pack-prompt"}
    assert change.properties_changes["resource-pack-prompt"] == ("old", "Welcome!")
    assert change.action == "none"
    assert change.publish_needed is False


def test_rp_change_no_properties_changes_all_match(tmp_path: Path) -> None:
    """All keys already match => properties_changes empty, action='none'."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    _seed_dest_zip(tmp_path, "survival-pack.zip", b"pk1")
    config = _make_config(tmp_path, ctx)
    _write_server_properties(
        config,
        require="true",
        url=_HTTP_PACK_URL,
        sha1=_sha1_hex(b"pk1"),
        prompt="",
    )

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.properties_changes == {}
    assert change.action == "none"
    assert change.publish_needed is False


def test_rp_change_no_props_change_publish_needed(tmp_path: Path) -> None:
    """All keys match but the destination is missing => publish_needed=True."""
    ctx = _build_ctx(tmp_path, {"survival-pack.zip": b"pk1"})
    config = _make_config(tmp_path, ctx)
    _write_server_properties(
        config,
        require="true",
        url=_HTTP_PACK_URL,
        sha1=_sha1_hex(b"pk1"),
        prompt="",
    )

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    assert change.properties_changes == {}
    assert change.action == "none"
    assert change.publish_needed is True


def test_rp_change_not_required(tmp_path: Path) -> None:
    """required=False sets require-resource-pack='false' and triggers a restart."""
    ctx = _build_ctx(
        tmp_path,
        {"survival-pack.zip": b"pk1"},
        rp_required={"survival-pack.zip": False},
    )
    config = _make_config(tmp_path, ctx)

    change = compute_resource_pack_change_from_context(config, "survival", ctx)

    before, after = change.properties_changes["require-resource-pack"]
    assert before is None
    assert after == "false"
    assert change.action == "restart"
