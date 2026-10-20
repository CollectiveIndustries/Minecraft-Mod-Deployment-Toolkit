# src/minecraft/modpack_upgrade/build/spec.py

"""Build specification loading and variable resolution.

Reads ``config.d/build/<name>.yaml`` and produces a typed
:class:`BuildSpec`. Every string-valued field is subject to ``${VAR}``
substitution during :func:`resolve_variables`. Only ``${VAR}`` where
``VAR`` matches ``[A-Za-z_][A-Za-z0-9_]*`` is substituted; literal
``{...}`` in URLs or shell snippets is left alone.

YAML loading goes through :func:`ConfigCore.load_config_file`, the
single-file loader from the shared configuration framework.

``side`` is required
--------------------
Every build spec declares a ``side`` field (``client``, ``server``, or
``both``). This is the *declared* side the mod author chose for the
in-house mod. It is written into the generated ``.pw.toml``'s ``side``
field, which becomes ``[[seed]].side_declared`` after ``load_seed``
reads the file, which feeds the resolver's dependency closure.

``side`` is NOT the final side. :doc:`docs/Side_Determination.md`
defines a precedence chain (``override > jar > provider > .pw.toml >
unknown``) where ``side_overrides.toml`` remains the final authority
across all cases. The build spec's ``side`` lands at the
``.pw.toml`` position - priority 4 - exactly where any other
``.pw.toml`` side lands.

``.pw.toml`` generation
-----------------------
The runner always generates a ``.pw.toml`` for the single jar the
build produces, using :func:`minecraft.modpack_upgrade.pw_toml.write_pw_toml`.
The mod's own build script does not need a ``.pw.toml``-generation
step. This centralizes the writer in the upgrade toolchain, removing
duplication across mod repos and pinning one implementation of the
schema, byte-stability contract, and side-sentinel semantics.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path

from ConfigCore import ConfigError as ConfigCoreError
from ConfigCore import load_config_file

from minecraft.modpack_upgrade.errors import SchemaError

__all__ = [
    "BuildArtifact",
    "BuildSpec",
    "PwTomlDependency",
    "load_build_spec",
    "resolve_variables",
]


_DEFAULT_TIMEOUT_SECONDS = 1800.0
_DEFAULT_WORKING_DIR = "."
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VALID_SIDES = ("client", "server", "both")


@dataclass(frozen=True)
class BuildArtifact:
    """One glob pattern the built jar must match.

    The runner expands each pattern under the clone root. Across all
    patterns, exactly one jar must match; that jar is the mod's primary
    artifact. The runner generates its ``.pw.toml`` and stages both into
    the warehouse.

    Patterns MUST NOT contain ``..`` (rejected before matching). Matches
    that resolve outside the clone directory (via symlink) are rejected.
    """

    path: str


@dataclass(frozen=True)
class PwTomlDependency:
    """A ``[[x-prismlauncher-dependencies]]`` edge for the generated .pw.toml.

    Project-ID edges only, matching the writer's schema. ``type`` is a
    free-form string (``required`` / ``optional`` / etc.); the writer
    does not validate it and the reader uppercases it on load.
    """

    addon_id: str
    type: str


@dataclass(frozen=True)
class BuildSpec:
    """A validated in-house build specification.

    ``side`` is required and restricted to ``client``, ``server``, or
    ``both``. ``unknown`` is deliberately excluded: it would make the
    mod unmarked and the resolver would silently drop it from the
    deployable set at materialize.

    ``mod_name`` is the display name written into the generated
    ``.pw.toml``'s ``name`` field. ``name`` on the spec itself is the
    filename stem (``createpollutioncleanup`` from
    ``config.d/build/createpollutioncleanup.yaml``); ``mod_name`` is
    the human-readable form (``"Custom Pollution Cleanup"``).
    """

    name: str
    variables: dict[str, str]
    repository: str
    ref: str
    entrypoint: str
    script: str
    side: str
    artifacts: tuple[BuildArtifact, ...]
    mod_name: str | None = None
    dependencies: tuple[PwTomlDependency, ...] = ()
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    working_dir: str = _DEFAULT_WORKING_DIR
    depth: int = 0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _require_string(body: dict, key: str) -> str:
    """Return ``body[key]`` as a non-empty string, or raise."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"build spec field {key!r} must be a non-empty string")
    return value


def _parse_variables(raw: object) -> dict[str, str]:
    """Parse the optional ``variables`` mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("build spec field 'variables' must be a mapping")
    return {str(k): str(v) for k, v in raw.items()}


def _parse_env(raw: object) -> dict[str, str]:
    """Parse the optional ``env`` mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError("build spec field 'env' must be a mapping")
    return {str(k): str(v) for k, v in raw.items()}


def _parse_args(raw: object) -> tuple[str, ...]:
    """Parse the optional ``args`` list."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaError("build spec field 'args' must be a list of strings")
    return tuple(str(a) for a in raw)


def _parse_artifact(raw: object) -> BuildArtifact:
    """Parse one artifact entry, string or mapping form."""
    if isinstance(raw, str):
        if not raw:
            raise SchemaError("artifact entry string must be non-empty")
        return BuildArtifact(path=raw)
    if isinstance(raw, dict):
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise SchemaError("artifact entry 'path' must be a non-empty string")
        return BuildArtifact(path=path)
    raise SchemaError(f"artifact entry must be a string or mapping, got {type(raw).__name__}")


def _parse_artifacts(raw: object) -> tuple[BuildArtifact, ...]:
    """Parse the required ``artifacts`` list."""
    if not isinstance(raw, list) or not raw:
        raise SchemaError("build spec field 'artifacts' must be a non-empty list")
    return tuple(_parse_artifact(item) for item in raw)


def _parse_side(raw: object) -> str:
    """Parse the required top-level ``side`` field."""
    if raw not in _VALID_SIDES:
        raise SchemaError(f"build spec field 'side' must be one of {list(_VALID_SIDES)}, got {raw!r}")
    return raw  # type: ignore[return-value]


def _parse_dependency(raw: object) -> PwTomlDependency:
    """Parse one ``dependencies[]`` entry."""
    if not isinstance(raw, dict):
        raise SchemaError("dependencies entries must be mappings")
    addon_id = raw.get("addon_id")
    if not isinstance(addon_id, str) or not addon_id:
        raise SchemaError("dependencies[].addon_id must be a non-empty string")
    dep_type = raw.get("type")
    if not isinstance(dep_type, str) or not dep_type:
        raise SchemaError("dependencies[].type must be a non-empty string")
    return PwTomlDependency(addon_id=addon_id, type=dep_type)


def _parse_dependencies(raw: object) -> tuple[PwTomlDependency, ...]:
    """Parse the optional ``dependencies`` list."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaError("build spec field 'dependencies' must be a list")
    return tuple(_parse_dependency(d) for d in raw)


def _parse_body(name: str, body: dict) -> BuildSpec:
    """Build a :class:`BuildSpec` from a validated raw mapping."""
    timeout_raw = body.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"build spec field 'timeout_seconds' must be numeric, got {timeout_raw!r}") from exc
    if timeout_seconds <= 0:
        raise SchemaError(f"build spec field 'timeout_seconds' must be > 0, got {timeout_seconds}")

    depth_raw = body.get("depth", 0)
    try:
        depth = int(depth_raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"build spec field 'depth' must be an integer, got {depth_raw!r}") from exc
    if depth < 0:
        raise SchemaError(f"build spec field 'depth' must be >= 0, got {depth}")

    mod_name_raw = body.get("name")
    if mod_name_raw is not None and (not isinstance(mod_name_raw, str) or not mod_name_raw):
        raise SchemaError("build spec field 'name' must be a non-empty string or omitted")

    return BuildSpec(
        name=name,
        variables=_parse_variables(body.get("variables")),
        repository=_require_string(body, "repository"),
        ref=_require_string(body, "ref"),
        entrypoint=_require_string(body, "entrypoint"),
        script=_require_string(body, "script"),
        side=_parse_side(body.get("side")),
        artifacts=_parse_artifacts(body.get("artifacts")),
        mod_name=mod_name_raw,
        dependencies=_parse_dependencies(body.get("dependencies")),
        args=_parse_args(body.get("args")),
        env=_parse_env(body.get("env")),
        timeout_seconds=timeout_seconds,
        working_dir=str(body.get("working_dir", _DEFAULT_WORKING_DIR)),
        depth=depth,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_build_spec(path: Path) -> BuildSpec:
    """Load and validate ``config.d/build/<name>.yaml``.

    The spec's ``name`` is derived from the filename stem. Raises
    :class:`SchemaError` on any malformed input.
    """
    if not path.is_file():
        raise SchemaError(f"build spec not found: {path}")
    try:
        body = load_config_file(path, "yaml")
    except ConfigCoreError as exc:
        raise SchemaError(f"could not read {path}: {exc}") from exc
    except OSError as exc:
        raise SchemaError(f"could not read {path}: {exc}") from exc
    if not isinstance(body, dict):
        raise SchemaError(f"{path}: top-level must be a mapping")
    return _parse_body(path.stem, body)


def _substitute(text: str, variables: dict[str, str]) -> str:
    """Substitute ``${VAR}`` patterns in ``text``; raise on undefined."""

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise SchemaError(f"undefined variable ${{{key}}} in {text!r}")
        return variables[key]

    return _VAR_RE.sub(_repl, text)


def resolve_variables(spec: BuildSpec, defaults: dict[str, str], overrides: dict[str, str]) -> BuildSpec:
    """Return a copy of ``spec`` with every ``${VAR}`` pattern resolved.

    Precedence (lowest to highest): ``defaults`` (typically manifest
    values), ``spec.variables`` (YAML block), ``overrides`` (typically
    CLI ``--var`` values).

    Substitution is applied to: ``repository``, ``ref``, ``script``,
    ``args``, ``env`` values, artifact ``path``, ``working_dir``, ``mod_name``,
    and dependency ``addon_id``. Fields with enum semantics (``side``,
    dependency ``type``, ``timeout_seconds``, ``depth``, ``entrypoint``)
    are NOT substituted.
    """
    merged: dict[str, str] = {}
    merged.update(defaults)
    merged.update(spec.variables)
    merged.update(overrides)

    return dataclasses.replace(
        spec,
        repository=_substitute(spec.repository, merged),
        ref=_substitute(spec.ref, merged),
        script=_substitute(spec.script, merged),
        args=tuple(_substitute(a, merged) for a in spec.args),
        env={k: _substitute(v, merged) for k, v in spec.env.items()},
        artifacts=tuple(BuildArtifact(path=_substitute(a.path, merged)) for a in spec.artifacts),
        working_dir=_substitute(spec.working_dir, merged),
        mod_name=_substitute(spec.mod_name, merged) if spec.mod_name is not None else None,
        dependencies=tuple(
            PwTomlDependency(
                addon_id=_substitute(d.addon_id, merged),
                type=d.type,
            )
            for d in spec.dependencies
        ),
    )
