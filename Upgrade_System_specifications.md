# Minecraft Upgrade System — Specification v1.0

**Status:** Design of record. Not yet implementation-ready — pending migration runbook, packing slip schema freeze, and `.pw.toml` writer contract.
**Supersedes:** `Upgrade_System_specifications.md` v0.1.0
**Baseline contract:** `Project_Specs.md` v3.0 (`deploy_pack` v2)
**Date:** 2026-10-09

---

## 1. Purpose

`deploy_pack` v2 applies an already-materialized artifact set. This specification defines the layer that **constructs, validates, and freezes** that artifact set:

1. Resolve exact mod versions from upstream providers against a target Minecraft/loader runtime.
2. Download artifacts once into a per-host warehouse.
3. Build in-house mods against the candidate runtime.
4. Iterate a dev server against crash logs until the candidate pack stabilizes.
5. Validate a copy of the production world against the candidate.
6. Produce a downloadable client pack.
7. Freeze the tested state into a git-tagged **packing slip**.
8. Hand the slip to a production daemon that fetches and deploys the exact tested artifacts.

The system is an extension of `deploy_pack`, not a replacement. Production deployment lifecycle remains owned by the v2 contract.

---

## 2. Normative Language

- **MUST / MUST NOT** — mandatory for conformance.
- **SHOULD / SHOULD NOT** — default requirement; deviations need documented reason.
- **MAY** — optional.

---

## 3. Three-Machine Topology

The system operates across three distinct machines on the same trusted network.

| Machine | Role |
|---|---|
| **Client-side** | Authoring. Pushes code, configs, kubejs, in-house mod source to git. Runs the Minecraft client for in-game review. |
| **Dev server** | Resolution, in-house builds, validation, migration testing, client ZIP publication. Headless. |
| **Prod server** | Fetches release artifacts, deploys via `deploy_pack`, runs health checks. Hosts the production daemon. |

**Constraints:**
- Prod MUST NOT receive binaries via rsync from client or dev.
- Prod MUST fetch release artifacts from upstream providers, or from dev's artifact store for in-house jars.
- Dev MUST NOT pull mods from prod (prod runs an older runtime during upgrade).
- Client MUST NOT be a runtime dependency for dev's resolution loop.

---

## 4. Storage Model

### 4.1 Repository layout

```
repo/
├── config.d/
│   ├── deploy_pack.toml
│   ├── upgrade.toml              # upgrade-system config (new)
│   ├── side_overrides.toml       # active today; retired after cutover
│   └── .env
├── src/minecraft/
│   ├── common/
│   ├── deploy_pack/
│   └── modpack_upgrade/          # new subsystem
├── sync/
│   ├── audit/
│   ├── config/                    # git-tracked
│   ├── downloads/                 # warehouse: flat jars
│   │   └── *.jar
│   ├── kubejs/                    # git-tracked
│   ├── packing_slips/             # NEW: git-tracked release contracts
│   │   ├── v1.0.0.toml
│   │   ├── v1.0.1.toml
│   │   └── current -> v1.0.1.toml
│   └── velocity-plugins/
├── www/                            # artifact store
│   ├── runs/<run-id>/              # dev-run artifacts
│   ├── releases/<tag>/             # approved release artifacts
│   └── artifacts/
│       └── in-house/<slug>/<build-id>/<file>.jar
├── tests/
├── docker-compose.yml
└── pyproject.toml
```

### 4.2 Invariants

- `sync/downloads/` is a flat directory. One copy per unique artifact filename.
- `mods/` at the instance root is the runtime deployment target.
- `packing_slips/` is git-tracked. Slips are text files.
- `www/` on dev accumulates run artifacts. `www/` on prod accumulates release artifacts.
- No symlinks in the `mods/` deployment path. Shipping writes real files.
- One physical copy per unique artifact per host, except the deployed copy in `mods/`.

---

## 5. Packing Slip Contract

### 5.1 Purpose

The packing slip is the immutable release registry. It declares which artifacts, configs, kubejs, and assets belong to a release, and identifies the runtime they were tested against.

### 5.2 Schema

```toml
packing_slip_version = 1
release_id = "v1.0.1"                    # immutable; re-use requires new ID
content_sha256 = "..."                   # canonical body hash

[meta]
created_at_utc = "2026-10-09T00:00:00Z"
resolver_version = "minecraft-upgrade 1.0.0"
author = "<operator>"

[runtime]
minecraft_version = "1.20.1"
loader_type = "forge"
loader_version = "47.4.10"
image_reference = "itzg/minecraft-server:java25"
image_digest = "sha256:..."
platform = "linux/amd64"
java_version = "21"

[inputs.git]
config_tree     = "<git tree hash of sync/config>"
kubejs_tree     = "<git tree hash of sync/kubejs>"
compose_tree    = "<git tree hash of docker-compose.yml>"
deploy_toml_tree = "<git tree hash of config.d/deploy_pack.toml>"

[tested]
world = "survival"                       # which world passed migration validation
snapshot_id = "<snapshot-id>"

[[jar]]
filename = "create-1.20.1-6.0.8.jar"
sha256 = "abc..."
side = "both"
provider = "modrinth"
project_id = "..."
version_id = "..."
pw_toml = "create-1.20.1-6.0.8.pw.toml"
pw_toml_sha256 = "..."
dependencies = [
  { namespace = "project", provider = "modrinth", project_id = "...", version_id = "...", type = "required" },
]

[[in_house_jar]]
filename = "custom-mod-1.0.0.jar"
sha256 = "..."
side = "both"
build_id = "<build-id>"
source_repo = "..."
source_commit = "<git sha>"
toolchain_id = "<toolchain-id>"
pw_toml = "custom-mod-1.0.0.pw.toml"
pw_toml_sha256 = "..."

[[resource_pack]]
filename = "survival-pack.zip"
sha256 = "..."
instance = "survival"
required = true
prompt = "..."

[[velocity_plugin]]
filename = "spark-1.10.187-velocity.jar"
sha256 = "..."

[client_distribution]
output_filename = "minecraft_client_20261009.zip"
sha256 = "..."
with_resources = true

[instances.survival]
targeted = true
required_sides = ["both", "server"]
config_mode = "merge"
kubejs_mode = "delete"
protect_patterns = []

[instances.creative]
targeted = true
required_sides = ["both", "server"]
config_mode = "merge"
kubejs_mode = "delete"
protect_patterns = []
```

### 5.3 Rules

1. `release_id` MUST be immutable. Re-using an ID with different content MUST fail.
2. `content_sha256` MUST be computed over the canonical body (see §5.4).
3. `[inputs.git]` tree hashes MUST match the working tree at deploy time. Mismatch blocks deploy.
4. `[tested].world` MUST be `survival` or `creative`, or absent if no migration test was required.
5. `[[jar]]` entries MUST resolve to a file in `sync/downloads/` with matching SHA-256 at deploy time.
6. `[[in_house_jar]]` entries MUST resolve to a `www/artifacts/in-house/` blob with matching SHA-256, or a mirrored artifact in the prod warehouse.
7. Every `[[jar]]` and `[[in_house_jar]]` MUST list its dependency edges with `namespace` (`project` or `modid`) and `source` (`pw_toml` or `jar_manifest`).
8. Every jar entry MUST reference a `.pw.toml` file whose SHA-256 is recorded.
9. `[instances.*]` MUST include every targeted production instance. A release that omits an instance MUST NOT deploy to it.
10. Protection patterns MUST be explicit per instance. `.deploy_protect` MUST NOT affect slip-mode deploys.

### 5.4 Canonical hashing

- UTF-8 encoding, NFC Unicode normalization.
- Keys sorted deterministically.
- Arrays representing sets sorted; semantically ordered arrays preserved.
- No floating-point values in identity fields.
- Paths relative to repo root, POSIX separators.
- Absent vs. empty values distinguished.
- SHA-256.

Distinct fingerprints:

| Fingerprint | Source |
|---|---|
| `content_sha256` | Canonical slip body |
| `manifest_sha256` | Raw `modpack.manifest.toml` bytes |
| `runtime_sha256` | Canonical runtime descriptor |
| `side_policy_sha256` | Raw `side_overrides.toml` (legacy mode only) |
| `server_inputs_sha256` | Deterministic manifest of server-stage inputs |
| `client_inputs_sha256` | Deterministic manifest of client-stage inputs |

---

## 6. Dev Pipeline

### 6.1 Dev runtime

- One Minecraft container. Headless.
- Velocity proxy available for integration stage.
- Discord bot as operator interface.
- Upgrade controller owns candidate state, artifact preparation, and container lifecycle.

### 6.2 Phase A — resolution and iteration

```
1. Read target runtime from docker-compose.
2. Load manifest (intent).
3. Query providers for each manifest root.
4. Select versions per policy (RELEASE → BETA → authorized ALPHA).
5. Resolve required dependency closure iteratively:
   - provider-declared dependencies
   - JAR-declared mod IDs
   - reject ambiguous mappings
6. Download selected artifacts to sync/downloads/.
7. Write .pw.toml for each.
8. Assemble candidate slip.
9. Loop:
   a. Ship candidate set to dev mods/.
   b. Boot container with throwaway world and minimal configs.
   c. Read logs, classify failures.
   d. Batch known fixes; add deps; swap versions.
   e. Restart.
   f. Respect caps.
10. Stable? → Phase B.
11. Ambiguous failure → pause, notify Discord, wait.
12. Cap reached → pause, notify Discord, wait.
```

### 6.3 Caps

| Limit | Value |
|---|---|
| Boot iterations | 15 max |
| Wall-clock duration | 45 min max |
| API requests | Configurable budget, journaled |
| Downloaded bytes | Configurable budget, journaled |

Cap-reached MUST pause, preserve state, notify Discord. MUST NOT drop mods or promote.

### 6.4 Phase B — mod-set approval

On stabilization:

- Publish run artifacts to `www/runs/<run-id>/`.
- Post Discord notification with link and approval buttons.
- Approval binds to candidate identity (slip body hash).
- User tests in-game; approves when workable.

### 6.5 Phase C — world migration

On mod-set approval:

- rsync prod world → dev (see §8).
- Preserve snapshot; make writable clone.
- Restart dev against clone + candidate pack + real configs/kubejs.
- Health check; publish migration report.
- Discord: link + approval button.
- Approval binds to candidate + snapshot identity.

### 6.6 Phase D — Velocity integration

After migration approval:

- Start dev Velocity proxy against dev backend.
- Verify forwarding, handshake, identity, connection.
- Publish report; no separate approval required (report-only in v1).

### 6.7 Phase E — promote

```
write sync/packing_slips/<tag>.toml
git add sync/packing_slips/<tag>.toml
git commit -m "Release <tag>: <summary>"
git tag -a <tag> -m "Shipping label"
git push origin main --tags
```

Tag MUST be unique. Slip MUST be signed by `content_sha256` verification.

---

## 7. In-House Mod Build Pipeline

### 7.1 Trigger

Explicit Discord command. No auto-build on push.

```
/upgrade build-inhouse <mod> <revision>
```

### 7.2 Flow

1. Resolve source revision to immutable commit SHA.
2. Clean isolated workspace.
3. Build with candidate's target Minecraft/loader runtime and recorded toolchain.
4. Hash resulting JAR.
5. Publish to `www/artifacts/in-house/<slug>/<build-id>/<file>.jar`.
6. Register as in-house artifact (CAS backing, web view via symlink).
7. Add to candidate slip; run standard candidate validation.

### 7.3 Build record

Must include:
- Source repository
- Immutable commit SHA
- Toolchain identity
- Target Minecraft + loader
- Build outcome (success/failure)
- Artifact SHA-256
- Dependency/build metadata for reproducibility

### 7.4 Rule

Compilation MUST NOT be treated as compatibility approval. The in-house JAR MUST pass the same validation as provider artifacts.

---

## 8. World Snapshot

### 8.1 Direction

Prod → dev. One-way.

### 8.2 Method

- Live `rsync --checksum`.
- No production stop.
- Per-file hash manifest.
- Snapshot preserved; writable clone created for testing.

### 8.3 Rationale

Stopping prod for snapshot costs ~45 minutes (stop + save-all + rsync + start). Accepted risk: torn chunks if a chunk writes during transfer. Mitigation: if migration test fails, re-run with `--stop-prod` to distinguish real incompat from torn read.

### 8.4 Retention

Snapshot MUST be retained until migration approval. After approval, MAY be discarded per retention policy.

### 8.5 Configuration

```toml
[world_snapshot]
source_host = "prod.example.com"
source_user = "deploy"
source_path = "/srv/minecraft/survival/world"
ssh_key = "~/.ssh/id_deploy_world_ro"
dest_root = "~/minecraft-dev/snapshots"
exclude = ["session.lock", "*.tmp", "*.lock"]
timeout_seconds = 1800
default_world = "survival"
stop_prod = false
```

`default_world` MUST be `survival` or `creative`. Discord command MAY override per-run.

---

## 9. Discord Interface

### 9.1 Bot

- `discord.py`
- Separate service on dev
- Narrow commands only

### 9.2 Commands

| Command | Purpose |
|---|---|
| `/upgrade status` | Current run state |
| `/upgrade build-inhouse <mod> <revision>` | Trigger in-house build |
| `/upgrade migrate-world [survival\|creative]` | Trigger world snapshot |
| `/upgrade approve` | Approve current prompt |
| `/upgrade reject [reason]` | Reject current prompt |

### 9.3 Rules

- Allowlist users/roles.
- Idempotent button clicks.
- Approval bound to candidate/snapshot identity.
- Approval state persisted.
- No arbitrary shell commands.
- Bot token outside git and out of logs.
- Bot MUST NOT be the controller; it submits structured actions to the upgrade controller via a narrow local interface.

### 9.4 Message format

Notifications MUST include a link to the run's `www/runs/<run-id>/` index.

Logs, crash reports, and diffs MUST be published as artifacts, not embedded in Discord messages.

---

## 10. Production Daemon

### 10.1 Role

Privileged production control plane. Owns verification, staging, deployment, health checks, and controlled recovery.

### 10.2 Exposure

- Tailscale-only HTTP endpoint.
- Authenticated.
- Replay-protected.
- Idempotent.

### 10.3 Inputs

- Immutable release ID (git tag)
- Expected git commit SHA
- Packing-slip content hash

MUST refuse: branch names, arbitrary commands, ambiguous "go" pokes.

### 10.4 Lifecycle

```
1. Fetch exact git tag/commit; verify slip.
2. Fetch release artifacts (providers + dev artifact store for in-house).
3. Verify all hashes.
4. Stage inactive immutable release.
5. Record publication success.
   ─── separate authorization ───
6. Activate release; invoke `deploy-pack --release <tag>`.
7. Post-deployment health check.
8. Report result; on failure, enter recovery.
```

Publication success MUST NOT be treated as deploy authorization. Deploy is a separate, explicit step.

### 10.5 Rollback

- Retain previous known-good release.
- Retain pre-upgrade world backup (once backups exist).
- If rollback safety cannot be determined, halt and request operator intervention.
- Restoring the git tag alone is NOT sufficient rollback if world format changed.

---

## 11. Shipping System

### 11.1 Purpose

Turn `sync/downloads/` + a packing slip into the target `mods/` folder.

### 11.2 Algorithm

```
1. Load slip.
2. For each [[jar]] / [[in_house_jar]] with side ∈ {server, both}:
   - verify artifact exists in warehouse with matching sha256
   - add to source set
3. Diff source set vs. target mods/ (via files.deploy_flat_files).
4. Remove stale unprotected; copy new/changed.
5. Log shipment.
```

### 11.3 Same code, different targets

| Host | Target |
|---|---|
| Dev | `~/minecraft-dev/mods/` |
| Prod | `<instance_root>/../mods/` (per deploy config) |

### 11.4 Rules

- Shipping MUST be atomic per file (reuse `files.py`).
- Shipping MUST verify SHA-256 before placement.
- Shipping MUST honor slip-declared protection patterns.
- Shipping MUST NOT read `side_overrides.toml` in slip mode.

---

## 12. `deploy_pack` Integration

### 12.1 Dual-path

`deploy-pack --release <tag>` deploys from a slip.
`deploy-pack` (no `--release`) runs the v2 legacy path.

Both paths MUST coexist until one release cycle completes successfully on prod.

### 12.2 Slip-mode behavior

| Module | Behavior in slip mode |
|---|---|
| `config_model.py` | Loads slip instead of resolving instance paths from compose |
| `deps.py` | NOT called |
| `overrides.py` | NOT called |
| `preflight/changes.py` | Compares slip vs. current state |
| `scope_server.py` | Uses shipping system to place jars; deploys configs/kubejs from working tree |
| `scope_client.py` | Builds from slip's `[client_distribution]` |
| `scope_resource_pack.py` | Uses slip's `[resource_pack]` |
| `prompt_ui.py` | Edits candidate slip review file |

### 12.3 Legacy-mode behavior

MUST remain unchanged in v1 migration period. `.deploy_protect`, `side_overrides.toml`, and `deps.resolve_mod_sources` MUST continue to function.

### 12.4 Working-tree verification

Pre-deploy (slip mode):

```
git status --porcelain sync/config sync/kubejs docker-compose.yml config.d/deploy_pack.toml
```

Non-empty output → abort.

### 12.5 Legacy path retirement

Removed only after:
- One packing-slip release deploys successfully on prod.
- Post-deploy verification passes.
- Rollback procedure has been exercised.

---

## 13. CLI Contract

### 13.1 Upgrade CLI (new)

Entry point: `minecraft-upgrade`.

| Command | Responsibility |
|---|---|
| `manifest check` | Validate manifest syntax/schema/IDs |
| `runtime inspect` | Report effective runtime from Compose |
| `update` | Query providers, resolve, write candidate |
| `build-inhouse <mod> <rev>` | Build in-house JAR |
| `materialize` | Download + verify artifacts |
| `iterate` | Run dev boot loop |
| `build-client` | Produce candidate client ZIP |
| `snapshot-world [survival\|creative]` | rsync prod → dev |
| `validate migration` | Test snapshot against candidate |
| `validate velocity` | Isolated proxy integration |
| `promote <tag>` | Write + stage slip |
| `status` | Summarize gates |
| `verify --tag <tag>` | Read-only slip verification |

### 13.2 `deploy-pack` (existing, extended)

| Command | Responsibility |
|---|---|
| `deploy-pack` | Legacy v2 path |
| `deploy-pack --release <tag>` | Slip-mode deploy |

### 13.3 Safety

- Default operations MUST be non-production and non-destructive.
- Any command starting Docker MUST clearly identify the candidate.
- Candidate commands MUST NOT send Discord notifications or publish to prod webroot.
- No validation command may stop, restart, or exec into a production container.

---

## 14. Exit Codes

### 14.1 `deploy-pack` (unchanged)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime failure |
| 2 | CLI usage error |
| 3 | Config/preflight failure |

### 14.2 `minecraft-upgrade`

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational failure (filesystem, Docker, timeout) |
| 2 | CLI usage error |
| 3 | Config/schema/runtime/lock integrity error |
| 4 | Resolution failure |
| 5 | Validation gate failed |
| 6 | Approval blocked |

### 14.3 Prod daemon

| Code | Meaning |
|---|---|
| 20 | Release verification blocked |
| 21 | Activation failed |
| 22 | Deploy command failed |
| 23 | Post-deploy verification failed |
| 24 | Recovery/rollback failed |

Underlying `deploy-pack` exit code recorded separately.

---

## 15. Configuration

### 15.1 `config.d/upgrade.toml` (new)

```toml
[resolver]
release_policy = "release_then_beta"
allow_alpha = false

[iteration]
max_boot_iterations = 15
max_wall_clock_seconds = 2700
max_api_requests = 5000
max_downloaded_bytes = 10_000_000_000

[world_snapshot]
source_host = "prod.example.com"
source_user = "deploy"
source_path = "/srv/minecraft/survival/world"
ssh_key = "~/.ssh/id_deploy_world_ro"
dest_root = "~/minecraft-dev/snapshots"
exclude = ["session.lock", "*.tmp", "*.lock"]
timeout_seconds = 1800
default_world = "survival"
stop_prod = false

[discord]
bot_token_env = "DISCORD_BOT_TOKEN"
guild_id = "..."
channel_id = "..."
allowlist_roles = ["..."]
allowlist_users = ["..."]

[providers]
curseforge_api_key_env = "CURSEFORGE_API_KEY"
modrinth_user_agent = "minecraft-upgrade/1.0 (contact@example.com)"
```

### 15.2 Secrets

- Provider API keys: CI-host secret store, env injection.
- Discord bot token: same.
- RCON password: existing `secrets/rcon_password`.
- No secrets in manifests, slips, `www/`, or logs.

### 15.3 Naming

| Context | Form |
|---|---|
| CLI flags | kebab-case |
| TOML keys/sections | snake_case |
| `.env` keys | snake_case |

---

## 16. Security & Reliability

1. Provider API secrets MUST live in CI-host secret storage; never in repo, slip, report, or log.
2. Download URLs MUST use HTTPS unless explicitly excepted.
3. Downloaded filenames MUST be sanitized. No absolute paths, traversal, or escapes.
4. Downloads MUST have bounded retries, timeouts, size limits.
5. Candidate writes MUST be atomic (reuse `files.py::_publish_atomically`).
6. Prod daemon MUST run with least privilege, no unrestricted shell.
7. CI-generated Compose MUST be built from validated data; no untrusted interpolation into shell.
8. Cleanup MUST run through `finally`-equivalent control paths.
9. Raw logs MAY contain player names and paths. Retention and access MUST be controlled.
10. Provider-provided scripts MUST NOT be executed as part of resolution.
11. Approved slip, approval record, artifact manifest, and validation evidence MUST be versionable/auditable.
12. No command may silently downgrade runtime, change loader, accept alpha, or ignore a missing dependency.

---

## 17. Migration & Coexistence

### 17.1 Initial migration

The first operation on prod after deploying the upgrade system is:

1. Create `sync/packing_slips/` directory.
2. Generate `v1.0.0.toml` as a **legacy baseline**:
   - No runtime migration claim.
   - Records current artifacts, tree hashes, and current world.
   - Marks `[tested]` absent (no validation gate has run).
3. `current` symlink points to `v1.0.0.toml`.
4. Legacy `deploy-pack` continues to work.

### 17.2 Dual-path period

- Legacy path active: `deploy-pack` (no flag).
- Slip path active: `deploy-pack --release v1.0.1`.
- Both consume the same `sync/downloads/`.

### 17.3 Cutover

After one successful slip-mode deploy on prod:

- Legacy mode removed.
- `side_overrides.toml` deleted.
- `.deploy_protect` deleted.
- `overrides.py` deleted.
- `deps.py` moved to `modpack_upgrade`.

### 17.4 Rollback of the migration itself

If migration fails:

1. Restore original `sync/downloads/`.
2. Revert `deploy_pack.toml` if changed.
3. Remove `sync/packing_slips/`.
4. Legacy path resumes.

---

## 18. Testing Requirements

### 18.1 Unit tests

- Slip canonical hashing — byte-stable, deterministic.
- `.pw.toml` writer — golden files, side sentinel `"unknown"`, exactly one provider update block.
- Resolver — release policy, dependency closure, iterative resolution.
- Log classifier — known fatal, known non-fatal, ambiguous.
- Shipping system — source set derivation, protection patterns.
- Git tree hash verification.

### 18.2 Integration tests

- Slip produced from a fixture, consumed by `deploy-pack --release`.
- Working-tree verification blocks dirty deploys.
- Provider artifacts resolve and hash correctly.
- In-house build produces a jar registered in slip.
- World snapshot flow (mock rsync).
- Discord bot commands route to controller.

### 18.3 Contract tests

- Generated `.pw.toml` parses with `deps.parse_prism_toml`.
- Slip-driven server set matches `deps.resolve_mod_sources` output.
- Slip-driven client set matches current client build output.
- Tree hashes match `git rev-parse HEAD:<subtree>`.

### 18.4 Manual tests

- 1.20.1 → 1.20.1 rebuild from API (proves pipeline).
- 1.20.1 → 1.21.x live upgrade on dev.
- Prod deploy of approved slip.
- Rollback of prod to previous release.

---

## 19. Acceptance Criteria

The system is acceptable for production use only when:

1. Candidate runtime is read from effective Compose, not duplicated.
2. Resolution follows RELEASE → BETA → authorized ALPHA.
3. Dependency closure is iterative and unambiguous.
4. Slip identifies exact provider versions and verified SHA-256.
5. Failed resolution leaves approved slip and artifacts intact.
6. Materialized `.pw.toml` parses with `deps.py`.
7. Side policy is slip-bound; `side_overrides.toml` is not regenerated.
8. Dev runs fresh-world boot tests in isolation.
9. Client artifact is built once and shipped unchanged.
10. Prod world snapshot is tested without modifying source.
11. Velocity integration is independent and reported.
12. Approval is blocked while any required stage fails.
13. Approved slip is bound to runtime and tested inputs.
14. Prod deploy uses `deploy-pack --release`.
15. Automated tests meet project coverage gates.
16. Full behavior is manually verified on dev before first production use.

---

## 20. Known Limits

- Provider compatibility flags are candidate filters, not guarantees.
- JAR metadata is incomplete for some mods.
- Static analysis identifies definite issues, not full compatibility.
- A server that starts may still have gameplay/world defects.
- A world that boots may contain missing/changed modded content.
- Velocity success cannot be inferred from bare server startup.
- Human testing is mandatory for first approved candidate and for world-affecting changes.
- Live world snapshot may capture torn chunks; re-run with `--stop-prod` on failure.
- Multi-world gate collapsed to single world per run; slip records which world was tested.

---

## 21. Appendices

### 21.1 Authority table

| Concern | Authority |
|---|---|
| Manifest intent | `modpack.manifest.toml` |
| Effective runtime | Candidate `docker-compose.yml` |
| Selected artifacts | Candidate slip |
| Approved release | `packing_slips/<tag>.toml` |
| Provider metadata | Modrinth/CurseForge adapters |
| Artifact bytes | Downloaded JARs + provider hashes + local SHA-256 |
| Side policy | Slip `[[jar]].side` (runtime); `side_overrides.toml` (legacy only) |
| Deployment interface | `sync/downloads/` + slip |
| Production lifecycle | `deploy_pack` v2 |
| World migration | Isolated test copy of verified snapshot |
| Velocity topology | Isolated dev proxy |
| Final promotion | Git tag + human approval |

### 21.2 Placeholder reference (Discord)

Unchanged from `Project_Specs.md` §11.3. New placeholders MUST be documented before use.

### 21.3 Exit code reference

See §14.

### 21.4 Glossary

- **Candidate** — artifact set + configs under test, not yet approved.
- **Slip** — packing slip, the release registry.
- **Shipping** — the operation that materializes a slip into `mods/`.
- **Promote** — write slip, commit, tag.
- **Publish** — transfer release artifacts to prod.
- **Deploy** — invoke `deploy-pack --release`.
- **Iteration** — one boot attempt after candidate artifact change.
- **Cap** — hard stop on automated resolution.
- **Ambiguous failure** — classifier cannot confidently fix; pause.
- **Legacy baseline** — `v1.0.0`, produced at migration time, no validation claim.

---

**End of specification.**

---

## What's still required before implementation

| Deliverable | Status |
|---|---|
| Migration runbook | Not written |
| Packing slip schema freeze | Not frozen |
| `.pw.toml` writer contract | Not frozen |
| Log classifier design | Not written |
| Dev Compose project spec | Not written |
| `deploy_pack` refactor plan | Not written |
| Prod daemon API contract | Not frozen |
| In-house build toolchain spec | Not written |

**No implementation is authorized until the above are written and reviewed.**
