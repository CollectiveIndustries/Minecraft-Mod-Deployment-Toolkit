# tests/deploy_pack/test_main.py

"""Tests for deploy_pack.main, Project_Specs.md §2.4, §2.5, §2.7, §7.1, §9.2.

Coverage:

  * §2.5 - the argument behavior matrix
  * §2.4 - the exit-code contract: 0 success, 1 runtime, 2 usage, 3 config
  * §2.4 - error precedence: exit-2 checks run before config load
  * §2.7 - the --audit-mods combination rules

Every test drives ``main()`` end-to-end with ``argv``. No private
helper is imported; the CLI surface is the only contract.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

import minecraft.deploy_pack.prompt_ui as prompt_ui_module
from minecraft.deploy_pack.main import main


def _check(argv: list[str]) -> tuple[int, str, str]:
    """Run ``main(argv)`` and return (exit_code, stdout, stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return (code, out.getvalue(), err.getvalue())


def _write_minimal_repo(tmp_path: Path) -> Path:
    """Create a minimal valid config.d/ tree. Return the config.d path."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (tmp_path / "docker-compose.yml").write_text(
        "\nservices:\n"
        "  mc-survival:\n"
        "    container_name: mc-survival\n"
        '    healthcheck:\n      test: ["CMD", "true"]\n'
        "    volumes:\n"
        "      - ./data/survival:/data\n"
        "      - ./shared/mods:/data/mods\n"
        "  nginx:\n"
        "    container_name: nginx\n"
        "    volumes:\n"
        "      - ./www:/usr/share/nginx/html\n",
        encoding="utf-8",
    )
    (config_dir / "deploy_pack.toml").write_text(
        "\n"
        'instance_discovery = "explicit"\n'
        'sync_root    = "./sync"\n'
        'modpack_dir  = "./sync/downloads"\n'
        'output_filename   = "minecraft_client_{date}.zip"\n'
        'download_base_url = "http://minecraft/downloads"\n'
        "\n[sync_mapping]\n"
        'config = "config"\n'
        'kubejs = "kubejs"\n'
        "\n[docker]\n"
        'compose_file = "./docker-compose.yml"\n'
        "\n[instances.survival]\n"
        'container = "mc-survival"\n'
        'config_mode = "merge"\n'
        'kubejs_mode = "delete"\n',
        encoding="utf-8",
    )
    return config_dir


# ---------------------------------------------------------------------------
# §2.5: no arguments and help
# ---------------------------------------------------------------------------


def test_no_arguments_prints_help_and_exits_0() -> None:
    """§2.5: no arguments prints help and exits 0."""
    code, out, err = _check([])
    assert code == 0
    assert "usage" in (out + err).lower()


def test_help_flag_prints_help_and_exits_0() -> None:
    """§2.5: --help prints help and exits 0."""
    code, out, err = _check(["--help"])
    assert code == 0
    assert "usage" in (out + err).lower()


def test_non_interactive_alone_prints_help_and_exits_0() -> None:
    """§2.5: --non-interactive alone prints help and exits 0."""
    code, out, err = _check(["--non-interactive"])
    assert code == 0
    assert "usage" in (out + err).lower()


# ---------------------------------------------------------------------------
# §2.5: exit-2 argument matrix
# ---------------------------------------------------------------------------


def test_dry_run_without_scope_is_exit_2() -> None:
    """§2.5: --dry-run without a scope is exit 2."""
    code, _out, err = _check(["--dry-run"])
    assert code == 2
    assert "scope" in err.lower()


def test_with_resources_without_client_or_full_is_exit_2() -> None:
    """§2.5: --with-resources requires --client or --full."""
    code, _out, err = _check(["--server", "--with-resources"])
    assert code == 2
    assert "with-resources" in err.lower()


def test_full_and_instance_are_mutually_exclusive() -> None:
    """§2.5: --full and --instance are mutually exclusive."""
    code, _out, err = _check(["--full", "--instance", "survival"])
    assert code == 2
    assert "mutually exclusive" in err


def test_instance_without_server_or_resource_pack_is_exit_2() -> None:
    """§2.5: --instance requires --server or --resource-pack."""
    code, _out, err = _check(["--instance", "survival"])
    assert code == 2
    assert "instance" in err.lower()


def test_client_with_instance_is_exit_2() -> None:
    """§2.5: --client --instance X is exit 2."""
    code, _out, err = _check(["--client", "--instance", "survival"])
    assert code == 2
    assert "instance" in err.lower()


def test_debug_deps_with_dry_run_without_scope_is_exit_2() -> None:
    """§2.5: --debug-deps --dry-run without a scope is exit 2."""
    code, _out, _err = _check(["--debug-deps", "--dry-run"])
    assert code == 2


def test_unknown_flag_is_exit_2() -> None:
    """§2.5: an unknown flag is a usage error."""
    code, _out, _err = _check(["--not-a-flag"])
    assert code == 2


# ---------------------------------------------------------------------------
# §2.7: --audit-mods combination rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--server",
        "--client",
        "--resource-pack",
        "--full",
        "--dry-run",
        "--with-resources",
        "--notify",
        "--debug-deps",
        "--non-interactive",
    ],
)
def test_audit_mods_rejects_incompatible_flags(flag: str) -> None:
    """§2.7: --audit-mods rejects scope, notify, dry-run, and modifier flags."""
    code, _out, err = _check(["--audit-mods", flag])
    assert code == 2
    assert "audit-mods" in err


def test_audit_mods_without_textual_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.7: --audit-mods requires the textual package."""
    config_dir = _write_minimal_repo(tmp_path)
    monkeypatch.setattr(prompt_ui_module, "HAS_TEXTUAL", False)
    code, _out, err = _check(["--audit-mods", "--config-dir", str(config_dir)])
    assert code == 1
    assert "textual" in err


# ---------------------------------------------------------------------------
# §2.5: no-op cases that exit 0
# ---------------------------------------------------------------------------


def test_debug_deps_without_scope_prints_diagnostics_and_exits_0(tmp_path: Path) -> None:
    """§2.5: --debug-deps without a scope prints the closure and exits 0."""
    config_dir = _write_minimal_repo(tmp_path)
    code, _out, _err = _check(["--debug-deps", "--config-dir", str(config_dir)])
    assert code == 0


def test_notify_without_scope_attempts_diagnostic_and_exits_0(tmp_path: Path) -> None:
    """§2.5: --notify without a scope attempts the diagnostic message and exits 0."""
    config_dir = _write_minimal_repo(tmp_path)
    code, _out, _err = _check(["--notify", "--config-dir", str(config_dir)])
    assert code == 0


def test_debug_deps_and_notify_without_scope_exits_0(tmp_path: Path) -> None:
    """§2.5: --debug-deps --notify without a scope prints closure and attempts diagnostic."""
    config_dir = _write_minimal_repo(tmp_path)
    code, _out, _err = _check(["--debug-deps", "--notify", "--config-dir", str(config_dir)])
    assert code == 0


# ---------------------------------------------------------------------------
# §2.4: exit-2 checks precede config load
# ---------------------------------------------------------------------------


def test_usage_error_precedes_missing_config_dir(tmp_path: Path) -> None:
    """§2.4: an exit-2 check runs before config load, so a bad path does not matter."""
    nonexistent = tmp_path / "does-not-exist"
    code, _out, err = _check(["--dry-run", "--config-dir", str(nonexistent)])
    assert code == 2
    assert "scope" in err.lower()


# ---------------------------------------------------------------------------
# §2.4: exit-3 for configuration failure
# ---------------------------------------------------------------------------


def test_missing_config_dir_is_exit_3(tmp_path: Path) -> None:
    """§2.4: a missing config tree is a configuration failure, exit 3."""
    nonexistent = tmp_path / "does-not-exist"
    code, _out, _err = _check(["--client", "--config-dir", str(nonexistent)])
    assert code == 3


def test_invalid_output_filename_is_exit_3(tmp_path: Path) -> None:
    """§7.1: a bad output_filename is exit 3."""
    config_dir = _write_minimal_repo(tmp_path)
    toml = config_dir / "deploy_pack.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            'output_filename   = "minecraft_client_{date}.zip"',
            'output_filename   = "pack.tar.gz"',
        ),
        encoding="utf-8",
    )
    code, _out, _err = _check(["--client", "--config-dir", str(config_dir)])
    assert code == 3


# ---------------------------------------------------------------------------
# §2.1: redundant but valid combinations
# ---------------------------------------------------------------------------


def test_full_with_server_is_accepted(tmp_path: Path) -> None:
    """§2.1: --full subsumes --server; passing both is redundant but valid."""
    config_dir = _write_minimal_repo(tmp_path)
    code, _out, _err = _check(["--full", "--server", "--dry-run", "--config-dir", str(config_dir)])
    assert code != 2


def test_full_with_with_resources_is_accepted(tmp_path: Path) -> None:
    """§2.1: --full --with-resources is valid and redundant."""
    config_dir = _write_minimal_repo(tmp_path)
    code, _out, _err = _check(["--full", "--with-resources", "--dry-run", "--config-dir", str(config_dir)])
    assert code != 2
