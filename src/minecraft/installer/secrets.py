# src/minecraft/installer/secrets.py

"""Secret generation, reading, writing, and rotation.

Every secret is a lowercase-hex string produced by ``secrets.token_hex``.
Files are written with mode 0600 and atomic replace so a crash mid-write
never leaves a partially-written credential on disk.
"""

from __future__ import annotations

import contextlib
import os
import secrets as _secrets
import tempfile
from pathlib import Path

from .errors import SecretsError

__all__ = [
    "ensure_secret_file",
    "generate_hex_secret",
    "read_secret_file",
    "write_secret_file",
]

_SECRET_BYTES = 32


def generate_hex_secret(nbytes: int = _SECRET_BYTES) -> str:
    """Return a 2*nbytes-character lowercase hex secret from a CSPRNG."""
    if nbytes <= 0:
        raise SecretsError(f"nbytes must be positive, got {nbytes}")
    return _secrets.token_hex(nbytes)


def read_secret_file(path: Path) -> str | None:
    """Read a secret file; return the stripped content, or None if absent.

    Raises SecretsError if the file exists but is empty, unreadable, or
    does not contain a valid hex-only secret.
    """
    if not path.exists():
        return None
    if not path.is_file():
        raise SecretsError(f"secret path exists but is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"could not read {path}: {exc}") from exc
    value = text.strip()
    if not value:
        raise SecretsError(f"secret file is empty: {path}")
    if not _is_valid_hex(value):
        raise SecretsError(f"secret file contains non-hex characters (expected [0-9a-fA-F]+): {path}")
    return value


def write_secret_file(path: Path, secret: str) -> None:
    """Atomically write ``secret`` to ``path`` with 0600 permissions."""
    if not secret:
        raise SecretsError("cannot write an empty secret")
    if not _is_valid_hex(secret):
        raise SecretsError("secret is not valid lowercase hex")
    _write_atomic(path, secret + "\n", mode=0o600)


def ensure_secret_file(path: Path, *, rotate: bool) -> tuple[str, bool]:
    """Ensure ``path`` holds a valid secret.

    Returns ``(secret, changed)``:
      * ``changed`` is True when a new secret was generated (either
        because ``rotate`` is True or because the file did not exist).
      * ``changed`` is False when the existing secret was reused.
    """
    if rotate:
        new_secret = generate_hex_secret()
        write_secret_file(path, new_secret)
        return new_secret, True
    existing = read_secret_file(path)
    if existing is not None:
        return existing, False
    new_secret = generate_hex_secret()
    write_secret_file(path, new_secret)
    return new_secret, True


def _is_valid_hex(value: str) -> bool:
    if len(value) < 2:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value)


def _write_atomic(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
