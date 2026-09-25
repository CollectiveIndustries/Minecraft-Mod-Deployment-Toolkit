# tests/deploy_pack/test_preflight_extended.py

"""Extended tests for deploy_pack.preflight.

test_preflight.py covers the end-to-end aggregation of failures and the
high-level planning logic. This module covers the helpers and branches
that file leaves unexercised:

  * ScopeSet helpers (``names``, ``bitmask``)
  * ``_is_pack_action`` / ``_hash_flat_dir`` / ``_hash_tree``
  * ``_is_unmarked`` and ``_load_server_mod_entries`` override handling
  * ``_compute_mods_change`` added / removed / updated paths
  * ``_compute_instance_server_change`` full diff
  * ``_resolve_mapping_value``
  * ``_compute_resource_pack_change``: publish-needed, restart-action,
    prompt-only variants
  * ``_classify_state`` state matrix
  * ``_check_rcon_available`` skip branches
  * ``run_preflight`` branches: ``www_dir_candidates`` warning,
    ``mods_dir_toml`` mismatch, unhealthy / starting warnings,
    service-match and stop-grace parse errors, ``restarting`` settle,
    RP filename validation failure, missing RP source, ``pack_required``
    warning with and without a client scope, online validation.

Fixtures stay self-contained: a fake runtime wired to the config's
compose file (so the §3.17 mount-drift check sees a matching set), a
compose-file builder, and a config builder, all copied from the sibling
file and trimmed to what these tests need. Where a branch only fires
when a real logger is present, tests pass the module logger explicitly
rather than ``None``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from minecraft.deploy_pack import preflight
from minecraft.deploy_pack.config_model import (
    BindMount,
    ComposeFile,
    ComposeLoadResult,
    ComposeService,
    DeploymentConfig,
    DiscordConfig,
    DockerConfig,
    InstanceConfig,
    ResourcePackConfig,
    ServiceMatchError,
    match_service_by_container,
    parse_go_duration,
)
from minecraft.deploy_pack.docker_runtime import ContainerState, Mount
from minecraft.deploy_pack.errors import ConfigError
from minecraft.deploy_pack.preflight import (
    ModsChange,
    PreflightError,
    ScopeSet,
    _check_rcon_available,
    _classify_state,
    _compute_instance_server_change,
    _compute_mods_change,
    _compute_resource_pack_change,
    _hash_flat_dir,
    _hash_tree,
    _is_pack_action,
    _is_unmarked,
    _load_server_mod_entries,
    _resolve_mapping_value,
    run_preflight,
)

# ---------------------------------------------------------------------------
# Shared logger
# ---------------------------------------------------------------------------


def _logger() -> logging.Logger:
    """Return a module-scoped logger for tests that need a real sink."""
    return logging.getLogger("test_preflight_extended")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Runtime:
    """Runtime stub wired to the config's compose file.

    ``list_mounts`` mirrors the compose service's binds so the §3.17
    drift check sees a set matching the instance-root derivations. A
    bare stub returning ``[]`` would fail that check on every server
    test that reaches the lifecycle block.
    """

    states: dict[str, ContainerState] = field(default_factory=dict)
    inspect_calls: list[str] = field(default_factory=list)
    restarting_settled: dict[str, ContainerState] = field(default_factory=dict)
    ping_calls: int = 0
    published: dict[str, dict] = field(default_factory=dict)
    compose: ComposeFile | None = None

    def ping(self) -> None:
        """Record a ping call."""
        self.ping_calls += 1

    def inspect(self, name: str) -> ContainerState:
        """Return the configured state or a healthy default."""
        self.inspect_calls.append(name)
        return self.states.get(name, ContainerState(name=name, exists=True, status="running", running=True, health="healthy", raw={"Mounts": []}))

    def wait_for_restarting_settle(self, names, total_timeout, poll_interval):
        """Return the settled states or re-inspect each name."""
        return self.restarting_settled or {n: self.inspect(n) for n in names}

    def list_mounts(self, name: str) -> list[Mount]:
        """Return the compose service's binds for ``name``, or empty."""
        if self.compose is None:
            return []
        for svc in self.compose.services.values():
            if svc.container_name == name:
                return [Mount(source=str(b.host_source), destination=b.container_target) for b in svc.binds]
        return []

    def published_ports(self, name: str) -> dict:
        """Return published-port mappings or empty."""
        return self.published.get(name, {})


def _compose_file(tmp_path: Path, members: dict[str, str]) -> ComposeFile:
    """Build a ComposeFile with one service per member plus nginx."""
    services: dict[str, ComposeService] = {}
    for name, container in members.items():
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        mods = tmp_path / "shared_mods"
        mods.mkdir(exist_ok=True)
        services[name] = ComposeService(
            name=name,
            container_name=container,
            binds=[BindMount(host_source=root, container_target="/data"), BindMount(host_source=mods, container_target="/data/mods")],
            stop_grace_period="10s",
            stop_signal=None,
            has_healthcheck=True,
            secrets=["rcon_password"],
            environment={"RCON_PORT": "25575"},
            env_files=[],
        )
    www = tmp_path / "www"
    www.mkdir(exist_ok=True)
    services["nginx"] = ComposeService(
        name="nginx",
        container_name="nginx",
        binds=[BindMount(host_source=www, container_target="/usr/share/nginx/html")],
        stop_grace_period=None,
        stop_signal=None,
        has_healthcheck=False,
        secrets=[],
        environment={},
        env_files=[],
    )
    secret_path = tmp_path / "rcon.txt"
    secret_path.write_text("pw\n", encoding="utf-8")
    return ComposeFile(path=tmp_path / "docker-compose.yml", base_dir=tmp_path, services=services, secret_files={"rcon_password": secret_path})


def _runtime(cfg: DeploymentConfig, **kwargs: Any) -> _Runtime:
    """Return a :class:`_Runtime` wired to ``cfg``'s compose file."""
    compose = cfg.compose.file if cfg.compose.ok else None
    return _Runtime(compose=compose, **kwargs)


def _instance(name: str, container: str, root: Path) -> InstanceConfig:
    """Return an InstanceConfig rooted at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    return InstanceConfig(
        name=name,
        container=container,
        instance_root=root,
        config_path=root / "config",
        kubejs_path=root / "kubejs",
        server_properties_path=root / "server.properties",
    )


def _config(
    tmp_path: Path,
    *,
    partition: list[str] | None = None,
    instances: dict[str, InstanceConfig] | None = None,
    compose_ok: bool = True,
    requested_instances: set[str] | None = None,
    partition_unknown: list[str] | None = None,
    resource_packs: dict[str, ResourcePackConfig] | None = None,
    restart_policy: dict[str, str] | None = None,
    discord: DiscordConfig | None = None,
    rcon_host: str | None = None,
    www_dir: Path | None = None,
    www_dir_error: str | None = None,
    www_dir_candidates: list[Path] | None = None,
    mods_dir_toml: Path | None = None,
    sync_mapping: dict | None = None,
) -> DeploymentConfig:
    """Build a DeploymentConfig with the given overrides."""
    partition = partition if partition is not None else ["survival"]
    instances = instances if instances is not None else {}
    compose: ComposeLoadResult
    if compose_ok:
        cf = _compose_file(tmp_path, {k: v.container for k, v in instances.items()})
        compose = ComposeLoadResult(file=cf, error=None)
        for inst in instances.values():
            try:
                svc = match_service_by_container(cf, inst.container)
            except ServiceMatchError as exc:
                inst.service_match_error = str(exc)
                continue
            inst.service = svc
            if svc.stop_grace_period:
                inst.stop_grace_period_raw = svc.stop_grace_period
                try:
                    inst.stop_grace_seconds = parse_go_duration(svc.stop_grace_period)
                except ValueError as exc:
                    inst.stop_grace_parse_error = str(exc)
            inst.stop_signal = svc.stop_signal
    else:
        compose = ComposeLoadResult(file=None, error="compose broken")
    www = www_dir if www_dir is not None else tmp_path / "www"
    www.mkdir(exist_ok=True)
    return DeploymentConfig(
        project_root=tmp_path,
        config_dir=tmp_path / "config.d",
        sync_root=tmp_path / "sync",
        modpack_dir=tmp_path / "sync" / "downloads",
        www_dir=www,
        www_dir_error=www_dir_error,
        www_dir_candidates=www_dir_candidates or [],
        output_filename="minecraft_client_{date}.zip",
        download_base_url="http://minecraft/downloads",
        protect_file=None,
        sync_mapping=sync_mapping
        or {
            "config": "config",
            "kubejs": "kubejs",
            "resourcepacks": {"resource_pack": "@www/resourcepacks", "client": "resourcepacks"},
        },
        restart_policy=restart_policy
        or {
            "config/*": "restart",
            "kubejs/client_scripts/*": "none",
            "kubejs/server_scripts/*": "reload",
            "kubejs/startup_scripts/*": "restart+pack",
            "kubejs/assets/*": "none+pack",
            "kubejs/data/*": "reload",
            "mods/*": "restart",
        },
        instances=instances,
        partition=partition,
        partition_unknown=partition_unknown or [],
        requested_instances=requested_instances,
        resource_packs=resource_packs or {},
        docker=DockerConfig(compose_file=tmp_path / "docker-compose.yml", health_poll_seconds=1, preflight_restarting_wait_seconds=1, rcon_host=rcon_host),
        discord=discord or DiscordConfig(),
        webhook_url=None,
        compose=compose,
        mods_dir_toml=mods_dir_toml,
    )


# ---------------------------------------------------------------------------
# ScopeSet helpers
# ---------------------------------------------------------------------------


class TestScopeSetHelpers:
    """Tests for :class:`ScopeSet`'s small helpers."""

    def test_names_empty(self) -> None:
        """Tests that an empty scope set yields no names."""
        assert ScopeSet().names() == []

    def test_names_ordered(self) -> None:
        """Tests that scope names come out in §5.4's fixed order."""
        assert ScopeSet(server=True, client=True, resource_pack=True).names() == ["server", "client", "resource-pack"]
        assert ScopeSet(client=True, resource_pack=True).names() == ["client", "resource-pack"]

    def test_bitmask_empty(self) -> None:
        """Tests that an empty scope set yields a zero bitmask."""
        assert ScopeSet().bitmask() == 0

    def test_bitmask_combined(self) -> None:
        """Tests that each scope contributes its §5.3 bit to the mask."""
        assert ScopeSet(server=True).bitmask() == 1
        assert ScopeSet(client=True).bitmask() == 2
        assert ScopeSet(resource_pack=True).bitmask() == 4
        assert ScopeSet(server=True, client=True, resource_pack=True).bitmask() == 7
        assert ScopeSet(client=True, resource_pack=True).bitmask() == 6


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestIsPackAction:
    """Tests for :func:`_is_pack_action`."""

    def test_positive(self) -> None:
        """Tests that actions ending in +pack are recognized."""
        assert _is_pack_action("none+pack")
        assert _is_pack_action("reload+pack")
        assert _is_pack_action("restart+pack")

    def test_negative(self) -> None:
        """Tests that plain actions are not pack actions."""
        assert not _is_pack_action("none")
        assert not _is_pack_action("reload")
        assert not _is_pack_action("restart")


class TestResolveMappingValue:
    """Tests for :func:`_resolve_mapping_value`."""

    def test_string_value(self) -> None:
        """Tests that a plain string mapping is returned for both sides."""
        assert _resolve_mapping_value("config", "server") == "config"
        assert _resolve_mapping_value("config", "client") == "config"

    def test_dict_value_per_side(self) -> None:
        """Tests that a dict mapping returns the per-side value."""
        m = {"server": "srv_config", "client": "cli_config"}
        assert _resolve_mapping_value(m, "server") == "srv_config"
        assert _resolve_mapping_value(m, "client") == "cli_config"

    def test_dict_missing_side_returns_none(self) -> None:
        """Tests that a dict without the requested side yields None."""
        assert _resolve_mapping_value({"server": "x"}, "client") is None

    def test_shared_dest_returned(self) -> None:
        """Tests that a shared destination is returned verbatim for the caller to skip."""
        assert _resolve_mapping_value({"server": "@www/rp", "client": "@www/rp"}, "server") == "@www/rp"


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


class TestHashFlatDir:
    """Tests for :func:`_hash_flat_dir`."""

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        """Tests that a missing directory yields an empty map."""
        assert _hash_flat_dir(tmp_path / "nope") == {}

    def test_only_jars_collected(self, tmp_path: Path) -> None:
        """Tests that only .jar files are hashed."""
        d = tmp_path / "mods"
        d.mkdir()
        (d / "a.jar").write_bytes(b"aaa")
        (d / "b.jar").write_bytes(b"bbb")
        (d / "readme.txt").write_text("x")
        out = _hash_flat_dir(d)
        assert set(out) == {"a.jar", "b.jar"}

    def test_hashes_differ_for_different_content(self, tmp_path: Path) -> None:
        """Tests that different content produces different hashes."""
        d = tmp_path / "mods"
        d.mkdir()
        (d / "a.jar").write_bytes(b"aaa")
        (d / "b.jar").write_bytes(b"bbb")
        out = _hash_flat_dir(d)
        assert out["a.jar"] != out["b.jar"]

    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        """Tests that identical content produces identical hashes."""
        d = tmp_path / "mods"
        d.mkdir()
        (d / "a.jar").write_bytes(b"same")
        (d / "b.jar").write_bytes(b"same")
        out = _hash_flat_dir(d)
        assert out["a.jar"] == out["b.jar"]


class TestHashTree:
    """Tests for :func:`_hash_tree`."""

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        """Tests that a missing directory yields an empty map."""
        assert _hash_tree(tmp_path / "nope") == {}

    def test_all_files_collected(self, tmp_path: Path) -> None:
        """Tests that every file in the tree is hashed, keyed by relative path."""
        root = tmp_path / "tree"
        (root / "a").mkdir(parents=True)
        (root / "a" / "one.txt").write_text("1")
        (root / "two.txt").write_text("2")
        out = _hash_tree(root)
        assert set(out) == {"a/one.txt", "two.txt"}

    def test_relative_paths_use_forward_slashes(self, tmp_path: Path) -> None:
        """Tests that nested paths use forward slashes regardless of OS."""
        root = tmp_path / "tree"
        (root / "deep" / "deeper").mkdir(parents=True)
        (root / "deep" / "deeper" / "x.txt").write_text("x")
        out = _hash_tree(root)
        assert "deep/deeper/x.txt" in out


# ---------------------------------------------------------------------------
# _is_unmarked / _load_server_mod_entries
# ---------------------------------------------------------------------------


class TestIsUnmarked:
    """Tests for :func:`_is_unmarked`."""

    def test_missing_side_raw_marked(self) -> None:
        """Tests that an entry without a side_raw key is marked."""
        assert not _is_unmarked({})

    def test_side_raw_none_marked(self) -> None:
        """Tests that an explicit None side_raw is marked."""
        assert not _is_unmarked({"side_raw": None})

    def test_in_set_marked(self) -> None:
        """Tests that client, server, and both are marked."""
        assert not _is_unmarked({"side_raw": "client"})
        assert not _is_unmarked({"side_raw": "server"})
        assert not _is_unmarked({"side_raw": "both"})

    def test_out_of_set_unmarked(self) -> None:
        """Tests that values outside {client, server, both} are unmarked."""
        assert _is_unmarked({"side_raw": "universal"})
        assert _is_unmarked({"side_raw": "none"})
        assert _is_unmarked({"side_raw": ""})


class _OvrStub:
    """Minimal SideOverrides stand-in for _load_server_mod_entries tests."""

    def __init__(self, *, by_id: dict | None = None, by_filename: dict | None = None, review: dict | None = None, empty: bool = True) -> None:
        self.by_id = by_id or {}
        self.by_filename = by_filename or {}
        self.deployment_tool_review = review or {}
        self._empty = empty

    def is_empty(self) -> bool:
        """Return whether the overrides are empty."""
        return self._empty


class TestLoadServerModEntries:
    """Tests for :func:`_load_server_mod_entries`."""

    def _prep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Create the .index directory and patch the deps pipeline."""
        index_dir = tmp_path / "sync" / "downloads" / ".index"
        index_dir.mkdir(parents=True)
        return index_dir

    def test_no_index_returns_empty(self, tmp_path: Path) -> None:
        """Tests that a missing .index directory short-circuits to []."""
        cfg = _config(tmp_path)
        assert _load_server_mod_entries(cfg, None) == []

    def test_empty_entries_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an empty index returns []."""
        self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: [])
        cfg = _config(tmp_path)
        assert _load_server_mod_entries(cfg, None) == []

    def test_marked_entries_pass_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that marked entries survive filtering when overrides are empty."""
        self._prep(tmp_path, monkeypatch)
        entries = [{"id": "a", "file": "a.jar", "side_raw": "server"}]
        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(preflight, "load_side_overrides", lambda p: _OvrStub(empty=True))
        monkeypatch.setattr(preflight.deps, "filter_prism_entries_by_side", lambda entries, side: list(entries))
        monkeypatch.setattr(
            preflight.deps,
            "expand_with_required",
            lambda **k: SimpleNamespace(entries=list(k["seed_entries"])),
        )
        cfg = _config(tmp_path)
        out = _load_server_mod_entries(cfg, None)
        assert out == entries

    def test_unmarked_entry_without_override_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an unmarked entry with no override is dropped before filtering."""
        self._prep(tmp_path, monkeypatch)
        entries = [
            {"id": "a", "file": "a.jar", "side_raw": "server"},
            {"id": "b", "file": "b.jar", "side_raw": "universal"},
        ]
        captured: dict[str, list] = {}

        def _capture(entries, side):
            captured["seen"] = list(entries)
            return list(entries)

        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(preflight, "load_side_overrides", lambda p: _OvrStub(empty=True))
        monkeypatch.setattr(preflight.deps, "filter_prism_entries_by_side", _capture)
        monkeypatch.setattr(preflight.deps, "expand_with_required", lambda **k: SimpleNamespace(entries=list(k["seed_entries"])))
        cfg = _config(tmp_path)
        _load_server_mod_entries(cfg, None)
        assert [e["id"] for e in captured["seen"]] == ["a"]

    def test_unmarked_entry_with_id_override_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an unmarked entry with a matching id override is kept."""
        self._prep(tmp_path, monkeypatch)
        entries = [{"id": "b", "file": "b.jar", "side_raw": "universal"}]
        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(preflight, "load_side_overrides", lambda p: _OvrStub(by_id={"b": "server"}, empty=False))
        monkeypatch.setattr(preflight, "apply_side_overrides", lambda entries, ovr: list(entries))
        monkeypatch.setattr(preflight.deps, "filter_prism_entries_by_side", lambda entries, side: list(entries))
        monkeypatch.setattr(preflight.deps, "expand_with_required", lambda **k: SimpleNamespace(entries=list(k["seed_entries"])))
        cfg = _config(tmp_path)
        out = _load_server_mod_entries(cfg, None)
        assert out == entries

    def test_unmarked_entry_with_filename_override_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an unmarked entry with a filename override is kept."""
        self._prep(tmp_path, monkeypatch)
        entries = [{"id": "b", "file": "b.jar", "side_raw": "universal"}]
        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(preflight, "load_side_overrides", lambda p: _OvrStub(by_filename={"b.jar": "server"}, empty=False))
        monkeypatch.setattr(preflight, "apply_side_overrides", lambda entries, ovr: list(entries))
        monkeypatch.setattr(preflight.deps, "filter_prism_entries_by_side", lambda entries, side: list(entries))
        monkeypatch.setattr(preflight.deps, "expand_with_required", lambda **k: SimpleNamespace(entries=list(k["seed_entries"])))
        cfg = _config(tmp_path)
        out = _load_server_mod_entries(cfg, None)
        assert out == entries

    def test_unmarked_entry_with_review_override_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an unmarked entry with a deployment_tool_review entry is kept."""
        self._prep(tmp_path, monkeypatch)
        entries = [{"id": "b", "file": "b.jar", "side_raw": "universal"}]
        monkeypatch.setattr(preflight.deps, "load_prism_index", lambda p: entries)
        monkeypatch.setattr(preflight, "load_side_overrides", lambda p: _OvrStub(review={"b.jar": "server"}, empty=False))
        monkeypatch.setattr(preflight, "apply_side_overrides", lambda entries, ovr: list(entries))
        monkeypatch.setattr(preflight.deps, "filter_prism_entries_by_side", lambda entries, side: list(entries))
        monkeypatch.setattr(preflight.deps, "expand_with_required", lambda **k: SimpleNamespace(entries=list(k["seed_entries"])))
        cfg = _config(tmp_path)
        out = _load_server_mod_entries(cfg, None)
        assert out == entries


# ---------------------------------------------------------------------------
# _compute_mods_change
# ---------------------------------------------------------------------------


class TestComputeModsChange:
    """Tests for :func:`_compute_mods_change`."""

    def test_none_mods_dir_returns_none(self, tmp_path: Path) -> None:
        """Tests that a None mods_dir short-circuits to None."""
        cfg = _config(tmp_path)
        assert _compute_mods_change(cfg, None, None) is None

    def test_added_removed_updated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that added, removed, and updated files are all reported."""
        cfg = _config(tmp_path)
        cfg.modpack_dir.mkdir(parents=True)
        index_dir = cfg.modpack_dir / ".index"
        index_dir.mkdir()
        (cfg.modpack_dir / "existing.jar").write_bytes(b"same")
        (cfg.modpack_dir / "updated.jar").write_bytes(b"new")
        (cfg.modpack_dir / "added.jar").write_bytes(b"added")
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()
        (mods_dir / "existing.jar").write_bytes(b"same")
        (mods_dir / "updated.jar").write_bytes(b"old")
        (mods_dir / "removed.jar").write_bytes(b"gone")

        entries = [
            {"file": "existing.jar", "side_raw": "server"},
            {"file": "updated.jar", "side_raw": "server"},
            {"file": "added.jar", "side_raw": "server"},
        ]
        monkeypatch.setattr(preflight, "_load_server_mod_entries", lambda c, log: entries)
        change = _compute_mods_change(cfg, mods_dir, None)
        assert change is not None
        assert change.added == ["added.jar"]
        assert change.removed == ["removed.jar"]
        assert change.updated == ["updated.jar"]
        assert change.any
        assert change.total == 3

    def test_skips_entries_without_source_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that entries whose source file is missing are skipped."""
        cfg = _config(tmp_path)
        cfg.modpack_dir.mkdir(parents=True)
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()
        entries = [{"file": "missing.jar", "side_raw": "server"}]
        monkeypatch.setattr(preflight, "_load_server_mod_entries", lambda c, log: entries)
        change = _compute_mods_change(cfg, mods_dir, None)
        assert change is not None
        assert change.added == []
        assert not change.any

    def test_skips_entries_without_filename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that entries without a filename key are skipped."""
        cfg = _config(tmp_path)
        cfg.modpack_dir.mkdir(parents=True)
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()
        entries = [{"id": "no-file", "side_raw": "server"}]
        monkeypatch.setattr(preflight, "_load_server_mod_entries", lambda c, log: entries)
        change = _compute_mods_change(cfg, mods_dir, None)
        assert change is not None
        assert change.added == []


class TestModsChangeHelpers:
    """Tests for :class:`ModsChange`'s derived properties."""

    def test_changed_paths_prefixes_mods(self) -> None:
        """Tests that changed_paths prefixes each entry with mods/."""
        m = ModsChange(added=["a.jar"], updated=["b.jar"], removed=["c.jar"])
        assert m.changed_paths() == ["mods/a.jar", "mods/b.jar", "mods/c.jar"]

    def test_empty_change(self) -> None:
        """Tests that an empty ModsChange reports any=False and total=0."""
        m = ModsChange()
        assert not m.any
        assert m.total == 0


# ---------------------------------------------------------------------------
# _compute_instance_server_change
# ---------------------------------------------------------------------------


class TestComputeInstanceServerChange:
    """Tests for :func:`_compute_instance_server_change`."""

    def test_missing_instance_returns_empty(self, tmp_path: Path) -> None:
        """Tests that an unknown member yields an empty change."""
        cfg = _config(tmp_path)
        change = _compute_instance_server_change(cfg, "ghost", None)
        assert change.member == "ghost"
        assert change.changed_paths == []

    def test_added_removed_updated(self, tmp_path: Path) -> None:
        """Tests that added, removed, and updated files under a tree are reported."""
        cfg = _config(tmp_path, sync_mapping={"config": "config"})
        sync_config = cfg.sync_root / "config"
        (sync_config / "sub").mkdir(parents=True)
        (sync_config / "same.txt").write_text("same")
        (sync_config / "updated.txt").write_text("new")
        (sync_config / "added.txt").write_text("added")
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        inst_root = tmp_path / "survival"
        (inst_root / "config" / "sub").mkdir(parents=True)
        (inst_root / "config" / "same.txt").write_text("same")
        (inst_root / "config" / "updated.txt").write_text("old")
        (inst_root / "config" / "removed.txt").write_text("gone")
        cfg.instances["survival"] = inst
        change = _compute_instance_server_change(cfg, "survival", None)
        assert "config/added.txt" in change.added
        assert "config/removed.txt" in change.removed
        assert "config/updated.txt" in change.updated
        assert change.any

    def test_shared_dest_skipped(self, tmp_path: Path) -> None:
        """Tests that @www/... destinations are skipped."""
        cfg = _config(tmp_path, sync_mapping={"resourcepacks": {"server": "@www/rp", "client": "resourcepacks"}})
        (cfg.sync_root / "resourcepacks").mkdir(parents=True)
        (cfg.sync_root / "resourcepacks" / "x.zip").write_bytes(b"x")
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg.instances["survival"] = inst
        change = _compute_instance_server_change(cfg, "survival", None)
        assert change.added == []

    def test_missing_source_dir_skipped(self, tmp_path: Path) -> None:
        """Tests that a mapping whose source dir doesn't exist is skipped."""
        cfg = _config(tmp_path, sync_mapping={"config": "config"})
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg.instances["survival"] = inst
        change = _compute_instance_server_change(cfg, "survival", None)
        assert change.added == []


# ---------------------------------------------------------------------------
# _compute_resource_pack_change
# ---------------------------------------------------------------------------


class TestComputeResourcePackChange:
    """Tests for :func:`_compute_resource_pack_change`."""

    def _setup(
        self,
        tmp_path: Path,
        *,
        source_exists: bool = True,
        dest_exists: bool = False,
        diff_changes: list | None = None,
    ) -> DeploymentConfig:
        client_sub = "resourcepacks"
        source_dir = tmp_path / "sync" / client_sub
        source_dir.mkdir(parents=True)
        if source_exists:
            (source_dir / "pack.zip").write_bytes(b"zip")
        www = tmp_path / "www"
        (www / "resourcepacks").mkdir(parents=True)
        if dest_exists:
            (www / "resourcepacks" / "pack.zip").write_bytes(b"zip")
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        props = inst.server_properties_path
        props.parent.mkdir(parents=True, exist_ok=True)
        props.write_text("resource-pack=\n", encoding="utf-8")
        rp = ResourcePackConfig(filename="pack.zip", required=True, prompt="Please accept")
        cfg = _config(
            tmp_path,
            instances={"survival": inst},
            resource_packs={"survival": rp},
            www_dir=www,
            sync_mapping={"resourcepacks": {"resource_pack": "@www/resourcepacks", "client": client_sub}},
        )
        return cfg

    def test_missing_rp_config_returns_empty(self, tmp_path: Path) -> None:
        """Tests that a member without an RP config yields an empty change."""
        cfg = _config(tmp_path)
        change = _compute_resource_pack_change(cfg, "survival", None)
        assert change.member == "survival"
        assert change.properties_changes == {}
        assert change.action == "none"

    def test_missing_source_zip_returns_empty(self, tmp_path: Path) -> None:
        """Tests that a missing source zip yields an empty change."""
        cfg = self._setup(tmp_path, source_exists=False)
        change = _compute_resource_pack_change(cfg, "survival", None)
        assert change.source_sha1 is None
        assert change.properties_changes == {}

    def test_source_present_computes_sha1(self, tmp_path: Path) -> None:
        """Tests that the source zip's SHA1 is recorded on the change."""
        cfg = self._setup(tmp_path)
        change = _compute_resource_pack_change(cfg, "survival", None)
        assert change.source_sha1 is not None

    def test_publish_needed_when_dest_missing(self, tmp_path: Path) -> None:
        """Tests that a missing dest zip marks publish_needed True."""
        cfg = self._setup(tmp_path, dest_exists=False)
        change = _compute_resource_pack_change(cfg, "survival", None)
        assert change.publish_needed is True

    def test_publish_not_needed_when_dest_matches(self, tmp_path: Path) -> None:
        """Tests that matching dest zip content marks publish_needed False."""
        cfg = self._setup(tmp_path, dest_exists=True)
        change = _compute_resource_pack_change(cfg, "survival", None)
        assert change.publish_needed is False


# ---------------------------------------------------------------------------
# _classify_state
# ---------------------------------------------------------------------------


class TestClassifyState:
    """Tests for :func:`_classify_state`."""

    def test_missing(self) -> None:
        """Tests that a missing container produces a diagnostic."""
        state = ContainerState(name="c", exists=False, status="missing", running=False, health=None, raw=None)
        assert _classify_state(state, "c") is not None

    def test_running_healthy(self) -> None:
        """Tests that a running, healthy container has no diagnostic."""
        state = ContainerState(name="c", exists=True, status="running", running=True, health="healthy", raw={})
        assert _classify_state(state, "c") is None

    def test_running_no_health(self) -> None:
        """Tests that running without a healthcheck produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="running", running=True, health=None, raw={})
        assert _classify_state(state, "c") is not None

    def test_exited_ok(self) -> None:
        """Tests that an exited container has no diagnostic."""
        state = ContainerState(name="c", exists=True, status="exited", running=False, health=None, raw={})
        assert _classify_state(state, "c") is None

    def test_created_ok(self) -> None:
        """Tests that a created container has no diagnostic."""
        state = ContainerState(name="c", exists=True, status="created", running=False, health=None, raw={})
        assert _classify_state(state, "c") is None

    def test_stopped_ok(self) -> None:
        """Tests that a stopped container has no diagnostic."""
        state = ContainerState(name="c", exists=True, status="stopped", running=False, health=None, raw={})
        assert _classify_state(state, "c") is None

    def test_paused_error(self) -> None:
        """Tests that a paused container produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="paused", running=False, health=None, raw={})
        assert _classify_state(state, "c") is not None

    def test_removing_error(self) -> None:
        """Tests that a removing container produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="removing", running=False, health=None, raw={})
        assert _classify_state(state, "c") is not None

    def test_dead_error(self) -> None:
        """Tests that a dead container produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="dead", running=False, health=None, raw={})
        assert _classify_state(state, "c") is not None

    def test_restarting_error(self) -> None:
        """Tests that a still-restarting container produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="restarting", running=False, health=None, raw={})
        assert _classify_state(state, "c") is not None

    def test_unknown_status(self) -> None:
        """Tests that an unknown status string produces a diagnostic."""
        state = ContainerState(name="c", exists=True, status="weird", running=False, health=None, raw={})
        assert _classify_state(state, "c") is not None


# ---------------------------------------------------------------------------
# _check_rcon_available
# ---------------------------------------------------------------------------


class TestCheckRconAvailable:
    """Tests for :func:`_check_rcon_available`."""

    def test_broken_compose_no_failures(self, tmp_path: Path) -> None:
        """Tests that a broken compose short-circuits to no failures."""
        cfg = _config(tmp_path, compose_ok=False)
        runtime = _Runtime()
        assert _check_rcon_available(cfg, runtime, ["survival"], {}, None) == []

    def test_missing_member_state_skipped(self, tmp_path: Path) -> None:
        """Tests that a restart_set member without a state is skipped."""
        cfg = _config(tmp_path)
        runtime = _Runtime()
        assert _check_rcon_available(cfg, runtime, ["survival"], {}, None) == []

    def test_not_running_member_skipped(self, tmp_path: Path) -> None:
        """Tests that a not-running restart_set member is skipped."""
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg = _config(tmp_path, instances={"survival": inst})
        stopped = ContainerState(name="mc-survival", exists=True, status="exited", running=False, health=None, raw={})
        assert _check_rcon_available(cfg, _Runtime(), ["survival"], {"survival": stopped}, None) == []

    def test_missing_instance_skipped(self, tmp_path: Path) -> None:
        """Tests that a restart_set member with no instance entry is skipped."""
        cfg = _config(tmp_path)
        running = ContainerState(name="c", exists=True, status="running", running=True, health="healthy", raw={})
        assert _check_rcon_available(cfg, _Runtime(), ["survival"], {"survival": running}, None) == []

    def test_missing_service_skipped(self, tmp_path: Path) -> None:
        """Tests that an instance without a service is skipped."""
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg = _config(tmp_path, instances={"survival": inst})
        # match_service_by_container fails for this instance (no compose entry),
        # leaving inst.service = None.
        cfg.instances["survival"].service = None
        running = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="healthy", raw={})
        assert _check_rcon_available(cfg, _Runtime(), ["survival"], {"survival": running}, None) == []


# ---------------------------------------------------------------------------
# run_preflight branches
# ---------------------------------------------------------------------------


class TestRunPreflightBranches:
    """Tests for the less-traveled branches of :func:`run_preflight`."""

    def test_www_dir_candidates_warned_for_client_scope(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that www_dir candidates are logged at WARN when www_dir is None."""
        cfg = _config(
            tmp_path,
            partition=[],
            instances={},
            www_dir=tmp_path / "www",
            www_dir_candidates=[tmp_path / "cand1", tmp_path / "cand2"],
        )
        # Force the candidate-warning path by clearing www_dir after construction.
        cfg = DeploymentConfig(**{**cfg.__dict__, "www_dir": None, "www_dir_error": "no candidates matched"})
        with caplog.at_level(logging.WARNING), pytest.raises(PreflightError):
            run_preflight(cfg, ScopeSet(client=True), False, False, False, _Runtime(), _logger())
        assert any("www_dir candidate" in r.message for r in caplog.records)

    def test_pack_required_warning_suppressed_with_client_scope(self, tmp_path: Path) -> None:
        """Tests that pack_required_warning is absent when the client scope is active."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "config").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs" / "startup_scripts").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs" / "startup_scripts" / "x.js").write_text("// new", encoding="utf-8")
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        plan = run_preflight(cfg, ScopeSet(server=True, client=True), False, False, False, _runtime(cfg), _logger())
        assert plan.pack_required is True
        assert plan.pack_required_warning is None

    def test_rp_filename_validation_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests that an invalid RP filename produces a §7.5 failure."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "resourcepacks").mkdir(parents=True)
        (tmp_path / "sync" / "resourcepacks" / "pack.zip").write_bytes(b"x")
        rp = ResourcePackConfig(filename="pack.zip", required=False, prompt="")
        cfg = _config(
            tmp_path,
            partition=["survival"],
            instances={"survival": inst},
            resource_packs={"survival": rp},
        )

        def _reject(name: str) -> None:
            raise ConfigError(f"invalid filename: {name!r}")

        monkeypatch.setattr(preflight, "validate_resource_pack_filename", _reject)
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg), _logger())
        assert any("§7.5" in f.source for f in ei.value.failures)

    def test_rp_source_missing_failure(self, tmp_path: Path) -> None:
        """Tests that a missing RP source zip produces a §7.8 failure."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "resourcepacks").mkdir(parents=True)
        rp = ResourcePackConfig(filename="pack.zip", required=False, prompt="")
        cfg = _config(
            tmp_path,
            partition=["survival"],
            instances={"survival": inst},
            resource_packs={"survival": rp},
        )
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg), _logger())
        assert any("§7.8" in f.source for f in ei.value.failures)

    def test_rp_client_sub_missing_failure(self, tmp_path: Path) -> None:
        """Tests that a missing resourcepacks.client mapping produces a §7.7 failure."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        rp = ResourcePackConfig(filename="pack.zip", required=False, prompt="")
        cfg = _config(
            tmp_path,
            partition=["survival"],
            instances={"survival": inst},
            resource_packs={"survival": rp},
            sync_mapping={"resourcepacks": {"resource_pack": "@www/resourcepacks"}},
        )
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(resource_pack=True), False, False, False, _runtime(cfg), _logger())
        assert any("§7.7" in f.source for f in ei.value.failures)

    def test_restarting_settles_to_running(self, tmp_path: Path) -> None:
        """Tests that a restarting container that settles to running passes preflight."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "config").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs").mkdir(parents=True)
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        restarting = ContainerState(name="mc-survival", exists=True, status="restarting", running=False, health=None, raw={"Mounts": []})
        settled = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="healthy", raw={"Mounts": []})
        runtime = _runtime(cfg, states={"mc-survival": restarting}, restarting_settled={"mc-survival": settled})
        plan = run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, _logger())
        assert plan.none_set == ["survival"]

    def test_service_match_error_reported(self, tmp_path: Path) -> None:
        """Tests that a service_match_error produces a §3.6 failure."""
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        inst.service_match_error = "no service matches"
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        # Clear the auto-populated service so the error branch is reachable.
        cfg.instances["survival"].service = None
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg), _logger())
        assert any("§3.6" in f.source for f in ei.value.failures)

    def test_stop_grace_parse_error_reported(self, tmp_path: Path) -> None:
        """Tests that a stop_grace_parse_error produces a §3.2 failure."""
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        # Match succeeded; inject the parse error post-hoc.
        cfg.instances["survival"].stop_grace_parse_error = "garbage"
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg), _logger())
        assert any("§3.2" in f.source for f in ei.value.failures)

    def test_no_healthcheck_reported(self, tmp_path: Path) -> None:
        """Tests that a service without a healthcheck produces a §3.8 failure."""
        inst = _instance("survival", "mc-survival", tmp_path / "survival")
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        # Clear the healthcheck flag on the matched service.
        cfg.instances["survival"].service.has_healthcheck = False
        with pytest.raises(PreflightError) as ei:
            run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg), _logger())
        assert any("§3.8" in f.source for f in ei.value.failures)

    def test_mods_dir_toml_mismatch_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that a TOML/compose mods_dir mismatch produces a WARN."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "config").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs").mkdir(parents=True)
        bogus = tmp_path / "wrong_mods"
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst}, mods_dir_toml=bogus)
        with caplog.at_level(logging.WARNING):
            run_preflight(cfg, ScopeSet(server=True), False, False, False, _runtime(cfg), _logger())
        assert any("mods_dir" in r.message and "compose" in r.message for r in caplog.records)

    def test_unhealthy_container_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that an unhealthy running container produces a WARN."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "config").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs").mkdir(parents=True)
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        unhealthy = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="unhealthy", raw={"Mounts": []})
        runtime = _runtime(cfg, states={"mc-survival": unhealthy})
        with caplog.at_level(logging.WARNING):
            run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, _logger())
        assert any("unhealthy" in r.message for r in caplog.records)

    def test_starting_container_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Tests that a starting container produces an INFO log."""
        root = tmp_path / "survival"
        inst = _instance("survival", "mc-survival", root)
        (tmp_path / "sync" / "config").mkdir(parents=True)
        (tmp_path / "sync" / "kubejs").mkdir(parents=True)
        cfg = _config(tmp_path, partition=["survival"], instances={"survival": inst})
        starting = ContainerState(name="mc-survival", exists=True, status="running", running=True, health="starting", raw={"Mounts": []})
        runtime = _runtime(cfg, states={"mc-survival": starting})
        with caplog.at_level(logging.INFO):
            run_preflight(cfg, ScopeSet(server=True), False, False, False, runtime, _logger())
        assert any("starting" in r.message for r in caplog.records)
