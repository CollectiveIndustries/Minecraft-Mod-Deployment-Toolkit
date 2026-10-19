# tests/modpack_upgrade/test_publish.py

"""Tests for ``minecraft-upgrade publish`` (Slice 10, complete + cleanup).

Full pipeline exercised end-to-end:

  * T1 preconditions: slip exists / verifies / release_id matches / no
    existing bundle
  * T2 preconditions: every artifact in warehouse with matching SHA-256
    and safe filename
  * T3: staging assembled (slip + config/ + kubejs/ + artifacts/);
    symlinks refused
  * T4: three manifest JSON files written, verified against disk,
    atomic commit; staging discarded
  * T5: approval.json written from the materialized run's
    acknowledgment.json (or auto-generated for loss-free runs);
    bundle-manifest extended with approval_sha256 /
    release_tag_object_sha / release_commit_sha; registry entry
    appended with a chained entry_hash

Exit 0 on success; exit 3 on preconditions / assembly / manifest / git
failures; exit 7 on registry append failure with best-effort bundle
rollback.

Cleanup turn: every defensive branch in ``_find_materialized_run_for_slip``,
``_last_entry_hash``, ``_resolve_git_refs``, and ``_append_registry_entry``
has a targeted test. These are branches that fire when the world is
broken (malformed state, unreadable registry, git unavailable,
unexpected tag SHA) -- the exact inputs most likely to be untested in
practice and most likely to hide a traceback behind a user-facing
message.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import tomli_w

from minecraft.modpack_upgrade import slip as slip_mod
from minecraft.modpack_upgrade.cli.commands import publish as publish_mod
from minecraft.modpack_upgrade.cli.commands.publish import run
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.slip import parse_slip
from minecraft.modpack_upgrade.state import RunState, save_state

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _hex(ch: str) -> str:
    """Return a 64-char hex string of repeated ``ch``."""
    return ch * 64


def _sha256(data: bytes) -> str:
    """Return the real SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


_DEFAULT_JAR_CONTENT = b"default-jar-content"


def _jar_entry(filename: str, content: bytes) -> dict:
    """Build a schema-valid ``[[jar]]`` entry with the real hash of ``content``."""
    return {
        "filename": filename,
        "sha256": _sha256(content),
        "side": "both",
        "provider": "modrinth",
        "pw_toml": f"{filename.removesuffix('.jar')}.pw.toml",
        "pw_toml_sha256": _hex("c"),
        "dependencies": [],
    }


def _rp_entry(filename: str, content: bytes) -> dict:
    """Build a schema-valid ``[[resource_pack]]`` entry."""
    return {
        "filename": filename,
        "sha256": _sha256(content),
        "instance": "survival",
        "required": True,
        "prompt": "",
    }


def _vp_entry(filename: str, content: bytes) -> dict:
    """Build a schema-valid ``[[velocity_plugin]]`` entry."""
    return {"filename": filename, "sha256": _sha256(content)}


def _build_release_slip_raw(
    *,
    release_id: str = "v1.0.0",
    jar_entries: list[dict] | None = None,
    resource_pack_entries: list[dict] | None = None,
    velocity_plugin_entries: list[dict] | None = None,
    resolution_inputs_sha256: str | None = None,
) -> dict:
    """Build a valid release slip dict with a fresh ``content_sha256``.

    ``resolution_inputs_sha256`` overrides the default fingerprint so
    tests that publish twice can disambiguate the materialized runs.
    """
    if jar_entries is None:
        jar_entries = [_jar_entry("mod-v2.jar", _DEFAULT_JAR_CONTENT)]

    raw = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": "upgrade",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "minecraft-upgrade 1.3.1",
            "author": "test",
        },
        "runtime": {
            "minecraft_version": "1.21.1",
            "loader_type": "forge",
            "loader_version": "52.0.1",
            "image_reference": "itzg/minecraft-server:java25",
            "image_digest": "sha256:" + _hex("1"),
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": _hex("2"),
                "kubejs_tree": _hex("3"),
                "compose_tree": _hex("4"),
                "deploy_toml_tree": _hex("5"),
            },
        },
        "fingerprints": {
            "manifest_sha256": _hex("6"),
            "resolution_inputs_sha256": resolution_inputs_sha256 or _hex("7"),
            "runtime_sha256": _hex("8"),
            "server_inputs_sha256": _hex("9"),
            "client_inputs_sha256": _hex("a"),
        },
        "jar": jar_entries,
        "resource_pack": resource_pack_entries or [],
        "velocity_plugin": velocity_plugin_entries or [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": ["both", "server"],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    raw["content_sha256"] = slip_mod.compute_content_sha256(raw)
    return raw


def _init_git_repo(repo: Path, release_id: str, *, annotated: bool = True) -> None:
    """Create a git repo at ``repo`` with one commit and a tag ``release_id``."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    if annotated:
        subprocess.run(
            ["git", "tag", "-a", release_id, "-m", "release"],
            cwd=repo,
            check=True,
        )
    else:
        subprocess.run(["git", "tag", release_id], cwd=repo, check=True)


def _write_materialized_run(
    state_dir: Path,
    *,
    run_id: str,
    resolution_inputs_sha256: str,
    release_mode: str = "upgrade",
    status: str = "materialized",
    acknowledgment: dict | None = None,
) -> None:
    """Create ``<state_dir>/runs/<run_id>/state.toml`` (+ optional acknowledgment.json)."""
    run_dir = state_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = RunState(
        run_id=run_id,
        status=status,
        release_mode=release_mode,
        paused_at_utc="2026-01-01T00:00:00Z",
        report_sha256=_hex("f"),
        resolution_inputs_sha256=resolution_inputs_sha256,
        manifest_path=str(state_dir.parent / "sync" / "modpack.manifest.toml"),
        effective_compose_sha256=(_hex("c") if release_mode == "update" else None),
    )
    save_state(run_dir / "state.toml", state)
    if acknowledgment is not None:
        (run_dir / "acknowledgment.json").write_text(json.dumps(acknowledgment, indent=2) + "\n", encoding="utf-8")


def _setup_publish_env(
    tmp_path: Path,
    *,
    release_id: str = "v1.0.0",
    slip_raw: dict | None = None,
    write_slip: bool = True,
    warehouse_files: dict[str, bytes] | None = None,
    create_config: bool = True,
    create_kubejs: bool = True,
    config_files: dict[str, bytes] | None = None,
    kubejs_files: dict[str, bytes] | None = None,
    create_run: bool = True,
    run_id: str = "run-abc",
    run_status: str = "materialized",
    acknowledgment: dict | None = None,
    create_git: bool = True,
    tag_as_annotated: bool = True,
) -> tuple[Path, Path, Path]:
    """Set up a project tree ready for ``publish``.

    Returns ``(config_dir, project_root, slip_path)``.
    """
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    (config_dir / "deploy_pack.toml").write_text(
        f'[daemon]\nstate_dir = "{state_dir}"\n',
        encoding="utf-8",
    )

    slip_dir = tmp_path / "sync" / "packing_slips"
    slip_dir.mkdir(parents=True)
    slip_path = slip_dir / f"{release_id}.toml"

    body = slip_raw if slip_raw is not None else _build_release_slip_raw(release_id=release_id)
    if write_slip:
        slip_path.write_text(tomli_w.dumps(body), encoding="utf-8")

    if warehouse_files is None:
        warehouse_files = {"mod-v2.jar": _DEFAULT_JAR_CONTENT}
    warehouse_dir = tmp_path / "sync" / "downloads"
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    for name, content in warehouse_files.items():
        (warehouse_dir / name).write_bytes(content)

    if create_config:
        config_src = tmp_path / "sync" / "config"
        config_src.mkdir(parents=True)
        for name, content in (config_files or {}).items():
            target = config_src / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    if create_kubejs:
        kubejs_src = tmp_path / "sync" / "kubejs"
        kubejs_src.mkdir(parents=True)
        for name, content in (kubejs_files or {}).items():
            target = kubejs_src / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    if create_run:
        _write_materialized_run(
            state_dir,
            run_id=run_id,
            resolution_inputs_sha256=body["fingerprints"]["resolution_inputs_sha256"],
            release_mode=body["release_mode"],
            status=run_status,
            acknowledgment=acknowledgment,
        )

    if create_git:
        _init_git_repo(tmp_path, release_id, annotated=tag_as_annotated)

    return config_dir, tmp_path, slip_path


def _read_manifest(bundle_dir: Path, name: str) -> dict:
    """Read and parse a manifest JSON from ``bundle_dir``."""
    return json.loads((bundle_dir / name).read_text(encoding="utf-8"))


def _read_registry(project_root: Path) -> list[dict]:
    """Return all entries from the registry, in file order."""
    path = project_root / "sync" / "packing_slips" / "release-index.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# T1 preconditions
# ---------------------------------------------------------------------------


def test_run_missing_slip_raises_3(tmp_path: Path) -> None:
    """A missing slip file raises ``UpgradeCliError(3)``."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, write_slip=False)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "slip not found" in str(ei.value)
    assert "v1.0.0.toml" in str(ei.value)


def test_run_malformed_slip_raises_3(tmp_path: Path) -> None:
    """A slip that is not valid TOML raises ``UpgradeCliError(3)``."""
    config_dir, project_root, slip_path = _setup_publish_env(tmp_path, write_slip=False)
    slip_path.write_text("not = valid = toml\n", encoding="utf-8")

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "slip is not verifiable" in str(ei.value)


def test_run_hash_mismatch_raises_3(tmp_path: Path) -> None:
    """A slip whose ``content_sha256`` does not verify raises ``UpgradeCliError(3)``."""
    body = _build_release_slip_raw(release_id="v1.0.0")
    body["content_sha256"] = _hex("0")
    config_dir, project_root, _ = _setup_publish_env(tmp_path, slip_raw=body, write_slip=True)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "slip is not verifiable" in str(ei.value)


def test_run_release_id_mismatch_raises_3(tmp_path: Path) -> None:
    """Slip's ``release_id`` != argument raises ``UpgradeCliError(3)``."""
    body = _build_release_slip_raw(release_id="v1.0.0")
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        release_id="v1.0.1",
        slip_raw=body,
        write_slip=True,
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.1")
    assert ei.value.exit_code == 3
    message = str(ei.value)
    assert "release_id='v1.0.0'" in message or 'release_id="v1.0.0"' in message
    assert "publish was called with release_id='v1.0.1'" in message


def test_run_existing_bundle_raises_3(tmp_path: Path) -> None:
    """An existing ``www/releases/<id>/`` directory raises ``UpgradeCliError(3)``."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "packing_slip.toml").write_text("# pre-existing")

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "already exists" in str(ei.value)
    assert "immutable" in str(ei.value)


# ---------------------------------------------------------------------------
# T2 preconditions: artifact existence + hash
# ---------------------------------------------------------------------------


def test_run_missing_jar_raises_3(tmp_path: Path) -> None:
    """A slip-declared jar absent from the warehouse raises ``UpgradeCliError(3)``."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, warehouse_files={})

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "artifact missing" in str(ei.value)
    assert "jar" in str(ei.value)
    assert "mod-v2.jar" in str(ei.value)
    assert "Section 6.3 #4" in str(ei.value)


def test_run_jar_hash_mismatch_raises_3(tmp_path: Path) -> None:
    """A jar whose warehouse bytes differ from the slip hash raises exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, warehouse_files={"mod-v2.jar": b"wrong-content"})

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "artifact hash mismatch" in str(ei.value)
    assert "slip=" in str(ei.value)
    assert "actual=" in str(ei.value)


def test_run_multiple_jars_all_present_passes(tmp_path: Path) -> None:
    """Multiple jars, all present with matching hashes -> exit 0."""
    a_content = b"jar-a-bytes"
    b_content = b"jar-b-bytes"
    slip_raw = _build_release_slip_raw(
        jar_entries=[
            _jar_entry("a.jar", a_content),
            _jar_entry("b.jar", b_content),
        ]
    )
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={"a.jar": a_content, "b.jar": b_content},
    )

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0


def test_run_one_of_many_jars_missing_raises_3(tmp_path: Path) -> None:
    """Multiple jars, one missing -> exit 3 naming the missing file."""
    a_content = b"jar-a-bytes"
    b_content = b"jar-b-bytes"
    slip_raw = _build_release_slip_raw(
        jar_entries=[
            _jar_entry("a.jar", a_content),
            _jar_entry("b.jar", b_content),
        ]
    )
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={"a.jar": a_content},
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "artifact missing" in str(ei.value)
    assert "b.jar" in str(ei.value)


def test_run_resource_pack_missing_raises_3(tmp_path: Path) -> None:
    """A slip-declared resource pack absent from the warehouse raises exit 3."""
    jar_content = b"jar-bytes"
    rp_content = b"rp-bytes"
    slip_raw = _build_release_slip_raw(
        jar_entries=[_jar_entry("mod-v2.jar", jar_content)],
        resource_pack_entries=[_rp_entry("survival-pack.zip", rp_content)],
    )
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={"mod-v2.jar": jar_content},
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "artifact missing" in str(ei.value)
    assert "resource pack" in str(ei.value)
    assert "survival-pack.zip" in str(ei.value)


def test_run_velocity_plugin_missing_raises_3(tmp_path: Path) -> None:
    """A slip-declared velocity plugin absent from the warehouse raises exit 3."""
    jar_content = b"jar-bytes"
    vp_content = b"vp-bytes"
    slip_raw = _build_release_slip_raw(
        jar_entries=[_jar_entry("mod-v2.jar", jar_content)],
        velocity_plugin_entries=[_vp_entry("spark.jar", vp_content)],
    )
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={"mod-v2.jar": jar_content},
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "artifact missing" in str(ei.value)
    assert "velocity plugin" in str(ei.value)
    assert "spark.jar" in str(ei.value)


def test_run_empty_jar_list_passes(tmp_path: Path) -> None:
    """A slip with no artifacts has nothing to verify; publish succeeds (exit 0)."""
    slip_raw = _build_release_slip_raw(jar_entries=[])
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={},
    )

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0


@pytest.mark.parametrize(
    "bad_filename",
    ["../evil.jar", "sub/evil.jar", "sub\\evil.jar", "..", ".", "bad\x00.jar"],
)
def test_run_unsafe_artifact_filename_raises_3(tmp_path: Path, bad_filename: str) -> None:
    """A slip-declared filename with path separators or traversal raises exit 3."""
    slip_raw = _build_release_slip_raw(jar_entries=[_jar_entry(bad_filename, b"bad-content")])
    config_dir, project_root, _ = _setup_publish_env(tmp_path, slip_raw=slip_raw, warehouse_files={})

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "unsafe artifact filename" in str(ei.value)


# ---------------------------------------------------------------------------
# T3: staging symlinks and cleanup
# ---------------------------------------------------------------------------


def test_run_symlink_in_config_raises_3(tmp_path: Path) -> None:
    """A symlink under ``sync/config/`` -> exit 3 before any bundle write."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    config_src = project_root / "sync" / "config"
    real_file = tmp_path / "real.txt"
    real_file.write_bytes(b"real")
    (config_src / "linked.txt").symlink_to(real_file)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "symlink in source tree" in str(ei.value)
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


def test_run_symlink_in_kubejs_raises_3(tmp_path: Path) -> None:
    """A symlink under ``sync/kubejs/`` -> exit 3 before any bundle write."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    kubejs_src = project_root / "sync" / "kubejs"
    real_file = tmp_path / "real.js"
    real_file.write_bytes(b"real")
    (kubejs_src / "linked.js").symlink_to(real_file)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "symlink in source tree" in str(ei.value)


def test_run_staging_cleans_up_on_copy_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-copy failure cleans up the staging directory (no residue)."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(publish_mod.shutil, "copy2", _explode)
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert not (project_root / "www" / "releases" / "v1.0.0.staging").exists()
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


def test_run_missing_config_source_warns_and_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing ``sync/config/`` -> warning, run commits an empty config/ dir."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_config=False)

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0

    combined = capsys.readouterr()
    assert "source config directory missing" in (combined.out + combined.err)
    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    assert (bundle_dir / "config").is_dir()
    assert list((bundle_dir / "config").iterdir()) == []


def test_run_missing_kubejs_source_warns_and_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing ``sync/kubejs/`` -> warning, run commits an empty kubejs/ dir."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_kubejs=False)

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0

    combined = capsys.readouterr()
    assert "source kubejs directory missing" in (combined.out + combined.err)


# ---------------------------------------------------------------------------
# Happy path: full pipeline
# ---------------------------------------------------------------------------


def test_run_happy_path_returns_0_and_commits_bundle(tmp_path: Path) -> None:
    """Preconditions pass -> bundle committed, exit 0."""
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        config_files={"app.toml": b"key = 'value'\n"},
        kubejs_files={"startup.js": b"// boot\n"},
    )

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    assert code == 0
    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    staging_dir = project_root / "www" / "releases" / "v1.0.0.staging"
    assert bundle_dir.is_dir()
    assert not staging_dir.exists()

    assert (bundle_dir / "packing_slip.toml").is_file()
    assert (bundle_dir / "approval.json").is_file()
    assert (bundle_dir / "config" / "app.toml").is_file()
    assert (bundle_dir / "kubejs" / "startup.js").is_file()
    assert (bundle_dir / "artifacts" / "mod-v2.jar").is_file()
    assert (bundle_dir / "bundle-manifest.json").is_file()
    assert (bundle_dir / "artifact-manifest.json").is_file()
    assert (bundle_dir / "client-distribution-manifest.json").is_file()


def test_run_happy_path_appends_one_registry_entry(tmp_path: Path) -> None:
    """A successful publish appends exactly one entry to the registry."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    entries = _read_registry(project_root)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["schema_version"] == 1
    assert entry["release_id"] == "v1.0.0"
    assert entry["previous_entry_hash"] == "0" * 64
    assert len(entry["entry_hash"]) == 64
    assert len(entry["tag_object_sha"]) in (40, 64)
    assert entry["tag_object_format"] in ("sha1", "sha256")
    assert len(entry["commit_sha"]) in (40, 64)
    assert len(entry["slip_content_sha256"]) == 64
    assert len(entry["bundle_manifest_sha256"]) == 64
    assert entry["signature"] == "UNSIGNED"
    assert entry["key_id"] == "UNSIGNED"


def test_run_happy_path_logs_summary_and_commit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The happy path prints the slip summary, staging summary, and commit summary."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    out = capsys.readouterr().out
    assert "slip verified" in out
    assert "release_id: v1.0.0" in out
    assert "MC 1.21.1" in out
    assert "artifacts verified:" in out
    assert "bundle staged:" in out
    assert "bundle committed:" in out
    assert "registry entry appended:" in out


# ---------------------------------------------------------------------------
# Manifest contents
# ---------------------------------------------------------------------------


def test_bundle_manifest_exists_and_parses(tmp_path: Path) -> None:
    """``bundle-manifest.json`` is valid JSON with the expected top-level keys."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    manifest = _read_manifest(project_root / "www" / "releases" / "v1.0.0", "bundle-manifest.json")
    assert manifest["schema_version"] == 1
    assert manifest["release_id"] == "v1.0.0"
    assert manifest["signature"] == "UNSIGNED"
    assert len(manifest["slip_content_sha256"]) == 64
    assert len(manifest["approval_sha256"]) == 64
    assert len(manifest["release_tag_object_sha"]) in (40, 64)
    assert len(manifest["release_commit_sha"]) in (40, 64)
    assert isinstance(manifest["files"], list)
    assert manifest["files"]


def test_bundle_manifest_lists_content_files(tmp_path: Path) -> None:
    """The manifest's files list covers every content file in the bundle."""
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        config_files={"app.toml": b"x"},
        kubejs_files={"boot.js": b"// y"},
    )
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    manifest = _read_manifest(bundle_dir, "bundle-manifest.json")
    paths = {entry["path"] for entry in manifest["files"]}
    assert "packing_slip.toml" in paths
    assert "approval.json" in paths
    assert "config/app.toml" in paths
    assert "kubejs/boot.js" in paths
    assert "artifacts/mod-v2.jar" in paths


def test_bundle_manifest_excludes_itself(tmp_path: Path) -> None:
    """A manifest cannot honestly hash itself; it must not list its own path."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    manifest = _read_manifest(project_root / "www" / "releases" / "v1.0.0", "bundle-manifest.json")
    paths = {entry["path"] for entry in manifest["files"]}
    assert "bundle-manifest.json" not in paths


def test_bundle_manifest_includes_other_manifests(tmp_path: Path) -> None:
    """The artifact and client-distribution manifests appear as content entries."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    manifest = _read_manifest(project_root / "www" / "releases" / "v1.0.0", "bundle-manifest.json")
    paths = {entry["path"] for entry in manifest["files"]}
    assert "artifact-manifest.json" in paths
    assert "client-distribution-manifest.json" in paths


def test_bundle_manifest_approval_sha256_matches_file(tmp_path: Path) -> None:
    """The manifest's ``approval_sha256`` matches the on-disk approval.json."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    manifest = _read_manifest(bundle_dir, "bundle-manifest.json")
    approval_sha = hashlib.sha256((bundle_dir / "approval.json").read_bytes()).hexdigest()
    assert manifest["approval_sha256"] == approval_sha


def test_manifest_hashes_match_disk(tmp_path: Path) -> None:
    """Every manifest files-list entry matches the committed bytes."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, config_files={"app.toml": b"content-bytes"})
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    manifest = _read_manifest(bundle_dir, "bundle-manifest.json")
    for entry in manifest["files"]:
        file_path = bundle_dir / entry["path"]
        assert file_path.is_file(), f"manifest entry missing: {entry['path']}"
        assert file_path.stat().st_size == entry["size_bytes"]
        actual_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
        assert actual_sha == entry["sha256"]


def test_artifact_manifest_lists_artifacts(tmp_path: Path) -> None:
    """``artifact-manifest.json`` lists every slip artifact with kind + path + hash."""
    jar_content = b"jar-bytes"
    slip_raw = _build_release_slip_raw(jar_entries=[_jar_entry("mod-v2.jar", jar_content)])
    config_dir, project_root, _ = _setup_publish_env(
        tmp_path,
        slip_raw=slip_raw,
        warehouse_files={"mod-v2.jar": jar_content},
    )
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    manifest = _read_manifest(project_root / "www" / "releases" / "v1.0.0", "artifact-manifest.json")
    assert manifest["schema_version"] == 1
    assert manifest["release_id"] == "v1.0.0"
    assert len(manifest["artifacts"]) == 1
    artifact = manifest["artifacts"][0]
    assert artifact["kind"] == "jar"
    assert artifact["path"] == "artifacts/mod-v2.jar"
    assert artifact["sha256"] == _sha256(jar_content)
    assert artifact["size_bytes"] == len(jar_content)


def test_client_distribution_manifest_is_placeholder(tmp_path: Path) -> None:
    """``client-distribution-manifest.json`` is a valid placeholder."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    manifest = _read_manifest(project_root / "www" / "releases" / "v1.0.0", "client-distribution-manifest.json")
    assert manifest["schema_version"] == 1
    assert manifest["release_id"] == "v1.0.0"
    assert manifest["members"] == []
    assert "Section 6.3 #5" in manifest.get("note", "")


# ---------------------------------------------------------------------------
# Approval sourcing
# ---------------------------------------------------------------------------


def test_approval_from_acknowledgment_json_when_present(tmp_path: Path) -> None:
    """A lossy run's acknowledgment.json is copied into approval.json verbatim."""
    ack = {
        "schema_version": 1,
        "run_id": "run-abc",
        "report_sha256": _hex("a"),
        "resolution_inputs_sha256": _hex("7"),
        "acknowledged_losses": [{"project_id": "gone-mod", "reason": "no_compatible_version"}],
        "acknowledged_by": "test-op",
        "acknowledged_at_utc": "2026-10-17T12:00:00Z",
    }
    config_dir, project_root, _ = _setup_publish_env(tmp_path, acknowledgment=ack, run_status="materialized")

    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    approval = json.loads((bundle_dir / "approval.json").read_text(encoding="utf-8"))
    assert approval == ack


def test_approval_auto_generated_when_no_acknowledgment(tmp_path: Path) -> None:
    """A loss-free run auto-generates an approval.json with empty losses."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, acknowledgment=None)

    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    bundle_dir = project_root / "www" / "releases" / "v1.0.0"
    approval = json.loads((bundle_dir / "approval.json").read_text(encoding="utf-8"))
    assert approval["schema_version"] == 1
    assert approval["run_id"] == "run-abc"
    assert approval["acknowledged_losses"] == []
    assert approval["acknowledged_by"] == "auto"
    assert "no losses required acknowledgment" in approval["note"]


# ---------------------------------------------------------------------------
# Registry chain
# ---------------------------------------------------------------------------


def test_registry_chain_links_two_entries(tmp_path: Path) -> None:
    """Entry 2's previous_entry_hash equals entry 1's entry_hash."""
    # First publish: v1.0.0 with default fingerprint _hex("7").
    config_dir, project_root, _ = _setup_publish_env(tmp_path, release_id="v1.0.0", run_id="run-a")
    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")

    # Prepare v1.0.1 in the same tree with a DIFFERENT fingerprint so
    # run discovery is unambiguous.
    state_dir = project_root / "state"
    body_b = _build_release_slip_raw(
        release_id="v1.0.1",
        resolution_inputs_sha256=_hex("b"),
    )
    (project_root / "sync" / "packing_slips" / "v1.0.1.toml").write_text(tomli_w.dumps(body_b), encoding="utf-8")
    _write_materialized_run(
        state_dir,
        run_id="run-b",
        resolution_inputs_sha256=_hex("b"),
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.1", "-m", "release"],
        cwd=project_root,
        check=True,
    )

    run(config_dir=config_dir, project_root=project_root, release_id="v1.0.1")

    entries = _read_registry(project_root)
    assert len(entries) == 2
    assert entries[1]["previous_entry_hash"] == entries[0]["entry_hash"]
    assert entries[1]["entry_hash"] != entries[0]["entry_hash"]
    assert entries[0]["previous_entry_hash"] == "0" * 64


def test_registry_entry_hash_is_deterministic() -> None:
    """Given the same chain fields, entry_hash is reproducible."""
    entry = {
        "schema_version": 1,
        "release_id": "v1.0.0",
        "tag_object_sha": "a" * 40,
        "commit_sha": "b" * 40,
        "slip_content_sha256": "c" * 64,
        "bundle_manifest_sha256": "d" * 64,
        "previous_entry_hash": "0" * 64,
    }
    h1 = publish_mod._compute_entry_hash(entry)
    h2 = publish_mod._compute_entry_hash(entry)
    assert h1 == h2
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# Cleanup: _find_materialized_run_for_slip branch coverage
# ---------------------------------------------------------------------------


def test_runs_dir_missing_raises_3(tmp_path: Path) -> None:
    """No ``runs/`` directory at all -> exit 3 with 'no runs directory' message."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_run=False)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no runs directory" in str(ei.value)


def test_file_in_runs_dir_is_skipped(tmp_path: Path) -> None:
    """A regular file inside ``runs/`` is skipped (only directories are scanned)."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    # The fixture already created state/runs/run-abc/. Add a stray file.
    (project_root / "state" / "runs" / "stray_file.txt").write_text("not a run dir\n")

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0


def test_run_dir_without_state_toml_skipped(tmp_path: Path) -> None:
    """A run directory with no ``state.toml`` is skipped."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    (project_root / "state" / "runs" / "run-empty").mkdir(parents=True)

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0


def test_malformed_state_toml_skipped(tmp_path: Path) -> None:
    """A run directory with malformed ``state.toml`` is skipped."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    bad_run = project_root / "state" / "runs" / "run-bad"
    bad_run.mkdir(parents=True)
    (bad_run / "state.toml").write_text("not = valid = toml\n", encoding="utf-8")

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0


def test_fingerprint_mismatch_raises_3(tmp_path: Path) -> None:
    """A materialized run whose fingerprint differs from the slip's -> no match -> exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_run=False)
    # Only create a run with a DIFFERENT fingerprint.
    _write_materialized_run(
        project_root / "state",
        run_id="run-other",
        resolution_inputs_sha256=_hex("0"),
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no materialized run" in str(ei.value)


def test_multiple_matching_runs_raises_3(tmp_path: Path) -> None:
    """Two materialized runs with the same fingerprint -> exit 3 (ambiguous)."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, run_id="run-a")
    _write_materialized_run(
        project_root / "state",
        run_id="run-b",
        resolution_inputs_sha256=_hex("7"),  # same as fixture default
    )

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "ambiguous" in str(ei.value)
    # No bundle committed.
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


# ---------------------------------------------------------------------------
# Cleanup: _last_entry_hash branch coverage
# ---------------------------------------------------------------------------


def test_last_entry_hash_missing_file_returns_genesis(tmp_path: Path) -> None:
    """A missing registry file returns the genesis hash (all zeroes)."""
    assert publish_mod._last_entry_hash(tmp_path / "nope.jsonl") == "0" * 64


def test_last_entry_hash_empty_file_returns_genesis(tmp_path: Path) -> None:
    """A registry file with only blank lines returns the genesis hash."""
    p = tmp_path / "empty.jsonl"
    p.write_text("\n\n\n", encoding="utf-8")
    assert publish_mod._last_entry_hash(p) == "0" * 64


def test_last_entry_hash_unreadable_raises_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` on registry read -> exit 7."""
    p = tmp_path / "unreadable.jsonl"
    p.write_text('{"entry_hash": "' + "a" * 64 + '"}\n', encoding="utf-8")
    real_open = open

    def _fail(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(p):
            raise OSError("permission denied")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", _fail)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._last_entry_hash(p)
    assert ei.value.exit_code == 7
    assert "could not read registry" in str(ei.value)


def test_last_entry_hash_malformed_json_raises_7(tmp_path: Path) -> None:
    """A registry whose last line is not valid JSON -> exit 7."""
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._last_entry_hash(p)
    assert ei.value.exit_code == 7
    assert "malformed JSON" in str(ei.value)


def test_last_entry_hash_non_dict_raises_7(tmp_path: Path) -> None:
    """A registry whose last line is a JSON array (not object) -> exit 7."""
    p = tmp_path / "list.jsonl"
    p.write_text('["not", "a", "dict"]\n', encoding="utf-8")
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._last_entry_hash(p)
    assert ei.value.exit_code == 7
    assert "not a JSON object" in str(ei.value)


def test_last_entry_hash_invalid_entry_hash_raises_7(tmp_path: Path) -> None:
    """A registry whose last entry has a wrong-length entry_hash -> exit 7."""
    p = tmp_path / "short.jsonl"
    p.write_text('{"entry_hash": "abc"}\n', encoding="utf-8")
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._last_entry_hash(p)
    assert ei.value.exit_code == 7
    assert "invalid entry_hash" in str(ei.value)


def test_last_entry_hash_valid_returns_entry_hash(tmp_path: Path) -> None:
    """A well-formed last entry returns its recorded entry_hash."""
    p = tmp_path / "good.jsonl"
    expected = "b" * 64
    p.write_text(
        json.dumps({"entry_hash": expected, "release_id": "v1.0.0"}) + "\n",
        encoding="utf-8",
    )
    assert publish_mod._last_entry_hash(p) == expected


# ---------------------------------------------------------------------------
# Cleanup: _resolve_git_refs branch coverage
# ---------------------------------------------------------------------------


def test_resolve_git_refs_filenotfound_on_tag_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary on the tag lookup -> exit 3."""

    def _raise(*args: object, **kwargs: object) -> tuple[int, str, str]:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(publish_mod, "run_git", _raise)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3
    assert "failed" in str(ei.value)


def test_resolve_git_refs_timeout_on_tag_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout on the tag lookup -> exit 3."""

    def _raise(*args: object, **kwargs: object) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    monkeypatch.setattr(publish_mod, "run_git", _raise)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3


def test_resolve_git_refs_unexpected_sha_length_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag SHA whose length isn't 40 or 64 -> exit 3 with a clear message."""

    def _fake(cmd: list[str], **kwargs: object) -> tuple[int, str, str]:
        return (0, "abc\n", "")

    monkeypatch.setattr(publish_mod, "run_git", _fake)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3
    assert "unexpected tag SHA length" in str(ei.value)


def test_resolve_git_refs_filenotfound_on_commit_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing git binary on the commit resolution -> exit 3."""
    calls = {"n": 0}

    def _fake(cmd: list[str], **kwargs: object) -> tuple[int, str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "a" * 40 + "\n", "")
        raise FileNotFoundError("git gone")

    monkeypatch.setattr(publish_mod, "run_git", _fake)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3
    assert "failed" in str(ei.value)


def test_resolve_git_refs_timeout_on_commit_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout on the commit resolution -> exit 3."""
    calls = {"n": 0}

    def _fake(cmd: list[str], **kwargs: object) -> tuple[int, str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "a" * 40 + "\n", "")
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    monkeypatch.setattr(publish_mod, "run_git", _fake)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3


def test_resolve_git_refs_rc_nonzero_on_commit_raises_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero return code on the commit resolution -> exit 3."""
    calls = {"n": 0}

    def _fake(cmd: list[str], **kwargs: object) -> tuple[int, str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "a" * 40 + "\n", "")
        return (1, "", "commit not found")

    monkeypatch.setattr(publish_mod, "run_git", _fake)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._resolve_git_refs(tmp_path, "v1.0.0")
    assert ei.value.exit_code == 3
    assert "could not resolve commit" in str(ei.value)


# ---------------------------------------------------------------------------
# Cleanup: _append_registry_entry branch coverage
# ---------------------------------------------------------------------------


def test_append_registry_entry_oserror_raises_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` while opening the registry for append -> exit 7."""
    slip = parse_slip(_build_release_slip_raw())
    registry = tmp_path / "reg.jsonl"
    real_open = open

    def _fail(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(registry):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", _fail)
    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._append_registry_entry(
            registry,
            slip=slip,
            tag_object_sha="a" * 40,
            tag_object_format="sha1",
            commit_sha="b" * 40,
            bundle_manifest_sha256="c" * 64,
        )
    assert ei.value.exit_code == 7
    assert "could not append" in str(ei.value)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_run_no_materialized_run_raises_3(tmp_path: Path) -> None:
    """Zero matching runs -> exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_run=False)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no runs directory" in str(ei.value)


def test_run_run_status_not_materialized_raises_3(tmp_path: Path) -> None:
    """A run whose status isn't materialized -> exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, run_status="resolved")

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "no materialized run" in str(ei.value)


def test_run_missing_git_tag_raises_3(tmp_path: Path) -> None:
    """A git repo without the release tag -> exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path)
    subprocess.run(["git", "tag", "-d", "v1.0.0"], cwd=project_root, check=True)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "not found" in str(ei.value)


def test_run_no_git_repo_raises_3(tmp_path: Path) -> None:
    """A project without a git repo -> exit 3."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, create_git=False)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3


def test_run_lightweight_tag_warns_but_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A lightweight tag produces a warning but publish still succeeds."""
    config_dir, project_root, _ = _setup_publish_env(tmp_path, tag_as_annotated=False)

    code = run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert code == 0

    combined = capsys.readouterr()
    assert "lightweight" in (combined.out + combined.err).lower()


def test_manifest_write_failure_cleans_up_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest write failure leaves no staging dir and no bundle dir."""
    original = publish_mod._write_json_atomic
    calls = {"count": 0}

    def _fail_first(path: Path, body: dict) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise UpgradeCliError(3, "simulated manifest write failure")
        original(path, body)

    monkeypatch.setattr(publish_mod, "_write_json_atomic", _fail_first)
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "simulated manifest write failure" in str(ei.value)
    assert not (project_root / "www" / "releases" / "v1.0.0.staging").exists()
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


def test_rename_failure_cleans_up_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename failure leaves no bundle dir; staging is cleaned up."""
    real_replace = Path.replace

    def _fail_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".staging"):
            raise OSError("simulated rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_replace)
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 3
    assert "could not commit bundle" in str(ei.value)
    assert "simulated rename failure" in str(ei.value)
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


def test_registry_append_failure_rolls_back_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry append failure rolls back the committed bundle and exits 7."""

    def _fail(*args: object, **kwargs: object) -> None:
        raise UpgradeCliError(7, "simulated registry failure")

    monkeypatch.setattr(publish_mod, "_append_registry_entry", _fail)
    config_dir, project_root, _ = _setup_publish_env(tmp_path)

    with pytest.raises(UpgradeCliError) as ei:
        run(config_dir=config_dir, project_root=project_root, release_id="v1.0.0")
    assert ei.value.exit_code == 7
    assert "simulated registry failure" in str(ei.value)
    assert not (project_root / "www" / "releases" / "v1.0.0").exists()


# ---------------------------------------------------------------------------
# Unit: _verify_manifest_hashes
# ---------------------------------------------------------------------------


def test_verify_manifest_hashes_catches_drift(tmp_path: Path) -> None:
    """``_verify_manifest_hashes`` raises when the recorded hash doesn't match."""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.txt"
    target.write_bytes(b"original")
    manifest = {
        "files": [
            {
                "path": "a.txt",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"original").hexdigest(),
            }
        ]
    }
    target.write_bytes(b"changed!")

    with pytest.raises(UpgradeCliError) as ei:
        publish_mod._verify_manifest_hashes(root, manifest)
    assert ei.value.exit_code == 3
    assert "manifest hash drift" in str(ei.value)
