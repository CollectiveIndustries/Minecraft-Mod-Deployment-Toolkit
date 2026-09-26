# src/minecraft/deploy_pack/config_model.py

"""Configuration loading, merging, and validation (Project_Specs.md §3).

Responsibilities (§9.2):
  * load config.d/.env and config.d/deploy_pack.{toml,yaml,yml} via ConfigCore
  * read docker-compose.yml as an enrichment source (§3.2)
  * resolve project-relative paths against the project root (§3.15)
  * derive www_dir from compose when not set in TOML (§3.19)
  * derive per-instance roots from compose (§3.18)
  * resolve the deployment partition from the requested instance set (§2.9)
  * parse stop_grace_period Go duration strings (§3.2)
  * validate in-game templates (§5.11), output_filename (§7.1),
    download_base_url (§7.5), docker timing keys (§3.9)

Explicit non-responsibilities:
  * No os.environ access (§9.2). The environment-variable config source
    was removed in v3.0 (§3.14, §3.16). config.d/.env is a flat file
    source only; keys are used verbatim (§3.12).
  * Partition-scoped checks (mods_dir bind agreement §3.7, healthcheck
    declaration §3.8, compose-vs-container drift §3.17) are preflight's
    job. This module does not raise on their behalf.
  * Discord template validation belongs to notifications.py (§5.11:
    validated only when --notify is active).
  * side_overrides.toml loading belongs to overrides.py.

Non-raising compose handling:
  load_compose and derive_www_dir never raise on missing / malformed /
  ambiguous input. They return structured results carrying a diagnostic
  string. Preflight applies §3.5's per-scope decision table and
  aggregates. This preserves §4.3 ("all independently detectable
  failures collected before any write") - a broken compose must not
  short-circuit a run that also has, say, an orphan [resource_pack.X]
  section.

Logging
-------

Module logger is ``minecraft.deploy_pack.config_model``. Config load
logs at INFO on entry and on success with the resolved partition and
path roots; each resolution step (path normalization, compose
derivation, instance/partition matching, CLI overlay merge) logs its
inputs and outcomes at DEBUG. Degraded-but-continuing conditions (a
broken compose file, an ambiguous www_dir, a TOML-vs-compose
disagreement on www_dir) log at WARN. Validation failures log at ERROR
immediately before the ``ConfigError``/``ValueError`` raise, so the
specific diagnostic lands in the sink regardless of how the caller
handles the exception. ``load_deployment_config`` accepts an optional
``logger=`` override; downstream helpers use the module logger.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ConfigCore import ConfigManager

from .errors import ConfigError
from .logging_setup import get_logger

_log = get_logger(__name__)

DEFAULT_RESTART_NOTICE_TEMPLATE = "Server pack updated. Restart in {time}."
DEFAULT_RESTART_CANCEL_NOTICE_TEMPLATE = "Server restart canceled; the deployment could not safely proceed."
DEFAULT_STOP_GRACE_SECONDS = 10


@dataclass
class BindMount:
    """Represents a bind mount between a host path and a container path.

    Attributes:
        host_source: Path on the host filesystem.
        container_target: Mount target path inside the container.
    """

    host_source: Path
    container_target: str


@dataclass
class ComposeService:
    """Represents a single service defined in a Docker Compose file.

    Attributes:
        name: Service name as declared in the Compose file.
        container_name: Optional explicit container name.
        binds: List of bind mounts configured for the service.
        stop_grace_period: Optional grace period before forced shutdown.
        stop_signal: Optional signal used to stop the container.
        has_healthcheck: Whether the service defines a healthcheck.
        secrets: Names of secrets attached to the service.
        environment: Environment variables for the service.
        env_files: Paths to environment files loaded by the service.
    """

    name: str
    container_name: str | None
    binds: list[BindMount]
    stop_grace_period: str | None
    stop_signal: str | None
    has_healthcheck: bool
    secrets: list[str]
    environment: dict[str, str]
    env_files: list[Path]


@dataclass
class ComposeFile:
    """Represents a parsed Docker Compose file.

    Attributes:
        path: Path to the Compose file.
        base_dir: Base directory used to resolve relative paths.
        services: Mapping of service names to their parsed ComposeService definitions.
        secret_files: Mapping of secret names to their resolved file paths.
    """

    path: Path
    base_dir: Path
    services: dict[str, ComposeService]
    secret_files: dict[str, Path]


@dataclass
class ComposeLoadResult:
    """Result of loading the compose file. Never raises (§3.5, §4.3).

    ``file`` is None if the file could not be read or parsed; ``error``
    holds a human-readable diagnostic in that case. Preflight applies
    §3.5's per-scope decision table to ``error``. ``file`` is None and
    ``error`` is None only if the compose file path was never provided
    (which is not currently reachable - compose_file always has a default).
    """

    file: ComposeFile | None
    error: str | None

    @property
    def ok(self) -> bool:
        """Checks whether the file is available."""
        return self.file is not None


@dataclass
class WwwDirResult:
    """Result of deriving www_dir from compose (§3.19). Never raises."""

    path: Path | None
    error: str | None
    candidates: list[Path] = field(default_factory=list)


@dataclass
class InstanceConfig:
    """Configuration for one Minecraft server instance.

    Combines TOML-declared fields with compose-derived data. Compose-
    derived attributes are populated only when a compose file loaded and
    the container lookup succeeded; otherwise the corresponding error
    fields carry the diagnostic for preflight to evaluate.
    """

    name: str
    container: str
    config_mode: str = "merge"
    kubejs_mode: str = "delete"
    service: ComposeService | None = None
    service_match_error: str | None = None
    instance_root: Path | None = None
    config_path: Path | None = None
    kubejs_path: Path | None = None
    server_properties_path: Path | None = None
    stop_grace_period_raw: str | None = None
    stop_grace_seconds: int = DEFAULT_STOP_GRACE_SECONDS
    stop_grace_parse_error: str | None = None
    stop_signal: str | None = None


@dataclass
class ResourcePackConfig:
    """A single resource pack entry advertised to clients.

    Describes the downloadable filename, whether clients are required to
    accept it, and an optional prompt shown to players.
    """

    filename: str
    required: bool
    prompt: str = ""


@dataclass
class DockerConfig:
    """Docker and compose runtime settings for the deployment.

    Captures the compose file location, restart and health timing
    thresholds, in-game notice requirements, restart notice templates, and
    the optional RCON host. Defaults match the shipped behavior and may be
    overridden from TOML.
    """

    compose_file: Path
    restart_wait_seconds: int = 300
    in_game_notice_required: bool = True
    health_timeout_seconds: int = 600
    health_poll_seconds: int = 2
    preflight_restarting_wait_seconds: int = 30
    cancel_notice_ready_timeout_seconds: int = 30
    restart_notice_template: str = DEFAULT_RESTART_NOTICE_TEMPLATE
    restart_cancel_notice_template: str = DEFAULT_RESTART_CANCEL_NOTICE_TEMPLATE
    rcon_host: str | None = None


@dataclass
class DiscordConfig:
    """Discord role and template configuration for webhook notifications.

    Holds the role names permitted to issue privileged commands and the
    optional message templates used for live, online, failure, and
    diagnostic notifications. Templates left as None fall back to the
    implementations' built-in defaults.
    """

    player_roles: list[str] = field(default_factory=list)
    operator_roles: list[str] = field(default_factory=list)
    live_template: str | None = None
    online_template: str | None = None
    failure_template: str | None = None
    diagnostic_template: str | None = None


@dataclass
class DeploymentConfig:
    """Fully resolved deployment configuration for a single run.

    Aggregates paths, instance partitions, resource packs, Docker and
    Discord settings, and the compose load result consumed by preflight
    and subsequent pipeline stages. Fields whose availability depends on
    external inputs (TOML, compose, CLI flags) carry their own error or
    candidate channels so preflight can decide fatality per the relevant
    section of the spec.

    Partition targeting (§2.9, §9.2):
      * ``partition`` - lexicographically sorted, deduplicated member names.
      * ``partition_unknown`` - requested names that are not configured.
      * ``requested_instances`` - the raw set of names passed via
        ``--instance``, or None when ``--instance`` was absent.
      * ``partition_requested`` - True when ``--instance`` was passed.
        Redundant with ``requested_instances is not None`` but exposed as
        a stable boolean for callers that only need the flag.
    """

    project_root: Path
    config_dir: Path
    sync_root: Path
    modpack_dir: Path
    www_dir: Path | None
    www_dir_error: str | None
    www_dir_candidates: list[Path]
    output_filename: str
    download_base_url: str
    protect_file: Path | None
    sync_mapping: dict[str, Any]
    restart_policy: dict[str, str]
    instances: dict[str, InstanceConfig]
    partition: list[str]
    partition_unknown: list[str]
    requested_instances: set[str] | None
    partition_requested: bool = False
    resource_packs: dict[str, ResourcePackConfig] = field(default_factory=dict)
    docker: DockerConfig = None  # type: ignore[assignment]
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    webhook_url: str | None = None
    compose: ComposeLoadResult = None  # type: ignore[assignment]
    mods_dir_toml: Path | None = None


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_DURATION_TOKEN = re.compile("(\\d+)([smh])")


def parse_go_duration(value: str) -> int:
    """Parse a Go duration string ('30s', '1m30s', '2h') to integer seconds.

    Raises ValueError on any input that is not a sequence of <int><unit>
    tokens covering the entire string. Sub-second units are rejected:
    the spec's examples are whole-second, and silently truncating
    '1.5s' to 1s is worse than refusing it.
    """
    s = value.strip()
    if not s:
        _log.error(f"parse_go_duration: empty duration {value!r}")
        raise ValueError("empty duration")
    total = 0
    pos = 0
    for m in _DURATION_TOKEN.finditer(s):
        if m.start() != pos:
            _log.error(f"parse_go_duration: invalid duration {value!r} (unexpected token at offset {pos})")
            raise ValueError(f"invalid duration: {value!r}")
        total += int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        pos = m.end()
    if pos != len(s):
        _log.error(f"parse_go_duration: invalid duration {value!r} (trailing data at offset {pos})")
        raise ValueError(f"invalid duration: {value!r}")
    _log.debug(f"parse_go_duration: {value!r} -> {total}s")
    return total


_PLACEHOLDER_RE = re.compile("\\{([^{}]*)\\}")


def validate_in_game_templates(docker: DockerConfig) -> None:
    """Validate the two in-game templates at config load time (§5.11).

    In-game templates are validated unconditionally, regardless of
    --notify. Discord templates are validated by notifications.py.
    """
    t = docker.restart_notice_template
    if not t:
        _log.error("validate_in_game_templates: [docker].restart_notice_template is empty")
        raise ConfigError("[docker].restart_notice_template must be non-empty")
    for m in _PLACEHOLDER_RE.finditer(t):
        if m.group(1) != "time":
            _log.error(f"validate_in_game_templates: [docker].restart_notice_template has unknown placeholder {{{m.group(1)}}}")
            raise ConfigError(f"[docker].restart_notice_template: unknown placeholder {{{m.group(1)}}} (only {{time}} is permitted)")
    c = docker.restart_cancel_notice_template
    if not c:
        _log.error("validate_in_game_templates: [docker].restart_cancel_notice_template is empty")
        raise ConfigError("[docker].restart_cancel_notice_template must be non-empty")
    if _PLACEHOLDER_RE.search(c):
        _log.error(f"validate_in_game_templates: [docker].restart_cancel_notice_template contains a placeholder ({c!r})")
        raise ConfigError("[docker].restart_cancel_notice_template must not contain placeholders")
    _log.debug("validate_in_game_templates: both in-game templates valid")


def validate_output_filename(name: str) -> None:
    """Validates an output filename for safety and required format."""
    if not name:
        _log.error("validate_output_filename: output_filename is empty")
        raise ConfigError("output_filename must be non-empty")
    if "\x00" in name:
        _log.error(f"validate_output_filename: output_filename contains NUL ({name!r})")
        raise ConfigError("output_filename must not contain NUL")
    if "/" in name or "\\" in name:
        _log.error(f"validate_output_filename: output_filename contains a path separator ({name!r})")
        raise ConfigError("output_filename must not contain path separators")
    if name in (".", ".."):
        _log.error(f"validate_output_filename: output_filename is {name!r}")
        raise ConfigError("output_filename must not be '.' or '..'")
    if not name.endswith(".zip"):
        _log.error(f"validate_output_filename: output_filename does not end in .zip ({name!r})")
        raise ConfigError("output_filename must end in '.zip'")
    _log.debug(f"validate_output_filename: {name!r} OK")


def validate_download_base_url(url: str) -> None:
    """Validates the download base URL.

    Raises:
        ConfigError: If the URL is empty.
    """
    if not url:
        _log.error("validate_download_base_url: download_base_url is empty")
        raise ConfigError("download_base_url must be non-empty")
    _log.debug(f"validate_download_base_url: {url!r} OK")


_NON_NEGATIVE = ("restart_wait_seconds", "preflight_restarting_wait_seconds", "cancel_notice_ready_timeout_seconds")
_POSITIVE = ("health_timeout_seconds", "health_poll_seconds")


def _validate_docker_timings(docker: DockerConfig) -> None:
    for key in _NON_NEGATIVE:
        value = getattr(docker, key)
        if value < 0:
            _log.error(f"_validate_docker_timings: [docker].{key} must be >= 0, got {value}")
            raise ConfigError(f"[docker].{key} must be >= 0, got {value}")
    for key in _POSITIVE:
        value = getattr(docker, key)
        if value <= 0:
            _log.error(f"_validate_docker_timings: [docker].{key} must be > 0, got {value}")
            raise ConfigError(f"[docker].{key} must be > 0, got {value}")
    _log.debug("_validate_docker_timings: all docker timing keys valid")


def _parse_binds(volumes: Any) -> list[BindMount]:
    """Return bind mounts only. Named and anonymous volumes are ignored."""
    result: list[BindMount] = []
    if not isinstance(volumes, list):
        _log.debug("_parse_binds: volumes is not a list; no binds parsed")
        return result
    for vol in volumes:
        if isinstance(vol, str):
            parts = vol.split(":")
            if len(parts) < 2:
                continue
            host_raw, target = (parts[0], parts[1])
            if not (host_raw.startswith("/") or host_raw.startswith("./") or host_raw.startswith("../") or host_raw.startswith("~")):
                continue
            result.append(BindMount(host_source=Path(host_raw), container_target=target))
        elif isinstance(vol, dict):
            if vol.get("type") != "bind":
                continue
            src = vol.get("source")
            dst = vol.get("target")
            if src and dst:
                result.append(BindMount(host_source=Path(str(src)), container_target=str(dst)))
    _log.debug(f"_parse_binds: parsed {len(result)} bind mount(s)")
    return result


def _parse_environment(env: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(env, dict):
        for k, v in env.items():
            result[str(k)] = "" if v is None else str(v)
    elif isinstance(env, list):
        for item in env:
            if not isinstance(item, str):
                continue
            if "=" in item:
                k, _, v = item.partition("=")
                result[k] = v
    return result


def _parse_env_files(value: Any, base_dir: Path) -> list[Path]:
    result: list[Path] = []
    if value is None:
        return result
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, str):
            continue
        p = Path(item)
        if not p.is_absolute():
            p = base_dir / p
        result.append(p)
    _log.debug(f"_parse_env_files: resolved {len(result)} env_file(s) under {base_dir}")
    return result


def _parse_secrets(value: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(value, list):
        return result
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            src = item.get("source")
            if src:
                result.append(str(src))
    _log.debug(f"_parse_secrets: parsed {len(result)} secret name(s)")
    return result


def _parse_service(name: str, raw: dict, base_dir: Path) -> ComposeService:
    _log.debug(f"_parse_service: parsing service {name!r}")
    return ComposeService(
        name=name,
        container_name=str(raw["container_name"]) if raw.get("container_name") else None,
        binds=_parse_binds(raw.get("volumes") or []),
        stop_grace_period=str(raw["stop_grace_period"]) if raw.get("stop_grace_period") else None,
        stop_signal=str(raw["stop_signal"]) if raw.get("stop_signal") else None,
        has_healthcheck=isinstance(raw.get("healthcheck"), dict),
        secrets=_parse_secrets(raw.get("secrets")),
        environment=_parse_environment(raw.get("environment")),
        env_files=_parse_env_files(raw.get("env_file"), base_dir),
    )


def load_compose(path: Path) -> ComposeLoadResult:
    """Load a compose file. Never raises (§3.5).

    Returns a ComposeLoadResult. On any failure, ``file`` is None and
    ``error`` carries a human-readable diagnostic. Preflight owns the
    per-scope decision table (§3.5): a broken compose is fatal for
    --server and (conditionally) --resource-pack, but only a warning for
    --client unless www_dir becomes undeterminable.
    """
    _log.debug(f"load_compose: reading {path}")
    if not path.is_file():
        _log.warning(f"load_compose: compose file not found: {path}")
        return ComposeLoadResult(None, f"Compose file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(f"load_compose: could not read {path}: {exc}")
        return ComposeLoadResult(None, f"Could not read compose file {path}: {exc}")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _log.warning(f"load_compose: could not parse {path}: {exc}")
        return ComposeLoadResult(None, f"Could not parse compose file {path}: {exc}")
    if raw is None:
        _log.warning(f"load_compose: compose file is empty: {path}")
        return ComposeLoadResult(None, f"Compose file is empty: {path}")
    if not isinstance(raw, dict):
        _log.warning(f"load_compose: {path}: top-level is not a mapping")
        return ComposeLoadResult(None, f"Compose file {path}: top-level must be a mapping")
    base_dir = path.parent
    services_raw = raw.get("services") or {}
    if not isinstance(services_raw, dict):
        _log.warning(f"load_compose: {path}: 'services' is not a mapping")
        return ComposeLoadResult(None, f"Compose file {path}: 'services' must be a mapping")
    services: dict[str, ComposeService] = {}
    for svc_name, svc_raw in services_raw.items():
        if not isinstance(svc_raw, dict):
            continue
        services[str(svc_name)] = _parse_service(str(svc_name), svc_raw, base_dir)
    secret_files: dict[str, Path] = {}
    secrets_raw = raw.get("secrets") or {}
    if isinstance(secrets_raw, dict):
        for sec_name, sec_raw in secrets_raw.items():
            if not isinstance(sec_raw, dict):
                continue
            file_value = sec_raw.get("file")
            if not file_value:
                continue
            p = Path(str(file_value))
            if not p.is_absolute():
                p = base_dir / p
            secret_files[str(sec_name)] = p
    _log.info(f"load_compose: loaded {len(services)} service(s) and {len(secret_files)} secret file(s) from {path}")
    return ComposeLoadResult(ComposeFile(path=path, base_dir=base_dir, services=services, secret_files=secret_files), None)


class ServiceMatchError(ConfigError):
    """Raised by match_service_by_container on zero or multiple matches.

    Callers in _build_deployment_config catch it and store the message on
    InstanceConfig.service_match_error so that preflight can aggregate
    (§4.3, §3.6). Public callers may treat it as a normal ConfigError.
    """


def match_service_by_container(compose: ComposeFile, container_name: str) -> ComposeService:
    """Return the single service whose container_name matches (§3.6).

    Raises ServiceMatchError (a ConfigError subclass) on zero or multiple
    matches. This is the config-layer API; preflight is responsible for
    deciding what to do with the failure.
    """
    _log.debug(f"match_service_by_container: looking up container_name={container_name!r} in {len(compose.services)} service(s)")
    matches = [svc for svc in compose.services.values() if svc.container_name == container_name]
    if not matches:
        _log.error(f"match_service_by_container: no compose service has container_name={container_name!r}")
        raise ServiceMatchError(f"No compose service has container_name={container_name!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(s.name for s in matches))
        _log.error(f"match_service_by_container: multiple services match container_name={container_name!r}: {names}")
        raise ServiceMatchError(f"Multiple compose services match container_name={container_name!r}: {names}")
    _log.debug(f"match_service_by_container: {container_name!r} -> service {matches[0].name!r}")
    return matches[0]


def _resolve_compose_path(p: Path, base_dir: Path) -> Path:
    """Resolve a compose-derived host path against the compose base dir.

    Compose file source paths are relative to the compose file's
    directory (that is Docker Compose semantics). Project TOML paths are
    resolved against the project root elsewhere; these are different
    bases and must not be conflated.
    """
    if not p.is_absolute():
        p = base_dir / p
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def resolve_compose_path(p: Path, base_dir: Path) -> Path:
    """Public alias for :func:`_resolve_compose_path`.

    Exposed so preflight's drift check (§3.17) can resolve compose bind
    sources consistently with the config layer. Compose file source
    paths are relative to the compose file's directory; this is a
    different base than project-root TOML paths and must not be
    conflated.
    """
    resolved = _resolve_compose_path(p, base_dir)
    _log.debug(f"resolve_compose_path: {p} (base={base_dir}) -> {resolved}")
    return resolved


def derive_instance_root(svc: ComposeService) -> Path | None:
    """Derive the instance root directory from a compose service (§3.18).

    Three shapes are supported:

      * A bind mount whose container target is exactly ``/data``. The
        host source of that bind is the instance root. This is the
        canonical shape assumed by the spec's reference compose file.

      * Per-subpath binds such as ``/data/config``, ``/data/kubejs``,
        ``/data/world``, or ``/data/server.properties``. ``/data`` itself
        is a Docker-managed named volume, and only the pieces that need
        host access are bound in. The instance root is inferred by
        stripping the container subpath from each host source.

      * A service may also declare binds under ``/data/`` that are not
        instance data -- for example ``/data/crash-reports`` pointing at
        ``./logs/survival/crash-reports``. Those binds imply a *different*
        root and would poison a naive common-prefix computation. The rule
        is: the candidate root that the most binds agree on wins; a tie
        is treated as genuinely ambiguous and returns None.

    ``/data/mods`` is excluded from the computation because it is a
    shared bind derived separately by :func:`derive_mods_dir`.
    """
    for bind in svc.binds:
        if bind.container_target == "/data":
            _log.debug(f"derive_instance_root: [{svc.name}] canonical /data bind -> {bind.host_source}")
            return bind.host_source

    implied_counts: dict[str, int] = {}
    for bind in svc.binds:
        target = bind.container_target
        if not target.startswith("/data/") or target == "/data/mods":
            continue
        subpath = target[len("/data/") :]
        host = str(bind.host_source)
        for sep in ("/", "\\"):
            suffix = sep + subpath
            if host.endswith(suffix):
                root = host[: -len(suffix)]
                implied_counts[root] = implied_counts.get(root, 0) + 1
                break

    if not implied_counts:
        _log.debug(f"derive_instance_root: [{svc.name}] no /data or /data/* bind found")
        return None
    if len(implied_counts) == 1:
        root = next(iter(implied_counts))
        _log.debug(f"derive_instance_root: [{svc.name}] single implied root -> {root}")
        return Path(root)
    ranked = sorted(implied_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if ranked[0][1] == ranked[1][1]:
        _log.warning(f"derive_instance_root: [{svc.name}] ambiguous /data/* binds (tie): " + ", ".join(f"{r} x{n}" for r, n in ranked))
        return None
    _log.debug(f"derive_instance_root: [{svc.name}] most-agreed root -> {ranked[0][0]} ({ranked[0][1]} bind(s))")
    return Path(ranked[0][0])


def derive_mods_dir(svc: ComposeService) -> Path | None:
    """Derives the mods directory from a compose service."""
    for bind in svc.binds:
        if bind.container_target == "/data/mods":
            _log.debug(f"derive_mods_dir: [{svc.name}] -> {bind.host_source}")
            return bind.host_source
    _log.debug(f"derive_mods_dir: [{svc.name}] no /data/mods bind found")
    return None


def derive_www_dir(compose: ComposeFile) -> WwwDirResult:
    """Find the unique bind source with a target starting /usr/share/nginx/.

    Never raises. Exact target '/usr/share/nginx' (no subpath) is
    ignored, per §3.19. On zero or multiple candidates, ``path`` is
    None and ``error`` explains why.
    """
    candidates: list[Path] = []
    for svc in compose.services.values():
        for bind in svc.binds:
            if bind.container_target.startswith("/usr/share/nginx/"):
                candidates.append(bind.host_source)
    seen: set[str] = set()
    unique: list[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    if not unique:
        _log.warning("derive_www_dir: no bind mount whose target starts with /usr/share/nginx/")
        return WwwDirResult(None, "www_dir could not be derived: no bind mount whose target starts with /usr/share/nginx/", [])
    if len(unique) > 1:
        _log.warning(f"derive_www_dir: {len(unique)} candidate mount(s) found; ambiguous: " + ", ".join(str(p) for p in unique))
        return WwwDirResult(None, f"www_dir could not be derived: {len(unique)} candidate mounts", unique)
    _log.debug(f"derive_www_dir: -> {unique[0]}")
    return WwwDirResult(unique[0], None, [])


def resolve_partition(instances: Mapping[str, InstanceConfig], requested: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """Resolve the deployment partition (§2.9).

    Returns (partition, unknown). ``partition`` is the sorted, deduplicated
    set of member names; ``unknown`` is the sorted set of requested names
    that are not configured.

    ``requested`` is None when --instance was absent; in that case the
    partition is every configured instance. Requested names are deduplicated
    and sorted lexicographically, per §2.9.

    Does not raise. Preflight raises exit 3 if ``unknown`` is non-empty
    (§2.5: "``--instance X`` where X not configured → Preflight error,
    exit 3"). This keeps §4.3 aggregation intact.
    """
    configured = set(instances.keys())
    if requested is None:
        partition = sorted(configured)
        _log.debug(f"resolve_partition: no --instance; partition = all {len(partition)} configured instance(s)")
        return (partition, [])
    requested_set = {str(name) for name in requested}
    unknown = sorted(requested_set - configured)
    partition = sorted(requested_set & configured)
    _log.info(f"resolve_partition: requested={sorted(requested_set)} -> partition={partition}; unknown={unknown}")
    return (partition, unknown)


def _find_config_file(config_dir: Path) -> Path | None:
    for ext in (".toml", ".yaml", ".yml"):
        candidate = config_dir / f"deploy_pack{ext}"
        if candidate.is_file():
            _log.debug(f"_find_config_file: using {candidate}")
            return candidate
    _log.debug(f"_find_config_file: no deploy_pack.{{toml,yaml,yml}} under {config_dir}")
    return None


def _load_config_files(config_dir: Path) -> dict[str, Any]:
    """Load config.d/.env and config.d/deploy_pack.{toml,yaml,yml}.

    Priority (§3.1, increasing): .env < TOML file < CLI arguments.

    Both files are fed to ConfigCore as file sources. The .env is loaded
    as a flat key-value source with literal keys (§3.12, §3.16): no
    prefix stripping, no separator expansion, no case folding. Malformed
    lines (no '=') are silently skipped by ConfigCore's parser; that is
    the required behavior per §3.12.
    """
    env_file = config_dir / ".env"
    toml_path = _find_config_file(config_dir)
    if not env_file.is_file() and toml_path is None:
        _log.debug(f"_load_config_files: neither .env nor deploy_pack.{{toml,yaml,yml}} present under {config_dir}")
        return {}
    mgr = ConfigManager()
    if env_file.is_file():
        _log.debug(f"_load_config_files: loading .env {env_file}")
        mgr.file(env_file, format="env")
    if toml_path is not None:
        _log.debug(f"_load_config_files: loading {toml_path}")
        mgr.file(toml_path)
    config = mgr.load()
    if hasattr(config, "as_dict"):
        result = dict(config.as_dict())
    else:
        result = dict(config)
    _log.debug(f"_load_config_files: loaded {len(result)} top-level key(s)")
    return result


def _parse_cli_overrides(args: list[str]) -> dict[str, Any]:
    """Convert ['--foo-bar', 'v', '--baz=1'] to {'foo_bar': 'v', 'baz': '1'}.

    §2.8: CLI flags are kebab-case; TOML keys are snake_case. argparse
    has already consumed the flags it owns, so anything left here is a
    config override. Hyphens are normalised to underscores so the
    overlay lands on the same TOML keys.
    """
    result: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("--"):
            i += 1
            continue
        body = arg[2:]
        if "=" in body:
            key, _, value = body.partition("=")
        else:
            key = body
            value = None
            if i + 1 < len(args) and (not args[i + 1].startswith("--")):
                value = args[i + 1]
                i += 1
        key = key.replace("-", "_")
        if value is None:
            result[key] = True
        else:
            result[key] = value
        i += 1
    if result:
        _log.debug(f"_parse_cli_overrides: parsed {len(result)} override(s): {sorted(result)}")
    return result


def _deep_merge(base: dict, overlay: dict) -> None:
    """In-place deep merge: overlay wins on conflicting leaves."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_deployment_config(
    config_dir: Path, requested_instances: Iterable[str] | None = None, cli_remaining: list[str] | None = None, logger: Any = None
) -> DeploymentConfig:
    """Load, merge, and validate configuration. Raises ConfigError on failure.

    ``requested_instances`` is the set of names supplied via --instance,
    or None if --instance was absent. Argparse parsing of --instance
    happens in main; resolution (default-set expansion, existence check,
    lexicographic sort, dedup) happens here (§2.9, §9.2).

    ``cli_remaining`` are arguments argparse did not consume. They are
    the highest-priority config source (§3.1).

    This function performs no os.environ access (§9.2).

    A broken compose file does NOT raise. The DeploymentConfig.compose
    field carries the diagnostic; preflight applies §3.5.
    """
    if logger is None:
        logger = _log
    logger.info(f"config load: config_dir={config_dir} requested_instances={sorted(requested_instances) if requested_instances is not None else None}")
    config_dir = config_dir.resolve()
    project_root = config_dir.parent
    raw = _load_config_files(config_dir)
    if cli_remaining:
        cli_overlay = _parse_cli_overrides(cli_remaining)
        _deep_merge(raw, cli_overlay)
        logger.debug(f"config load: merged {len(cli_overlay)} CLI override(s) over file sources")
    return _build_deployment_config(raw=raw, config_dir=config_dir, project_root=project_root, requested_instances=requested_instances, logger=logger)


def _build_deployment_config(
    raw: dict[str, Any], config_dir: Path, project_root: Path, requested_instances: Iterable[str] | None, logger: Any = None
) -> DeploymentConfig:
    """Turn the merged raw dict into a typed DeploymentConfig."""
    if logger is None:
        logger = _log

    def _path(value: str | None, default: str | None = None) -> Path | None:
        v = value if value is not None else default
        if v is None:
            return None
        p = Path(str(v))
        if not p.is_absolute():
            p = project_root / p
        try:
            return p.resolve()
        except OSError:
            return p.absolute()

    instance_discovery = str(raw.get("instance_discovery", "explicit"))
    if instance_discovery != "explicit":
        logger.error(f"config: instance_discovery must be 'explicit', got {instance_discovery!r}")
        raise ConfigError(f"instance_discovery must be 'explicit', got {instance_discovery!r}")
    sync_root = _path(raw.get("sync_root"), "./sync")
    modpack_dir = _path(raw.get("modpack_dir"), "./sync/downloads")
    mods_dir_toml = _path(raw.get("mods_dir"))
    output_filename = str(raw.get("output_filename", "minecraft_client_{date}.zip"))
    download_base_url = str(raw.get("download_base_url", ""))
    protect_file_raw = raw.get("protect_file")
    validate_output_filename(output_filename)
    validate_download_base_url(download_base_url)
    logger.debug(f"config: project_root={project_root} sync_root={sync_root} modpack_dir={modpack_dir} mods_dir_toml={mods_dir_toml}")
    docker_raw = raw.get("docker") or {}
    if not isinstance(docker_raw, dict):
        logger.error("config: [docker] is not a table")
        raise ConfigError("[docker] must be a table")
    compose_file = _path(docker_raw.get("compose_file"), "./docker-compose.yml")
    docker = DockerConfig(
        compose_file=compose_file,
        restart_wait_seconds=int(docker_raw.get("restart_wait_seconds", 300)),
        in_game_notice_required=bool(docker_raw.get("in_game_notice_required", True)),
        health_timeout_seconds=int(docker_raw.get("health_timeout_seconds", 600)),
        health_poll_seconds=int(docker_raw.get("health_poll_seconds", 2)),
        preflight_restarting_wait_seconds=int(docker_raw.get("preflight_restarting_wait_seconds", 30)),
        cancel_notice_ready_timeout_seconds=int(docker_raw.get("cancel_notice_ready_timeout_seconds", 30)),
        restart_notice_template=str(docker_raw.get("restart_notice_template", DEFAULT_RESTART_NOTICE_TEMPLATE)),
        restart_cancel_notice_template=str(docker_raw.get("restart_cancel_notice_template", DEFAULT_RESTART_CANCEL_NOTICE_TEMPLATE)),
        rcon_host=str(docker_raw["rcon_host"]) if docker_raw.get("rcon_host") else None,
    )
    _validate_docker_timings(docker)
    validate_in_game_templates(docker)
    logger.debug(f"config: [docker] resolved; compose_file={compose_file} rcon_host={docker.rcon_host}")
    compose_result = load_compose(compose_file)
    if not compose_result.ok:
        logger.warning(f"config: compose unavailable ({compose_result.error}); preflight will apply §3.5")
    instances_raw = raw.get("instances") or {}
    if not isinstance(instances_raw, dict):
        logger.error("config: [instances] is not a table")
        raise ConfigError("[instances] must be a table")
    instances: dict[str, InstanceConfig] = {}
    for name, body in instances_raw.items():
        name = str(name)
        if not isinstance(body, dict):
            logger.error(f"config: [instances.{name}] is not a table")
            raise ConfigError(f"[instances.{name}] must be a table")
        container = body.get("container")
        if not container:
            logger.error(f"config: [instances.{name}].container is required")
            raise ConfigError(f"[instances.{name}].container is required")
        inst = InstanceConfig(
            name=name, container=str(container), config_mode=str(body.get("config_mode", "merge")), kubejs_mode=str(body.get("kubejs_mode", "delete"))
        )
        if inst.config_mode not in ("merge", "delete"):
            logger.error(f"config: [instances.{name}].config_mode must be 'merge' or 'delete', got {inst.config_mode!r}")
            raise ConfigError(f"[instances.{name}].config_mode must be 'merge' or 'delete'")
        if inst.kubejs_mode != "delete":
            logger.error(f"config: [instances.{name}].kubejs_mode must be 'delete', got {inst.kubejs_mode!r}")
            raise ConfigError(f"[instances.{name}].kubejs_mode must be 'delete'")
        if compose_result.ok:
            compose = compose_result.file
            assert compose is not None
            try:
                svc = match_service_by_container(compose, inst.container)
            except ServiceMatchError as exc:
                inst.service_match_error = str(exc)
                logger.warning(f"config: [instances.{name}] compose match failed: {exc}")
            else:
                inst.service = svc
                root = derive_instance_root(svc)
                if root is not None:
                    inst.instance_root = _resolve_compose_path(root, compose.base_dir)
                    inst.config_path = inst.instance_root / "config"
                    inst.kubejs_path = inst.instance_root / "kubejs"
                    inst.server_properties_path = inst.instance_root / "server.properties"
                else:
                    logger.warning(f"config: [instances.{name}] no /data bind found on service {svc.name!r}; instance_root unresolved")
                if svc.stop_grace_period:
                    inst.stop_grace_period_raw = svc.stop_grace_period
                    try:
                        inst.stop_grace_seconds = parse_go_duration(svc.stop_grace_period)
                    except ValueError as exc:
                        inst.stop_grace_parse_error = str(exc)
                inst.stop_signal = svc.stop_signal
        instances[name] = inst
    logger.info(f"config: resolved {len(instances)} instance(s): {sorted(instances)}")
    partition, partition_unknown = resolve_partition(instances, requested_instances)
    rp_raw = raw.get("resource_pack") or {}
    if not isinstance(rp_raw, dict):
        logger.error("config: [resource_pack] is not a table")
        raise ConfigError("[resource_pack] must be a table")
    resource_packs: dict[str, ResourcePackConfig] = {}
    for name, body in rp_raw.items():
        name = str(name)
        if not isinstance(body, dict):
            logger.error(f"config: [resource_pack.{name}] is not a table")
            raise ConfigError(f"[resource_pack.{name}] must be a table")
        if "filename" not in body:
            logger.error(f"config: [resource_pack.{name}].filename is required")
            raise ConfigError(f"[resource_pack.{name}].filename is required")
        if "required" not in body:
            logger.error(f"config: [resource_pack.{name}].required is required")
            raise ConfigError(f"[resource_pack.{name}].required is required")
        resource_packs[name] = ResourcePackConfig(filename=str(body["filename"]), required=bool(body["required"]), prompt=str(body.get("prompt", "")))
        if name not in instances:
            logger.error(f"config: [resource_pack.{name}] has no matching [instances.{name}]")
            raise ConfigError(f"[resource_pack.{name}] has no matching [instances.{name}]")
    logger.debug(f"config: resolved {len(resource_packs)} resource pack(s): {sorted(resource_packs)}")
    sync_mapping_raw = raw.get("sync_mapping") or {}
    if not isinstance(sync_mapping_raw, dict):
        logger.error("config: [sync_mapping] is not a table")
        raise ConfigError("[sync_mapping] must be a table")
    sync_mapping = dict(sync_mapping_raw)
    restart_policy_raw = raw.get("restart_policy") or {}
    if not isinstance(restart_policy_raw, dict):
        logger.error("config: [restart_policy] is not a table")
        raise ConfigError("[restart_policy] must be a table")
    restart_policy = {str(k): str(v) for k, v in restart_policy_raw.items()}
    discord_raw = raw.get("discord") or {}
    if not isinstance(discord_raw, dict):
        logger.error("config: [discord] is not a table")
        raise ConfigError("[discord] must be a table")
    tags_raw = discord_raw.get("tags") or {}
    if not isinstance(tags_raw, dict):
        logger.error("config: [discord.tags] is not a table")
        raise ConfigError("[discord.tags] must be a table")
    messages_raw = discord_raw.get("messages") or {}
    if not isinstance(messages_raw, dict):
        logger.error("config: [discord.messages] is not a table")
        raise ConfigError("[discord.messages] must be a table")

    def _msg_template(name: str) -> str | None:
        block = messages_raw.get(name)
        if block is None:
            return None
        if not isinstance(block, dict):
            logger.error(f"config: [discord.messages.{name}] is not a table")
            raise ConfigError(f"[discord.messages.{name}] must be a table")
        t = block.get("template")
        return str(t) if t is not None else None

    discord = DiscordConfig(
        player_roles=[str(x) for x in tags_raw.get("player_roles") or []],
        operator_roles=[str(x) for x in tags_raw.get("operator_roles") or []],
        live_template=_msg_template("live"),
        online_template=_msg_template("online"),
        failure_template=_msg_template("failure"),
        diagnostic_template=_msg_template("diagnostic"),
    )
    logger.debug(
        f"config: [discord] resolved; player_roles={len(discord.player_roles)} operator_roles={len(discord.operator_roles)} "
        f"templates live={'y' if discord.live_template else 'n'} online={'y' if discord.online_template else 'n'} "
        f"failure={'y' if discord.failure_template else 'n'} diagnostic={'y' if discord.diagnostic_template else 'n'}"
    )
    webhook_url_raw = raw.get("webhook_url")
    webhook_url = str(webhook_url_raw) if webhook_url_raw else None
    www_dir_toml = _path(raw.get("www_dir"))
    www_dir: Path | None
    www_dir_error: str | None
    www_dir_candidates: list[Path]
    if www_dir_toml is not None:
        www_dir = www_dir_toml
        www_dir_error = None
        www_dir_candidates = []
        logger.debug(f"config: www_dir set from TOML -> {www_dir}")
        if compose_result.ok:
            compose = compose_result.file
            assert compose is not None
            derived = derive_www_dir(compose)
            if derived.path is not None:
                derived_resolved = _resolve_compose_path(derived.path, compose.base_dir)
                if derived_resolved != www_dir:
                    logger.warning(f"www_dir: TOML={www_dir} compose={derived_resolved} (TOML wins)")
                else:
                    logger.debug(f"config: www_dir TOML and compose agree on {www_dir}")
    elif compose_result.ok:
        compose = compose_result.file
        assert compose is not None
        derived = derive_www_dir(compose)
        www_dir_error = derived.error
        www_dir_candidates = [_resolve_compose_path(c, compose.base_dir) for c in derived.candidates]
        www_dir = _resolve_compose_path(derived.path, compose.base_dir) if derived.path is not None else None
        if www_dir is not None:
            logger.debug(f"config: www_dir derived from compose -> {www_dir}")
        else:
            logger.warning(f"config: www_dir could not be derived from compose: {www_dir_error}")
    else:
        www_dir = None
        www_dir_error = compose_result.error or "www_dir is not set in TOML and no compose file is available"
        www_dir_candidates = []
        logger.warning(f"config: www_dir unresolved: {www_dir_error}")
    protect_file = _path(protect_file_raw) if protect_file_raw else None
    logger.debug(f"config: protect_file={protect_file} webhook_url={'set' if webhook_url else 'unset'}")
    result = DeploymentConfig(
        project_root=project_root,
        config_dir=config_dir,
        sync_root=sync_root,
        modpack_dir=modpack_dir,
        www_dir=www_dir,
        www_dir_error=www_dir_error,
        www_dir_candidates=www_dir_candidates,
        output_filename=output_filename,
        download_base_url=download_base_url,
        protect_file=protect_file,
        sync_mapping=sync_mapping,
        restart_policy=restart_policy,
        instances=instances,
        partition=partition,
        partition_unknown=partition_unknown,
        partition_requested=requested_instances is not None,
        requested_instances=(set(requested_instances) if requested_instances is not None else None),
        resource_packs=resource_packs,
        docker=docker,
        discord=discord,
        webhook_url=webhook_url,
        compose=compose_result,
        mods_dir_toml=mods_dir_toml,
    )
    logger.info(f"config: built; partition={partition} compose_ok={compose_result.ok} www_dir={www_dir}")
    return result


__all__ = [
    "BindMount",
    "ComposeFile",
    "ComposeLoadResult",
    "ComposeService",
    "DeploymentConfig",
    "DiscordConfig",
    "DockerConfig",
    "InstanceConfig",
    "ResourcePackConfig",
    "ServiceMatchError",
    "WwwDirResult",
    "derive_instance_root",
    "derive_mods_dir",
    "derive_www_dir",
    "load_compose",
    "load_deployment_config",
    "match_service_by_container",
    "parse_go_duration",
    "resolve_compose_path",
    "resolve_partition",
    "validate_download_base_url",
    "validate_in_game_templates",
    "validate_output_filename",
]
