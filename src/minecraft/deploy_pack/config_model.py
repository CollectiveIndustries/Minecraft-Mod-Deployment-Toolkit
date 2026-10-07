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

Structure
---------

The module is organised into eleven sections with explicit banner
comments. Each section corresponds to one concern and its helpers are
only called from within that section or from the public API.

    §1   Data model
    §2   Primitive validators and parsers       (§3.2, §3.9, §5.11, §7.1, §7.5)
    §3   Compose parsing                        (§3.2, §3.5)
    §4   Service matching and path resolution   (§3.6, §3.15)
    §5   Instance root derivation               (§3.18)
    §6   www_dir derivation                     (§3.19)
    §7   Partition resolution                   (§2.9)
    §8   Config file loading                    (§3.1, §3.12)
    §9   CLI override merge                     (§3.1)
    §10  Schema section builders                (§3.9)
    §11  Top-level assembly                     (§3.1, §3.15, §3.19)

Aggressive decomposition: every public entry point is a thin
orchestrator delegating to single-purpose helpers. Multi-step
validators (filename, docker timings, in-game templates) are driven
by small tables of `(check, message)` pairs; per-item compose parsers
short-form / long-form paths are separated so a failure inside one
form can be traced without reading the other.

Logging
-------

Module logger is ``minecraft.deploy_pack.config_model``. Config load
logs at INFO on entry and on success with the resolved partition and
path roots; each resolution step logs its inputs and outcomes at
DEBUG. Degraded-but-continuing conditions (a broken compose file, an
ambiguous www_dir, a TOML-vs-compose disagreement on www_dir) log at
WARN. Validation failures log at ERROR immediately before the
``ConfigError``/``ValueError`` raise, so the specific diagnostic lands
in the sink regardless of how the caller handles the exception.
``load_deployment_config`` accepts an optional ``logger=`` override;
downstream helpers use the module logger.
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


# ===========================================================================
# §1  Data model
# ===========================================================================


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


# ===========================================================================
# §2  Primitive validators and parsers
# ===========================================================================


# ----- §3.2  Go-duration parsing --------------------------------------


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_DURATION_TOKEN = re.compile("(\\d+)([smh])")


def _raise_duration_error(value: str, message: str) -> None:
    """Log at ERROR and raise ValueError with a consistent shape."""
    _log.error(f"parse_go_duration: {message} {value!r}")
    raise ValueError(f"invalid duration: {value!r}")


def _consume_duration_tokens(s: str) -> int:
    """Walk ``s`` left-to-right, summing every <int><unit> token.

    Raises ValueError if the string has any non-token content at any
    position: leading whitespace is already stripped by the caller, and
    the walk fails on any character not consumed by a token.
    """
    total = 0
    pos = 0
    for m in _DURATION_TOKEN.finditer(s):
        if m.start() != pos:
            _raise_duration_error(s, "unexpected token at offset")
        total += int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        pos = m.end()
    if pos != len(s):
        _raise_duration_error(s, "trailing data at offset")
    return total


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
    total = _consume_duration_tokens(s)
    _log.debug(f"parse_go_duration: {value!r} -> {total}s")
    return total


# ----- §5.11  In-game templates ---------------------------------------


_PLACEHOLDER_RE = re.compile("\\{([^{}]*)\\}")


def _validate_restart_notice_template(template: str) -> None:
    """§5.11: non-empty; only ``{time}`` permitted."""
    if not template:
        _log.error("validate_in_game_templates: [docker].restart_notice_template is empty")
        raise ConfigError("[docker].restart_notice_template must be non-empty")
    for m in _PLACEHOLDER_RE.finditer(template):
        if m.group(1) != "time":
            _log.error(f"validate_in_game_templates: [docker].restart_notice_template has unknown placeholder {{{m.group(1)}}}")
            raise ConfigError(f"[docker].restart_notice_template: unknown placeholder {{{m.group(1)}}} (only {{time}} is permitted)")


def _validate_restart_cancel_notice_template(template: str) -> None:
    """§5.11: non-empty; no placeholders at all."""
    if not template:
        _log.error("validate_in_game_templates: [docker].restart_cancel_notice_template is empty")
        raise ConfigError("[docker].restart_cancel_notice_template must be non-empty")
    if _PLACEHOLDER_RE.search(template):
        _log.error(f"validate_in_game_templates: [docker].restart_cancel_notice_template contains a placeholder ({template!r})")
        raise ConfigError("[docker].restart_cancel_notice_template must not contain placeholders")


def validate_in_game_templates(docker: DockerConfig) -> None:
    """Validate the two in-game templates at config load time (§5.11).

    In-game templates are validated unconditionally, regardless of
    --notify. Discord templates are validated by notifications.py.
    """
    _validate_restart_notice_template(docker.restart_notice_template)
    _validate_restart_cancel_notice_template(docker.restart_cancel_notice_template)
    _log.debug("validate_in_game_templates: both in-game templates valid")


# ----- §7.1  Output filename ------------------------------------------


def _filename_check_messages(name: str) -> list[tuple[bool, str]]:
    """Return ``(violated, message)`` pairs in the order checks must run.

    Centralising the checks keeps :func:`validate_output_filename` a
    two-line loop; the first violated check raises.
    """
    return [
        (not name, "output_filename must be non-empty"),
        ("\x00" in name, "output_filename must not contain NUL"),
        ("/" in name or "\\" in name, "output_filename must not contain path separators"),
        (name in (".", ".."), "output_filename must not be '.' or '..'"),
        (not name.endswith(".zip"), "output_filename must end in '.zip'"),
    ]


def _first_violation(name: str) -> str | None:
    """Return the message for the first violated check, or None."""
    for violated, message in _filename_check_messages(name):
        if violated:
            return message
    return None


def validate_output_filename(name: str) -> None:
    """Validates an output filename for safety and required format."""
    message = _first_violation(name)
    if message is not None:
        _log.error(f"validate_output_filename: {message} ({name!r})")
        raise ConfigError(message)
    _log.debug(f"validate_output_filename: {name!r} OK")


# ----- §7.5  download_base_url ----------------------------------------


def validate_download_base_url(url: str) -> None:
    """Validates the download base URL.

    Raises:
        ConfigError: If the URL is empty.
    """
    if not url:
        _log.error("validate_download_base_url: download_base_url is empty")
        raise ConfigError("download_base_url must be non-empty")
    _log.debug(f"validate_download_base_url: {url!r} OK")


# ----- §3.9  Docker timing keys ---------------------------------------


_NON_NEGATIVE_KEYS = (
    "restart_wait_seconds",
    "preflight_restarting_wait_seconds",
    "cancel_notice_ready_timeout_seconds",
)
_POSITIVE_KEYS = ("health_timeout_seconds", "health_poll_seconds")


def _check_non_negative(key: str, value: int) -> None:
    """Raise if ``value < 0`` for a non-negative timing key."""
    if value < 0:
        _log.error(f"_validate_docker_timings: [docker].{key} must be >= 0, got {value}")
        raise ConfigError(f"[docker].{key} must be >= 0, got {value}")


def _check_positive(key: str, value: int) -> None:
    """Raise if ``value <= 0`` for a positive timing key."""
    if value <= 0:
        _log.error(f"_validate_docker_timings: [docker].{key} must be > 0, got {value}")
        raise ConfigError(f"[docker].{key} must be > 0, got {value}")


def _validate_docker_timings(docker: DockerConfig) -> None:
    for key in _NON_NEGATIVE_KEYS:
        _check_non_negative(key, getattr(docker, key))
    for key in _POSITIVE_KEYS:
        _check_positive(key, getattr(docker, key))
    _log.debug("_validate_docker_timings: all docker timing keys valid")


# ===========================================================================
# §3  Compose parsing
# ===========================================================================


def _parse_short_form_bind(spec: str) -> BindMount | None:
    """Parse ``host:target`` string form. Returns None for named volumes / malformed."""
    parts = spec.split(":")
    if len(parts) < 2:
        return None
    host_raw, target = parts[0], parts[1]
    if not (host_raw.startswith("/") or host_raw.startswith("./") or host_raw.startswith("../") or host_raw.startswith("~")):
        return None
    return BindMount(host_source=Path(host_raw), container_target=target)


def _parse_long_form_bind(spec: dict) -> BindMount | None:
    """Parse the ``{type: bind, source, target}`` mapping form. None for volumes."""
    if spec.get("type") != "bind":
        return None
    src = spec.get("source")
    dst = spec.get("target")
    if not src or not dst:
        return None
    return BindMount(host_source=Path(str(src)), container_target=str(dst))


def _parse_binds(volumes: Any) -> list[BindMount]:
    """Return bind mounts only. Named and anonymous volumes are ignored."""
    result: list[BindMount] = []
    if not isinstance(volumes, list):
        _log.debug("_parse_binds: volumes is not a list; no binds parsed")
        return result
    for vol in volumes:
        if isinstance(vol, str):
            bind = _parse_short_form_bind(vol)
        elif isinstance(vol, dict):
            bind = _parse_long_form_bind(vol)
        else:
            bind = None
        if bind is not None:
            result.append(bind)
    _log.debug(f"_parse_binds: parsed {len(result)} bind mount(s)")
    return result


def _env_from_dict(env: dict) -> dict[str, str]:
    """Normalise a dict-form ``environment`` block."""
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _env_from_list(env: list) -> dict[str, str]:
    """Normalise a list-form ``environment`` block; bare items are ignored."""
    out: dict[str, str] = {}
    for item in env:
        if not isinstance(item, str):
            continue
        if "=" in item:
            k, _, v = item.partition("=")
            out[k] = v
    return out


def _parse_environment(env: Any) -> dict[str, str]:
    if isinstance(env, dict):
        return _env_from_dict(env)
    if isinstance(env, list):
        return _env_from_list(env)
    return {}


def _resolve_env_file_path(item: Any, base_dir: Path) -> Path | None:
    """Resolve one ``env_file`` entry; None for non-string items."""
    if not isinstance(item, str):
        return None
    p = Path(item)
    if not p.is_absolute():
        p = base_dir / p
    return p


def _parse_env_files(value: Any, base_dir: Path) -> list[Path]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[Path] = []
    for item in items:
        resolved = _resolve_env_file_path(item, base_dir)
        if resolved is not None:
            result.append(resolved)
    _log.debug(f"_parse_env_files: resolved {len(result)} env_file(s) under {base_dir}")
    return result


def _parse_one_secret(item: Any) -> str | None:
    """Extract the secret source name from a short- or long-form entry."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        src = item.get("source")
        return str(src) if src else None
    return None


def _parse_secrets(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        name = _parse_one_secret(item)
        if name is not None:
            result.append(name)
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


def _read_compose_text(path: Path, logger: Any) -> tuple[str | None, str | None]:
    """Read the compose file as UTF-8 text. Returns (text, error_message)."""
    logger.debug(f"load_compose: reading {path}")
    if not path.is_file():
        logger.warning(f"load_compose: compose file not found: {path}")
        return (None, f"Compose file not found: {path}")
    try:
        return (path.read_text(encoding="utf-8"), None)
    except OSError as exc:
        logger.warning(f"load_compose: could not read {path}: {exc}")
        return (None, f"Could not read compose file {path}: {exc}")


def _parse_compose_yaml(text: str, path: Path, logger: Any) -> tuple[dict | None, str | None]:
    """Parse YAML into a top-level mapping. Returns (raw_dict, error_message)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(f"load_compose: could not parse {path}: {exc}")
        return (None, f"Could not parse compose file {path}: {exc}")
    if raw is None:
        logger.warning(f"load_compose: compose file is empty: {path}")
        return (None, f"Compose file is empty: {path}")
    if not isinstance(raw, dict):
        logger.warning(f"load_compose: {path}: top-level is not a mapping")
        return (None, f"Compose file {path}: top-level must be a mapping")
    return (raw, None)


def _parse_compose_services(raw: dict, base_dir: Path, path: Path, logger: Any) -> tuple[dict[str, ComposeService] | None, str | None]:
    """Build the services mapping. Returns (services, error_message)."""
    services_raw = raw.get("services") or {}
    if not isinstance(services_raw, dict):
        logger.warning(f"load_compose: {path}: 'services' is not a mapping")
        return (None, f"Compose file {path}: 'services' must be a mapping")
    services: dict[str, ComposeService] = {}
    for svc_name, svc_raw in services_raw.items():
        if not isinstance(svc_raw, dict):
            continue
        services[str(svc_name)] = _parse_service(str(svc_name), svc_raw, base_dir)
    return (services, None)


def _resolve_secret_file_path(file_value: Any, base_dir: Path) -> Path | None:
    """Resolve a ``secrets.<name>.file`` value against the compose base dir."""
    if not file_value:
        return None
    p = Path(str(file_value))
    if not p.is_absolute():
        p = base_dir / p
    return p


def _parse_compose_secrets(raw: dict, base_dir: Path) -> dict[str, Path]:
    """Build the secret-files mapping from the top-level 'secrets' block."""
    secret_files: dict[str, Path] = {}
    secrets_raw = raw.get("secrets") or {}
    if not isinstance(secrets_raw, dict):
        return secret_files
    for sec_name, sec_raw in secrets_raw.items():
        if not isinstance(sec_raw, dict):
            continue
        resolved = _resolve_secret_file_path(sec_raw.get("file"), base_dir)
        if resolved is not None:
            secret_files[str(sec_name)] = resolved
    return secret_files


def load_compose(path: Path) -> ComposeLoadResult:
    """Load a compose file. Never raises (§3.5).

    Returns a ComposeLoadResult. On any failure, ``file`` is None and
    ``error`` carries a human-readable diagnostic. Preflight owns the
    per-scope decision table (§3.5): a broken compose is fatal for
    --server and (conditionally) --resource-pack, but only a warning for
    --client unless www_dir becomes undeterminable.
    """
    text, err = _read_compose_text(path, _log)
    if err is not None:
        return ComposeLoadResult(None, err)
    assert text is not None

    raw, err = _parse_compose_yaml(text, path, _log)
    if err is not None:
        return ComposeLoadResult(None, err)
    assert raw is not None

    base_dir = path.parent
    services, err = _parse_compose_services(raw, base_dir, path, _log)
    if err is not None:
        return ComposeLoadResult(None, err)
    assert services is not None

    secret_files = _parse_compose_secrets(raw, base_dir)
    _log.info(f"load_compose: loaded {len(services)} service(s) and {len(secret_files)} secret file(s) from {path}")
    return ComposeLoadResult(ComposeFile(path=path, base_dir=base_dir, services=services, secret_files=secret_files), None)


# ===========================================================================
# §4  Service matching and path resolution
# ===========================================================================


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


# ===========================================================================
# §5  Instance root derivation (§3.18)
# ===========================================================================


def _find_canonical_data_bind(svc: ComposeService) -> Path | None:
    """Return the host source of an exact ``/data`` bind, or None."""
    for bind in svc.binds:
        if bind.container_target == "/data":
            _log.debug(f"derive_instance_root: [{svc.name}] canonical /data bind -> {bind.host_source}")
            return bind.host_source
    return None


def _candidate_root_from_bind(bind: BindMount) -> str | None:
    """Return the implied root for one ``/data/<subpath>`` bind, or None.

    ``/data/mods`` and any bind not under ``/data/`` are excluded: mods
    is a shared bind derived separately by :func:`derive_mods_dir`.
    """
    target = bind.container_target
    if not target.startswith("/data/") or target == "/data/mods":
        return None
    subpath = target[len("/data/") :]
    host = str(bind.host_source)
    for sep in ("/", "\\"):
        suffix = sep + subpath
        if host.endswith(suffix):
            return host[: -len(suffix)]
    return None


def _collect_root_candidates(svc: ComposeService) -> dict[str, int]:
    """Count how many binds imply each candidate root from ``/data/*`` subpaths."""
    implied_counts: dict[str, int] = {}
    for bind in svc.binds:
        root = _candidate_root_from_bind(bind)
        if root is None:
            continue
        implied_counts[root] = implied_counts.get(root, 0) + 1
    return implied_counts


def _rank_root_candidates(implied_counts: dict[str, int]) -> list[tuple[str, int]]:
    """Return the candidates ranked by count desc, then path asc."""
    return sorted(implied_counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _pick_root_candidate(implied_counts: dict[str, int], svc: ComposeService) -> Path | None:
    """Pick the most-agreed root; a tie is genuinely ambiguous and returns None."""
    if not implied_counts:
        _log.debug(f"derive_instance_root: [{svc.name}] no /data or /data/* bind found")
        return None
    if len(implied_counts) == 1:
        root = next(iter(implied_counts))
        _log.debug(f"derive_instance_root: [{svc.name}] single implied root -> {root}")
        return Path(root)
    ranked = _rank_root_candidates(implied_counts)
    if ranked[0][1] == ranked[1][1]:
        _log.warning(f"derive_instance_root: [{svc.name}] ambiguous /data/* binds (tie): " + ", ".join(f"{r} x{n}" for r, n in ranked))
        return None
    _log.debug(f"derive_instance_root: [{svc.name}] most-agreed root -> {ranked[0][0]} ({ranked[0][1]} bind(s))")
    return Path(ranked[0][0])


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
    canonical = _find_canonical_data_bind(svc)
    if canonical is not None:
        return canonical
    return _pick_root_candidate(_collect_root_candidates(svc), svc)


def derive_mods_dir(svc: ComposeService) -> Path | None:
    """Derives the mods directory from a compose service."""
    for bind in svc.binds:
        if bind.container_target == "/data/mods":
            _log.debug(f"derive_mods_dir: [{svc.name}] -> {bind.host_source}")
            return bind.host_source
    _log.debug(f"derive_mods_dir: [{svc.name}] no /data/mods bind found")
    return None


# ===========================================================================
# §6  www_dir derivation (§3.19)
# ===========================================================================


def _collect_nginx_candidates(compose: ComposeFile) -> list[Path]:
    """Collect host sources of binds whose target starts with ``/usr/share/nginx/``."""
    candidates: list[Path] = []
    for svc in compose.services.values():
        for bind in svc.binds:
            if bind.container_target.startswith("/usr/share/nginx/"):
                candidates.append(bind.host_source)
    return candidates


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    """Deduplicate paths, preserving first-seen order."""
    seen: set[str] = set()
    unique: list[Path] = []
    for c in paths:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def derive_www_dir(compose: ComposeFile) -> WwwDirResult:
    """Find the unique bind source with a target starting /usr/share/nginx/.

    Never raises. Exact target '/usr/share/nginx' (no subpath) is
    ignored, per §3.19. On zero or multiple candidates, ``path`` is
    None and ``error`` explains why.
    """
    unique = _dedupe_paths(_collect_nginx_candidates(compose))
    if not unique:
        _log.warning("derive_www_dir: no bind mount whose target starts with /usr/share/nginx/")
        return WwwDirResult(
            None,
            "www_dir could not be derived: no bind mount whose target starts with /usr/share/nginx/",
            [],
        )
    if len(unique) > 1:
        _log.warning(f"derive_www_dir: {len(unique)} candidate mount(s) found; ambiguous: " + ", ".join(str(p) for p in unique))
        return WwwDirResult(None, f"www_dir could not be derived: {len(unique)} candidate mounts", unique)
    _log.debug(f"derive_www_dir: -> {unique[0]}")
    return WwwDirResult(unique[0], None, [])


# ===========================================================================
# §7  Partition resolution (§2.9)
# ===========================================================================


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


# ===========================================================================
# §8  Config file loading (§3.1, §3.12)
# ===========================================================================


def _find_config_file(config_dir: Path) -> Path | None:
    for ext in (".toml", ".yaml", ".yml"):
        candidate = config_dir / f"deploy_pack{ext}"
        if candidate.is_file():
            _log.debug(f"_find_config_file: using {candidate}")
            return candidate
    _log.debug(f"_find_config_file: no deploy_pack.{{toml,yaml,yml}} under {config_dir}")
    return None


def _configure_sources(mgr: ConfigManager, env_file: Path, toml_path: Path | None) -> None:
    """Register the file sources on a ConfigManager in priority order (§3.1).

    Lower-priority sources are registered first; ConfigCore's merge
    rules give later-registered sources higher priority.
    """
    if env_file.is_file():
        _log.debug(f"_load_config_files: loading .env {env_file}")
        mgr.file(env_file, format="env")
    if toml_path is not None:
        _log.debug(f"_load_config_files: loading {toml_path}")
        mgr.file(toml_path)


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
    _configure_sources(mgr, env_file, toml_path)
    config = mgr.load()
    result = dict(config.as_dict()) if hasattr(config, "as_dict") else dict(config)
    _log.debug(f"_load_config_files: loaded {len(result)} top-level key(s)")
    return result


# ===========================================================================
# §9  CLI override merge (§3.1)
# ===========================================================================


def _split_cli_token(token: str) -> tuple[str, str | None]:
    """Split ``--key=value`` into ('key', 'value'); ``--key`` into ('key', None)."""
    body = token[2:]
    if "=" in body:
        key, _, value = body.partition("=")
        return (key, value)
    return (body, None)


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
        key, value = _split_cli_token(arg)
        if value is None and i + 1 < len(args) and not args[i + 1].startswith("--"):
            value = args[i + 1]
            i += 1
        key = key.replace("-", "_")
        result[key] = True if value is None else value
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


def _merge_cli_overrides(raw: dict[str, Any], cli_remaining: list[str] | None, logger: Any) -> None:
    """Parse CLI overrides and merge them on top of the file sources (§3.1)."""
    if not cli_remaining:
        return
    cli_overlay = _parse_cli_overrides(cli_remaining)
    _deep_merge(raw, cli_overlay)
    logger.debug(f"config load: merged {len(cli_overlay)} CLI override(s) over file sources")


# ===========================================================================
# §10  Schema section builders (§3.9)
# ===========================================================================


def _resolve_project_path(value: Any, project_root: Path, default: Any = None) -> Path | None:
    """Resolve a TOML/env path value against the project root (§3.15).

    ``value`` and ``default`` may both be None; if so the result is None.
    If ``value`` is not None it takes precedence over ``default``.
    Relative paths resolve against ``project_root``; absolute paths are
    returned as-is after realpath.
    """
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


def _require_table(raw: dict, key: str, *, logger: Any) -> dict:
    """Return the ``[key]`` subtable or raise; treats missing as empty dict."""
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        logger.error(f"config: [{key}] is not a table")
        raise ConfigError(f"[{key}] must be a table")
    return value


def _validate_instance_discovery(raw: dict[str, Any], logger: Any) -> None:
    """§3.6: only ``'explicit'`` is supported."""
    discovery = str(raw.get("instance_discovery", "explicit"))
    if discovery != "explicit":
        logger.error(f"config: instance_discovery must be 'explicit', got {discovery!r}")
        raise ConfigError(f"instance_discovery must be 'explicit', got {discovery!r}")


def _build_docker_config(raw: dict[str, Any], project_root: Path, logger: Any) -> DockerConfig:
    """Build and validate the ``[docker]`` section (§3.9)."""
    docker_raw = _require_table(raw, "docker", logger=logger)
    compose_file = _resolve_project_path(docker_raw.get("compose_file"), project_root, "./docker-compose.yml")
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
    return docker


def _load_compose_or_warn(path: Path, logger: Any) -> ComposeLoadResult:
    """Load compose; warn when unavailable but never raise (§3.5)."""
    result = load_compose(path)
    if not result.ok:
        logger.warning(f"config: compose unavailable ({result.error}); preflight will apply §3.5")
    return result


def _apply_matched_service(inst: InstanceConfig, svc: ComposeService, compose: ComposeFile, logger: Any) -> None:
    """Populate the compose-derived root paths for an instance."""
    inst.service = svc
    root = derive_instance_root(svc)
    if root is not None:
        inst.instance_root = _resolve_compose_path(root, compose.base_dir)
        inst.config_path = inst.instance_root / "config"
        inst.kubejs_path = inst.instance_root / "kubejs"
        inst.server_properties_path = inst.instance_root / "server.properties"
    else:
        logger.warning(f"config: [instances.{inst.name}] no /data bind found on service {svc.name!r}; instance_root unresolved")


def _apply_stop_grace(inst: InstanceConfig, svc: ComposeService) -> None:
    """Populate ``stop_grace_*`` fields; a parse failure is recorded, not raised."""
    if svc.stop_grace_period:
        inst.stop_grace_period_raw = svc.stop_grace_period
        try:
            inst.stop_grace_seconds = parse_go_duration(svc.stop_grace_period)
        except ValueError as exc:
            inst.stop_grace_parse_error = str(exc)
    inst.stop_signal = svc.stop_signal


def _enrich_instance_from_compose(inst: InstanceConfig, compose: ComposeFile, logger: Any) -> None:
    """Match the container against compose and populate derived fields.

    On a service-match failure, records the diagnostic on
    ``inst.service_match_error`` and returns without touching the
    instance root. The remaining fields (root, stop grace) are derived
    only when a service was found.
    """
    try:
        svc = match_service_by_container(compose, inst.container)
    except ServiceMatchError as exc:
        inst.service_match_error = str(exc)
        logger.warning(f"config: [instances.{inst.name}] compose match failed: {exc}")
        return
    _apply_matched_service(inst, svc, compose, logger)
    _apply_stop_grace(inst, svc)


def _validate_instance_modes(name: str, config_mode: str, kubejs_mode: str, logger: Any) -> None:
    """§3.9: ``config_mode`` in {merge, delete}; ``kubejs_mode`` must be 'delete'."""
    if config_mode not in ("merge", "delete"):
        logger.error(f"config: [instances.{name}].config_mode must be 'merge' or 'delete', got {config_mode!r}")
        raise ConfigError(f"[instances.{name}].config_mode must be 'merge' or 'delete'")
    if kubejs_mode != "delete":
        logger.error(f"config: [instances.{name}].kubejs_mode must be 'delete', got {kubejs_mode!r}")
        raise ConfigError(f"[instances.{name}].kubejs_mode must be 'delete'")


def _construct_instance(name: str, body: dict, logger: Any) -> InstanceConfig:
    """Validate field values and build a bare InstanceConfig (no compose enrichment)."""
    container = body.get("container")
    if not container:
        logger.error(f"config: [instances.{name}].container is required")
        raise ConfigError(f"[instances.{name}].container is required")
    inst = InstanceConfig(
        name=name,
        container=str(container),
        config_mode=str(body.get("config_mode", "merge")),
        kubejs_mode=str(body.get("kubejs_mode", "delete")),
    )
    _validate_instance_modes(name, inst.config_mode, inst.kubejs_mode, logger)
    return inst


def _build_instance_config(name: str, body: Any, compose_result: ComposeLoadResult, logger: Any) -> InstanceConfig:
    """Validate one ``[instances.X]`` block and enrich it from compose."""
    if not isinstance(body, dict):
        logger.error(f"config: [instances.{name}] is not a table")
        raise ConfigError(f"[instances.{name}] must be a table")
    inst = _construct_instance(name, body, logger)
    if compose_result.ok:
        compose = compose_result.file
        assert compose is not None
        _enrich_instance_from_compose(inst, compose, logger)
    return inst


def _build_instances(raw: dict[str, Any], compose_result: ComposeLoadResult, logger: Any) -> dict[str, InstanceConfig]:
    """Build the ``[instances]`` table (§3.2)."""
    instances_raw = _require_table(raw, "instances", logger=logger)
    instances: dict[str, InstanceConfig] = {}
    for name, body in instances_raw.items():
        name_str = str(name)
        instances[name_str] = _build_instance_config(name_str, body, compose_result, logger)
    logger.info(f"config: resolved {len(instances)} instance(s): {sorted(instances)}")
    return instances


def _validate_resource_pack_required_keys(name: str, body: dict, logger: Any) -> None:
    """§3.9: ``[resource_pack.X]`` requires ``filename`` and ``required``."""
    if "filename" not in body:
        logger.error(f"config: [resource_pack.{name}].filename is required")
        raise ConfigError(f"[resource_pack.{name}].filename is required")
    if "required" not in body:
        logger.error(f"config: [resource_pack.{name}].required is required")
        raise ConfigError(f"[resource_pack.{name}].required is required")


def _validate_resource_pack_orphan(name: str, instances: Mapping[str, InstanceConfig], logger: Any) -> None:
    """§7.9: every ``[resource_pack.X]`` must have a matching ``[instances.X]``."""
    if name not in instances:
        logger.error(f"config: [resource_pack.{name}] has no matching [instances.{name}]")
        raise ConfigError(f"[resource_pack.{name}] has no matching [instances.{name}]")


def _build_one_resource_pack(name: str, body: Any, instances: Mapping[str, InstanceConfig], logger: Any) -> ResourcePackConfig:
    """Validate one ``[resource_pack.X]`` block (§3.9) and check for an orphan."""
    if not isinstance(body, dict):
        logger.error(f"config: [resource_pack.{name}] is not a table")
        raise ConfigError(f"[resource_pack.{name}] must be a table")
    _validate_resource_pack_required_keys(name, body, logger)
    _validate_resource_pack_orphan(name, instances, logger)
    return ResourcePackConfig(
        filename=str(body["filename"]),
        required=bool(body["required"]),
        prompt=str(body.get("prompt", "")),
    )


def _build_resource_packs(raw: dict[str, Any], instances: Mapping[str, InstanceConfig], logger: Any) -> dict[str, ResourcePackConfig]:
    """Build the ``[resource_pack]`` table and enforce §7.9 orphan rules."""
    rp_raw = _require_table(raw, "resource_pack", logger=logger)
    resource_packs: dict[str, ResourcePackConfig] = {}
    for name, body in rp_raw.items():
        name_str = str(name)
        resource_packs[name_str] = _build_one_resource_pack(name_str, body, instances, logger)
    logger.debug(f"config: resolved {len(resource_packs)} resource pack(s): {sorted(resource_packs)}")
    return resource_packs


def _build_sync_mapping(raw: dict[str, Any], logger: Any) -> dict[str, Any]:
    """Return the ``[sync_mapping]`` table as a shallow copy."""
    return dict(_require_table(raw, "sync_mapping", logger=logger))


def _build_restart_policy(raw: dict[str, Any], logger: Any) -> dict[str, str]:
    """Return the ``[restart_policy]`` table with stringified keys and values."""
    restart_policy_raw = _require_table(raw, "restart_policy", logger=logger)
    return {str(k): str(v) for k, v in restart_policy_raw.items()}


def _extract_message_template(messages_raw: dict, name: str, logger: Any) -> str | None:
    """Extract a single ``[discord.messages.<name>].template`` value, or None."""
    block = messages_raw.get(name)
    if block is None:
        return None
    if not isinstance(block, dict):
        logger.error(f"config: [discord.messages.{name}] is not a table")
        raise ConfigError(f"[discord.messages.{name}] must be a table")
    t = block.get("template")
    return str(t) if t is not None else None


def _build_discord_config(raw: dict[str, Any], logger: Any) -> DiscordConfig:
    """Build the ``[discord]`` section: roles and message templates (§3.9)."""
    discord_raw = _require_table(raw, "discord", logger=logger)
    tags_raw = _require_table(discord_raw, "tags", logger=logger)
    messages_raw = _require_table(discord_raw, "messages", logger=logger)
    discord = DiscordConfig(
        player_roles=[str(x) for x in tags_raw.get("player_roles") or []],
        operator_roles=[str(x) for x in tags_raw.get("operator_roles") or []],
        live_template=_extract_message_template(messages_raw, "live", logger),
        online_template=_extract_message_template(messages_raw, "online", logger),
        failure_template=_extract_message_template(messages_raw, "failure", logger),
        diagnostic_template=_extract_message_template(messages_raw, "diagnostic", logger),
    )
    logger.debug(
        f"config: [discord] resolved; player_roles={len(discord.player_roles)} operator_roles={len(discord.operator_roles)} "
        f"templates live={'y' if discord.live_template else 'n'} online={'y' if discord.online_template else 'n'} "
        f"failure={'y' if discord.failure_template else 'n'} diagnostic={'y' if discord.diagnostic_template else 'n'}"
    )
    return discord


def _resolve_webhook_url(raw: dict[str, Any]) -> str | None:
    """Return the ``webhook_url`` value as a string, or None."""
    webhook_url_raw = raw.get("webhook_url")
    return str(webhook_url_raw) if webhook_url_raw else None


# ----- §3.4 / §3.19  www_dir resolution -------------------------------


def _resolve_www_dir_from_toml(www_dir_toml: Path, compose_result: ComposeLoadResult, logger: Any) -> tuple[Path | None, str | None, list[Path]]:
    """TOML-declared www_dir wins; compose disagreement is informational only."""
    logger.debug(f"config: www_dir set from TOML -> {www_dir_toml}")
    if compose_result.ok:
        compose = compose_result.file
        assert compose is not None
        derived = derive_www_dir(compose)
        if derived.path is not None:
            derived_resolved = _resolve_compose_path(derived.path, compose.base_dir)
            if derived_resolved != www_dir_toml:
                logger.warning(f"www_dir: TOML={www_dir_toml} compose={derived_resolved} (TOML wins)")
            else:
                logger.debug(f"config: www_dir TOML and compose agree on {www_dir_toml}")
    return (www_dir_toml, None, [])


def _resolve_www_dir_from_compose(compose_result: ComposeLoadResult, logger: Any) -> tuple[Path | None, str | None, list[Path]]:
    """Derive www_dir from the compose nginx bind (§3.19)."""
    compose = compose_result.file
    assert compose is not None
    derived = derive_www_dir(compose)
    error = derived.error
    candidates = [_resolve_compose_path(c, compose.base_dir) for c in derived.candidates]
    path = _resolve_compose_path(derived.path, compose.base_dir) if derived.path is not None else None
    if path is not None:
        logger.debug(f"config: www_dir derived from compose -> {path}")
    else:
        logger.warning(f"config: www_dir could not be derived from compose: {error}")
    return (path, error, candidates)


def _resolve_www_dir_unavailable(compose_result: ComposeLoadResult, logger: Any) -> tuple[Path | None, str | None, list[Path]]:
    """No TOML value and no compose file: www_dir cannot be determined."""
    error = compose_result.error or "www_dir is not set in TOML and no compose file is available"
    logger.warning(f"config: www_dir unresolved: {error}")
    return (None, error, [])


def _resolve_www_dir(raw: dict[str, Any], project_root: Path, compose_result: ComposeLoadResult, logger: Any) -> tuple[Path | None, str | None, list[Path]]:
    """Return (www_dir, error, candidates). TOML > compose > unavailable (§3.4)."""
    www_dir_toml = _resolve_project_path(raw.get("www_dir"), project_root)
    if www_dir_toml is not None:
        return _resolve_www_dir_from_toml(www_dir_toml, compose_result, logger)
    if compose_result.ok:
        return _resolve_www_dir_from_compose(compose_result, logger)
    return _resolve_www_dir_unavailable(compose_result, logger)


# ===========================================================================
# §11  Top-level assembly (§3.1, §3.15, §3.19)
# ===========================================================================


@dataclass
class _TopLevelScalars:
    """The four scalars read from the top level of the merged raw dict."""

    output_filename: str
    download_base_url: str
    protect_file_raw: Any
    mods_dir_toml: Path | None


def _resolve_top_level_paths(raw: dict[str, Any], project_root: Path, logger: Any) -> tuple[Path, Path, Path | None]:
    """Resolve ``sync_root``, ``modpack_dir``, ``mods_dir_toml`` (§3.15)."""
    sync_root = _resolve_project_path(raw.get("sync_root"), project_root, "./sync")
    modpack_dir = _resolve_project_path(raw.get("modpack_dir"), project_root, "./sync/downloads")
    mods_dir_toml = _resolve_project_path(raw.get("mods_dir"), project_root)
    logger.debug(f"config: project_root={project_root} sync_root={sync_root} modpack_dir={modpack_dir} mods_dir_toml={mods_dir_toml}")
    return (sync_root, modpack_dir, mods_dir_toml)


def _resolve_top_level_scalars(raw: dict[str, Any]) -> _TopLevelScalars:
    """Read and validate the four top-level scalars (§7.1, §7.5)."""
    output_filename = str(raw.get("output_filename", "minecraft_client_{date}.zip"))
    download_base_url = str(raw.get("download_base_url", ""))
    validate_output_filename(output_filename)
    validate_download_base_url(download_base_url)
    return _TopLevelScalars(
        output_filename=output_filename,
        download_base_url=download_base_url,
        protect_file_raw=raw.get("protect_file"),
        mods_dir_toml=None,  # overwritten by caller after paths are resolved
    )


def _resolve_protect_file(protect_file_raw: Any, project_root: Path) -> Path | None:
    """Resolve the optional protect file path, or None when unset."""
    return _resolve_project_path(protect_file_raw, project_root) if protect_file_raw else None


def _build_all_sections(
    raw: dict[str, Any],
    project_root: Path,
    instances: dict[str, InstanceConfig],
    logger: Any,
) -> tuple[
    DockerConfig,
    ComposeLoadResult,
    dict[str, ResourcePackConfig],
    dict[str, Any],
    dict[str, str],
    DiscordConfig,
    str | None,
    tuple[Path | None, str | None, list[Path]],
]:
    """Build every non-instance section in spec order.

    Returns the tuple of section results; the caller assembles them.
    """
    docker = _build_docker_config(raw, project_root, logger)
    compose_result = _load_compose_or_warn(docker.compose_file, logger)
    resource_packs = _build_resource_packs(raw, instances, logger)
    sync_mapping = _build_sync_mapping(raw, logger)
    restart_policy = _build_restart_policy(raw, logger)
    discord = _build_discord_config(raw, logger)
    webhook_url = _resolve_webhook_url(raw)
    www_dir_triple = _resolve_www_dir(raw, project_root, compose_result, logger)
    return (docker, compose_result, resource_packs, sync_mapping, restart_policy, discord, webhook_url, www_dir_triple)


def _assemble_config(
    *,
    project_root: Path,
    config_dir: Path,
    sync_root: Path,
    modpack_dir: Path,
    mods_dir_toml: Path | None,
    scalars: _TopLevelScalars,
    protect_file: Path | None,
    instances: dict[str, InstanceConfig],
    partition: list[str],
    partition_unknown: list[str],
    requested_instances: Iterable[str] | None,
    sections: tuple,
) -> DeploymentConfig:
    """Assemble the final DeploymentConfig from the resolved parts."""
    (
        docker,
        compose_result,
        resource_packs,
        sync_mapping,
        restart_policy,
        discord,
        webhook_url,
        (www_dir, www_dir_error, www_dir_candidates),
    ) = sections
    return DeploymentConfig(
        project_root=project_root,
        config_dir=config_dir,
        sync_root=sync_root,
        modpack_dir=modpack_dir,
        www_dir=www_dir,
        www_dir_error=www_dir_error,
        www_dir_candidates=www_dir_candidates,
        output_filename=scalars.output_filename,
        download_base_url=scalars.download_base_url,
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


def load_deployment_config(
    config_dir: Path,
    requested_instances: Iterable[str] | None = None,
    cli_remaining: list[str] | None = None,
    logger: Any = None,
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
    _merge_cli_overrides(raw, cli_remaining, logger)
    return _build_deployment_config(
        raw=raw,
        config_dir=config_dir,
        project_root=project_root,
        requested_instances=requested_instances,
        logger=logger,
    )


def _build_deployment_config(
    raw: dict[str, Any],
    config_dir: Path,
    project_root: Path,
    requested_instances: Iterable[str] | None,
    logger: Any = None,
) -> DeploymentConfig:
    """Turn the merged raw dict into a typed DeploymentConfig.

    Walks the schema in spec order: top-level scalars and paths,
    ``[docker]`` + compose, ``[instances]`` + partition, then every
    remaining section via :func:`_build_all_sections`. Assembly is a
    separate step so the orchestrator reads as a linear sequence of
    "resolve X, resolve Y, assemble."
    """
    if logger is None:
        logger = _log

    _validate_instance_discovery(raw, logger)

    sync_root, modpack_dir, mods_dir_toml = _resolve_top_level_paths(raw, project_root, logger)
    scalars = _resolve_top_level_scalars(raw)

    docker = _build_docker_config(raw, project_root, logger)
    compose_result = _load_compose_or_warn(docker.compose_file, logger)

    instances = _build_instances(raw, compose_result, logger)
    partition, partition_unknown = resolve_partition(instances, requested_instances)

    sections = _build_all_sections(raw, project_root, instances, logger)
    # docker and compose_result are rebuilt inside _build_all_sections; drop
    # the earlier copies so only one set flows to assembly.
    docker, compose_result = sections[0], sections[1]

    protect_file = _resolve_protect_file(scalars.protect_file_raw, project_root)
    logger.debug(f"config: protect_file={protect_file} webhook_url={'set' if sections[6] else 'unset'}")

    result = _assemble_config(
        project_root=project_root,
        config_dir=config_dir,
        sync_root=sync_root,
        modpack_dir=modpack_dir,
        mods_dir_toml=mods_dir_toml,
        scalars=scalars,
        protect_file=protect_file,
        instances=instances,
        partition=partition,
        partition_unknown=partition_unknown,
        requested_instances=requested_instances,
        sections=sections,
    )
    logger.info(f"config: built; partition={partition} compose_ok={compose_result.ok} www_dir={result.www_dir}")
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
