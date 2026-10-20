# docs/Build_Specs.md

**Status:** Frozen as spec v1.4.0
**File:** `config.d/build/<name>.yaml`
**Consumer:** `minecraft-upgrade build-inhouse`
**Author:** Human (mod maintainer)
**Command reference:** `Upgrade_System_specifications.md` §9, §15.1.8
**Side precedence reference:** `Side_Determination.md` §1

---

## Purpose

The build spec declares *how to build one in-house mod* from its own repository. It does not contain build logic — it contains the coordinates for running the mod's own build script, the side the mod operates on, and the metadata needed to generate the mod's `.pw.toml`.

The upgrade toolchain is deliberately platform-agnostic. It does not know or care whether a mod targets Forge, NeoForge, Fabric, or Quilt. It clones the repository, checks out the declared ref, invokes the declared entrypoint, generates the `.pw.toml` from the spec's declared side and dependencies, and stages both files.

---

## Scope

### What the build spec declares

- Which repository to clone.
- Which branch, tag, or commit SHA to check out.
- Which entrypoint to invoke and which script to run.
- **The side the mod operates on** (`client` / `server` / `both`).
- Optional display name and dependency edges.
- What environment and arguments to pass.
- What artifact glob patterns to expect.

### What the build spec does NOT declare

- Gradle version, wrapper URL, or toolchain image.
- Minecraft version, loader, or loader version.
- Build flags, dependency versions, or repository URLs.
- Whether the mod is deployed — that's decided by `[in-house].filenames` in `upgrade.toml` and by `side_overrides.toml` if the operator chooses to override.

### What the runner does NOT do

- Does not sandbox the build.
- Does not restrict filesystem or network access.
- Does not validate that the resulting artifact is "safe."
- Does not auto-add the jar to `[in-house].filenames`.
- Does not treat the declared `side` as final.

The build spec is a trusted execution model. The mod team's code is trusted to build itself. Operators MUST review the mod's build script before adding the spec.

---

## Schema

```yaml
# config.d/build/createpollutioncleanup.yaml

# Required. Client / server / both. Written into the generated .pw.toml
# as its `side` field. `unknown` is rejected -- it would make the mod
# unmarked and the resolver would silently drop it at materialize.
side: both

# Optional. Display name for the .pw.toml's `name` field. Substitutable.
name: "Custom Pollution Cleanup"

# Optional. Project-ID dependency edges emitted as
# [[x-prismlauncher-dependencies]] blocks in the .pw.toml.
dependencies:
  - addon_id: "flywheel"
    type: required

# Optional. Variables for ${VAR} substitution in every field below.
variables:
  mod_version: "0.1.2-alpha"
  java_home: "/usr/lib/jvm/jdk-17"

# Required. Any git URL.
repository: "https://github.com/example/CPC.git"

# Required. Branch, tag, or commit SHA.
ref: "v${mod_version}"

# Required. Executable on DEV's PATH or absolute.
entrypoint: "bash"

# Required. Argument to entrypoint, relative to working_dir.
script: "build-local.sh"

# Optional. Additional CLI args appended to entrypoint argv.
args: []

# Optional. Environment variables merged over parent env.
env:
  JAVA_HOME: "${java_home}"

# Optional. Script timeout in seconds. Default 1800.
timeout_seconds: 1800

# Optional. Working directory relative to clone root. Default ".".
working_dir: "."

# Optional. Shallow-clone depth. Default 0 (full clone).
depth: 0

# Required. One or more glob patterns. Across all patterns, exactly one
# .jar must match; that jar is the mod's primary artifact.
artifacts:
  - "build/libs/createpollutioncleanup-*.jar"
```

---

## §1 Field Reference

| Field | Required | Type | Default | Substitutable |
|---|---|---|---|---|
| `side` | **Yes** | `client` / `server` / `both` | — | No |
| `name` | No | string | `null` | Yes |
| `dependencies` | No | list of `{addon_id, type}` | `[]` | `addon_id` only |
| `variables` | No | mapping | `{}` | No |
| `repository` | Yes | string | — | Yes |
| `ref` | Yes | string | — | Yes |
| `entrypoint` | Yes | string | — | No |
| `script` | Yes | string | — | Yes |
| `args` | No | list of string | `[]` | Yes |
| `env` | No | mapping | `{}` | values only |
| `timeout_seconds` | No | float > 0 | `1800` | No |
| `working_dir` | No | string | `"."` | Yes |
| `depth` | No | int ≥ 0 | `0` | No |
| `artifacts` | **Yes** | list | — | each `path` |

All required fields MUST be non-empty strings. `side` MUST be one of `client`, `server`, `both`. `unknown` is rejected.

---

## §2 Side Declaration

### §2.1 Why `side` is required

The resolver needs to know whether an in-house mod belongs on the client, the server, or both *before* it walks the dependency closure. The declaration flows through the toolchain:

```
config.d/build/<name>.yaml
    side: server
        │
        ▼  build-inhouse
sync/downloads/<name>-<version>.jar
sync/downloads/.index/<name>-<version>.pw.toml   ← side = "server"
        │
        ▼  operator adds filename to [in-house].filenames
config.d/upgrade.toml [in-house].filenames = ["<name>-<version>.jar"]
        │
        ▼  update
load_seed → [[seed]].side_declared = "server"
        │
        ▼  resolver
side_effective computation → dependency closure walk
        │
        ▼  materialize
final side via Side_Determination precedence chain
```

Without the build spec's `side`, the operator would have to hand-write the `.pw.toml` for every in-house mod, duplicating the schema, byte-stability rules, and side-sentinel semantics across mod repos. The build spec moves that writer into the toolchain.

### §2.2 `side` is NOT the final side

`side_overrides.toml` remains the final authority across all cases. The build spec's `side` lands at the `.pw.toml` position — priority 4 — in the precedence chain defined by `Side_Determination.md §1`:

| Priority | Source |
|---|---|
| 1 | `side_overrides.toml` (final authority) |
| 2 | JAR metadata — Fabric / Quilt only (`environment` field) |
| 3 | Provider metadata — Modrinth only |
| 4 | **`.pw.toml` side** — the build spec's declaration lands here |
| 5 | `unknown` → exit 3 |

Practical implications for in-house mods:

- **Forge / NeoForge in-house mods**: Forge JARs rarely set `clientSideOnly`, so the resolver falls through to `.pw.toml`. The build spec's side is effectively final unless the operator adds an override.
- **Fabric / Quilt in-house mods**: JAR metadata wins if it sets `environment` to `client` or `server`. To force the build spec's value, use `side_overrides.toml`.
- **Any loader**: `side_overrides.toml` always wins if present.

The chain is unchanged. The build spec is the source of a declaration, not an exception path.

---

## §3 Variable Resolution

Three sources are merged before substitution, in order of increasing priority:

1. **Manifest defaults** — `minecraft_version`, `loader`, `loader_version` from `sync/modpack.manifest.toml`. Missing or malformed manifest → empty layer, warning logged.
2. **YAML `variables:` block** — the spec's own variables. Shadows manifest values with the same key.
3. **CLI overrides** — `--var KEY=VALUE` pairs. Shadows both.

Only `${VAR}` where `VAR` matches `[A-Za-z_][A-Za-z0-9_]*` is substituted. Literal `{...}` is left alone. An undefined `${VAR}` produces exit code 3 with a diagnostic naming the variable and field.

**Fields with enum semantics are never substituted:** `side`, `dependencies[].type`, `timeout_seconds`, `depth`, `entrypoint`. Everything else (`repository`, `ref`, `script`, `args`, `env` values, `artifacts[].path`, `working_dir`, `name`, `dependencies[].addon_id`) is subject to substitution.

---

## §4 Artifacts

Each artifact entry is a glob pattern evaluated by `pathlib.Path.glob()` relative to the clone root. Across all patterns, exactly one `.jar` must match; that jar is the mod's primary artifact.

**The exactly-one-jar rule** prevents silent misconfiguration. Zero matches usually means the build didn't produce the expected output; multiple matches usually means the operator needs to tighten the pattern. Both are errors.

Patterns containing `..` are rejected before matching. Matches that resolve outside the clone directory (via symlink) are rejected.

Artifact entries take string form:

```yaml
artifacts:
  - "build/libs/mymod-*.jar"
```

Multiple patterns are allowed. Patterns that resolve to the same jar dedupe:

```yaml
artifacts:
  - "build/libs/*.jar"
  - "build/libs/mymod-1.0.0.jar"   # same jar as above; deduped
```

---

## §5 `.pw.toml` Generation

After the build succeeds, the runner generates a `.pw.toml` for the single matched jar using `minecraft.modpack_upgrade.pw_toml.write_pw_toml`. The generated `.pw.toml` has:

| Field | Source |
|---|---|
| `filename` | The matched jar's basename |
| `side` | The build spec's `side` |
| `name` | The build spec's `name` (omitted if `null`) |
| `[[x-prismlauncher-dependencies]]` | The build spec's `dependencies` |
| `[download]` | **Omitted** — in-house artifacts have no download URL |
| `[update.*]` | **Omitted** — in-house artifacts have no provider identity |

The `.pw.toml` is staged at `sync/downloads/.index/<jar-stem>.pw.toml`. The jar is staged at `sync/downloads/<jar-filename>`.

**Mods that currently generate their own `.pw.toml` must delete that step from their build script.** The tool's generated `.pw.toml` is what the resolver sees.

The writer enforces byte-stability, NFC normalization, and the schema rules in §5.7 of the main spec. Because it lives in the toolchain and not in each mod's build script, a single implementation of those rules is guaranteed.

---

## §6 Runner Behavior

The runner executes:

1. **Create clone directory** at `.build-workspace/<name>-<timestamp>/`.
2. **Clone** the repository at `ref`. Depth-limited clones use `--depth N --branch REF`. Timeout 300s.
3. **Checkout** `ref` (skipped for shallow clones). Timeout 60s.
4. **Run script** `[entrypoint, script, *args]` in `working_dir` with merged env. Timeout `timeout_seconds`.
5. **Expand artifacts** — each pattern under the clone root. Traversal-rejected patterns fail immediately.
6. **Select single jar** — exactly one `.jar` must match across all patterns.
7. **Generate `.pw.toml`** via `write_pw_toml`.
8. **Stage** the jar to `sync/downloads/` and the `.pw.toml` to `sync/downloads/.index/`.
9. **Cleanup** — clone removed on success, kept on failure.

Every failure produces a structured `BuildOutcome` with `success=False`. The CLI translates it to a diagnostic and exit code.

---

## §7 Failure Handling

| Code | Meaning | Triggers |
|---|---|---|
| 0 | Success | Build ran, jar and `.pw.toml` staged |
| 1 | Build failure | Clone, checkout, script, artifact match, or `.pw.toml` write failed |
| 2 | CLI usage error | `--var` without `=`, missing positional |
| 3 | Config/spec error | Spec missing, malformed YAML, missing `side` or required field, invalid `timeout_seconds` / `depth`, undefined `${VAR}` |

On build failure the clone is preserved at `.build-workspace/<name>-<timestamp>/` with the failure path named in the diagnostic. On success the clone is removed unless `keep_on_success=True`.

On success the CLI prints:

```
build: <name>
status: ok
  staged: /path/to/sync/downloads/<jar-filename>
  staged: /path/to/sync/downloads/.index/<jar-stem>.pw.toml
note: add these filenames to config.d/upgrade.toml [in-house].filenames:
  <jar-filename>
```

---

## §8 Worked Example

The CPC mod from the operating environment:

```yaml
# config.d/build/createpollutioncleanup.yaml
side: both
name: "Create Pollution Cleanup"
dependencies:
  - addon_id: "flywheel"
    type: required

variables:
  mod_version: "0.1.2-alpha"

repository: "https://github.com/example/CPC.git"
ref: "v${mod_version}"
entrypoint: "bash"
script: "build-local.sh"

artifacts:
  - "build/libs/createpollutioncleanup-*.jar"
```

The mod's `build-local.sh` still runs `gradlew clean build`, but the `generatePackwizMetadata` gradle task is no longer needed — the tool generates the `.pw.toml` from the spec.

After `build-inhouse createpollutioncleanup`:

```
sync/downloads/createpollutioncleanup-0.1.2-alpha.jar
sync/downloads/.index/createpollutioncleanup-0.1.2-alpha.pw.toml
```

The `.pw.toml`:

```toml
filename = "createpollutioncleanup-0.1.2-alpha.jar"
name = "Create Pollution Cleanup"
side = "both"

[[x-prismlauncher-dependencies]]
addonId = "flywheel"
type = "required"
```

The operator adds `createpollutioncleanup-0.1.2-alpha.jar` to `[in-house].filenames` in `upgrade.toml`. The resolver picks it up at the next `update` with `side_declared = "both"`.

---

## §9 Integration with the Resolver

`build-inhouse` stages the artifact into the warehouse. It does **not** modify the resolver's inputs. A subsequent `update` invocation only sees the built jar as `provider = "in-house"` if the operator has added the jar's filename to `config.d/upgrade.toml`:

```toml
[in-house]
filenames = [
    "createpollutioncleanup-0.1.2-alpha.jar",
]
```

The CLI prints a copy-pasteable list of filenames on success to make this easy.

A built jar without a `[in-house].filenames` entry is classified by its `.pw.toml`. The tool's generated `.pw.toml` has no `[update.*]` block, so the resolver classifies the jar as `"unknown"` and reports it in `[[lost]]` with reason `no_compatible_version`. This surfaces at `update` time; the tool does not silently treat the jar as in-house.

The retirement of `[in-house].filenames` in favor of a `.pw.toml` writer-level marker is deferred to a later slice. See `Upgrade_System_specifications.md` §9.7.

---

## §10 Security Posture

The build script runs with the DEV operator's privileges, in a fresh clone, with the operator's full environment. This is a fully trusted execution model.

The tool's guarantees:

- Artifact glob patterns cannot escape the clone directory via `..` or symlink resolution.
- The staging step writes the jar to `sync/downloads/<name>` and the `.pw.toml` to `sync/downloads/.index/<stem>.pw.toml` — both locations the resolver expects. There is no way for the build spec to redirect staging to arbitrary paths.

Beyond that, the script can write anywhere the operator's account can write. Operators MUST review a mod's build script before adding the spec.

---

## §11 Version History

| Version | Change |
|---|---|
| v1.4.0 | Initial freeze. `side` is a required top-level field; the runner always generates `.pw.toml` via the shared writer; `side_overrides.toml` remains final authority per `Side_Determination.md §1`. |

---

**End of Build Specs.**