# tests/deploy_pack/test_daemon_deploy.py

"""Unit tests for ``daemon.deploy`` (spec v1.3 Section 12.4 steps 11-19).

Each test scripts the orchestrator's injectables (service control,
health probe, clock, pull transport, journal, lock) and asserts on the
returned :class:`DeployOutcome` plus the journal's terminal stage.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import tomli_w

from minecraft.deploy_pack.daemon.deploy import (
    DeployRequest,
    deploy,
)
from minecraft.deploy_pack.daemon.health import HealthCheckSpec
from minecraft.deploy_pack.daemon.journal import DeploymentJournal, Stage
from minecraft.deploy_pack.daemon.lock import DeploymentLock
from minecraft.deploy_pack.daemon.pull import PullRequest
from minecraft.modpack_upgrade import slip as slip_mod

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _jar_entry(filename: str, content: bytes) -> dict:
    """Build a schema-valid ``[[jar]]`` entry."""
    return {
        "filename": filename,
        "sha256": _sha(content),
        "side": "both",
        "provider": "modrinth",
        "pw_toml": f"{filename.removesuffix('.jar')}.pw.toml",
        "pw_toml_sha256": "0" * 64,
        "dependencies": [],
    }


def _slip_body(jar_entries: list[dict], release_id: str) -> dict:
    """Build a valid slip body with a fresh ``content_sha256``."""
    body = {
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
            "image_digest": "sha256:" + "0" * 64,
            "platform": "linux/amd64",
            "java_version": "21",
        },
        "inputs": {
            "git": {
                "config_tree": "a" * 40,
                "kubejs_tree": "a" * 40,
                "compose_tree": "a" * 40,
                "deploy_toml_tree": "a" * 40,
            },
        },
        "fingerprints": {
            "manifest_sha256": "0" * 64,
            "resolution_inputs_sha256": "0" * 64,
            "runtime_sha256": "0" * 64,
            "server_inputs_sha256": "0" * 64,
            "client_inputs_sha256": "0" * 64,
        },
        "jar": jar_entries,
        "resource_pack": [],
        "velocity_plugin": [],
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": [],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_bundle_files(bundle: Path, jars: dict[str, bytes], release_id: str = "v1.0.0") -> None:
    """Write a fully valid bundle: slip + artifacts + ``bundle-manifest.json``.

    The manifest lists the slip and every artifact so that the pull
    verifier accepts the bundle. ``bundle-manifest.json`` itself is not
    listed (its bytes depend on itself, so it is excluded from the
    ``files`` array by convention; ``bundle-manifest.sig`` is likewise
    exempt).
    """
    bundle.mkdir(parents=True)
    (bundle / "artifacts").mkdir()
    entries: list[dict] = []
    for filename, content in jars.items():
        (bundle / "artifacts" / filename).write_bytes(content)
        entries.append(_jar_entry(filename, content))
    body = _slip_body(entries, release_id)
    slip_bytes = tomli_w.dumps(body).encode("utf-8")
    (bundle / "packing_slip.toml").write_bytes(slip_bytes)

    manifest_files: list[dict] = [
        {
            "path": "packing_slip.toml",
            "size_bytes": len(slip_bytes),
            "sha256": _sha(slip_bytes),
        }
    ]
    for filename, content in jars.items():
        manifest_files.append(
            {
                "path": f"artifacts/{filename}",
                "size_bytes": len(content),
                "sha256": _sha(content),
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "slip_content_sha256": body["content_sha256"],
        "approval_sha256": "0" * 64,
        "release_tag_object_sha": "a" * 40,
        "release_commit_sha": "b" * 40,
        "files": manifest_files,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (bundle / "bundle-manifest.json").write_bytes(manifest_bytes)


def _populating_transport(jars: dict[str, bytes], release_id: str = "v1.0.0"):
    """Return an rsync transport that writes a valid bundle to the staging dir.

    The pull layer invokes the transport with the staging directory as
    the final argv element, verifies the staged bundle, then renames
    staging to the final path. The transport must write to the staging
    directory, not the final one, or the verification sees an empty
    directory and the rename later fails.
    """

    def _transport(args, _timeout):
        dest = Path(args[-1].rstrip("/"))
        _write_bundle_files(dest, jars, release_id)
        return (0, "", "")

    return _transport


class _FakeServiceControl:
    """Scriptable service controller: per-container stop/start outcomes."""

    def __init__(self) -> None:
        """Initialize empty scripts."""
        self._stop_scripts: dict[str, Exception | None] = {}
        self._start_scripts: dict[str, Exception | None] = {}
        self.stop_calls: list[str] = []
        self.start_calls: list[str] = []

    def script_stop(self, container: str, exc: Exception | None) -> None:
        """Set the stop outcome for ``container``."""
        self._stop_scripts[container] = exc

    def script_start(self, container: str, exc: Exception | None) -> None:
        """Set the start outcome for ``container``."""
        self._start_scripts[container] = exc

    def stop(self, container: str) -> None:
        """Record and possibly raise for ``container``."""
        self.stop_calls.append(container)
        exc = self._stop_scripts.get(container)
        if exc is not None:
            raise exc

    def start(self, container: str) -> None:
        """Record and possibly raise for ``container``."""
        self.start_calls.append(container)
        exc = self._start_scripts.get(container)
        if exc is not None:
            raise exc


class _FakeHealthProbe:
    """Healthy by default; ``set_unhealthy`` flips a single container."""

    def __init__(self) -> None:
        """Initialize as fully healthy."""
        self._running: dict[str, bool] = {}
        self._unhealthy: dict[str, bool] = {}

    def set_unhealthy(self, container: str) -> None:
        """Mark ``container`` unhealthy at final check."""
        self._unhealthy[container] = True

    def is_running(self, container: str) -> bool:
        """Return True unless explicitly unset."""
        return self._running.get(container, True)

    def restart_count(self, container: str) -> int:
        """Return zero restarts."""
        del container
        return 0

    def recent_logs(self, container: str) -> str:
        """Return a startup sentinel unless the container is unhealthy."""
        if self._unhealthy.get(container):
            return "---- Minecraft Crash Report ----\n"
        return 'Done (10s)! For help, type "help"\n'

    def rcon_list(self, container: str) -> tuple[bool, str]:
        """Return ``(True, "0 players")`` unless the container is unhealthy."""
        if self._unhealthy.get(container):
            return (False, "connection refused")
        return (True, "0 players online")


class _FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self) -> None:
        """Initialize at t=0."""
        self._now = 0.0

    def now(self) -> float:
        """Return the current fake time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by ``seconds``."""
        self._now += seconds


def _health_spec(container: str = "mc-survival") -> HealthCheckSpec:
    """Return a spec with short timeouts so failures don't spin."""
    return HealthCheckSpec(
        container=container,
        rcon_list_required_successes=1,
        timeout_seconds=5.0,
        poll_interval_seconds=0.1,
        rcon_list_interval_seconds=0.1,
    )


def _request(
    tmp_path: Path,
    *,
    release_id: str = "v1.0.0",
    containers: tuple[str, ...] = ("mc-survival",),
    is_upgrade: bool = False,
    prior_bundle_dir: Path | None = None,
) -> DeployRequest:
    """Build a DeployRequest rooted under ``tmp_path``."""
    return DeployRequest(
        run_id="run-1",
        release_id=release_id,
        pull=PullRequest(
            source_host="dev.example.com",
            source_user="deploy",
            source_path=Path("/srv/remote"),
            ssh_key=tmp_path / "id_deploy",
            target_root=tmp_path / "releases",
            release_id=release_id,
        ),
        target_mods_dir=tmp_path / "mods",
        target_world_dir=(tmp_path / "world" if is_upgrade else None),
        world_backup_root=(tmp_path / "backups" if is_upgrade else None),
        prior_bundle_dir=prior_bundle_dir,
        containers=containers,
        protect_patterns=(),
        lock=DeploymentLock(tmp_path / "deploy.lock"),
        health_specs=tuple(_health_spec(c) for c in containers),
        is_upgrade=is_upgrade,
    )


def _journal(tmp_path: Path) -> DeploymentJournal:
    """Return a journal rooted under ``tmp_path``."""
    return DeploymentJournal(tmp_path / "journal.jsonl")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_completes(tmp_path: Path) -> None:
    """A fully valid deployment returns success and journals COMPLETE."""
    jars = {"mod.jar": b"mod-bytes"}
    transport = _populating_transport(jars)
    journal = _journal(tmp_path)

    outcome = deploy(
        request=_request(tmp_path),
        journal=journal,
        service_control=_FakeServiceControl(),
        health_probe=_FakeHealthProbe(),
        clock=_FakeClock(),
        pull_transport=transport,
    )

    assert outcome.success is True
    stages = [r.stage.value for r in journal.read_all()]
    assert "complete" in stages
    assert stages[-1] == "complete"
    assert (tmp_path / "mods" / "mod.jar").read_bytes() == b"mod-bytes"


def test_happy_path_releases_lock(tmp_path: Path) -> None:
    """The lock file is removed after a successful deployment."""
    jars = {"mod.jar": b"m"}
    request = _request(tmp_path)

    deploy(
        request=request,
        journal=_journal(tmp_path),
        service_control=_FakeServiceControl(),
        health_probe=_FakeHealthProbe(),
        clock=_FakeClock(),
        pull_transport=_populating_transport(jars),
    )

    assert not request.lock.path.exists()


# ---------------------------------------------------------------------------
# Refusal-before-start
# ---------------------------------------------------------------------------


def test_unresolved_prior_run_refuses(tmp_path: Path) -> None:
    """An unresolved prior run blocks the new deployment."""
    journal = _journal(tmp_path)
    journal.append_stage(
        run_id="prior-run",
        release_id="v0.9.0",
        stage=Stage.SHIP_STARTED,
        detail="crashed mid-deploy",
    )

    outcome = deploy(
        request=_request(tmp_path),
        journal=journal,
        service_control=_FakeServiceControl(),
        health_probe=_FakeHealthProbe(),
        clock=_FakeClock(),
        pull_transport=_populating_transport({"x.jar": b"x"}),
    )

    assert outcome.success is False
    assert outcome.error_code == 21
    assert "unresolved prior" in (outcome.message or "")


def test_lock_held_refuses(tmp_path: Path) -> None:
    """A held lock returns error code 25."""
    request = _request(tmp_path)
    request.lock.acquire(holder="other-run")

    try:
        outcome = deploy(
            request=request,
            journal=_journal(tmp_path),
            service_control=_FakeServiceControl(),
            health_probe=_FakeHealthProbe(),
            clock=_FakeClock(),
            pull_transport=_populating_transport({"x.jar": b"x"}),
        )
    finally:
        request.lock.release()

    assert outcome.success is False
    assert outcome.error_code == 25
    assert outcome.stage == "lock"


# ---------------------------------------------------------------------------
# Stage failures
# ---------------------------------------------------------------------------


def test_pull_failure_returns_code_22(tmp_path: Path) -> None:
    """A failing pull returns error code 22 without stopping services."""

    def _fail_transport(_args, _timeout):
        return (23, "", "rsync: connection refused")

    outcome = deploy(
        request=_request(tmp_path),
        journal=_journal(tmp_path),
        service_control=_FakeServiceControl(),
        health_probe=_FakeHealthProbe(),
        clock=_FakeClock(),
        pull_transport=_fail_transport,
    )

    assert outcome.success is False
    assert outcome.error_code == 22
    assert outcome.stage == "pull"


def test_stop_failure_returns_code_22(tmp_path: Path) -> None:
    """A stop failure returns 22 after attempting to restart."""
    jars = {"mod.jar": b"m"}
    service = _FakeServiceControl()
    service.script_stop("mc-survival", RuntimeError("stop boom"))

    outcome = deploy(
        request=_request(tmp_path),
        journal=_journal(tmp_path),
        service_control=service,
        health_probe=_FakeHealthProbe(),
        clock=_FakeClock(),
        pull_transport=_populating_transport(jars),
    )

    assert outcome.success is False
    assert outcome.error_code == 22
    # Post-failure start was attempted.
    assert "mc-survival" in service.start_calls


def test_health_failure_returns_code_23(tmp_path: Path) -> None:
    """A health failure returns 23 and journals FAILED."""
    jars = {"mod.jar": b"m"}
    probe = _FakeHealthProbe()
    probe.set_unhealthy("mc-survival")

    journal = _journal(tmp_path)
    outcome = deploy(
        request=_request(tmp_path),
        journal=journal,
        service_control=_FakeServiceControl(),
        health_probe=probe,
        clock=_FakeClock(),
        pull_transport=_populating_transport(jars),
    )

    assert outcome.success is False
    assert outcome.error_code == 23
    assert outcome.stage == "health"
    stages = [r.stage.value for r in journal.read_all()]
    assert "failed" in stages


# ---------------------------------------------------------------------------
# Recovery integration
# ---------------------------------------------------------------------------


def test_health_failure_recovers_from_prior_bundle(tmp_path: Path) -> None:
    """Health failure after a prior-bundle deploy restores the prior jar set."""
    prior_bundle = tmp_path / "prior"
    _write_bundle_files(prior_bundle, {"old.jar": b"old-bytes"}, release_id="v0.9.0")

    jars = {"new.jar": b"new-bytes"}
    probe = _FakeHealthProbe()
    probe.set_unhealthy("mc-survival")

    outcome = deploy(
        request=_request(tmp_path, prior_bundle_dir=prior_bundle),
        journal=_journal(tmp_path),
        service_control=_FakeServiceControl(),
        health_probe=probe,
        clock=_FakeClock(),
        pull_transport=_populating_transport(jars),
    )

    assert outcome.success is False
    assert outcome.error_code == 23
    # Prior artifact restored after the health failure triggered recovery.
    assert (tmp_path / "mods" / "old.jar").read_bytes() == b"old-bytes"
    assert not (tmp_path / "mods" / "new.jar").exists()


def test_upgrade_creates_backup_then_health_failure_restores_world(tmp_path: Path) -> None:
    """An upgrade backs up the world; a health failure restores it."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"original-level")

    jars = {"new.jar": b"new-bytes"}
    probe = _FakeHealthProbe()
    probe.set_unhealthy("mc-survival")

    outcome = deploy(
        request=_request(tmp_path, is_upgrade=True),
        journal=_journal(tmp_path),
        service_control=_FakeServiceControl(),
        health_probe=probe,
        clock=_FakeClock(),
        pull_transport=_populating_transport(jars),
    )

    assert outcome.success is False
    assert outcome.error_code == 23
    # Backup exists and world was restored from it.
    backup_dir = tmp_path / "backups" / "v1.0.0"
    assert backup_dir.is_dir()
    assert (world / "level.dat").read_bytes() == b"original-level"
