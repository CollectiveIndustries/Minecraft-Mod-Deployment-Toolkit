# tests/modpack_upgrade/test_update_command.py

"""Update command end-to-end tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from minecraft.modpack_upgrade.cli.commands import _compose
from minecraft.modpack_upgrade.cli.commands.update import (
    _env_as_mapping,
    _extract_compose_runtime,
    run,
)
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.providers.base import Provider, ProviderVersion
from minecraft.modpack_upgrade.providers.curseforge import CurseForgeProvider
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry


def _setup_project(
    tmp_path: Path,
    *,
    mods: dict[str, str] | None = None,
    policy_no_version: str = "ask",
    selection: str = "release_then_beta",
) -> Path:
    """Create a project tree with manifest, seed, and side_overrides.

    Returns the config_dir. Creates a ``docker-compose.yml`` whose
    Minecraft service declares the ``itzg/minecraft-server`` env-var
    convention that ``--release-mode update`` needs in order to prove
    Manifest_Schema Section 2 equivalence.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    (config_dir / "upgrade.toml").write_text("[in-house]\nfilenames = []\n")
    (config_dir / "side_overrides.toml").write_text("")

    manifest = tmp_path / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "[minecraft]\n"
        'version = "1.21.1"\n'
        'loader = "forge"\n'
        'loader_version = "52.0.1"\n'
        "\n"
        "[policy]\n"
        f'selection = "{selection}"\n'
        f'no_version = "{policy_no_version}"\n'
    )

    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)
    for filename, side in (mods or {}).items():
        stem = filename.removesuffix(".jar")
        (index / f"{stem}.pw.toml").write_text(f'filename = "{filename}"\nside = "{side}"\n[update.modrinth]\nmod-id = "{stem}"\nversion = "v0"\n')
        (downloads / filename).write_bytes(b"seed")

    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  mc-survival:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        '      VERSION: "1.21.1"\n'
        '      TYPE: "FORGE"\n'
        '      FORGE_VERSION: "52.0.1"\n'
    )

    return config_dir


class _FakeModrinth(Provider):
    """Provider fixture: returns a scripted list per project_id."""

    def __init__(self, versions: dict[str, list[ProviderVersion]]) -> None:
        self.name = "modrinth"
        self._versions = versions

    def query_versions(self, project_id: str, game_version: str, loader: str) -> list[ProviderVersion]:
        return list(self._versions.get(project_id, []))

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        return b""


class _NullTransport:
    """Transport stub that raises if used."""

    def get(self, url: str, **kwargs: object) -> object:
        raise AssertionError(f"unexpected transport use: {url}")


def _registry(versions: dict[str, list[ProviderVersion]]) -> ProviderRegistry:
    """Build a registry with fake Modrinth + inert CurseForge."""
    r = ProviderRegistry()
    r.register(_FakeModrinth(versions))
    r.register(CurseForgeProvider(transport=_NullTransport(), api_key=None))
    return r


def _pv(version_id: str = "v2", *, filename: str = "mod-v2.jar", channel: str = "release") -> ProviderVersion:
    """Build a ProviderVersion fixture."""
    return ProviderVersion(
        version_id=version_id,
        filename=filename,
        download_url=f"https://example/{filename}",
        hashes={"sha1": "a" * 40},
        game_versions=("1.21.1",),
        loaders=("forge",),
        release_channel=channel,
        dependencies=(),
        client_side="required",
        server_side="required",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_no_losses_exits_0(tmp_path: Path) -> None:
    """No losses + upgrade mode -> exit 0, state=resolved."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"})
    registry = _registry({"mod": [_pv()]})
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0
    assert (tmp_path / "state" / "runs" / "run-1" / "state.toml").is_file()
    assert (tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml").is_file()


def test_happy_path_writes_report(tmp_path: Path) -> None:
    """The report is written to ``www/runs/<run-id>/``."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"})
    registry = _registry({"mod": [_pv()]})
    run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    text = (tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml").read_text()
    assert 'run_id = "run-1"' in text
    assert 'status = "complete"' in text


def test_happy_path_writes_state(tmp_path: Path) -> None:
    """The state is written to ``<state_dir>/runs/<run-id>/state.toml``."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"})
    registry = _registry({"mod": [_pv()]})
    run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    text = (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()
    assert 'status = "resolved"' in text
    assert 'release_mode = "upgrade"' in text


# ---------------------------------------------------------------------------
# Loss paths
# ---------------------------------------------------------------------------


def test_losses_ask_exits_6(tmp_path: Path) -> None:
    """Losses + ask -> exit 6, state=awaiting_acknowledgment."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"}, policy_no_version="ask")
    registry = _registry({"mod": []})
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 6
    assert 'status = "awaiting_acknowledgment"' in (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()


def test_losses_drop_exits_0(tmp_path: Path) -> None:
    """Losses + drop -> exit 0, state=resolved."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"}, policy_no_version="drop")
    registry = _registry({"mod": []})
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0
    assert 'status = "resolved"' in (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()


def test_losses_fail_exits_4(tmp_path: Path) -> None:
    """Losses + fail -> exit 4, state=failed."""
    config_dir = _setup_project(tmp_path, mods={"mod.jar": "both"}, policy_no_version="fail")
    registry = _registry({"mod": []})
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 4
    assert 'status = "failed"' in (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()


# ---------------------------------------------------------------------------
# Preflight errors
# ---------------------------------------------------------------------------


def test_missing_manifest_exits_3(tmp_path: Path) -> None:
    """A missing manifest exits 3."""
    config_dir = _setup_project(tmp_path, mods={})
    (tmp_path / "sync" / "modpack.manifest.toml").unlink()
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="upgrade",
            registry=_registry({}),
            run_id="run-1",
            now="2026-01-01T00:00:00Z",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3


def test_missing_side_overrides_exits_3(tmp_path: Path) -> None:
    """A missing side_overrides.toml exits 3."""
    config_dir = _setup_project(tmp_path, mods={})
    (config_dir / "side_overrides.toml").unlink()
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="upgrade",
            registry=_registry({}),
            run_id="run-1",
            now="2026-01-01T00:00:00Z",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3


def test_invalid_release_mode_exits_2(tmp_path: Path) -> None:
    """Invalid release_mode exits 2 (usage)."""
    config_dir = _setup_project(tmp_path, mods={})
    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=tmp_path, release_mode="bogus", registry=_registry({}))
    assert ei.value.exit_code == 2


# ---------------------------------------------------------------------------
# Update mode
# ---------------------------------------------------------------------------


def test_update_mode_stores_compose_sha(tmp_path: Path) -> None:
    """release_mode=update writes effective_compose_sha256 into state."""
    config_dir = _setup_project(tmp_path, mods={})
    registry = _registry({})
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="update",
        registry=registry,
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0
    text = (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()
    assert "effective_compose_sha256" in text


def test_update_mode_missing_compose_exits_3(tmp_path: Path) -> None:
    """release_mode=update fails if compose is missing."""
    config_dir = _setup_project(tmp_path, mods={})
    (tmp_path / "docker-compose.yml").unlink()
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="update",
            registry=_registry({}),
            run_id="run-1",
            now="2026-01-01T00:00:00Z",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3


# ---------------------------------------------------------------------------
# BUG-01 regression: manifest vs. compose equivalence for --release-mode update
# ---------------------------------------------------------------------------


def test_update_mode_manifest_compose_mismatch_exits_3(tmp_path: Path) -> None:
    """Manifest [minecraft] must match Compose for --release-mode update (Manifest_Schema Section 2)."""
    config_dir = _setup_project(tmp_path, mods={})
    # Compose declares a different MC version + loader version than the
    # manifest (1.21.1/forge/52.0.1). Mismatch must be refused.
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  mc-survival:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        '      VERSION: "1.20.1"\n'
        '      TYPE: "FORGE"\n'
        '      FORGE_VERSION: "47.4.10"\n'
    )
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="update",
            registry=_registry({}),
            run_id="run-1",
            now="2026-01-01T00:00:00Z",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "mismatch" in str(ei.value).lower()
    # No report or state should have been written (write-order guarantee).
    assert not (tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml").exists()
    assert not (tmp_path / "state" / "runs" / "run-1" / "state.toml").exists()


def test_update_mode_compose_without_env_exits_3(tmp_path: Path) -> None:
    """Update mode refuses when the Compose service declares no version env vars.

    Manifest_Schema Section 8 rule 8 requires an affirmative match; "cannot
    verify" is exit 3, not silent pass.
    """
    config_dir = _setup_project(tmp_path, mods={})
    (tmp_path / "docker-compose.yml").write_text("services:\n  mc-survival:\n    image: itzg/minecraft-server\n")
    with pytest.raises(UpgradeCliError) as ei:
        run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="update",
            registry=_registry({}),
            run_id="run-1",
            now="2026-01-01T00:00:00Z",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 3
    assert "does not declare" in str(ei.value)


def test_upgrade_mode_does_not_check_compose_equivalence(tmp_path: Path) -> None:
    """Upgrade mode is manifest-authoritative; no manifest-vs-compose check."""
    config_dir = _setup_project(tmp_path, mods={})
    # Compose still has the matching env vars, but the manifest/Compose
    # equality rule only applies to update mode. Delete env vars to prove
    # upgrade doesn't check them.
    (tmp_path / "docker-compose.yml").write_text("services:\n  mc-survival:\n    image: itzg/minecraft-server\n")
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="upgrade",
        registry=_registry({}),
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0


# ---------------------------------------------------------------------------
# Slice 8 pair 6: update delegates compose resolution to the shared _compose module
# ---------------------------------------------------------------------------
#
# ``_compose`` is exhaustively tested in ``test_compose.py``. These tests
# pin only the wiring: a future refactor that re-introduces a local copy of
# ``resolve_compose_path`` or ``compute_compose_sha256`` on ``update.py``
# would fail these, even if the local copy happened to behave identically
# today. They also make the pair-7 removal of the deprecated wrappers on
# ``acknowledge.py`` safe: if ``update.py`` still depended on them, these
# spies would stop firing when the wrappers are deleted.


def test_update_mode_delegates_compose_resolution_to_shared_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Update calls _compose.resolve_compose_path (twice, by design).

    Two call sites are expected:

      1. ``_load_inputs`` resolves the path directly, to check it exists
         and to hand it to ``_verify_manifest_matches_compose``.
      2. ``_compose.compute_compose_sha256`` re-resolves the path itself
         on every call - it does not trust callers to pass the right
         path, keeping the ``(config_dir, project_root)`` API stable
         across all three commands.

    Both calls must go through ``_compose.resolve_compose_path``. A
    regression that inlines the resolution in ``update.py``, or that
    stops ``compute_compose_sha256`` from re-resolving internally,
    changes this count and fails the assertion.
    """
    config_dir = _setup_project(tmp_path, mods={})
    calls: list[tuple[Path, Path]] = []
    real = _compose.resolve_compose_path

    def _spy(cfg: Path, root: Path) -> Path:
        calls.append((cfg, root))
        return real(cfg, root)

    monkeypatch.setattr(_compose, "resolve_compose_path", _spy)
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="update",
        registry=_registry({}),
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0
    assert calls == [(config_dir, tmp_path), (config_dir, tmp_path)]


def test_update_mode_delegates_compose_sha_to_shared_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Update calls _compose.compute_compose_sha256, not a local copy."""
    config_dir = _setup_project(tmp_path, mods={})
    calls: list[tuple[Path, Path]] = []
    fake_sha = "e" * 64

    def _spy(cfg: Path, root: Path) -> str:
        calls.append((cfg, root))
        return fake_sha

    monkeypatch.setattr(_compose, "compute_compose_sha256", _spy)
    code = run(
        config_dir=config_dir,
        project_root=tmp_path,
        release_mode="update",
        registry=_registry({}),
        run_id="run-1",
        now="2026-01-01T00:00:00Z",
        state_dir=tmp_path / "state",
    )
    assert code == 0
    assert calls == [(config_dir, tmp_path)]
    # The spy's return value must be what the state recorded; if update.py
    # bypassed the shared module, the fake sha would never appear in the
    # state file.
    state_text = (tmp_path / "state" / "runs" / "run-1" / "state.toml").read_text()
    assert fake_sha in state_text


# ---------------------------------------------------------------------------
# Slice 8 pair 9a: _env_as_mapping branch coverage
# ---------------------------------------------------------------------------
#
# Every branch of the compose-environment normaliser is exercised directly.
# The end-to-end paths through ``_extract_compose_runtime`` and ``run()``
# already cover the dict-with-string-values happy path; these cases pin the
# remaining coercion and rejection branches so a future refactor cannot
# silently change e.g. ``None`` -> absent vs. ``""`` or start reading the
# host environment for bare list entries.


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Mapping form, ordinary case.
        ({"A": "1", "B": "2"}, {"A": "1", "B": "2"}),
        # Mapping form, empty.
        ({}, {}),
        # Mapping form, None value coerces to empty string.
        ({"A": None}, {"A": ""}),
        # Mapping form, int and bool values coerce through str().
        ({"A": 1}, {"A": "1"}),
        ({"A": True}, {"A": "True"}),
        # Mapping form, non-str key coerces through str().
        ({1: "A"}, {"1": "A"}),
        # List form, empty.
        ([], {}),
        # List form, well-formed "KEY=VALUE".
        (["A=1", "B=2"], {"A": "1", "B": "2"}),
        # List form, bare "KEY" (no '=') is dropped (host-env passthrough).
        (["A=1", "B"], {"A": "1"}),
        # List form, non-str items are dropped.
        (["A", 42, "B=2"], {"B": "2"}),
        # Non-dict, non-list inputs return empty.
        (None, {}),
        ("oops", {}),
        (42, {}),
    ],
)
def test_env_as_mapping(raw: Any, expected: dict[str, str]) -> None:
    """_env_as_mapping normalizes every supported compose env shape."""
    assert _env_as_mapping(raw) == expected


# ---------------------------------------------------------------------------
# Slice 8 pair 9a: _extract_compose_runtime branch coverage
# ---------------------------------------------------------------------------
#
# Every branch of the compose-runtime extractor is exercised directly.
# The end-to-end paths through ``run()`` already cover the "mapping form,
# matching image, all three values present" happy path; these cases pin
# the failure paths and alternate env shapes.


def _write_compose(tmp_path: Path, body: str) -> Path:
    """Write ``body`` as ``docker-compose.yml`` and return its path."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_extract_compose_runtime_missing_file(tmp_path: Path) -> None:
    """A missing compose file (OSError) yields None."""
    assert _extract_compose_runtime(tmp_path / "absent.yml") is None


def test_extract_compose_runtime_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML (yaml.YAMLError) yields None."""
    path = _write_compose(tmp_path, "key: [unclosed\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_scalar_top_level(tmp_path: Path) -> None:
    """A top-level scalar (not a mapping) yields None."""
    path = _write_compose(tmp_path, "just_a_string\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_list_top_level(tmp_path: Path) -> None:
    """A top-level list (not a mapping) yields None."""
    path = _write_compose(tmp_path, "- a\n- b\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_services_not_table(tmp_path: Path) -> None:
    """A non-mapping ``services`` value yields None."""
    path = _write_compose(tmp_path, "services: not_a_table\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_non_dict_service_skipped(tmp_path: Path) -> None:
    """A non-mapping service entry is skipped."""
    path = _write_compose(tmp_path, "services:\n  mc:\n    - a\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_non_minecraft_image_skipped(tmp_path: Path) -> None:
    """A service whose image does not contain 'minecraft' is skipped."""
    path = _write_compose(tmp_path, "services:\n  web:\n    image: nginx:latest\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_minecraft_image_missing_env(tmp_path: Path) -> None:
    """A minecraft service with no ``environment`` block yields None."""
    path = _write_compose(tmp_path, "services:\n  mc:\n    image: itzg/minecraft-server\n")
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_minecraft_image_partial_env(tmp_path: Path) -> None:
    """A minecraft service missing any of the three values yields None."""
    body = "services:\n  mc:\n    image: itzg/minecraft-server\n    environment:\n      VERSION: '1.21.1'\n      TYPE: FORGE\n"
    path = _write_compose(tmp_path, body)
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_unknown_type_returns_none(tmp_path: Path) -> None:
    """An unrecognised ``TYPE`` value yields None."""
    body = "services:\n  mc:\n    image: itzg/minecraft-server\n    environment:\n      VERSION: '1.21.1'\n      TYPE: VANILLA\n      FORGE_VERSION: '52.0.1'\n"
    path = _write_compose(tmp_path, body)
    assert _extract_compose_runtime(path) is None


def test_extract_compose_runtime_list_form_env(tmp_path: Path) -> None:
    """The list form of ``environment`` parses identically to the mapping form."""
    body = "services:\n  mc:\n    image: itzg/minecraft-server\n    environment:\n      - VERSION=1.21.1\n      - TYPE=FORGE\n      - FORGE_VERSION=52.0.1\n"
    path = _write_compose(tmp_path, body)
    assert _extract_compose_runtime(path) == ("1.21.1", "forge", "52.0.1")


def test_extract_compose_runtime_first_matching_service_wins(tmp_path: Path) -> None:
    """A later fully-declaring service wins over an earlier partial one."""
    body = (
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "  mc-broken:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        "      VERSION: '1.20'\n"
        "  mc-good:\n"
        "    image: itzg/minecraft-server\n"
        "    environment:\n"
        "      VERSION: '1.21.1'\n"
        "      TYPE: FORGE\n"
        "      FORGE_VERSION: '52.0.1'\n"
    )
    path = _write_compose(tmp_path, body)
    assert _extract_compose_runtime(path) == ("1.21.1", "forge", "52.0.1")
