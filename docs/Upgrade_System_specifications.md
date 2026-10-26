docs/Upgrade_System_specifications.md

**Status:** Design of record. Architecture frozen at v1.4.0. Slices 1–14 implemented and passing (2421 tests, 92.43% coverage at Slice 14 close). Slice 15 T3b+T3c implemented and passing (2711 tests, 92.55% coverage); T11 (approval replay and expiry) remains. Slice 16 (migration rehearsal) remains. Production cutover is gated on T11 and T20.

**Supersedes:** v1.3.1

**Baseline contract:** `Project_Specs.md` v3.0 (`deploy_pack` v2)

**Date:** 2026-10-19

**Changes in v1.4.0:**

1. **§9 rewritten** — In-house mods are built by **cloning the mod's repository and running its own build script**, not by the upgrade toolchain. The tool never needs to know Forge, NeoForge, or Fabric. This replaces the v1.3.1 §9 which assumed the tool owned the toolchain.
2. **§12 updated** — Production daemon's deployment orchestration is implemented. Stages 11–19 of §12.4 are wired end-to-end. Journal, lock, pull, health, backup, and recovery all exist as modules.
3. **§15.1 updated** — `build-inhouse` signature changed from `<mod> <revision>` to `<name> [--var KEY=VALUE ...]`. Git ref comes from the spec file, not the CLI.
4. **§17 updated** — `config.d/build/<name>.yaml` is a new configuration file class.
5. **§20 updated** — 34 of 35 acceptance tests covered. Only T11 (approval replay/expiry) remains, blocked on Slice 15.
6. **§22 updated** — new known limits from Slice 13/14.
7. **§23.1 updated** — authority table reflects new `config.d/build/` authority and repository-trigger build semantics.
8. **§11 and §17 updated** — Slice 15 T3b/T3c Discord bot interface documented: signed-HTTP interaction endpoint (§11.1.1) and `/notify` service endpoint (§11.1.2), distinct auth models per route (§11.3.1), `[discord].application_id` and `[discord].public_key` added to `config.d/upgrade.toml` (§17.1), `[discord.notify_dispatch]` added to `config.d/deploy_pack.toml` (§17.2), new `config.d/discord_bot.toml` config class (§17.6).
9. **Slice 14.5 amendments applied** — §4.1 storage tree (`usage.jsonl` replaces the phantom `ledger.sqlite`), §8.2.1 caps implementation, §17.1 `[budget]` block, §20 T36, §22 known limits, §23.1 authority table rows.

**Retained from v1.3.1:**
- Artifact authority: prod receives exact tested bytes.
- Slip `provider`/`project_id`/`version_id` are provenance metadata, not fetch instructions.
- Promote stages the slip and stops. Human commits, tags, pushes.
- Slip is additive to existing config. Instance-to-Compose resolution preserved.
- Verify, don't derive. Paths come from existing config; slip verifies.
- `release_mode` (`update` | `upgrade`) governs runtime authority.
- Canonical hashing pipeline (RFC 8785, NFC, bounded integers, safe `None` normalization).
- DaemonCore-based production daemon.
- Four-command resolver lifecycle with explicit state machine.
- `resolution_inputs_sha256` and `expected_hashes` semantics.
- In-house identification via `config.d/upgrade.toml [in-house].filenames` — **not** retired in v1.4.0; retirement is deferred to Slice 17+.

**v1.4.0 documentation set:** This document plus three companions constitute the frozen v1.4.0 contract. All four are normative.

- `docs/Manifest_Schema.md` — manifest format, seed rules, command lifecycle
- `docs/Resolution_Report.md` — report schema, state machine, acknowledgment
- `docs/Side_Determination.md` — side precedence chain, per-provider rules

---

## 1. Purpose

`deploy_pack` v2 applies an already-materialized artifact set. This specification defines the layer that **constructs, validates, freezes, publishes, and transports** that artifact set:

1. Resolve exact mod versions from upstream providers against a target Minecraft/loader runtime.
2. Download artifacts once into DEV's warehouse.
3. Build in-house mods by cloning their repositories and running their own build scripts.
4. Iterate a DEV server against crash logs until the candidate pack stabilizes.
5. Validate a copy of the production world against the candidate.
6. Produce a downloadable client pack.
7. Freeze the tested state into a git-tagged **packing slip**.
8. Publish an immutable release bundle.
9. Serve the bundle to production, which pulls it, verifies it, and deploys it.

The system extends `deploy_pack`; it does not replace it. Production deployment lifecycle remains owned by the v2 contract.

---

## 2. Normative Language

- **MUST / MUST NOT** — mandatory for conformance.
- **SHOULD / SHOULD NOT** — default requirement; deviations need documented reason.
- **MAY** — optional.

---

## 3. Three-Machine Topology

| Machine | Role |
|---|---|
| **Client-side** | Authoring. Pushes code, configs, kubejs, in-house mod source to git. Runs the Minecraft client for in-game review. |
| **Dev server** | Resolution, in-house builds, validation, migration testing, client ZIP publication, artifact warehouse, release bundle publication. Hosts the `latest` integration server. |
| **Prod server** | Hosts `survival` and `creative` instances. Pulls release bundles from DEV. Deploys via `deploy_pack`. Runs health checks. Hosts the production daemon. |

### 3.1 Artifact flow

```
Providers (Modrinth, CurseForge)          In-house mod repos (any git host)
        │                                          │
        │ fetch (DEV only)                         │ clone + build (DEV only)
        ▼                                          ▼
                        DEV warehouse (sync/downloads/)
                                       │
                                       │ resolve + validate + freeze
                                       ▼
                        DEV release bundle (www/releases/<release-id>/)
                                       │
                                       │ rsync over SSH, pull-only, read-only account
                                       ▼
                              Prod staging/<release>.partial/
                                       │
                                       │ verify + activate
                                       ▼
                              Prod releases/<release>/
                                       │
                                       │ shipping system
                                       ▼
                              Prod mods/
```

### 3.2 Constraints

- **DEV** is the only machine that contacts upstream providers.
- **DEV** is the only machine that clones in-house mod repositories and runs their build scripts.
- **Prod** MUST pull release bundles from DEV via rsync over SSH. Prod MUST NOT contact Modrinth, CurseForge, any in-house mod repository, or any other mod provider during any release cycle.
- **DEV** MUST NOT push to prod. DEV exposes its publication directory; prod initiates transfer.
- Prod MUST receive the exact artifact bytes that DEV tested. The bytes prod receives MUST hash to the slip's recorded SHA-256 values.
- DEV MUST NOT pull mods from prod (prod runs an older runtime during upgrade).
- Client MUST NOT be a runtime dependency for DEV's resolution loop.
- Container images MAY be pulled by prod from an explicitly allowlisted OCI registry by immutable digest. This is a separately authorized source class and does not violate the provider constraint.
- **SMB/NFS are not supported.** rsync over SSH is the only supported transport.

---

## 4. Storage Model

### 4.1 Repository layout

```
repo/
├── config.d/
│   ├── build/
│   │   ├── <name>.yaml           # in-house build specifications (NEW v1.4.0)
│   │   └── ...
│   ├── deploy_pack.toml
│   ├── upgrade.toml              # upgrade-system config
│   ├── side_overrides.toml       # active today; retired after cutover
│   └── .env
├── docs/
│   ├── Manifest_Schema.md
│   ├── Resolution_Report.md
│   ├── Side_Determination.md
│   └── Spec_Amendments_v1.3.1.md
├── src/minecraft/
│   ├── common/
│   ├── deploy_pack/
│   └── modpack_upgrade/
├── sync/
│   ├── audit/
│   ├── config/                    # git-tracked
│   ├── downloads/                 # warehouse (cache + active pack)
│   │   ├── *.jar                  # jars (not git-tracked)
│   │   └── .index/                # git-tracked seed source
│   │       └── *.pw.toml
│   ├── kubejs/                    # git-tracked
│   ├── packing_slips/             # git-tracked release contracts
│   │   ├── stable                 # text file: one line, e.g. "v1.0.2.toml"
│   │   ├── v1.0.0.toml
│   │   ├── v1.0.1.toml
│   │   └── v1.0.2.toml
│   └── velocity-plugins/
├── .build-workspace/              # NEW v1.4.0; transient; kept on failure
│   └── <name>-<timestamp>/        # in-house mod clone
├── www/                            # artifact store (DEV)
│   ├── runs/<run-id>/              # DEV-run artifacts
│   │   └── resolution-report.toml  # DEV-local; published for link access
│   └── releases/<release-id>/      # immutable published bundles
│       ├── packing_slip.toml
│       ├── approval.json
│       ├── bundle-manifest.json
│       ├── artifact-manifest.json
│       ├── client-distribution-manifest.json
│       ├── config/                 # config snapshot
│       ├── kubejs/                 # kubejs snapshot
│       ├── runtime/                # runtime payload
│       └── artifacts/              # exact required artifacts
├── tests/
├── docker-compose.yml
└── pyproject.toml

# DEV-local operational state (not Git-tracked)
/var/lib/minecraft-upgrade/
├── latest-slip                     # candidate pointer (one line)
├── usage.jsonl                    # API + byte budget log (append-only)
├── runs/<run-id>/                  # per-run resolver state
│   ├── state.toml
│   ├── acknowledgment.json         # absent if not required
│   └── staging/                    # transient; cleaned post-materialize
└── candidates/<run-id>/            # per-candidate sealed artifacts
    ├── metadata/*.pw.toml          # candidate-only
    └── packing_slip.toml           # DEV-local; git-tracked via promote

# Prod-local state
/srv/minecraft/
├── staging/<release>.partial/      # incomplete transfer
├── releases/<release>/             # verified immutable bundle
├── backups/world/<release>/        # pre-upgrade world backup
└── active-release                  # current deployment identity

# Prod daemon state
/var/lib/minecraft-deploy/
├── idempotency.sqlite
├── audit.jsonl
├── daemon.lock                     # deployment lock (single-file, atomic)
├── operation.jsonl                 # deployment journal (append-only)
└── state/                          # FileStateStore (DaemonCore)
```

### 4.2 Pointer files

| File | Location | Git-tracked | Meaning | Updated |
|---|---|---|---|---|
| `stable` | `sync/packing_slips/stable` | Yes | Points at an immutable release slip. Consumed by prod `survival` and `creative`. | Manually, after release approval and tag push. |
| `latest-slip` | `/var/lib/minecraft-upgrade/latest-slip` | No | Points at a candidate ID. Consumed by DEV's `latest` integration server only. | By DEV's resolver when a new candidate is selected. |

The `latest` server's Git inputs are **not** recorded in a pointer. `latest` reads one clean committed HEAD revision for configs/kubejs on each deployment; the pointer only fixes the JAR set.

### 4.3 Invariants

- `sync/downloads/` is a flat jar directory plus `.index/`. One copy per unique artifact filename in the jar set.
- `sync/downloads/.index/` is git-tracked and is the authoritative seed for resolution. Only `promote` writes to it.
- `mods/` at the instance root is the runtime deployment target.
- `packing_slips/` is git-tracked. Slips and `stable` are text.
- `www/releases/<release-id>/` is immutable once published.
- `www/runs/<run-id>/` is DEV-local; not transferred to production.
- `.build-workspace/` is transient. A successful build removes its clone; a failed build keeps it for inspection.
- `latest-slip` is DEV-local and not Git-tracked.
- Candidate slips (`/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml`) are DEV-local. Only `promote` copies them to `sync/packing_slips/<tag>.toml`.
- A sealed candidate slip is **immutable between `materialize` and `promote`**. After `materialize` writes `candidates/<run-id>/packing_slip.toml`, no command modifies it in place until `promote` runs. A change to any input between those two commands produces a new run-id and a new slip; the affected command exits 3 with a diagnostic before mutating anything.

  `promote --release-mode upgrade` is the **single deliberate exception**. Upgrade-mode releases declare the slip's `[runtime]` as authoritative for the target runtime; `promote` must reconcile the effective Compose file against that runtime before the release can be tagged. Doing so changes the Compose file's working-tree blob hash, which `[inputs.git].compose_tree` records. Reconciliation therefore:

  1. Applies the Compose env-var diff atomically (`_compose.apply_compose_diff`) — verify-before-write, temp-then-`os.replace`.
  2. Recomputes `[inputs.git].compose_tree` via `git hash-object` on the working-tree Compose file.
  3. Recomputes `content_sha256` over the updated slip body.
  4. Re-seals the slip in place, atomically.

  The pre-promote slip is superseded. The **re-sealed** slip — not the pre-promote version — is what `promote` copies to `sync/packing_slips/<tag>.toml`. Rollback to a pre-promote slip is not supported; each upgrade release is a fresh identity, and any `compose_tree` or `content_sha256` change produced by a `--release-mode upgrade` promote yields a new release identity distinct from any prior candidate.

  `promote --release-mode update` does **not** re-seal. Update-mode releases treat the effective Compose as runtime authority and never modify it, so the materialized slip is what gets promoted verbatim.
- No symlinks in the `mods/` deployment path. Shipping writes real files.
- One physical copy per unique artifact per host, except the deployed copy in `mods/`.
- Prod's release store is populated **only** by pulling bundles from DEV. It is never populated by provider API calls.
- DEV never initiates a transfer to prod. Prod is the puller.

---

## 5. Packing Slip Contract

### 5.1 Purpose

The packing slip is the immutable release registry. It declares which artifacts, configs, kubejs, and assets belong to a release, and identifies the runtime they were tested against.

Runtime authority depends on `release_mode`:

- **`update`** — Same-game-version update. Docker Compose is authoritative for runtime. Slip `[runtime]` records what was tested; it MUST match Compose.
- **`upgrade`** — Planned release upgrade. Slip `[runtime]` is authoritative for target runtime. Compose MUST be reconciled to match.

`release_mode` is determined by the CLI `--release-mode` flag on `minecraft-upgrade update` and flows into the slip at `materialize`. It is not declared in the manifest.

### 5.2 Schema

```toml
packing_slip_version = 1
release_id = "v1.0.2"                    # immutable; re-use requires new ID
release_mode = "update"                  # "update" or "upgrade" — immutable
content_sha256 = "..."                   # canonical body hash (see §5.4)

[meta]
created_at_utc = "2026-10-12T00:00:00Z"
resolver_version = "minecraft-upgrade 1.4.0"
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
config_tree      = "<git tree hash of sync/config>"
kubejs_tree      = "<git tree hash of sync/kubejs>"
compose_tree     = "<git tree hash of docker-compose.yml>"
deploy_toml_tree = "<git tree hash of config.d/deploy_pack.toml>"

[fingerprints]
manifest_sha256          = "..."   # raw bytes of sync/modpack.manifest.toml
resolution_inputs_sha256 = "..."   # canonical semantic input snapshot (§5.4.1)

runtime_sha256           = "..."   # canonical [runtime] descriptor
server_inputs_sha256     = "..."   # canonical server-stage input manifest
client_inputs_sha256     = "..."   # canonical client-stage input manifest

[tested]
world = "survival"               # "survival" | "creative" | absent
snapshot_id = "<snapshot-id>"    # SHA-256 of world snapshot manifest

[[jar]]
filename = "create-1.20.1-6.0.8.jar"
sha256 = "abc..."                # locally verified SHA-256 of artifact bytes
side = "both"                    # "both" | "server" | "client"

provider = "modrinth"                    # provenance metadata
provider_endpoint = "https://api.modrinth.com/v2"  # preparation-stage locator
project_id = "..."
version_id = "..."

pw_toml = "create-1.20.1-6.0.8.pw.toml"
pw_toml_sha256 = "..."

dependencies = [
  { namespace = "project", provider = "modrinth", project_id = "...",
    version_id = "...", type = "required", source = "pw_toml" },
]

[[jar]]
filename = "custom-mod-1.0.0.jar"
sha256 = "def..."
side = "both"

provider = "in-house"
provider_endpoint = "www/artifacts/in-house/custom-mod/<build-id>/"

pw_toml = "custom-mod-1.0.0.pw.toml"
pw_toml_sha256 = "..."

build_id = "<build-id>"
source_repo = "..."
source_commit = "<git sha>"
toolchain_id = "<toolchain-id>"

dependencies = []

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
output_filename = "minecraft_client_20261012.zip"
sha256 = "..."
with_resources = true
member_manifest_sha256 = "..."

[instances.survival]
targeted = true
required_sides = ["both", "server"]
config_mode = "merge"            # "merge" | "delete"
kubejs_mode = "delete"           # "delete" only
config_deletions = []            # explicit relative paths to remove under merge
protect_patterns = []

[instances.creative]
targeted = true
required_sides = ["both", "server"]
config_mode = "merge"
kubejs_mode = "delete"
config_deletions = []
protect_patterns = []
```

The enum comments are normative. `config_mode ∈ {merge, delete}`. `kubejs_mode = delete` only. `kubejs_mode = "merge"` is not supported.

### 5.3 Rules

1. `release_id` MUST be immutable. Re-using an ID with different content MUST fail.
2. `content_sha256` MUST be computed over the canonical body (§5.4).
3. `release_mode` MUST be `"update"` or `"upgrade"`.
4. **Production release deploys MUST verify `[inputs.git]` tree hashes against the working tree. Mismatch blocks deploy.**
5. **Integration deploys (`latest`) MUST NOT enforce `[inputs.git]` tree hashes.** Integration composes the JAR set from the candidate slip with one clean committed Git revision.
6. Every `[[jar]]` MUST resolve through its declared `provider` + `provider_endpoint` during preparation. Resulting artifact MUST stage at `sync/downloads/<filename>` and match recorded SHA-256. Applies equally to external and in-house artifacts.
7. Every `[[jar]]` MUST follow the same promotion and release-packaging workflow. Provider origin MUST NOT create a separate deployment pipeline or distinct registry section.
8. Every `[[jar]]` MUST declare dependency edges with:

   - `namespace ∈ {"project", "modid"}`
   - `source ∈ {"pw_toml", "jar_manifest", "provider_metadata"}`
   - `type ∈ {"required", "optional", "incompatible", "embedded", "tool", "include"}`

   Empty array if none.

   **CurseForge `relationType` → `type` mapping:**

   | `relationType` | `type` |
   |---|---|
   | 1 | `embedded` |
   | 2 | `optional` |
   | 3 | `required` |
   | 4 | `tool` |
   | 5 | `incompatible` |
   | 6 | `include` |

   Modrinth string `dependency_type` values (`required` / `optional` / `incompatible` / `embedded`) map by identity. The `.pw.toml` writer emits only `required`, `optional`, `incompatible`, `embedded`. `tool` and `include` are preserved in the slip with `source = "provider_metadata"` and not written to `.pw.toml` (§5.7).

9. Every `[[jar]]` MUST reference a `.pw.toml` file whose SHA-256 is recorded and validated at preparation. The `.pw.toml` file MUST conform to §5.7.
10. `[instances.*]` MUST include every targeted production instance. A release that omits an instance MUST NOT deploy to it.
11. Protection patterns MUST be explicit per instance. `.deploy_protect` MUST NOT affect slip-mode deploys. The matching primitive and scope are defined in §13.5.
12. `provider`, `project_id`, `version_id`, `provider_endpoint`, and in-house build metadata record origin and provenance. They MUST NOT instruct production deployment to fetch directly from a provider. Production MUST consume the validated staged artifact.
13. `provider_endpoint` MUST identify a location that can resolve the selected artifact. If generic (e.g., API base), provider-specific IDs are required.
14. `[tested].world` MUST be `survival` or `creative`, or absent if no migration test was required.
15. `release_mode = "update"`: resolved Compose runtime MUST match slip `[runtime]`.
16. `release_mode = "upgrade"`: effective Compose MUST match slip `[runtime]`; slip `compose_tree` MUST match working-tree Compose.
17. `[fingerprints]` values are part of the canonical slip body. Changing a fingerprint changes the slip identity and invalidates prior approval. This is intentional.
18. Slip parsing MUST preserve document order for `[[jar]]`, `[[resource_pack]]`, and `[[velocity_plugin]]`. Canonical ordering (§5.4) is applied only when computing `content_sha256`.
19. `config_mode` MUST be `"merge"` or `"delete"`. `kubejs_mode` MUST be `"delete"`.

### 5.4 Canonical hashing

The canonicalization algorithm is normative.

**Pipeline.** The canonicalization pipeline is:

```
TOML parse -> NFC normalization and collision checks -> None
normalization -> slip-specific array ordering -> RFC 8785 canonical
JSON -> UTF-8 -> SHA-256.
```

**Algorithm:**

1. Parse TOML using Python's standard-library `tomllib`.
2. Normalize all strings and keys to Unicode NFC.
3. Reject duplicate keys and keys that collide after normalization. Keys are case-sensitive: `"Side"` and `"side"` are distinct; two keys that normalize to the same NFC string are rejected.
4. Apply `None` normalization:
   - A `None` **mapping value** is dropped. TOML has no null; a programmatically-constructed body with `{"provider": None}` MUST hash identically to a TOML-round-tripped body with the key absent.
   - A `None` **array element** is rejected. Removing an element from an array silently changes its cardinality and, for ordered sequences, its meaning; failing validation is the frozen rule.
   - `None` nested inside a mapping inside an array, or inside an array inside a mapping, is normalized by the same rules applied recursively: mapping-`None` stripped, array-`None` rejected.
5. Permit only strings, booleans, safe integers, arrays, and tables in the canonical body. Reject floats and TOML datetime values. A "safe integer" is bounded by RFC 8785 §3.2.2.3: `-(2^53 - 1) <= n <= 2^53 - 1`. Integers outside this range are rejected even though Python's `int` is arbitrary-precision, because canonical JSON is a cross-language interchange format.
6. Remove the root `content_sha256` field before canonicalization.
7. Sort object keys according to RFC 8785 (JSON Canonicalization Scheme).
8. Apply array ordering rules (below).
9. Encode as UTF-8 and compute SHA-256.

**Normalization order.** NFC normalization MUST occur before RFC 8785 serialization. RFC 8785 requires string data to be preserved as supplied and does not itself perform Unicode normalization; the preceding NFC step is a slip-specific preprocessing rule, not part of RFC 8785. An implementation that applied raw RFC 8785 to unnormalized input would produce different hashes for equivalent slips and would fail to catch NFC-collision keys.

**Implementation:** Pinned Python `rfc8785` dependency. Exact version pinned in `pdm.lock`. Test vectors verify output stability.

**Array ordering:**

| Field | Rule |
|---|---|
| Top-level `[[jar]]` | Set, sorted by NFC-normalized `filename` |
| `[[jar]].dependencies` | Set, sorted by complete canonical dependency identity |
| `[[resource_pack]]` | Set, sorted by `instance`, then `filename` |
| `[[velocity_plugin]]` | Set, sorted by `filename` |
| `[instances.*].required_sides` | Set, sorted lexicographically after NFC normalization |
| `[instances.*].config_deletions` | Set, sorted lexicographically after NFC normalization |
| Client-distribution member manifest | Set, sorted by archive path |
| Snapshot file manifest | Set, sorted by relative POSIX path |
| Ordered operational steps, migration actions, user-defined sequences | Preserve declared order |

Duplicate entries within a set are rejected unless the schema explicitly defines deduplication semantics. Duplicate JAR filenames in one release are always rejected.

**Fingerprints:**

| Fingerprint | Source |
|---|---|
| `content_sha256` | Canonical slip body |
| `manifest_sha256` | Raw `sync/modpack.manifest.toml` bytes |
| `resolution_inputs_sha256` | Canonical semantic resolver input snapshot (§5.4.1) |
| `runtime_sha256` | Canonical JSON of resolved `[runtime]` descriptor |
| `server_inputs_sha256` | Canonical manifest of server-stage input files |
| `client_inputs_sha256` | Canonical manifest of client-stage input files |

Each input manifest contains normalized relative path, file kind, file mode where applicable, byte size, and content SHA-256. Ownership, timestamps, and incidental filesystem metadata are excluded.

#### 5.4.1 `resolution_inputs_sha256` calculation

Semantic snapshot of every input the resolver consumed. Raw-file identity is recorded separately (`manifest_sha256`, `side_overrides_sha256` in the report).

```
canonical_snapshot = {
  "release_mode": "update" | "upgrade",
  "manifest": {
    "minecraft": {"version": <str>, "loader": <str>, "loader_version": <str>},
    "policy": {"selection": <str>, "no_version": <str>}
  },
  "in_house_filenames": [<str>, ...],   # sorted lexicographically
  "seed": [
    {
      "provider":           "modrinth" | "curseforge" | "in-house",
      "current_filename":   <str>,
      "jar_sha256":         <64-hex>,
      "current_version_id": <str>,       # present for external only
      "build_id":           <str>,       # present for in-house when known
      "pw_toml": {
        "side_declared":    <str>,       # "" if absent or "unknown"
        "dependencies": [
          {
            "addon_id":      <str>,
            "type":          <str>,
            "namespace":     "project" | "modid"
          }
          # sorted by (addon_id, type, namespace)
        ]
      }
    }
    # sorted by (project_id_or_filename, current_filename)
  ],
  "side_overrides": {
    "by_id":                  { <id>: <side> },
    "by_filename":            { <filename>: <side> },
    "deployment_tool_review": { <filename>: <side> }
  }
}

resolution_inputs_sha256 = SHA256(RFC 8785 canonical JSON of canonical_snapshot)
```

Determinism requirements:

- Every collection sorted as noted.
- Raw file hashes (`manifest_sha256`, `side_overrides_sha256`) NOT in the snapshot. Formatting-only edits do not change this fingerprint.
- `generated_at_utc` not included.
- `jar_sha256` requires the seed jar. Missing jar = exit 3 before fingerprint.
- In-house seed entries omit `current_version_id` (no external identity). `build_id` present only when a config or build record supplies one. `jar_sha256` is the sole identity when neither is available.
- Ordering: `manifest_sha256` first, then `resolution_inputs_sha256`, then slip body, then `content_sha256`.

### 5.5 Deployment modes

| Mode | Applies to | JAR source | Config/KubeJS source | Tree hashes |
|---|---|---|---|---|
| **`release`** | `survival`, `creative` | Release bundle | Release bundle | Enforced |
| **`integration`** | `latest` | DEV-local candidate | One clean committed HEAD revision | Not enforced |

Both modes verify artifact SHA-256 before shipping. Both consume the same `[[jar]]` contract.

`deployment_mode` (`release` vs `integration`) is orthogonal to `release_mode` (`update` vs `upgrade`). The former governs shipping; the latter governs runtime authority.

### 5.6 Runtime authority decision logic

| `release_mode` | Authoritative runtime source | Deployment check |
|---|---|---|
| `update` | Docker Compose | Slip `[runtime]` MUST match effective Compose. `compose_tree` MUST match working tree. Mismatch → abort. |
| `upgrade` | Slip `[runtime]` | Effective Compose MUST match slip target; `compose_tree` hash MUST match. Mismatch → abort. |

`release_mode` is set by `--release-mode` CLI flag on `update` and flows into the slip at `materialize`. It is persisted in `state.toml` so `acknowledge` and `materialize` re-verify Compose integrity for `update` mode.

Authority changes only when the release explicitly declares an upgrade.

### 5.7 `.pw.toml` writer contract

The `.pw.toml` files referenced by `[[jar]].pw_toml` MUST conform to the schema consumed by `deploy_pack.deps.parse_prism_toml`. The writer lives at `modpack_upgrade.pw_toml`.

**Rules:**

1. `filename` — required; exact staged artifact filename. No path separators, no NUL, not `.` or `..`.
2. `name` — optional display name.
3. `side` — always written explicitly. One of `client`, `server`, `both`, or the sentinel `unknown`. The sentinel preserves the parser's unmarked-entry detection (§3.11, §6.3 of `Project_Specs.md` v3.0): an omitted `side` would default to `both` in the parser and silently misclassify the artifact.
4. `[download]` — emitted only when `url`, `hash-format`, and `hash` are all present. Partial blocks are rejected.
5. `[update.modrinth]` or `[update.curseforge]` — exactly one provider block for an externally sourced artifact; zero for an in-house artifact that has no upstream identity; never two.
6. `[[x-prismlauncher-dependencies]]` — project-ID edges only, as `addonId` and `type`. Mod-ID edges are recorded in the packing slip (§5.2, `[[jar]].dependencies`) and MUST NOT be mislabeled as Prism project IDs.
7. Output is byte-stable. Field order is fixed, strings are NFC normalized, and the pinned `tomli-w` dependency's emitter runs. Golden fixtures pin the output.
8. The writer MUST NOT modify `deps.py`. Compatibility with the existing parser is a hard constraint, not a soft goal.

The generated file's SHA-256 is recorded as `[[jar]].pw_toml_sha256` in the packing slip. Production obtains artifact bytes from the release bundle (§6), not from the `.pw.toml`'s download URL.

**`source = "provider_metadata"` edges (CurseForge `Tool` / `Include`) are preserved in the slip but not written to `.pw.toml`** because Prism has no representation for them. The slip carries them for audit; the writer omits them.

**In-house artifacts.** Prior to Slice 17 (deferred), in-house `.pw.toml` files are produced by the in-house mod's own build script (§9). The upgrade tool does not write them. See §9.7 for the deferred retirement of the `[in-house].filenames` config list.

### 5.8 `SlipContext`

`SlipContext` is the immutable bridge between a verified slip and the existing `deploy_pack` pipeline. It is constructed once, validated at construction, and carried on `DeploymentConfig` and `PreflightPlan`.

**Fields:**

- `slip: PackingSlip` — the verified slip.
- `mode: "release" | "integration"` — the deployment mode (§5.5).
- `bundle_root: Path` — release bundle root, or candidate store root in integration mode.
- `config_source: Path` — the config snapshot root.
- `kubejs_source: Path` — the KubeJS snapshot root.
- `jar_sources: Mapping[str, Path]` — filename → artifact path.
- `rp_sources: Mapping[str, Path]` — filename → resource pack path.
- `runtime_payload: Path` — staged runtime payload location.
- `captured_head: str | None` — the committed Git revision captured at construction time (integration mode only).

**Rules:**

1. `SlipContext` is validated at construction. Scope modules MUST NOT independently reinterpret pointers or mutable source paths.
2. In slip modes (`release` and `integration`), `deps.resolve_mod_sources` and `overrides.py` MUST NOT be called. The slip-derived source set is the authority.
3. In `integration` mode, config and KubeJS inputs come from a single clean committed Git revision captured at construction time. The `latest-slip` pointer is not consulted by scope modules; it is resolved once by the integration entry point and its result is embedded in `SlipContext`.
4. In `release` mode, config and KubeJS inputs come from the release bundle's snapshots. Tree hashes are enforced.
5. When `DeploymentConfig.slip is None`, `deploy_pack` behaves exactly as v2. Slip-mode branches are additive; no legacy behavior changes.

### 5.9 Manifest and resolver contract

The manifest schema, seed source, and resolver command lifecycle are defined in `docs/Manifest_Schema.md` and summarized here.

**Manifest** (`sync/modpack.manifest.toml`): 5 fields.

```toml
[minecraft]
version = "..."
loader = "forge" | "neoforge" | "fabric" | "quilt"
loader_version = "..."

[policy]
selection = "release_then_beta" | "release_only" | "release_then_beta_then_alpha"
no_version = "ask" | "drop" | "fail"
```

Manifest does not declare mods, versions, sides, or release mode.

**Seed** (`sync/downloads/.index/*.pw.toml`): one entry per Prism-authored `.pw.toml`. Jars in `sync/downloads/` not referenced by an `.index` entry are warehouse cache, ignored by the resolver.

**Side determination**: `docs/Side_Determination.md`. Precedence chain: `side_overrides.toml` > JAR metadata > provider metadata > `.pw.toml` side. `side_overrides.toml` is final authority. Malformed `.pw.toml` side fails at parse time; `"unknown"` or absent side is valid.

**In-house identification**: `config.d/upgrade.toml [in-house].filenames`. Filenames in that list are treated as `provider = "in-house"`, bypassing provider query. Retirement deferred (§9.7).

---

## 6. Release Bundle Contract

### 6.1 Publication

Publication is an explicit operator command:

```
minecraft-upgrade publish <release-id>
```

Publication is not automatic. Promotion does not publish.

### 6.2 Bundle contents

Each bundle at `www/releases/<release-id>/` MUST contain:

| File | Purpose |
|---|---|
| `packing_slip.toml` | The immutable slip. |
| `approval.json` | Signed approval record. |
| `bundle-manifest.json` | Inventory of every file in the bundle by relative path, size, and SHA-256. |
| `artifact-manifest.json` | Inventory of every artifact by kind, path, size, and SHA-256. |
| `client-distribution-manifest.json` | Member inventory of the client ZIP. |
| `config/` | Snapshot of config inputs. |
| `kubejs/` | Snapshot of KubeJS inputs. |
| `runtime/` | Verified runtime payload (server JAR, loader installer, pinned assets). |
| `artifacts/` | Exact required artifacts, including JARs, resource packs, velocity plugins, client ZIP. |

The bundle manifest is signed. The bundle is immutable: existing bundles MUST NOT be modified. Re-publication of the same release ID MUST fail if the bundle already exists with different content.

### 6.3 Publication preconditions

Publication MUST verify, before writing any bundle file:

1. The slip exists at `sync/packing_slips/<release-id>.toml`.
2. The slip `content_sha256` matches the canonical hash.
3. The approval record exists and is valid, bound to candidate identity including `resolution_report_sha256`.
4. Every artifact in the slip exists in DEV's warehouse with matching SHA-256.
5. The client ZIP exists and matches `client_distribution.sha256`.
6. The runtime payload is staged and its recorded hashes match.
7. Config and KubeJS snapshots are captured and their tree hashes match `[inputs.git]`.

Any failure → abort. No partial bundle is published.

### 6.4 Bundle manifest

`bundle-manifest.json` MUST be a canonical JSON document with:

```json
{
  "schema_version": 1,
  "release_id": "v1.0.2",
  "release_tag_object_sha": "...",
  "release_commit_sha": "...",
  "slip_content_sha256": "...",
  "approval_sha256": "...",
  "files": [
    { "path": "artifacts/create-1.20.1-6.0.8.jar", "size_bytes": 123456, "sha256": "..." }
  ]
}
```

The manifest itself is signed by the release-signing key. The signature is stored alongside the manifest as `bundle-manifest.sig`.

---

## 7. Release Registry and Trust

### 7.1 Registry

`release-index.jsonl` is a Git-tracked, append-only, signed registry. Each line is a JSON object:

```json
{
  "schema_version": 1,
  "release_id": "v1.0.2",
  "tag_object_sha": "...",
  "tag_object_format": "sha1" | "sha256",
  "commit_sha": "...",
  "slip_content_sha256": "...",
  "bundle_manifest_sha256": "...",
  "previous_entry_hash": "...",
  "entry_hash": "...",
  "signature": "...",
  "key_id": "..."
}
```

The chain begins at a pinned trust anchor committed to the repository. Each entry's `previous_entry_hash` MUST match the prior entry's `entry_hash`.

### 7.2 Trust anchor

The trust anchor is a specific signed initial entry. It is distributed to prod by configuration and MUST be verified before any chain walk. Prod pins trusted public keys; a new key requires explicit operator approval.

### 7.3 Prod verification

Prod MUST reject:

- Retargeted tags (tag object SHA changed).
- Deleted tags.
- Altered slips (slip hash mismatch).
- Truncated registry chains.
- Forked registry entries (two entries with the same `previous_entry_hash`).
- Invalid signatures.
- A registry entry whose `release_id` maps to a different identity than previously recorded.

Prod maintains a durable record of the last accepted registry-chain head.

### 7.4 Git object format

Git object IDs are recorded using their actual object format. The registry entry records `tag_object_format` as `"sha1"` or `"sha256"`. Git object IDs MUST NOT be mislabeled as SHA-256 hashes unless the repository actually uses SHA-256 object format.

### 7.5 Key management

- Release-signing private key: DEV-host secret store.
- Approval-signing private key: DEV-host secret store.
- Prod pins trusted public keys.
- Key rotation requires a new `key_id` and operator-approved trust-anchor update.
- Revocation requires removing the key from prod's trusted set and recording the revocation in the registry.

---

## 8. Dev Pipeline

### 8.1 Dev runtime

- One Minecraft container for the resolution loop.
- Velocity proxy available for integration stage.
- `latest` integration container (separate from resolution loop).
- Discord bot as operator interface (Slice 15).
- Upgrade controller owns candidate state, artifact preparation, container lifecycle, and publication.

### 8.2 Phase A — four-command lifecycle

Phase A is split across four commands. Each is invoked explicitly by the operator; each exits and prints the next command. The pipeline is:

```
build-inhouse <name>         (optional, before update)
    → clone mod's repo, run its build script, stage artifacts
    → warehouse now has the in-house jar and its pw.toml

update --release-mode <mode>
    → resolve versions
    → write resolution report (provisional sides)
    → write state.toml
    → exit per policy and state
    ▼
[acknowledge <run-id>]        ← only if state=awaiting_acknowledgment
    → verify report + inputs + Compose
    → record acknowledgment
    → state=acknowledged
    ▼
materialize --run-id <run-id>
    → verify report + inputs + Compose
    → download artifacts (external) / source from warehouse (in-house)
    → verify hashes
    → inspect jar metadata; final side
    → seal slip → DEV-local candidates/<run-id>/
    → state=materialized
    ▼
iterate --run-id <run-id>
    → boot loop on finalized candidate
    → classify failures, batch fixes, caps
```

**Command contracts** are frozen in §15.1.

**State machine** (tracked in `state.toml`):

| State | Meaning | Materialize accepts? |
|---|---|---|
| `resolved` | `update` completed. No losses, or losses with `drop`. | Yes |
| `awaiting_acknowledgment` | `update` completed with losses under `ask`. | No |
| `acknowledged` | Human acknowledged losses. | Yes |
| `materialized` | Slip sealed. | N/A (ready for iterate) |
| `failed` | `update` completed with losses under `fail`. | No |

**`update` state outcomes:**

| Outcome | State | Exit |
|---|---|---|
| No losses | `resolved` | 0 |
| Losses + `drop` | `resolved` | 0 |
| Losses + `ask` | `awaiting_acknowledgment` | 6 |
| Losses + `fail` | `failed` | 4 |
| Preflight error | (none written) | 3 |
| Provider error | (none written) | 1 |

**When `acknowledge` is skipped:** runs with no losses, and runs with losses under `drop`. `materialize` proceeds directly from `resolved`. The integrity checks that `acknowledge` performs are also performed by `materialize`, so no gap exists.

**`materialize` never mutates the active seed.** `.index/` is updated only by `promote`, in a deliberate git-committed step.

**A failed boot does not mutate the previous iteration. A changed artifact or input produces a new candidate identity.**

#### 8.2.1 Caps

| Limit | Value |
|---|---|
| Boot iterations | 15 max |
| Wall-clock duration | 45 min max |
| API requests | Configurable budget, journaled persistently |
| Downloaded bytes | Configurable budget, journaled persistently |

Budgets are persisted in an append-only JSONL usage log at `[budget].usage_log_path` (default `<project_root>/var/usage.jsonl`). Each line records one API call or one download with a timestamp and, for downloads, a byte count. The log is read on every cap check; records older than `[budget].window_hours` (default 24) are ignored. A malformed line is logged and skipped; an unreadable log file aborts the run with exit 3 (fails closed per the original spec).

The resolver's per-run `provider_requests_made` counter and the usage log record the same facts. The log persists across runs and across restarts; the report counter does not.

### 8.3 Phase B — mod-set approval

On stabilization (after `iterate` succeeds):

- Publish run artifacts to `www/runs/<run-id>/`.
- Post Discord notification with link to `www/runs/<run-id>/resolution-report.toml` and approval buttons.
- Approval binds to candidate identity (slip body hash, runtime hash, input fingerprints, snapshot ID, validation report hash, **`resolution_report_sha256`**).
- User tests in-game; approves when workable.

### 8.4 Phase C — world migration

On mod-set approval:

- rsync prod world → DEV (see §10).
- Preserve snapshot; make writable clone (copy-on-write reflink or full copy; never a hardlink to source).
- Restart DEV against clone + candidate pack + real configs/kubejs.
- Health check; publish migration report.
- Discord: link + approval button.

### 8.5 Phase D — Velocity integration

After migration approval:

- Start DEV Velocity proxy against DEV backend.
- Verify forwarding, handshake, identity, connection.
- Publish report.

### 8.6 Phase E — promote

Promotion is a **staging** step, not a commit and not a publication. Human consent is required.

```
minecraft-upgrade promote <tag>
```

1. Load state and candidate slip from `/var/lib/minecraft-upgrade/candidates/<run-id>/`.
2. If `release_mode = "upgrade"`:
   - Reconcile `docker-compose.yml` to match slip `[runtime]`.
   - Update slip `[inputs.git].compose_tree`.
   - Recompute `content_sha256`.
3. Copy slip to `sync/packing_slips/<tag>.toml`.
4. Copy candidate `metadata/*.pw.toml` files to `sync/downloads/.index/`. Only files corresponding to `[[resolved]]` entries; do not overwrite unrelated `.index` entries.
5. `git add sync/packing_slips/<tag>.toml sync/downloads/.index/` and, if changed, `docker-compose.yml`.
6. Print exact commit/tag/push instructions:
   ```
   git commit -m "Release <tag>: <summary>"
   git tag -a <tag> -m "Shipping label"
   git push origin main --tags
   ```
7. STOP. Human runs the git commands and updates `stable`.

The `<tag>` argument becomes the slip's `release_id` and the publication identifier passed to `minecraft-upgrade publish <release-id>`. The three names (`<tag>`, slip `release_id`, `<release-id>` at publish) refer to the same value.

After tag push, human updates `stable`:

```
echo "v1.0.2.toml" > sync/packing_slips/stable
git add sync/packing_slips/stable
git commit -m "Mark v1.0.2 stable"
git push
```

The `stable` update is a separate, deliberate act. Publication is a separate, deliberate act.

### 8.7 `latest-slip` update

`latest-slip` is updated by DEV's resolver when a new candidate slip is selected. It is DEV-local, not Git-tracked, and does not require human approval. It affects only the `latest` integration container.

Ordinary KubeJS and config commits do not change `latest-slip`. They change the committed Git revision the `latest` container reads on deploy.

### 8.8 Known fixes

A known fix is a version-controlled, declarative rule, not an arbitrary script inferred from log text. Rules are scoped to a target runtime and, where relevant, a specific mod/project and version.

Allowed operations:

- Select a different exact version.
- Exclude an optional dependency.
- Apply a reviewed configuration patch.
- Modify a reviewed JVM parameter.

Arbitrary shell execution and automatic patching of unknown files are prohibited. Fixes are authored and reviewed by the toolkit maintainer. The log classifier may suggest applicable fixes; it may automatically apply only rules explicitly marked safe for automation. Every applied fix and its source revision is recorded in the candidate receipt.

### 8.9 Minimal boot-test configs

Minimal configs are fixed, version-controlled templates under a runtime-specific validation profile:

```
validation/profiles/<runtime-id>/minimal/
```

The candidate must resolve to a compatible template. If no template exists, the boot stage is blocked until a reviewed template is added. The template tree hash is included in the test evidence.

Minimal-config testing and testing with real configs/KubeJS/world data are separate stages.

---

## 9. In-House Mod Build Pipeline

### 9.1 Purpose

In-house mods are built by **cloning the mod's repository and running its own build script**. The upgrade toolchain does not own build logic, gradle configuration, toolchain images, or platform-specific knowledge.

The rationale is platform-agnosticism. A mod team chooses Forge, NeoForge, Fabric, Quilt, or a mix. Each mod carries its own build pipeline (gradle wrapper, `build.gradle`, toolchain pins, dependency repositories) in its own repository. The upgrade system's job is to **clone, check out, run, and stage**. That is all.

**What the tool owns:**

- The build specification file (`config.d/build/<name>.yaml`).
- Variable resolution against the manifest and CLI overrides.
- The clone workspace (`.build-workspace/<name>-<timestamp>/`).
- Artifact staging to the warehouse.

**What the mod's repository owns:**

- Build scripts (`build-local.sh`, `gradlew`, `build.gradle`, etc.).
- Toolchain pins (`gradle.properties`, `gradle/wrapper/gradle-wrapper.properties`).
- The `.pw.toml` output (the mod's build script produces it).
- Platform choice (Forge, NeoForge, Fabric, Quilt).

**What the tool explicitly does NOT do (removed from v1.3.1 §9):**

- Does not know or care what Minecraft loader the mod targets.
- Does not provide a toolchain image.
- Does not write `.pw.toml` for in-house mods (the mod's build script does).
- Does not add `[[jar]]` entries to the candidate slip directly (the operator marks the built artifact as in-house; see §9.7).
- Does not register a build record in a separate store (the slip's `build_id`, `source_repo`, and `source_commit` fields carry that; §5.2).

### 9.2 `config.d/build/<name>.yaml` schema

Each in-house mod has a build spec at `config.d/build/<name>.yaml`. The `name` (filename stem) is the mod's build-spec identifier.

```yaml
# config.d/build/createpollutioncleanup.yaml

# Optional. Variables available for ${VAR} substitution in every
# field below. Manifest values (${minecraft_version}, ${loader},
# ${loader_version}) are always available and can be shadowed here.
variables:
  mod_version: "0.1.2-alpha"
  java_home: "/usr/lib/jvm/jdk-17"

# Required. Any git URL: HTTPS, SSH, local path.
repository: "https://github.com/example/CPC.git"

# Required. Branch, tag, or commit SHA. Substituted before clone.
ref: "v${mod_version}"

# Required. Executable on the DEV host's PATH.
entrypoint: "bash"

# Required. Argument passed to entrypoint. Path relative to
# working_dir (unless absolute).
script: "build-local.sh"

# Optional. Additional CLI arguments appended to entrypoint's argv.
args: []

# Optional. Environment variables merged on top of the parent env.
env:
  JAVA_HOME: "${java_home}"

# Optional. Script timeout in seconds. Default 1800.
timeout_seconds: 1800

# Optional. Working directory (relative to clone root). Default ".".
working_dir: "."

# Optional. Shallow-clone depth. Default 0 (full clone). When > 0,
# ref MUST be a branch or tag name (not a commit SHA).
depth: 0

# Required. Each pattern must match exactly one file after the build.
# Destination is auto-detected:
#   *.pw.toml   -> sync/downloads/.index/
#   anything    -> sync/downloads/
# Set `dest:` to override (project-root-relative, or absolute).
artifacts:
  - path: "build/libs/createpollutioncleanup-*.jar"
  - path: "build/.index/createpollutioncleanup.pw.toml"
```

Field rules:

| Field | Required | Type | Notes |
|---|---|---|---|
| `variables` | No | mapping | String values; merged with defaults and overrides per §9.3. |
| `repository` | Yes | string | Non-empty. Any git URL form. |
| `ref` | Yes | string | Non-empty. Branch, tag, or commit SHA. |
| `entrypoint` | Yes | string | Non-empty. Must be on PATH or absolute. |
| `script` | Yes | string | Non-empty. Relative to `working_dir`. |
| `args` | No | list of strings | Empty by default. |
| `env` | No | mapping | String values. Merged over parent env. |
| `timeout_seconds` | No | float > 0 | Default 1800. |
| `working_dir` | No | string | Default `"."`. Must exist in the clone. |
| `depth` | No | int ≥ 0 | Default 0. When > 0, `--branch <ref>` is used at clone time, so `ref` must be a branch or tag. |
| `artifacts` | Yes | list | Non-empty. Each entry is a string (short form) or `{path, dest}` (long form). `dest` optional. |

An artifact entry's `path` is a glob evaluated by Python's `pathlib.Path.glob()`. It MUST match exactly one file after the build completes. Zero matches → failure with diagnostic naming the pattern. Multiple matches → failure naming every candidate. Paths containing `..` are rejected. Matched paths that resolve outside the clone directory (via symlink) are rejected.

### 9.3 Variable resolution

Precedence (lowest to highest):

1. **`defaults`** — the caller's contribution, typically manifest values:
   - `minecraft_version`, `loader`, `loader_version` from `sync/modpack.manifest.toml`
   - Empty if the manifest is missing or invalid (warning logged, not error)
2. **`spec.variables`** — the YAML spec's `variables:` block.
3. **`overrides`** — `--var KEY=VALUE` pairs from the CLI.

Substitution applies to: `repository`, `ref`, `script`, every element of `args`, every value in `env`, every artifact's `path` and `dest`, and `working_dir`. `entrypoint` is NOT substituted — it's expected to be a bare binary name on `PATH` or an absolute path, and shell-style substitution in an executable name is a footgun.

Only `${VAR}` where `VAR` matches `[A-Za-z_][A-Za-z0-9_]*` is substituted. Literal `{...}` (for example, `build-{date}.sh`) is left alone, so brace syntax in shell snippets or brace-expansion patterns survives.

An undefined `${VAR}` after merge → error with exit code 3. The error names the variable and the field where it appeared, so the operator sees exactly what to fix.

### 9.4 Runner behavior

The runner (`modpack_upgrade.build.runner`) implements the following steps. Every failure is reported as a `BuildOutcome` with `success=False`; the runner never raises on operational failure.

1. **Create clone directory.** `tempfile.mkdtemp(prefix="<name>-", dir=".build-workspace/")`. Unique per call; safe for concurrent builds (though not currently used concurrently).

2. **Clone.** `git clone [--depth N --branch REF] <repository> <clone_dir>`.
   - `--depth N` only when `spec.depth > 0`, and only with `--branch REF` (git rejects `--depth` with a commit SHA).
   - Timeout: 300 seconds.
   - Errors: non-zero exit names the stderr excerpt; timeout and missing-git-binary are named explicitly.

3. **Checkout.** `git checkout --detach <ref>` in the clone.
   - Skipped when `depth > 0` (the shallow clone already selected the ref).
   - Timeout: 60 seconds.

4. **Run script.** `[entrypoint, script, *args]` executed via `subprocess.run` with `cwd=<clone_dir>/<working_dir>` and `env = {**os.environ, **spec.env}`.
   - Timeout: `spec.timeout_seconds`.
   - `FileNotFoundError` → "entrypoint not found on PATH".
   - `TimeoutExpired` → "script timed out after N s".
   - Non-zero exit → "script exited with N", with stdout and stderr excerpts captured (last 2000 chars).

5. **Expand artifacts.** Each `path` glob is expanded under `clone_dir`. Rules per §9.2.
   - `..` in the pattern → rejected before matching.
   - Zero matches → named failure.
   - Multiple matches → named failure listing every candidate.
   - Matched path resolves outside the clone directory → named failure.

6. **Stage artifacts.** Each matched artifact is copied to its destination:
   - `*.pw.toml` → `sync/downloads/.index/<name>`
   - everything else → `sync/downloads/<name>`
   - `dest` override → `<project_root>/<dest>` (or absolute `dest` verbatim)

7. **Cleanup.** On success: `shutil.rmtree(clone_dir)`. On failure: clone is kept at `<clone_dir>` for operator inspection; the path is named in the failure outcome and in the CLI diagnostic.

### 9.5 Failure handling

A build failure produces:

- A `BuildOutcome` with `success=False`, `failure_message`, and both stdout/stderr excerpts.
- A CLI diagnostic via `minecraft-upgrade build-inhouse` that prints:
  - `status: failed (<message>)`
  - `clone kept for inspection: <path>`
  - `stderr (tail):` followed by up to the last 2000 characters of stderr.
- Process exit code 1.

A build-spec validation failure (missing required field, malformed YAML, invalid `timeout_seconds`, undefined `${VAR}`) produces exit code 3 with a targeted diagnostic.

A build that stages successfully does NOT write to `[in-house].filenames`. The operator must do so manually (see §9.7). The CLI prints a hint naming every staged jar so the operator can copy the filenames directly.

### 9.6 Security posture

The build script runs with the DEV operator's privileges in a fresh clone of the mod's repository. This is a **fully trusted execution model**: the mod team's code is trusted to build itself.

The upgrade tool's responsibility ends at "run the script the mod declared." It does not sandbox the build, does not restrict the script's filesystem access, does not restrict network access, and does not verify that the produced artifact is "safe." Those are the operator's responsibility before adding the mod to the manifest and running `build-inhouse`.

The `artifacts:` glob expansion rejects:

- Patterns containing `..` (path traversal in the pattern).
- Matches that resolve outside the clone directory (symlinks pointing elsewhere).

This is defense-in-depth against accidental misconfiguration, not a security boundary against a malicious build script. A malicious script can write anywhere the operator's account can write.

### 9.7 Deferred: `.pw.toml` in-house marker and `[in-house].filenames` retirement

The v1.3.1 §9.5 assumed Slice 13 would introduce a `.pw.toml` writer marker and retire the `[in-house].filenames` config list. **That did not happen in Slice 14.** The transitional mechanism remains in force.

**Current behavior (v1.4.0):**

- The mod's own build script produces its `.pw.toml`. The `[update.*]` block, if any, comes from the mod's script.
- The operator adds the built jar's filename to `config.d/upgrade.toml [in-house].filenames`.
- The resolver classifies matching filenames as `provider = "in-house"` regardless of the `.pw.toml`'s provider block.
- Omitting the jar from `[in-house].filenames` leaves the resolver to classify it from the `.pw.toml`. If the `.pw.toml` has no `[update.*]` block, the resolver classifies it as `"unknown"` and reports it in `[[lost]]` (§Spec A5.1). This is the operator error the hint in §9.4 step 4 is designed to prevent.

**Retirement (Slice 17+):**

The retirement requires:

1. `.pw.toml` writer-level marker for in-house identity (a new top-level field, spec-frozen).
2. `seed.py` and `inhouse.py` changes to read the marker.
3. Migration of existing production configs.

Until all three land, `[in-house].filenames` remains the authority. The v1.4.0 docs do not schedule the retirement.

---

## 10. World Snapshot and Migration

### 10.1 Direction

Prod → DEV. One-way.

### 10.2 Method

- Live `rsync --checksum` over SSH.
- No production stop.
- Per-file hash manifest.
- Snapshot preserved; writable clone created for testing.
- Clone MUST be a separate copy or copy-on-write reflink; never a hardlink to source files.

### 10.3 Snapshot identity

`snapshot_id` is the SHA-256 of a canonical file manifest for the entire selected world snapshot.

Each entry contains:

- Normalized relative POSIX path.
- File type.
- File size.
- File mode bits.
- Content SHA-256.

Modification times, owner IDs, and group IDs are not part of the identity. Symlinks and special filesystem objects are rejected rather than followed. Exclusions are explicitly configured; broad patterns MUST NOT silently omit potentially important world data.

### 10.4 Rationale

Stopping prod for snapshot costs ~45 minutes. Accepted risk: torn chunks. Mitigation: if migration test fails, re-run with `--stop-prod`.

### 10.5 Retention

Snapshot retained until migration approval. After approval, MAY be discarded per retention policy.

### 10.6 Configuration

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

### 10.7 World migration test requirement

A world migration test is mandatory when:

- The release declares `release_mode = "upgrade"`.
- The release changes mods that affect persistent world data, registries, dimensions, world generation, or saved mod content.
- The impact is unknown.

If impact is unknown, the test is required. Absence of `[tested]` in the slip is only permitted when the release demonstrably cannot affect persistent world data.

### 10.8 Pre-upgrade world backup

A separate, verified pre-upgrade world backup is required before any `release_mode = "upgrade"` production deployment. The backup is:

- Created on prod before stopping services.
- Stored at `/srv/minecraft/backups/world/<release>/`.
- Verified against a canonical file manifest (`backup-manifest.json` in the backup directory, containing `path`, `size_bytes`, `mode`, `sha256` per file).
- Retained per retention policy.

The DEV migration-test snapshot and the production rollback backup are distinct artifacts.

Backup creation is implemented by the production daemon (§12.4 stage 13) via `minecraft.deploy_pack.daemon.backup.create_backup`. Restoration is implemented by `minecraft.deploy_pack.daemon.recovery.restore_world`, invoked automatically when a deployment fails after the stop phase (§12.4, §12.10).

---

## 11. Discord Interface

### 11.1 Bot

The bot is a **standalone peer daemon**, not a submodule of `deploy_pack` or `modpack_upgrade`. It runs on DaemonCore, owns Discord communication end-to-end (inbound interactions and outbound notifications), and never mutates approval state directly — it forwards every slash-command action to the `minecraft-upgrade` CLI subprocess.

Implementation:

- Discord REST calls are made via a narrow `BotClient` protocol; the concrete transport is `DiscordRestClient` (Slice 15 T3b). No gateway/websocket connection is used; the bot operates entirely over Discord's HTTP APIs.
- Slash-command interactions are delivered as signed HTTP POSTs to the bot's interaction endpoint. The endpoint verifies Discord's Ed25519 request signature before any handler runs (§11.3).
- Outbound notifications from `deploy_pack` are delivered via a separate `POST /notify` route authenticated by a shared bearer token (§11.3).
- The bot invokes the `minecraft-upgrade` CLI as a subprocess for every command handler; it does not import the resolver, approval, or materialize logic directly.
- The bot is not the controller and has no direct filesystem access to mutate approvals.

#### 11.1.1 Interaction endpoint

`POST /interactions` receives Discord's application-command payloads. The endpoint enforces, in order:

1. Ed25519 signature verification (`X-Signature-Ed25519` + `X-Signature-Timestamp` headers, validated against the bot's configured `public_key`). Missing or invalid signature → HTTP 401 before any payload bytes are parsed.
2. JSON parse. Malformed body → HTTP 400.
3. PING/PONG handshake (`type: 1` → `type: 1`).
4. Application commands (`type: 2`) normalized into an `InteractionContext` and scheduled on a background task. The endpoint returns DEFERRED (`type: 5`) immediately, satisfying Discord's three-second initial-response window even when the CLI subprocess takes longer.
5. Component, modal, and autocomplete interactions → HTTP 400 until those surfaces land in a later slice.

#### 11.1.2 Notify endpoint

`POST /notify` receives outbound notifications from the `deploy_pack` process. It is registered only when the bot daemon is configured with a non-`None` `notify_bearer_token` (see §17.6). The endpoint enforces, in order:

1. Bearer-token authentication (`Authorization: Bearer <token>` header, constant-time comparison against the configured token). Missing or invalid token → HTTP 401.
2. JSON parse. Malformed body → HTTP 400.
3. Payload validation: `{"content": str, "kind": str}` with both non-empty → HTTP 400 on violation.
4. Delivery to the bot's configured operator channel via `BotClient.send`. Delivery failure → HTTP 502 (so the caller can report failure without falling back to the webhook).
5. Success → HTTP 200 with `{"status": "ok"}`.

Both endpoints are served by the same `DiscordBotPlugin` and share the same `BotClient` for outbound delivery, so the delivery path is unified.

### 11.2 Commands

Discord slash commands (delivered via `POST /interactions`, §11.1.1):

| Command | Purpose |
|---|---|
| `/upgrade status` | Current run state |
| `/upgrade build-inhouse <name>` | Trigger in-house build from `config.d/build/<name>.yaml` |
| `/upgrade migrate-world [survival\|creative]` | Trigger world snapshot |
| `/upgrade approve` | Approve current prompt |
| `/upgrade reject [reason]` | Reject current prompt |

The `POST /notify` route (§11.1.2) is **not a Discord command**. It is a service-to-service endpoint that `deploy_pack` calls to deliver notifications through the bot's outbound Discord path. Its existence does not add any user-facing slash commands.

### 11.3 Rules

- Allowlist users/roles for slash commands. Users whose ID or role is not allowlisted receive a rejection reply and no CLI invocation.
- Idempotent button clicks; interaction ID persisted with a unique constraint (§11.4).
- Approval bound to candidate/snapshot identity (§11.4).
- Approval state persisted.
- No arbitrary shell commands.
- Bot token outside git and out of logs.
- Bot MUST NOT be the controller.
- Approval actions use Discord's signed HTTP interaction mechanism. The controller verifies Discord's Ed25519 request signature, validates guild/channel/user identity and authorized role, and checks the pending approval against stored candidate state.

#### 11.3.1 Auth model

The two bot routes use **different auth mechanisms**, by design:

| Route | Auth | Provided by |
|---|---|---|
| `POST /interactions` | `X-Signature-Ed25519` + `X-Signature-Timestamp` headers, verified against the bot's configured `public_key` | Discord |
| `POST /notify` | `Authorization: Bearer <token>`, constant-time comparison against the bot's configured `notify_bearer_token` | `deploy_pack` |

The `/interactions` signature is the same Ed25519 mechanism Discord requires for every registered application. The `/notify` bearer token is the same `.env` variable the bot daemon's `[daemon].notify_bearer_token_key` names — one secret, two consumers (§17.6). Neither route shares credentials with the other.

The `/notify` route is deliberately not reachable when the bot is built without a notify token: the plugin registers it only when the token is configured, so a bot daemon that does not participate in the notify path has no authenticated notify surface at all.

### 11.4 Approval replay and expiry

Each approval action is bound to:

- A unique Discord interaction ID.
- A one-time approval nonce.
- An explicit approval scope.
- Candidate content hash.
- Runtime hash.
- Server and client input fingerprints.
- Snapshot ID, where applicable.
- Validation report hash.
- **`resolution_report_sha256`**.

The interaction ID is persisted with a unique constraint. A duplicate returns its previous result. Approval expires after seven days if the release has not been promoted. Changes to bound candidate inputs, runtime identity, migration snapshot, or required validation evidence invalidate approval immediately.

### 11.5 Message format

Notifications MUST include a link to `www/runs/<run-id>/` and to the specific `www/runs/<run-id>/resolution-report.toml`. Logs and diffs are published as artifacts, not embedded.

---

## 12. Production Daemon

### 12.1 Role

Privileged production control plane. Owns verification, staging, deployment, health checks, and controlled recovery.

**The daemon verifies the release and the transfer. It does not resolve replacement mod versions. It does not contact upstream mod providers. It does not clone in-house mod repositories.**

### 12.2 Exposure

- Tailscale-only HTTP endpoint.
- Authenticated, replay-protected, idempotent.
- Only one production deployment may run at a time (enforced by the deployment lock, §12.6).
- A durable operation journal records each stage.
- A rollback journal preserves prior managed file state.

### 12.3 Inputs

- Immutable release ID (git tag) or pointer name (`stable`)
- Expected git commit SHA
- Packing-slip content hash

MUST refuse: branch names, arbitrary commands, ambiguous pokes.

### 12.4 Lifecycle — pull-based

The publication phase (steps 1–10) and deployment phase (steps 11–19) are distinct operations with separate authorization. The publication phase runs on prod, but publishes a release that was prepared on DEV. The deployment phase runs on prod and is triggered by an operator hitting `/deploy`.

**Publication phase (PROD, triggered by `minecraft-upgrade publish` on DEV then pull on PROD):**

```
P1.  DEV: operator runs `minecraft-upgrade publish <release-id>`.
P2.  DEV: verifies preconditions (§6.3).
P3.  DEV: writes the immutable bundle.
P4.  DEV: signs the bundle manifest and records the registry entry.
P5.  PROD: pulls the release bundle from DEV via rsync over SSH into
     staging/<release>.partial/.
P6.  PROD: verifies the bundle manifest and every file hash.
P7.  PROD: atomically renames staging/<release>.partial/ -> releases/<release>/.
P8.  PROD: verifies slip.runtime against effective Compose (per release_mode).
P9.  PROD: verifies slip [inputs.git] tree hashes against the working tree
     (release mode only).
P10. PROD: records publication success in the deployment ledger.
```

Steps P1–P4 are implemented by `minecraft-upgrade publish` (§15.1.8). Steps P5–P10 are implemented by `daemon.pull.pull_release` and the precondition checks in `daemon.deploy._run_stages`, exercised by `/deploy`.

**Deployment phase (PROD, triggered by `/deploy`):**

```
11.  Acquire the deployment lock.
12.  Pull the release bundle (if not already present).
13.  Create pre-upgrade world backup (for release_mode="upgrade" only).
14.  Load the release slip.
15.  Stop services consuming the shared mod directory.
16.  Ship artifacts via the shipping transaction (§13.2).
17.  Start services.
18.  Run post-start health checks.
19.  On success: journal COMPLETE, release the lock, record the active release.
     On failure after the stop phase: attempt recovery (§12.10), attempt
     to start services, journal the terminal stage, release the lock.
```

Steps 11–19 are implemented by `daemon.deploy.deploy` and executed as nine functions of shape `_stage_<name>`. Every stage transition is journaled with the stage name, the run ID, the release ID, and a free-form detail string.

**Refusal before start.** Before acquiring the lock, the daemon reads the operation journal (§12.7). If any prior run's last-recorded stage is not terminal, the deployment is refused with an `ActivationFailedError` (HTTP 500 / code 21). The operator must resolve the unresolved run before any new deployment can proceed. This is the "halt and require operator intervention" contract.

### 12.5 Pull transport

- rsync over SSH.
- Prod initiates. DEV exposes the publication directory as a read-only rsync target.
- DEV never initiates a transfer to prod.
- Auth via SSH key restricted to a forced command running rsync in read-only mode, rooted at the publication directory.
- Interactive shell, PTY, port forwarding, agent forwarding, and arbitrary commands are disabled.
- DEV's raw `sync/downloads/` warehouse is not exposed to that account.
- Prod cannot upload, delete, rename, or modify publication files.

The prod-side implementation is `minecraft.deploy_pack.daemon.pull.pull_release`. It uses an injectable `RsyncTransport` callable for the subprocess invocation. Verification against `bundle-manifest.json` covers: every listed file exists with matching size and SHA-256; no extra files exist except `bundle-manifest.json` and `bundle-manifest.sig`, which are exempt (they cannot honestly appear in the manifest).

### 12.6 Deployment lock

- A single lock file enforces one concurrent deployment.
- Acquisition is atomic via `mkstemp`-then-`os.link`. Two concurrent attempts (from separate threads or processes) cannot both succeed; exactly one wins.
- The lock is acquired before stage 11 and released after stage 19 (in a `finally` block, so it is released on any exit path).
- A stale lock (mtime older than `stale_after_seconds`, default 3600) requires operator confirmation before override. The daemon refuses to auto-release stale locks.
- A malformed lock file is treated as stale.
- Concurrent deployment requests are rejected with `DeploymentLockHeldError` (HTTP 409 / code 25).

### 12.7 Operation journal

- Every stage transition is recorded in a durable, append-only JSONL journal at `[daemon].journal_path`.
- Each line is a `JournalRecord`: `{timestamp_utc, run_id, release_id, stage, detail}`.
- Every write is followed by `os.fsync`. The journal is written a dozen times per deployment, not thousands; durability is the point.
- Malformed lines are logged as warnings and skipped (defensive against partial writes after a crash). Blank lines are ignored.
- On daemon startup or before any new deployment, `unresolved_runs()` returns the run IDs whose last-recorded stage is not terminal (COMPLETE, FAILED, RECOVERY_COMPLETED, RECOVERY_FAILED).
- An unresolved run blocks all new deployments (see §12.4 refusal-before-start).

Stage vocabulary (wire format; see `minecraft.deploy_pack.daemon.journal.Stage`):

```
pull_started          pull_completed
backup_started        backup_completed
lock_acquired
ship_started          ship_completed          ship_failed
services_stopped      services_started
health_check_started  health_check_passed     health_check_failed
recovery_started      recovery_completed      recovery_failed
complete              failed
```

An interrupted or ambiguous recovery MUST halt safely; it MUST NOT guess.

### 12.8 Runtime staging

- Runtime payload is included in the release bundle.
- Prod pre-stages the runtime before stopping services.
- Production startup MUST use the staged payload.
- Production startup MUST NOT silently retrieve new server or loader files.
- The implementation MUST adapt the existing container startup mechanism if necessary to satisfy this requirement.

### 12.9 Health checks

The daemon performs health checks for each targeted service via `minecraft.deploy_pack.daemon.health.run_health_checks`. Each container's checks are:

1. Container is running.
2. Startup-completion log sentinel observed within `timeout_seconds` (default 600). The sentinel is `"Done ("` — the vanilla Minecraft convention.
3. No unexpected restart during the health window (baseline captured at the start of the health check).
4. RCON `list` succeeds three times, 15 seconds apart. A response on the first attempt that returns `(False, ...)` is retried at the poll interval until the overall timeout is reached.
5. No configured fatal log marker appears in the recent logs.

Every check runs against an injectable `HealthProbe`. Fatal markers default to `("---- Minecraft Crash Report ----", "Exception in thread", "[FATAL]", "UnsupportedClassVersionError")`. Additional fatal markers can be configured per spec via `HealthCheckSpec.fatal_markers`.

The startup-sentinel and RCON-list waits both check for fatal markers on every poll, so a container that crashes before reaching the sentinel or during the RCON window is reported with the specific marker rather than a generic timeout. This is the diagnosis operators need; a container that fails with "sentinel never appeared" without the crash signature is worse than useless.

Failure of any check produces a `ContainerHealthResult` with `passed=False` and one or more failure reasons. Aggregate result reports the full set so a post-mortem can see every failure at once.

RCON is enabled only on the internal Docker network; it is not published to the host or public network. The health probe consumes pre-resolved `RconTransport` objects produced by `docker_runtime.select_rcon_transport`, exactly as the interactive CLI does.

### 12.10 Recovery behavior

If a deployment fails **after the stop phase** (stages 15–18: ship, start, health), the daemon attempts recovery:

1. **File restore (if inputs exist).** If a prior active release is recorded in `[daemon].state_dir/active-release`, restore the prior mods via `shipping.ship` against the prior bundle. If this was an `upgrade` deployment, restore the world from `backups/world/<release>/` after verifying the backup against its manifest.

   The two restores are attempted independently: a failure in the world restore does not prevent the mods restore from running, and vice versa. The operator sees the full set of failures.

2. **Start services.** Every container the deployment stopped is started. A failure to start any container escalates to a `RecoveryFailedError` (HTTP 500 / code 24), regardless of whether the file restore succeeded.

3. **Journal terminal stage.** Recovery journals `RECOVERY_STARTED`, then `RECOVERY_COMPLETED` or `RECOVERY_FAILED`. The overall deployment journals `FAILED` with the original failing stage named in the detail.

If the deployment fails **before the stop phase** (stages 11–14: lock, pull, backup, slip load), no files were touched, so no file recovery is attempted. Services are not started (they were never stopped). The failure is journaled as `FAILED`, the lock is released, and the outcome carries the failing stage and its error code.

For a same-runtime update:

- Restore the previous approved artifact/config/KubeJS state.
- Rerun health checks (deferred; the current implementation returns the failure without re-checking).

For a runtime/world-format upgrade:

- Restore the pre-upgrade world backup.
- Restore the previous runtime.
- Restore the previous artifacts.
- Rerun health checks (deferred).

If the backup is missing, corrupt, or cannot be proven compatible with the rollback plan, the daemon halts (recovery is refused) and requires operator intervention. The operator must resolve the ambiguity before any new deployment can proceed (which the journal's unresolved-run check enforces).

The daemon records the failure and recovery outcome in the journal and in the audit log. The failure notification to the configured Discord operations channel is deferred to Slice 15.

### 12.11 Rollback

- Retain the previous known-good release bundle.
- Retain the pre-upgrade world backup.
- Restoring the git tag alone is NOT sufficient rollback if the world format changed.
- Rollback restores the compatible combination of world, runtime, artifacts, and inputs.

### 12.12 Daemon API

The HTTP admin plugin (`minecraft.deploy_pack.daemon.plugins.http.HttpAdminPlugin`) exposes:

| Method | Path | Purpose | Auth | Idempotency |
|---|---|---|---|---|
| GET | `/status` | Daemon status (health + Minecraft-specific fields). | Yes | — |
| POST | `/verify` | Verify a release without side effects. | Yes | `Idempotency-Key` required |
| POST | `/publish` | Verify a release and record a publication receipt. | Yes | `Idempotency-Key` required |
| POST | `/deploy` | Verify then dispatch to the deployment orchestrator. | Yes | `Idempotency-Key` required |

Authentication is `CombinedAuth(TailscaleAuth, BearerAuth)`. The Tailscale layer restricts source IPs to the configured CIDRs and records the `Tailscale-User-Login` header principal when present. The bearer layer requires an `Authorization: Bearer <token>` header whose token matches the env var named by `[daemon].bearer_token_env`. Both layers must pass.

Error responses use `ErrorResponse { code, message, detail }` with HTTP status derived from the error class (§16.3). All `MinecraftDaemonError` subclasses are caught by the plugin's exception handler; all other exceptions fall through to FastAPI's default 500 handling (intentional — framework errors should surface with their real messages, not be reformatted as 409/26).

`Idempotency-Key` is required on every POST. A reused key with a different request body raises `IdempotencyConflictError` (HTTP 409 / code 27). A reused key with the same body returns the cached response.

---

## 13. Shipping System

### 13.1 Purpose

Turn a release bundle (or DEV candidate) into the target `mods/` folder.

### 13.2 Algorithm

```
1. Load slip.
2. For each [[jar]] with side ∈ {server, both}:
   - verify artifact exists with matching sha256
   - add to source set
3. Diff source set vs. target mods/.
4. Preflight:
   - enabled JAR not in slip and not protected → block
   - stale JAR tracked by previous release and not in new release → remove
   - .jar.disabled → preserve and report
   - nested directories, symlinks, special objects → never traverse
   - unexpected non-JAR files → report; block if they could affect loading
5. Pre-stage all changes.
6. Stop services consuming the shared mod directory.
7. Apply changes atomically per file with a rollback journal.
8. Verify resulting inventory against the slip plus retained protected files.
9. Start services.
10. Log shipment.
```

Steps 3–8 are implemented as a logically atomic transaction by `modpack_upgrade.shipping.ship`. The transaction:

- Pre-stages every new and changed file and verifies each staged file's SHA-256 against its source.
- Backs up every file about to be removed or replaced.
- Records every operation to an append-only JSONL journal.
- Applies operations atomically per file.
- Verifies the resulting inventory against the desired set plus any protected files retained at their pre-transaction hashes.
- On failure, walks the journal backwards and restores the pre-transaction inventory.

The transaction does not stop or start services; the caller wraps it in lifecycle phases (either `deploy_pack`'s own hooks, or the daemon's `ServiceControl`, or the daemon's `_run_stages`).

### 13.3 Same code, different targets

| Host | Mode | Target |
|---|---|---|
| DEV (resolution loop) | candidate | `~/minecraft-dev/mods/` |
| DEV (`latest`) | integration | `~/minecraft-dev/latest/mods/` |
| Prod (`survival`, `creative`) | release | `<instance_root>/../mods/` |

### 13.4 Rules

- Shipping MUST be atomic per file.
- Shipping MUST verify SHA-256 before placement.
- Shipping MUST honor slip-declared protection patterns.
- Shipping MUST NOT read `side_overrides.toml` in slip mode.
- Shipping MUST NOT intentionally leave a mixed release running.
- A failed step before activation MUST restore the prior managed file set before restarting services.

### 13.5 Config and KubeJS semantics

**`config_mode = "merge"`:**

- Source file replaces target file with the same relative path.
- Source-only files are added.
- Target-only files are retained unless listed in `config_deletions`.
- File removals are explicit through `config_deletions`.
- Protected files are not modified or removed. If a release attempts to alter a protected path, preflight fails.

**`kubejs_mode = "delete"`:**

- Stop the target server.
- Remove the entire target `kubejs/` directory.
- Recreate it.
- Deploy the captured KubeJS source tree.
- Files not present in the captured source are removed.
- Protected paths cannot coexist with this mode; overlapping protection is a preflight error.

**`protect_patterns`:**

The matching primitive is Python's standard-library `fnmatch.fnmatch` (POSIX glob). Not Git wildmatch. `*` matches any character sequence (including `/`), `?` matches exactly one character, `[seq]` matches any character in `seq`, `[!seq]` matches any character not in `seq`. Every other character matches literally.

`deploy_pack` applies two matching scopes:

- **Full path** — the normalized relative POSIX path is matched against each pattern.
- **Component** — every `/`-separated path component is matched against each pattern.

The `deploy_pack` pattern loader strips a single trailing `/` before matching. `modpack_upgrade.shipping` does not strip trailing slashes and does not perform component matching; it matches against the flat `.jar` filename only, because shipping operates on a flat directory.

When the same pattern list protects both `mods/` jars and an instance's config tree, shipping sees only the flat filename and the config-tree copy sees the nested relative path plus its components. A pattern that is meaningful in only one context (e.g. `config/*`) simply does not match in the other; it is not an error.

Protection governs removal only. A protected file that also appears in the source set is still overwritten with the source's content.

### 13.6 Provider independence guarantee

Prod MUST function without:

- Provider API credentials.
- Network access to Modrinth, CurseForge, any in-house mod repository, or any other mod provider.
- Any configuration referencing provider endpoints for fetching.

The only upstream mod-artifact source for prod is DEV's release bundle.

---

## 14. `deploy_pack` Integration

### 14.1 Modes

| Mode | Invocation | Used by |
|---|---|---|
| Legacy | `deploy-pack` | Transitional only |
| Release | `deploy-pack --release <tag>` or `--release-pointer stable` | `survival`, `creative` |
| Integration | `deploy-pack --integration --release-pointer latest-slip` | `latest` |
| Dev-candidate | Internal; not a `deploy_pack` mode | DEV resolution loop |

### 14.2 What the slip governs

- Which artifacts deploy.
- Which sides.
- Which configs/kubejs (via tree hashes for release mode; informational for integration mode).
- Which runtime was tested.
- Which instances are targeted.
- Which protection patterns apply.

### 14.3 What the slip does NOT govern

- Container names (from `deploy_pack.toml` + Compose).
- Service names (from Compose).
- Instance root paths (from Compose bind mounts).
- Docker orchestration settings.
- `www_dir`, `sync_root`, `modpack_dir`.
- RCON transport selection.
- Notification configuration.

### 14.4 Slip-mode behavior by module

| Module | Behavior in slip mode |
|---|---|
| `config_model.py` | Unchanged; still loads `deploy_pack.toml` and resolves instances from Compose. Additionally attaches `SlipContext`. |
| Slip loading | Reads slip; reconciles `[runtime]` against effective Compose; reconciles `[inputs.git]` against working tree (release mode only); provides `SlipContext` to the pipeline. |
| `deps.py` | NOT called in slip mode. The slip-derived source set is the authority (§5.8). |
| `overrides.py` | NOT called in slip mode. |
| `preflight/changes.py` | Compares slip-declared state vs. current target state. |
| `scope_server.py` | Uses `SlipContext` and the shipping transaction (§13.2) to place jars; deploys configs/kubejs from `SlipContext`'s source roots. |
| `scope_client.py` | Builds from slip's `[client_distribution]` and the client-distribution manifest. |
| `scope_resource_pack.py` | Uses slip's `[[resource_pack]]`. |
| `prompt_ui.py` | Edits candidate slip review file. |

### 14.5 Pointer resolution

A pointer is either `stable`, `latest-slip`, or a direct tag.

```
1. If the pointer names a recognized alias file:
   - read the file (single line)
   - load the referenced slip
2. Else if the pointer names a valid release tag:
   - load sync/packing_slips/<tag>.toml directly
3. If both interpretations are possible: alias file wins.
4. Reject: malformed aliases, unknown releases, invalid tags, path traversal.
5. Production aliases resolve only to published releases. `stable` cannot
   refer to `latest-slip`, an RC, or an unapproved candidate.
```

The pointer is a logical release reference, not a filesystem path.

### 14.6 Reconciliation requirements

**Release mode:**

1. Load slip.
2. Load `deploy_pack.toml`; resolve instances via Compose.
3. `release_mode = "update"`: verify slip `[runtime]` matches effective Compose.
4. `release_mode = "upgrade"`: verify effective Compose matches slip `[runtime]`.
5. Verify `compose_tree`, `config_tree`, `kubejs_tree` against working tree.
6. Verify `[instances.*]` covers deployment targets.
7. Any mismatch → abort.

**Integration mode:**

1. Load candidate slip.
2. Load `deploy_pack.toml`; resolve instances via Compose.
3. `release_mode = "update"`: verify slip `[runtime]` matches effective Compose.
4. `release_mode = "upgrade"`: verify effective Compose matches slip `[runtime]`.
5. Do NOT verify `config_tree` or `kubejs_tree`. Configs/kubejs come from one clean committed HEAD revision captured at `SlipContext` construction.
6. Verify `[instances.*]` covers deployment targets.
7. Any mismatch on enforced fields → abort.

### 14.7 Working-tree verification

Release mode pre-deploy:

```
git status --porcelain sync/config sync/kubejs docker-compose.yml config.d/deploy_pack.toml
```

Non-empty output → abort.

Integration mode: dirty deployment inputs are rejected. Moving committed HEAD is supported; deploying uncommitted changes is not.

### 14.8 Legacy-mode behavior

MUST remain unchanged during migration period. `.deploy_protect`, `side_overrides.toml`, and `deps.resolve_mod_sources` MUST continue to function.

### 14.9 Legacy path retirement

Removed only after:

- One packing-slip release deploys successfully on prod.
- Post-deploy verification passes.
- Rollback procedure has been exercised.

---

## 15. CLI Contract

### 15.1 `minecraft-upgrade`

Entry point: `minecraft-upgrade`.

| Command | Responsibility |
|---|---|
| `manifest check` | Validate manifest syntax/schema/IDs |
| `runtime inspect` | Report effective runtime from Compose |
| `status` | Summarize gates and state |
| `verify --tag <tag>` | Read-only slip verification |
| `update --release-mode <mode>` | Resolve. Write report + state. |
| `acknowledge <run-id>` | Verify + record loss acceptance |
| `materialize --run-id <run-id>` | Verify + download + seal slip |
| `iterate --run-id <run-id>` | Boot finalized candidate |
| `build-inhouse <name> [--var KEY=VALUE ...]` | Clone + run + stage an in-house build |
| `build-client` | Produce candidate client ZIP |
| `snapshot-world [survival\|creative]` | rsync prod → DEV |
| `validate migration` | Test snapshot against candidate |
| `validate velocity` | Isolated proxy integration |
| `publish <release-id>` | Create immutable release bundle. Explicit operator command. |
| `promote <tag>` | Write slip, stage in git, print instructions. Does not commit/tag/push. |

#### 15.1.1 `update` contract

```
minecraft-upgrade update --release-mode {update|upgrade} [--config-dir PATH]
```

Behavior:

1. Read manifest; validate; if `--release-mode update`, verify manifest matches Compose.
2. Read `config.d/upgrade.toml [in-house].filenames`.
3. Read seed from `.index/*.pw.toml`. Verify each jar exists. Verify each `.pw.toml` side is valid (in `{client, server, both, unknown}` or absent; malformed → exit 3).
4. Read `side_overrides.toml` (missing → exit 3).
5. Compute `manifest_sha256`.
6. Compute `resolution_inputs_sha256` (§5.4.1).
7. For each seed entry:
   - In-house: skip provider query; preserve current version; verify `jar_sha256` matches warehouse bytes.
   - External: query provider for compatible versions; select per `selection`; if none → `[[lost]]` reason="no_compatible_version".
8. Compute provisional sides (override > provider > current `.pw.toml`).
9. Walk dependency closure; cascade-drop dependents.
10. Detect cycles → exit 3.
11. Detect conflicts.
12. Write `www/runs/<run-id>/resolution-report.toml`.
13. Write `/var/lib/minecraft-upgrade/runs/<run-id>/state.toml` with state per policy and `effective_compose_sha256` if `--release-mode update`.
14. Print next command. Exit per §16.2.

Exit codes: 0, 6, 4, 3, 1.

#### 15.1.2 `acknowledge` contract

```
minecraft-upgrade acknowledge <run-id> [--accept-losses] [--operator <id>]
```

1. Load `state.toml`. Verify `status == "awaiting_acknowledgment"`.
2. Run integrity checks (§15.1.3).
3. Present losses. Require explicit acknowledgment (or `--accept-losses`). Non-interactive without `--accept-losses` → exit 6.
4. Write `acknowledgment.json`.
5. Update `state.status = "acknowledged"`.
6. Print `next: materialize --run-id <id>`. Exit 0.

Exit codes: 0, 3 (drift/tamper), 6 (unacknowledged).

#### 15.1.3 Shared integrity checks

Both `acknowledge` and `materialize` perform, in order:

1. Load `state.toml`.
2. Load report from `www/runs/<run-id>/resolution-report.toml`.
3. SHA-256 of report vs. `state.report_sha256`. Mismatch → exit 3.
4. Recompute `resolution_inputs_sha256`. Mismatch → exit 3.
5. If `release_mode == "update"`: Compose SHA-256 vs. `state.effective_compose_sha256`. Mismatch → exit 3.

#### 15.1.4 `materialize` contract

```
minecraft-upgrade materialize --run-id <run-id>
```

1. Load `state.toml`. Verify `status ∈ {resolved, acknowledged}`:
   - `awaiting_acknowledgment` → exit 6
   - `failed` → exit 3
   - `materialized` → exit 0 (already done), print next
2. Run integrity checks (§15.1.3).
3. Prepare staging dir.
4. For each `[[resolved]]`:
   - In-house: source from warehouse; verify `jar_sha256` against `[[seed]]`; verify `expected_hashes` if present.
   - External: download to staging; verify every `expected_hashes[]`; compute SHA-256 for slip.
5. For each resolved artifact:
   - Copy from staging to `sync/downloads/<filename>`. Collision handling per §15.1.5.
   - Resolve final side via Side_Determination chain. If `unknown`, **record and continue** (do not fail yet).
5a. After all `[[resolved]]` entries are processed: if any final side is `unknown`, exit 3 with the full list of unresolved mods, and suggest `side_overrides.toml` entries. This preserves the diagnostic that `materialize`'s per-entry expected_hashes verification provides: the operator sees every mod needing an override in one attempt, not one per re-run.
6. Write `.pw.toml` to `candidates/<run-id>/metadata/<filename>.pw.toml`. Never to `.index/`.
7. Assemble slip with final sides + verified SHA-256.
8. Compute `content_sha256`. Seal.
9. Write to `candidates/<run-id>/packing_slip.toml`.
10. Update `state.status = "materialized"`. Clean staging.
11. Print `next: iterate --run-id <id>`. Exit 0.

Exit codes: 0, 1 (network), 3 (hash/side/drift/tamper), 6 (unacknowledged).

#### 15.1.5 Filename collision handling

When a resolved artifact's filename matches an existing warehouse artifact:

- Same SHA-256 → reuse.
- Different SHA-256 → exit 3 (`filename collision`). Do not overwrite.

#### 15.1.6 `iterate` contract

```
minecraft-upgrade iterate --run-id <run-id>
```

1. Load `state.toml`. Verify `status == "materialized"`.
2. Load slip from `candidates/<run-id>/packing_slip.toml`.
3. Enter boot loop (§8.2.1 caps).
4. On classifier ambiguity or cap exhaustion: update `state.status` to `failed` (leaving `state.reason = "ambiguous_failure"` or `"cap_exhausted"` for operator diagnosis), leave the run directory in place under `www/runs/<run-id>/`, and post the Discord notification with the run link per §11.5. The operator inspects and either extends caps manually or abandons the candidate.

#### 15.1.7 `promote` contract

```
minecraft-upgrade promote <tag>
```

1. Load state and candidate slip from `candidates/<run-id>/`.
2. If `release_mode = "upgrade"`: reconcile `docker-compose.yml`, update slip `compose_tree`, recompute `content_sha256`.
3. Copy slip to `sync/packing_slips/<tag>.toml`.
4. Copy candidate `metadata/*.pw.toml` to `sync/downloads/.index/`.
5. `git add sync/packing_slips/<tag>.toml sync/downloads/.index/` and, if changed, `docker-compose.yml`.
6. Print exact commit/tag/push instructions.
7. STOP.

#### 15.1.8 `build-inhouse` contract

```
minecraft-upgrade build-inhouse <name> [--var KEY=VALUE ...] [--config-dir PATH]
```

1. Read `config.d/build/<name>.yaml`. Validate against §9.2.
2. Read the manifest for default variables (`minecraft_version`, `loader`, `loader_version`). Missing or invalid manifest is a warning; only variables that appear in the spec need to resolve.
3. Merge variables: defaults < YAML `variables:` < `--var` overrides. Substitute every `${VAR}` in the spec's string fields (§9.3). Undefined `${VAR}` → exit 3.
4. Clone the repository into `.build-workspace/<name>-<timestamp>/` at `ref`. Missing repository, missing ref, or git failure → exit 1.
5. Run `[entrypoint, script, *args]` in `working_dir` with merged `env`. Timeout `timeout_seconds`. Non-zero exit → exit 1 with stdout/stderr tails.
6. Expand every artifact pattern under the clone root. Zero or multiple matches → exit 1 naming candidates. Traversal rejected → exit 1.
7. Copy matched artifacts to destinations: `.pw.toml` → `sync/downloads/.index/`, other → `sync/downloads/`, `dest:` override → project-root-relative.
8. On success: clone removed. On failure: clone kept at `<clone_path>` for inspection; path named in the diagnostic.
9. Print a hint naming every staged jar so the operator can add it to `[in-house].filenames`.

Exit codes: 0 (success), 1 (build failure), 3 (spec/validation error).

A second positional argument (the old `build-inhouse <mod> <revision>` form) is accepted but ignored with a warning; the ref comes from the spec's `ref:` field. This tolerance exists so operators with muscle memory from the pre-Slice-14 signature don't lose their build invocation, but the correct usage is spec-file-driven.

### 15.2 `deploy-pack`

| Command | Responsibility |
|---|---|
| `deploy-pack` | Legacy v2 path |
| `deploy-pack --release <tag>` | Release mode, direct tag |
| `deploy-pack --release-pointer <name>` | Release mode, pointer file or tag |
| `deploy-pack --integration --release-pointer latest-slip` | Integration mode |

### 15.3 Safety

- Default operations MUST be non-production and non-destructive.
- Any command starting Docker MUST clearly identify the candidate.
- Candidate commands MUST NOT send Discord notifications or publish to prod webroot.
- No validation command may stop, restart, or exec into a production container.

---

## 16. Exit Codes

### 16.1 `deploy-pack`

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime failure |
| 2 | CLI usage error |
| 3 | Config/preflight failure |

### 16.2 `minecraft-upgrade`

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational failure (provider API, network, download, build failure) |
| 2 | CLI usage error |
| 3 | Config/schema/integrity/preflight failure |
| 4 | Resolution failure (`no_version = "fail"` with losses) |
| 5 | Validation gate failed |
| 6 | Approval blocked (`ask` with losses; unacknowledged pause) |
| 7 | Publication failure |

**Clarifications:**

- **1**: provider API errors (`update`), download errors (`materialize`), in-house build failures (`build-inhouse`).
- **3**: manifest invalid, seed jar missing, malformed `.pw.toml` side, `side_overrides.toml` missing, dependency cycle, `materialize` hash mismatch, `materialize` side unresolved, `acknowledge`/`materialize` drift/tamper, build spec invalid, undefined build variable.
- **4**: `update` with `no_version = "fail"` and losses.
- **6**: `update` with `no_version = "ask"` and losses; `acknowledge` or `materialize` on unacknowledged run.

`update` state outcomes map to codes per §8.2.

### 16.3 Prod daemon

| Code | Meaning | HTTP |
|---|---|---|
| 20 | Release verification blocked | 409 |
| 21 | Activation failed | 500 |
| 22 | Deploy command failed | 500 |
| 23 | Post-deploy verification failed | 500 |
| 24 | Recovery/rollback failed | 500 |
| 25 | Concurrent deployment lock held | 409 |
| 26 | Registry chain verification failed | 409 |
| 27 | Idempotency conflict | 409 |
| 28 | Authentication failed | 401 |
| 29 | Authenticated but not authorized | 403 |
| 30 | Endpoint present but not implemented | 501 |

Error-code-to-HTTP mapping is fixed at the class level in `minecraft.deploy_pack.daemon.errors`. The plugin's exception handler serves the mapped HTTP status for every `MinecraftDaemonError` subclass. No other exception class is caught there; unexpected errors fall through to FastAPI's default 500 handling.

---

## 17. Configuration

### 17.1 `config.d/upgrade.toml`

```toml
[resolver]
release_policy = "release_then_beta"
allow_alpha = false

[iteration]
max_boot_iterations = 15
max_wall_clock_seconds = 2700

[budget]
# Optional. Default: <project_root>/var/usage.jsonl. Absolute paths are
# used verbatim; relative paths resolve against the project root.
usage_log_path = "var/usage.jsonl"

# Rolling window for cap enforcement. Default 24.
usage_window_hours = 24

# Optional. When both are unset, no usage log is written and no caps
# are enforced.
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

[warehouse_transport]
method = "rsync-ssh"
ssh_key = "~/.ssh/id_deploy_warehouse_ro"
publication_root = "/srv/minecraft-dev/www/releases"

[publication]
bundle_root = "www/releases"
registry_path = "sync/packing_slips/release-index.jsonl"
signing_key_env = "RELEASE_SIGNING_KEY"

[in-house]
filenames = [
    "custom-mod-1.0.0.jar",
    "another-custom.jar",
]

[discord]
# The bot token is read from config.d/.env under the fixed key
# DISCORD_BOT_TOKEN. The bot_token_env field documents the key name for
# forward compatibility; the current implementation reads the literal
# key directly.
bot_token_env = "DISCORD_BOT_TOKEN"

# Discord application snowflake. Required by the signed-HTTP interaction
# endpoint (Section 11.1.1) to validate that inbound interactions target
# this application.
application_id = "1234567890123456789"

# Ed25519 public key (64 lowercase hex) issued by Discord on the
# application page. Verifies every inbound interaction signature
# (Section 11.3.1).
public_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

guild_id = "..."
channel_id = "..."
allowlist_roles = ["..."]
allowlist_users = ["..."]

[providers]
curseforge_api_key_env = "CURSEFORGE_API_KEY"
modrinth_user_agent = "minecraft-upgrade/1.4.0 (contact@example.com)"

[registry]
trust_anchor_path = "sync/packing_slips/release-index.anchor"
trusted_keys_path = "config.d/trusted-release-keys.toml"

[state]
latest_slip_path = "/var/lib/minecraft-upgrade/latest-slip"
candidate_store = "/var/lib/minecraft-upgrade/candidates"
runs_store = "/var/lib/minecraft-upgrade/runs"
```

**`[in-house].filenames`** declares which jars in the seed are in-house mods. Filenames in the list are treated as `provider = "in-house"`, bypass provider query, and preserve their current version. The sorted filename list participates in `resolution_inputs_sha256`.

If `[in-house]` is present, `filenames` MUST be present (may be `[]`). A missing `[in-house]` table is equivalent to `filenames = []`.

Retirement of this list is deferred (§9.7).

**`[discord]` additions (Slice 15 T3b/T3c):** `application_id` and `public_key` are consumed by the bot daemon's signed-HTTP interaction endpoint (§11.1.1). `application_id` is a Discord snowflake string; `public_key` is 64 lowercase hex chars. Both are issued by Discord on the application page. The bot reads its token from `config.d/.env` under the fixed key named by `bot_token_env`.

**`[budget]` (Slice 14.5):** replaces the phantom `[state].ledger_path` from earlier spec revisions. `max_api_requests` and `max_downloaded_bytes` were previously declared under `[iteration]` but never read; they now live under `[budget]` and are enforced via the append-only usage log described in §8.2.1.

### 17.2 `config.d/deploy_pack.toml` additions

```toml
[docker]
deployment_mode = "release"              # "release" | "integration" | "legacy"
packing_slip_pointer = "stable"          # pointer name or tag
# For DEV's latest container:
# deployment_mode = "integration"
# packing_slip_pointer = "latest-slip"

[daemon]
state_dir = "/var/lib/minecraft-deploy"
slip_dir = "sync/packing_slips"
release_store = "/srv/minecraft/releases"
staging_root = "/srv/minecraft/staging"
http_host = "127.0.0.1"
http_port = 8765
bearer_token_env = "MINECRAFT_DAEMON_TOKEN"
tailscale_cidrs = "100.64.0.0/10,fd7a:115c:a1e0::/48"
idempotency_path = "/var/lib/minecraft-deploy/idempotency.sqlite"
audit_path = "/var/lib/minecraft-deploy/audit.jsonl"
lock_path = "/var/lib/minecraft-deploy/daemon.lock"
trusted_keys_path = "config.d/trusted_keys.toml"
registry_path = "sync/packing_slips/release-index.jsonl"
journal_path = "/var/lib/minecraft-deploy/operation.jsonl"
world_backup_root = "/srv/minecraft/backups/world"

# Slice 13 additions: pull source
source_host = "dev.example.com"          # empty disables /deploy
source_user = "deploy"
source_path = "/srv/minecraft-dev/www/releases"
ssh_key = "~/.ssh/id_deploy_warehouse_ro"

[discord.notify_dispatch]
# Opt into bot-daemon notification delivery (Slice 15 T3c). When this
# section is present, deploy_pack dispatches every notification through
# the bot daemon's POST /notify endpoint rather than the legacy
# webhook. Both keys are required when the section is present.
#
# When this section is absent, deploy_pack uses the legacy webhook
# path (unchanged behavior from prior slices).
bot_daemon_url = "http://127.0.0.1:8766"

# Name of the .env key whose value is the shared bearer token. This is
# the same .env value the bot daemon's [daemon].notify_bearer_token_key
# names; one secret, two consumers (Section 17.6).
bot_daemon_token_env = "DISCORD_BOT_NOTIFY_TOKEN"
```

If `[daemon].source_host` is empty, the daemon's `/deploy` endpoint returns 501 (NotImplementedError) after verification. This is the correct behavior for a daemon that cannot pull. Verification and `/verify` / `/publish` still work.

### 17.3 `config.d/build/<name>.yaml`

See §9.2 for the full schema. Summary:

| Field | Required | Type |
|---|---|---|
| `variables` | No | mapping of string |
| `repository` | Yes | string (git URL) |
| `ref` | Yes | string (branch/tag/SHA) |
| `entrypoint` | Yes | string |
| `script` | Yes | string |
| `args` | No | list of strings |
| `env` | No | mapping of string |
| `timeout_seconds` | No | float > 0 |
| `working_dir` | No | string |
| `depth` | No | int ≥ 0 |
| `artifacts` | Yes | list of string or `{path, dest}` |

Variable resolution and failure handling are described in §9.3 through §9.5.

### 17.4 Secrets

- Provider API keys: DEV-host secret store.
- Discord bot token: DEV.
- Release-signing private key: DEV.
- Approval-signing private key: DEV.
- RCON password: existing `secrets/rcon_password`.
- SSH keys for rsync transport: on prod (initiating pull) and DEV (allowing read-only access).
- No secrets in manifests, slips, bundles, `www/`, or logs.

### 17.5 Naming

| Context | Form |
|---|---|
| CLI flags | kebab-case |
| TOML keys/sections | snake_case |
| `.env` keys | snake_case |

### 17.6 `config.d/discord_bot.toml`

New in Slice 15. The bot daemon reads its runtime configuration from this file's `[daemon]` section. It is separate from `config.d/upgrade.toml` (application-level config: guild, channel, allowlists, keys) and `config.d/deploy_pack.toml` (deploy-side configuration).

```toml
[daemon]
# Directory for daemon-owned durable state (lock file, audit journal).
# Relative paths resolve against the project root (config_dir.parent).
state_dir = "var/discord-bot"

# Single-file deployment lock. Mirrors deploy_pack daemon lock semantics.
lock_path = "var/discord-bot/bot.lock"

# Signed-HTTP interaction endpoint bind address. Loopback by default;
# Discord requires a public-reachable URL, so in practice the daemon
# sits behind a reverse proxy or Tailscale funnel that terminates TLS
# and forwards to this address.
http_host = "127.0.0.1"
http_port = 8766

# Name of the .env key whose value authenticates POST /notify calls
# from the deploy_pack process. Absent = /notify disabled (the plugin
# will not register the route). When set, the .env key's value MUST
# be non-empty; a missing value raises at daemon construction so a
# partially-configured notify path cannot silently do nothing.
notify_bearer_token_key = "DISCORD_BOT_NOTIFY_TOKEN"

# CLI binary and timeout for minecraft-upgrade subprocess invocations
# triggered by slash-command handlers.
cli_binary = "minecraft-upgrade"
cli_timeout_seconds = 60
```

The file is required for the daemon to start. A bot that should not run a daemon simply has no `discord_bot.toml`. This contrasts with `config.d/upgrade.toml`'s `[discord]` section, which is optional and disables the bot when absent.

The bot daemon does not begin serving HTTP until `DiscordBotPlugin.start` is invoked by DaemonCore's plugin lifecycle. See §11.1 for the plugin's HTTP lifecycle contract.

---

## 18. Security & Reliability

1. Provider API secrets MUST live in DEV-host secret storage; never in repo, slip, bundle, report, or log.
2. Download URLs MUST use HTTPS unless explicitly excepted.
3. Downloaded filenames MUST be sanitized. Path traversal MUST be rejected.
4. Downloads MUST have bounded retries, timeouts, size limits.
5. Candidate writes MUST be atomic.
6. Prod daemon MUST run with least privilege, no unrestricted shell.
7. CI-generated Compose MUST be built from validated data.
8. Cleanup MUST run through `finally`-equivalent control paths.
9. Raw logs MAY contain player names and paths. Retention and access MUST be controlled.
10. Provider-provided scripts MUST NOT be executed.
11. Approved slip, approval record, bundle manifest, and validation evidence MUST be versionable and auditable.
12. No command may silently downgrade runtime, change loader, accept alpha, or ignore a missing dependency.
13. DEV's publication directory MUST expose read-only access to prod.
14. Prod MUST NOT hold provider API credentials.
15. Release bundles MUST be immutable once published.
16. Release registry MUST be append-only and signed.
17. Prod MUST pin trusted public keys and maintain a durable record of accepted registry-chain heads.
18. Approval actions MUST use Discord's signed interaction mechanism; forged interactions MUST fail.
19. Approval records MUST be signed and bound to immutable candidate identity.
20. Runtime startup MUST NOT silently download unpinned server or loader files.
21. Deployment MUST acquire a single exclusive lock.
22. Interrupted or ambiguous deployment recovery MUST halt safely.
23. `materialize` MUST NOT mutate `sync/downloads/.index/`. Only `promote` writes to `.index/`.
24. Staging isolation: candidate downloads MUST be staged in run-local directories and verified before publishing to the shared warehouse.
25. **The in-house build script runs with the DEV operator's privileges.** The tool's responsibility ends at running the script the mod declared; it does not sandbox the build, restrict network access, or verify that the resulting artifact is safe. Operators MUST review a mod's build script before running `build-inhouse` against it.
26. **The bot daemon's `/notify` endpoint is a service-to-service route, not a Discord command.** It authenticates via bearer token (constant-time compare) and MUST NOT be reachable when the bot is built without a notify token (§11.3.1). The `/interactions` route's Ed25519 signature is independent and is the only signature Discord issues.

---

## 19. Migration & Coexistence

### 19.1 Initial migration

On prod, after deploying the upgrade system:

1. Create `sync/packing_slips/`.
2. Generate `v1.0.0.toml` as a **legacy baseline**:
   - `release_mode = "update"`.
   - No runtime migration claim.
   - Records current artifacts, tree hashes, current world.
   - `[tested]` absent.
3. Write `stable` → `v1.0.0.toml`.
4. Legacy `deploy-pack` continues to work.

### 19.2 Dual-path period

- Legacy path active: `deploy-pack`.
- Release path active: `deploy-pack --release-pointer stable`.
- Integration path active on DEV: `deploy-pack --integration --release-pointer latest-slip`.
- All consume the same `sync/downloads/` on DEV.

### 19.3 Cutover

After one successful slip-mode deploy on prod:

- Legacy mode removed.
- `side_overrides.toml` deleted.
- `.deploy_protect` deleted.
- `overrides.py` deleted.
- `deps.py` moved to `modpack_upgrade`.

### 19.4 Rollback of the upgrade-system migration

Migration rollback MUST NOT depend on deleting the new release registry. Preserve published releases and audit records.

Before cutover:

- Create a verified backup of the old toolkit checkout, deployment configuration, artifact inventory, and current active release.
- Retain the known-good legacy deployment code and its test path.

If cutover fails:

1. Stop new deployments.
2. Restore the previous deployment configuration and known-good code.
3. Restore or verify the prior legacy artifact inventory.
4. Reactivate the legacy command path.
5. Confirm the current deployment still matches its prior manifest.
6. Preserve the failed migration records for diagnosis.

Do not delete immutable slips or rewrite release history as rollback.

---

## 20. Acceptance-Test Checklist

**Status as of v1.4.0:** 35 of 36 acceptance tests covered. Only T11 (approval replay/expiry) remains, blocked on Slice 15. T20 (migration rollback drill) is a rehearsal, not a code test, and is scheduled for Slice 16.

| ID | Test | Required pass condition | Status |
|---|---|---|---|
| T01 | Provider-isolated production deployment | Deploy a valid release with Modrinth/CurseForge credentials absent and endpoints blocked. All artifacts validate; no provider API is contacted. | OK |
| T02 | Tag and pointer integrity | Retarget a signed test tag, alter its registry entry, create an invalid channel reference, and create ambiguous/malformed pointer cases. All invalid cases fail closed. | OK |
| T03 | Filename collision | Attempt to stage two different byte sequences under the same artifact filename. The resolver rejects the collision or generates a deterministic collision-safe filename before changing any existing artifact. | OK |
| T04 | Dirty working tree | Release and integration deployments reject dirty deployment inputs. A clean integration run accepts a newer committed HEAD while continuing to use the selected candidate JAR set. | OK |
| T05 | Partial transfer | Interrupt rsync after partial transfer. The staging directory cannot be selected for deployment; the previous active release remains untouched. | OK |
| T06 | Approval invalidation | Change candidate hash, runtime, snapshot ID, or required evidence independently. Promotion/deployment is denied until applicable validation and approval are repeated. | OK |
| T07 | World-aware rollback | Deploy a test upgrade that changes a simulated world-format sentinel and then fails health checks. Verify the prior world snapshot, runtime, and exact prior artifact inventory are restored together. | OK |
| T08 | Interrupted deployment | Inject failure during file copy, rename, permission change, hash verification, and service start. Final state matches either previous release or fully validated new release; never a mixed active state. | OK |
| T09 | Runtime payload offline | Start a candidate with provider credentials absent and egress to Modrinth/CurseForge blocked. Runtime and all artifacts are available; recorded hashes match. | OK |
| T10 | Registry chain integrity | Truncate, fork, or alter a historical registry entry. Production rejects the chain and refuses to deploy. | OK |
| T11 | Approval replay and expiry | Replay the same Discord interaction; submit an expired approval; change each identity field. Stale or duplicate actions are rejected or return the previously recorded result. | PENDING Slice 15 |
| T12 | Config merge semantics | Test additions, replacements, target-only files, explicit deletions, protected paths, and path-pattern edge cases against a golden fixture. | OK |
| T13 | KubeJS delete semantics | Seed the target with stale scripts, nested directories, and a local-only file. After shipping, only files from the captured source tree remain. | OK |
| T14 | Client pack reproducibility | Build the ZIP twice from identical inputs; compare hashes. Test side filtering. Enumerate ZIP members and compare every hash with its manifest. | OK |
| T15 | Daemon API auth and idempotency | Submit forged, unauthorized, and replayed requests. All invalid cases fail; repeated valid requests return the original result. | OK |
| T16 | Concurrent deployment lock | Start two deployment requests simultaneously. One proceeds; the other is rejected or queued without corrupting state. | OK |
| T17 | Shared-mod shipping | Seed every case in a test `mods/` directory: enabled JAR in slip, enabled JAR not in slip, stale JAR, `.jar.disabled`, nested directory, non-JAR file. Assert exact preflight decision and resulting inventory. | OK |
| T18 | Config/KubeJS rollback | Deploy a release that changes config and KubeJS, then roll back. Previous approved bundle's config and KubeJS are restored exactly. | OK |
| T19 | Health-check failure recovery | Simulate startup timeout, missing RCON response, repeated container restarts, fatal logs, and proxy backend failure. Daemon halts or rolls back per contract. | OK |
| T20 | Migration rollback drill | Run the migration rollback on a production-like clone. Old command path, artifact inventory, and active release are restorable without data loss. | SLICE 16 (rehearsal) |
| T21 | Release bundle immutability | Attempt to republish an existing release ID with changed content. Publication fails; existing bundle remains untouched. | OK |
| T22 | Bundle manifest verification | Alter any file in a published bundle. Prod rejects the bundle and refuses to deploy. | OK |
| T23 | Pointer ambiguity | Create a state where both an alias file and a tag share a name. Alias wins. Malformed or path-traversing pointers fail closed. | OK |
| T24 | In-house build registration | Build an in-house mod via `build-inhouse`; verify it enters `sync/downloads/`, appears as a unified `[[jar]]` entry after operator marks it in `[in-house].filenames`, and passes the same validation as provider artifacts. | OK |
| T25 | Publication precondition failure | Attempt to publish with a missing artifact, mismatched hash, or invalid approval. Publication aborts; no partial bundle is written. | OK |
| T26 | Four-command lifecycle | `update` -> `acknowledge` -> `materialize` -> `iterate` completes successfully. Each command exits with documented code; each prints correct next command. | OK |
| T27 | `no_version = "drop"` skips acknowledgment | `update` with losses and `drop` -> exit 0, state=resolved. `materialize` proceeds directly. No `acknowledgment.json` written. | OK |
| T28 | `no_version = "fail"` halts at update | `update` with losses and `fail` -> exit 4, state=failed. No slip sealed; `materialize` refuses (exit 3). | OK |
| T29 | Materialize detects drift | After `update`, modify manifest, seed, or side overrides. `materialize` fails with exit 3 (report hash or `resolution_inputs_sha256` mismatch). | OK |
| T30 | Staging isolation | `materialize` downloads to `runs/<run-id>/staging/`; publishes to warehouse; does not write to `.index/`. Only `promote` writes to `.index/`. | OK |
| T31 | In-house bypass | In-house mods in `[in-house].filenames` are not queried against providers. Their current version is preserved. Their `jar_sha256` in the slip matches the warehouse artifact. | OK |
| T32 | `expected_hashes` multi-algorithm verification | Materialize verifies every listed digest (sha512, sha1, md5 as applicable). Any mismatch -> exit 3. Slip's `sha256` is locally computed independently. | OK |
| T33 | Side precedence chain | Override beats jar metadata; jar metadata beats provider; provider beats `.pw.toml`. All five scenarios tested. | OK |
| T34 | Filename collision at warehouse | Attempt to materialize artifact with same filename as warehouse artifact but different SHA-256. Exit 3 without overwriting. Same SHA-256 reuses without copy. | OK |
| T35 | `resolution_inputs_sha256` stability | Formatting-only edits to manifest or `side_overrides.toml` produce identical fingerprint. Semantic edits change it. | OK |
| T36 | API cap enforcement | Configure `max_api_requests = 5`, resolve a manifest requiring more than 5 API calls. Resolution aborts with exit 6 and the cap diagnostic. The `usage.jsonl` log records exactly 5 `api` lines for the run. | OK |

---

## 21. Acceptance Criteria

The system is acceptable for production use only when:

1. Candidate runtime read from effective Compose; not duplicated.
2. Resolution follows RELEASE -> BETA -> authorized ALPHA per `[policy].selection`.
3. Dependency closure is iterative and unambiguous.
4. Slip identifies exact provider versions and verified SHA-256.
5. Failed resolution leaves approved slip and artifacts intact.
6. Materialized `.pw.toml` parses with `deps.py`.
7. Side policy is slip-bound; `side_overrides.toml` not regenerated.
8. DEV runs fresh-world boot tests in isolation.
9. Client artifact is built once and shipped unchanged.
10. Prod world snapshot tested without modifying source.
11. Velocity integration is independent and reported.
12. Approval blocked while any required stage fails.
13. Approved slip bound to runtime and tested inputs.
14. Prod deploy uses `deploy-pack --release` or `--release-pointer`.
15. Prod pulls release bundles from DEV. Prod never contacts mod providers.
16. Promote stages the slip; a human commits, tags, and pushes.
17. Slip loading is additive to existing config; instance-to-Compose resolution preserved.
18. Pre-deploy verification reconciles slip against effective Compose; no path derived solely from the slip.
19. `release_mode` is explicit and enforced via CLI flag and slip field.
20. Deployment mode (`release` vs `integration`) is explicit and enforced.
21. `latest` composes frozen JARs from `latest-slip` with one clean committed Git revision.
22. Unified `[[jar]]` array; no `[[in_house_jar]]` section.
23. Release bundles are immutable and verified.
24. Release registry is append-only and signed; prod rejects chain tampering.
25. Runtime payload is staged; no silent runtime downloads.
26. Deployment is a logical transaction with a durable lock and journal.
27. Rollback restores world, runtime, artifacts, and inputs together.
28. Approval is signed, bound to immutable identity including `resolution_report_sha256`, and expires.
29. Daemon API is authenticated, idempotent, and replay-protected.
30. Automated tests meet project coverage gates.
31. Full behavior manually verified on DEV before first production use.
32. All acceptance tests in §20 pass.
33. Four-command resolver lifecycle (`update` -> `acknowledge` -> `materialize` -> `iterate`) works end-to-end.
34. State machine transitions correctly per §8.2.
35. `resolution_inputs_sha256` is stable across formatting-only changes.
36. `materialize` performs full integrity checks.
37. Staging isolation preserves active `.index/`.
38. In-house mods identified via `[in-house].filenames` and bypass provider query.
39. **In-house build pipeline clones the mod's own repository, checks out its declared ref, runs its own build script, and stages the artifacts its build produces. The upgrade toolchain does not own build logic.**

---

## 22. Known Limits

- Provider compatibility flags are candidate filters, not guarantees.
- JAR metadata is incomplete for some mods.
- Static analysis identifies definite issues, not full compatibility.
- A server that starts may still have gameplay/world defects.
- A world that boots may contain missing/changed modded content.
- Velocity success cannot be inferred from bare server startup.
- Human testing is mandatory for first approved candidate and for world-affecting changes.
- Live world snapshot may capture torn chunks; re-run with `--stop-prod` on failure.
- Multi-world gate collapsed to single world per run; slip records which world was tested.
- SMB/NFS transports are not supported; rsync over SSH is required.
- Provider hash algorithms vary (sha512/sha1/md5); SHA-256 always computed locally.
- **In-house build pipeline runs with the DEV operator's privileges.** The tool does not sandbox the build script. Malicious build scripts have full operator access. Review before running.
- **In-house build pipeline requires the mod's repository to be reachable from DEV.** Offline DEV cannot run `build-inhouse` unless the repository is a local clone.
- **In-house build pipeline does not cache clones across invocations.** Every `build-inhouse` is a fresh clone. Slow repositories make slow builds.
- **In-house build pipeline does not clean up failed clones automatically.** A failed build leaves the clone at `.build-workspace/<name>-<timestamp>/` for operator inspection. The operator is expected to remove it manually after investigation.
- **In-house `[in-house].filenames` marker is still manual.** After `build-inhouse` succeeds, the operator must add the jar's filename to `config.d/upgrade.toml [in-house].filenames`. The `.pw.toml` writer marker that would retire this step is deferred (§9.7).
- In-house identification is transitional via `[in-house].filenames`; retirement is deferred to Slice 17+.
- `iterate` requires prior `materialize`; no combined mode.
- CurseForge-sourced mods always require overrides (no side field in provider metadata).
- The full log-classifier pattern catalog evolves after interface freeze; its status model and fix-application permissions are fixed.
- **Production daemon deployment orchestration is implemented but not yet exercised against a real production deployment.** All modules (`deploy`, `pull`, `health`, `backup`, `recovery`, `service_control`, `health_probe`, `deploy_runtime`) are tested at module and HTTP-endpoint level. First production deployment will exercise the plumbing end-to-end for the first time.
- **Production daemon's recovery paths are implemented but not yet exercised against a real failure.** T07, T08, and T19 cover them via injected failures; a real prod failure (network drop, disk full, OOM) may find gaps in the failure modes we didn't anticipate.
- **Cap enforcement is best-effort under concurrency.** The usage log assumes a single writer. Concurrent `update` invocations may briefly exceed the configured cap by up to one call per concurrent writer. Multi-writer coordination is deferred to v2.0.0.
- **Cap enforcement is per-window.** A cap of 5000 API requests per 24 hours resets at the window boundary; there is no cross-window accumulation.
- **Byte cap can be exceeded by one download.** The byte-cap check runs before each download. If a single download pushes the total past the cap, the *next* download is refused. Operators mitigate by setting `max_bytes` on individual downloads below their cap surplus.
- Production readiness is not claimed until §20 tests pass and the migration runbook is exercised on DEV.

---

## 23. Appendices

### 23.1 Authority table

| Concern | Authority |
|---|---|
| Target runtime intent | `sync/modpack.manifest.toml [minecraft]` |
| Release mode | CLI `--release-mode` + `state.toml` |
| Effective runtime | Candidate `docker-compose.yml` |
| Runtime authority — update | Docker Compose |
| Runtime authority — upgrade | Packing slip `[runtime]` |
| Seed (active pack) | `sync/downloads/.index/*.pw.toml` |
| In-house filenames | `config.d/upgrade.toml [in-house].filenames` |
| **In-house build specification** | **`config.d/build/<name>.yaml`** |
| **In-house build execution** | **Mod's repository (own build script)** |
| Side policy | `config.d/side_overrides.toml` |
| Selection policy | `[policy].selection` |
| Loss policy | `[policy].no_version` |
| Usage log | `[budget].usage_log_path` (append-only JSONL) |
| API cap | `[budget].max_api_requests` |
| Byte cap | `[budget].max_downloaded_bytes` |
| Selected artifacts | Candidate slip |
| Approved release | `packing_slips/<tag>.toml` |
| Release slip | `sync/packing_slips/<tag>.toml` |
| Released bundle | `www/releases/<release-id>/` |
| Release registry | `packing_slips/release-index.jsonl` |
| Trust anchor | `packing_slips/release-index.anchor` |
| Pointer — stable | `packing_slips/stable` (Git-tracked) |
| Pointer — latest | `/var/lib/minecraft-upgrade/latest-slip` (DEV-local) |
| Provider metadata | Modrinth/CurseForge adapters (DEV only) |
| Artifact bytes (prod) | DEV release bundle, rsync pull |
| Artifact bytes (DEV) | Providers + in-house builds |
| Resolution evidence | `www/runs/<run-id>/resolution-report.toml` |
| Loss acknowledgment | `/var/lib/minecraft-upgrade/runs/<run-id>/acknowledgment.json` |
| Candidate slip | `/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml` |
| `.pw.toml` writer (external) | `modpack_upgrade.pw_toml` (§5.7) |
| `.pw.toml` writer (in-house) | Mod's own build script (§9.4) |
| Slip-mode source sets | `SlipContext` (§5.8) |
| Side precedence | `docs/Side_Determination.md` |
| Protect-pattern matching | `fnmatch.fnmatch`, two scopes (§13.5) |
| Shipping transaction | `modpack_upgrade.shipping` (§13.2) |
| Instance/container mapping | `deploy_pack.toml` + Compose |
| Deployment interface | Release bundle + slip |
| Production lifecycle | `deploy_pack` v2 |
| **Production deployment orchestration** | **`minecraft.deploy_pack.daemon` (§12.4)** |
| World migration | Isolated test copy of verified snapshot |
| World rollback | Pre-upgrade world backup + previous release bundle |
| Velocity topology | Isolated DEV proxy; prod proxy if present |
| Final promotion | Human-committed git tag + `stable` update |
| Publication | Explicit `minecraft-upgrade publish` |
| Discord bot application config | `config.d/upgrade.toml [discord]` (§17.1) |
| Discord bot daemon runtime | `config.d/discord_bot.toml [daemon]` (§17.6) |
| Bot → Discord notifications | `POST /notifications` dispatch via `BotClient` |
| Deploy → bot daemon notifications | `config.d/deploy_pack.toml [discord.notify_dispatch]` (§17.2) |

### 23.2 Placeholder reference (Discord)

Unchanged from `Project_Specs.md` §11.3. New placeholders MUST be documented before use.

### 23.3 Exit code reference

See §16.

### 23.4 Glossary

- **Candidate** — artifact set + configs under test, not yet approved.
- **Slip** — packing slip, the release registry.
- **Release bundle** — immutable published artifact set for a release.
- **Release registry** — signed append-only chain of release identities.
- **Trust anchor** — pinned signed initial registry entry.
- **Pointer** — text file (`stable`) or DEV-local file (`latest-slip`) naming a slip or candidate.
- **Release mode** — `update` or `upgrade`. Runtime authority selector. Set by CLI `--release-mode`.
- **Deployment mode** — `release` (immutable, hashes enforced) or `integration` (moving committed Git inputs).
- **Manifest** — target runtime intent declaration. 5 fields. Not a mod list.
- **Seed** — active pack inventory from `sync/downloads/.index/*.pw.toml`.
- **Resolution report** — machine-readable record of resolver output, losses, and provisional sides.
- **Acknowledgment** — human decision record permitting materialization after `no_version = "ask"` pause.
- **Candidate slip** — sealed slip produced by `materialize`, DEV-local until `promote`.
- **Shipping** — operation that materializes a slip into `mods/`.
- **Shipping transaction** — logically atomic apply of a source set to a target directory with rollback (§13.2).
- **SlipContext** — immutable bridge between a verified slip and the existing pipeline (§5.8).
- **Promote** — copy candidate slip to release registry; print git instructions; do not commit.
- **Publish** — create an immutable release bundle. Explicit operator command.
- **Deploy** — activate staged release; invoke `deploy-pack --release <tag>`, or hit `/deploy` on the production daemon.
- **Iteration** — one boot attempt after candidate artifact change.
- **Cap** — hard stop on automated resolution.
- **Ambiguous failure** — classifier cannot confidently fix; pause.
- **Legacy baseline** — `v1.0.0`, produced at migration time, `release_mode = "update"`.
- **Provenance metadata** — `provider` / `project_id` / `version_id` / `provider_endpoint`. Record origin; do not drive prod fetches.
- **`latest-slip`** — DEV-local candidate pointer.
- **`stable`** — Git-tracked pointer consumed by production.
- **Operation journal** — durable record of deployment stage transitions (§12.7).
- **Rollback journal** — durable record of prior managed file state (§12.10).
- **Bundle manifest** — signed inventory of every file in a release bundle.
- **`resolution_inputs_sha256`** — canonical semantic fingerprint of resolver inputs.
- **`expected_hashes`** — provider-supplied hashes (any algorithm) for download verification; distinct from slip's locally-computed `sha256`.
- **In-house build spec** — `config.d/build/<name>.yaml`. Declares repository, ref, entrypoint, script, and artifact patterns. The tool runs the script; the mod's repository owns the build logic.
- **Build workspace** — `.build-workspace/<name>-<timestamp>/`. Transient; kept on failure for inspection, removed on success.
- **Build outcome** — structured result of `run_build`. Success flag, staged artifacts map, failure message, stdout/stderr excerpts.
- **Usage log** — append-only JSONL record of API calls and downloaded bytes. Bounds the resolver's total spend against `[budget]` caps (§8.2.1).
- **Discord bot daemon** — standalone peer service over DaemonCore. Owns Discord communication end-to-end (§11.1).
- **`/interactions` endpoint** — signed-HTTP endpoint for Discord application-command payloads. Auth via Ed25519 signature (§11.1.1).
- **`/notify` endpoint** — service-to-service endpoint for `deploy_pack` notifications. Auth via bearer token (§11.1.2). Not a Discord command.

---

## 24. Behavioral Summary

| Operation | Behavior |
|---|---|
| Manifest validation | 5 fields; no per-mod policy; exit 3 on any violation. |
| Update | Resolve, write report + state. Exit 0/6/4/3/1 per state. |
| Acknowledge | Human decision gate. Verify report + inputs + Compose. Record decision. |
| Materialize | Verify + download + seal slip. Staging isolation. Final side determination. |
| Iterate | Boot loop on finalized candidate. |
| Promote | Stage slip + metadata to git; print instructions. No commit. |
| **Build-inhouse** | **Clone repository, check out ref, run build script, expand artifact patterns, stage artifacts. Exit 0/1/3.** |
| Publish | Create immutable bundle. |
| Pull (DEV->Prod) | rsync-over-SSH pull from DEV publication dir. |
| Deploy | `/deploy` on daemon: lock -> pull -> backup -> stop -> ship -> start -> health -> complete, with recovery on post-stop failure. |
| Config load | Slip is additive. `SlipContext` attached when slip present. |
| Runtime check — update | Compose authoritative. Slip must match. `compose_tree` enforced. |
| Runtime check — upgrade | Slip authoritative. Compose must match slip + `compose_tree` hash. |
| Path resolution | Derived from `deploy_pack.toml` + Compose. Slip verifies. |
| Artifact authority | Prod pulls exact tested bytes from DEV bundle. |
| Release deploy tree hashes | Enforced. |
| Integration deploy tree hashes | Not enforced. One clean committed HEAD revision. |
| `latest` JAR source | Frozen via `latest-slip`. |
| `latest` Git source | One clean committed HEAD revision, captured once per deployment. |
| Release identity | Signed tag + registry chain + bundle manifest. |
| Slip parsing | Preserves document order. Canonical ordering applied only for hashing. |
| `.pw.toml` writer (external) | One external provider block or zero for in-house. `side = "unknown"` sentinel. Byte-stable. |
| `.pw.toml` writer (in-house) | Mod's own build script. Tool does not write it. |
| Shipping | Logically atomic transaction. |
| Protect patterns | `fnmatch.fnmatch`. |
| Rollback | Previous approved bundle + pre-upgrade world backup (for upgrades). |
| Concurrent deployment | Single exclusive lock. Atomic acquisition via `mkstemp`-then-link. |
| Interrupted deployment | Journal records every stage. Startup refuses if any run is unresolved. |
| Approval | Signed, bound to immutable identity including `resolution_report_sha256`, expires after seven days. |
| Runtime startup | Uses staged payload. No silent downloads. |
| Side determination | Override > jar metadata > provider > `.pw.toml`. Exit 3 if all unknown. |
| In-house mods | `[in-house].filenames`; bypass provider; preserve version; `jar_sha256` or `build_id`. |
| `expected_hashes` verification | Multi-algorithm; any mismatch -> exit 3. |
| Filename collision | Same SHA-256 -> reuse; different -> exit 3. |
| **In-house build script** | **Runs with operator's privileges. Not sandboxed. Reviewed before running.** |
| **In-house build artifact staging** | **`.pw.toml` -> `sync/downloads/.index/`; other -> `sync/downloads/`. Traversal-rejected at glob expansion.** |
| **In-house build failure** | **Clone kept for inspection. Exit 1. Diagnostic names the clone path.** |
| **Health probe** | **Five checks per container. Missing container, stopped, no health block, restart drift, RCON unreachable, or fatal marker all report with specific reasons.** |
| **Usage log** | **Append-only JSONL at `[budget].usage_log_path`. Read on every cap check; records older than `[budget].usage_window_hours` ignored. Malformed lines logged and skipped; unreadable log aborts (exit 3).** |
| **Discord `/interactions`** | **Ed25519 signature required. PING/PONG handshake. Application commands normalized, scheduled, DEFERRED. Unsupported types -> HTTP 400.** |
| **Discord `/notify`** | **Bearer-token auth (constant-time compare). Payload validated. Delivery failure -> HTTP 502, no webhook fallback. Route not registered when `notify_bearer_token` is `None`.** |
| **Discord bot HTTP lifecycle** | **`start()` constructs uvicorn server, awaits readiness, holds task. `stop()` signals shutdown, awaits task. `health()` reports `unhealthy` when not serving.** |

---

**End of specification v1.4.0.**

---

## Pending before production cutover

| Deliverable | Status |
|---|---|
| Slice 15 implementation (Discord bot, approval flow) | T3b+T3c implemented and passing; T11 (approval replay/expiry) pending |
| Slice 16 implementation (migration runbook, T20 rehearsal) | Not started |
| Slice 17+ (full log classifier, `[in-house].filenames` retirement) | Not started |
| `docs/Manifest_Schema.md` | Frozen (rev f) |
| `docs/Resolution_Report.md` | Frozen (rev g) |
| `docs/Side_Determination.md` | Frozen (rev f) |
| `docs/Build_Specs.md` | Not written (Slice 14 follow-up) |
| Migration runbook | Not written |
| OCI registry allowlist and image pinning procedure | Not written |
| World backup and restoration drill | Not written |
| All §20 acceptance tests | 35/36 covered; T11 pending Slice 15 |
| Verification that existing SMB config is truly inactive | Not verified |

**Design freeze is complete.** Implementation is authorized against §20 and the frozen v1.4.0 contract. Production cutover requires all §20 tests passing and the migration runbook exercised on DEV.