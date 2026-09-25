# tests/deploy_pack/test_preflight_changes.py

"""Tests for preflight.changes: mods, instance-server, resource-pack diffs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from minecraft.deploy_pack.preflight import changes as changes_mod
from minecraft.deploy_pack.properties import PropertiesDiff, PropertyChange

# ---------------------------------------------------------------------------
# compute_mods_change
# ---------------------------------------------------------------------------


def test_compute_mods_change_returns_none_when_mods_dir_none():
    """No mods_dir means there is nothing to diff."""
    config = SimpleNamespace(config_dir=Path("/cfg"), modpack_dir=Path("/mp"))
    assert changes_mod.compute_mods_change(config, None) is None


def test_compute_mods_change_classifies_added_updated_removed(monkeypatch, tmp_path):
    """The source set is diffed against hash_flat_dir's snapshot."""
    config = SimpleNamespace(config_dir=tmp_path, modpack_dir=tmp_path / "mp")
    sources = {
        "added.jar": tmp_path / "src" / "added.jar",
        "updated.jar": tmp_path / "src" / "updated.jar",
        "same.jar": tmp_path / "src" / "same.jar",
    }
    sha = {
        sources["added.jar"]: "a",
        sources["updated.jar"]: "b-new",
        sources["same.jar"]: "c",
    }
    monkeypatch.setattr(changes_mod, "load_side_overrides", lambda p: SimpleNamespace())
    monkeypatch.setattr(changes_mod.deps, "resolve_mod_sources", lambda mp, side, ovr: sources)
    monkeypatch.setattr(changes_mod, "compute_sha256", lambda p: sha[p])
    monkeypatch.setattr(
        changes_mod,
        "hash_flat_dir",
        lambda d: {"updated.jar": "b-old", "same.jar": "c", "removed.jar": "z"},
    )

    result = changes_mod.compute_mods_change(config, tmp_path / "mods")
    assert result is not None
    assert result.added == ["added.jar"]
    assert result.updated == ["updated.jar"]
    assert result.removed == ["removed.jar"]


def test_compute_mods_change_skips_unreadable_sources(monkeypatch, tmp_path):
    """An OSError from compute_sha256 excludes the mod from the source set."""
    config = SimpleNamespace(config_dir=tmp_path, modpack_dir=tmp_path / "mp")
    sources = {"bad.jar": tmp_path / "bad.jar", "good.jar": tmp_path / "good.jar"}

    def _sha(p):
        if p.name == "bad.jar":
            raise OSError("gone")
        return "good"

    monkeypatch.setattr(changes_mod, "load_side_overrides", lambda p: SimpleNamespace())
    monkeypatch.setattr(changes_mod.deps, "resolve_mod_sources", lambda mp, side, ovr: sources)
    monkeypatch.setattr(changes_mod, "compute_sha256", _sha)
    monkeypatch.setattr(changes_mod, "hash_flat_dir", lambda d: {})

    result = changes_mod.compute_mods_change(config, tmp_path / "mods")
    assert result is not None
    assert result.added == ["good.jar"]
    assert "bad.jar" not in result.added


# ---------------------------------------------------------------------------
# compute_instance_server_change
# ---------------------------------------------------------------------------


def test_instance_change_returns_empty_when_member_unknown():
    """A member not in config.instances yields an empty change set."""
    config = SimpleNamespace(instances={}, sync_mapping={}, sync_root=Path("/sync"))
    result = changes_mod.compute_instance_server_change(config, "ghost")
    assert result.member == "ghost"
    assert result.changed_paths == []


def test_instance_change_returns_empty_when_instance_root_none():
    """An instance without a /data bind is silently skipped."""
    config = SimpleNamespace(
        instances={"a": SimpleNamespace(instance_root=None)},
        sync_mapping={"config": "config"},
        sync_root=Path("/sync"),
    )
    result = changes_mod.compute_instance_server_change(config, "a")
    assert result.changed_paths == []


def test_instance_change_skips_unmapped_and_shared_destinations(monkeypatch, tmp_path):
    """Mappings that resolve to None or a shared destination are ignored."""
    inst = SimpleNamespace(instance_root=tmp_path / "inst")
    config = SimpleNamespace(
        instances={"a": inst},
        sync_mapping={"a": "skip-me", "b": "@shared/x", "c": "kept"},
        sync_root=tmp_path / "sync",
    )

    def _resolve(v, side):
        return {"skip-me": None, "@shared/x": "mods", "kept": "config"}[v]

    monkeypatch.setattr(changes_mod, "resolve_mapping_for_side", _resolve)
    monkeypatch.setattr(changes_mod, "is_shared_dest", lambda dest: dest == "mods")
    # The 'kept' source dir does not exist -> contributes nothing.
    result = changes_mod.compute_instance_server_change(config, "a")
    assert result.changed_paths == []


def test_instance_change_skips_when_source_not_dir(monkeypatch, tmp_path):
    """A missing source directory contributes nothing for that key."""
    inst = SimpleNamespace(instance_root=tmp_path / "inst")
    config = SimpleNamespace(
        instances={"a": inst},
        sync_mapping={"config": "config"},
        sync_root=tmp_path / "sync",
    )
    monkeypatch.setattr(changes_mod, "resolve_mapping_for_side", lambda v, side: v)
    monkeypatch.setattr(changes_mod, "is_shared_dest", lambda dest: False)

    result = changes_mod.compute_instance_server_change(config, "a")
    assert result.changed_paths == []


def test_instance_change_classifies_added_removed_updated(monkeypatch, tmp_path):
    """The three sets are computed from the source/dest hash maps."""
    sync = tmp_path / "sync"
    inst_root = tmp_path / "inst"
    src = sync / "config"
    src.mkdir(parents=True)

    inst = SimpleNamespace(instance_root=inst_root)
    config = SimpleNamespace(
        instances={"a": inst},
        sync_mapping={"config": "config"},
        sync_root=sync,
    )

    def _hash(path):
        if path == src:
            return {"added.json": "1", "changed.json": "new", "same.json": "same"}
        return {"changed.json": "old", "same.json": "same", "removed.json": "9"}

    monkeypatch.setattr(changes_mod, "resolve_mapping_for_side", lambda v, side: v)
    monkeypatch.setattr(changes_mod, "is_shared_dest", lambda dest: False)
    monkeypatch.setattr(changes_mod, "hash_tree", _hash)

    result = changes_mod.compute_instance_server_change(config, "a")
    assert "config/added.json" in result.added
    assert "config/changed.json" in result.updated
    assert "config/removed.json" in result.removed
    assert result.changed_paths == result.added + result.updated + result.removed


# ---------------------------------------------------------------------------
# compute_resource_pack_change
# ---------------------------------------------------------------------------


def _rp(filename="p.zip", required=True, prompt="hi"):
    """Build a minimal [resource_pack.X] stub."""
    return SimpleNamespace(filename=filename, required=required, prompt=prompt)


def _rp_full_config(tmp_path, *, www_dir):
    """Build a config that reaches the property-diff stage."""
    src_dir = tmp_path / "sync" / "rp"
    src_dir.mkdir(parents=True)
    zip_path = src_dir / "p.zip"
    zip_path.write_bytes(b"zip")
    sp = tmp_path / "sp"
    sp.write_bytes(b"")
    inst = SimpleNamespace(server_properties_path=sp)
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": {"resource_pack": "@www/pack", "client": "rp"}},
        sync_root=tmp_path / "sync",
        download_base_url="http://example.com",
        www_dir=www_dir,
    )
    return config, zip_path


def test_rp_change_returns_empty_when_no_pack_configured():
    """No [resource_pack.a] section -> empty change object."""
    config = SimpleNamespace(resource_packs={}, instances={})
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.member == "a"
    assert result.properties_changes == {}
    assert result.action == "none"
    assert result.publish_needed is False


def test_rp_change_returns_empty_when_instance_missing():
    """Pack configured for a member but no instance -> empty change."""
    config = SimpleNamespace(resource_packs={"a": _rp()}, instances={})
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_server_properties_path_none():
    """An instance without a server.properties path is skipped."""
    inst = SimpleNamespace(server_properties_path=None)
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_resourcepacks_not_dict(tmp_path):
    """A non-dict resourcepacks mapping is treated as absent."""
    inst = SimpleNamespace(server_properties_path=tmp_path / "sp")
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": "not-a-dict"},
        sync_root=tmp_path / "sync",
        download_base_url="http://x",
        www_dir=None,
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_dest_value_missing(tmp_path):
    """Missing resource_pack key in [sync_mapping.resourcepacks] -> empty."""
    inst = SimpleNamespace(server_properties_path=tmp_path / "sp")
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": {"client": "rp"}},
        sync_root=tmp_path / "sync",
        download_base_url="http://x",
        www_dir=None,
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_dest_value_not_str(tmp_path):
    """A non-str resource_pack value is treated as absent."""
    inst = SimpleNamespace(server_properties_path=tmp_path / "sp")
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": {"resource_pack": 42}},
        sync_root=tmp_path / "sync",
        download_base_url="http://x",
        www_dir=None,
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_client_sub_missing(tmp_path):
    """Without [sync_mapping.resourcepacks].client there is no source dir."""
    inst = SimpleNamespace(server_properties_path=tmp_path / "sp")
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": {"resource_pack": "@www/pack"}},
        sync_root=tmp_path / "sync",
        download_base_url="http://x",
        www_dir=None,
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_returns_empty_when_source_zip_missing(tmp_path):
    """A configured client dir with no matching zip -> empty."""
    inst = SimpleNamespace(server_properties_path=tmp_path / "sp")
    config = SimpleNamespace(
        resource_packs={"a": _rp()},
        instances={"a": inst},
        sync_mapping={"resourcepacks": {"resource_pack": "@www/pack", "client": "rp"}},
        sync_root=tmp_path / "sync",
        download_base_url="http://x",
        www_dir=None,
    )
    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.properties_changes == {}


def test_rp_change_full_flow_publish_and_restart(monkeypatch, tmp_path):
    """Restart keys changed + destination zip missing -> publish + restart."""
    www = tmp_path / "www"
    (www / "pack").mkdir(parents=True)
    config, zip_path = _rp_full_config(tmp_path, www_dir=www)

    monkeypatch.setattr(changes_mod, "compute_sha1", lambda p: "abc123")
    monkeypatch.setattr(
        changes_mod,
        "build_resource_pack_url",
        lambda base, dest, fn: f"{base}/{dest[5:]}/{fn}",
    )
    monkeypatch.setattr(
        changes_mod,
        "compute_diff",
        lambda path, edits: PropertiesDiff(changes=[PropertyChange("require-resource-pack", "false", "true")]),
    )

    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.source_sha1 == "abc123"
    assert result.source_zip == zip_path
    assert result.publish_needed is True
    assert "require-resource-pack" in result.properties_changes
    assert result.action == "restart"


def test_rp_change_prompt_only_change_means_none_action(monkeypatch, tmp_path):
    """Only the prompt changed + dest sha matches -> publish=False, action=none."""
    www = tmp_path / "www"
    dest_dir = www / "pack"
    dest_dir.mkdir(parents=True)
    (dest_dir / "p.zip").write_bytes(b"zip")
    config, _ = _rp_full_config(tmp_path, www_dir=www)

    monkeypatch.setattr(changes_mod, "compute_sha1", lambda p: "abc123")
    monkeypatch.setattr(changes_mod, "build_resource_pack_url", lambda b, d, f: "url")
    monkeypatch.setattr(
        changes_mod,
        "compute_diff",
        lambda path, edits: PropertiesDiff(changes=[PropertyChange("resource-pack-prompt", "old", "new")]),
    )

    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.publish_needed is False
    assert result.action == "none"


def test_rp_change_publish_needed_when_dest_sha_differs(monkeypatch, tmp_path):
    """Dest exists but sha1 differs from source -> publish_needed=True."""
    www = tmp_path / "www"
    dest_dir = www / "pack"
    dest_dir.mkdir(parents=True)
    (dest_dir / "p.zip").write_bytes(b"other")
    config, _ = _rp_full_config(tmp_path, www_dir=www)

    def _sha(p):
        if p.parent == dest_dir:
            return "different"
        return "abc123"

    monkeypatch.setattr(changes_mod, "compute_sha1", _sha)
    monkeypatch.setattr(changes_mod, "build_resource_pack_url", lambda b, d, f: "url")
    monkeypatch.setattr(
        changes_mod,
        "compute_diff",
        lambda path, edits: PropertiesDiff(changes=[PropertyChange("resource-pack-prompt", "", "hi")]),
    )

    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.publish_needed is True


def test_rp_change_publish_needed_when_dest_sha_raises(monkeypatch, tmp_path):
    """An OSError computing the dest sha1 falls back to publish_needed=True."""
    www = tmp_path / "www"
    dest_dir = www / "pack"
    dest_dir.mkdir(parents=True)
    (dest_dir / "p.zip").write_bytes(b"other")
    config, _ = _rp_full_config(tmp_path, www_dir=www)

    def _sha(p):
        if p.parent == dest_dir:
            raise OSError("can't read")
        return "abc123"

    monkeypatch.setattr(changes_mod, "compute_sha1", _sha)
    monkeypatch.setattr(changes_mod, "build_resource_pack_url", lambda b, d, f: "url")
    monkeypatch.setattr(
        changes_mod,
        "compute_diff",
        lambda path, edits: PropertiesDiff(changes=[PropertyChange("resource-pack-prompt", "", "hi")]),
    )

    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.publish_needed is True


def test_rp_change_no_www_dir_skips_publish_check(monkeypatch, tmp_path):
    """When www_dir is None the publish decision is never computed."""
    config, _ = _rp_full_config(tmp_path, www_dir=None)

    monkeypatch.setattr(changes_mod, "compute_sha1", lambda p: "abc123")
    monkeypatch.setattr(changes_mod, "build_resource_pack_url", lambda b, d, f: "url")
    monkeypatch.setattr(
        changes_mod,
        "compute_diff",
        lambda path, edits: PropertiesDiff(changes=[PropertyChange("resource-pack-sha1", None, "abc123")]),
    )

    result = changes_mod.compute_resource_pack_change(config, "a")
    assert result.publish_needed is False
    assert result.action == "restart"
