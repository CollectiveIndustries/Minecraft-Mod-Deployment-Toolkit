# docs/Side_Determination.md

**Status:** Frozen as spec v1.3.1 (slice 7a, rev f)
**Consumer:** `materialize` (final side), preflight (verification), shipping (filtering)
**Authority:** `config.d/side_overrides.toml` is final; everything else is best-effort

**Rev e changes:** `unknown` and absent `.pw.toml` sides are not preflight failures. Only malformed values fail. Side resolution runs at materialize; exit 3 only if precedence chain exhausts. In-house unified with general chain. Drafting text removed. Scope clarified (minecraft-upgrade strict, deploy-pack legacy unchanged).

**Rev f changes:** §4.1 corrected. Modrinth exposes two independent side fields — `client_side` and `server_side`, each with values `required` / `optional` / `unsupported` / `unknown` — not a single `environment` enum. The corrected table in §4.1 matches `resolver._modrinth_side` exactly. The prior rev e table described a hypothetical pre-2.0 Modrinth enum and was never implemented.

---

## §1. Precedence chain

First source yielding non-`unknown` wins.

| Priority | Source | Reliability |
|---|---|---|
| 1 | `side_overrides.toml` | **FINAL AUTHORITY** |
| 2 | Valid JAR metadata for target loader (Fabric only) | High |
| 3 | Provider metadata (Modrinth only) | Medium |
| 4 | `.pw.toml` `side` field | Low |
| 5 | `"unknown"` — exit 3 | Blocked |

**When determination runs:** at `materialize`. `update` computes
**provisional** sides (override > provider > current `.pw.toml`), which
appear in the report. `materialize` recomputes with jar bytes present.
If final is `unknown`, exit 3.

**All mods follow this chain.** In-house mods are not special. In-house
Forge mods have no jar-level side field (same as external Forge mods) and
typically require an override — documented as expected.

---

## §2. `.pw.toml` side acceptance

The seed `.pw.toml`'s side value feeds the chain:

| Value | Treatment |
|---|---|
| `"client"` / `"server"` / `"both"` | Used at priority 4 |
| `"unknown"` | Treated as absent at priority 4; chain continues |
| absent | Treated as absent at priority 4 |
| anything else | Malformed — schema error → exit 3 pre-report |

**`unknown` and absent are valid.** They mean "the writer didn't know."
No preflight rejection. The override (priority 1) can still resolve them.

**Malformed values** (int, list, unrecognized string) are schema
violations. Reject at preflight. Exit 3.

---

## §3. JAR metadata by loader

### §3.1 Fabric — `fabric.mod.json` `environment`

Per Fabric spec, `environment` defaults to `"*"` when omitted.

| `environment` | Determined side |
|---|---|
| `"*"` or absent | `both` |
| `"client"` | `client` |
| `"server"` | `server` |
| array form (e.g. `["client"]`) | `unknown` — requires override |

Absent → `both` because the format's default. Not inference.

Array form → `unknown` even though the format permits it. Slip's
three-value enum cannot express it. Requires override.

### §3.2 Forge / NeoForge — no reliable jar-level side

Only `[[mods]] clientSideOnly = true` provides a signal (→ `client`).
When `false` or absent, side ambiguous between `both` and `server`. Skip
to provider.

`[[dependencies.<modid>]] side` declares the dependency's required side,
not the mod's. Never use it for the mod's own side.

### §3.3 Quilt — `quilt.mod.json` (same as Fabric)

Mapping identical to Fabric.

### §3.4 In-house mods

Same chain as external. In-house Forge mods typically need overrides.
Not special-cased.

---

## §4. Provider metadata

### §4.1 Modrinth

Modrinth's API (`GET /v2/project/<id>`) exposes **two independent**
side fields per project — `client_side` and `server_side` — not a
single combined `environment` enum. Each field carries one of four
values: `required`, `optional`, `unsupported`, `unknown`.

The adapter (`resolver._modrinth_side`) combines the two fields into a
slip side as follows:

| `client_side` | `server_side` | Slip side | Rationale |
|---|---|---|---|
| `unsupported` | `unsupported` | `unknown` | Mod runs on neither side — needs override |
| `unsupported` | any other | `server` | Server-only mod |
| any other | `unsupported` | `client` | Client-only mod |
| `None` (absent) | any value | `unknown` | Missing data — do not infer |
| any value | `None` (absent) | `unknown` | Missing data — do not infer |
| any other | any other | `both` | Runs on both sides |

Notes:

- **"Any other"** means any value other than `unsupported` or `None` —
  including `required`, `optional`, and `unknown`. Modrinth's `unknown`
  on one or both sides falls through to `both`; the adapter does not
  infer a restriction from Modrinth's `unknown`.
- **Absent (`None`) fields** are treated as `unknown` at the slip level.
  Do not conflate with `optional` — an absent field is missing data,
  not a "no constraint" signal.
- **The slip's three-value enum flattens** what Modrinth's four-value
  fields can express. `client_side=required` / `server_side=optional`
  becomes `both`, losing the "prefers client" bias. Only the flattened
  `side_provisional` appears in the resolution report's `[[resolved]]`
  entry; the raw `client_side` / `server_side` strings are **not**
  preserved in the report.

### §4.2 CurseForge — always `unknown`

No `clientSide` / `serverSide` field. `gameVersions` includes `"Client"` /
`"Server"` pseudo-versions; absence is not evidence of client-only. Always
`unknown`. Requires override.

---

## §5. `.pw.toml` fallback

| Value | Determined side |
|---|---|
| `"client"` / `"server"` / `"both"` | as declared; log warning |
| `"unknown"` or absent | `unknown` |

Warning when `.pw.toml` is the determining source:

```
WARN  project_id "X" side from .pw.toml. Add override to be sure.
```

---

## §6. `side_overrides.toml` — final authority

Sections: `[by_id]`, `[by_filename]`, `[deployment_tool_review]`.
Precedence: `by_id` > `by_filename` > `deployment_tool_review`.

When an override exists: used unconditionally. No conflict warning. No
stale detection. Override is truth.

**File required for `minecraft-upgrade update`.** Missing →
`update` exits 3. An empty file is valid (declares "no overrides yet").

**Overrides resolve values, not syntax.** A malformed `.pw.toml` fails
at parse time, before overrides are consulted. If `side = 42` or
`side = ["client"]`, that is a schema error and `update` exits 3
regardless of any override. If `side = "unknown"` or the field is
absent, the file parses cleanly and the override chain proceeds.

**Legacy `deploy-pack` behavior is unchanged.** The existing
`Project_Specs.md` v3.0 §3.11 rule ("missing `side_overrides.toml` is
treated as empty") continues to apply to the legacy `deploy-pack` code
path during the dual-path migration period. The stricter requirement
above applies only to `minecraft-upgrade update`, which is a new
command introduced in v1.3.1. When legacy mode is retired (§19.3 of the
base spec), the strict rule becomes universal.

---

## §7. Unresolved side at materialize

If after full chain (override > jar > provider > `.pw.toml`) the side is
`unknown` at materialize:

```
error: side unresolved for "<filename>" (project_id "<id>", provider "<p>").
       All sources yielded unknown.
       Add an entry to config.d/side_overrides.toml and re-run materialize.
```

Exit 3. Does not dispatch by `no_version`. That policy covers version
selection, not side safety.

---

## §8. Precedence summary

| Scenario | Result | Source |
|---|---|---|
| Override | Override value | `override` |
| No override; Fabric jar `environment` present or absent | Jar value | `jar` |
| No override; Fabric array form | `unknown` | `unknown` |
| No override; Forge `clientSideOnly=true` | `client` | `jar` |
| No override; Forge `clientSideOnly=false` or absent | Skip to provider | — |
| No override; Modrinth usable (see §4.1) | Mapped value | `provider` |
| No override; CurseForge | `unknown` | `unknown` |
| No override; provider `unknown` | Fall to `.pw.toml` | — |
| No override; `.pw.toml` usable | `.pw.toml` value | `pw_toml` |
| All `unknown` | Exit 3 | `unknown` |

---

## §9. Version history

| Version | Change |
|---|---|
| v1.3.1 rev c | `unknown`/absent `.pw.toml` side is valid. Malformed rejects. In-house unified. |
| v1.3.1 rev d | Drafting text removed from §6. Scope clarified: minecraft-upgrade strict, deploy-pack legacy unchanged. |
| v1.3.1 rev e | No changes; carried forward from rev d. |
| v1.3.1 rev f | §4.1 corrected to reflect the real Modrinth `client_side` / `server_side` API shape. Corrected mapping now matches `resolver._modrinth_side`. Removed the false claim that the report preserves the raw Modrinth enum. |

---

**End of Side Determination.**
