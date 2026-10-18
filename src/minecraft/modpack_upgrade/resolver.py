# src/minecraft/modpack_upgrade/resolver.py

"""Version resolution (Section 15.1.1, Section 5.3, Section A5.1).

Walks the seed + transitive required dependencies, selects a version
per ``[policy].selection``, computes provisional sides, cascade-drops
dependents of unresolvable deps, and builds a :class:`ResolutionReport`.

Skipped mods (override value ``"skipped"``) are excluded from
``resolved`` - they're neither deployed nor audited as losses. The
seed entry remains in ``[[seed]]`` with ``side_effective = "skipped"``
so operators can see what was intentionally omitted.
"""

from __future__ import annotations

from dataclasses import dataclass

from LoggingCore import get_logger

from .errors import UpgradeError
from .manifest import Manifest
from .overrides import SideOverrides
from .providers.base import ProviderDependency, ProviderVersion
from .providers.registry import ProviderRegistry
from .report import (
    CascadeEntry,
    Diagnostics,
    ExpectedHash,
    LostEntry,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    ResolvedEntry,
    SeedEntry,
    TargetRuntime,
)
from .seed import LoadedSeed

__all__ = ["CycleError", "Resolver", "ResolverInputs"]

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Selection policy (Section 3.1)
# ---------------------------------------------------------------------------


_SELECTION_CHANNELS: dict[str, tuple[str, ...]] = {
    "release_then_beta": ("release", "beta"),
    "release_only": ("release",),
    "release_then_beta_then_alpha": ("release", "beta", "alpha"),
}


def _select_candidate(versions: list[ProviderVersion], selection_policy: str) -> ProviderVersion | None:
    """Return the highest-priority version per ``selection_policy``, or None."""
    allowed = _SELECTION_CHANNELS.get(selection_policy)
    if allowed is None:
        raise ValueError(f"unknown selection policy: {selection_policy!r}")
    for channel in allowed:
        for version in versions:
            if version.release_channel == channel:
                return version
    return None


# ---------------------------------------------------------------------------
# Side mapping (Side_Determination Section 4.1)
# ---------------------------------------------------------------------------


def _modrinth_side(client_side: str | None, server_side: str | None) -> str:
    """Map Modrinth's client_side / server_side fields to a slip side."""
    if client_side is None or server_side is None:
        return "unknown"
    if client_side == "unsupported" and server_side == "unsupported":
        return "unknown"
    if client_side == "unsupported":
        return "server"
    if server_side == "unsupported":
        return "client"
    return "both"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CycleError(UpgradeError):
    """A dependency cycle was found during resolution (Section 16.2 exit 3)."""

    exit_code = 3

    def __init__(self, chain: list[str]) -> None:
        self.chain = list(chain)
        super().__init__(f"dependency cycle: {' -> '.join(self.chain)}")


# ---------------------------------------------------------------------------
# Inputs and internal state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverInputs:
    """Everything the resolver needs, provided by the ``update`` command."""

    manifest: Manifest
    release_mode: str
    in_house_filenames: tuple[str, ...]
    seed: LoadedSeed
    side_overrides: SideOverrides
    registry: ProviderRegistry
    manifest_sha256: str
    resolution_inputs_sha256: str
    side_overrides_sha256: str


@dataclass
class _ResolvedCandidate:
    """Internal state for one successfully resolved project."""

    project_id: str | None
    provider: str
    version: ProviderVersion | None
    seed_entry: SeedEntry | None
    is_in_house: bool
    filename: str
    jar_sha256: str | None
    provider_side: str | None
    required_deps: tuple[ProviderDependency, ...]
    is_dependency_only: bool


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class Resolver:
    """Stateful resolver. One instance per ``update`` invocation."""

    def __init__(self, inputs: ResolverInputs) -> None:
        """Store inputs; no I/O until :meth:`resolve`."""
        self._inputs = inputs
        self._resolved: dict[str, _ResolvedCandidate] = {}
        self._failed: dict[str, LostEntry] = {}
        self._cascade: dict[str, CascadeEntry] = {}
        self._request_count: int = 0

    # -- public API ---------------------------------------------------

    def resolve(self, *, run_id: str, generated_at_utc: str) -> ResolutionReport:
        """Run the full resolution and return a :class:`ResolutionReport`."""
        work = self._seed_phase()
        self._closure_phase(work)
        cycle = self._detect_cycle()
        if cycle is not None:
            raise CycleError(cycle)
        self._cascade_phase()
        return self._build_report(run_id=run_id, generated_at_utc=generated_at_utc)

    # -- phase 1: seed ------------------------------------------------

    def _seed_phase(self) -> list[tuple[str, str, SeedEntry]]:
        """Add seed entries to resolved/failed; return external work queue."""
        work: list[tuple[str, str, SeedEntry]] = []
        for entry in self._inputs.seed.report_entries:
            key = entry.project_id or entry.filename
            if entry.provider == "in-house":
                self._add_in_house(entry, key)
            elif entry.provider == "unknown":
                self._add_unknown_provider(entry, key)
            else:
                work.append((key, entry.provider, entry))
        return work

    def _add_in_house(self, entry: SeedEntry, key: str) -> None:
        """Register an in-house seed entry as resolved."""
        self._resolved[key] = _ResolvedCandidate(
            project_id=None,
            provider="in-house",
            version=None,
            seed_entry=entry,
            is_in_house=True,
            filename=entry.filename,
            jar_sha256=entry.jar_sha256,
            provider_side=None,
            required_deps=(),
            is_dependency_only=False,
        )

    def _add_unknown_provider(self, entry: SeedEntry, key: str) -> None:
        """Register a seed entry with no provider identity as failed."""
        self._failed[key] = LostEntry(
            filename=entry.filename,
            provider=entry.provider,
            project_id=entry.project_id or entry.filename,
            reason="no_compatible_version",
            detail="seed .pw.toml has no [update.*] block; cannot query provider",
        )

    # -- phase 2: closure ---------------------------------------------

    def _closure_phase(self, work: list[tuple[str, str, SeedEntry]]) -> None:
        """Resolve the work queue and walk required dependencies transitively."""
        while work:
            key, provider, seed_entry = work.pop(0)
            if key in self._resolved or key in self._failed or key in self._cascade:
                continue
            candidates = self._query_provider(key, provider)
            selected = _select_candidate(candidates, self._inputs.manifest.selection)
            if selected is None:
                self._record_no_compatible(key, provider, seed_entry)
                continue
            self._record_resolved(key, provider, selected, seed_entry)
            for dep in selected.dependencies:
                dep_key, dep_provider = self._dep_key_and_provider(dep)
                if dep_key is None or dep_provider is None:
                    continue
                work.append((dep_key, dep_provider, None))

    def _dep_key_and_provider(self, dep: ProviderDependency) -> tuple[str | None, str | None]:
        """Map a dependency edge to (key, provider), or (None, None)."""
        if dep.dependency_type != "required":
            return (None, None)
        if dep.project_id is not None:
            return (dep.project_id, "modrinth")
        if dep.mod_id is not None:
            return (dep.mod_id, "curseforge")
        return (None, None)

    def _query_provider(self, key: str, provider: str) -> list[ProviderVersion]:
        """Query ``provider`` for versions of ``key``."""
        self._request_count += 1
        p = self._inputs.registry.get(provider)
        return p.query_versions(
            key,
            self._inputs.manifest.minecraft_version,
            self._inputs.manifest.loader,
        )

    def _record_no_compatible(self, key: str, provider: str, seed_entry: SeedEntry | None) -> None:
        """Record a ``no_compatible_version`` loss."""
        filename = seed_entry.filename if seed_entry else f"{key}.jar"
        self._failed[key] = LostEntry(
            filename=filename,
            provider=provider,
            project_id=key,
            reason="no_compatible_version",
            detail=(
                f"no {provider} version matches "
                f"{self._inputs.manifest.minecraft_version}+{self._inputs.manifest.loader} "
                f"under policy {self._inputs.manifest.selection!r}"
            ),
        )

    def _record_resolved(
        self,
        key: str,
        provider: str,
        selected: ProviderVersion,
        seed_entry: SeedEntry | None,
    ) -> None:
        """Register a successful resolution and prepare its dep list."""
        provider_side = _modrinth_side(selected.client_side, selected.server_side) if provider == "modrinth" else None
        required = tuple(d for d in selected.dependencies if d.dependency_type == "required")
        self._resolved[key] = _ResolvedCandidate(
            project_id=key,
            provider=provider,
            version=selected,
            seed_entry=seed_entry,
            is_in_house=False,
            filename=selected.filename,
            jar_sha256=None,
            provider_side=provider_side,
            required_deps=required,
            is_dependency_only=seed_entry is None,
        )

    # -- phase 3: cycle detection -------------------------------------

    def _detect_cycle(self) -> list[str] | None:
        """Return a cycle chain, or None."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = dict.fromkeys(self._resolved, WHITE)
        parent: dict[str, str] = {}

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            entry = self._resolved[node]
            for dep in entry.required_deps:
                dep_key, _ = self._dep_key_and_provider(dep)
                if dep_key is None or dep_key not in self._resolved:
                    continue
                if color[dep_key] == GRAY:
                    path = [node]
                    cur = node
                    while cur != dep_key:
                        cur = parent[cur]
                        path.append(cur)
                    path.reverse()
                    path.append(dep_key)
                    return path
                if color[dep_key] == WHITE:
                    parent[dep_key] = node
                    result = dfs(dep_key)
                    if result is not None:
                        return result
            color[node] = BLACK
            return None

        for key in list(self._resolved.keys()):
            if color.get(key) == WHITE:
                cycle = dfs(key)
                if cycle is not None:
                    return cycle
        return None

    # -- phase 4: cascade ---------------------------------------------

    def _cascade_phase(self) -> None:
        """Iteratively move entries with unresolvable required deps to cascade."""
        changed = True
        while changed:
            changed = False
            for key in list(self._resolved.keys()):
                if key in self._cascade:
                    continue
                entry = self._resolved[key]
                chain = self._resolve_cascade_chain(entry, set())
                if chain is not None:
                    self._cascade[key] = CascadeEntry(
                        filename=entry.filename,
                        provider=entry.provider,
                        project_id=entry.project_id or entry.filename,
                        reason="dependency_unsatisfiable",
                        dependency_chain=tuple(chain),
                    )
                    del self._resolved[key]
                    changed = True

    def _resolve_cascade_chain(self, entry: _ResolvedCandidate, visited: set[str]) -> list[str] | None:
        """Return ``[failed_dep, ..., this_entry]``, or None if all deps are OK."""
        key = entry.project_id or entry.filename
        if key in visited:
            return None
        visited.add(key)
        for dep in entry.required_deps:
            dep_key, _ = self._dep_key_and_provider(dep)
            if dep_key is None:
                continue
            if dep_key in self._failed:
                return [dep_key, key]
            if dep_key in self._cascade:
                downstream = list(self._cascade[dep_key].dependency_chain)
                return [*downstream, key]
            if dep_key in self._resolved:
                sub = self._resolve_cascade_chain(self._resolved[dep_key], visited)
                if sub is not None:
                    return [*sub, key]
        return None

    # -- phase 5: report builder --------------------------------------

    def _build_report(self, *, run_id: str, generated_at_utc: str) -> ResolutionReport:
        """Assemble the final :class:`ResolutionReport`."""
        has_losses = bool(self._failed or self._cascade)
        status = _derive_report_status(has_losses, self._inputs.manifest.no_version)

        seed_entries = tuple(self._build_seed_entry(e) for e in self._inputs.seed.report_entries)
        resolved_entries_list: list[ResolvedEntry] = []
        for k, e in self._resolved.items():
            entry = self._build_resolved_entry(k, e)
            if entry.side_provisional == "skipped":
                _log.info(f"resolver: excluding {entry.filename} (override=skipped)")
                continue
            resolved_entries_list.append(entry)
        resolved_entries = tuple(resolved_entries_list)

        return ResolutionReport(
            run_id=run_id,
            generated_at_utc=generated_at_utc,
            status=status,
            inputs=ReportInputs(
                manifest_sha256=self._inputs.manifest_sha256,
                resolution_inputs_sha256=self._inputs.resolution_inputs_sha256,
                side_overrides_sha256=self._inputs.side_overrides_sha256,
                release_mode=self._inputs.release_mode,
            ),
            target_runtime=TargetRuntime(
                minecraft_version=self._inputs.manifest.minecraft_version,
                loader=self._inputs.manifest.loader,
                loader_version=self._inputs.manifest.loader_version,
            ),
            policy=ReportPolicy(
                selection=self._inputs.manifest.selection,
                no_version=self._inputs.manifest.no_version,
            ),
            seed=seed_entries,
            resolved=resolved_entries,
            lost=tuple(self._failed.values()),
            cascade=tuple(self._cascade.values()),
            conflict=(),
            diagnostics=Diagnostics(provider_requests_made=self._request_count),
        )

    def _build_seed_entry(self, original: SeedEntry) -> SeedEntry:
        """Rebuild a seed entry with ``side_effective`` computed."""
        key = original.project_id or original.filename
        resolved = self._resolved.get(key)
        side_eff, _ = self._provisional_side(
            project_id=original.project_id,
            filename=original.filename,
            side_declared=original.side_declared,
            provider_side=resolved.provider_side if resolved is not None else None,
        )
        return SeedEntry(
            filename=original.filename,
            provider=original.provider,
            jar_sha256=original.jar_sha256,
            side_declared=original.side_declared,
            side_effective=side_eff,
            project_id=original.project_id,
            current_version_id=original.current_version_id,
            build_id=original.build_id,
        )

    def _build_resolved_entry(self, key: str, entry: _ResolvedCandidate) -> ResolvedEntry:
        """Convert an internal resolved candidate to a report entry."""
        if entry.is_in_house:
            side_declared = entry.seed_entry.side_declared if entry.seed_entry is not None else "unknown"
            side, source = self._provisional_side(
                project_id=None,
                filename=entry.filename,
                side_declared=side_declared,
                provider_side=None,
            )
            return ResolvedEntry(
                filename=entry.filename,
                provider="in-house",
                is_in_house=True,
                is_dependency_only=entry.is_dependency_only,
                side_provisional=side,
                side_source_provisional=source,
                project_id=None,
                version_id=None,
                build_id=entry.seed_entry.build_id if entry.seed_entry is not None else None,
                expected_hashes=(),
            )
        v = entry.version
        assert v is not None
        side_declared = entry.seed_entry.side_declared if entry.seed_entry is not None else "unknown"
        side, source = self._provisional_side(
            project_id=entry.project_id,
            filename=v.filename,
            side_declared=side_declared,
            provider_side=entry.provider_side,
        )
        expected = tuple(ExpectedHash(algorithm=algo, value=val) for algo, val in sorted(v.hashes.items()))
        return ResolvedEntry(
            filename=v.filename,
            provider=entry.provider,
            is_in_house=False,
            is_dependency_only=entry.is_dependency_only,
            side_provisional=side,
            side_source_provisional=source,
            project_id=entry.project_id,
            version_id=v.version_id,
            upgraded_from_version_id=(entry.seed_entry.current_version_id if entry.seed_entry is not None else None),
            expected_hashes=expected,
        )

    def _provisional_side(
        self,
        *,
        project_id: str | None,
        filename: str,
        side_declared: str | None,
        provider_side: str | None,
    ) -> tuple[str, str]:
        """Apply the side precedence chain (override > provider > .pw.toml)."""
        for section in (
            self._inputs.side_overrides.by_id,
            self._inputs.side_overrides.by_filename,
            self._inputs.side_overrides.deployment_tool_review,
        ):
            if project_id and project_id in section:
                return (section[project_id], "override")
            if filename in section:
                return (section[filename], "override")
        if provider_side is not None and provider_side != "unknown":
            return (provider_side, "provider")
        if side_declared in ("client", "server", "both"):
            return (side_declared, "pw_toml")
        return ("unknown", "unknown")


# ---------------------------------------------------------------------------
# Status derivation (Section 3.1)
# ---------------------------------------------------------------------------


def _derive_report_status(has_losses: bool, policy: str) -> str:
    """Map (has_losses, policy) to the report status per Section 3.1."""
    if not has_losses:
        return "complete"
    if policy == "drop":
        return "complete"
    if policy == "ask":
        return "paused_awaiting_acknowledgment"
    if policy == "fail":
        return "failed"
    raise ValueError(f"unknown no_version policy: {policy!r}")
