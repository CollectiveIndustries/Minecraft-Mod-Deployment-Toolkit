# src/minecraft/modpack_upgrade/cli/commands/build_client.py

"""``minecraft-upgrade build-client`` -- produce a candidate client ZIP (Section 15.1).

Takes the most recent (or explicitly named) materialized candidate, builds
a SlipContext from its sealed slip and the current sync/config + sync/kubejs
trees, and drives `deploy_pack.scope_client.deploy_client_scope` to produce
the client ZIP, changelog HTML, and @www shared-item publication.

The output filename comes from `slip.client_distribution.output_filename`,
not from `[deploy_pack].output_filename`. A candidate that does not declare
`[client_distribution]` cannot produce a client ZIP -- this is enforced and
returns exit 3.

Exit codes:
  0  client ZIP, changelog, shared items all written
  1  runtime failure (ZIP write, changelog write, shared-item write)
  3  config/slip error (missing candidate, missing artifacts, invalid slip)
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import load_deployment_config
from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.deploy_pack.scope_client import deploy_client_scope
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.slip import PackingSlip, load_slip
from minecraft.modpack_upgrade.slip_context import SlipContext

__all__ = ["run"]

_log = get_logger(__name__)
_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.build_client")


def _find_candidate(candidates_root: Path, run_id: str | None) -> Path:
    """Return the candidate directory to build from."""
    if not candidates_root.is_dir():
        raise UpgradeCliError(3, f"no candidates directory at {candidates_root}; run `materialize` first")
    if run_id is not None:
        candidate = candidates_root / run_id
        if not candidate.is_dir() or not (candidate / "packing_slip.toml").is_file():
            raise UpgradeCliError(3, f"candidate not found: {candidate}")
        return candidate
    candidates = [d for d in candidates_root.iterdir() if (d / "packing_slip.toml").is_file()]
    if not candidates:
        raise UpgradeCliError(3, f"no materialized candidates under {candidates_root}")
    candidates.sort(key=lambda d: (d / "packing_slip.toml").stat().st_mtime, reverse=True)
    if len(candidates) > 1:
        _log_cli.warning(f"multiple candidates found; using most recent: {candidates[0].name}")
    return candidates[0]


def _build_slip_context(candidate_dir: Path, slip: PackingSlip, project_root: Path) -> SlipContext:
    """Build a SlipContext for ZIP building.

    ``captured_head`` is required by SlipContext's validation but unused
    for client-ZIP building, so a fixed marker is supplied. The world
    tree is not consulted; only the artifact set, config, and kubejs are.
    """
    artifacts_root = candidate_dir / "artifacts"
    if not artifacts_root.is_dir():
        raise UpgradeCliError(3, f"candidate artifacts directory missing: {artifacts_root}")

    jar_sources: dict[str, Path] = {}
    for jar in slip.jars:
        path = artifacts_root / jar.filename
        if not path.is_file():
            raise UpgradeCliError(3, f"jar missing from candidate: {path}")
        jar_sources[jar.filename] = path

    rp_sources: dict[str, Path] = {}
    for rp in slip.resource_packs:
        path = artifacts_root / rp.filename
        if not path.is_file():
            raise UpgradeCliError(3, f"resource pack missing from candidate: {path}")
        rp_sources[rp.filename] = path

    config_source = project_root / "sync" / "config"
    kubejs_source = project_root / "sync" / "kubejs"
    if not config_source.is_dir():
        raise UpgradeCliError(3, f"config source missing: {config_source}")
    if not kubejs_source.is_dir():
        raise UpgradeCliError(3, f"kubejs source missing: {kubejs_source}")

    return SlipContext(
        slip=slip,
        mode="integration",
        bundle_root=candidate_dir,
        config_source=config_source,
        kubejs_source=kubejs_source,
        jar_sources=MappingProxyType(jar_sources),
        rp_sources=MappingProxyType(rp_sources),
        runtime_payload=candidate_dir / "runtime",
        captured_head="build-client",
    )


def run(
    *,
    config_dir: Path,
    project_root: Path,
    run_id: str | None = None,
    with_resources: bool = False,
) -> int:
    """Build a client ZIP from a materialized candidate."""
    if not with_resources:
        _log_cli.info("with_resources=False; run with --with-resources to include resource packs")
    daemon_config = load_daemon_config(config_dir)
    candidates_root = Path(daemon_config.state_dir) / "candidates"
    candidate_dir = _find_candidate(candidates_root, run_id)
    _log_cli.info(f"candidate: {candidate_dir}")

    slip_path = candidate_dir / "packing_slip.toml"
    try:
        slip = load_slip(slip_path)
    except Exception as exc:
        raise UpgradeCliError(3, f"could not load candidate slip: {exc}") from exc

    if slip.client_distribution is None:
        raise UpgradeCliError(
            3,
            "candidate slip does not declare [client_distribution]; cannot build client ZIP",
        )

    slip_ctx = _build_slip_context(candidate_dir, slip, project_root)
    try:
        config = load_deployment_config(config_dir, slip=slip_ctx)
    except Exception as exc:
        raise UpgradeCliError(3, f"could not load deploy_pack config: {exc}") from exc

    result = deploy_client_scope(config, with_resources, protect_patterns=[])
    if not result.success:
        raise UpgradeCliError(1, result.failure_message or "client scope failed")

    _log_cli.info("")
    _log_cli.info(f"ZIP: {result.zip_path}")
    _log_cli.info(f"SHA-256: {result.zip_sha256}")
    if result.changelog_path:
        _log_cli.info(f"changelog: {result.changelog_path}")
    if result.zip_url:
        _log_cli.info(f"URL: {result.zip_url}")
    if result.initial_build:
        _log_cli.info("(initial build: no prior baseline)")
    return 0
