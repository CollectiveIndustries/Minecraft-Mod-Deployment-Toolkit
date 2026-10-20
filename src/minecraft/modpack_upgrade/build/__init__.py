# src/minecraft/modpack_upgrade/build/__init__.py

"""In-house mod build pipeline (spec v1.4.0 Section 9, repository-trigger model).

A thin orchestrator: clone repo, checkout ref, run entrypoint + script,
validate exactly one jar, generate its ``.pw.toml`` with the shared
writer at :mod:`minecraft.modpack_upgrade.pw_toml`, stage both into the
warehouse.

The build spec's required top-level ``side`` field is written into the
generated ``.pw.toml`` as its ``side``. That becomes
``[[seed]].side_declared`` after ``load_seed`` reads the file, feeding
the resolver's dependency closure. ``side_overrides.toml`` remains the
final authority across all cases - see ``docs/Side_Determination.md``.
"""

from .runner import BuildOutcome, run_build
from .spec import (
    BuildArtifact,
    BuildSpec,
    PwTomlDependency,
    load_build_spec,
    resolve_variables,
)

__all__ = [
    "BuildArtifact",
    "BuildOutcome",
    "BuildSpec",
    "PwTomlDependency",
    "load_build_spec",
    "resolve_variables",
    "run_build",
]
