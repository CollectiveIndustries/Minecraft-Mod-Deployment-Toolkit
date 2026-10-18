# src/minecraft/modpack_upgrade/slip.py

"""Packing slip schema, canonical hashing, and verification (spec Section 5)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import canonical
from .errors import HashMismatchError, SchemaError, SlipError

__all__ = [
    "ClientDistribution",
    "Dependency",
    "Fingerprints",
    "GitInputs",
    "InstanceSpec",
    "JarEntry",
    "PackingSlip",
    "ResourcePackEntry",
    "RuntimeDescriptor",
    "TestedInfo",
    "VelocityPluginEntry",
    "compute_content_sha256",
    "load_slip",
    "parse_slip",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_MODES = ("update", "upgrade")
_JAR_SIDES = ("both", "server", "client")
_CONFIG_MODES = ("merge", "delete")
_KUBEJS_MODES = ("delete",)
_DEP_NAMESPACES = ("project", "modid")
# Amendments A1.2: provider_metadata joins the source enum.
_DEP_SOURCES = ("pw_toml", "jar_manifest", "provider_metadata")
# Amendments A1.3: type is constrained to six values.
_DEP_TYPES = ("required", "optional", "incompatible", "embedded", "tool", "include")
_TESTED_WORLDS = ("survival", "creative")

# Backward-compat placeholder for v1.3.0 slips that predate A1.1.
_ZERO_SHA256 = "0" * 64


# ===========================================================================
# Section 1  Typed dataclasses
# ===========================================================================


@dataclass(frozen=True)
class RuntimeDescriptor:
    """Runtime descriptor fields recorded in a packing slip's [runtime] table."""

    minecraft_version: str
    loader_type: str
    loader_version: str
    image_reference: str
    image_digest: str
    platform: str
    java_version: str


@dataclass(frozen=True)
class GitInputs:
    """Git tree hashes recorded in a packing slip's [inputs.git] table."""

    config_tree: str
    kubejs_tree: str
    compose_tree: str
    deploy_toml_tree: str


@dataclass(frozen=True)
class Fingerprints:
    """Canonical fingerprints recorded in a packing slip's [fingerprints] table.

    Amendment A1.1: ``resolution_inputs_sha256`` replaces the retired
    ``side_policy_sha256``; its inputs are covered by ``side_overrides``
    inside the resolver input snapshot (spec Section 5.4.1).

    For slips created under v1.3.0 that predate A1.1, the field is
    absent from disk and ``parse_slip`` fills it with a zero-hash
    placeholder. Newly materialized slips always carry the real value.
    """

    manifest_sha256: str
    resolution_inputs_sha256: str
    runtime_sha256: str
    server_inputs_sha256: str
    client_inputs_sha256: str


@dataclass(frozen=True)
class TestedInfo:
    """World and snapshot identity recorded in a packing slip's [tested] table."""

    world: str | None
    snapshot_id: str | None


@dataclass(frozen=True)
class Dependency:
    """A single dependency edge on a [[jar]] entry.

    ``source`` is one of ``pw_toml`` / ``jar_manifest`` / ``provider_metadata``
    (Amendments A1.2). ``type`` is one of the six values in ``_DEP_TYPES``
    (Amendments A1.3).
    """

    namespace: str
    provider: str | None
    project_id: str | None
    version_id: str | None
    type: str
    source: str


@dataclass(frozen=True)
class JarEntry:
    """A single [[jar]] entry in a packing slip."""

    filename: str
    sha256: str
    side: str
    provider: str
    provider_endpoint: str | None
    project_id: str | None
    version_id: str | None
    pw_toml: str
    pw_toml_sha256: str
    dependencies: tuple[Dependency, ...]
    build_id: str | None
    source_repo: str | None
    source_commit: str | None
    toolchain_id: str | None

    @property
    def is_in_house(self) -> bool:
        """True when the entry's provider is 'in-house'."""
        return self.provider == "in-house"


@dataclass(frozen=True)
class ResourcePackEntry:
    """A single [[resource_pack]] entry in a packing slip."""

    filename: str
    sha256: str
    instance: str
    required: bool
    prompt: str


@dataclass(frozen=True)
class VelocityPluginEntry:
    """A single [[velocity_plugin]] entry in a packing slip."""

    filename: str
    sha256: str


@dataclass(frozen=True)
class ClientDistribution:
    """The [client_distribution] table in a packing slip."""

    output_filename: str
    sha256: str
    with_resources: bool
    member_manifest_sha256: str | None


@dataclass(frozen=True)
class InstanceSpec:
    """A single [instances.*] table in a packing slip."""

    targeted: bool
    required_sides: tuple[str, ...]
    config_mode: str
    kubejs_mode: str
    config_deletions: tuple[str, ...]
    protect_patterns: tuple[str, ...]


@dataclass(frozen=True)
class PackingSlip:
    """A fully parsed and validated packing slip."""

    packing_slip_version: int
    release_id: str
    release_mode: str
    content_sha256: str
    runtime: RuntimeDescriptor
    inputs_git: GitInputs
    fingerprints: Fingerprints
    tested: TestedInfo
    jars: tuple[JarEntry, ...]
    resource_packs: tuple[ResourcePackEntry, ...]
    velocity_plugins: tuple[VelocityPluginEntry, ...]
    client_distribution: ClientDistribution | None
    instances: Mapping[str, InstanceSpec]
    meta: Mapping[str, Any]


# ===========================================================================
# Section 2  Canonical body preparation (spec Section 5.4)
# ===========================================================================


def _strip_none(value: Any) -> Any:
    """Recursively drop ``None`` values from mappings.

    Spec Section 5.4 distinguishes two cases:

    * A ``None`` **mapping value** is dropped. TOML has no null; a
      programmatically-constructed body with ``{"provider": None}``
      MUST hash identically to a TOML-round-tripped body with the key
      absent.
    * A ``None`` **array element** is not stripped here. It survives
      to ``canonical.canonicalize``, which raises ``TypeError``.
      Removing an element from an array silently changes its
      cardinality and, for ordered sequences, its meaning; failing
      validation is the frozen rule.

    ``None`` nested inside a mapping or array inside a mapping is
    normalized by the same rules applied recursively: dict-internal
    ``None`` values are stripped, array-internal ``None`` elements are
    not.
    """
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def _sort_dependencies(deps: list) -> list:
    """Return ``deps`` sorted by canonical serialization of each entry."""

    def key(item: Any) -> bytes:
        if not isinstance(item, dict):
            raise SchemaError("dependencies entries must be tables")
        return canonical.serialize(canonical.canonicalize(item))

    return sorted(deps, key=key)


def _sort_jars(jars: list) -> list:
    """Return ``jars`` sorted by NFC filename, with each entry's dependencies sorted."""

    def key(item: Any) -> str:
        if not isinstance(item, dict):
            raise SchemaError("[[jar]] entries must be tables")
        filename = item.get("filename")
        if not isinstance(filename, str):
            raise SchemaError("[[jar]].filename must be a string")
        return canonical.nfc(filename)

    sorted_jars = sorted(jars, key=key)
    out: list[dict] = []
    for entry in sorted_jars:
        copy = dict(entry)
        if isinstance(copy.get("dependencies"), list):
            copy["dependencies"] = _sort_dependencies(copy["dependencies"])
        out.append(copy)
    return out


def _sort_resource_packs(packs: list) -> list:
    """Return ``packs`` sorted by (instance, filename)."""

    def key(item: Any) -> tuple[str, str]:
        if not isinstance(item, dict):
            raise SchemaError("[[resource_pack]] entries must be tables")
        instance = item.get("instance", "")
        filename = item.get("filename", "")
        if not isinstance(instance, str) or not isinstance(filename, str):
            raise SchemaError("[[resource_pack]].instance and .filename must be strings")
        return (canonical.nfc(instance), canonical.nfc(filename))

    return sorted(packs, key=key)


def _sort_velocity_plugins(plugins: list) -> list:
    """Return ``plugins`` sorted by filename."""

    def key(item: Any) -> str:
        if not isinstance(item, dict):
            raise SchemaError("[[velocity_plugin]] entries must be tables")
        filename = item.get("filename", "")
        if not isinstance(filename, str):
            raise SchemaError("[[velocity_plugin]].filename must be a string")
        return canonical.nfc(filename)

    return sorted(plugins, key=key)


def _sort_instance_arrays(instances: dict) -> dict:
    """Return a copy of ``instances`` with each set-valued array sorted."""
    out: dict[str, Any] = {}
    for name, spec in instances.items():
        if not isinstance(spec, dict):
            out[name] = spec
            continue
        copy = dict(spec)
        for array_key in ("required_sides", "config_deletions"):
            value = copy.get(array_key)
            if isinstance(value, list):
                copy[array_key] = sorted(canonical.nfc(x) for x in value if isinstance(x, str))
        out[name] = copy
    return out


def _prepare_canonical_body(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical copy of ``raw`` for hashing.

    Removes ``content_sha256``, strips ``None`` values (absent in TOML),
    and applies the spec's array-ordering rules. Does not mutate ``raw``.
    """
    body = _strip_none({k: v for k, v in raw.items() if k != "content_sha256"})

    if isinstance(body.get("jar"), list):
        body["jar"] = _sort_jars(body["jar"])
    if isinstance(body.get("resource_pack"), list):
        body["resource_pack"] = _sort_resource_packs(body["resource_pack"])
    if isinstance(body.get("velocity_plugin"), list):
        body["velocity_plugin"] = _sort_velocity_plugins(body["velocity_plugin"])
    if isinstance(body.get("instances"), dict):
        body["instances"] = _sort_instance_arrays(body["instances"])
    return body


def compute_content_sha256(raw: dict[str, Any]) -> str:
    """SHA-256 of the canonical slip body (excluding ``content_sha256``)."""
    return canonical.digest(_prepare_canonical_body(raw))


# ===========================================================================
# Section 3  Schema validation helpers
# ===========================================================================


def _require_table(body: dict, key: str, *, required: bool = True) -> dict:
    """Return ``body[key]`` as a table, or raise SchemaError."""
    value = body.get(key)
    if value is None:
        if required:
            raise SchemaError(f"missing required table: [{key}]")
        return {}
    if not isinstance(value, dict):
        raise SchemaError(f"[{key}] must be a table")
    return value


def _require_str(body: dict, key: str, *, path: str = "") -> str:
    """Return ``body[key]`` as a non-empty string, or raise SchemaError."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{path}{key} must be a non-empty string")
    return value


def _optional_str(body: dict, key: str, *, path: str = "") -> str | None:
    """Return ``body[key]`` as a string or None, or raise SchemaError."""
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{path}{key} must be a string")
    return value


def _require_int(body: dict, key: str, *, path: str = "") -> int:
    """Return ``body[key]`` as an int (excluding bool), or raise SchemaError."""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{path}{key} must be an integer")
    return value


def _require_bool(body: dict, key: str, *, path: str = "") -> bool:
    """Return ``body[key]`` as a bool, or raise SchemaError."""
    value = body.get(key)
    if not isinstance(value, bool):
        raise SchemaError(f"{path}{key} must be a boolean")
    return value


def _optional_bool(body: dict, key: str, *, default: bool = False, path: str = "") -> bool:
    """Return ``body[key]`` as a bool, defaulting to ``default`` when absent."""
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SchemaError(f"{path}{key} must be a boolean")
    return value


def _require_enum(body: dict, key: str, allowed: tuple[str, ...], *, path: str = "") -> str:
    """Return ``body[key]`` as a string from ``allowed``, or raise SchemaError."""
    value = body.get(key)
    if not isinstance(value, str):
        raise SchemaError(f"{path}{key} must be a string")
    if value not in allowed:
        raise SchemaError(f"{path}{key} must be one of {allowed}, got {value!r}")
    return value


def _require_sha256(body: dict, key: str, *, path: str = "") -> str:
    """Return ``body[key]`` as a lowercase 64-char SHA-256 hex string, or raise."""
    value = body.get(key)
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SchemaError(f"{path}{key} must be a lowercase 64-char SHA-256 hex string")
    return value


def _require_list_of_tables(body: dict, key: str, *, required: bool = False) -> list[dict]:
    """Return ``body[key]`` as an array of tables, or raise SchemaError."""
    value = body.get(key)
    if value is None:
        if required:
            raise SchemaError(f"missing required array of tables: [[{key}]]")
        return []
    if not isinstance(value, list):
        raise SchemaError(f"[[{key}]] must be an array of tables")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SchemaError(f"[[{key}]][{index}] must be a table")
    return value


def _require_str_list(body: dict, key: str, *, path: str = "") -> tuple[str, ...]:
    """Return ``body[key]`` as a tuple of strings, or raise SchemaError."""
    value = body.get(key, [])
    if not isinstance(value, list):
        raise SchemaError(f"{path}{key} must be an array of strings")
    for item in value:
        if not isinstance(item, str):
            raise SchemaError(f"{path}{key} must contain only strings")
    return tuple(value)


# ===========================================================================
# Section 4  Schema-section builders
# ===========================================================================


def _build_runtime(body: dict) -> RuntimeDescriptor:
    """Build the [runtime] descriptor from ``body``, or raise SchemaError."""
    raw = _require_table(body, "runtime")
    path = "[runtime]."
    return RuntimeDescriptor(
        minecraft_version=_require_str(raw, "minecraft_version", path=path),
        loader_type=_require_str(raw, "loader_type", path=path),
        loader_version=_require_str(raw, "loader_version", path=path),
        image_reference=_require_str(raw, "image_reference", path=path),
        image_digest=_require_str(raw, "image_digest", path=path),
        platform=_require_str(raw, "platform", path=path),
        java_version=_require_str(raw, "java_version", path=path),
    )


def _build_git_inputs(body: dict) -> GitInputs:
    """Build the [inputs.git] descriptor from ``body``, or raise SchemaError."""
    inputs = _require_table(body, "inputs")
    raw = _require_table(inputs, "git")
    path = "[inputs.git]."
    return GitInputs(
        config_tree=_require_str(raw, "config_tree", path=path),
        kubejs_tree=_require_str(raw, "kubejs_tree", path=path),
        compose_tree=_require_str(raw, "compose_tree", path=path),
        deploy_toml_tree=_require_str(raw, "deploy_toml_tree", path=path),
    )


def _build_fingerprints(body: dict) -> Fingerprints:
    """Build the [fingerprints] descriptor from ``body``, or raise SchemaError.

    Amendment A1.1: ``resolution_inputs_sha256`` replaces the retired
    ``side_policy_sha256``. It is required for slips created under
    v1.3.1. Slips created under v1.3.0 predate the field; those are
    accepted with a zero-hash placeholder so historical release bundles
    remain loadable and their canonical hash still verifies. Newly
    materialized slips always carry the real value.
    """
    raw = _require_table(body, "fingerprints")
    path = "[fingerprints]."
    resolution_inputs = raw.get("resolution_inputs_sha256")
    if resolution_inputs is None:
        resolution_inputs = _ZERO_SHA256
    elif not isinstance(resolution_inputs, str) or _SHA256_RE.fullmatch(resolution_inputs) is None:
        raise SchemaError(f"{path}resolution_inputs_sha256 must be a lowercase 64-char SHA-256 hex string")
    return Fingerprints(
        manifest_sha256=_require_sha256(raw, "manifest_sha256", path=path),
        resolution_inputs_sha256=resolution_inputs,
        runtime_sha256=_require_sha256(raw, "runtime_sha256", path=path),
        server_inputs_sha256=_require_sha256(raw, "server_inputs_sha256", path=path),
        client_inputs_sha256=_require_sha256(raw, "client_inputs_sha256", path=path),
    )


def _build_tested(body: dict) -> TestedInfo:
    """Build the [tested] descriptor from ``body``, or raise SchemaError."""
    raw = body.get("tested")
    if raw is None:
        return TestedInfo(world=None, snapshot_id=None)
    if not isinstance(raw, dict):
        raise SchemaError("[tested] must be a table")
    world = raw.get("world")
    if world is not None and (not isinstance(world, str) or world not in _TESTED_WORLDS):
        raise SchemaError(f"[tested].world must be one of {_TESTED_WORLDS}")
    snapshot_id = raw.get("snapshot_id")
    if snapshot_id is not None and not isinstance(snapshot_id, str):
        raise SchemaError("[tested].snapshot_id must be a string")
    return TestedInfo(world=world, snapshot_id=snapshot_id)


def _build_dependency(raw: dict, index: int) -> Dependency:
    """Build one dependency edge, or raise SchemaError.

    Amendment A1.2: ``source`` accepts ``provider_metadata``. Amendment
    A1.3: ``type`` is constrained to the six-value enum.
    """
    path = f"[[jar]].dependencies[{index}]."
    if not isinstance(raw, dict):
        raise SchemaError(f"{path} must be a table")
    namespace = raw.get("namespace")
    if namespace not in _DEP_NAMESPACES:
        raise SchemaError(f"{path}namespace must be one of {_DEP_NAMESPACES}")
    source = raw.get("source")
    if source not in _DEP_SOURCES:
        raise SchemaError(f"{path}source must be one of {_DEP_SOURCES}")
    dep_type = _require_str(raw, "type", path=path)
    if dep_type not in _DEP_TYPES:
        raise SchemaError(f"{path}type must be one of {_DEP_TYPES}, got {dep_type!r}")
    return Dependency(
        namespace=namespace,
        provider=_optional_str(raw, "provider", path=path),
        project_id=_optional_str(raw, "project_id", path=path),
        version_id=_optional_str(raw, "version_id", path=path),
        type=dep_type,
        source=source,
    )


def _build_jar(raw: dict, index: int) -> JarEntry:
    """Build one [[jar]] entry, or raise SchemaError."""
    path = f"[[jar]][{index}]."
    deps_raw = raw.get("dependencies", [])
    if not isinstance(deps_raw, list):
        raise SchemaError(f"{path}dependencies must be an array")
    dependencies = tuple(_build_dependency(item, i) for i, item in enumerate(deps_raw))
    return JarEntry(
        filename=_require_str(raw, "filename", path=path),
        sha256=_require_sha256(raw, "sha256", path=path),
        side=_require_enum(raw, "side", _JAR_SIDES, path=path),
        provider=_require_str(raw, "provider", path=path),
        provider_endpoint=_optional_str(raw, "provider_endpoint", path=path),
        project_id=_optional_str(raw, "project_id", path=path),
        version_id=_optional_str(raw, "version_id", path=path),
        pw_toml=_require_str(raw, "pw_toml", path=path),
        pw_toml_sha256=_require_sha256(raw, "pw_toml_sha256", path=path),
        dependencies=dependencies,
        build_id=_optional_str(raw, "build_id", path=path),
        source_repo=_optional_str(raw, "source_repo", path=path),
        source_commit=_optional_str(raw, "source_commit", path=path),
        toolchain_id=_optional_str(raw, "toolchain_id", path=path),
    )


def _build_resource_pack(raw: dict, index: int) -> ResourcePackEntry:
    """Build one [[resource_pack]] entry, or raise SchemaError."""
    path = f"[[resource_pack]][{index}]."
    return ResourcePackEntry(
        filename=_require_str(raw, "filename", path=path),
        sha256=_require_sha256(raw, "sha256", path=path),
        instance=_require_str(raw, "instance", path=path),
        required=_require_bool(raw, "required", path=path),
        prompt=_optional_str(raw, "prompt", path=path) or "",
    )


def _build_velocity_plugin(raw: dict, index: int) -> VelocityPluginEntry:
    """Build one [[velocity_plugin]] entry, or raise SchemaError."""
    path = f"[[velocity_plugin]][{index}]."
    return VelocityPluginEntry(
        filename=_require_str(raw, "filename", path=path),
        sha256=_require_sha256(raw, "sha256", path=path),
    )


def _build_client_distribution(body: dict) -> ClientDistribution | None:
    """Build the [client_distribution] descriptor, or return None when absent."""
    raw = body.get("client_distribution")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemaError("[client_distribution] must be a table")
    path = "[client_distribution]."
    return ClientDistribution(
        output_filename=_require_str(raw, "output_filename", path=path),
        sha256=_require_sha256(raw, "sha256", path=path),
        with_resources=_require_bool(raw, "with_resources", path=path),
        member_manifest_sha256=_optional_str(raw, "member_manifest_sha256", path=path),
    )


def _build_instance(name: str, raw: dict) -> InstanceSpec:
    """Build one [instances.*] entry, or raise SchemaError."""
    path = f"[instances.{name}]."
    if not isinstance(raw, dict):
        raise SchemaError(f"{path} must be a table")
    required_sides = _require_str_list(raw, "required_sides", path=path)
    for side in required_sides:
        if side not in _JAR_SIDES:
            raise SchemaError(f"{path}required_sides contains invalid value {side!r}")
    config_deletions = _require_str_list(raw, "config_deletions", path=path)
    protect_patterns = _require_str_list(raw, "protect_patterns", path=path)
    return InstanceSpec(
        targeted=_optional_bool(raw, "targeted", default=False, path=path),
        required_sides=tuple(sorted(canonical.nfc(s) for s in required_sides)),
        config_mode=_require_enum(raw, "config_mode", _CONFIG_MODES, path=path),
        kubejs_mode=_require_enum(raw, "kubejs_mode", _KUBEJS_MODES, path=path),
        config_deletions=tuple(sorted(canonical.nfc(s) for s in config_deletions)),
        protect_patterns=protect_patterns,
    )


def _check_unique_filenames(jars: tuple[JarEntry, ...]) -> None:
    """Raise SchemaError if two [[jar]] entries share a filename."""
    seen: set[str] = set()
    for jar in jars:
        if jar.filename in seen:
            raise SchemaError(f"duplicate [[jar]].filename: {jar.filename!r}")
        seen.add(jar.filename)


# ===========================================================================
# Section 5  Public API
# ===========================================================================


def parse_slip(raw: dict[str, Any]) -> PackingSlip:
    """Validate a parsed slip body and return a typed :class:`PackingSlip`.

    Preserves the document order of ``[[jar]]``, ``[[resource_pack]]``,
    and ``[[velocity_plugin]]``. Canonical ordering is applied only for
    hashing. Does not verify ``content_sha256``; use :func:`load_slip`.
    """
    if not isinstance(raw, dict):
        raise SchemaError("slip body must be a TOML table")

    version = _require_int(raw, "packing_slip_version")
    if version != 1:
        raise SchemaError(f"unsupported packing_slip_version: {version}")

    release_id = _require_str(raw, "release_id")
    release_mode = _require_enum(raw, "release_mode", _RELEASE_MODES)
    content_sha256 = _require_sha256(raw, "content_sha256")

    runtime = _build_runtime(raw)
    inputs_git = _build_git_inputs(raw)
    fingerprints = _build_fingerprints(raw)
    tested = _build_tested(raw)

    jars_raw = _require_list_of_tables(raw, "jar", required=True)
    jars = tuple(_build_jar(item, i) for i, item in enumerate(jars_raw))
    _check_unique_filenames(jars)

    resource_packs = tuple(_build_resource_pack(item, i) for i, item in enumerate(_require_list_of_tables(raw, "resource_pack")))
    velocity_plugins = tuple(_build_velocity_plugin(item, i) for i, item in enumerate(_require_list_of_tables(raw, "velocity_plugin")))

    client_distribution = _build_client_distribution(raw)

    instances_raw = _require_table(raw, "instances")
    instances = {name: _build_instance(name, spec) for name, spec in instances_raw.items()}

    meta_raw = raw.get("meta") or {}
    if not isinstance(meta_raw, dict):
        raise SchemaError("[meta] must be a table")

    return PackingSlip(
        packing_slip_version=version,
        release_id=release_id,
        release_mode=release_mode,
        content_sha256=content_sha256,
        runtime=runtime,
        inputs_git=inputs_git,
        fingerprints=fingerprints,
        tested=tested,
        jars=jars,
        resource_packs=resource_packs,
        velocity_plugins=velocity_plugins,
        client_distribution=client_distribution,
        instances=MappingProxyType(dict(instances)),
        meta=MappingProxyType(dict(meta_raw)),
    )


def load_slip(path: Path) -> PackingSlip:
    """Parse, validate, and hash-verify a packing slip from disk.

    Raises :class:`SchemaError` on schema violations and
    :class:`HashMismatchError` when the recomputed ``content_sha256``
    does not match the recorded value.
    """
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"{path}: malformed TOML: {exc}") from exc
    except OSError as exc:
        raise SlipError(f"{path}: could not read slip: {exc}") from exc

    slip = parse_slip(raw)

    computed = compute_content_sha256(raw)
    if computed != slip.content_sha256:
        raise HashMismatchError(f"{path}: content_sha256 mismatch: recorded={slip.content_sha256} computed={computed}")
    return slip
