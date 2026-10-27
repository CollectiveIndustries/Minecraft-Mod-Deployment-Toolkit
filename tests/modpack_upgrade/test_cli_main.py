# tests/modpack_upgrade/test_cli_main.py

"""CLI entry-point dispatch and exit-code tests."""

from __future__ import annotations

from pathlib import Path

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
# Previously-stubbed commands are now implemented
# ---------------------------------------------------------------------------
#
# ``build-client``, ``snapshot-world``, and both ``validate`` subcommands
# were dispatched to ``stubs.not_implemented`` through the T3b/T3c era. The
# final round of the upgrade-toolchain build-out replaced those stubs with
# real handlers. This test confirms the stub path is gone: each command now
# produces a meaningful exit-3 config error against a minimal sandbox,
# rather than exit 1 with "not yet implemented".


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["build-client"], id="build-client"),
        pytest.param(["snapshot-world"], id="snapshot-world"),
        pytest.param(["validate", "migration"], id="validate-migration"),
        pytest.param(["validate", "velocity"], id="validate-velocity"),
    ],
)
def test_previously_stubbed_commands_are_implemented(
    argv: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The four commands previously stubbed now reach real handlers.

    Against a minimal sandbox each produces a specific exit-3 config error
    (missing candidate / missing config / missing compose). None emits the
    old "not yet implemented" message.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{(tmp_path / "state").as_posix()}"\n',
        encoding="utf-8",
    )

    code = main(["--config-dir", str(config_dir), *argv])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not yet implemented" not in combined, f"stub marker still emitted by {argv!r}"
    assert code == 3, f"{argv!r} should exit 3 with config error; got {code}"


# ---------------------------------------------------------------------------
# build-inhouse (implemented in Slice 14; no longer a stub)
# ---------------------------------------------------------------------------


def test_build_inhouse_missing_spec_exits_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """``build-inhouse`` with no spec file exits 3 (not the stub 1)."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    code = main(["--config-dir", str(config_dir), "build-inhouse", "missing"])
    assert code == 3
    captured = capsys.readouterr()
    assert "build spec error" in captured.err
    assert "not yet implemented" not in captured.err


# ---------------------------------------------------------------------------
# Implemented resolver commands (require flags; enforce their own preconditions)
# ---------------------------------------------------------------------------


def test_update_requires_release_mode(capsys: pytest.CaptureFixture[str]):
    """`update` requires --release-mode; argparse exits 2."""
    code = main(["update"])
    assert code == 2


def test_update_accepts_release_mode_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`update --release-mode upgrade` parses and reaches the update command.

    Without a project tree the command fails at manifest load (exit 3).
    Full happy-path coverage lives in test_update_command.py.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    code = main(["--config-dir", str(config_dir), "update", "--release-mode", "upgrade"])
    assert code == 3
    captured = capsys.readouterr()
    assert "manifest" in captured.err.lower()


def test_materialize_requires_run_id(capsys: pytest.CaptureFixture[str]):
    """`materialize` requires --run-id; argparse exits 2."""
    code = main(["materialize"])
    assert code == 2


def test_iterate_requires_run_id(capsys: pytest.CaptureFixture[str]):
    """`iterate` requires --run-id; argparse exits 2."""
    code = main(["iterate"])
    assert code == 2


def test_promote_without_candidate_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`promote` exits 3 when no materialized candidate exists."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "promote", "v1.0.2"])
    assert code == 3
    assert "no materialized candidate" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_requires_release_id(capsys: pytest.CaptureFixture[str]):
    """`publish` without a release-id argument exits 2 (usage)."""
    code = main(["publish"])
    assert code == 2


def test_publish_missing_slip_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`publish` with no slip for the release-id exits 3.

    Also pins the precondition ordering in ``publish.run``: slip
    verification runs BEFORE signing-key loading, so a missing slip is
    reported even when the signing env var is also unset. The current
    test environment has no ``RELEASE_SIGNING_KEY``, and this test still
    sees "slip not found" -- if a future refactor moves key-load before
    slip-check, this test would see the key error instead.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "publish", "v1.0.0"])
    assert code == 3
    captured = capsys.readouterr()
    assert "slip not found" in captured.err


def test_publish_verify_only_without_trusted_keys_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """`publish --verify-only` with no trusted keys file exits 3.

    --verify-only skips the signing key entirely; it only needs the
    trusted public keys. Without ``config.d/trusted_keys.toml`` (and no
    ``[registry].trusted_keys_path`` override), the load fails with a
    clear "trusted keys file not found" message before chain walking.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{tmp_path / "state"}"\n',
        encoding="utf-8",
    )
    code = main(["--config-dir", str(config_dir), "publish", "v1.0.0", "--verify-only"])
    assert code == 3
    captured = capsys.readouterr()
    assert "trusted keys file not found" in captured.err
