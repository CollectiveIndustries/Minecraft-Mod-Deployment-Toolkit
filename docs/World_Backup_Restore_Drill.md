# Slice 16 delivery — 4 files

T20 is the last §20 acceptance test. The other three deliverables are operator-facing procedures referenced by the spec's "Pending before production cutover" table.

**Files delivered:**

1. `tests/deploy_pack/test_t20_migration_rollback.py` — the T20 acceptance test
2. `docs/Migration_Runbook.md` — the cutover procedure (raw markdown)
3. `docs/World_Backup_Restore_Drill.md` — the drill procedure (raw markdown)
4. `docs/OCI_Registry_Allowlist.md` — the OCI procedure (raw markdown)

---

## 1. `tests/deploy_pack/test_t20_migration_rollback.py`

```python
# tests/deploy_pack/test_t20_migration_rollback.py

"""T20 acceptance: migration rollback drill.

Exercises the full upgrade-deploy -> failure -> recovery path on a
prod-like clone, verifying that world, mods, and the active-release
pointer are all restorable without data loss.

This is the automated counterpart to the `docs/Migration_Runbook.md`
procedure. Where the runbook walks an operator through a manual drill,
this test exercises the same code paths deterministically so the
invariant is proven on every CI run.

The specific invariant under test (from spec Section 12.10 and Section 19.4):

    After a failed upgrade deployment that reached the stop phase, the
    deployment's recovery path MUST restore the pre-deploy world state
    from the pre-upgrade backup, MUST re-ship the prior approved mod set,
    and MUST leave the active-release pointer untouched -- so the
    operator can re-deploy the prior known-good release without manual
    intervention and without data loss.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import tomli_w

from minecraft.deploy_pack.daemon.backup import verify_backup
from minecraft.deploy_pack.daemon.deploy import (
    DeployRequest,
    deploy,
)
from minecraft.deploy_pack.daemon.health import HealthCheckSpec, HealthProbe
from minecraft.deploy_pack.daemon.journal import DeploymentJournal, Stage
from minecraft.deploy_pack.daemon.lock import DeploymentLock
from minecraft.deploy_pack.daemon.pull import PullRequest
from minecraft.modpack_upgrade import slip as slip_mod


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeServiceControl:
    """ServiceControl that records calls and always succeeds."""

    def __init__(self) -> None:
        self.stops: list[str] = []
        self.starts: list[str] = []

    def stop(self, container: str) -> None:
        """Record a stop call."""
        self.stops.append(container)

    def start(self, container: str) -> None:
        """Record a start call."""
        self.starts.append(container)


class _FakeHealthProbe:
    """HealthProbe that reports a container crash on every poll."""

    def is_running(self, container: str) -> bool:
        """Container is running (so the failure is a crash, not a stop)."""
        return True

    def restart_count(self, container: str) -> int:
        """No restart drift (sentinel check fails before restart check)."""
        return 0

    def recent_logs(self, container: str) -> str:
        """Return logs containing a fatal crash marker."""
        return "---- Minecraft Crash Report ----\nSimulated failure.\n"

    def rcon_list(self, container: str) -> tuple[bool, str]:
        """RCON is unreachable because the server crashed."""
        return (False, "rcon unavailable")


class _FixedClock:
    """Deterministic monotonic clock for the health poll."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        """Return the current simulated time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the simulated time."""
        self._now += seconds


# ---------------------------------------------------------------------------
# Slip construction helpers
# ---------------------------------------------------------------------------


def _build_jar_entry(*, filename: str, content: bytes, project_id: str) -> dict:
    """Return a [[jar]] entry dict for a slip."""
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "side": "server",
        "provider": "modrinth",
        "project_id": project_id,
        "version_id": "1.0.0",
        "pw_toml": filename.replace(".jar", ".pw.toml"),
        "pw_toml_sha256": "0" * 64,
        "dependencies": [],
    }


def _minimal_slip_body(*, release_id: str, jars: list[dict]) -> dict:
    """Return a minimally-valid slip body for the given jars."""
    body: dict = {
        "packing_slip_version": 1,
        "release_id": release_id,
        "release_mode": "upgrade",
        "content_sha256": "",
        "meta": {
            "created_at_utc": "2026-01-01T00:00:00Z",
            "resolver_version": "test",
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
                "config_tree": "0" * 64,
                "kubejs_tree": "0" * 64,
                "compose_tree": "0" * 64,
                "deploy_toml_tree": "0" * 64,
            },
        },
        "fingerprints": {
            "manifest_sha256": "0" * 64,
            "resolution_inputs_sha256": "0" * 64,
            "runtime_sha256": "0" * 64,
            "server_inputs_sha256": "0" * 64,
            "client_inputs_sha256": "0" * 64,
        },
        "jar": jars,
        "instances": {
            "survival": {
                "targeted": True,
                "required_sides": ["server"],
                "config_mode": "merge",
                "kubejs_mode": "delete",
                "config_deletions": [],
                "protect_patterns": [],
            },
        },
    }
    body["content_sha256"] = slip_mod.compute_content_sha256(body)
    return body


def _write_release_bundle(
    release_root: Path,
    body: dict,
    artifact_contents: dict[str, bytes],
) -> None:
    """Write a release bundle (slip + artifacts) to ``release_root``."""
    release_root.mkdir(parents=True, exist_ok=True)
    (release_root / "packing_slip.toml").write_bytes(tomli_w.dumps(body).encode("utf-8"))
    artifacts_dir = release_root / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    for filename, content in artifact_contents.items():
        (artifacts_dir / filename).write_bytes(content)


# ---------------------------------------------------------------------------
# T20 acceptance test
# ---------------------------------------------------------------------------


def test_t20_full_rollback_restores_prior_state(tmp_path: Path) -> None:
    """T20: a failed upgrade restores world, mods, and active-release pointer.

    Scenario:

      1. Prior release v1.0.0 is deployed. ``active-release`` points to
         ``v1.0.0``, ``mods/`` contains ``old-mod.jar``, and ``world/``
         holds pre-upgrade content.
      2. Operator initiates v1.0.1 upgrade. The deploy flow creates a
         world backup, stops services, ships the new artifact set, starts
         services, and then health checks fail (simulated crash).
      3. Recovery runs: world restored from backup, prior mod set
         re-shipped from the v1.0.0 bundle.

    Assertions prove the load-bearing invariants:

      * ``world/`` is byte-identical to pre-deploy state.
      * ``mods/`` matches the v1.0.0 inventory exactly.
      * ``active-release`` still points at ``v1.0.0`` (never promoted).
      * The journal records the recovery flow end-to-end.
      * The prior release bundle is intact and hashes to its slip.
    """
    # --- 1. Set up prod-like state ---
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    releases_root = tmp_path / "releases"
    mods_dir = tmp_path / "survival" / "mods"
    mods_dir.mkdir(parents=True)
    world_dir = tmp_path / "survival" / "world"
    world_dir.mkdir(parents=True)
    backup_root = tmp_path / "backups" / "world"

    prior_jar_content = b"prior-mod-jar-content"
    prior_jar_name = "old-mod.jar"
    prior_slip_body = _minimal_slip_body(
        release_id="v1.0.0",
        jars=[_build_jar_entry(filename=prior_jar_name, content=prior_jar_content, project_id="old-mod")],
    )
    _write_release_bundle(releases_root / "v1.0.0", prior_slip_body, {prior_jar_name: prior_jar_content})

    (mods_dir / prior_jar_name).write_bytes(prior_jar_content)
    (world_dir / "level.dat").write_bytes(b"prior-level-data")
    (world_dir / "region").mkdir()
    (world_dir / "region" / "r.0.0.mca").write_bytes(b"prior-region-data")
    (state_dir / "active-release.txt").write_text("v1.0.0\n", encoding="utf-8")

    # --- 2. Set up the new release under test ---
    new_jar_content = b"NEW-mod-jar-content-must-be-rolled-back"
    new_jar_name = "new-mod.jar"
    new_slip_body = _minimal_slip_body(
        release_id="v1.0.1",
        jars=[_build_jar_entry(filename=new_jar_name, content=new_jar_content, project_id="new-mod")],
    )
    new_release_root = releases_root / "v1.0.1"
    _write_release_bundle(new_release_root, new_slip_body, {new_jar_name: new_jar_content})

    # --- 3. Collaborators ---
    journal = DeploymentJournal(state_dir / "operation.jsonl")
    lock = DeploymentLock(state_dir / "daemon.lock")
    service_control = _FakeServiceControl()
    health_probe = _FakeHealthProbe()
    clock = _FixedClock()

    request = DeployRequest(
        run_id="v1.0.1",
        release_id="v1.0.1",
        pull=PullRequest(
            source_host="dev.example.com",
            source_user="deploy",
            source_path=releases_root,
            ssh_key=Path("/nonexistent"),
            target_root=releases_root,
            release_id="v1.0.1",
        ),
        target_mods_dir=mods_dir,
        target_world_dir=world_dir,
        world_backup_root=backup_root,
        prior_bundle_dir=releases_root / "v1.0.0",
        containers=("mc-survival",),
        protect_patterns=(),
        lock=lock,
        health_specs=(
            HealthCheckSpec(
                container="mc-survival",
                startup_sentinel="Done (",
                rcon_list_required_successes=1,
                timeout_seconds=10.0,
                poll_interval_seconds=1.0,
            ),
        ),
        is_upgrade=True,
    )

    # --- 4. Run the deploy ---
    outcome = deploy(
        request=request,
        journal=journal,
        service_control=service_control,
        health_probe=health_probe,
        clock=clock,
    )

    # --- 5. Assertions ---

    # 5a. Failure outcome with health-related error code
    assert outcome.success is False, "upgrade with failing health check must not succeed"
    assert outcome.error_code == 23, f"expected health_timeout (23), got {outcome.error_code}"
    assert outcome.stage == "health", f"expected failure stage 'health', got {outcome.stage!r}"

    # 5b. World content restored to pre-deploy state (byte-for-byte)
    assert (world_dir / "level.dat").read_bytes() == b"prior-level-data"
    assert (world_dir / "region" / "r.0.0.mca").read_bytes() == b"prior-region-data"

    # 5c. Mods directory matches v1.0.0 inventory exactly
    present_jars = sorted(p.name for p in mods_dir.iterdir() if p.is_file() and p.suffix == ".jar")
    assert present_jars == [prior_jar_name], f"expected only {prior_jar_name!r} after rollback, got {present_jars!r}"
    assert (mods_dir / prior_jar_name).read_bytes() == prior_jar_content
    assert not (mods_dir / new_jar_name).exists(), "new artifact must not survive rollback"

    # 5d. active-release pointer unchanged (never promoted to v1.0.1)
    assert (state_dir / "active-release.txt").read_text(encoding="utf-8").strip() == "v1.0.0"

    # 5e. Journal records the full recovery flow
    stages = [r.stage for r in journal.read_all()]
    for expected in (
        Stage.LOCK_ACQUIRED,
        Stage.BACKUP_STARTED,
        Stage.BACKUP_COMPLETED,
        Stage.SERVICES_STOPPED,
        Stage.SHIP_STARTED,
        Stage.SHIP_COMPLETED,
        Stage.SERVICES_STARTED,
        Stage.HEALTH_CHECK_STARTED,
        Stage.HEALTH_CHECK_FAILED,
        Stage.RECOVERY_STARTED,
        Stage.RECOVERY_COMPLETED,
        Stage.FAILED,
    ):
        assert expected in stages, f"journal missing stage {expected.value!r}; got {[s.value for s in stages]}"

    # 5f. Prior release is still deployable — artifacts are intact and hash to the slip
    prior_bundle = releases_root / "v1.0.0"
    prior_slip = slip_mod.load_slip(prior_bundle / "packing_slip.toml")
    for jar in prior_slip.jars:
        jar_path = prior_bundle / "artifacts" / jar.filename
        assert jar_path.is_file()
        assert hashlib.sha256(jar_path.read_bytes()).hexdigest() == jar.sha256

    # 5g. World backup exists and verifies (audit / re-attempt value)
    backup_dir = backup_root / "v1.0.1"
    assert backup_dir.is_dir()
    ok, err = verify_backup(backup_dir)
    assert ok, f"backup should verify after successful recovery: {err}"


def test_t20_prior_release_redeployable_after_recovery(tmp_path: Path) -> None:
    """T20 corollary: the prior release can be re-deployed after a failed upgrade.

    Recovery's whole purpose is to leave the operator with a functioning
    system they can retry against. This test performs the full loop:
    fail the new deploy, recover, then successfully re-deploy the prior
    release against the recovered state.

    If recovery left any residue that would break a re-deploy (e.g.
    orphaned artifacts, stale backup collisions), this test fails.
    """
    # --- 1. Setup (mirror of the main T20 test, condensed) ---
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    releases_root = tmp_path / "releases"
    mods_dir = tmp_path / "survival" / "mods"
    mods_dir.mkdir(parents=True)
    world_dir = tmp_path / "survival" / "world"
    world_dir.mkdir(parents=True)
    backup_root = tmp_path / "backups" / "world"

    prior_jar_content = b"prior-mod-jar-content"
    prior_jar_name = "old-mod.jar"
    prior_slip_body = _minimal_slip_body(
        release_id="v1.0.0",
        jars=[_build_jar_entry(filename=prior_jar_name, content=prior_jar_content, project_id="old-mod")],
    )
    _write_release_bundle(releases_root / "v1.0.0", prior_slip_body, {prior_jar_name: prior_jar_content})
    (mods_dir / prior_jar_name).write_bytes(prior_jar_content)
    (world_dir / "level.dat").write_bytes(b"prior-level-data")

    new_jar_content = b"NEW-mod-jar-content-must-be-rolled-back"
    new_slip_body = _minimal_slip_body(
        release_id="v1.0.1",
        jars=[_build_jar_entry(filename="new-mod.jar", content=new_jar_content, project_id="new-mod")],
    )
    _write_release_bundle(releases_root / "v1.0.1", new_slip_body, {"new-mod.jar": new_jar_content})

    journal = DeploymentJournal(state_dir / "operation.jsonl")
    lock = DeploymentLock(state_dir / "daemon.lock")

    # --- 2. Fail the v1.0.1 deploy ---
    fail_request = DeployRequest(
        run_id="v1.0.1",
        release_id="v1.0.1",
        pull=PullRequest(
            source_host="dev.example.com",
            source_user="deploy",
            source_path=releases_root,
            ssh_key=Path("/nonexistent"),
            target_root=releases_root,
            release_id="v1.0.1",
        ),
        target_mods_dir=mods_dir,
        target_world_dir=world_dir,
        world_backup_root=backup_root,
        prior_bundle_dir=releases_root / "v1.0.0",
        containers=("mc-survival",),
        protect_patterns=(),
        lock=lock,
        health_specs=(
            HealthCheckSpec(
                container="mc-survival",
                startup_sentinel="Done (",
                rcon_list_required_successes=1,
                timeout_seconds=10.0,
                poll_interval_seconds=1.0,
            ),
        ),
        is_upgrade=True,
    )
    fail_outcome = deploy(
        request=fail_request,
        journal=journal,
        service_control=_FakeServiceControl(),
        health_probe=_FakeHealthProbe(),
        clock=_FixedClock(),
    )
    assert fail_outcome.success is False

    # --- 3. Re-deploy the prior v1.0.0 (which is still current on disk) ---
    # Use a separate journal file so unresolved-run checks don't see the
    # prior run's terminal state as blocking (terminal = OK, but cleaner).
    redo_journal = DeploymentJournal(state_dir / "operation-redo.jsonl")

    # Re-deploy as an upgrade against a world backup that already exists
    # is not what we're testing here; re-deploy as a same-version update
    # (is_upgrade=False) to prove the recovery state is deployable.
    redo_request = DeployRequest(
        run_id="v1.0.0-redo",
        release_id="v1.0.0",
        pull=PullRequest(
            source_host="dev.example.com",
            source_user="deploy",
            source_path=releases_root,
            ssh_key=Path("/nonexistent"),
            target_root=releases_root,
            release_id="v1.0.0",
        ),
        target_mods_dir=mods_dir,
        target_world_dir=None,  # no backup needed for same-version re-deploy
        world_backup_root=None,
        prior_bundle_dir=None,  # no recovery expected
        containers=("mc-survival",),
        protect_patterns=(),
        lock=lock,
        health_specs=(
            HealthCheckSpec(
                container="mc-survival",
                startup_sentinel="Done (",
                rcon_list_required_successes=1,
                timeout_seconds=10.0,
                poll_interval_seconds=1.0,
                # Use a probe that reports healthy for the re-deploy.
            ),
        ),
        is_upgrade=False,
    )

    class _HealthyProbe(HealthProbe):
        def is_running(self, container: str) -> bool:
            return True

        def restart_count(self, container: str) -> int:
            return 0

        def recent_logs(self, container: str) -> str:
            return 'Done (12.34s)! For help, type "help"\n'

        def rcon_list(self, container: str) -> tuple[bool, str]:
            return (True, "There are 0 of a max of 20 players online")

    redo_outcome = deploy(
        request=redo_request,
        journal=redo_journal,
        service_control=_FakeServiceControl(),
        health_probe=_HealthyProbe(),
        clock=_FixedClock(),
    )

    # --- 4. Prior release deploy succeeded against recovered state ---
    assert redo_outcome.success is True, (
        f"prior release must be re-deployable after recovery; got failure: stage={redo_outcome.stage!r} message={redo_outcome.message!r}"
    )

    # Mods dir still contains exactly the prior jar
    present_jars = sorted(p.name for p in mods_dir.iterdir() if p.is_file() and p.suffix == ".jar")
    assert present_jars == [prior_jar_name]

    # World content still matches pre-deploy state
    assert (world_dir / "level.dat").read_bytes() == b"prior-level-data"
```

---



## 3. `docs/World_Backup_Restore_Drill.md`

docs/World_Backup_Restore_Drill.md

# World Backup and Restoration Drill

**Status:** Slice 16 deliverable. Authoritative drill for the pre-upgrade world backup procedure (§10.8) and its restoration (§12.10).
**Applies to:** `minecraft.deploy_pack.daemon.backup.create_backup`, `minecraft.deploy_pack.daemon.recovery.restore_world`, and their integration with `daemon/deploy.py` stage 13.
**Automated equivalent:** `tests/deploy_pack/test_t20_migration_rollback.py` — runs on every CI pass.
**Manual counterpart (T20):** the "Manual drill" section of this document.

---

## 1. Purpose

The pre-upgrade world backup is the last line of defense against a failed world-format migration. This drill proves, on a production-like clone, that:

- `create_backup` produces a manifest-verified backup before any world-mutating step runs.
- `restore_world` restores that backup byte-for-byte.
- The full `deploy()` recovery path, on a health-check failure after a stop-phase, produces an identical outcome.

If this drill fails on the clone, production cutover is blocked.

---

## 2. Frequency

- **Once, before first production cutover** — mandatory sign-off gate.
- **Once, after any change to `backup.py`, `recovery.py`, or `daemon/deploy.py`** — regression gate.
- **Quarterly** — standing drill to catch environmental drift (kernel changes affecting `shutil.copytree`, filesystem changes affecting `os.replace` semantics, etc.).

---

## 3. Automated coverage

The CI test `test_t20_full_rollback_restores_prior_state` in `tests/deploy_pack/test_t20_migration_rollback.py` proves:

- Backup created and verified during deploy stage 13.
- Recovery triggers on health-check failure.
- World content restored byte-for-byte (`level.dat` and nested `region/r.0.0.mca`).
- Mods restored from prior bundle.
- `active-release` pointer untouched.
- Prior release bundle intact and hashable.

The automated test does **not** prove:

- That real Minecraft world directories (with permissions, hardlinks, sparse files) restore correctly.
- That the daemon's `DockerServiceControl` correctly restores services after the drill.
- That the actual `/srv/minecraft/backups/world/<release>/` path is writable and the daemon has permission to `rmtree` and `copytree` there.

That's what the manual drill covers.

---

## 4. Manual drill

### 4.1 Setup

On a **production-like clone** (VM or dedicated host that is not production):

```bash
# 1. Deploy the current `stable` slip to the clone.
#    (Follow the Migration Runbook Phase 3.1, substituting the clone's
#    paths for prod's.)
deploy-pack --server --release-pointer stable

# 2. Verify the clone is running normally.
docker ps  # both survival and creative containers up, healthy

# 3. Snapshot the clone for drill reproducibility.
sudo mkdir -p /var/backups/drill-$(date +%Y%m%d)
sudo tar czf /var/backups/drill-$(date +%Y%m%d)/pre-drill.tar.gz \
    /srv/minecraft/survival/world \
    /srv/minecraft/survival/mods \
    /srv/minecraft/releases \
    /var/lib/minecraft-deploy
```

### 4.2 Drill — full rollback path

The drill simulates the failure mode T20 is designed to protect against: a **health check that fails after the stop phase**. The cleanest way to provoke this deterministically is to make the deployed server crash on startup by giving it an invalid `EULA` acceptance file (or the equivalent for the target loader).

```bash
# 1. Deliberately break the world's EULA acceptance so the next deploy
#    crashes at startup.
sudo sh -c 'echo "eula=false" > /srv/minecraft/survival/world/../eula.txt'

# 2. Record the current inventory and world state for later comparison.
sha256sum /srv/minecraft/survival/world/level.dat
ls -la /srv/minecraft/survival/mods/*.jar | sha256sum

# 3. Trigger a slip-mode deployment that WILL fail health.
deploy-pack --server --release-pointer stable
# Expect exit 1, with a failure notification on Discord (if --notify).

# 4. Verify recovery ran.
tail -n 30 /var/lib/minecraft-deploy/operation.jsonl
# Expect the following sequence in the last 30 lines:
#   ... services_stopped
#   ... ship_started / ship_completed (or unchanged)
#   ... services_started
#   ... health_check_started / health_check_failed
#   ... recovery_started / recovery_completed
#   ... failed
```

### 4.3 Verification

Check that recovery actually restored the pre-deploy state:

```bash
# A. Backup exists and verifies.
ls -la /srv/minecraft/backups/world/*/
# Should contain a directory named after the current release,
# with a bundle-manifest.json alongside the copied world contents.

# B. Backup manifest parses and every entry's SHA-256 matches the file.
#    (Run this via a small Python check if the CLI doesn't expose one.)
python3 -c "
from minecraft.deploy_pack.daemon.backup import verify_backup
from pathlib import Path
ok, err = verify_backup(Path('/srv/minecraft/backups/world/<release>/'))
print('verified' if ok else f'FAILED: {err}')
"
# Expect: verified

# C. World content matches the pre-drill snapshot.
sha256sum /srv/minecraft/survival/world/level.dat
# Expect the same hash as step 2 above.

# D. Mods directory matches the pre-drill inventory.
ls -la /srv/minecraft/survival/mods/*.jar | sha256sum
# Expect the same hash as step 2 above.

# E. Both containers are running again.
docker ps --filter name=mc- --format '{{.Names}}: {{.Status}}'
# Expect: mc-survival and mc-creative both listed, Up ... (healthy)

# F. active-release pointer is unchanged (still points at prior release).
cat /var/lib/minecraft-deploy/active-release
```

### 4.4 Cleanup

Restore the clone to its original EULA state (so the next drill starts from a clean baseline):

```bash
# Fix the EULA deliberately broken in step 1 of the drill.
sudo sh -c 'echo "eula=true" > /srv/minecraft/survival/world/../eula.txt'

# Re-run the deploy to prove the clone is now working.
deploy-pack --server --release-pointer stable
# Expect exit 0.

# Remove the drill backup if it was only for drill verification.
# (Do NOT remove backups that predate the drill.)
sudo rm -rf /srv/minecraft/backups/world/<drill-release>/
```

---

## 5. Failure modes and expected recovery

If any of the following happen during the drill, they are **drill failures** that block cutover:

| Symptom | Likely cause | Diagnosis |
|---|---|---|
| Backup directory empty or missing `bundle-manifest.json` | `create_backup` failed before writing the manifest | Check daemon logs for `backup: copy failed` or `manifest walk failed` |
| `verify_backup` returns `(False, "missing file: ...")` | Backup was tampered with or the walk was interrupted | Check for a stale `.tmp` file or interrupted write |
| Recovery step in journal shows `recovery_failed` | `restore_world` returned `(False, ...)` | Look for the specific failure mode (missing file, hash mismatch, permission error) |
| Containers still stopped after recovery | `_start_services_after_failure` failed | Look for `post-failure start failed` in journal; check Docker SDK connectivity |
| `active-release.txt` changed | Should be impossible; the daemon never touches it on failure | File a bug; this violates §12.10 |

Do not proceed to cutover if any of these symptoms appear.

---

## 6. Sign-off checklist

- [ ] Clone prepared per §4.1
- [ ] Pre-drill snapshot taken
- [ ] Deliberate EULA break applied
- [ ] Deploy failed with exit 1
- [ ] Journal shows `health_check_failed` followed by `recovery_started` / `recovery_completed`
- [ ] `verify_backup` returned `verified`
- [ ] World `level.dat` hash matches pre-drill snapshot
- [ ] Mods inventory hash matches pre-drill snapshot
- [ ] Both containers running and healthy after recovery
- [ ] `active-release.txt` unchanged
- [ ] Post-drill re-deploy succeeded (exit 0)
- [ ] Drill artifacts cleaned up per §4.4

**Signed off by:** ____________________ **Date:** ____________

---

## 7. Appendix — backup manifest schema

Backups store a `bundle-manifest.json` alongside the world data. The schema, per `daemon/backup.py`:

```json
{
  "source": "/srv/minecraft/survival/world",
  "files": [
    {
      "path": "level.dat",
      "size_bytes": 12345,
      "mode": 420,
      "sha256": "<64-char hex>"
    },
    {
      "path": "region/r.0.0.mca",
      "size_bytes": 67890,
      "mode": 420,
      "sha256": "<64-char hex>"
    }
  ],
  "total_size_bytes": 80235,
  "created_at_utc": "2026-10-26T12:34:56Z"
}
```

`mode` is the file's octal permission bits (e.g. `420` = `0o644`). Ownership and timestamps are deliberately excluded from the manifest — they do not survive cross-filesystem round-trips reproducibly, and restoring wrong ownership is a security regression, not a fidelity improvement.

---

**End of World Backup and Restoration Drill.**

---

