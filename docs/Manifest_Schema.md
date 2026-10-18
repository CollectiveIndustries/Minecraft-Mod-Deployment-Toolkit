# docs/Manifest_Schema.md

**Status:** Frozen as spec v1.3.1 (slice 7a, rev e)
**File:** `sync/modpack.manifest.toml`
**Consumer:** `minecraft-upgrade update`
**Author:** Human (pack maintainer)

**Rev e changes:** Side-handling preflight does not reject `unknown`/absent `.pw.toml` side. `drop` runs land in `resolved` state. Materialize performs full integrity check. In-house identification documented. Pipeline verify line distinguishes external (expected_hashes) and in-house (jar_sha256) verification paths. Command count corrected.

---

## Purpose

The manifest declares target runtime intent and resolver policy. It does
not declare mods, versions, sides, release mode, or per-mod policy. Those
come from other authorities (§7).

---

## Schema

```toml
[minecraft]
version = "1.21.1"
loader = "forge"
loader_version = "52.0.1"

[policy]
selection = "release_then_beta"
no_version = "ask"
```

All five fields required. No defaults. Missing any field → exit 3.

---

## §1. `[minecraft]`

| Field | Type | Notes |
|---|---|---|
| `version` | string | Minecraft version |
| `loader` | string | `forge` / `neoforge` / `fabric` / `quilt` |
| `loader_version` | string | Exact loader version |

The resolver's only runtime input from the manifest. Current Compose
reconciled per `release_mode` (§2).

---

## §2. Release mode — CLI flag

`release_mode` ∈ `{"update", "upgrade"}` determines runtime authority
(spec §5.6):

- `update`: Compose authoritative. Manifest `[minecraft]` MUST match
  effective Compose, else exit 3.
- `upgrade`: Manifest authoritative. Compose reconciled at `promote`.

Required CLI argument:

```bash
minecraft-upgrade update --release-mode upgrade
```

Persisted in `state.toml`. Re-verified at `acknowledge` AND at
`materialize` for `update` mode (Compose may change between them).

---

## §3. `[policy]`

### §3.1 `selection`

| Value | Behavior |
|---|---|
| `"release_then_beta"` | Release, else beta, never alpha |
| `"release_only"` | Release; fail if only beta/alpha |
| `"release_then_beta_then_alpha"` | Release → beta → alpha |

Required, no default.

### §3.2 `no_version`

Behavior when **a mod has no version compatible with target runtime** OR
**a dependency cannot be satisfied**. Does NOT cover side determination
(§3.3).

| Value | Behavior | `update` exit |
|---|---|---|
| `"ask"` | Produce report. Exit 6. Await `acknowledge`. | 6 |
| `"drop"` | Omit mod. Report. Exit 0. Run state = `resolved`. | 0 |
| `"fail"` | Abort. Report. Exit 4. Run state = `failed`. | 4 |

`no_version` never permits an unresolved side to proceed. Side handling
is at materialize (§3.3).

`ask` exits, does not block. Writes report and state, prints next
command. No indefinite wait.

**Acknowledgment is not approval.** It permits materialization and boot
testing. Phase B approval (spec §8.4) is a separate, later gate.

### §3.3 Loss categories

| Failure | Where | Dispatch |
|---|---|---|
| No compatible version | `update` | `no_version` policy |
| Dependency unsatisfiable | `update` | `no_version` policy |
| Dependency cycle | `update` | Exit 3 |
| Malformed `.pw.toml` (schema) | Preflight | Exit 3 |
| Seed jar missing | Preflight | Exit 3 |
| Side unresolved after full chain | `materialize` | Exit 3 |

**Only the first two are `no_version`-dispatched.** The rest are
integrity failures.

**`side = "unknown"` in a `.pw.toml` is valid.** It means "the writer
didn't know." It is NOT a preflight error. The precedence chain
(override > jar > provider > .pw.toml) resolves it. Only if the chain
produces `unknown` at materialize does the run fail.

**Absent side field** in `.pw.toml` is also valid. Same treatment.

**Malformed side** (`side = 42`, `side = ["client"]`, etc.) IS a schema
error → exit 3.

---

## §4. Command lifecycle and state machine

Five commands compose into the pipeline. Each exits and prints the next.
Run state transitions through a fixed set of values, tracked in
`state.toml`.

### State values

| State | Meaning | Materialize accepts? |
|---|---|---|
| `resolved` | `update` completed. No losses, or losses with `drop`. | Yes |
| `awaiting_acknowledgment` | `update` completed with losses under `ask`. | No (must acknowledge first) |
| `acknowledged` | Human acknowledged losses. | Yes |
| `materialized` | Slip sealed. | N/A (ready for iterate) |
| `failed` | `update` completed with losses under `fail`. | No |

### Transition table

| `update` outcome | Resulting state |
|---|---|
| No losses (any policy) | `resolved` |
| Losses + `no_version = "drop"` | `resolved` |
| Losses + `no_version = "ask"` | `awaiting_acknowledgment` |
| Losses + `no_version = "fail"` | `failed` |
| Preflight error (any) | (no state written; exit 3) |

| `acknowledge` outcome | Resulting state |
|---|---|
| Success | `acknowledged` |

| `materialize` outcome | Resulting state |
|---|---|
| Success | `materialized` |
| Failure | (unchanged) |

### Pipeline

```
update --release-mode <mode>
    → resolve versions
    → write resolution report (provisional sides)
    → write state.toml
    → exit per policy and state
      0 + state=resolved → "next: materialize --run-id <id>"
      6 + state=awaiting_acknowledgment → "next: acknowledge <id>"
      4 + state=failed
      3 (preflight)
      1 (provider API)
    ▼
[acknowledge <run-id>]        ← only if state=awaiting_acknowledgment
    → verify report + inputs + Compose
    → present losses, require acknowledgment
    → write acknowledgment.json, state=acknowledged
    → "next: materialize --run-id <id>"
    ▼
materialize --run-id <run-id>
    → verify report + inputs + Compose (same as acknowledge)
    → download artifacts to run staging dir
    → verify (external: each expected_hashes digest, compute local
       SHA-256 for the slip; in-house: jar_sha256 against the
       corresponding [[seed]] entry)
    → copy to sync/downloads/ (filename collision policy, A5.2.1)
    → inspect jar metadata for final side (overrides win)
    → fail if side unresolved
    → write .pw.toml to candidate metadata dir
    → seal slip → candidates/<run-id>/packing_slip.toml
    → state=materialized
    → "next: iterate --run-id <id>"
    ▼
iterate --run-id <run-id>
    → boot candidate
    → validation phases
    ▼
promote <tag>
    → copy candidate slip to sync/packing_slips/<tag>.toml
    → copy candidate .pw.toml files to sync/downloads/.index/
    → git stage; print instructions
```

**When `acknowledge` is skipped:** runs with no losses, and runs with
losses under `drop`. Materialize proceeds directly from `resolved`. The
integrity checks that `acknowledge` performs are also performed by
`materialize`, so no gap exists.

**Materialize never mutates the active seed.** `.index/` is updated only
by `promote`, in a deliberate git-committed step.

---

## §5. What the manifest does NOT contain

| Not in manifest | Where instead | Why |
|---|---|---|
| Mod list | `sync/downloads/.index/*.pw.toml` | Prism-authored active pack |
| Per-mod pins | Not supported | Bytecode-compat assumptions rarely hold |
| Per-mod sides | `config.d/side_overrides.toml` | Human-tested authority |
| Per-mod `no_version` | Not supported | Unmaintainable |
| Dependencies | Provider + `.pw.toml` metadata | Authoritative at resolution |
| Credentials | `config.d/upgrade.toml` → env | Spec §17.1 |
| `release_mode` | CLI flag | Per-run |
| Current pack state | `.index/*.pw.toml` | Machine-derived |

---

## §6. Seed source

Seed = `sync/downloads/.index/*.pw.toml`. Git-tracked, Prism-authored.

Rules:

- Every `.pw.toml` in `.index/` = one seed entry.
- Each seed `.pw.toml` references a jar via `filename`. That jar MUST
  exist in `sync/downloads/`. Missing → exit 3 before report.
- Jars in `sync/downloads/` with no matching `.pw.toml` in `.index/` =
  warehouse cache. Ignored.

### §6.1 In-house identification

In-house mods have no external provider identity. Before slice 13
provides a writer-level marker, in-house artifacts are identified by
filename in `config.d/upgrade.toml`:

```toml
[in-house]
filenames = ["custom-mod-1.0.0.jar", "another-custom.jar"]
```

The resolver consults this list. Filenames in the list are treated as
in-house, regardless of their `.pw.toml` content. Other mods are
resolved against external providers.

The list is part of `resolution_inputs_sha256`'s computation. Slice 13
introduces a `.pw.toml` marker and retires this list; the transition
is documented there.

In-house mods:
- Appear in the seed with `provider = "in-house"`.
- Bypass provider query during resolution.
- Have their current version preserved (rebuilt in slice 13).
- Follow the general side precedence chain (§Side_Determination).

---

## §7. Authority precedence

| Concern | Authority |
|---|---|
| Target runtime | `[minecraft]` |
| Release mode | CLI `--release-mode` + state.toml |
| Seed | `sync/downloads/.index/*.pw.toml` |
| In-house filenames | `config.d/upgrade.toml [in-house].filenames` |
| Side | `config.d/side_overrides.toml` > jar metadata > provider > `.pw.toml` |
| Selection | `[policy].selection` |
| Loss policy | `[policy].no_version` |
| Report | `www/runs/<run-id>/resolution-report.toml` |
| Acknowledgment | `/var/lib/minecraft-upgrade/runs/<run-id>/acknowledgment.json` |
| Candidate slip | `/var/lib/minecraft-upgrade/candidates/<run-id>/packing_slip.toml` |

---

## §8. Validation

`manifest check` and `update` enforce:

1. File exists, parses as TOML.
2. Top-level keys are exactly `[minecraft]` and `[policy]`.
3. `[minecraft].version` non-empty string.
4. `[minecraft].loader` ∈ `{forge, neoforge, fabric, quilt}`.
5. `[minecraft].loader_version` non-empty string.
6. `[policy].selection` ∈ three-value enum.
7. `[policy].no_version` ∈ `{ask, drop, fail}`.
8. If `--release-mode update`: `[minecraft]` matches effective Compose.

Any failure → exit 3. `manifest check` prints all violations.

---

## §9. Version history

| Version | Change |
|---|---|
| v1.3.1 rev c | State machine named. `drop` → `resolved`. Side preflight relaxed. In-house identification via config. |
| v1.3.1 rev e | Pipeline verify line distinguishes external (expected_hashes) and in-house (jar_sha256). Command count corrected to five. |

---

**End of Manifest Schema.**
