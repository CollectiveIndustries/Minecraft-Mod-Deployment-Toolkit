## 2. `docs/Migration_Runbook.md`

docs/Migration_Runbook.md

# Migration Runbook

**Status:** Slice 16 deliverable. Authoritative procedure for migrating production from the legacy `deploy-pack` path to slip-mode `deploy-pack --release-pointer stable`.
**Applies to:** spec v1.4.0 (§19 Migration & Coexistence, §19.4 Rollback, §6 Publication, §12 Production Daemon).
**Rehearsal:** **T20** (see `docs/World_Backup_Restore_Drill.md` and `tests/deploy_pack/test_t20_migration_rollback.py`).

---

## 1. Purpose

This runbook moves production off the legacy deploy path and onto the slip-mode path in a way that is **reversible at every step** and **provably lossless under rollback**.

The end state of this runbook is: production deployments are performed via `deploy-pack --release-pointer stable`; the legacy `deploy-pack` path remains present but is no longer the primary interface. `side_overrides.toml`, `.deploy_protect`, and `overrides.py` are **not** retired by this runbook — that is a separate, later removal (§19.3) that may be scheduled weeks after cutover.

---

## 2. Preconditions

All of the following must be true before Phase 1 begins:

- [ ] Spec v1.4.0 §20 acceptance tests pass: **36/36** (T11 and T20 are the final two).
- [ ] Slice 15 Discord bot is deployed on DEV and `/upgrade status` returns successfully via Discord.
- [ ] `sync/packing_slips/` exists in the git repo and contains at least one committed slip (produced via `promote`).
- [ ] `sync/packing_slips/release-index.jsonl` exists and its chain verifies via `minecraft-upgrade publish --verify-only --all-releases`.
- [ ] `www/releases/` on DEV contains at least one published immutable bundle.
- [ ] `config.d/trusted_keys.toml` is present, and its pinned keys match the DEV signing key.
- [ ] Prod has been snapshotted at the filesystem level (VM snapshot, host backup, or equivalent) — this is a safety net independent of the upgrade toolchain.
- [ ] Prod's Docker daemon has been verified to reach DEV's publication directory via rsync-over-SSH.
- [ ] `docs/OCI_Registry_Allowlist.md` procedure has been executed; prod's image references have been pinned by digest.
- [ ] The T20 drill in `docs/World_Backup_Restore_Drill.md` has been executed on a production-like clone and signed off.

---

## 3. Phase 1 — Deploy the legacy baseline (v1.0.0)

The purpose of this phase is to record production's **current** state as an approved slip before anything changes. If we later need to roll back the migration, the v1.0.0 slip is what we roll back to.

### 3.1 Procedure

On **DEV**:

```bash
# 1. Verify the current state is deployable.
minecraft-upgrade manifest check
minecraft-upgrade status

# 2. Resolve and materialize the current state as a legacy baseline.
minecraft-upgrade update --release-mode update
# Note the run-id from the output; assume "run-baseline" below.

# 3. Materialize the candidate slip.
minecraft-upgrade materialize --run-id run-baseline

# 4. Boot-test on DEV to prove the candidate actually runs.
minecraft-upgrade iterate --run-id run-baseline

# 5. Approve via the Discord bot (this validates the whole T11 path
#    on a live interaction).
#    In Discord: /upgrade approve run-baseline

# 6. Promote the slip under the tag v1.0.0.
minecraft-upgrade promote v1.0.0

# 7. Commit, tag, push.
git commit -m "Release v1.0.0: legacy baseline"
git tag -a v1.0.0 -m "Legacy baseline (migration start)"
git push origin main --tags

# 8. Publish the immutable release bundle.
minecraft-upgrade publish v1.0.0

# 9. Verify the chain end-to-end.
minecraft-upgrade publish --verify-only v1.0.0

# 10. Mark v1.0.0 stable.
echo "v1.0.0.toml" > sync/packing_slips/stable
git add sync/packing_slips/stable
git commit -m "Mark v1.0.0 stable"
git push
```

### 3.2 Verification

- [ ] `minecraft-upgrade publish --verify-only v1.0.0` returns "overall: OK".
- [ ] `www/releases/v1.0.0/` exists on DEV, contains `bundle-manifest.json`, `bundle-manifest.sig`, `packing_slip.toml`, `approval.json`, `config/`, `kubejs/`, `artifacts/`.
- [ ] `sync/packing_slips/v1.0.0.toml` is committed and its `content_sha256` verifies.
- [ ] `sync/packing_slips/stable` reads `v1.0.0.toml`.

### 3.3 Rollback from Phase 1 failure

If any step fails, no production behavior has changed. Fix the underlying problem and re-run from step 1. The `v1.0.0` slip and bundle are not created until step 6+7, so re-running is safe.

If step 6 (promote) succeeded but a later step failed, remove the staged slip and re-run:

```bash
git restore --staged sync/packing_slips/v1.0.0.toml sync/downloads/.index/
rm -f sync/packing_slips/v1.0.0.toml
# If publish already committed the bundle, remove it too.
rm -rf www/releases/v1.0.0/
# If a registry line was appended, DO NOT delete it; the chain is
# append-only. Instead, note the failure and escalate.
```

The registry chain is append-only. If `publish` appended an entry before failing, the chain now reflects an unreachable-by-tag entry. **Do not delete lines from the registry.** Instead, diagnose why publish failed, fix, and re-publish — the second publish will append a new entry with the corrected bundle hash. The chain then contains both, and the operator's `stable` pointer chooses which is authoritative.

---

## 4. Phase 2 — Dual-path verification

The purpose of this phase is to prove both paths work against the same v1.0.0 state, before either becomes the sole path.

### 4.1 Legacy path dry-run (prod, read-only)

On **prod**:

```bash
deploy-pack --server --dry-run
```

- [ ] Completes without preflight errors.
- [ ] Report shows the current state as unchanged (no drift from v1.0.0 slip).

### 4.2 Slip-mode path dry-run (prod, read-only)

On **prod**:

```bash
deploy-pack --server --dry-run --release-pointer stable
```

- [ ] Completes without preflight errors.
- [ ] Slip `[runtime]` reconciles against prod's effective Compose (release-mode update check).
- [ ] `[inputs.git]` tree hashes match prod's working tree.
- [ ] Plan shows the current state as unchanged.

### 4.3 Integration path dry-run (DEV, read-only)

On **DEV**:

```bash
deploy-pack --server --dry-run --integration --release-pointer latest-slip
```

- [ ] Completes without preflight errors.
- [ ] Config/KubeJS tree hashes are **not** enforced (integration mode).
- [ ] Plan shows the current candidate state as unchanged.

### 4.4 Rollback from Phase 2 failure

No production state was touched (all dry-runs). Fix the discrepancy and re-run. If the discrepancy is in the v1.0.0 slip itself, return to Phase 3.1 step 6 to re-promote.

---

## 5. Phase 3 — Cutover

The purpose of this phase is to make slip-mode the primary path.

### 5.1 Procedure

On **prod**:

```bash
# 1. Take a final pre-cutover backup of the legacy state.
#    This is a filesystem backup (tar) of:
#    - the current toolchain checkout
#    - config.d/ (including deploy_pack.toml and side_overrides.toml)
#    - sync/downloads/ inventory (SHAs only; jars are on disk and rebuilt)
#    - active-release.txt
# 2. Store the backup at a path outside any directory the upgrade toolchain
#    manages, e.g. /var/backups/minecraft-upgrade/pre-cutover/
```

```bash
# 3. Run the first slip-mode deployment (a no-op against v1.0.0, but it
#    exercises the whole path against prod).
deploy-pack --server --release-pointer stable
```

### 5.2 Verification

- [ ] `deploy-pack --server --release-pointer stable` completes with exit 0.
- [ ] Prod's `mods/` matches the v1.0.0 slip's server-side jars exactly.
- [ ] `active-release.txt` on prod now reads `v1.0.0` (written by the daemon on success — should already say this).
- [ ] `deploy-pack --server --dry-run` (legacy) and `deploy-pack --server --dry-run --release-pointer stable` (slip) both agree the state is unchanged.
- [ ] No failures journaled in `/var/lib/minecraft-deploy/operation.jsonl` on prod.

### 5.3 Rollback from Phase 3 failure

If `deploy-pack --release-pointer stable` fails:

1. The failure is either pre-stop (no state changed) or post-stop (recovery runs automatically).
2. Inspect `/var/lib/minecraft-deploy/operation.jsonl` on prod for the terminal stage.
3. If the daemon recovered successfully, prod is still on v1.0.0. Diagnose the slip-mode failure and re-run Phase 5.1.
4. If the daemon's recovery also failed (`RECOVERY_FAILED` in journal), restore from the pre-cutover backup:
   ```bash
   # On prod
   systemctl stop minecraft-deploy-daemon
   tar xzf /var/backups/minecraft-upgrade/pre-cutover/toolchain.tar.gz -C /
   tar xzf /var/backups/minecraft-upgrade/pre-cutover/config.tar.gz -C /srv/minecraft/
   # Verify the restored state
   deploy-pack --server --dry-run  # legacy path, should show unchanged
   systemctl start minecraft-deploy-daemon
   ```
5. The registry chain is still intact (it was never modified by a failed deploy). Do not touch it.

---

## 6. Phase 4 — Post-cutover verification

Wait 7 days after Phase 3, during which at least one normal deployment activity must occur (e.g. a config change, a mod update, a resource-pack refresh).

### 6.1 Procedure

During the 7-day window:

- [ ] At least one slip-mode deployment succeeds end-to-end (config/kubejs change is sufficient; no mod change required).
- [ ] The Discord bot's `/upgrade status` reports the post-deployment state correctly.
- [ ] The daemon's `/status` HTTP endpoint reports `healthy`.

After the 7-day window:

```bash
# On prod
deploy-pack --server --dry-run --release-pointer stable
# Should show no drift.
```

### 6.2 Rollback from Phase 4 failure

If a slip-mode deployment fails during the 7-day window, the same rollback procedure as §5.3 applies. **Do not proceed to Phase 5 until a full week passes with no failures.**

---

## 7. Phase 5 — Retire legacy mode (separate, later)

This is **out of scope for the migration runbook**. Retirement of the legacy path (§19.3) is a separate activity that may be scheduled weeks or months later, after the operator has full confidence in slip mode. It removes `side_overrides.toml`, `.deploy_protect`, `overrides.py`, and moves `deps.py` into `modpack_upgrade`.

**Do not delete any of these files as part of the migration runbook.** Cutover leaves the legacy path fully present and functional.

---

## 8. Sign-off checklist

Each item is signed off by the operator running the migration.

**Phase 1 — Baseline**
- [ ] v1.0.0 slip committed and tagged
- [ ] v1.0.0 bundle published and verifies
- [ ] `stable` points at v1.0.0

**Phase 2 — Dual-path**
- [ ] Legacy dry-run clean on prod
- [ ] Slip-mode dry-run clean on prod
- [ ] Integration dry-run clean on DEV

**Phase 3 — Cutover**
- [ ] First slip-mode deploy succeeded on prod
- [ ] `active-release.txt` on prod reads `v1.0.0`
- [ ] Legacy and slip dry-runs agree state is unchanged

**Phase 4 — Post-cutover**
- [ ] At least one slip-mode deploy succeeded within 7 days
- [ ] No failures journaled
- [ ] 7-day quiet window passed

**Signed off by:** ____________________ **Date:** ____________

---

## 9. Appendix — emergency contact paths

- **Spec:** `docs/Upgrade_System_specifications.md` §19, §12.
- **Daemon health:** `curl http://127.0.0.1:8765/status` (tailscale required).
- **Journal:** `/var/lib/minecraft-deploy/operation.jsonl`.
- **Pre-cutover backup:** `/var/backups/minecraft-upgrade/pre-cutover/`.
- **Registry chain:** `sync/packing_slips/release-index.jsonl` (never delete lines).

---

**End of Migration Runbook.**

---