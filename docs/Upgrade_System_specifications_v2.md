# docs/Upgrade_System_specifications_v2.md

**Status:** Draft for future implementation. Not to be implemented until the v1.4.0 baseline is in production and stable. v1.4.0 remains the current contract; this document is the design of record for the enterprise release that follows.

**Supersedes (when implemented):** v1.4.0

**Baseline contract:** v1.4.0 is the required starting point. v2.0.0 assumes v1.4.0 is already deployed and functioning in single-DEV mode.

**Date drafted:** 2026-10-20

**Not yet implemented.** No code exists for v2.0.0. No slices are authorized. The document exists so that design decisions made during v1.4.0 development are captured before they are forgotten.

---

## Purpose of this document

v1.4.0 solves the single-DEV problem: one machine, one daemon, one warehouse, one registry, one approval flow. It works. It ships. It is the correct design for a pack authoring workflow owned by one operator on one server.

v2.0.0 solves the *distributed* problem: multiple DEV runners, shared state, an artifact server as store of record, first-class compat matrix, graph analysis, and a Discord bot that is a peer service rather than a bolted-on feature. It is the design of record for teams that operate more than one DEV server, or that want the operational guarantees a single-DEV architecture cannot provide (runner failover, artifact durability independent of one machine, cross-runner budget accounting).

The two documents are complementary. v1.4.0 does not contain v2.0.0 features by accident; v2.0.0 does not rewrite v1.4.0 features that already work. The boundary is:

| Concern | v1.4.0 | v2.0.0 |
|---|---|---|
| DEV runner count | 1 | N (2+, unbounded) |
| Runner state | Local, stateful | Stateless (core holds all state) |
| Coordination database | None (local files) | MySQL/MariaDB via DBCore |
| Warehouse | Local `sync/downloads/` | Artifact server (local cache optional) |
| Bundle store | Local `www/releases/` | Artifact server |
| Registry | JSONL append | MySQL |
| API cap enforcement | Phantom (§17.1 `ledger_path` unused) | MySQL-backed, real |
| Compat matrix | None | First-class, learned from boot history |
| Graph analysis | None | `minecraft-upgrade graph diff` CLI |
| Discord bot | Phantom (slice 15, not built) | Real, peer service, MySQL-backed |
| Daemon lock | Local file | Not needed (core serializes) |
| Migration path | — | One-time state migration |

---

## 1. Purpose

`deploy_pack` v2 and the `minecraft-upgrade` v1.4.0 toolchain apply materialized artifact sets to Minecraft servers. v2.0.0 extends the toolchain to operate across multiple DEV runners, coordinated by a central core, with durable shared state and an artifact server as the store of record.

v2.0.0 provides:

1. **Multi-runner resolution.** N DEV runners can execute `update`, `materialize`, `iterate`, and `promote` concurrently. The core dispatches work, runners execute it, artifacts flow through the artifact server.
2. **Durable shared state.** Registry, budget, compat matrix, approval queue, and per-run state are stored in MySQL. Any runner can fail; another runner picks up the work.
3. **Store of record.** Artifact bytes live in a dedicated artifact server. Warehouse on each runner is a disposable local cache. Production pulls from the artifact server, not from a specific DEV host.
4. **Learned compatibility.** Boot attempts are recorded; the compat matrix is derived. When Create 7 is rolled back to 6, the tool knows which mods were tested against which versions and reports what's affected — without a boot loop.
5. **Real budget enforcement.** API request and byte budgets are enforced across runs, across runners, and across restarts.

v2.0.0 does not change the packing-slip contract, the release bundle contract, the canonical hashing pipeline, or the side-determination precedence chain. Those are v1.4.0 features that carry forward unchanged.

---

## 2. Normative Language

- **MUST / MUST NOT** — mandatory for v2.0.0 conformance.
- **SHOULD / SHOULD NOT** — default requirement; deviations need documented reason.
- **MAY** — optional.

---

## 3. Topology

### 3.1 Roles

| Role | Description |
|---|---|
| **Client** | Authoring machine. Pushes code, configs, kubejs, in-house mod source to git. No changes from v1.4.0. |
| **Core** | Central control plane on DEV. Hosts MySQL. Owns the work dispatcher. Accepts HTTP API requests from clients and runners. Authoritative source of coordination state. |
| **Runner** | Stateless worker. Polls the core for work, executes it locally, uploads artifacts to the artifact server, reports status back. Can be added or removed freely. |
| **Artifact server** | Store of record for all artifact bytes. Accepts uploads from runners, serves downloads to runners and prod. Independent of the core and of any runner. |
| **Discord bot** | Peer service. Reads and writes to the core's MySQL via the core's HTTP API. Not a runner, not the core, not a daemon of record. |
| **Prod server** | Pulls release bundles from the artifact server. Deploys via `deploy_pack`. Unchanged from v1.4.0 in deploy lifecycle; only the pull source changes (artifact server instead of a specific DEV). |

### 3.2 Data flow

```
                              ┌──────────────────┐
                              │     Client       │
                              └────────┬─────────┘
                                       │ git push
                                       ▼
┌────────────────────────────────────────────────────────────┐
│  Core (DEV)                                                │
│  ┌──────────────────┐  ┌────────────────┐                  │
│  │ MySQL/MariaDB    │  │ Work dispatcher│                  │
│  │ (via DBCore)     │  │ (queue+leases) │                  │
│  └──────────────────┘  └───────┬────────┘                  │
│                                │                           │
│                                ▼                           │
│                        ┌──────────────┐                    │
│                        │ HTTP API     │                    │
│                        └──────────────┘                    │
└────────────────────────────────────────────────────────────┘
                              │                    ▲
                work claims   │                    │ status updates
                              ▼                    │
        ┌──────────────────────────────────────────────┐
        │  Runners (N, stateless)                      │
        │  ┌───────────┐  ┌───────────┐  ┌──────────┐  │
        │  │ Runner A  │  │ Runner B  │  │ Runner C │  │
        │  └─────┬─────┘  └─────┬─────┘  └────┬─────┘  │
        └────────┼──────────────┼─────────────┼────────┘
                 │ upload       │ upload      │ upload
                 ▼              ▼             ▼
        ┌──────────────────────────────────────────────┐
        │  Artifact server (store of record)           │
        │  - jars, pw.toml, slips, bundles, configs   │
        └──────────────────────────────────────────────┘
                              │
                              │ pull (sha-verified)
                              ▼
                        ┌──────────────┐
                        │ Prod server  │
                        └──────────────┘

        ┌──────────────────┐
        │  Discord bot     │──── HTTP ────► Core
        │  (peer service)  │
        └──────────────────┘
```

### 3.3 Constraints

- **Core is required.** No coordination without it. A v2.0.0 deployment MUST have exactly one running core.
- **MySQL is required.** The core cannot start without a reachable MySQL/MariaDB instance.
- **Artifact server is required.** Runners upload to it; prod pulls from it. A local-only deployment is v1.4.0, not v2.0.0.
- **Runners are optional per task.** The core can run with zero runners; the system is idle but healthy. Runners can be added or removed freely.
- **Runners MUST have no shared filesystem with each other or with the core.** Each runner has only local transient storage. Everything durable flows through the artifact server or MySQL.
- **Runners MUST be stateless.** A runner can be destroyed at any time. No state survives runner failure.
- **Prod MUST pull artifacts from the artifact server, not from a specific DEV host or runner.**
- **Prod MUST NOT contact Modrinth, CurseForge, any in-house mod repository, or any other mod provider during any release cycle.** Unchanged from v1.4.0.
- **SMB/NFS remain unsupported.** Cross-runner coordination is via MySQL and HTTP. Filesystem sharing is not a coordination mechanism.

---

## 4. Storage Model

### 4.1 Repository layout (per-runner)

The runner-side repository layout matches v1.4.0. No changes:

```
repo/
├── config.d/
├── docs/
├── src/
├── sync/
│   ├── config/
│   ├── downloads/        # LOCAL CACHE ONLY; source of truth is artifact server
│   │   └── .index/
│   ├── kubejs/
│   ├── packing_slips/    # LOCAL CHECKOUT of git-tracked slips
│   └── velocity-plugins/
├── .build-workspace/     # transient; per-runner
├── www/                  # LOCAL CACHE; bundles live in artifact server
├── tests/
├── docker-compose.yml
└── pyproject.toml
```

Key differences from v1.4.0:
- `sync/downloads/` is a **local cache**, not a store of record. It may be empty on a fresh runner.
- `www/releases/` is a **local cache**. Bundles are pulled from the artifact server on demand.
- `sync/packing_slips/` remains a **git checkout** — slips are still git-tracked, still subject to the human-committed push flow. Only the registry (see below) moves to MySQL.

### 4.2 Core-side state (MySQL)

All v1.4.0 local state moves to MySQL:

| v1.4.0 location | v2.0.0 location |
|---|---|
| `/var/lib/minecraft-upgrade/ledger.sqlite` (phantom) | MySQL `budget` table |
| `/var/lib/minecraft-upgrade/runs/<run-id>/state.toml` | MySQL `runs` table |
| `/var/lib/minecraft-upgrade/latest-slip` | MySQL `pointers` table |
| `sync/packing_slips/release-index.jsonl` | MySQL `registry_entries` table |
| (none) | MySQL `work_queue`, `work_leases`, `approvals`, `compat_matrix`, `boot_attempts`, `pack_snapshots`, `snapshot_versions` |

### 4.3 Artifact server layout

The artifact server stores bytes addressed by their SHA-256. Two layouts are acceptable:

**Layout A — hash-addressed (recommended):**
```
artifacts/<sha256[:2]>/<sha256[2:4]>/<sha256>.bin
```
The same bytes appear once regardless of how many releases, files, or slots reference them. Deduplicates automatically. Requires a metadata layer (MySQL) to map `(release_id, kind, filename) -> sha256`.

**Layout B — path-addressed (simpler):**
```
releases/<release_id>/...
downloads/<filename>
in-house/<name>/<build_id>/<filename>
```
Matches v1.4.0 paths. Simpler to browse, no metadata layer required to reconstruct a path, but duplicates bytes if the same file appears in multiple releases.

Either layout works. Layout A is recommended for storage efficiency at scale. Layout B is acceptable for smaller deployments.

### 4.4 Invariants

- **The SHA-256 in the slip is the identity of the artifact.** Any byte sequence with that hash is valid.
- **The artifact server is the store of record.** If a file is in the artifact server and hashes correctly, it is authoritative regardless of what local caches exist or don't exist.
- **Local caches are disposable.** A runner may have a completely empty `sync/downloads/` at any moment. It will populate on demand from the artifact server.
- **The MySQL database is the store of record for coordination state.** Registry, budget, approvals, compat matrix, work queue.
- **The git repository is the store of record for configuration.** Manifests, slips, configs, kubejs, deploy_pack.toml — all still git-tracked. v2.0.0 does not move config to MySQL.
- **`stable` pointer.** Moved to MySQL `pointers` table. Git-tracked `sync/packing_slips/stable` remains as a *cache* of the pointer for CI readability, but the authoritative value is MySQL.

---

## 5. Core Daemon

### 5.1 Responsibilities

The core daemon owns:

- MySQL connection pool
- HTTP API for runners and clients
- Work dispatcher (queue + leasing)
- Registry append serialization
- Budget enforcement
- Compat matrix derivation
- Approval queue management
- Pointer management

The core daemon does **not**:

- Execute resolution, materialization, or shipping
- Hold artifact bytes
- Run Minecraft containers
- Talk to mod providers
- Clone in-house mod repositories

All of those are runner responsibilities.

### 5.2 Exposure

- HTTP API on a Tailscale-only interface (configurable bind address).
- Authentication: bearer token + Tailscale source-IP (same pattern as v1.4.0).
- All operations idempotent where applicable.
- Single instance. No horizontal scaling of the core itself in v2.0.0.
- HA (multiple cores) is out of scope for v2.0.0 and reserved for v2.1.0.

### 5.3 HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/status` | Core health, runner count, active work |
| `POST` | `/work/claim` | Runner requests work; core leases a task |
| `POST` | `/work/complete` | Runner reports completion |
| `POST` | `/work/heartbeat` | Runner refreshes lease |
| `POST` | `/runs` | Create a run (used by `update`) |
| `GET` | `/runs/<id>` | Get run state |
| `PATCH` | `/runs/<id>` | Update run state |
| `POST` | `/registry/append` | Append a registry entry |
| `GET` | `/registry/head` | Current chain head |
| `POST` | `/budget/consume` | Record API or byte consumption |
| `GET` | `/budget/remaining` | Current remaining budget |
| `POST` | `/approvals` | Create approval request |
| `GET` | `/approvals/<id>` | Get approval state |
| `POST` | `/approvals/<id>/sign` | Sign approval (from Discord bot) |
| `GET` | `/compat/query` | Query compat matrix |

Exact request/response schemas are defined in §8.

### 5.4 Work dispatcher

- Work is enqueued as rows in `work_queue`.
- Runners claim work via `POST /work/claim`. The core uses `SELECT ... FOR UPDATE SKIP LOCKED` to atomically lease a queue item to the requesting runner.
- Leases have a TTL (`work_leases.expires_at_utc`). A runner MUST heartbeat every `lease_heartbeat_seconds` (default 30) to keep its lease alive.
- If a lease expires (runner died, network dropped), the core returns the work to `work_queue` for another runner to claim.
- Retries: `work_queue.max_attempts` (default 3). After exhaustion, the work moves to `work_queue.state = "failed"`.
- Work kinds: `resolve`, `materialize`, `iterate`, `promote`, `publish`, `build_inhouse`, `pull_bundle`.

### 5.5 Registry append

Serialization is via MySQL row-level locking:

```sql
BEGIN;
SELECT entry_hash FROM registry_entries
WHERE release_id IS NOT NULL
ORDER BY id DESC LIMIT 1
FOR UPDATE;
-- INSERT new entry with previous_entry_hash from the row above
COMMIT;
```

Two runners appending simultaneously serialize at the MySQL level. No interleaved JSONL, no chain corruption, no shared file lock needed.

### 5.6 Budget enforcement

Budget is `budget` table rows keyed by `(window_start, window_end, kind)`. The core records consumption via `POST /budget/consume` and answers remaining-budget queries via `GET /budget/remaining`.

Runners MUST call `GET /budget/remaining` before each provider call or download. If remaining is insufficient, the runner aborts with a specific error. If remaining is sufficient, the runner proceeds, calls `POST /budget/consume` on completion, and continues.

The window is configurable (`[budget].window = "24h"` default). Daily budget resets automatically.

### 5.7 Compat matrix derivation

The core derives compatibility knowledge from `boot_attempts` and `pack_snapshots`:

- Every `iterate` invocation writes a `boot_attempts` row per attempt.
- A row records: `run_id`, `attempt_number`, `started_at_utc`, `ended_at_utc`, `result`, `classification`, `crash_signature`.
- The core runs a periodic job (`[core].compat_derive_interval_hours`, default 24) that populates `learned_compat` from `boot_attempts` joined against `snapshot_versions`.

The derivation algorithm:

```
For each boot_attempt:
    if result == "success":
        For each pair (mod A, mod B) where A and B are both in the snapshot:
            learned_compat.works(A, B) += 1
    elif result == "failure":
        For each mod in crash_signature.failing_mods_hint:
            For each other mod in the snapshot:
                learned_compat.fails(mod, other) += 1
```

Boot outcomes are not conclusive on their own. A "success" for a particular pack composition is *evidence* that the composition works, not proof. The compat matrix accumulates evidence; resolvers and the graph-diff CLI present the evidence with confidence.

### 5.8 Pointer management

`stable`, `latest-slip`, and any future pointers are stored in the `pointers` table. Reads are from MySQL, not from files. The git-tracked `stable` file becomes a cache that is regenerated by `publish` and read by CI; the MySQL value is authoritative.

---

## 6. Runners

### 6.1 Responsibilities

A runner:

- Polls the core for work.
- Executes the work locally (CLI commands, Docker operations, git clones).
- Pulls required artifacts from the artifact server into its local cache.
- Uploads produced artifacts to the artifact server.
- Reports status and completion back to the core.

### 6.2 Statefulness

**Runners are stateless.** A runner holds no state beyond:

- Its working git checkout
- Its local caches (`sync/downloads/`, `www/releases/`)
- Its transient workspace (`.build-workspace/`)

All state that matters is in MySQL or the artifact server. If a runner is destroyed:

- The core re-leases its in-flight work after the lease expires.
- The new runner pulls fresh artifacts from the artifact server.
- The new runner resumes the work from the recorded state in MySQL.

### 6.3 Registration

A runner registers with the core by calling `POST /work/claim` with its identifier. The core records the runner in `runners` table on first contact.

Runner identifiers are stable (hostname + UUID persisted in `<runner_state_dir>/runner-id`). Restarting a runner with the same identifier resumes its work.

### 6.4 Local cache

`sync/downloads/` is a hash-verified cache. When a runner needs a jar for materialize or shipping, it:

1. Checks `sync/downloads/<filename>`. If present and hashing to the expected SHA-256, uses it.
2. Otherwise pulls from the artifact server.
3. Verifies the SHA-256 after pull.

If the local cache is empty, the runner still works — just slower on first invocation.

### 6.5 Upload to artifact server

When a runner produces an artifact (in-house build, downloaded mod jar, published bundle), it uploads to the artifact server via HTTP PUT with SHA-256 verification:

```
PUT /artifacts/<sha256>
Content-Length: ...
X-Verify-SHA256: <sha256>

<bytes>
```

The artifact server verifies the hash on receive. If the bytes don't match the declared hash, the upload is rejected.

Uploads are idempotent: uploading the same bytes twice is a no-op.

---

## 7. Work Dispatcher

### 7.1 Work kinds

| Kind | Input | Runner action |
|---|---|---|
| `resolve` | `manifest_path`, `release_mode` | Run `minecraft-upgrade update` |
| `materialize` | `run_id` | Run `minecraft-upgrade materialize` |
| `iterate` | `run_id` | Run `minecraft-upgrade iterate` |
| `promote` | `tag` | Run `minecraft-upgrade promote` |
| `publish` | `release_id` | Run `minecraft-upgrade publish` |
| `build_inhouse` | `name`, `overrides` | Run `minecraft-upgrade build-inhouse` |
| `pull_bundle` | `release_id` | Pull bundle from artifact server, verify, cache locally |

### 7.2 Work lifecycle

```
queued → claimed → executing → completed
                    │
                    └→ failed (after max_attempts)
                    │
                    └→ released (lease expired, back to queued)
```

### 7.3 Concurrency

Multiple runners can claim work concurrently. The `SELECT ... FOR UPDATE SKIP LOCKED` pattern ensures no two runners claim the same work item.

Per-run serialization: work items for the same `run_id` MUST be executed in order (`resolve` → `materialize` → `iterate`). The dispatcher enforces this by refusing to claim `materialize` for a `run_id` whose `resolve` is not `completed`.

### 7.4 Priority

Work has a priority field (`work_queue.priority`, default 0). Higher priority is claimed first. Operators may set priority explicitly; the default is FIFO within the same priority.

---

## 8. MySQL Schema

All tables created via DBCore `SchemaBlueprint`. SQLite-compatible for local development, MariaDB for production.

### 8.1 Tables

```sql
-- Coordination state
CREATE TABLE runs (
    run_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    release_mode    TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    report_sha256   TEXT,
    resolution_inputs_sha256 TEXT,
    effective_compose_sha256 TEXT,
    paused_at_utc   TEXT NOT NULL,
    reason          TEXT,
    created_at_utc  TEXT NOT NULL,
    updated_at_utc  TEXT NOT NULL
);

CREATE TABLE pointers (
    name            TEXT PRIMARY KEY,           -- "stable" | "latest-slip"
    target          TEXT NOT NULL,              -- release_id or candidate_id
    updated_at_utc  TEXT NOT NULL
);

CREATE TABLE registry_entries (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id              TEXT NOT NULL UNIQUE,
    tag_object_sha          TEXT NOT NULL,
    tag_object_format       TEXT NOT NULL,
    commit_sha              TEXT NOT NULL,
    slip_content_sha256     TEXT NOT NULL,
    bundle_manifest_sha256  TEXT NOT NULL,
    previous_entry_hash     TEXT NOT NULL,
    entry_hash              TEXT NOT NULL,
    signature               TEXT NOT NULL,
    key_id                  TEXT NOT NULL,
    appended_at_utc         TEXT NOT NULL
);

CREATE TABLE budget (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start_utc TEXT NOT NULL,
    window_end_utc  TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- "api_request" | "downloaded_bytes"
    consumed        INTEGER NOT NULL DEFAULT 0,
    updated_at_utc  TEXT NOT NULL,
    UNIQUE (window_start_utc, kind)
);

CREATE TABLE work_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    run_id          TEXT,
    params_json     TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    enqueued_at_utc TEXT NOT NULL,
    updated_at_utc  TEXT NOT NULL
);

CREATE TABLE work_leases (
    work_id         INTEGER PRIMARY KEY,
    runner_id       TEXT NOT NULL,
    claimed_at_utc  TEXT NOT NULL,
    expires_at_utc  TEXT NOT NULL,
    FOREIGN KEY (work_id) REFERENCES work_queue(id)
);

CREATE TABLE runners (
    runner_id       TEXT PRIMARY KEY,
    first_seen_utc  TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    hostname        TEXT
);

CREATE TABLE approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- "loss_ack" | "mod_set" | "world_migration" | "velocity_integration"
    binding_json    TEXT NOT NULL,              -- full binding data per §11.4
    state           TEXT NOT NULL DEFAULT 'pending',
    created_at_utc  TEXT NOT NULL,
    signed_at_utc   TEXT,
    signed_by       TEXT,
    expires_at_utc  TEXT NOT NULL
);

-- Boot and compatibility
CREATE TABLE pack_snapshots (
    run_id          TEXT PRIMARY KEY,
    created_at_utc  TEXT NOT NULL,
    slip_sha256     TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE snapshot_versions (
    run_id          TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    PRIMARY KEY (run_id, project_id),
    FOREIGN KEY (run_id) REFERENCES pack_snapshots(run_id)
);

CREATE TABLE declared_edges (
    from_project_id     TEXT NOT NULL,
    to_project_id       TEXT NOT NULL,
    version_constraint  TEXT,
    dependency_type     TEXT NOT NULL,
    source              TEXT NOT NULL,          -- pw_toml | jar_manifest | provider_metadata
    PRIMARY KEY (from_project_id, to_project_id, dependency_type, source)
);

CREATE TABLE boot_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL,
    started_at_utc  TEXT NOT NULL,
    ended_at_utc    TEXT NOT NULL,
    result          TEXT NOT NULL,              -- success | failure | ambiguous
    classification  TEXT,                       -- startup_ok | crash_report | timeout | etc.
    crash_signature TEXT,
    FOREIGN KEY (run_id) REFERENCES pack_snapshots(run_id)
);

CREATE TABLE learned_compat (
    mod_project_id      TEXT NOT NULL,
    mod_version_id      TEXT NOT NULL,
    dep_project_id      TEXT NOT NULL,
    dep_version_id      TEXT NOT NULL,
    result              TEXT NOT NULL,          -- works | fails | unknown
    evidence_count      INTEGER NOT NULL DEFAULT 0,
    first_seen_run_id   TEXT NOT NULL,
    last_verified_run_id TEXT NOT NULL,
    updated_at_utc      TEXT NOT NULL,
    PRIMARY KEY (mod_project_id, mod_version_id, dep_project_id, dep_version_id)
);
```

### 8.2 Migration story

v1.4.0 → v2.0.0 migration is one-time and mechanical:

1. `runs/<run-id>/state.toml` files → rows in `runs`
2. `sync/packing_slips/release-index.jsonl` → rows in `registry_entries` (chain continuity preserved)
3. `latest-slip` file → row in `pointers`
4. No `budget` rows (v1.4.0 had no budget)
5. No `boot_attempts` rows (v1.4.0 didn't record them)
6. No `pack_snapshots` / `snapshot_versions` / `learned_compat` (v1.4.0 didn't record them)

The migration tool (`minecraft-upgrade migrate-to-v2`) is a separate CLI command that runs once. It is idempotent: running it twice produces the same result.

After migration, the v1.4.0 daemon and all local state files are decommissioned.

---

## 9. Artifact Server

### 9.1 Contract

The artifact server MUST expose:

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/artifacts/<sha256>` | Upload bytes; server verifies SHA-256 on receive |
| `GET` | `/artifacts/<sha256>` | Download bytes; client verifies SHA-256 on receive |
| `HEAD` | `/artifacts/<sha256>` | Check existence without download |
| `POST` | `/bundles/<release_id>` | Assemble a bundle from already-uploaded artifacts; server verifies against the bundle manifest |
| `GET` | `/bundles/<release_id>/...` | Download a file within a bundle |

Authentication: bearer token or mutual TLS, configurable.

### 9.2 Implementations

v2.0.0 does not mandate a specific artifact server implementation. Any of the following satisfies the contract:

- **Nexus Repository** — supports raw artifact repository with hash verification.
- **Gitea / Forgejo packages** — general-purpose artifact hosting.
- **Custom HTTP server** — ~200 lines of Python; verify on both directions; recommended for full contract control.

The `bundle` endpoint is not standard in any of the above. For Nexus/Gitea, bundle assembly can be done client-side by the runner (upload each artifact, then the core assembles the manifest); for a custom server, the bundle endpoint is native.

### 9.3 Retention

The artifact server retains all bytes referenced by the registry. Retention policies (e.g., delete bundles older than 1 year) are the operator's responsibility but MUST NOT delete anything referenced by a live registry entry.

---

## 10. Discord Bot

### 10.1 Role

The Discord bot is a **peer service**, not a daemon. It:

- Listens for Discord commands (`/upgrade status`, `/upgrade approve`, etc.).
- Translates them into HTTP API calls against the core.
- Returns the core's response as the Discord reply.
- Holds no business logic beyond translation and Discord interaction signature verification.

### 10.2 State

The Discord bot holds no state. All state (approvals, run status, compat queries) lives in the core's MySQL. The bot is fully restartable.

### 10.3 Approval replay protection

Every Discord interaction has a unique interaction ID. The bot records the interaction ID in `approvals` (or a new `discord_interactions` table) with a unique constraint. A replayed interaction returns the previously recorded response. No double-approval.

### 10.4 Approval expiry

Approvals have an `expires_at_utc` (default 7 days). Expired approvals are rejected. The core's periodic job prunes expired rows.

### 10.5 Binding

An approval is bound to a full identity hash:

```
binding = sha256(canonical_json({
    "run_id": ...,
    "slip_content_sha256": ...,
    "runtime_sha256": ...,
    "resolution_report_sha256": ...,
    "snapshot_id": ...,
    "validation_report_sha256": ...,
}))
```

Any change to the bound data invalidates the approval. `promote` and `publish` verify the binding hash against the current values before proceeding.

---

## 11. Compat Matrix and Boot History

### 11.1 Boot history recording

Every `iterate` invocation writes one row per attempt to `boot_attempts`. The row records the crash signature if the attempt failed.

The crash signature includes:

- `Exception in thread` trace first-line (or nil)
- Forge/Fabric crash report marker if present
- Log tail excerpt (last 500 characters of the attempt's stderr)
- `failing_mods_hint`: extracted from `Caused by: ... <mod id>` patterns when present

Crash signature extraction is best-effort. `failing_mods_hint` is a hint, not proof.

### 11.2 Learned compat derivation

The core's periodic job derives `learned_compat` rows from `boot_attempts` joined against `snapshot_versions`. See §5.7.

### 11.3 Query API

`GET /compat/query` accepts:

```json
{
    "project_id": "create-nuclear",
    "dep_project_id": "create",
    "dep_version_id": "6.0.8-289"
}
```

Returns:

```json
{
    "evidence": [
        {"result": "works", "count": 12, "latest_run": "abc"},
        {"result": "fails", "count": 0}
    ],
    "confidence": "high",
    "recommendation": "safe to upgrade"
}
```

Confidence is derived from evidence count: `count >= 5` with no contradicting evidence is "high"; `count == 0` is "unknown"; contradicting evidence is "conflicting."

---

## 12. Graph Analysis CLI

### 12.1 `minecraft-upgrade graph diff`

Compares two slips and reports dependency-graph changes.

```
minecraft-upgrade graph diff --from v1.0.0 --to v1.0.1
```

Reports:

- Mods added, removed, upgraded, downgraded
- Direct dependents of each change
- Transitive dependents (via `declared_edges`)
- Boot history overrides (from `learned_compat`): mods known to fail with this change

### 12.2 `minecraft-upgrade graph query`

```
minecraft-upgrade graph query --project create --version 6.0.8-289
```

Reports:

- Mods that declare a dependency on `create` (direct)
- Transitive dependents
- For each: current version in `stable`, learned compat with `create@6.0.8-289`, known boot failures

### 12.3 No boot loop required

Graph analysis is a query against existing data. No resolution, no download, no boot attempt. Sub-second response time on packs of 200+ mods.

---

## 13. API Cap Enforcement

### 13.1 Enforced at the core

Budget consumption is recorded in `budget` and enforced at the core. Runners query `GET /budget/remaining` before each provider call or download.

### 13.2 Windows

Configurable window: `[budget].window = "24h"` (default) or `"1h"`, `"7d"`, etc.

### 13.3 Caps

```toml
[budget]
window = "24h"
max_api_requests = 5000
max_downloaded_bytes = 10000000000
```

When remaining budget is insufficient, the runner fails with exit code 6 (approval blocked). The operator can raise the cap or wait for the window to roll over.

### 13.4 Audit

Every consumption is recorded. The `budget` table can be queried for spend history, per-provider breakdown (future), and trend analysis.

---

## 14. Migration from v1.4.0

### 14.1 Prerequisites

Before migrating:

- v1.4.0 is deployed and functioning in single-DEV mode.
- The current `stable` release is documented.
- MySQL instance is provisioned and reachable.
- Artifact server is provisioned and reachable.
- The v1.4.0 git repository (with `sync/packing_slips/*.toml`) is on the same machine as the migration.

### 14.2 Migration steps

1. Run `minecraft-upgrade migrate-to-v2 --mysql-url <url>`. This:
   - Creates the v2.0.0 schema.
   - Imports every `state.toml` into `runs`.
   - Imports `release-index.jsonl` into `registry_entries` (chain preserved).
   - Imports `latest-slip` into `pointers`.
   - Leaves the v1.4.0 files in place (idempotent migration).

2. Verify the chain by running `minecraft-upgrade publish --verify-only --all-releases` against MySQL. If the chain matches the JSONL chain, proceed.

3. Upload existing artifacts to the artifact server (via `minecraft-upgrade export-artifacts` or direct upload).

4. Decommission the v1.4.0 daemon. Enable the v2.0.0 core.

5. Update prod config to pull from the artifact server, not from DEV.

6. Run one full release cycle on the new architecture. Verify:
   - Resolution works (via runner)
   - Materialization works
   - Registry appends work
   - Bundle publication works
   - Prod pull works

7. Decommission v1.4.0 entirely.

### 14.3 Rollback

Migration is reversible. v1.4.0 files remain until step 7. Rollback steps:

1. Stop the v2.0.0 core.
2. Re-enable the v1.4.0 daemon.
3. Point prod back at DEV.
4. The v1.4.0 state files are untouched and functional.

Migration is a non-destructive copy from v1 files → MySQL. Until step 7, both systems can run in parallel (with the caveat that only one is authoritative at a time).

---

## 15. CLI Contract (deltas from v1.4.0)

Most commands are unchanged. The deltas:

| Command | Change |
|---|---|
| `minecraft-upgrade update` | Now `POST /work` to core; may run on any runner |
| `minecraft-upgrade materialize` | Unchanged semantics; runner pulls artifacts from artifact server |
| `minecraft-upgrade iterate` | Now writes `boot_attempts` rows to core |
| `minecraft-upgrade promote` | Unchanged semantics; slip is staged on runner and pushed to artifact server |
| `minecraft-upgrade publish` | Registry append goes to core; bundle goes to artifact server |
| `minecraft-upgrade build-inhouse` | Unchanged semantics; artifact uploaded to artifact server |
| `minecraft-upgrade graph diff` | New command |
| `minecraft-upgrade graph query` | New command |
| `minecraft-upgrade migrate-to-v2` | New command |
| `minecraft-upgrade export-artifacts` | New command |

Runner-side commands read their core configuration from `[core]` in `upgrade.toml`:

```toml
[core]
url = "https://core.dev.example.com:8443"
bearer_token_env = "CORE_BEARER_TOKEN"
runner_id_path = "/var/lib/minecraft-runner/runner-id"
```

---

## 16. Exit Codes

Unchanged from v1.4.0. Runner-side exit codes remain the same; core-side exit codes are HTTP status codes (see §5.3 API table for details).

---

## 17. Configuration

### 17.1 `config.d/upgrade.toml` additions

```toml
[core]
url = "https://core.dev.example.com:8443"
bearer_token_env = "CORE_BEARER_TOKEN"
lease_heartbeat_seconds = 30

[artifact_server]
url = "https://artifacts.dev.example.com"
bearer_token_env = "ARTIFACT_SERVER_TOKEN"

[budget]
window = "24h"
max_api_requests = 5000
max_downloaded_bytes = 10000000000
```

### 17.2 `config.d/core.toml` (new, core-side only)

```toml
[database]
provider_type = "mariadb"
db_host = "mysql.internal"
db_port = 3306
db_user = "minecraft"
db_password_env = "MYSQL_PASSWORD"
db_database = "minecraft_core"

[http]
bind = "0.0.0.0:8443"
bearer_token_env = "CORE_BEARER_TOKEN"
tailscale_cidrs = "100.64.0.0/10"

[compat]
derive_interval_hours = 24
```

### 17.3 Migration from v1.4.0

v1.4.0's `[state].ledger_path` is removed. Budget is now enforced via the `budget` table. v1.4.0's phantom ledger is replaced by a real one.

---

## 18. Security

1. The core requires authentication for every request. Bearer token + Tailscale source-IP.
2. Runners authenticate to the core with the same mechanism. Runner IDs are pre-registered (an operator adds a runner explicitly).
3. The artifact server authenticates both directions (upload and download).
4. MySQL credentials are never in git. They are env-var or secret-store managed.
5. All inter-service communication is over TLS.
6. Prod pulls from the artifact server, never from the core or a runner.
7. The Discord bot authenticates to the core with a scoped bearer token (approval-only permissions).
8. Approval replay protection is required (§10.3).
9. Approval binding is verified before `promote` and `publish`.
10. Runner IDs are recorded in the audit trail for every work execution.

---

## 19. Acceptance Tests

New tests for v2.0.0:

| ID | Test | Pass condition |
|---|---|---|
| T2-01 | Multi-runner resolution | Two runners resolve the same manifest; both produce identical `resolution_inputs_sha256`. |
| T2-02 | Runner failover | Kill runner A mid-resolve; the core re-leases the work to runner B; the run completes. |
| T2-03 | Registry append under contention | Two runners append simultaneously; chain integrity preserved, one wins, one retries. |
| T2-04 | Budget enforcement across runners | Two runners each request provider calls; total consumption is enforced, not per-runner. |
| T2-05 | Budget survives restart | Stop the core, restart, budget state preserved. |
| T2-06 | Artifact server is store of record | Delete a runner's local cache; it pulls from artifact server and continues. |
| T2-07 | Artifact server hash verification | Upload bytes with a mismatched SHA-256; rejected. |
| T2-08 | Bundle assembly from artifact server | Two runners materialize the same slip; bundle assembled from artifacts already in the server (no duplicate uploads). |
| T2-09 | Compat matrix derivation | Twelve successful boots and one failure with a specific mod version combination produce a confident `works` classification. |
| T2-10 | Graph diff query | Roll Create 7 → 6; `graph diff` reports affected mods and their compat evidence in under a second. |
| T2-11 | Discord bot is a peer | Kill the Discord bot; core continues. Restart the bot; approvals resume. |
| T2-12 | Approval replay refused | Replay the same interaction ID; second attempt returns the original response, does not double-sign. |
| T2-13 | Approval expiry | Sign an approval, wait past expiry, `promote` refuses with the appropriate error. |
| T2-14 | Migration from v1.4.0 | Run the migration tool against a v1.4.0 state; all rows imported correctly; chain verifies. |
| T2-15 | Migration rollback | After migration, revert to v1.4.0; all state restored. |
| T2-16 | Core without runners | Start the core with zero runners; it reports "idle" and accepts work queuing. Add a runner; work executes. |
| T2-17 | Runner without core | Start a runner; it retries the core connection; when the core comes up, work proceeds. |
| T2-18 | Two DEV runners share approved release | Both runners can promote the same release; one stages, one verifies, both agree on chain. |

---

## 20. Acceptance Criteria

v2.0.0 is acceptable for production use only when:

1. All v1.4.0 acceptance criteria remain met (34 of 35 + T11 from v1.4.0 slice 15).
2. All T2-* tests pass.
3. Migration from v1.4.0 has been exercised on a production-like clone.
4. Rollback to v1.4.0 has been exercised on a production-like clone.
5. Two runners have operated in parallel for at least one full release cycle without coordination errors.
6. Budget enforcement has prevented an over-quota consumption attempt.
7. Compat matrix has answered a real "what changed?" question from a real boot history.
8. Artifact server has survived a runner cache wipe.
9. The Discord bot has been killed and restarted without affecting core operation.
10. Documentation is complete: v2.0.0 spec, artifact server contract, MySQL schema, migration runbook, rollback runbook.

---

## 21. Known Limits and Open Questions

### 21.1 Known limits

- The core is a single point of failure. v2.0.0 does not support HA. v2.1.0 may add core clustering.
- The artifact server is a single point of failure. Durability is the operator's responsibility (backups, replication).
- MySQL is a single point of failure. MariaDB Galera or similar is out of scope for v2.0.0.
- Boot-history-based compat derivation is best-effort. It does not prove compatibility, it accumulates evidence.
- The graph diff CLI assumes slips are available. Historical slips must be retained (they are, in git).

### 21.2 Open questions

- **Which artifact server?** Nexus, Gitea, or custom. The design is agnostic; the choice is operational. Custom is recommended for the bundle endpoint.
- **Runner authentication scheme?** Bearer token shared across runners, or per-runner certificates? Per-runner certs scale better but require more setup. Deferred to implementation.
- **Core HA?** Not in v2.0.0. Reserved for v2.1.0.
- **Compat derivation frequency?** Daily is a starting point. May need tuning based on pack size.
- **Multi-tenant?** The current design assumes one operator, one set of runners, one artifact server. Multi-tenant (multiple teams sharing a core) is a much larger design; not in scope.

---

## 22. Appendices

### 22.1 v1.4.0 features carried forward unchanged

- Packing slip contract (§5 of v1.4.0)
- Release bundle contract (§6)
- Canonical hashing (§5.4)
- Side determination precedence (§Side_Determination.md)
- `deploy_pack` v2 integration (§14)
- `manifest check`, `runtime inspect`, `status`, `verify` commands
- Shipping transaction (§13)

### 22.2 v1.4.0 features replaced

| v1.4.0 | v2.0.0 replacement |
|---|---|
| `state.toml` files | `runs` MySQL table |
| `release-index.jsonl` | `registry_entries` MySQL table |
| `latest-slip` file | `pointers` MySQL table |
| Phantom `ledger.sqlite` | Real `budget` MySQL table |
| Single daemon | Core + runners |
| Local warehouse | Artifact server |
| Local bundle store | Artifact server |
| Local file lock | MySQL row lock (no lock needed) |
| Phantom Discord bot | Real Discord bot as peer service |
| No compat matrix | First-class compat matrix |

### 22.3 Glossary additions

- **Core** — central control plane on DEV. Hosts MySQL, dispatches work, owns the registry.
- **Runner** — stateless worker. Polls core, executes work, uploads artifacts.
- **Artifact server** — store of record for artifact bytes. Independent service.
- **Work lease** — temporary claim on a work item by a specific runner. Expires if not heartbeat.
- **Compat matrix** — derived table of learned compatibility between mod versions.
- **Boot attempt** — one record of one `iterate` attempt. Source of compat evidence.
- **Graph diff** — CLI command that answers "what's affected by upgrading/rolling back X" from existing slip data.

---

**End of v2.0.0 draft.**