# tests/modpack_upgrade/test_resolver.py

"""Version resolver tests (Section 15.1.1, Section 5.3)."""

from __future__ import annotations

import pytest

from minecraft.modpack_upgrade.manifest import Manifest
from minecraft.modpack_upgrade.overrides import SideOverrides
from minecraft.modpack_upgrade.providers.base import (
    Provider,
    ProviderDependency,
    ProviderVersion,
)
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry
from minecraft.modpack_upgrade.report import ResolutionReport, SeedEntry
from minecraft.modpack_upgrade.resolution_inputs import SeedSnapshotEntry
from minecraft.modpack_upgrade.resolver import (
    _SELECTION_CHANNELS,
    CycleError,
    Resolver,
    ResolverInputs,
    _derive_report_status,
    _modrinth_side,
    _select_candidate,
)
from minecraft.modpack_upgrade.seed import LoadedSeed


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _manifest(**overrides: object) -> Manifest:
    """Build a Manifest fixture."""
    base: dict[str, object] = {
        "minecraft_version": "1.21.1",
        "loader": "forge",
        "loader_version": "52.0.1",
        "selection": "release_then_beta",
        "no_version": "ask",
        "raw_sha256": _hex("0"),
    }
    base.update(overrides)
    return Manifest(**base)


def _pv(
    version_id: str,
    *,
    filename: str = "mod.jar",
    channel: str = "release",
    client_side: str | None = None,
    server_side: str | None = None,
    deps: tuple[ProviderDependency, ...] = (),
    hashes: dict[str, str] | None = None,
) -> ProviderVersion:
    """Build a ProviderVersion fixture."""
    return ProviderVersion(
        version_id=version_id,
        filename=filename,
        download_url=f"https://example/{filename}",
        hashes=hashes or {"sha1": "a" * 40},
        game_versions=("1.21.1",),
        loaders=("forge",),
        release_channel=channel,
        dependencies=deps,
        client_side=client_side,
        server_side=server_side,
    )


def _seed_entry(
    *,
    filename: str = "mod.jar",
    provider: str = "modrinth",
    project_id: str | None = "mod",
    current_version_id: str | None = "v1",
    side_declared: str = "both",
    jar_sha256: str | None = None,
) -> SeedEntry:
    """Build a SeedEntry fixture."""
    return SeedEntry(
        filename=filename,
        provider=provider,
        jar_sha256=jar_sha256 or _hex("a"),
        side_declared=side_declared,
        side_effective=side_declared,
        project_id=project_id,
        current_version_id=current_version_id,
    )


def _snapshot_entry(seed: SeedEntry) -> SeedSnapshotEntry:
    """Build a SeedSnapshotEntry from a SeedEntry."""
    return SeedSnapshotEntry(
        provider=seed.provider,
        current_filename=seed.filename,
        jar_sha256=seed.jar_sha256,
        side_declared=seed.side_declared if seed.side_declared != "unknown" else "",
        dependencies=(),
        current_version_id=seed.current_version_id,
    )


def _loaded_seed(*entries: SeedEntry) -> LoadedSeed:
    """Build a LoadedSeed from SeedEntry objects."""
    return LoadedSeed(
        report_entries=entries,
        snapshot_entries=tuple(_snapshot_entry(e) for e in entries),
    )


class _FakeProvider(Provider):
    """Provider fixture: returns a scripted list per project_id."""

    def __init__(self, name: str, versions: dict[str, list[ProviderVersion]]) -> None:
        self.name = name
        self._versions = versions
        self.calls: list[str] = []

    def query_versions(self, project_id: str, game_version: str, loader: str) -> list[ProviderVersion]:
        self.calls.append(project_id)
        return list(self._versions.get(project_id, []))

    def fetch_artifact(self, version: ProviderVersion) -> bytes:
        return b""


def _make_registry(
    modrinth_versions: dict[str, list[ProviderVersion]] | None = None,
    curseforge_versions: dict[str, list[ProviderVersion]] | None = None,
) -> ProviderRegistry:
    """Build a registry with fake providers."""
    registry = ProviderRegistry()
    registry.register(_FakeProvider("modrinth", modrinth_versions or {}))
    registry.register(_FakeProvider("curseforge", curseforge_versions or {}))
    return registry


def _inputs(
    *,
    manifest: Manifest | None = None,
    seed: LoadedSeed | None = None,
    overrides: SideOverrides | None = None,
    registry: ProviderRegistry | None = None,
) -> ResolverInputs:
    """Build a ResolverInputs with sensible defaults."""
    return ResolverInputs(
        manifest=manifest or _manifest(),
        release_mode="upgrade",
        in_house_filenames=(),
        seed=seed or _loaded_seed(),
        side_overrides=overrides or SideOverrides(),
        registry=registry or _make_registry(),
        manifest_sha256=_hex("1"),
        resolution_inputs_sha256=_hex("2"),
        side_overrides_sha256=_hex("3"),
    )


def _resolve(inputs: ResolverInputs) -> ResolutionReport:
    """Run the resolver with a deterministic run_id."""
    return Resolver(inputs).resolve(run_id="run-abc", generated_at_utc="2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def test_selection_channels_match_spec() -> None:
    """Section 3.1 policy -> channel sequence."""
    assert _SELECTION_CHANNELS["release_then_beta"] == ("release", "beta")
    assert _SELECTION_CHANNELS["release_only"] == ("release",)
    assert _SELECTION_CHANNELS["release_then_beta_then_alpha"] == ("release", "beta", "alpha")


def test_select_candidate_prefers_release_over_beta() -> None:
    """release_then_beta picks release when both are available."""
    versions = [_pv("v-beta", channel="beta"), _pv("v-release", channel="release")]
    selected = _select_candidate(versions, "release_then_beta")
    assert selected is not None
    assert selected.version_id == "v-release"


def test_select_candidate_release_only_returns_none_when_only_beta() -> None:
    """release_only returns None when only beta versions exist."""
    versions = [_pv("v-beta", channel="beta")]
    assert _select_candidate(versions, "release_only") is None


def test_select_candidate_with_alpha_falls_through() -> None:
    """release_then_beta_then_alpha accepts alpha as last resort."""
    versions = [_pv("v-alpha", channel="alpha")]
    selected = _select_candidate(versions, "release_then_beta_then_alpha")
    assert selected is not None
    assert selected.version_id == "v-alpha"


def test_select_candidate_unknown_policy_raises() -> None:
    """An unknown policy raises ValueError."""
    with pytest.raises(ValueError):
        _select_candidate([], "bogus")


def test_modrinth_side_both_required() -> None:
    """client_side=required + server_side=required -> both."""
    assert _modrinth_side("required", "required") == "both"


def test_modrinth_side_client_only() -> None:
    """client_side=required + server_side=unsupported -> client."""
    assert _modrinth_side("required", "unsupported") == "client"


def test_modrinth_side_server_only() -> None:
    """server_side=required + client_side=unsupported -> server."""
    assert _modrinth_side("unsupported", "required") == "server"


def test_modrinth_side_both_optional() -> None:
    """Both optional -> both."""
    assert _modrinth_side("optional", "optional") == "both"


def test_modrinth_side_both_unsupported() -> None:
    """Both unsupported -> unknown."""
    assert _modrinth_side("unsupported", "unsupported") == "unknown"


def test_modrinth_side_missing_hints() -> None:
    """Missing hints -> unknown."""
    assert _modrinth_side(None, None) == "unknown"


def test_derive_report_status_no_losses() -> None:
    """No losses -> complete."""
    assert _derive_report_status(False, "ask") == "complete"


def test_derive_report_status_losses_drop() -> None:
    """Losses + drop -> complete."""
    assert _derive_report_status(True, "drop") == "complete"


def test_derive_report_status_losses_ask() -> None:
    """Losses + ask -> paused_awaiting_acknowledgment."""
    assert _derive_report_status(True, "ask") == "paused_awaiting_acknowledgment"


def test_derive_report_status_losses_fail() -> None:
    """Losses + fail -> failed."""
    assert _derive_report_status(True, "fail") == "failed"


def test_derive_report_status_unknown_policy_raises() -> None:
    """Unknown policy raises."""
    with pytest.raises(ValueError):
        _derive_report_status(True, "bogus")


# ---------------------------------------------------------------------------
# Resolution - happy paths
# ---------------------------------------------------------------------------


def test_empty_seed_produces_empty_report() -> None:
    """An empty seed yields a report with no entries."""
    report = _resolve(_inputs(seed=_loaded_seed()))
    assert report.seed == ()
    assert report.resolved == ()
    assert report.lost == ()
    assert report.cascade == ()
    assert report.status == "complete"


def test_single_external_seed_resolves() -> None:
    """A single Modrinth seed resolves to the newest matching version."""
    registry = _make_registry({"mod": [_pv("v2", filename="mod-v2.jar")]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.resolved) == 1
    assert report.resolved[0].version_id == "v2"
    assert report.resolved[0].filename == "mod-v2.jar"
    assert report.resolved[0].provider == "modrinth"


def test_single_in_house_seed_resolves_from_seed() -> None:
    """An in-house mod is added to resolved without a provider query."""
    registry = _make_registry()
    report = _resolve(
        _inputs(
            seed=_loaded_seed(_seed_entry(provider="in-house", project_id=None, current_version_id=None, filename="custom.jar")),
            registry=registry,
        )
    )
    assert len(report.resolved) == 1
    assert report.resolved[0].provider == "in-house"
    assert report.resolved[0].is_in_house is True
    assert report.resolved[0].filename == "custom.jar"


def test_unknown_provider_seed_fails() -> None:
    """A seed entry with no [update.*] block goes to lost."""
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry(provider="unknown", project_id=None, current_version_id=None))))
    assert len(report.lost) == 1
    assert "[update.*]" in report.lost[0].detail


def test_no_compatible_version_goes_to_lost() -> None:
    """A provider returning no matching versions yields a lost entry."""
    registry = _make_registry({"mod": []})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.lost) == 1
    assert report.lost[0].reason == "no_compatible_version"


# ---------------------------------------------------------------------------
# Selection policy in context
# ---------------------------------------------------------------------------


def test_selection_policy_release_only_fails_when_only_beta() -> None:
    """release_only policy fails when only beta versions are available."""
    registry = _make_registry({"mod": [_pv("v-beta", channel="beta")]})
    report = _resolve(_inputs(manifest=_manifest(selection="release_only"), seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.lost) == 1


def test_selection_policy_with_alpha_accepts_alpha() -> None:
    """release_then_beta_then_alpha accepts alpha as a last resort."""
    registry = _make_registry({"mod": [_pv("v-alpha", channel="alpha")]})
    report = _resolve(
        _inputs(
            manifest=_manifest(selection="release_then_beta_then_alpha"),
            seed=_loaded_seed(_seed_entry()),
            registry=registry,
        )
    )
    assert len(report.resolved) == 1
    assert report.resolved[0].version_id == "v-alpha"


# ---------------------------------------------------------------------------
# Dependency closure
# ---------------------------------------------------------------------------


def test_required_dep_is_pulled_in() -> None:
    """A required dependency is resolved and included."""
    dep = ProviderDependency(project_id="lib", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry(
        {
            "mod": [_pv("v1", deps=(dep,))],
            "lib": [_pv("lib-v1", filename="lib.jar")],
        }
    )
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.resolved) == 2
    names = {e.filename for e in report.resolved}
    assert "lib.jar" in names


def test_optional_dep_is_not_pulled_in() -> None:
    """An optional dependency is not resolved unless a seed entry needs it."""
    dep = ProviderDependency(project_id="lib", mod_id=None, version_id=None, dependency_type="optional")
    registry = _make_registry({"mod": [_pv("v1", deps=(dep,))]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.resolved) == 1


def test_dep_only_mod_is_marked_dependency_only() -> None:
    """A dependency-only resolved entry has ``is_dependency_only=True``."""
    dep = ProviderDependency(project_id="lib", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry(
        {
            "mod": [_pv("v1", deps=(dep,))],
            "lib": [_pv("lib-v1", filename="lib.jar")],
        }
    )
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    lib = next(e for e in report.resolved if e.filename == "lib.jar")
    assert lib.is_dependency_only is True


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------


def test_cascade_drops_dependent_of_failed_dep() -> None:
    """A dependent whose required dep is lost is cascade-dropped."""
    dep = ProviderDependency(project_id="lib", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry(
        {
            "mod": [_pv("v1", deps=(dep,))],
            "lib": [],
        }
    )
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert len(report.lost) == 1
    assert len(report.cascade) == 1
    assert report.cascade[0].project_id == "mod"
    assert report.cascade[0].dependency_chain == ("lib", "mod")


def test_cascade_chain_walks_transitively() -> None:
    """A -> B -> C where C fails cascades both A and B."""
    b_to_c = ProviderDependency(project_id="c", mod_id=None, version_id=None, dependency_type="required")
    a_to_b = ProviderDependency(project_id="b", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry(
        {
            "a": [_pv("a-v1", filename="a.jar", deps=(a_to_b,))],
            "b": [_pv("b-v1", filename="b.jar", deps=(b_to_c,))],
            "c": [],
        }
    )
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry(project_id="a", filename="a.jar")), registry=registry))
    assert len(report.cascade) == 2
    cascade_ids = {e.project_id for e in report.cascade}
    assert cascade_ids == {"a", "b"}
    a_cascade = next(e for e in report.cascade if e.project_id == "a")
    assert a_cascade.dependency_chain[0] == "c"
    assert a_cascade.dependency_chain[-1] == "a"


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_self_cycle_raises() -> None:
    """A -> A raises CycleError."""
    self_dep = ProviderDependency(project_id="a", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry({"a": [_pv("a-v1", filename="a.jar", deps=(self_dep,))]})
    inputs = _inputs(seed=_loaded_seed(_seed_entry(project_id="a", filename="a.jar")), registry=registry)
    with pytest.raises(CycleError):
        _resolve(inputs)


def test_two_node_cycle_raises() -> None:
    """A -> B -> A raises CycleError."""
    b_dep = ProviderDependency(project_id="b", mod_id=None, version_id=None, dependency_type="required")
    a_dep = ProviderDependency(project_id="a", mod_id=None, version_id=None, dependency_type="required")
    registry = _make_registry(
        {
            "a": [_pv("a-v1", filename="a.jar", deps=(b_dep,))],
            "b": [_pv("b-v1", filename="b.jar", deps=(a_dep,))],
        }
    )
    inputs = _inputs(seed=_loaded_seed(_seed_entry(project_id="a", filename="a.jar")), registry=registry)
    with pytest.raises(CycleError):
        _resolve(inputs)


# ---------------------------------------------------------------------------
# Provisional sides
# ---------------------------------------------------------------------------


def test_provisional_side_from_override() -> None:
    """An override wins over provider metadata."""
    registry = _make_registry({"mod": [_pv("v1", client_side="required", server_side="required")]})
    ovr = SideOverrides(by_filename={"mod.jar": "client"})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry, overrides=ovr))
    assert report.resolved[0].side_provisional == "client"
    assert report.resolved[0].side_source_provisional == "override"


def test_provisional_side_from_provider_when_no_override() -> None:
    """No override -> provider metadata side is used."""
    registry = _make_registry({"mod": [_pv("v1", client_side="required", server_side="unsupported")]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert report.resolved[0].side_provisional == "client"
    assert report.resolved[0].side_source_provisional == "provider"


def test_provisional_side_from_pw_toml_when_no_provider() -> None:
    """No override, no provider side -> .pw.toml side used."""
    registry = _make_registry({"mod": [_pv("v1", client_side=None, server_side=None)]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry(side_declared="server")), registry=registry))
    assert report.resolved[0].side_provisional == "server"
    assert report.resolved[0].side_source_provisional == "pw_toml"


def test_provisional_side_unknown_when_nothing_resolves() -> None:
    """No override, no provider side, .pw.toml says unknown -> unknown."""
    registry = _make_registry({"mod": [_pv("v1", client_side=None, server_side=None)]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry(side_declared="unknown")), registry=registry))
    assert report.resolved[0].side_provisional == "unknown"
    assert report.resolved[0].side_source_provisional == "unknown"


def test_skipped_override_excluded_from_resolved() -> None:
    """An override of 'skipped' excludes the entry from ``resolved``.

    The seed entry remains for audit with ``side_effective = "skipped"``.
    """
    registry = _make_registry({"mod": [_pv("v1", filename="mod.jar")]})
    ovr = SideOverrides(by_filename={"mod.jar": "skipped"})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry, overrides=ovr))
    assert report.resolved == ()
    assert len(report.seed) == 1
    assert report.seed[0].side_effective == "skipped"


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_carries_target_runtime_and_policy() -> None:
    """The report's target_runtime and policy come from the manifest."""
    report = _resolve(_inputs())
    assert report.target_runtime.minecraft_version == "1.21.1"
    assert report.target_runtime.loader == "forge"
    assert report.policy.selection == "release_then_beta"


def test_report_carries_input_hashes() -> None:
    """The report's inputs carry the three hashes."""
    report = _resolve(_inputs())
    assert report.inputs.manifest_sha256 == _hex("1")
    assert report.inputs.resolution_inputs_sha256 == _hex("2")
    assert report.inputs.side_overrides_sha256 == _hex("3")


def test_report_provider_request_count() -> None:
    """Every provider query increments ``provider_requests_made``."""
    registry = _make_registry({"mod": [_pv("v1", filename="mod.jar")]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    assert report.diagnostics.provider_requests_made >= 1


def test_report_expected_hashes_include_known_algorithms() -> None:
    """``expected_hashes`` mirrors the provider's declared hashes."""
    registry = _make_registry({"mod": [_pv("v1", hashes={"sha1": "a" * 40, "sha512": "b" * 128})]})
    report = _resolve(_inputs(seed=_loaded_seed(_seed_entry()), registry=registry))
    hashes = {h.algorithm: h.value for h in report.resolved[0].expected_hashes}
    assert hashes == {"sha1": "a" * 40, "sha512": "b" * 128}
