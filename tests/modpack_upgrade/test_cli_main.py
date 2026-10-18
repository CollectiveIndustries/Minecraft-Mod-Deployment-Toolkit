# tests/modpack_upgrade/test_cli_main.py

"""CLI entry-point dispatch and exit-code tests."""

from __future__ import annotations

import pytest

from minecraft.modpack_upgrade.cli.main import main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]):
    """--help prints the parser help and exits 0."""
    code = main(["--help"])
    assert code == 0
    captured = capsys.readouterr()
    assert "minecraft-upgrade" in captured.out
    assert "verify" in captured.out
    assert "status" in captured.out


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]):
    """--version prints the version and exits 0."""
    code = main(["--version"])
    assert code == 0
    captured = capsys.readouterr()
    assert "minecraft-upgrade 1.3.0" in captured.out


def test_no_args_exits_two(capsys: pytest.CaptureFixture[str]):
    """No arguments produces a usage error (exit 2)."""
    code = main([])
    assert code == 2
    captured = capsys.readouterr()
    assert "usage" in (captured.err + captured.out).lower()


def test_unknown_command_exits_two(capsys: pytest.CaptureFixture[str]):
    """An unknown subcommand produces a usage error (exit 2)."""
    code = main(["bogus"])
    assert code == 2


def test_verify_requires_tag(capsys: pytest.CaptureFixture[str]):
    """Verify without --tag exits 2."""
    code = main(["verify"])
    assert code == 2


def test_runtime_requires_action(capsys: pytest.CaptureFixture[str]):
    """Runtime without a subaction exits 2."""
    code = main(["runtime"])
    assert code == 2


def test_manifest_requires_action(capsys: pytest.CaptureFixture[str]):
    """Manifest without a subaction exits 2."""
    code = main(["manifest"])
    assert code == 2


def test_validate_requires_action(capsys: pytest.CaptureFixture[str]):
    """Validate without a subaction exits 2."""
    code = main(["validate"])
    assert code == 2


def test_runtime_help_exits_zero(capsys: pytest.CaptureFixture[str]):
    """Runtime --help exits 0."""
    code = main(["runtime", "--help"])
    assert code == 0


def test_verify_help_exits_zero(capsys: pytest.CaptureFixture[str]):
    """Verify --help exits 0."""
    code = main(["verify", "--help"])
    assert code == 0


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["update"],
        ["materialize"],
        ["iterate"],
        ["build-client"],
        ["snapshot-world"],
        ["build-inhouse", "mymod", "abc123"],
        ["validate", "migration"],
        ["validate", "velocity"],
        ["publish", "v1.0.2"],
        ["promote", "v1.0.2"],
    ],
)
def test_stubs_exit_one(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
):
    """Every stub command exits 1 with a 'not yet implemented' message."""
    code = main(argv)
    assert code == 1
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.err
