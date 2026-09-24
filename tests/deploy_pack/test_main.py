# tests/deploy_pack/test_main.py

"""Tests for deploy_pack.main.

Focus: §2.5's exit-2 matrix, scope resolution, argument parsing, and
the top-level exit-code mapping (§2.4). The full runtime sequence is
exercised through the scope/hook tests; here we cover the outer shell.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from minecraft.deploy_pack.errors import UsageError
from minecraft.deploy_pack.main import _Args, _build_parser, _has_any_work, _parse_instances, _resolve_scopes, _validate_args, main


def test_parse_instances_none() -> None:
    """Tests that None input returns None when parsing instances."""
    assert _parse_instances(None) is None


def test_parse_instances_single() -> None:
    """Tests that a single instance string is parsed into a set containing that instance."""
    assert _parse_instances(["survival"]) == {"survival"}


def test_parse_instances_comma_separated() -> None:
    """Tests that comma-separated instance strings are parsed into a set of individual instances."""
    assert _parse_instances(["a,b"]) == {"a", "b"}


def test_parse_instances_repeated() -> None:
    """Tests parsing instances from a list of distinct values, expecting all entries in the set."""
    assert _parse_instances(["a", "b"]) == {"a", "b"}


def test_parse_instances_mixed() -> None:
    """Tests parsing instances from a mix of comma-separated and single-value strings."""
    assert _parse_instances(["a,b", "c"]) == {"a", "b", "c"}


def test_parse_instances_whitespace() -> None:
    """Tests parsing instances from a whitespace-padded string, expecting trimmed set entries."""
    assert _parse_instances([" a , b "]) == {"a", "b"}


def test_parse_instances_empty_string() -> None:
    """Tests parsing instances from an empty string, expecting an empty set."""
    assert _parse_instances([""]) == set()


def test_scopes_server() -> None:
    """Tests that resolving scopes with server enabled sets only the server scope and disables resources."""
    s = _resolve_scopes(_Args(server=True))
    assert s.scope_set.server
    assert not s.scope_set.client
    assert not s.scope_set.resource_pack
    assert s.with_resources is False


def test_scopes_full() -> None:
    """Tests that resolving scopes with full enabled sets all scopes and enables resources."""
    s = _resolve_scopes(_Args(full=True))
    assert s.scope_set.server
    assert s.scope_set.client
    assert s.scope_set.resource_pack
    assert s.with_resources is True


def test_scopes_with_resources_client() -> None:
    """Tests that resolving scopes with client and with_resources enables the client scope and resource flags."""
    s = _resolve_scopes(_Args(client=True, with_resources=True))
    assert s.scope_set.client
    assert s.with_resources is True


def _check(argv: list[str]) -> tuple[int | None, str | None]:
    """Run main() and return (exit_code, error_message)."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = main(argv)
    return (code, stderr.getvalue())


@pytest.mark.parametrize("argv", [[], ["--help"]])
def test_no_args_help(argv: list[str]) -> None:
    """Tests that running with no arguments exits successfully and prints help."""
    code, _ = _check(argv)
    assert code == 0


def test_non_interactive_alone_prints_help() -> None:
    """Tests that the --non-interactive flag alone exits successfully and prints help."""
    code, _ = _check(["--non-interactive"])
    assert code == 0


def test_dry_run_without_scope_is_exit_2() -> None:
    """Tests that --dry-run without a scope exits with code 2 and reports a scope error."""
    code, err = _check(["--dry-run"])
    assert code == 2
    assert "scope" in err


def test_dry_run_with_scope_passes_argcheck() -> None:
    """Tests that --dry-run with --client passes the argument check without a scope error."""
    code, err = _check(["--dry-run", "--client"])
    assert code != 2 or "--dry-run requires a scope" not in err


def test_with_resources_without_client_is_exit_2() -> None:
    """Tests that --with-resources without --client exits with code 2 and reports an error."""
    code, err = _check(["--server", "--with-resources"])
    assert code == 2
    assert "with-resources" in err.lower()


def test_with_resources_with_client_ok() -> None:
    """Tests that --with-resources is accepted when used together with --client."""
    code, err = _check(["--client", "--with-resources"])
    assert not (code == 2 and "with-resources" in err.lower())


def test_full_with_instance_is_exit_2() -> None:
    """Tests that combining --full with --instance is rejected as mutually exclusive."""
    code, err = _check(["--full", "--instance", "survival"])
    assert code == 2
    assert "mutually exclusive" in err


def test_instance_without_server_or_rp_is_exit_2() -> None:
    """Tests that --instance without --server or --resource-pack exits with code 2 and an instance-related error message."""
    code, err = _check(["--instance", "survival"])
    assert code == 2
    assert "instance" in err.lower()


def test_client_with_instance_is_exit_2() -> None:
    """Tests that using --client with --instance exits with code 2 and an instance-related error message."""
    code, err = _check(["--client", "--instance", "survival"])
    assert code == 2
    assert "instance" in err.lower()


def test_instance_with_server_ok() -> None:
    """Tests that --instance with --server is accepted without an instance-related validation error."""
    code, err = _check(["--server", "--instance", "survival"])
    assert not (code == 2 and "instance" in err.lower())


def test_instance_with_resource_pack_ok() -> None:
    """Tests that --instance with --resource-pack is accepted without an instance-related validation error."""
    code, err = _check(["--resource-pack", "--instance", "survival"])
    assert not (code == 2 and "instance" in err.lower())


def test_full_with_resources_redundant_but_valid() -> None:
    """Tests that combining --full and --with-resources is accepted without a validation error."""
    code, err = _check(["--full", "--with-resources"])
    assert not (code == 2 and "with-resources" in err.lower())


def test_full_with_server_redundant_but_valid() -> None:
    """Tests that combining --full with --server is redundant but does not cause a usage error."""
    code, _err = _check(["--full", "--server"])
    assert code != 2


@pytest.mark.parametrize(
    "flag", ["--server", "--client", "--resource-pack", "--full", "--dry-run", "--with-resources", "--notify", "--debug-deps", "--non-interactive"]
)
def test_audit_mods_rejects_scope_flags(flag: str) -> None:
    """Tests that --audit-mods rejects a provided scope flag with a usage error.

    Args:
        flag: The scope flag expected to be incompatible with --audit-mods.
    """
    code, err = _check(["--audit-mods", flag])
    assert code == 2
    assert "audit-mods" in err


def test_audit_mods_allows_config_dir() -> None:
    """Tests that --audit-mods allows the --config-dir option without an audit-mods usage error."""
    code, err = _check(["--audit-mods", "--config-dir", "/tmp/x"])
    assert not (code == 2 and "audit-mods" in err)


def test_audit_mods_allows_debug() -> None:
    """Tests that audit-mods with debug does not produce a usage error."""
    code, err = _check(["--audit-mods", "--debug"])
    assert not (code == 2 and "audit-mods" in err)


def test_validate_full_and_instance_raises() -> None:
    """Tests that validation raises when both full and instance options are provided."""
    parser = _build_parser()
    with pytest.raises(UsageError):
        _validate_args(_Args(full=True, instance={"a"}), parser)


def test_validate_instance_without_server_raises() -> None:
    """Tests that validation raises when an instance is provided without the server flag."""
    parser = _build_parser()
    with pytest.raises(UsageError):
        _validate_args(_Args(instance={"a"}), parser)


def test_validate_instance_with_server_ok() -> None:
    """Tests that validation passes when an instance is provided with the server flag."""
    parser = _build_parser()
    _validate_args(_Args(server=True, instance={"a"}), parser)


def test_validate_instance_with_resource_pack_ok() -> None:
    """Tests that instance and resource_pack together pass validation."""
    parser = _build_parser()
    _validate_args(_Args(resource_pack=True, instance={"a"}), parser)


def test_validate_client_with_instance_raises() -> None:
    """Tests that specifying both client and instance raises a UsageError."""
    parser = _build_parser()
    with pytest.raises(UsageError):
        _validate_args(_Args(client=True, instance={"a"}), parser)


def test_has_any_work_empty() -> None:
    """Tests that no work is detected when arguments are empty."""
    assert not _has_any_work(_Args())


def test_has_any_work_non_interactive_only() -> None:
    """Tests that _has_any_work returns False when only non_interactive is set."""
    assert not _has_any_work(_Args(non_interactive=True))


def test_has_any_work_with_scope() -> None:
    """Tests that _has_any_work returns True for each non-interactive scope option."""
    assert _has_any_work(_Args(server=True))
    assert _has_any_work(_Args(client=True))
    assert _has_any_work(_Args(resource_pack=True))
    assert _has_any_work(_Args(full=True))


def test_has_any_work_with_debug_deps() -> None:
    """Tests that _has_any_work returns True when debug_deps is enabled."""
    assert _has_any_work(_Args(debug_deps=True))


def test_has_any_work_with_notify() -> None:
    """Tests that _has_any_work returns True when notify is enabled."""
    assert _has_any_work(_Args(notify=True))


def test_has_any_work_with_audit_mods() -> None:
    """Tests that _has_any_work returns True when audit_mods is enabled."""
    assert _has_any_work(_Args(audit_mods=True))


def test_debug_deps_without_scope_exit_0(tmp_path) -> None:
    """--debug-deps without a scope prints closure, exit 0."""
    cfg_dir = tmp_path / "config.d"
    cfg_dir.mkdir()
    (cfg_dir / "deploy_pack.toml").write_text(
        'output_filename = "pack.zip"\ndownload_base_url = "http://x"\nsync_root = "./sync"\nmodpack_dir = "./sync/downloads"\n[sync_mapping]\nconfig = "config"\n',
        encoding="utf-8",
    )
    (tmp_path / "sync" / "downloads" / ".index").mkdir(parents=True)

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(["--debug-deps", "--config-dir", str(cfg_dir)])
    assert code == 0


def test_parser_accepts_all_flags() -> None:
    """Tests that the parser accepts all supported command-line flags."""
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--server",
            "--client",
            "--resource-pack",
            "--notify",
            "--dry-run",
            "--with-resources",
            "--instance",
            "a",
            "--instance",
            "b,c",
            "--debug",
            "--debug-deps",
            "--non-interactive",
            "--config-dir",
            "/x",
        ]
    )
    assert args.server
    assert args.instance == ["a", "b,c"]


def test_parser_full_flag() -> None:
    """Tests that the parser correctly handles the --full flag."""
    parser = _build_parser()
    args = parser.parse_args(["--full"])
    assert args.full
