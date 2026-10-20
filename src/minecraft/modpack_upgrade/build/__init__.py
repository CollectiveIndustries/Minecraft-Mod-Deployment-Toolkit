# src/minecraft/modpack_upgrade/build/__init__.py

"""In-house mod build pipeline (spec v1.3 Section 9, revised for repository-trigger).

A thin orchestrator: clone repo, checkout ref, run entrypoint + script,
collect artifacts matching declared glob patterns, copy them to the
warehouse. Everything else -- toolchain, gradle, platform choice --
lives in the mod's own repository.

The tool never needs to know whether a mod is Forge, NeoForge, or
Fabric. It reads ``config.d/build/<name>.yaml`` and executes what the
file says.
"""

from .runner import BuildOutcome, run_build
from .spec import BuildArtifact, BuildSpec, load_build_spec, resolve_variables

__all__ = [
    "BuildArtifact",
    "BuildOutcome",
    "BuildSpec",
    "load_build_spec",
    "resolve_variables",
    "run_build",
]
