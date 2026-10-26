# tests/modpack_upgrade/test_cli_no_stubs.py

"""Guard: no `minecraft-upgrade` subcommand is a stub.

Every subcommand documented in spec Section 15.1 is invoked via
`pdm run minecraft-upgrade ...`. If the output contains the stub marker
"is not yet implemented", the test fails with the exact line and exit
code.

One test per command via parametrize. A stub produces exactly one
failure, with a targeted message naming the command, the stub line, and
the exit code. You do not scan a wall of output to find which command
was the problem.

Isolation per case:
  - `--config-dir` points at a sandbox containing a `deploy_pack.toml`
    whose `[daemon]` section redirects state_dir / slip_dir /
    release_store / staging / journal / lock / audit / idempotency into
    tmp_path. Nothing the CLI does can touch real operator state.
  - `MINECRAFT_UPGRADE_NO_DOCKER=1` prevents Docker connections.
  - `run_id="test-run"` / `tag="v0.0.0"` do not exist in the sandbox,
    so commands reach their handler and fail on "not found" (exit 3)
    instead of performing work. Not-a-stub is the correct classification.
  - 30s timeout bounds each case.

What this test does NOT catch:
  - `iterate`'s "production boot driver not wired in this build" error.
    That emits a different string, so it will not match the marker. It
    is a real implementation gap but it is not a `stubs.not_implemented`
    stub, so it belongs in a separate check.
  - Any stub that emits its message only after doing real work first.
    None are currently structured that way; if one appears, extend the
    fixture to reach its handler.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

STUB_MARKER = "is not yet implemented"
TIMEOUT_SECONDS = 30.0

# (id, argv). ids appear in pytest output as `test_no_stub_commands[<id>]`.
# Extending: add a new tuple. Argv needs only enough to reach the handler;
# a stub emits its marker before validating arguments.
COMMANDS: list[tuple[str, list[str]]] = [
    ("manifest-check", ["manifest", "check"]),
    ("runtime-inspect", ["runtime", "inspect"]),
    ("status", ["status"]),
    ("verify", ["verify", "--tag", "v0.0.0"]),
    ("update-update-mode", ["update", "--release-mode", "update"]),
    ("update-upgrade-mode", ["update", "--release-mode", "upgrade"]),
    ("acknowledge", ["acknowledge", "test-run"]),
    ("materialize", ["materialize", "--run-id", "test-run"]),
    ("iterate", ["iterate", "--run-id", "test-run"]),
    ("build-inhouse", ["build-inhouse", "test-name"]),
    ("build-client", ["build-client"]),
    ("snapshot-world", ["snapshot-world", "survival"]),
    ("validate-migration", ["validate", "migration"]),
    ("validate-velocity", ["validate", "velocity"]),
    ("publish", ["publish", "v0.0.0"]),
    ("publish-verify-only", ["publish", "v0.0.0", "--verify-only"]),
    ("promote", ["promote", "v0.0.0"]),
    ("approve", ["approve", "test-run"]),
    ("reject", ["reject", "test-run"]),
]


def _find_project_root() -> Path:
    """Return the directory containing pdm.lock/pyproject.toml.

    Walks up from this file's location so the test works regardless of
    whether pytest was invoked from the project root or a subdirectory.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pdm.lock").is_file() or (parent / "pyproject.toml").is_file():
            return parent
    pytest.fail(
        f"Could not find pdm project root: no pdm.lock or pyproject.toml in any ancestor of {here}. This test requires running inside the minecraft project."
    )


@pytest.fixture(scope="module")
def project_root() -> Path:
    """The pdm project root; cwd for every `pdm run` invocation."""
    return _find_project_root()


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A fully isolated config directory with daemon paths redirected to tmp_path.

    Returns the config_dir itself (what `--config-dir` expects). All
    writes the CLI might attempt land under tmp_path and are cleaned up
    automatically. Real operator state is never touched.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "deploy_pack.toml").write_text(
        "[daemon]\n"
        f'state_dir = "{(tmp_path / "state").as_posix()}"\n'
        f'slip_dir = "{(tmp_path / "slips").as_posix()}"\n'
        f'release_store = "{(tmp_path / "releases").as_posix()}"\n'
        f'staging_root = "{(tmp_path / "staging").as_posix()}"\n'
        f'idempotency_path = "{(tmp_path / "idempotency.sqlite").as_posix()}"\n'
        f'audit_path = "{(tmp_path / "audit.jsonl").as_posix()}"\n'
        f'lock_path = "{(tmp_path / "lock").as_posix()}"\n'
        f'journal_path = "{(tmp_path / "journal").as_posix()}"\n',
        encoding="utf-8",
    )
    (config_dir / "upgrade.toml").write_text("", encoding="utf-8")
    return config_dir


@pytest.mark.parametrize(
    "argv",
    [pytest.param(argv, id=name) for name, argv in COMMANDS],
)
def test_no_stub_commands(
    argv: list[str],
    project_root: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The given subcommand must not emit the stub marker.

    Subprocess is used deliberately: it exercises the real entrypoint
    (exactly what a user runs), isolates `sys.exit()` and logging state
    from one case to the next, and survives commands that crash.
    """
    monkeypatch.setenv("MINECRAFT_UPGRADE_NO_DOCKER", "1")

    invocation = [
        "pdm",
        "run",
        "minecraft-upgrade",
        "--config-dir",
        str(sandbox),
        *argv,
    ]

    try:
        result = subprocess.run(
            invocation,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"Command timed out after {TIMEOUT_SECONDS:.0f}s: "
            f"`pdm run minecraft-upgrade {' '.join(argv)}`.\n"
            f"This is not a stub failure; the command is hanging. "
            f"Investigate separately before treating this as green."
        )

    combined = result.stdout + "\n" + result.stderr
    stub_lines = [line.strip() for line in combined.splitlines() if STUB_MARKER in line]

    if stub_lines:
        first = stub_lines[0]
        pytest.fail(
            f"`minecraft-upgrade {' '.join(argv)}` is a stub.\n"
            f"  Emitted:   {first}\n"
            f"  Exit code: {result.returncode}\n"
            f"\n"
            f"This command appears in spec Section 15.1 as a documented "
            f"subcommand. Implement it, or remove it from the CLI surface "
            f"until it lands."
        )
