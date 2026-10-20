# src/minecraft/modpack_upgrade/build/spec.py

"""Build specification loading and variable resolution.

Reads ``config.d/build/<name>.yaml`` and produces a typed
:class:`BuildSpec`. Every string-valued field is subject to
``${VAR}`` substitution during :func:`resolve_variables`. Only
``${VAR}`` where ``VAR`` matches ``[A-Za-z_][A-Za-z0-9_]*`` is
substituted; literal ``{...}`` in URLs or shell snippets is left
alone, so artifact patterns and script paths can use brace syntax if
they need to.

YAML loading goes through :func:`ConfigCore.load_config_file`, the
single-file loader from the shared configuration framework. This is
the same YAML parser ``config_model.py`` uses internally, with the
same exception-wrapping rules: malformed YAML surfaces as
``ConfigCore.ConfigError``, which we translate to our own
``SchemaError``. We do not use ``ConfigManager`` here because its
multi-source merge iterates ``.items()`` on each source, which would
raise ``AttributeError`` on a top-level non-mapping YAML instead of
letting us produce a clean diagnostic.

Variable precedence (lowest to highest): caller-supplied defaults
(typically manifest-derived values), ``variables:`` block in the YAML,
caller-supplied overrides (typically CLI ``--var`` values).
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
    "load_build_spec",
    "resolve_variables",
]


_DEFAULT_TIMEOUT_SECONDS = 1800.0
_DEFAULT_WORKING_DIR = "."
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class BuildArtifact:
    """One expected build output.

    ``path`` is a glob pattern relative to the clone root. After the
    build completes, the pattern must match exactly one file; if it
    matches zero or more than one, the build fails with a diagnostic
    naming every candidate.

    ``dest`` is an optional override for the destination. When ``None``,
    the destination is inferred by extension: ``.pw.toml`` -> the
    seed index; anything else -> the warehouse. When set, it is
    interpreted relative to the project root (unless absolute).
    """

    path: str
    dest: str | None = None


@dataclass(frozen=True)
class BuildSpec:
    """A validated in-house build specification."""

    name: str
    variables: dict[str, str]
    repository: str
    ref: str
    entrypoint: str
    script: str
    artifacts: tuple[BuildArtifact, ...]
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
    """Parse one artifact entry, short (string) or long (mapping) form."""
    if isinstance(raw, str):
        if not raw:
            raise SchemaError("artifact entry string must be non-empty")
        return BuildArtifact(path=raw)
    if isinstance(raw, dict):
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise SchemaError("artifact entry 'path' must be a non-empty string")
        dest = raw.get("dest")
        if dest is not None and (not isinstance(dest, str) or not dest):
            raise SchemaError("artifact entry 'dest' must be a non-empty string or omitted")
        return BuildArtifact(path=path, dest=dest)
    raise SchemaError(f"artifact entry must be a string or mapping, got {type(raw).__name__}")


def _parse_artifacts(raw: object) -> tuple[BuildArtifact, ...]:
    """Parse the required ``artifacts`` list."""
    if not isinstance(raw, list) or not raw:
        raise SchemaError("build spec field 'artifacts' must be a non-empty list")
    return tuple(_parse_artifact(item) for item in raw)


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

    return BuildSpec(
        name=name,
        variables=_parse_variables(body.get("variables")),
        repository=_require_string(body, "repository"),
        ref=_require_string(body, "ref"),
        entrypoint=_require_string(body, "entrypoint"),
        script=_require_string(body, "script"),
        artifacts=_parse_artifacts(body.get("artifacts")),
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
    :class:`SchemaError` on any malformed input, wrapping ConfigCore's
    :class:`ConfigError` for YAML syntax errors and ``OSError`` for
    I/O failures.
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
    CLI ``--var`` values). The result is a new :class:`BuildSpec`;
    ``spec`` is not mutated.
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
        artifacts=tuple(
            BuildArtifact(
                path=_substitute(a.path, merged),
                dest=_substitute(a.dest, merged) if a.dest is not None else None,
            )
            for a in spec.artifacts
        ),
        working_dir=_substitute(spec.working_dir, merged),
    )
