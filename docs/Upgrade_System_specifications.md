# Minecraft Upgrade System — Specification v1.3.1

**Status:** Design of record. Architecture frozen. Slices 1–6 implemented and passing (1436 tests, 91.08% coverage). Slice 7a documentation frozen at rev e. Production readiness requires slices 7b–17, all §20 acceptance tests, and exercising the migration runbook.

**Supersedes:** v1.3.0

**Baseline contract:** `Project_Specs.md` v3.0 (`deploy_pack` v2)

**Date:** 2026-10-17

**Changes in v1.3.1:**

1. Manifest schema frozen (5 fields): `[minecraft]` + `[policy]`. No per-mod policy.
2. `resolution_inputs_sha256` added to `[fingerprints]`; `side_policy_sha256` retired.
3. `[[jar]].dependencies.source` extended with `provider_metadata`; `type` constrained to six values.
4. Four-command resolver lifecycle: `update` → `acknowledge` → `materialize` → `iterate` (plus `promote`). Explicit state machine.
5. `--release-mode {update|upgrade}` required CLI flag on `update`; persisted for resume.
6. Resolution report schema frozen (`docs/Resolution_Report.md`). Report hash binds to Phase B approval.
7. `expected_hashes` array replaces `expected_sha256`. Provider hashes preserved with algorithms; slip carries locally-verified SHA-256.
8. Side determination runs at `materialize`; CurseForge always `unknown`; Fabric absent `environment` → `both`; `unknown`/absent `.pw.toml` side is valid through preflight.
9. In-house mods identified via `config.d/upgrade.toml [in-house].filenames`; fingerprint uses `build_id` or `jar_sha256`; `current_version_id` omitted.
10. Staging isolation: candidate downloads and `.pw.toml` files staged in run-local directories; `.index/` mutated only by `promote`.
11. Candidate slip is DEV-local; git-tracked release slip produced by `promote` only.
12. Exit codes clarified: network = 1; integrity = 3; fail = 4; ask-pause = 6.

**Retained from v1.3.0:**
- Artifact authority: prod receives exact tested bytes.
- Slip `provider`/`project_id`/`version_id` are provenance metadata, not fetch instructions.
- Promote stages the slip and stops. Human commits, tags, pushes.
- Slip is additive to existing config. Instance-to-Compose resolution preserved.
- Verify, don't derive. Paths come from existing config; slip verifies.
- `release_mode` (`update` | `upgrade`) governs runtime authority.
- Canonical hashing pipeline (RFC 8785, NFC, bounded integers, safe `None` normalization).
- DaemonCore-based production daemon.

**v1.3.1 documentation set:** This document plus three companions constitute the frozen v1.3.1 contract. All four are normative.

- `docs/Manifest_Schema.md` — manifest format, seed rules, command lifecycle
- `docs/Resolution_Report.md` — report schema, state machine, acknowledgment
- `docs/Side_Determination.md` — side precedence chain, per-provider rules

---

## 1. Purpose

`deploy_pack` v2 applies an already-materialized artifact set. This specification defines the layer that **constructs, validates, freezes, publishes, and transports** that artifact set:

1. Resolve exact mod versions from upstream providers against a target Minecraft/loader runtime.
2. Download artifacts once into DEV's warehouse.
3. Build in-house mods against the candidate runtime.
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
Providers (Modrinth, CurseForge)
        │
        │ fetch (DEV only)
        ▼
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
- **Prod** MUST pull release bundles from DEV via rsync over SSH. Prod MUST NOT contact Modrinth, CurseForge, or any other mod provider during any release cycle.
- **DEV** MUST NOT push to prod. DEV exposes its publication directory; prod initiates transfer.
- Prod MUST receive the exact artifact bytes that DEV tested. The bytes prod receives MUST hash to the slip's recorded SHA-256 values.
- DEV MUST NOT pull mods from prod (prod runs an older runtime during upgrade).
- Client MUST NOT be a runtime dependency for DEV's resolution loop.
- Container images MAY be pulled by prod from an explicitly allowlisted OCI registry by immutable digest. This is a separately authorized source class and does not violate the provider constraint.
- **SMB/NFS are not supported in v1.3.1.** rsync over SSH is the only supported transport.

---

## 4. Storage Model

### 4.1 Repository layout

```
repo/
├── config.d/
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
├── ledger.sqlite                   # deployment ledger + API/byte budgets
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
- `latest-slip` is DEV-local and not Git-tracked.
- Candidate slips (`/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml`) are DEV-local. Only `promote` copies them to `sync/packing_slips/<tag>.toml`.
<!-- REVIEW-FIX 4: candidate-slip immutability restored as an explicit invariant -->
- A sealed candidate slip is **immutable**. After `materialize` writes `candidates/<run-id>/packing_slip.toml`, no command modifies it in place. A change to any input produces a new run-id and a new slip. `promote` copies; it does not edit.
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
resolver_version = "minecraft-upgrade 1.3.1"
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

The enum comments are normative. `config_mode ∈ {merge, delete}`. `kubejs_mode = delete` only. `kubejs_mode = "merge"` is not supported in v1.3.1.

### 5.3 Rules

1. `release_id` MUST be immutable. Re-using an ID with different content MUST fail.
2. `content_sha256` MUST be computed over the canonical body (§5.4).
3. `release_mode` MUST be `"update"` or `"upgrade"`.
4. **Production release deploys MUST verify `[inputs.git]` tree hashes against the working tree. Mismatch blocks deploy.**
5. **Integration deploys (`latest`) MUST NOT enforce `[inputs.git]` tree hashes.** Integration composes the JAR set from the candidate slip with one clean committed Git revision.
6. Every `[[jar]]` MUST resolve through its declared `provider` + `provider_endpoint` during preparation. Resulting artifact MUST stage at `sync/downloads/<filename>` and match recorded SHA-256. Applies equally to external and in-house artifacts.
7. Every `[[jar]]` MUST follow the same promotion and release-packaging workflow. Provider origin MUST NOT create a separate deployment pipeline or distinct registry section.
8. Every `[[jar]]` MUST declare dependency edges with:

   - `namespace` ∈ `{"project", "modid"}`
   - `source` ∈ `{"pw_toml", "jar_manifest", "provider_metadata"}`
   - `type` ∈ `{"required", "optional", "incompatible", "embedded", "tool", "include"}`

   Empty array if none.

<!-- REVIEW-FIX 1: CurseForge numeric relationType → type mapping restored as normative -->
   **CurseForge `relationType` → `type` mapping.** The CurseForge API returns numeric `relationType` values. The adapter MUST map them as follows:

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

`side_policy_sha256` from v1.3.0 is retired. Its semantic inputs are covered by `side_overrides` inside `resolution_inputs_sha256`.

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

**In-house identification**: `config.d/upgrade.toml [in-house].filenames`. Filenames in that list are treated as `provider = "in-house"`, bypassing provider query. Retired when slice 13 introduces a `.pw.toml` marker.

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
- Discord bot as operator interface.
- Upgrade controller owns candidate state, artifact preparation, container lifecycle, and publication.

### 8.2 Phase A — four-command lifecycle

Phase A is split across four commands. Each is invoked explicitly by the operator; each exits and prints the next command. The pipeline is:

```
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

Budgets are persisted in a SQLite event ledger keyed by run ID and resolution attempt. A controller restart resumes the same run and its accumulated usage. It MUST NOT reset caps. A corrupt or unreadable budget ledger fails closed and requires operator intervention.

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

<!-- REVIEW-FIX 5: <tag> ↔ <release_id> equivalence stated -->
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

### 9.1 Trigger

Explicit Discord command. No auto-build on push.

```
/upgrade build-inhouse <mod> <revision>
```

### 9.2 Flow

1. Resolve source revision to immutable commit SHA.
2. Clean isolated workspace.
3. Build with candidate's target Minecraft/loader runtime and recorded toolchain.
4. Hash resulting JAR.
5. Publish to `www/artifacts/in-house/<slug>/<build-id>/<file>.jar`.
6. Register as in-house artifact (CAS backing, web view).
7. Add to candidate slip as a `[[jar]]` entry with `provider = "in-house"`.
8. Stage into `sync/downloads/` for release bundle inclusion.
9. Emit the in-house `.pw.toml` per §5.7 with no provider update block.

### 9.3 Build record

Source repository, immutable commit SHA, toolchain identity, target runtime, outcome, artifact SHA-256, dependency metadata.

### 9.4 Rule

Compilation MUST NOT be treated as compatibility approval. The in-house JAR MUST pass the same validation as provider artifacts.

### 9.5 In-house identification (transitional)

Before slice 13, in-house artifacts are identified via `config.d/upgrade.toml [in-house].filenames`. Filenames in that list are treated as `provider = "in-house"`, bypass provider query, and preserve their current version. The sorted filename list participates in `resolution_inputs_sha256` (§5.4.1).

If `[in-house]` is present, `filenames` MUST be present (may be `[]`). A missing `[in-house]` table is equivalent to `filenames = []`.

Slice 13 introduces a `.pw.toml` writer-level marker and retires the config list.

### 9.6 Unified artifact contract

An in-house mod and a Modrinth mod are both JAR artifacts. They follow the same packing, validation, publication, and deployment contract. The `provider` field records origin; the deployment pipeline does not branch on it.

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
- Verified against a canonical file manifest.
- Retained per retention policy.

The DEV migration-test snapshot and the production rollback backup are distinct artifacts.

---

## 11. Discord Interface

### 11.1 Bot

- `discord.py`
- Separate service on DEV
- Narrow commands only
- The bot is not the controller and has no direct filesystem access to mutate approvals.

### 11.2 Commands

| Command | Purpose |
|---|---|
| `/upgrade status` | Current run state |
| `/upgrade build-inhouse <mod> <revision>` | Trigger in-house build |
| `/upgrade migrate-world [survival\|creative]` | Trigger world snapshot |
| `/upgrade approve` | Approve current prompt |
| `/upgrade reject [reason]` | Reject current prompt |

### 11.3 Rules

- Allowlist users/roles.
- Idempotent button clicks; interaction ID persisted with a unique constraint.
- Approval bound to candidate/snapshot identity.
- Approval state persisted.
- No arbitrary shell commands.
- Bot token outside git and out of logs.
- Bot MUST NOT be the controller.
- Approval actions use Discord's signed HTTP interaction mechanism. The controller verifies Discord's Ed25519 request signature, validates guild/channel/user identity and authorized role, and checks the pending approval against stored candidate state.

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

**The daemon verifies the release and the transfer. It does not resolve replacement mod versions. It does not contact upstream mod providers.**

### 12.2 Exposure

- Tailscale-only HTTP endpoint.
- Authenticated, replay-protected, idempotent.
- Only one production deployment may run at a time.
- A durable operation journal records each stage.
- A rollback journal preserves prior managed file state.

### 12.3 Inputs

- Immutable release ID (git tag) or pointer name (`stable`)
- Expected git commit SHA
- Packing-slip content hash

MUST refuse: branch names, arbitrary commands, ambiguous pokes.

### 12.4 Lifecycle — pull-based

```
Publication phase (DEV):
  P1. Operator runs `minecraft-upgrade publish <release-id>`.
  P2. DEV verifies preconditions (§6.3).
  P3. DEV writes the immutable bundle.
  P4. DEV signs the bundle manifest and records the registry entry.

Deployment phase (PROD):
  1. Receive deploy request with release ID or pointer.
  2. Resolve pointer (if applicable) to a release ID and record the
     channel-commit receipt.
  3. Fetch the release registry and verify the chain against the pinned
     trust anchor and the last accepted head.
  4. Verify the release tag signature, tag object identity, commit,
     slip hash, and bundle manifest.
  5. Pull the release bundle from DEV via rsync over SSH into
     staging/<release>.partial/.
  6. Verify the bundle manifest and every file hash.
  7. Move the verified bundle to releases/<release>/.
  8. Verify slip.runtime against effective Compose (per release_mode).
  9. Verify slip [inputs.git] tree hashes against the working tree
     (release mode only).
 10. Record publication success in the deployment ledger.
     ─── separate authorization ───
 11. Acquire the deployment lock.
 12. Pre-stage runtime and artifacts.
 13. Stop services consuming the shared mod directory.
 14. Apply staged changes using the shipping transaction (§13.2).
 15. Verify the resulting inventory against the selected slip.
 16. Start services.
 17. Run post-deployment health checks.
 18. On success: record the active release; release the lock.
 19. On failure: enter recovery (rollback or halt).
```

Steps 1–10 are the **publication** phase on prod. Steps 11–19 are the **deployment** phase. Publication and deployment remain distinct operations with separate authorization.

Slip modes attach a `SlipContext` (§5.8) to `DeploymentConfig` and `PreflightPlan`. Scope modules take their source sets from the context, not from `deps.resolve_mod_sources` or `overrides.py`.

### 12.5 Pull transport

- rsync over SSH.
- Prod initiates. DEV exposes the publication directory as a read-only rsync target.
- DEV never initiates a transfer to prod.
- Auth via SSH key restricted to a forced command running rsync in read-only mode, rooted at the publication directory.
- Interactive shell, PTY, port forwarding, agent forwarding, and arbitrary commands are disabled.
- DEV's raw `sync/downloads/` warehouse is not exposed to that account.
- Prod cannot upload, delete, rename, or modify publication files.

### 12.6 Deployment lock

- A single lock file or database row enforces one concurrent deployment.
- The lock is acquired before step 11 and released after step 18.
- A stale lock from a crashed daemon MUST be detectable and MUST require operator confirmation before override.
- Concurrent deployment requests are rejected or queued; they MUST NOT corrupt state.

### 12.7 Operation journal

- Every stage transition is recorded in a durable journal.
- On restart, the daemon reads the journal and either resumes safely or halts for operator intervention.
- An interrupted or ambiguous recovery MUST halt safely; it MUST NOT guess.

### 12.8 Runtime staging

- Runtime payload is included in the release bundle.
- Prod pre-stages the runtime before stopping services.
- Production startup MUST use the staged payload.
- Production startup MUST NOT silently retrieve new server or loader files.
- The implementation MUST adapt the existing container startup mechanism if necessary to satisfy this requirement.

### 12.9 Health checks

The daemon performs health checks for each targeted service:

- Expected container is running.
- Startup-completion log sentinel observed within the configured timeout (default 600 seconds).
- No unexpected container restart during the validation window.
- Authenticated local RCON `list` command succeeds three times, 15 seconds apart.
- No configured fatal log condition appears during the health window.
- Where Velocity is part of the release, a separate proxy-to-backend connection test must succeed.

RCON is enabled only on the internal Docker network; it is not published to the host or public network.

### 12.10 Recovery behavior

If a health check fails:

- The daemon stops the affected service from accepting player traffic.
- Preserves logs and the failed deployment receipt.
- Attempts recovery only if it can prove the required prior artifacts and world backup exist.

For a same-runtime update:

- Restore the previous approved artifact/config/KubeJS state.
- Rerun health checks.

For a runtime/world-format upgrade:

- Restore the pre-upgrade world backup.
- Restore the previous runtime.
- Restore the previous artifacts.
- Rerun health checks.

If the backup is missing, corrupt, or cannot be proven compatible with the rollback plan, the daemon halts and requires operator intervention.

The daemon records the failure and recovery outcome, and sends a notification to the configured Discord operations channel and local system journal.

### 12.11 Rollback

- Retain the previous known-good release bundle.
- Retain the pre-upgrade world backup.
- Restoring the git tag alone is NOT sufficient rollback if the world format changed.
- Rollback restores the compatible combination of world, runtime, artifacts, and inputs.

### 12.12 Daemon API

The daemon API MUST be specified before implementation. At minimum it defines:

- Endpoints and HTTP methods.
- Request and response schemas.
- Authentication (Tailscale identity plus a shared secret or mTLS).
- Idempotency key semantics.
- Replay protection.
- Error codes and error bodies.
- Status endpoint.

The API is an implementation deliverable with acceptance tests T15 and T16.

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

The transaction does not stop or start services; the caller wraps it in lifecycle phases (§12.4 steps 13 and 16).

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
- Network access to Modrinth, CurseForge, or any other mod provider.
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
| `build-inhouse <mod> <rev>` | Build in-house JAR |
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
<!-- REVIEW-FIX 3: collect-then-fail restored for unknown sides -->
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
<!-- REVIEW-FIX 2: pause/notify behavior on ambiguity or cap exhaustion restored -->
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
| 1 | Operational failure (provider API, network, download) |
| 2 | CLI usage error |
| 3 | Config/schema/integrity/preflight failure |
| 4 | Resolution failure (`no_version = "fail"` with losses) |
| 5 | Validation gate failed |
| 6 | Approval blocked (`ask` with losses; unacknowledged pause) |
| 7 | Publication failure |

**Clarifications:**

- **1**: provider API errors (`update`), download errors (`materialize`).
- **3**: manifest invalid, seed jar missing, malformed `.pw.toml` side, `side_overrides.toml` missing, dependency cycle, `materialize` hash mismatch, `materialize` side unresolved, `acknowledge`/`materialize` drift/tamper.
- **4**: `update` with `no_version = "fail"` and losses.
- **6**: `update` with `no_version = "ask"` and losses; `acknowledge` or `materialize` on unacknowledged run.

`update` state outcomes map to codes per §8.2.

### 16.3 Prod daemon

| Code | Meaning |
|---|---|
| 20 | Release verification blocked |
| 21 | Activation failed |
| 22 | Deploy command failed |
| 23 | Post-deploy verification failed |
| 24 | Recovery/rollback failed |
| 25 | Concurrent deployment lock held |
| 26 | Registry chain verification failed |

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
bot_token_env = "DISCORD_BOT_TOKEN"
guild_id = "..."
channel_id = "..."
allowlist_roles = ["..."]
allowlist_users = ["..."]

[providers]
curseforge_api_key_env = "CURSEFORGE_API_KEY"
modrinth_user_agent = "minecraft-upgrade/1.3.1 (contact@example.com)"

[registry]
trust_anchor_path = "sync/packing_slips/release-index.anchor"
trusted_keys_path = "config.d/trusted-release-keys.toml"

[state]
latest_slip_path = "/var/lib/minecraft-upgrade/latest-slip"
candidate_store = "/var/lib/minecraft-upgrade/candidates"
runs_store = "/var/lib/minecraft-upgrade/runs"
ledger_path = "/var/lib/minecraft-upgrade/ledger.sqlite"
```

**`[in-house].filenames`** declares which jars in the seed are in-house mods. Filenames in the list are treated as `provider = "in-house"`, bypass provider query, and preserve their current version. The sorted filename list participates in `resolution_inputs_sha256`.

If `[in-house]` is present, `filenames` MUST be present (may be `[]`). A missing `[in-house]` table is equivalent to `filenames = []`.

### 17.2 `config.d/deploy_pack.toml` additions

```toml
[docker]
deployment_mode = "release"              # "release" | "integration" | "legacy"
packing_slip_pointer = "stable"          # pointer name or tag
# For DEV's latest container:
# deployment_mode = "integration"
# packing_slip_pointer = "latest-slip"

[daemon]
lock_path = "/var/lib/minecraft-deploy/deploy.lock"
journal_path = "/var/lib/minecraft-deploy/operation.jsonl"
release_store = "/srv/minecraft/releases"
staging_root = "/srv/minecraft/staging"
world_backup_root = "/srv/minecraft/backups/world"
```

### 17.3 Secrets

- Provider API keys: DEV-host secret store.
- Discord bot token: DEV.
- Release-signing private key: DEV.
- Approval-signing private key: DEV.
- RCON password: existing `secrets/rcon_password`.
- SSH keys for rsync transport: on prod (initiating pull) and DEV (allowing read-only access).
- No secrets in manifests, slips, bundles, `www/`, or logs.

### 17.4 Naming

| Context | Form |
|---|---|
| CLI flags | kebab-case |
| TOML keys/sections | snake_case |
| `.env` keys | snake_case |

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

| ID | Test | Required pass condition |
|---|---|---|
| T01 | Provider-isolated production deployment | Deploy a valid release with Modrinth/CurseForge credentials absent and endpoints blocked. All artifacts validate; no provider API is contacted. |
| T02 | Tag and pointer integrity | Retarget a signed test tag, alter its registry entry, create an invalid channel reference, and create ambiguous/malformed pointer cases. All invalid cases fail closed. |
| T03 | Filename collision | Attempt to stage two different byte sequences under the same artifact filename. The resolver rejects the collision or generates a deterministic collision-safe filename before changing any existing artifact. |
| T04 | Dirty working tree | Release and integration deployments reject dirty deployment inputs. A clean integration run accepts a newer committed HEAD while continuing to use the selected candidate JAR set. |
| T05 | Partial transfer | Interrupt rsync after partial transfer. The staging directory cannot be selected for deployment; the previous active release remains untouched. |
| T06 | Approval invalidation | Change candidate hash, runtime, snapshot ID, or required evidence independently. Promotion/deployment is denied until applicable validation and approval are repeated. |
| T07 | World-aware rollback | Deploy a test upgrade that changes a simulated world-format sentinel and then fails health checks. Verify the prior world snapshot, runtime, and exact prior artifact inventory are restored together. |
| T08 | Interrupted deployment | Inject failure during file copy, rename, permission change, hash verification, and service start. Final state matches either previous release or fully validated new release; never a mixed active state. |
| T09 | Runtime payload offline | Start a candidate with provider credentials absent and egress to Modrinth/CurseForge blocked. Runtime and all artifacts are available; recorded hashes match. |
| T10 | Registry chain integrity | Truncate, fork, or alter a historical registry entry. Production rejects the chain and refuses to deploy. |
| T11 | Approval replay and expiry | Replay the same Discord interaction; submit an expired approval; change each identity field. Stale or duplicate actions are rejected or return the previously recorded result. |
| T12 | Config merge semantics | Test additions, replacements, target-only files, explicit deletions, protected paths, and path-pattern edge cases against a golden fixture. |
| T13 | KubeJS delete semantics | Seed the target with stale scripts, nested directories, and a local-only file. After shipping, only files from the captured source tree remain. |
| T14 | Client pack reproducibility | Build the ZIP twice from identical inputs; compare hashes. Test side filtering. Enumerate ZIP members and compare every hash with its manifest. |
| T15 | Daemon API auth and idempotency | Submit forged, unauthorized, and replayed requests. All invalid cases fail; repeated valid requests return the original result. |
| T16 | Concurrent deployment lock | Start two deployment requests simultaneously. One proceeds; the other is rejected or queued without corrupting state. |
| T17 | Shared-mod shipping | Seed every case in a test `mods/` directory: enabled JAR in slip, enabled JAR not in slip, stale JAR, `.jar.disabled`, nested directory, non-JAR file. Assert exact preflight decision and resulting inventory. |
| T18 | Config/KubeJS rollback | Deploy a release that changes config and KubeJS, then roll back. Previous approved bundle's config and KubeJS are restored exactly. |
| T19 | Health-check failure recovery | Simulate startup timeout, missing RCON response, repeated container restarts, fatal logs, and proxy backend failure. Daemon halts or rolls back per contract. |
| T20 | Migration rollback drill | Run the migration rollback on a production-like clone. Old command path, artifact inventory, and active release are restorable without data loss. |
| T21 | Release bundle immutability | Attempt to republish an existing release ID with changed content. Publication fails; existing bundle remains untouched. |
| T22 | Bundle manifest verification | Alter any file in a published bundle. Prod rejects the bundle and refuses to deploy. |
| T23 | Pointer ambiguity | Create a state where both an alias file and a tag share a name. Alias wins. Malformed or path-traversing pointers fail closed. |
| T24 | In-house build registration | Build an in-house mod; verify it enters `sync/downloads/`, appears as a unified `[[jar]]` entry, and passes the same validation as provider artifacts. |
| T25 | Publication precondition failure | Attempt to publish with a missing artifact, mismatched hash, or invalid approval. Publication aborts; no partial bundle is written. |
| **T26** | **Four-command lifecycle** | `update` → `acknowledge` → `materialize` → `iterate` completes successfully. Each command exits with documented code; each prints correct next command. |
| **T27** | **`no_version = "drop"` skips acknowledgment** | `update` with losses and `drop` → exit 0, state=resolved. `materialize` proceeds directly. No `acknowledgment.json` written. |
| **T28** | **`no_version = "fail"` halts at update** | `update` with losses and `fail` → exit 4, state=failed. No slip sealed; `materialize` refuses (exit 3). |
| **T29** | **Materialize detects drift** | After `update`, modify manifest, seed, or side overrides. `materialize` fails with exit 3 (report hash or `resolution_inputs_sha256` mismatch). |
| **T30** | **Staging isolation** | `materialize` downloads to `runs/<run-id>/staging/`; publishes to warehouse; does not write to `.index/`. Only `promote` writes to `.index/`. |
| **T31** | **In-house bypass** | In-house mods in `[in-house].filenames` are not queried against providers. Their current version is preserved. Their `jar_sha256` in the slip matches the warehouse artifact. |
| **T32** | **`expected_hashes` multi-algorithm verification** | Materialize verifies every listed digest (sha512, sha1, md5 as applicable). Any mismatch → exit 3. Slip's `sha256` is locally computed independently. |
| **T33** | **Side precedence chain** | Override beats jar metadata; jar metadata beats provider; provider beats `.pw.toml`. All five scenarios tested. |
| **T34** | **Filename collision at warehouse** | Attempt to materialize artifact with same filename as warehouse artifact but different SHA-256. Exit 3 without overwriting. Same SHA-256 reuses without copy. |
| **T35** | **`resolution_inputs_sha256` stability** | Formatting-only edits to manifest or `side_overrides.toml` produce identical fingerprint. Semantic edits change it. |

---

## 21. Acceptance Criteria

The system is acceptable for production use only when:

1. Candidate runtime read from effective Compose; not duplicated.
2. Resolution follows RELEASE → BETA → authorized ALPHA per `[policy].selection`.
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
33. Four-command resolver lifecycle (`update` → `acknowledge` → `materialize` → `iterate`) works end-to-end.
34. State machine transitions correctly per §8.2.
35. `resolution_inputs_sha256` is stable across formatting-only changes.
36. `materialize` performs full integrity checks.
37. Staging isolation preserves active `.index/`.
38. In-house mods identified via `[in-house].filenames` and bypass provider query.

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
- SMB/NFS transports are not supported in v1.3.1; rsync over SSH is required.
- Provider hash algorithms vary (sha512/sha1/md5); SHA-256 always computed locally.
- In-house identification is transitional via `[in-house].filenames`; retired when slice 13 introduces `.pw.toml` marker.
- `iterate` requires prior `materialize`; no combined mode.
- CurseForge-sourced mods always require overrides (no side field in provider metadata).
- The full log-classifier pattern catalog evolves after interface freeze; its status model and fix-application permissions are fixed.
- In-house build toolchain image is selected during implementation; its identity, source commit, output artifacts, dependencies, and reproducibility record are fixed now.
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
| Side policy | `config.d/side_overrides.toml` |
| Selection policy | `[policy].selection` |
| Loss policy | `[policy].no_version` |
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
| `.pw.toml` writer | `modpack_upgrade.pw_toml` (§5.7) |
| Slip-mode source sets | `SlipContext` (§5.8) |
| Side precedence | `docs/Side_Determination.md` |
| Protect-pattern matching | `fnmatch.fnmatch`, two scopes (§13.5) |
| Shipping transaction | `modpack_upgrade.shipping` (§13.2) |
| Instance/container mapping | `deploy_pack.toml` + Compose |
| Deployment interface | Release bundle + slip |
| Production lifecycle | `deploy_pack` v2 |
| World migration | Isolated test copy of verified snapshot |
| World rollback | Pre-upgrade world backup + previous release bundle |
| Velocity topology | Isolated DEV proxy; prod proxy if present |
| Final promotion | Human-committed git tag + `stable` update |
| Publication | Explicit `minecraft-upgrade publish` |

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
- **Deploy** — activate staged release; invoke `deploy-pack --release <tag>`.
- **Iteration** — one boot attempt after candidate artifact change.
- **Cap** — hard stop on automated resolution.
- **Ambiguous failure** — classifier cannot confidently fix; pause.
- **Legacy baseline** — `v1.0.0`, produced at migration time, `release_mode = "update"`.
- **Provenance metadata** — `provider` / `project_id` / `version_id` / `provider_endpoint`. Record origin; do not drive prod fetches.
- **`latest-slip`** — DEV-local candidate pointer.
- **`stable`** — Git-tracked pointer consumed by production.
- **Operation journal** — durable record of deployment stage transitions.
- **Rollback journal** — durable record of prior managed file state.
- **Bundle manifest** — signed inventory of every file in a release bundle.
- **`resolution_inputs_sha256`** — canonical semantic fingerprint of resolver inputs.
- **`expected_hashes`** — provider-supplied hashes (any algorithm) for download verification; distinct from slip's locally-computed `sha256`.

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
| Publish | Create immutable bundle. |
| Pull (DEV→Prod) | rsync-over-SSH pull from DEV publication dir. |
| Deploy | `deploy-pack --release <tag>` after acquiring deployment lock. |
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
| `.pw.toml` writer | One external provider block or zero for in-house. `side = "unknown"` sentinel. Byte-stable. |
| Shipping | Logically atomic transaction. |
| Protect patterns | `fnmatch.fnmatch`. |
| Rollback | Previous approved bundle + pre-upgrade world backup (for upgrades). |
| Concurrent deployment | Single exclusive lock. |
| Interrupted deployment | Recovery from journal or halt. |
| Approval | Signed, bound to immutable identity including `resolution_report_sha256`, expires after seven days. |
| Runtime startup | Uses staged payload. No silent downloads. |
| Side determination | Override > jar metadata > provider > `.pw.toml`. Exit 3 if all unknown. |
| In-house mods | `[in-house].filenames`; bypass provider; preserve version; `jar_sha256` or `build_id`. |
| `expected_hashes` verification | Multi-algorithm; any mismatch → exit 3. |
| Filename collision | Same SHA-256 → reuse; different → exit 3. |

---

**End of specification v1.3.1.**

---

## Pending before production cutover

| Deliverable | Status |
|---|---|
| Slice 7b implementation (resolver, provider adapters, four CLI commands) | Not started |
| `docs/Manifest_Schema.md` | Frozen (rev e) |
| `docs/Resolution_Report.md` | Frozen (rev e) |
| `docs/Side_Determination.md` | Frozen (rev e) |
| Migration runbook | Not written |
| Prod daemon API contract | Not frozen |
| Log classifier design | Not written; interface fixed |
| DEV Compose project spec | Not written |
| `deploy_pack` refactor plan | Not written |
| In-house build toolchain spec | Not written; record format fixed |
| rsync-over-SSH warehouse contract | Not written |
| Release registry and trust-anchor operational procedure | Not written |
| OCI registry allowlist and image pinning procedure | Not written |
| World backup and restoration drill | Not written |
| All §20 acceptance tests | Not fully implemented |
| Verification that existing SMB config is truly inactive | Not verified |

**Design freeze is complete.** Implementation is authorized against §20 and the frozen v1.3.1 contract. Production cutover requires all §20 tests passing and the migration runbook exercised on DEV.