# src/minecraft/modpack_upgrade/cli/commands/materialize.py

"""``minecraft-upgrade materialize --run-id <run-id>`` - fetch, verify, seal (Section 15.1.4)."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import tomli_w
from LoggingCore import get_logger

from minecraft.deploy_pack.config_model import load_compose
from minecraft.deploy_pack.daemon.config import load_config as load_daemon_config
from minecraft.deploy_pack.files import atomic_copy, atomic_write, compute_sha256, sha256_bytes
from minecraft.modpack_upgrade import canonical
from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.errors import ProviderError, SchemaError
from minecraft.modpack_upgrade.integrity import IntegrityContext, check_integrity
from minecraft.modpack_upgrade.providers.config import build_registry, load_provider_config
from minecraft.modpack_upgrade.providers.registry import ProviderRegistry
from minecraft.modpack_upgrade.pw_toml import (
    Dependency as PwTomlDependency,
)
from minecraft.modpack_upgrade.pw_toml import (
    PwTomlEntry,
    write_pw_toml,
)
from minecraft.modpack_upgrade.report import (
    ResolutionReport,
    ResolvedEntry,
    SeedEntry,
    load_report,
)
from minecraft.modpack_upgrade.side_determination import (
    SideResolution,
    resolve_final_side,
)
from minecraft.modpack_upgrade.state import (
    STATUS_ACKNOWLEDGED,
    STATUS_AWAITING_ACKNOWLEDGMENT,
    STATUS_FAILED,
    STATUS_MATERIALIZED,
    STATUS_RESOLVED,
    RunState,
    load_state,
    save_state,
)

from .acknowledge import _compute_compose_sha256

__all__ = ["run"]

_log = get_logger(__name__)
_log_cli = get_logger("minecraft.modpack_upgrade.cli.commands.materialize")

_HASH_PRIORITY: tuple[str, ...] = ("sha512", "sha256", "sha1", "md5")


# ---------------------------------------------------------------------------
# Status gate
# ---------------------------------------------------------------------------


def _refuse_bad_status(status: str, run_id: str) -> int | None:
    """Return the exit code if materialize must refuse, else None."""
    if status == STATUS_AWAITING_ACKNOWLEDGMENT:
        raise UpgradeCliError(
            6,
            f"run {run_id!r} requires acknowledgment: run `minecraft-upgrade acknowledge {run_id}` first",
        )
    if status == STATUS_FAILED:
        raise UpgradeCliError(3, f"run {run_id!r} has status 'failed'; cannot materialize")
    if status == STATUS_MATERIALIZED:
        _log_cli.info(f"run {run_id!r} is already materialized")
        _log_cli.info(f"next: iterate --run-id {run_id}")
        return 0
    if status not in (STATUS_RESOLVED, STATUS_ACKNOWLEDGED):
        raise UpgradeCliError(3, f"unexpected state status {status!r}")
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _state_dir(config_dir: Path) -> Path:
    """Return the daemon state directory."""
    return Path(load_daemon_config(config_dir).state_dir)


def _staging_dir(state_dir: Path, run_id: str) -> Path:
    """Return the run-local staging directory."""
    return state_dir / "runs" / run_id / "staging"


def _candidates_dir(state_dir: Path, run_id: str) -> Path:
    """Return the run-local candidate directory."""
    return state_dir / "candidates" / run_id


# ---------------------------------------------------------------------------
# In-house fetch
# ---------------------------------------------------------------------------


def _verify_in_house(
    entry: ResolvedEntry,
    seed_by_filename: dict[str, SeedEntry],
    warehouse_dir: Path,
) -> str:
    """Verify the in-house jar against its seed SHA-256 and any expected_hashes.

    Per Resolution_Report Section 3.3 / Section A5.2 step 6a: the seed ``jar_sha256`` is
    the primary identity. When ``expected_hashes`` are also present
    (unusual for in-house, but allowed by the schema), every listed
    digest is verified against the warehouse bytes.
    """
    seed = seed_by_filename.get(entry.filename)
    if seed is None:
        raise UpgradeCliError(3, f"in-house jar {entry.filename!r} has no seed entry")
    jar_path = warehouse_dir / entry.filename
    if not jar_path.is_file():
        raise UpgradeCliError(3, f"in-house jar not found in warehouse: {jar_path}")
    actual = compute_sha256(jar_path)
    if actual != seed.jar_sha256:
        raise UpgradeCliError(
            3,
            f"in-house jar hash mismatch for {entry.filename!r}: seed={seed.jar_sha256} actual={actual}",
        )
    if entry.expected_hashes:
        # Section A5.2 step 6a: expected_hashes, if present, must also verify.
        content = jar_path.read_bytes()
        _verify_expected_hashes(entry, content)
    _log.info(f"in-house jar verified: {entry.filename}")
    return actual


# ---------------------------------------------------------------------------
# External fetch
# ---------------------------------------------------------------------------


def _verify_expected_hashes(entry: ResolvedEntry, content: bytes) -> None:
    """Verify every ``entry.expected_hashes[]`` digest against ``content`` (Section 3.3)."""
    for exp in entry.expected_hashes:
        actual = hashlib.new(exp.algorithm, content).hexdigest()
        if actual.lower() != exp.value.lower():
            raise UpgradeCliError(
                1,
                f"hash mismatch for {entry.filename!r} ({exp.algorithm}): expected {exp.value}, got {actual}",
            )


def _fetch_external(
    entry: ResolvedEntry,
    report: ResolutionReport,
    registry: ProviderRegistry,
    staging_dir: Path,
    warehouse_dir: Path,
) -> dict:
    """Fetch an external artifact via its provider; verify and stage."""
    if entry.provider not in ("modrinth", "curseforge"):
        raise UpgradeCliError(3, f"unknown provider {entry.provider!r} for {entry.filename!r}")
    if entry.project_id is None or entry.version_id is None:
        raise UpgradeCliError(3, f"external entry {entry.filename!r} missing project_id or version_id")
    try:
        provider = registry.get(entry.provider)
    except ProviderError as exc:
        raise UpgradeCliError(3, str(exc)) from exc
    try:
        versions = provider.query_versions(
            entry.project_id,
            report.target_runtime.minecraft_version,
            report.target_runtime.loader,
        )
    except ProviderError as exc:
        raise UpgradeCliError(1, str(exc)) from exc
    matching = next((v for v in versions if v.version_id == entry.version_id), None)
    if matching is None:
        raise UpgradeCliError(
            1,
            f"version {entry.version_id!r} no longer available for {entry.project_id!r}",
        )
    try:
        artifact_bytes = provider.fetch_artifact(matching)
    except ProviderError as exc:
        raise UpgradeCliError(1, str(exc)) from exc

    _verify_expected_hashes(entry, artifact_bytes)
    actual_sha256 = sha256_bytes(artifact_bytes)

    staging_path = staging_dir / entry.filename
    atomic_write(staging_path, artifact_bytes)
    _copy_to_warehouse(staging_path, warehouse_dir, entry.filename, actual_sha256)
    _log.info(f"external artifact fetched and verified: {entry.filename}")

    return {
        "sha256": actual_sha256,
        "download_url": matching.download_url,
        "hashes": dict(matching.hashes),
        "version_id": matching.version_id,
        "version": matching,
    }


def _copy_to_warehouse(staging_path: Path, warehouse_dir: Path, filename: str, sha256: str) -> None:
    """Copy staging -> warehouse with collision detection (Section 15.1.5)."""
    dest = warehouse_dir / filename
    if dest.is_file():
        existing_sha = compute_sha256(dest)
        if existing_sha == sha256:
            _log.debug(f"warehouse collision resolved by reuse: {filename}")
            return
        raise UpgradeCliError(
            3,
            f"filename collision: {filename!r} has different bytes in warehouse (existing={existing_sha}, new={sha256})",
        )
    atomic_copy(staging_path, dest)


# ---------------------------------------------------------------------------
# Side resolution
# ---------------------------------------------------------------------------


def _resolve_side(entry: ResolvedEntry, report: ResolutionReport, warehouse_dir: Path, seed_by_filename: dict[str, SeedEntry]) -> SideResolution:
    """Resolve the final side via the precedence chain (Side_Determination)."""
    seed = seed_by_filename.get(entry.filename)
    side_declared = seed.side_declared if seed is not None else "unknown"

    side_override_result: str | None = None
    provider_side: str | None = None
    if entry.side_source_provisional == "override":
        side_override_result = entry.side_provisional
    elif entry.side_source_provisional == "provider":
        provider_side = entry.side_provisional

    jar_path = warehouse_dir / entry.filename
    return resolve_final_side(
        filename=entry.filename,
        project_id=entry.project_id,
        jar_path=jar_path,
        loader=report.target_runtime.loader,
        side_override_result=side_override_result,
        provider_side=provider_side,
        side_declared=side_declared,
    )


# ---------------------------------------------------------------------------
# .pw.toml writer
# ---------------------------------------------------------------------------


def _pick_hash(hashes: dict[str, str]) -> tuple[str, str] | None:
    """Pick the strongest available algorithm, or None."""
    for algo in _HASH_PRIORITY:
        value = hashes.get(algo)
        if value:
            return (algo, value)
    return None


def _collect_prism_deps(version: object | None) -> tuple[PwTomlDependency, ...]:
    """Return project-ID edges for ``.pw.toml`` (Section 5.7 rule 6)."""
    if version is None:
        return ()
    dependencies = getattr(version, "dependencies", ())
    out: list[PwTomlDependency] = []
    for dep in dependencies:
        if getattr(dep, "project_id", None) is None:
            continue
        dep_type = getattr(dep, "dependency_type", "")
        if dep_type not in ("required", "optional", "incompatible", "embedded"):
            continue
        out.append(PwTomlDependency(addon_id=str(dep.project_id), type=dep_type))
    return tuple(out)


def _write_one_pw_toml(
    entry: ResolvedEntry,
    fetch_info: dict,
    side: str,
    dest: Path,
) -> str:
    """Write one ``.pw.toml``; return its SHA-256."""
    hash_choice = _pick_hash(fetch_info["hashes"])
    pw = PwTomlEntry(
        filename=entry.filename,
        side=side,
        name=entry.project_id or entry.filename.removesuffix(".jar"),
        download_url=fetch_info["download_url"],
        download_hash_format=hash_choice[0] if hash_choice else None,
        download_hash=hash_choice[1] if hash_choice else None,
        modrinth_mod_id=entry.project_id if entry.provider == "modrinth" else None,
        modrinth_version=fetch_info["version_id"] if entry.provider == "modrinth" else None,
        curseforge_project_id=(int(entry.project_id) if entry.provider == "curseforge" and entry.project_id else None),
        curseforge_file_id=(int(fetch_info["version_id"]) if entry.provider == "curseforge" and fetch_info["version_id"] else None),
        dependencies=_collect_prism_deps(fetch_info["version"]),
    )
    return write_pw_toml(pw, dest)


def _write_all_pw_tomls(
    entries: list[tuple[ResolvedEntry, str]],
    fetched: dict[str, dict],
    candidates_dir: Path,
) -> dict[str, str]:
    """Write a ``.pw.toml`` for each entry; return ``{filename: pw_toml_sha256}``."""
    metadata_dir = candidates_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for entry, side in entries:
        dest = metadata_dir / f"{entry.filename.removesuffix('.jar')}.pw.toml"
        out[entry.filename] = _write_one_pw_toml(entry, fetched[entry.filename], side, dest)
    return out


# ---------------------------------------------------------------------------
# Runtime / git / instances
# ---------------------------------------------------------------------------


def _find_compose_path(config_dir: Path, project_root: Path) -> Path:
    """Return the compose path from ``deploy_pack.toml`` or the default."""
    toml_path = config_dir / "deploy_pack.toml"
    if not toml_path.is_file():
        return project_root / "docker-compose.yml"
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return project_root / "docker-compose.yml"
    docker = data.get("docker", {})
    if not isinstance(docker, dict):
        return project_root / "docker-compose.yml"
    raw = docker.get("compose_file")
    if not isinstance(raw, str) or not raw:
        return project_root / "docker-compose.yml"
    p = Path(raw)
    return p if p.is_absolute() else project_root / p


_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def _resolve_runtime(config_dir: Path, project_root: Path, report: ResolutionReport) -> dict:
    """Build the slip ``[runtime]`` dict.

    ``image_reference`` and ``platform`` come from the first compose
    service that declares them. ``image_digest`` and ``java_version``
    are placeholders until a later slice wires ``docker inspect``.
    """
    compose_path = _find_compose_path(config_dir, project_root)
    image_ref = "itzg/minecraft-server:latest"
    platform = "linux/amd64"
    result = load_compose(compose_path)
    if result.ok and result.file is not None:
        for svc in result.file.services.values():
            if svc.image:
                image_ref = svc.image
                if svc.platform:
                    platform = svc.platform
                break
    else:
        _log.warning(f"materialize: compose unavailable ({result.error}); using default image/platform")
    return {
        "minecraft_version": report.target_runtime.minecraft_version,
        "loader_type": report.target_runtime.loader,
        "loader_version": report.target_runtime.loader_version,
        "image_reference": image_ref,
        "image_digest": _PLACEHOLDER_DIGEST,
        "platform": platform,
        "java_version": "unknown",
    }


def _git_tree_hash(project_root: Path, relpath: str) -> str:
    """Return ``git rev-parse HEAD:<relpath>``, or a zero placeholder."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"HEAD:{relpath}"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "0" * 64
    if result.returncode != 0:
        return "0" * 64
    return result.stdout.strip() or "0" * 64


def _resolve_git_tree_hashes(project_root: Path) -> dict[str, str]:
    """Return the four git tree hashes for ``[inputs.git]``."""
    return {
        "config_tree": _git_tree_hash(project_root, "sync/config"),
        "kubejs_tree": _git_tree_hash(project_root, "sync/kubejs"),
        "compose_tree": _git_tree_hash(project_root, "docker-compose.yml"),
        "deploy_toml_tree": _git_tree_hash(project_root, "config.d/deploy_pack.toml"),
    }


def _load_instances(config_dir: Path) -> dict[str, dict]:
    """Load per-instance specs from ``deploy_pack.toml``.

    Parsed directly rather than via ``load_deployment_config``: only the
    instance name, config_mode, and kubejs_mode are needed to build the
    slip's ``[instances.*]`` table. Full deployment-config validation
    (``download_base_url``, ``output_filename``, compose, etc.) is
    irrelevant to the slip and would over-constrain materialize.
    """
    toml_path = config_dir / "deploy_pack.toml"
    if not toml_path.is_file():
        raise UpgradeCliError(3, f"deploy_pack.toml not found: {toml_path}")
    try:
        with open(toml_path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpgradeCliError(3, f"could not parse {toml_path}: {exc}") from exc
    instances_raw = data.get("instances")
    if not isinstance(instances_raw, dict) or not instances_raw:
        raise UpgradeCliError(3, f"no [instances] table in {toml_path}")
    out: dict[str, dict] = {}
    for name, spec in instances_raw.items():
        if not isinstance(spec, dict):
            raise UpgradeCliError(3, f"[instances.{name}] in {toml_path} must be a table")
        out[str(name)] = {
            "targeted": True,
            "required_sides": ["both", "server"],
            "config_mode": str(spec.get("config_mode", "merge")),
            "kubejs_mode": str(spec.get("kubejs_mode", "delete")),
            "config_deletions": [],
            "protect_patterns": [],
        }
    return out


# ---------------------------------------------------------------------------
# Slip assembly
# ---------------------------------------------------------------------------


def _server_inputs_manifest(jars: list[dict]) -> dict:
    """Canonical manifest of server-stage jars."""
    files = sorted(
        [{"path": j["filename"], "sha256": j["sha256"]} for j in jars if j["side"] in ("both", "server")],
        key=lambda x: x["path"],
    )
    return {"files": files}


def _client_inputs_manifest(jars: list[dict]) -> dict:
    """Canonical manifest of client-stage jars."""
    files = sorted(
        [{"path": j["filename"], "sha256": j["sha256"]} for j in jars if j["side"] in ("both", "client")],
        key=lambda x: x["path"],
    )
    return {"files": files}


def _slip_deps(entry: ResolvedEntry, fetch_info: dict) -> list[dict]:
    """Build the ``[[jar]].dependencies`` list for the slip (Section 5.2)."""
    version = fetch_info.get("version")
    if version is None:
        return []
    out: list[dict] = []
    for dep in getattr(version, "dependencies", ()):
        dep_dict: dict[str, object] = {}
        if getattr(dep, "project_id", None) is not None:
            dep_dict["namespace"] = "project"
            dep_dict["provider"] = entry.provider
            dep_dict["project_id"] = str(dep.project_id)
            if getattr(dep, "version_id", None) is not None:
                dep_dict["version_id"] = str(dep.version_id)
            dep_dict["type"] = getattr(dep, "dependency_type", "")
            dep_dict["source"] = "pw_toml"
        elif getattr(dep, "mod_id", None) is not None:
            dep_dict["namespace"] = "modid"
            dep_dict["provider"] = entry.provider
            dep_dict["type"] = getattr(dep, "dependency_type", "")
            dep_dict["source"] = "provider_metadata"
        else:
            continue
        out.append(dep_dict)
    return out


def _endpoint_for_provider(provider: str) -> str | None:
    """Return the canonical provider endpoint for the slip."""
    if provider == "modrinth":
        return "https://api.modrinth.com/v2"
    if provider == "curseforge":
        return "https://api.curseforge.com/v1"
    return None


def _build_jar_entries(
    entries_with_sides: list[tuple[ResolvedEntry, str]],
    fetched: dict[str, dict],
    pw_toml_hashes: dict[str, str],
) -> list[dict]:
    """Build the slip ``[[jar]]`` array."""
    jars: list[dict] = []
    for entry, side in entries_with_sides:
        fetch_info = fetched[entry.filename]
        jar_dict: dict[str, object] = {
            "filename": entry.filename,
            "sha256": fetch_info["sha256"],
            "side": side,
            "provider": entry.provider,
            "pw_toml": f"{entry.filename.removesuffix('.jar')}.pw.toml",
            "pw_toml_sha256": pw_toml_hashes[entry.filename],
            "dependencies": _slip_deps(entry, fetch_info),
        }
        endpoint = _endpoint_for_provider(entry.provider)
        if endpoint is not None:
            jar_dict["provider_endpoint"] = endpoint
        if entry.project_id is not None:
            jar_dict["project_id"] = entry.project_id
        if entry.version_id is not None:
            jar_dict["version_id"] = entry.version_id
        if entry.build_id is not None:
            jar_dict["build_id"] = entry.build_id
        jars.append(jar_dict)
    return jars


def _assemble_slip(
    *,
    report: ResolutionReport,
    state: RunState,
    fetched: dict[str, dict],
    entries_with_sides: list[tuple[ResolvedEntry, str]],
    pw_toml_hashes: dict[str, str],
    project_root: Path,
    config_dir: Path,
    run_id: str,
    timestamp: str,
) -> dict:
    """Assemble the slip body dict (without ``content_sha256``)."""
    runtime = _resolve_runtime(config_dir, project_root, report)
    jars = _build_jar_entries(entries_with_sides, fetched, pw_toml_hashes)
    server_sha = canonical.digest(_server_inputs_manifest(jars))
    client_sha = canonical.digest(_client_inputs_manifest(jars))
    runtime_sha = canonical.digest(runtime)
    instances = _load_instances(config_dir)

    return {
        "packing_slip_version": 1,
        "release_id": run_id,
        "release_mode": state.release_mode,
        "meta": {
            "created_at_utc": timestamp,
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "materialize",
        },
        "runtime": runtime,
        "inputs": {"git": _resolve_git_tree_hashes(project_root)},
        "fingerprints": {
            "manifest_sha256": report.inputs.manifest_sha256,
            "resolution_inputs_sha256": report.inputs.resolution_inputs_sha256,
            "runtime_sha256": runtime_sha,
            "server_inputs_sha256": server_sha,
            "client_inputs_sha256": client_sha,
        },
        "jar": jars,
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": instances,
    }


def _seal_slip(body: dict, dest: Path) -> str:
    """Compute ``content_sha256``, validate via parse_slip, write. Returns the hash."""
    computed = slip_mod.compute_content_sha256(body)
    body["content_sha256"] = computed
    try:
        slip_mod.parse_slip(body)
    except SchemaError as exc:
        raise UpgradeCliError(3, f"assembled slip failed schema validation: {exc}") from exc
    text = tomli_w.dumps(body)
    atomic_write(dest, text.encode("utf-8"))
    return computed


# ---------------------------------------------------------------------------
# Integrity context builder
# ---------------------------------------------------------------------------


def _build_integrity_ctx(
    *,
    config_dir: Path,
    project_root: Path,
    run_id: str,
    state: RunState,
) -> IntegrityContext:
    """Build the IntegrityContext for the run."""
    effective_compose_sha: str | None = None
    if state.release_mode == "update":
        effective_compose_sha = _compute_compose_sha256(config_dir, project_root)
    return IntegrityContext(
        config_dir=config_dir,
        modpack_dir=project_root / "sync" / "downloads",
        manifest_path=Path(state.manifest_path),
        report_path=project_root / "www" / "runs" / run_id / "resolution-report.toml",
        effective_compose_sha256=effective_compose_sha,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(
    *,
    config_dir: Path,
    project_root: Path,
    run_id: str,
    env: Mapping[str, str] | None = None,
    registry: ProviderRegistry | None = None,
    state_dir: Path | None = None,
    now: str | None = None,
) -> int:
    """Execute ``minecraft-upgrade materialize``. Returns exit code."""
    effective_state_dir = state_dir if state_dir is not None else _state_dir(config_dir)
    state_path = effective_state_dir / "runs" / run_id / "state.toml"

    try:
        state = load_state(state_path)
    except SchemaError as exc:
        raise UpgradeCliError(3, str(exc)) from exc

    early = _refuse_bad_status(state.status, run_id)
    if early is not None:
        return early

    integrity_ctx = _build_integrity_ctx(
        config_dir=config_dir,
        project_root=project_root,
        run_id=run_id,
        state=state,
    )
    # BUG-02: any schema violation in the current inputs (manifest, seed,
    # side_overrides) is an integrity failure -> exit 3, not exit 1.
    try:
        failures = check_integrity(state, integrity_ctx)
    except SchemaError as exc:
        raise UpgradeCliError(3, f"integrity check failed: {exc}") from exc
    if failures:
        raise UpgradeCliError(3, "integrity check failed:\n  - " + "\n  - ".join(failures))

    report = load_report(integrity_ctx.report_path)

    if state.status == STATUS_RESOLVED and (report.lost or report.cascade) and report.policy.no_version != "drop":
        raise UpgradeCliError(
            3,
            "resolved run with losses but policy is not 'drop' (should be impossible)",
        )

    seed_by_filename = {e.filename: e for e in report.seed}

    warehouse_dir = project_root / "sync" / "downloads"
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir(effective_state_dir, run_id)
    staging.mkdir(parents=True, exist_ok=True)
    candidates = _candidates_dir(effective_state_dir, run_id)

    effective_registry = registry
    if effective_registry is None:
        provider_config = load_provider_config(config_dir, env=env)
        effective_registry = build_registry(provider_config)

    timestamp = now or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        fetched: dict[str, dict] = {}
        for entry in report.resolved:
            if entry.is_in_house:
                sha256 = _verify_in_house(entry, seed_by_filename, warehouse_dir)
                fetched[entry.filename] = {
                    "sha256": sha256,
                    "download_url": None,
                    "hashes": {},
                    "version_id": None,
                    "version": None,
                }
            else:
                fetched[entry.filename] = _fetch_external(
                    entry,
                    report,
                    effective_registry,
                    staging,
                    warehouse_dir,
                )

        entries_with_sides: list[tuple[ResolvedEntry, str]] = []
        unresolved: list[ResolvedEntry] = []
        for entry in report.resolved:
            side_res = _resolve_side(entry, report, warehouse_dir, seed_by_filename)
            if side_res.side == "unknown":
                unresolved.append(entry)
                continue
            entries_with_sides.append((entry, side_res.side))

        if unresolved:
            names = ", ".join(f"{e.filename} ({e.project_id or e.filename})" for e in unresolved)
            raise UpgradeCliError(
                3,
                f"side unresolved for {len(unresolved)} artifact(s): {names}. Add entries to config.d/side_overrides.toml and re-run materialize.",
            )

        pw_toml_hashes = _write_all_pw_tomls(entries_with_sides, fetched, candidates)

        body = _assemble_slip(
            report=report,
            state=state,
            fetched=fetched,
            entries_with_sides=entries_with_sides,
            pw_toml_hashes=pw_toml_hashes,
            project_root=project_root,
            config_dir=config_dir,
            run_id=run_id,
            timestamp=timestamp,
        )
        slip_path = candidates / "packing_slip.toml"
        slip_sha = _seal_slip(body, slip_path)
        _log.info(f"slip sealed: {slip_path} (content_sha256={slip_sha})")

    finally:
        with contextlib.suppress(OSError):
            if staging.is_dir():
                for child in staging.iterdir():
                    with contextlib.suppress(OSError):
                        child.unlink()
                with contextlib.suppress(OSError):
                    staging.rmdir()

    new_state = replace(state, status=STATUS_MATERIALIZED)
    save_state(state_path, new_state)

    _log_cli.info(f"slip: {slip_path}")
    _log_cli.info(f"artifacts: {len(entries_with_sides)}")
    _log_cli.info(f"next: iterate --run-id {run_id}")
    return 0
