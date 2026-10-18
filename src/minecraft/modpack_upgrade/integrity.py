# src/minecraft/modpack_upgrade/integrity.py

"""Shared integrity checks for `acknowledge` and `materialize` (Section 6.3).

Both commands perform the same five checks in the same order:

  1. Load state.toml (caller supplies the loaded RunState).
  2. Load report from `www/runs/<run-id>/resolution-report.toml`.
  3. SHA-256 of report bytes vs. `state.report_sha256`.
  4. Recompute `resolution_inputs_sha256` from current inputs.
  5. If `release_mode == "update"`: recompute effective Compose SHA-256 vs.
     `state.effective_compose_sha256`.

`check_integrity` returns `(ok, failures)`. Callers translate a non-empty
failure list to exit 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .inhouse import load_in_house_filenames
from .manifest import load_manifest
from .overrides import load_side_overrides
from .report import compute_report_sha256
from .resolution_inputs import compute_resolution_inputs_sha256
from .seed import load_seed
from .state import RunState

__all__ = ["IntegrityContext", "check_integrity"]


@dataclass(frozen=True)
class IntegrityContext:
    """All inputs the integrity check needs beyond `RunState`.

    Computed once by the caller so a single invocation doesn't reload the
    manifest, seed, overrides, and Compose four times.
    """

    config_dir: Path
    modpack_dir: Path
    manifest_path: Path
    report_path: Path
    effective_compose_sha256: str | None  # only meaningful for release_mode == "update"


def check_integrity(state: RunState, ctx: IntegrityContext) -> list[str]:
    """Return every Section 6.3 violation. Empty list = integrity intact."""
    failures: list[str] = []

    # Step 3 - report hash
    if not ctx.report_path.is_file():
        failures.append(f"report not found: {ctx.report_path}")
        return failures  # nothing else is meaningful without the report
    actual_report_sha = compute_report_sha256(ctx.report_path)
    if actual_report_sha != state.report_sha256:
        failures.append(f"report hash mismatch: state={state.report_sha256} actual={actual_report_sha}")

    # Step 4 - resolution_inputs_sha256
    manifest = load_manifest(ctx.manifest_path)
    in_house = load_in_house_filenames(ctx.config_dir)
    side_overrides = load_side_overrides(ctx.config_dir / "side_overrides.toml")
    seed = load_seed(ctx.modpack_dir / ".index", ctx.modpack_dir, in_house)
    actual_inputs_sha = compute_resolution_inputs_sha256(
        release_mode=state.release_mode,
        manifest=manifest,
        in_house_filenames=in_house,
        seed=seed.snapshot_entries,
        side_overrides=side_overrides,
    )
    if actual_inputs_sha != state.resolution_inputs_sha256:
        failures.append(f"resolution_inputs hash mismatch: state={state.resolution_inputs_sha256} actual={actual_inputs_sha}")

    # Step 5 - Compose hash for update mode
    if state.release_mode == "update":
        if ctx.effective_compose_sha256 is None:
            failures.append("release_mode='update' requires effective_compose_sha256; caller did not supply one")
        elif ctx.effective_compose_sha256 != state.effective_compose_sha256:
            failures.append(f"compose hash mismatch: state={state.effective_compose_sha256} actual={ctx.effective_compose_sha256}")

    return failures
