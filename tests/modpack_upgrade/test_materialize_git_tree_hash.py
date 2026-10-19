# tests/modpack_upgrade/test_materialize_git_tree_hash.py

"""Tests for ``materialize._git_tree_hash`` (Slice 9 Turn C2).

After migration to ``PlatformCore.git.path_object_id``, ``_git_tree_hash``
is a thin application adapter with a fail-open contract:

* **Success:** return the git object ID (40-char blob hash for a file,
  40-char tree hash for a directory) via ``git rev-parse HEAD:<path>``.
* **Failure:** return ``"0" * 64`` -- on any of:
    - ``CalledProcessError`` (rev not found, path not in tree, not a repo)
    - ``FileNotFoundError`` (git binary missing)
    - ``subprocess.TimeoutExpired`` (git timed out)
    - empty stdout (defensive; treated the same as failure)

The fail-open behavior is intentional and asymmetric with ``promote``:
materialize runs on fresh candidate directories that may legitimately
have no git metadata (a candidate built outside a git checkout). The
tree hashes in the slip are *descriptive*, not *enforced* -- a
candidate with ``"0" * 64`` tree hashes is still a valid, loadable
slip. ``promote`` conversely requires a working git environment because
the whole point of promote is to stage a git commit
(``promote._git_blob_hash`` raises ``UpgradeCliError(3)`` on any failure).

These tests exercise both paths directly. The two happy-path tests use a
real ``tmp_path`` git repository (no mocks for the success path); the
four fail-open tests monkeypatch ``materialize.path_object_id`` to force
each failure mode without shelling out. Verification in the happy-path
tests uses raw ``subprocess.run`` for ground truth, so a bug in the
wrapper cannot mask itself.

The existing 53 tests in ``test_materialize.py`` are unchanged; this
file is a companion that isolates the git-tree-hash concern, following
the same pattern as ``test_subprocess_git_timeout.py`` in CollectiveCores.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands import materialize as materialize_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_git(args: list[str], cwd: Path) -> str:
    """Run git directly (not through PlatformCore) for verification."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_git_repo(repo: Path) -> None:
    """Initialize a real git repo with a local identity (no global config needed)."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _commit_file(repo: Path, relpath: str, content: bytes = b"test") -> None:
    """Write ``relpath`` under ``repo`` and commit it."""
    f = repo / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    subprocess.run(["git", "add", "--", relpath], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    """Provide a real, initialized git repo under tmp_path."""
    repo = tmp_path / "git-repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


# ---------------------------------------------------------------------------
# Success path (real git)
# ---------------------------------------------------------------------------


def test_git_tree_hash_returns_real_blob_hash(real_git_repo: Path) -> None:
    """For a committed file, returns the blob ID at HEAD."""
    _commit_file(real_git_repo, "tracked.txt", b"committed content")

    wrapper_hash = materialize_mod._git_tree_hash(real_git_repo, "tracked.txt")

    raw = _raw_git(["rev-parse", "HEAD:tracked.txt"], real_git_repo).strip()
    assert wrapper_hash == raw
    assert len(wrapper_hash) == 40
    # Blob, not tree.
    obj_type = _raw_git(["cat-file", "-t", wrapper_hash], real_git_repo).strip()
    assert obj_type == "blob"


def test_git_tree_hash_returns_real_tree_hash_for_directory(real_git_repo: Path) -> None:
    """For a committed directory, returns the tree ID at HEAD.

    ``git rev-parse HEAD:<path>`` returns a tree object ID for a
    directory, not a blob. This confirms the function's name -- it is
    not a "file hash" helper, it is a "git object ID at a path" helper.
    """
    _commit_file(real_git_repo, "sub/inner.txt", b"x")

    wrapper_hash = materialize_mod._git_tree_hash(real_git_repo, "sub")

    raw = _raw_git(["rev-parse", "HEAD:sub"], real_git_repo).strip()
    assert wrapper_hash == raw
    obj_type = _raw_git(["cat-file", "-t", wrapper_hash], real_git_repo).strip()
    assert obj_type == "tree"


# ---------------------------------------------------------------------------
# Fail-open paths (mock PlatformCore.git.path_object_id)
# ---------------------------------------------------------------------------
#
# Four failure modes, each translated to the "0" * 64 placeholder. The
# tests monkeypatch ``materialize.path_object_id`` (the re-import) so
# they exercise the exact exception-to-placeholder translation in
# ``_git_tree_hash`` without depending on real git failure conditions.


_PLACEHOLDER = "0" * 64


def test_git_tree_hash_returns_placeholder_on_calledprocesserror(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``CalledProcessError`` (rev not found / path not in tree) -> placeholder.

    This is the most likely real-world failure: a fresh candidate
    directory has no ``HEAD`` yet, so ``git rev-parse HEAD:<path>``
    exits non-zero. Materialize must not fail because of it -- the tree
    hashes are descriptive.
    """

    def _raise(*args: object, **kwargs: object) -> str:
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "rev-parse", "HEAD:anything"],
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr(materialize_mod, "path_object_id", _raise)

    assert materialize_mod._git_tree_hash(real_git_repo, "anything.txt") == _PLACEHOLDER


def test_git_tree_hash_returns_placeholder_on_filenotfound(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary (FileNotFoundError) -> placeholder.

    Materialize is useful in environments where git is not installed
    (a minimal CI image, a container without git, an air-gapped
    operations host). The slip is still valid; only the tree hashes are
    placeholders.
    """

    def _raise(*args: object, **kwargs: object) -> str:
        raise FileNotFoundError("git binary not on PATH")

    monkeypatch.setattr(materialize_mod, "path_object_id", _raise)

    assert materialize_mod._git_tree_hash(real_git_repo, "anything.txt") == _PLACEHOLDER


def test_git_tree_hash_returns_placeholder_on_timeout(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout (TimeoutExpired) -> placeholder.

    A pathological network-mounted repo with a stalled filesystem should
    not hang the whole materialize run. The 10-second timeout on
    ``path_object_id`` (passed from ``_git_tree_hash``) bounds it; on
    expiry the placeholder is returned.
    """

    def _raise(*args: object, **kwargs: object) -> str:
        raise subprocess.TimeoutExpired(cmd=["git", "rev-parse"], timeout=10)

    monkeypatch.setattr(materialize_mod, "path_object_id", _raise)

    assert materialize_mod._git_tree_hash(real_git_repo, "anything.txt") == _PLACEHOLDER


def test_git_tree_hash_returns_placeholder_on_empty_output(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty hash result (defensive) -> placeholder.

    ``path_object_id`` returns ``stdout.strip()``; if git were to emit
    an empty line (should not happen, but the contract is "non-empty or
    raise"), the wrapper treats it as a failure rather than propagating
    an empty string into the slip.
    """
    monkeypatch.setattr(materialize_mod, "path_object_id", lambda *a, **kw: "")

    assert materialize_mod._git_tree_hash(real_git_repo, "anything.txt") == _PLACEHOLDER


# ---------------------------------------------------------------------------
# Signature contract: timeout and rev passed to path_object_id
# ---------------------------------------------------------------------------


def test_git_tree_hash_passes_head_and_timeout_to_path_object_id(real_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper calls path_object_id with rev="HEAD" and timeout=10."""
    captured: dict[str, object] = {}

    def _spy(relpath: str | Path, *, rev: str = "HEAD", repo_path: str = ".", timeout: float | None = None) -> str:
        captured["relpath"] = str(relpath)
        captured["rev"] = rev
        captured["repo_path"] = repo_path
        captured["timeout"] = timeout
        return "a" * 40

    monkeypatch.setattr(materialize_mod, "path_object_id", _spy)

    result = materialize_mod._git_tree_hash(real_git_repo, "sync/config")

    assert result == "a" * 40
    assert captured["relpath"] == "sync/config"
    assert captured["rev"] == "HEAD"
    assert captured["repo_path"] == str(real_git_repo)
    assert captured["timeout"] == 10
