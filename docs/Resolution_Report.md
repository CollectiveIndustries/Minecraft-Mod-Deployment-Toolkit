# docs/Resolution_Report.md

**Status:** Frozen as spec v1.3.1 (slice 7a, rev f)
**File:** `www/runs/<run-id>/resolution-report.toml`
**Author:** Resolver (written by `update`)
**Consumer:** `acknowledge`, `materialize`, Discord link, human review

**Rev e changes:** `expected_hashes` array replaces `expected_sha256`. `unmarked` removed (no such array; sides are provisional and non-preflight). In-house identity documented. State values align with Manifest_Schema §4. §3.2 clarifies that `[[seed]].jar_sha256` is the sole in-house identity when `build_id` is absent. §6.5 defensive drop check covers `[[cascade]]`.

**Rev f changes:** §3.1 marked `[[conflict]]` as reserved for v1.3.2. The v1.3.1 resolver does not detect conflicts; the section is schema-frozen but always absent. No behavioral change.

---

## §1. Purpose

Resolution report records what `update` resolved and lost. Evidence for
the human, input to `acknowledge` and `materialize`, reference for Phase
B Discord. Contains **provisional sides**; final sides are in the slip.

---

## §2. Seed and warehouse

### §2.1 Seed

`sync/downloads/.index/*.pw.toml`. Git-tracked. One entry per
`.pw.toml`. Each references a jar via `filename`; jar MUST exist in
`sync/downloads/`, else exit 3 pre-report.

### §2.2 Warehouse

`sync/downloads/*.jar`. Cache + active. Jars with no `.pw.toml` in
`.index/` are cache, not seed. Ignored.

### §2.3 Side acceptance in seed

Seed `.pw.toml` `side` values:

| Value | Treatment |
|---|---|
| `"client"` / `"server"` / `"both"` | Valid; used in precedence chain |
| `"unknown"` | Valid; means "writer didn't know"; precedence chain attempts resolution |
| absent | Valid; same as `"unknown"` |
| anything else (`42`, `["client"]`, etc.) | Schema error → exit 3 pre-report |

**No preflight rejection for `unknown` or absent.** No `[[unmarked]]`
array exists in the report. Side resolution happens at materialize.

---

## §3. Report structure

Top-level scalars and tables required unless marked optional. Repeated
sections optional (empty resolution = no entries).

```toml
schema_version = 1
run_id = "a4f3c2e1-7b82-4c1a-9e3f-1d2a3b4c5d6e"
generated_at_utc = "2026-10-17T14:23:11Z"
status = "paused_awaiting_acknowledgment"

[inputs]
manifest_sha256 = "abc123..."
resolution_inputs_sha256 = "def456..."
side_overrides_sha256 = "789abc..."
release_mode = "upgrade"

[target_runtime]
minecraft_version = "1.21.1"
loader = "forge"
loader_version = "52.0.1"

[policy]
selection = "release_then_beta"
no_version = "ask"

# External provider seed entry
[[seed]]
filename = "create-1.20.1-6.0.8.jar"
provider = "modrinth"
project_id = "create"
current_version_id = "abc123"      # present for external; omitted for in-house
jar_sha256 = "1a2b3c..."
side_declared = "both"
side_effective = "server"

# In-house seed entry (no external identity)
[[seed]]
filename = "custom-mod-1.0.0.jar"
provider = "in-house"
# project_id: omitted — no external project
# current_version_id: omitted — no external version
build_id = "build-2026-10-15-abc"  # optional; present only when known
jar_sha256 = "9d8e7f..."
side_declared = "unknown"
side_effective = "unknown"

# External provider resolved entry
[[resolved]]
filename = "create-1.21.1-7.0.1.jar"
provider = "modrinth"
project_id = "create"
version_id = "def456"              # present for external; omitted for in-house
is_in_house = false
upgraded_from_version_id = "abc123"  # optional; omitted for new additions
is_dependency_only = false
side_provisional = "server"
side_source_provisional = "override"

expected_hashes = [
  { algorithm = "sha512", value = "4d5e6f..." },
  { algorithm = "sha1", value = "7a8b9c..." },
]

# In-house resolved entry (version preserved from seed)
[[resolved]]
filename = "custom-mod-1.0.0.jar"
provider = "in-house"
# project_id: omitted
# version_id: omitted
build_id = "build-2026-10-15-abc"  # optional; present only when known
is_in_house = true
# upgraded_from_version_id: omitted (in-house is not "upgraded" via provider)
is_dependency_only = false
side_provisional = "unknown"
side_source_provisional = "unknown"
# expected_hashes: omitted for in-house unless a build record supplies them

[[lost]]
filename = "create-new-age-1.20.1-2.0.jar"
provider = "modrinth"
project_id = "create-new-age"
reason = "no_compatible_version"
detail = "no release or beta for MC 1.21.1 + forge 52.0.1"

[[cascade]]
filename = "cna-addon-1.20.1-1.5.jar"
provider = "modrinth"
project_id = "cna-addon"
reason = "dependency_unsatisfiable"
dependency_chain = ["create-new-age", "cna-addon"]

[[conflict]]
project_id = "some-mod"
candidates = [
  { version_id = "v1", filename = "some-mod-1.0.jar", source = "seed" },
  { version_id = "v2", filename = "some-mod-2.0.jar", source = "dependency" },
]
resolution = "seed_version_preferred"

[diagnostics]
errors = []
warnings = [
  "side_overrides.toml has 3 entries for mods no longer in the pack",
]
provider_requests_made = 143
```

### §3.1 Enumerated values

- `status` ∈ `"complete"` | `"paused_awaiting_acknowledgment"` | `"failed"`
- `release_mode` ∈ `"update"` | `"upgrade"`
- `[[resolved]].side_provisional` ∈ `"client"` | `"server"` | `"both"` | `"unknown"`
- `[[resolved]].side_source_provisional` ∈ `"override"` | `"pw_toml"` | `"provider"` | `"unknown"`
- `[[lost]].reason` ∈ `"no_compatible_version"` | `"dependency_unsatisfiable"`
- `[[cascade]].reason` = `"dependency_unsatisfiable"`
- `[[conflict]].resolution` ∈ `"seed_version_preferred"` | `"blocked"`
- `expected_hashes[].algorithm` ∈ `"sha512"` | `"sha256"` | `"sha1"` | `"md5"`

> **`[[conflict]]` is reserved for v1.3.2.** The v1.3.1 resolver does
> not detect conflicts — a project is enqueued once and short-circuits
> on any subsequent visit, so two different pinned versions of the same
> project cannot currently race. `[[conflict]]` is therefore **always
> absent** from v1.3.1 reports. The schema above (including the
> `resolution` enum and the `candidates[]` shape) is frozen so that
> future version-pinning support can emit entries without a schema
> bump. Writers MUST NOT emit `[[conflict]]` in v1.3.1; readers accept
> it defensively (see `report._conflict_from_dict`) so a future
> producer does not require a reader upgrade. Consumers that key on
> `[[conflict]]` should treat absence as "no conflict detected", not as
> "conflict detection skipped."

### §3.2 Absent optional fields

TOML has no `null`. Omit absent fields:

**External-provider entries (`provider ∈ {modrinth, curseforge}`):**
- `[[seed]].current_version_id` — required
- `[[resolved]].version_id` — required
- `[[resolved]].expected_hashes` — omitted when provider supplies none
- `[[resolved]].upgraded_from_version_id` — omitted for new additions

**In-house entries (`provider == "in-house"`):**
- `[[seed]].project_id` — omitted (no external project)
- `[[seed]].current_version_id` — omitted (no external version)
- `[[seed]].build_id` — omitted unless a build record supplies one
- `[[resolved]].project_id` — omitted
- `[[resolved]].version_id` — omitted
- `[[resolved]].build_id` — omitted unless known; the corresponding
  `[[seed]].jar_sha256` is the sole identity when `build_id` is absent.
- `[[resolved]].upgraded_from_version_id` — omitted
- `[[resolved]].expected_hashes` — omitted unless build record supplies

**Unconditional:** `[[conflict]]` and `[[cascade]]` sections omitted
when empty; `[diagnostics].errors`, `.warnings` omitted when empty.

### §3.3 `expected_hashes`

Provider-supplied hashes, algorithm-tagged. Includes **all** digests the
provider returned, not a single chosen algorithm:

- Modrinth: usually SHA-512 and SHA-1
- CurseForge: SHA-1 and MD5

`materialize` verifies every listed digest against the downloaded bytes.
If any fails → exit 3.

If no `expected_hashes` are present (provider returned none), materialize
proceeds and logs a warning. The slip's `[[jar]].sha256` is the
locally-computed SHA-256 of the downloaded bytes — independent of
provider claims, always computed by materialize, always present in the
sealed slip.

The slip's `[[jar]].sha256` is the canonical artifact identity. Provider
hashes are secondary integrity checks that reduce the risk of a bad
download before we trust our own computation. They are not a substitute
for computing SHA-256 ourselves.

---

## §4. Deterministic ordering

| Array | Sort key |
|---|---|
| `[[seed]]` | `(project_id_or_filename, filename)` |
| `[[resolved]]` | `(project_id_or_filename, filename)` |
| `[[lost]]` | `(project_id_or_filename, filename)` |
| `[[cascade]]` | `(project_id_or_filename, filename)` |
| `[[conflict]]` | `project_id` |
| `[[conflict]].candidates` | `version_id` |
| `[[cascade]].dependency_chain` | root first, this mod last |
| `expected_hashes` | `algorithm` |
| `[diagnostics].errors`, `.warnings` | lexicographic |

For in-house mods (`project_id` absent), sort by `filename`.

Sorting is for diffs, not hash reproducibility. Reports carry `run_id`
and `generated_at_utc`.

---

## §5. Report hash

SHA-256 of report raw bytes. Uses:

- **Integrity:** `update` records it in state. **Both** `acknowledge` and
  `materialize` verify it before proceeding.
- **Binding:** Phase B approval identity (spec §8.4). Any alteration
  invalidates the approval.

Not a reproducible content hash. For reproducible identity of semantic
inputs, use `resolution_inputs_sha256`.

---

## §6. Run state and acknowledgment

DEV-local files under `/var/lib/minecraft-upgrade/runs/<run-id>/`.

### §6.1 `state.toml`

Written by `update`.

```toml
run_id = "a4f3c2e1-..."
status = "awaiting_acknowledgment"
release_mode = "upgrade"
effective_compose_sha256 = "..."   # only if release_mode="update"
paused_at_utc = "2026-10-17T14:23:11Z"
report_sha256 = "0a1b2c..."
resolution_inputs_sha256 = "3d4e5f..."
manifest_path = "/srv/minecraft/sync/modpack.manifest.toml"
```

`status` ∈ `"resolved"` | `"awaiting_acknowledgment"` | `"acknowledged"` | `"materialized"` | `"failed"`.

State transitions and acceptance rules match Manifest_Schema §4.

### §6.2 `acknowledgment.json`

Written by `acknowledge`. Only exists for runs that required
acknowledgment.

```json
{
  "schema_version": 1,
  "run_id": "a4f3c2e1-...",
  "report_sha256": "0a1b2c...",
  "resolution_inputs_sha256": "3d4e5f...",
  "acknowledged_losses": [
    {"project_id": "create-new-age", "reason": "no_compatible_version"},
    {"project_id": "cna-addon", "reason": "dependency_unsatisfiable"}
  ],
  "acknowledged_by": "interactive-cli",
  "acknowledged_at_utc": "2026-10-17T14:31:52Z"
}
```

### §6.3 Shared integrity checks

Both `acknowledge` and `materialize` perform the following, in order,
before proceeding:

1. Load `state.toml`.
2. Load report from `www/runs/<run-id>/resolution-report.toml`.
3. SHA-256 of report bytes vs. `state.report_sha256`. Mismatch → exit 3.
4. Recompute `resolution_inputs_sha256` from current inputs. Mismatch →
   exit 3.
5. If `state.release_mode == "update"`: recompute effective Compose
   SHA-256 vs. `state.effective_compose_sha256`. Mismatch → exit 3.

These are the "report and inputs unchanged" guarantees. They apply to
every path, whether or not the run required acknowledgment.

### §6.4 `acknowledge` verification

For runs with `state.status == "awaiting_acknowledgment"`:

1. Run §6.3 integrity checks.
2. Present losses. Require explicit acknowledgment, or accept with
   `--accept-losses`. Non-interactive without `--accept-losses` → exit 6.
3. Write `acknowledgment.json`.
4. Update `state.status = "acknowledged"`.
5. Print `next: materialize --run-id <run-id>`. Exit 0.

`acknowledge` is a **human-decision gate**. The integrity checks are a
side effect — `materialize` performs them independently, so the run is
protected even when `acknowledge` is skipped.

### §6.5 `materialize` verification

For runs with `state.status` ∈ `{resolved, acknowledged}`:

1. Run §6.3 integrity checks.
2. If `state.status == "resolved"` and either `[[lost]]` or `[[cascade]]`
   is non-empty → confirm `[policy].no_version == "drop"` in the report.
   Otherwise refuse with exit 3 (should be impossible if `update`
   dispatched correctly; defensive).
3. Proceed with materialization (§Spec_Amendments A5.2).

---

## §7. Excluded content

No credentials, tokens, signed URLs, environment values, or raw provider
response bodies. Only fields the resolver uses.

---

## §8. Storage model

```
sync/downloads/
├── *.jar                          # warehouse (cache + active)
└── .index/*.pw.toml               # git-tracked seed source

www/runs/<run-id>/                 # DEV-local; published for Discord link
└── resolution-report.toml

/var/lib/minecraft-upgrade/
├── runs/<run-id>/
│   ├── state.toml
│   ├── acknowledgment.json         # absent if not required
│   └── staging/                    # materialize downloads here first
└── candidates/<run-id>/
    ├── metadata/*.pw.toml          # candidate-only; published to .index/ by promote
    └── packing_slip.toml           # DEV-local; copied to release registry by promote
```

`.index/` is git-tracked. Warehouse jars are not. `runs/` and
`candidates/` are DEV-local. `www/runs/` is DEV-local, published for
link access only.

---

## §9. Version history

| Version | Change |
|---|---|
| v1.3.1 rev c | `expected_hashes` array. `unmarked` removed. In-house identity. Staging. State alignment. |
| v1.3.1 rev d | Dual external/in-house examples. §3.2 per-provider optional fields. §6.5 defensive drop check covers `[[cascade]]`. |
| v1.3.1 rev e | §3.2 clarifies `[[seed]].jar_sha256` is sole in-house identity when `build_id` absent. |
| v1.3.1 rev f | §3.1 marks `[[conflict]]` reserved for v1.3.2. No behavioral change; the section was always absent in v1.3.1. |

---

**End of Resolution Report.**