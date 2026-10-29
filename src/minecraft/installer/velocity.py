# src/minecraft/installer/velocity.py

"""Velocity forwarding secret distribution.

Velocity stores its secret in ``forwarding.secret`` at the proxy's data
root. Each modded backend server that uses Proxy-Compatible-Forge (or an
equivalent mod) stores the same secret in a ``[forwarding]`` block of a
TOML config file. Both are edited by this module.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from .errors import SecretsError

__all__ = [
    "read_pcf_forwarding_secret",
    "update_pcf_forwarding_secret",
    "write_default_pcf",
]

_FORWARDING_HEADER = "[forwarding]"
_SECRET_LINE = re.compile(r"^\s*secret\s*=")


_DEFAULT_PCF_TEMPLATE = """\
[forwarding]
        #Enable or disable player info forwarding. Changing this setting requires a server restart.
        enabled = true
        #The type of forwarding to use
        #Allowed Values: LEGACY, BUNGEEGUARD, MODERN
        mode = "MODERN"
        #The forwarding secret shared with the proxy
        secret = "{secret}"
        #A list of approved proxy hostnames or IP addresses. If the connecting proxy's hostname or IP isn't in this list, the player will be disconnected. Leave empty to allow all.
        approvedProxyHosts = []

#CrossStitch Settings - For Wrapping Modded Command Arguments
[crossStitch]
        enabled = true
        forceWrappedArguments = []
        forceWrapVanillaArguments = false

#Debug Settings
[debug]
        enabled = false
        disabledMixins = []

#Advanced Settings
[advanced]
        modernForwardingVersion = "NO_OVERRIDE"
"""


def write_default_pcf(path: Path, secret: str) -> None:
    """Create a fresh PCF file at ``path`` with ``secret`` embedded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, _DEFAULT_PCF_TEMPLATE.format(secret=secret), mode=0o600)


def read_pcf_forwarding_secret(path: Path) -> str | None:
    """Return the secret from ``[forwarding]`` in ``path``, or None if absent."""
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_forwarding = False
    for line in lines:
        stripped = line.strip()
        if stripped == _FORWARDING_HEADER:
            in_forwarding = True
            continue
        if in_forwarding and stripped.startswith("[") and stripped.endswith("]"):
            in_forwarding = False
            continue
        if in_forwarding and _SECRET_LINE.match(line):
            _, _, value = line.partition("=")
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                return value[1:-1]
            return value
    return None


def update_pcf_forwarding_secret(path: Path, secret: str) -> bool:
    """Ensure the ``[forwarding]`` secret in ``path`` matches ``secret``.

    Creates the file with the default template if missing. Preserves
    comments, indentation, and line endings outside the secret line.
    Returns True iff the file changed.
    """
    if not path.exists():
        write_default_pcf(path, secret)
        return True
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"could not read {path}: {exc}") from exc

    new_text, changed = _rewrite_secret_line(original, secret)
    if not changed:
        return False
    _write_atomic(path, new_text, mode=0o600)
    return True


def _rewrite_secret_line(text: str, secret: str) -> tuple[str, bool]:
    """Return ``(new_text, changed)``: splice a new secret into the file."""
    lines = text.splitlines(keepends=True)
    in_forwarding = False
    found = False
    changed = False
    out: list[str] = []

    for line in lines:
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        stripped = body.strip()

        if stripped == _FORWARDING_HEADER:
            in_forwarding = True
            out.append(line)
            continue

        if in_forwarding and stripped.startswith("[") and stripped.endswith("]"):
            in_forwarding = False
            out.append(line)
            continue

        if in_forwarding and _SECRET_LINE.match(body):
            indent = body[: len(body) - len(body.lstrip())]
            new_body = f'{indent}secret = "{secret}"'
            if new_body != body:
                changed = True
            out.append(new_body + eol)
            found = True
            continue

        out.append(line)

    if not found:
        for i, line in enumerate(out):
            body = line.rstrip("\r\n")
            if body.strip() == _FORWARDING_HEADER:
                eol = line[len(body) :] or "\n"
                out.insert(i + 1, f'secret = "{secret}"{eol}')
                changed = True
                break
        else:
            if out and not out[-1].endswith(("\n", "\r")):
                out[-1] = out[-1] + "\n"
            out.append(f"{_FORWARDING_HEADER}\n")
            out.append(f'secret = "{secret}"\n')
            changed = True

    return "".join(out), changed


def _write_atomic(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
