# tests/modpack_upgrade/test_update_command.py

"""Update command end-to-end tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands.update import run
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
