# docs/Spec_Amendments_v1.3.1.md

**Status:** Ready to apply to `Upgrade_System_specifications.md`
**Target:** v1.3.0 → v1.3.1
**Scope:** slice 7a, rev e

**Rev e changes:** A10 wording corrected to a single conditional (present table requires filenames; absent table equivalent to empty). A5.2 pipeline summary distinguishes external (expected_hashes) and in-house (jar_sha256) verification. A2.2 clarified that `[[seed]].jar_sha256` is the sole in-house identity when `build_id` is absent.

Every amendment names the section, states what changes, and gives the exact replacement text or the new sub-section. No code changes during 7a; these amendments are the contract slice 7b implements against.

---

## A1. §5.2 — slip schema

### A1.1 `[fingerprints]`

**Current:**

```toml
[fingerprints]
manifest_sha256      = "..."
runtime_sha256       = "..."
server_inputs_sha256 = "..."
client_inputs_sha256 = "..."
```

**Replace with:**

```toml
[fingerprints]
manifest_sha256          = "..."  # raw bytes of modpack.manifest.toml
resolution_inputs_sha256 = "..."  # semantic resolver input fingerprint
runtime_sha256           = "..."  # canonical [runtime] descriptor
server_inputs_sha256     = "..."  # canonical server-stage input manifest
client_inputs_sha256     = "..."  # canonical client-stage input manifest
```

### A1.2 `[[jar]].dependencies[].source`

**Current values:** `"pw_toml" | "jar_manifest"`

**Add:** `"provider_metadata"`

**Full enum:** `"pw_toml" | "jar_manifest" | "provider_metadata"`

- `pw_toml`: edge declared in a `.pw.toml`
- `jar_manifest`: edge declared in the jar's `mods.toml` / `neoforge.mods.toml` / `fabric.mod.json`
- `provider_metadata`: edge declared by a provider where no `.pw.toml` representation exists (CurseForge `Tool` / `Include`)

### A1.3 `[[jar]].dependencies[].type`

**Current:** free-form string.

**Constrain to:** `"required" | "optional" | "incompatible" | "embedded" | "tool" | "include"`

Mapping from provider relation types:

| Provider | Provider value | Slip `type` |
|---|---|---|
| Modrinth | `required` / `optional` / `incompatible` / `embedded` | identity |
| CurseForge | 1 (`EmbeddedLibrary`) | `embedded` |
| CurseForge | 2 (`OptionalDependency`) | `optional` |
| CurseForge | 3 (`RequiredDependency`) | `required` |
| CurseForge | 4 (`Tool`) | `tool` |
| CurseForge | 5 (`Incompatible`) | `incompatible` |
| CurseForge | 6 (`Include`) | `include` |

`.pw.toml` writer emits only `required` / `optional` / `incompatible` / `embedded`. `tool` and `include` preserved in slip with `source = "provider_metadata"`, not written to `.pw.toml`.

---

## A2. §5.4 — fingerprints and release mode

### A2.1 Fingerprint table

**Retire:** `side_policy_sha256`

**Add:** `resolution_inputs_sha256`

**New table:**

| Fingerprint | Source |
|---|---|
| `content_sha256` | Canonical slip body |
| `manifest_sha256` | Raw `modpack.manifest.toml` bytes |
| `resolution_inputs_sha256` | Canonical semantic resolver input snapshot (§A2.2) |
| `runtime_sha256` | Canonical JSON of resolved `[runtime]` descriptor |
| `server_inputs_sha256` | Canonical manifest of server-stage input files |
| `client_inputs_sha256` | Canonical manifest of client-stage input files |

`side_policy_sha256` is retired; its inputs are covered by
`side_overrides` inside `resolution_inputs_sha256` (§A2.2).

### A2.2 `resolution_inputs_sha256` calculation

**Semantic, not raw.** The fingerprint captures what the resolver consumed
and acted on. Raw-file identity is recorded separately in the report and
slip.

**Calculation:**

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
      # provider-conditional identity:
      #   external → "current_version_id": <str>
      #   in-house → "build_id": <str>  (if known)
      #   in-house → "build_id" omitted if unknown; jar_sha256 alone is identity
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

Notes:

- `in_house_filenames` comes from `config.d/upgrade.toml
  [in-house].filenames`, sorted. Present even when empty.
- In-house seed entries **omit** `current_version_id` (no external
  identity). `build_id` present only when the config or slice 13 build
  records supply one. `jar_sha256` is the sole identity when neither is
  available.
- Raw file hashes (`manifest_sha256`, `side_overrides_sha256`) are NOT in
  the snapshot. Formatting-only edits do not change this fingerprint.
- `jar_sha256` requires the seed jar. Missing jar = exit 3 before
  fingerprint.
- Ordering: `manifest_sha256` first, then `resolution_inputs_sha256`,
  then slip body, then `content_sha256`.

---

## A3. §8.2 Phase A — four-command lifecycle

Phase A is split across four commands. Each is documented in §A5.

**Command sequence:**

```
update --release-mode <mode>
    → resolve versions
    → write resolution report (provisional sides)
    → write state.toml
    → exit per policy
      0: no losses or drop; next: materialize
      6: ask with losses; next: acknowledge
      4: fail with losses
      3: preflight error

acknowledge <run-id>
    → verify report + inputs + Compose
    → present losses, require acknowledgment
    → write acknowledgment.json
    → exit
      0: next: materialize
      3: drift/tamper
      6: unacknowledged pause

materialize --run-id <run-id>
    → verify report + inputs + Compose
    → for external: download to staging; verify every expected_hashes
      digest; compute local SHA-256 independently
    → for in-house: source from warehouse; verify jar_sha256 against seed
    → publish to warehouse (subject to filename collision policy)
    → inspect jar metadata; final side
    → seal slip → DEV-local candidates/<run-id>/
    → state=materialized
    → exit
      0: next: iterate
      1: network/operational
      3: hash mismatch or side unresolved

iterate --run-id <run-id>
    → boot loop on finalized candidate
    → (existing Phase A step 16 onward)
```

### A3.1 Update contract

`update` writes the report and state only. It does NOT download artifacts,
does NOT assemble the slip, does NOT determine final sides.

See §A5.1 for full contract.

### A3.2 Materialize contract

`materialize` verifies, downloads, determines final sides, and seals the
slip. See §A5.2.

### A3.3 Iterate contract

`iterate` boots the finalized candidate. See §A5.3.

### A3.4 Acknowledge contract

`acknowledge` is the human-decision gate. See §A5.4.

---

## A4. §8.4 Phase B — approval identity

Add `resolution_report_sha256` to the approval-bound identity set:

```
approval_binds_to = {
  candidate_content_sha256:   <slip content hash>,
  runtime_sha256:             <runtime descriptor hash>,
  server_inputs_sha256:       <server inputs hash>,
  client_inputs_sha256:       <client inputs hash>,
  snapshot_id:                <world snapshot id>,
  validation_report_sha256:   <existing>,
  resolution_report_sha256:   <NEW — hash of resolution report>
}
```

Alteration of any of these invalidates the approval.

---

## A5. §15.1 — CLI commands

**Add to command table:**

| Command | Responsibility |
|---|---|
| `update --release-mode <mode>` | Resolve versions. Write report + state. |
| `acknowledge <run-id>` | Verify + record loss acceptance. |
| `materialize --run-id <run-id>` | Verify + download + seal slip. |
| `iterate --run-id <run-id>` | Boot finalized candidate. |
| `promote <tag>` | Copy candidate slip + metadata into release registry. |

### A5.1 `update` contract

```
minecraft-upgrade update --release-mode {update|upgrade} [--config-dir PATH]
```

Behavior:

1. Read manifest; validate; if `--release-mode update`, verify manifest
   matches Compose.
2. Read `config.d/upgrade.toml [in-house].filenames`.
3. Read seed from `.index/*.pw.toml`. Verify each jar exists. Verify each
   `.pw.toml` side is valid (in `{client, server, both, unknown}` or
   absent; malformed → exit 3).
4. Read `side_overrides.toml` (missing → exit 3).
5. Compute `manifest_sha256`.
6. Compute `resolution_inputs_sha256` (§A2.2).
7. For each seed entry:
   a. If provider == "in-house":
      - Skip provider query.
      - Preserve current version (source: seed jar in warehouse).
      - Confirm `jar_sha256` matches warehouse bytes. Mismatch → exit 3.
   b. Else (external provider):
      - Query provider for versions declaring target MC + loader.
      - Select version per `selection`.
      - If no compatible version → `[[lost]]` reason="no_compatible_version".
8. Compute provisional sides (override > provider > current `.pw.toml`).
9. Walk dependency closure; cascade-drop dependents.
10. Detect cycles → exit 3.
11. Detect conflicts.
12. Write `www/runs/<run-id>/resolution-report.toml`.
13. Write `/var/lib/minecraft-upgrade/runs/<run-id>/state.toml` with:
    - state per policy (see §A6)
    - `effective_compose_sha256` if `--release-mode update`
14. Print next command. Exit per §A6.

Exit codes: 0, 6, 4, 3, 1.

Next command output:

- exit 0 (`resolved`): `next: materialize --run-id <id>`
- exit 6 (`awaiting_acknowledgment`): `next: acknowledge <id>`
- exit 4 (`failed`): `error: resolution failed with N losses`
- exit 3: `error: <reason>`

### A5.2 `materialize` contract

```
minecraft-upgrade materialize --run-id <run-id>
```

Behavior:

1. Load `state.toml`.
2. Verify `status ∈ {resolved, acknowledged}`:
   - `awaiting_acknowledgment` → exit 6 (`must acknowledge first`)
   - `failed` → exit 3 (`run failed`)
   - `materialized` → exit 0 (already done) with
     `next: iterate --run-id <id>`
3. Load report.
4. **Integrity checks** (identical to acknowledge):
   a. SHA-256 of report vs. `state.report_sha256`. Mismatch → exit 3.
   b. Recompute `resolution_inputs_sha256`. Mismatch → exit 3.
   c. If `release_mode == "update"`: Compose vs.
      `state.effective_compose_sha256`. Mismatch → exit 3.
5. Prepare staging: `/var/lib/minecraft-upgrade/runs/<run-id>/staging/`.
6. For each `[[resolved]]`:
   a. If `is_in_house == true`:
      - Source artifact from `sync/downloads/<filename>` (warehouse).
        Missing → exit 3.
      - Verify SHA-256 of warehouse bytes matches `[[seed]].jar_sha256`
        for the corresponding seed entry. Mismatch → exit 3.
      - If `expected_hashes` present (unusual for in-house): compute and
        compare each. Mismatch → exit 3.
      - Copy to staging (for consistency with the external path).
      - Continue to step 7.
   b. Else (external):
      - Download artifact to staging.
      - For each `expected_hashes[]`: compute and compare. Mismatch →
        exit 3 (report all mismatches).
      - Compute SHA-256 of downloaded bytes. Store for slip.
      - Continue to step 7.
7. For each resolved artifact:
   a. Copy from staging to `sync/downloads/<filename>`. Handle collision
      per §A5.2.1.
   b. Resolve final side via Side_Determination chain (jar bytes now
      present, overrides win).
   c. If side `unknown` → record; continue collecting.
8. If any side `unknown` → exit 3 (`add override and re-run`).
9. Write `.pw.toml` for each resolved jar to
   `/var/lib/minecraft-upgrade/candidates/<run-id>/metadata/<filename>.pw.toml`.
   (Never write to `.index/`.)
10. Assemble slip with final sides + verified SHA-256.
11. Compute `content_sha256`. Seal.
12. Write slip to `/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml`.
13. Update `state.status = "materialized"`.
14. Clean up staging dir.
15. Print `next: iterate --run-id <id>`. Exit 0.

Exit codes: 0, 1 (network), 3 (hash mismatch / side unresolved / drift /
tamper), 6 (unacknowledged).

#### A5.2.1 Filename collision handling

When a resolved artifact's `filename` matches an existing warehouse
artifact (`sync/downloads/<filename>`):

- Compute SHA-256 of existing.
- If matches resolved artifact's SHA-256: reuse existing. No copy.
- If differs: exit 3 (`filename collision: <filename> has different
  bytes in warehouse`). Do not overwrite. T03 acceptance test requires
  rejection before existing bytes are changed.

Warehouse is a cache. Failed collision = the resolver's chosen filename
conflicts with a previous candidate's. Report to the operator; they
decide whether to archive the old artifact or change the filename.

### A5.3 `iterate` contract

```
minecraft-upgrade iterate --run-id <run-id>
```

Behavior:

1. Load `state.toml`. Verify `status == "materialized"`.
2. Load slip from
   `/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml`.
3. Enter boot loop (existing §8.2 step 16).
4. Existing failure classification, batch fixes, caps.

### A5.4 `acknowledge` contract

```
minecraft-upgrade acknowledge <run-id> [--accept-losses] [--operator <id>]
```

Behavior:

1. Load `state.toml`. Verify `status == "awaiting_acknowledgment"`.
2. Run §A5.2 steps 3–4 (integrity checks).
3. Present losses to operator.
4. Require explicit acknowledgment, or `--accept-losses`. Non-interactive
   without `--accept-losses` → exit 6.
5. Write `acknowledgment.json`.
6. Update `state.status = "acknowledged"`.
7. Print `next: materialize --run-id <id>`. Exit 0.

`acknowledge` is a **human-decision gate**, not the sole integrity gate.
`materialize` performs the same integrity checks, so the run is protected
whether or not `acknowledge` was run.

### A5.5 `promote` contract

```
minecraft-upgrade promote <tag>
```

Behavior:

1. Load state and candidate slip from
   `/var/lib/minecraft-upgrade/candidates/<run-id>/`.
2. Copy slip to `sync/packing_slips/<tag>.toml` (git-tracked).
3. Copy candidate metadata `.pw.toml` files to
   `sync/downloads/.index/` (git-tracked). Only files corresponding to
   `[[resolved]]` entries; do not overwrite unrelated `.index` entries.
4. `git add sync/packing_slips/<tag>.toml sync/downloads/.index/`.
5. Print exact commit/tag/push instructions.
6. STOP. Human runs the git commands and updates `stable`.

`promote` is a staging step. It does not commit, tag, or push.

---

## A6. §16.2 — exit codes

### `minecraft-upgrade`

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

Clarifications:

- **1**: provider API errors (`update`), download errors (`materialize`).
- **3**: manifest invalid, seed jar missing, malformed `.pw.toml` side,
  `side_overrides.toml` missing, dependency cycle, `materialize` hash
  mismatch, `materialize` side unresolved, `acknowledge` / `materialize`
  drift/tamper.
- **4**: `update` with `no_version = "fail"` and losses.
- **6**: `update` with `no_version = "ask"` and losses; `acknowledge` or
  `materialize` on unacknowledged run.

### `update` state outcomes (mapping to code)

| Outcome | State | Exit |
|---|---|---|
| No losses | `resolved` | 0 |
| Losses + `drop` | `resolved` | 0 |
| Losses + `ask` | `awaiting_acknowledgment` | 6 |
| Losses + `fail` | `failed` | 4 |
| Preflight error | (none) | 3 |
| Provider error | (none) | 1 |

---

## A7. §23.1 — authority table

Add:

| Concern | Authority |
|---|---|
| Target runtime intent | `sync/modpack.manifest.toml [minecraft]` |
| Release mode | CLI `--release-mode` + state.toml |
| Seed (active pack) | `sync/downloads/.index/*.pw.toml` |
| In-house filenames | `config.d/upgrade.toml [in-house].filenames` |
| Side policy | `config.d/side_overrides.toml` |
| Selection policy | `[policy].selection` |
| Loss policy | `[policy].no_version` |
| Resolution evidence | `www/runs/<run-id>/resolution-report.toml` |
| Loss acknowledgment | `/var/lib/minecraft-upgrade/runs/<run-id>/acknowledgment.json` |
| Candidate slip | `/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml` |
| Release slip | `sync/packing_slips/<tag>.toml` |

Modify: "Manifest intent" → "Target runtime intent".

---

## A8. Version bump

Header: v1.3 → v1.3.1.

Changes:

1. Manifest schema: five fields, no per-mod policy.
2. `resolution_inputs_sha256`; `side_policy_sha256` retired.
3. Dependencies `source` and `type` enums.
4. Four-command lifecycle with state machine
   (`resolved` / `awaiting_acknowledgment` / `acknowledged` /
   `materialized` / `failed`).
5. `--release-mode` required on `update`; persisted in state.toml.
6. Resolution report: `expected_hashes` array. Report hash in approval
   identity. UUID4 run IDs.
7. Side determination at materialize; CurseForge `unknown`; Fabric
   absent → `both`; unresolved side → exit 3; `unknown` / absent
   `.pw.toml` side allowed through preflight.
8. Integrity checks at both `acknowledge` and `materialize`.
9. Staging isolation; candidate slip DEV-local; `.index/` updated by
   `promote` only.
10. In-house identification via `config.d/upgrade.toml [in-house]`;
    fingerprint uses `build_id` or `jar_sha256`, omits
    `current_version_id`.
11. Exit codes: network = 1; integrity = 3; fail = 4; ask-pause = 6.

---

## A9. §4.1 — storage model additions

**Add to `sync/` layout:**

```
sync/
├── downloads/
│   ├── *.jar                          # warehouse (cache + active)
│   └── .index/*.pw.toml               # git-tracked seed
├── packing_slips/
│   ├── stable                         # existing
│   └── <tag>.toml                     # existing release registry
```

**Add to `/var/lib/minecraft-upgrade/` layout:**

```
/var/lib/minecraft-upgrade/
├── latest-slip                        # existing
├── ledger.sqlite                      # existing
├── runs/<run-id>/                     # NEW
│   ├── state.toml
│   ├── acknowledgment.json            # absent if not required
│   └── staging/                       # transient; cleaned post-materialize
└── candidates/<run-id>/               # NEW (replaces old candidates/<candidate-id>/)
    ├── metadata/*.pw.toml             # candidate-only; .index/ updated by promote
    └── packing_slip.toml              # DEV-local; git-tracked via promote
```

Notes:

- Candidate slips are **DEV-local**. Only after `promote` do they appear
  at `sync/packing_slips/<tag>.toml` (git-tracked).
- `.index/` is git-tracked. Mutated only by `promote`, never by
  `materialize`.
- `runs/<run-id>/staging/` is transient, cleaned up after successful
  materialize.
- Old `/var/lib/minecraft-upgrade/candidates/<candidate-id>/` path in
  v1.3.0 is replaced by the `runs/` + `candidates/` split.

---

## A10. §17.1 — `config.d/upgrade.toml` additions

Add an `[in-house]` table to the `upgrade.toml` example in §17.1:

```toml
[in-house]
filenames = [
    "custom-mod-1.0.0.jar",
    "another-custom.jar",
]
```

The `[in-house].filenames` list declares which jars in the seed are
in-house mods with no external provider identity. The resolver:

- Treats these filenames as `provider = "in-house"` during resolution.
- Skips provider query for them.
- Preserves their current version (rebuilt in slice 13).
- Includes the sorted filename list in
  `resolution_inputs_sha256` (§A2.2).

If `[in-house]` is present, `filenames` MUST be present (may be `[]`).
A missing `[in-house]` table is equivalent to `filenames = []`. The
effective list participates in `resolution_inputs_sha256` either way.

This is the transitional mechanism; slice 13 introduces a `.pw.toml`
writer-level marker and retires the config list.

---

**End of Spec Amendments v1.3.1.**
