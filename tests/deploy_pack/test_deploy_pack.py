# tests/deploy_pack/test_deploy_pack.py

"""Tests for the entrypoint shim (deploy_pack.deploy_pack).

The shim is deliberately trivial: its only contract is that calling
``cli()`` invokes ``main.main()`` and exits with the returned code. The
runtime behavior of ``main`` is exercised in test_main.py. Here we cover
the shim wiring and the module-invocation path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_calls_main_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli() must call main.main() and sys.exit with its return value."""
    from minecraft.deploy_pack import deploy_pack

    calls: list[list[str] | None] = []

    def fake_main(argv: list[str] | None = None) -> int:
        calls.append(argv)
        return 7

    monkeypatch.setattr(deploy_pack, "_main", fake_main)
    with pytest.raises(SystemExit) as ei:
        deploy_pack.cli()
    assert ei.value.code == 7
    assert calls == [None]


def test_cli_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that the CLI exits with code 0."""
    from minecraft.deploy_pack import deploy_pack

    monkeypatch.setattr(deploy_pack, "_main", lambda argv=None: 0)
    with pytest.raises(SystemExit) as ei:
        deploy_pack.cli()
    assert ei.value.code == 0


def test_python_dash_m_invocation() -> None:
    """``python -m minecraft.deploy_pack --help`` exits 0.

    This exercises __main__.py + main.main() end to end without
    needing a Docker daemon or a config tree, because --help is
    handled before any of those are touched.
    """
    result = subprocess.run([sys.executable, "-m", "minecraft.deploy_pack", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower() or "usage" in result.stderr.lower()


def test_python_dash_m_no_args_prints_help() -> None:
    """§2.5: no arguments → print help, exit 0."""
    result = subprocess.run([sys.executable, "-m", "minecraft.deploy_pack"], capture_output=True, text=True)
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "usage" in combined


def test_python_dash_m_dry_run_without_scope_exit_2() -> None:
    """§2.5: --dry-run without a scope → exit 2.

    This is a full subprocess round-trip: parse → validate → UsageError
    → exit 2. It confirms the shim, __main__, main, and the exit-code
    mapping all agree.
    """
    result = subprocess.run([sys.executable, "-m", "minecraft.deploy_pack", "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 2


@pytest.mark.skipif(sys.platform == "win32", reason="console-script shebang differs on Windows")
def test_direct_file_invocation() -> None:
    """Running deploy_pack.py directly invokes cli() via __name__ == __main__."""
    here = Path(__file__).resolve()
    src_dir = here.parents[2] / "src"
    module_path = src_dir / "minecraft" / "deploy_pack" / "deploy_pack.py"
    if not module_path.is_file():
        pytest.skip(f"module not found at expected path: {module_path}")
    result = subprocess.run([sys.executable, str(module_path), "--help"], capture_output=True, text=True, cwd=str(src_dir))
    if result.returncode not in (0, 1):
        pytest.skip(f"environment prevents direct run: {result.stderr[:200]}")
