# src/minecraft/deploy_pack/preflight/types.py

"""Data model for preflight results. No logic, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from minecraft.deploy_pack.docker_runtime import ContainerState
from minecraft.deploy_pack.errors import ConfigError


@dataclass(frozen=True)
class ScopeSet:
    """Which deployment scopes are active for this invocation (§2.1)."""

    server: bool = False
    client: bool = False
    resource_pack: bool = False

    def any(self) -> bool:
        """Return True if at least one scope is active."""
        return self.server or self.client or self.resource_pack

    def names(self) -> list[str]:
        """Return active scope names in §5.4's fixed order."""
        out: list[str] = []
        if self.server:
            out.append("server")
        if self.client:
            out.append("client")
        if self.resource_pack:
            out.append("resource-pack")
        return out

    def bitmask(self) -> int:
        """Return the §5.3 bitmask: server=1, client=2, resource-pack=4."""
        m = 0
        if self.server:
            m |= 1
        if self.client:
            m |= 2
        if self.resource_pack:
            m |= 4
        return m


@dataclass
class PreflightFailure:
    """A single preflight check failure."""

    source: str
    message: str


class PreflightError(ConfigError):
    """Raised after every preflight check has run (§4.3). Exit 3."""

    def __init__(self, failures: list[PreflightFailure]) -> None:
        """Collect every aggregated failure into one message."""
        self.failures = list(failures)
        lines = [f"Preflight failed with {len(self.failures)} error(s):"]
        for f in self.failures:
            lines.append(f"  [{f.source}] {f.message}")
        super().__init__("\n".join(lines))


@dataclass
class ModsChange:
    """Diff between the source mod set and the current mods_dir (§4.11)."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        """Return True if any mod was added, updated, or removed."""
        return bool(self.added or self.updated or self.removed)

    @property
    def total(self) -> int:
        """Return the total number of changed mods."""
        return len(self.added) + len(self.updated) + len(self.removed)

    def changed_paths(self) -> list[str]:
        """Return changed mod paths prefixed with ``mods/``."""
        out = [f"mods/{f}" for f in self.added]
        out += [f"mods/{f}" for f in self.updated]
        out += [f"mods/{f}" for f in self.removed]
        return out


@dataclass
class InstanceServerChange:
    """Diff for one instance's config and kubejs trees (server scope)."""

    member: str
    changed_paths: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        """Return True if any path changed for this instance."""
        return bool(self.changed_paths)


@dataclass
class ResourcePackChange:
    """Resource-pack evaluation for one instance (§4.6.6)."""

    member: str
    properties_changes: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    publish_needed: bool = False
    source_sha1: str | None = None
    source_zip: Path | None = None
    action: str = "none"

    @property
    def prompt_only(self) -> bool:
        """Return True if only the prompt changed and no publish is needed."""
        return set(self.properties_changes) == {"resource-pack-prompt"} and not self.publish_needed


@dataclass
class ReasonEntry:
    """One entry of §4.6.3's ``reasons`` list."""

    path_prefix: str
    action: str
    changed_paths: list[str]


@dataclass
class MemberPlan:
    """Preflight plan for a single partition member."""

    member: str
    container: str
    changed_paths: list[str] = field(default_factory=list)
    effective_action: str = "none"
    pack_required: bool = False
    reasons: list[ReasonEntry] = field(default_factory=list)
    server_action: str | None = None
    resource_pack_action: str | None = None
    resource_pack_target: dict[str, str] = field(default_factory=dict)
    resource_pack_publish: bool = False
    resource_pack_source: Path | None = None


@dataclass
class PreflightPlan:
    """Output of :func:`run_preflight`."""

    scopes: ScopeSet
    partition: list[str]
    member_plans: dict[str, MemberPlan]
    none_set: list[str]
    reload_set: list[str]
    restart_set: list[str]
    pack_required: bool
    container_states: dict[str, ContainerState]
    warnings: list[str] = field(default_factory=list)
    mods_change: ModsChange | None = None
    mods_dir: Path | None = None
    targeted: bool = False
    mods_drift: bool = False
    pack_required_warning: str | None = None
