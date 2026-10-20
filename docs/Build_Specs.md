# docs/Build_Specs.md

**Status:** Frozen as spec v1.4.0
**File:** `config.d/build/<name>.yaml`
**Consumer:** `minecraft-upgrade build-inhouse`
**Author:** Human (mod maintainer)
**Command reference:** `Upgrade_System_specifications.md` §9, §15.1.8

---

## Purpose

The build spec declares *how to build one in-house mod* from its own repository. It does not contain build logic — it contains the coordinates for running the mod's own build script and staging the artifacts that script produces.

The upgrade toolchain is deliberately platform-agnostic. It does not know or care whether a mod targets Forge, NeoForge, Fabric, or Quilt. It clones the repository, checks out the declared ref, invokes the declared entrypoint, and copies the declared artifacts. Everything else — gradle configuration, toolchain pins, dependency resolution, the `.pw.toml` that describes the mod — lives in the mod's repository.

The build spec is the boundary between "the upgrade toolchain" and "the mod's own pipeline."

---

## Scope

### What the build spec declares

- Which repository to clone.
- Which branch, tag, or commit SHA to check out.
- Which entrypoint to invoke.
- Which script to run.
- What environment and arguments to pass.
- What artifacts to expect, and where to stage them.

### What the build spec does NOT declare

- Gradle version, wrapper URL, or toolchain image.
- Minecraft version, loader, or loader version (those come from the manifest; the mod's `gradle.properties` carries its own pins).
- Build flags, dependency versions, or repository URLs.
- The `.pw.toml` contents (the mod's build script writes it).
- Whether the resulting artifact will be deployed — that's decided by the manifest and the operator's `[in-house].filenames` entry, not the build spec.

### What the runner does NOT do

- Does not sandbox the build.
- Does not restrict the build script's filesystem or network access.
- Does not validate that the resulting artifact is "safe."
- Does not add the built jar to `[in-house].filenames` — that remains a manual operator step (see §Integration with the resolver).
- Does not add `[[jar]]` entries to the candidate slip directly.

The build spec is a trusted execution model. The mod team's code is trusted to build itself. The operator reviews the mod's `build-local.sh` (or equivalent) before running `build-inhouse` against it.

---

## Schema

A complete build spec with every field:

```yaml
# config.d/build/createpollutioncleanup.yaml

# Optional. Variables available for ${VAR} substitution in every field
# below. Manifest values (${minecraft_version}, ${loader},
# ${loader_version}) are always available and can be shadowed here.
variables:
  mod_version: "0.1.2-alpha"
  java_home: "/usr/lib/jvm/jdk-17"

# Required. Any git URL: HTTPS, SSH, or local path.
repository: "https://github.com/example/CPC.git"

# Required. Branch, tag, or commit SHA. Substituted before clone.
ref: "v${mod_version}"

# Required. Executable on the DEV host's PATH, or an absolute path.
entrypoint: "bash"

# Required. Argument passed to the entrypoint. Path relative to
# working_dir (unless absolute).
script: "build-local.sh"

# Optional. Additional CLI arguments appended to the entrypoint argv.
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
artifacts:
  - path: "build/libs/createpollutioncleanup-*.jar"
  - path: "build/.index/createpollutioncleanup.pw.toml"
```

A minimal build spec:

```yaml
repository: "https://github.com/example/CPC.git"
ref: "v1.0.0"
entrypoint: "bash"
script: "build-local.sh"
artifacts:
  - path: "build/libs/*.jar"
  - path: "build/.index/*.pw.toml"
```

---

## §1 Field Reference

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `variables` | No | mapping<string, string> | `{}` | Values available for `${VAR}` substitution. Merged with manifest values and CLI overrides per §2. |
| `repository` | Yes | string | — | Any git URL form: `https://`, `git@`, `file://`, or a local path. Must be non-empty. |
| `ref` | Yes | string | — | Branch name, tag, or commit SHA. Substituted before clone. |
| `entrypoint` | Yes | string | — | Executable name (resolved via `PATH`) or absolute path. Must be non-empty. |
| `script` | Yes | string | — | Argument passed to the entrypoint. Relative to `working_dir` (unless absolute). Must be non-empty. |
| `args` | No | list<string> | `[]` | Each element is substituted, then appended to the entrypoint argv. |
| `env` | No | mapping<string, string> | `{}` | Merged on top of the parent process environment. Each value is substituted. |
| `timeout_seconds` | No | float > 0 | `1800` | Overall script timeout. Timeout → build failure with exit code 1. |
| `working_dir` | No | string | `"."` | Working directory for the script, relative to the clone root. Must exist in the clone. |
| `depth` | No | int ≥ 0 | `0` | When `> 0`, git is invoked with `--depth N --branch <ref>`. Requires `ref` to be a branch or tag name — `git` rejects `--depth` with a commit SHA. |
| `artifacts` | Yes | list of artifact entries | — | Non-empty. See §3. |

### §1.1 Field validation

Every required field MUST be a non-empty string. Empty strings, `null`, missing keys, and non-string values (numbers, booleans, lists, mappings) are rejected with a `SchemaError` that names the offending field.

`timeout_seconds` MUST parse as a float strictly greater than zero. `depth` MUST parse as an integer greater than or equal to zero. `variables`, `env`, and `artifacts` MUST be of the correct container type when present.

Malformed YAML produces a `SchemaError` with the parse error attached. A missing spec file produces a `SchemaError` with the expected path.

All schema violations surface as process exit code 3.

### §1.2 Filename rules

The spec's `name` is the filename stem. `config.d/build/createpollutioncleanup.yaml` produces a spec named `createpollutioncleanup`. The name appears in:

- The clone directory name: `.build-workspace/createpollutioncleanup-<timestamp>/`
- CLI diagnostics.
- The `build-inhouse <name>` invocation.

The name is not constrained beyond "a valid filename on the DEV host's filesystem." Dots, dashes, and underscores are all legal.

---

## §2 Variable Resolution

Three sources of variables are merged before substitution, in order of increasing priority:

1. **Manifest defaults** — `minecraft_version`, `loader`, and `loader_version` read from `sync/modpack.manifest.toml`. If the manifest is missing or malformed, this layer is empty and a warning is logged. Variables from this layer are only required when a spec references them; a spec that does not use `${minecraft_version}` runs fine without a manifest.

2. **YAML `variables:` block** — the spec's own `variables:` mapping. Shadows manifest values with the same key.

3. **CLI overrides** — `--var KEY=VALUE` pairs. Shadows both YAML variables and manifest values.

The merged mapping is applied to every string field that appears in §1, with one exception: `entrypoint` is not substituted. It's expected to be a bare binary name or an absolute path, and shell-style substitution in an executable name is a footgun. `repository`, `ref`, `script`, every element of `args`, every value in `env`, every artifact's `path` and `dest`, and `working_dir` are all subject to substitution.

### §2.1 Substitution syntax

Only `${VAR}` where `VAR` matches `[A-Za-z_][A-Za-z0-9_]*` is substituted. Literal `{...}` (for example, `build-{date}.sh`) is left alone, so brace syntax in shell snippets or brace-expansion patterns survives unchanged.

An undefined `${VAR}` after merge produces a `SchemaError` that names both the missing variable and the field where the reference appeared. This surfaces as process exit code 3.

### §2.2 Precedence example

```yaml
# config.d/build/cpc.yaml
variables:
  mod_version: "0.1.2-alpha"
repository: "https://github.com/example/CPC.git"
ref: "v${mod_version}"
```

```bash
# Manifest declares minecraft_version = "1.20.1"

# Run 1 (defaults only): ref = "v0.1.2-alpha"
minecraft-upgrade build-inhouse cpc

# Run 2 (CLI override wins): ref = "v0.2.0-beta"
minecraft-upgrade build-inhouse cpc --var mod_version=0.2.0-beta

# Run 3 (referencing an undefined variable fails):
# spec says `repository: "https://${git_host}/CPC.git"` and git_host is
# not defined in manifest, YAML, or CLI.
# exit code 3, error names `${git_host}` in repository
```

---

## §3 Artifacts

Each artifact entry declares one file (or one glob pattern expected to match exactly one file) that the build produces, and where it should be staged.

### §3.1 Entry forms

Short form — just a path:

```yaml
artifacts:
  - "build/libs/createpollutioncleanup-*.jar"
```

Long form — path with optional `dest` override:

```yaml
artifacts:
  - path: "build/libs/createpollutioncleanup-*.jar"
    dest: "sync/downloads/custom-name.jar"
```

### §3.2 Path rules

An artifact's `path` is a glob pattern evaluated by `pathlib.Path.glob()` relative to the clone root. The pattern MUST:

- Contain no `..` components. Path traversal in the pattern is rejected before matching.
- Match exactly one file after the build completes. Zero matches → build failure with exit code 1. Multiple matches → build failure naming every candidate.
- Not resolve (via symlink) to a path outside the clone directory. A match that resolves outside is rejected with a diagnostic.

### §3.3 Destination rules

When `dest` is omitted, the destination is inferred from the matched filename:

| Filename suffix | Destination |
|---|---|
| `.pw.toml` | `sync/downloads/.index/<filename>` |
| anything else | `sync/downloads/<filename>` |

When `dest` is present, it is:

- Interpreted as project-root-relative (default), or as an absolute path if the `dest` value starts with `/`.
- Created with its full parent chain if the intermediate directories do not exist.
- Not subject to the `.pw.toml`-to-`.index/` inference — the caller's `dest` is final.

### §3.4 Match-exactly-one rule

The exactly-one rule exists because a glob that matches zero files usually means the build didn't run correctly, and a glob that matches multiple files usually means the mod produces versioned artifacts (like `mod-1.0.0.jar` and `mod-1.0.1.jar`) and the caller needs to disambiguate. Both cases are almost always operator error rather than a legitimate outcome.

If the mod genuinely produces multiple jars, list them as separate artifact entries with distinct patterns:

```yaml
artifacts:
  - path: "build/libs/mymod-main-*.jar"
  - path: "build/libs/mymod-compat-*.jar"
```

---

## §4 Runner Behavior

The runner executes the following steps. Every step failure produces a structured `BuildOutcome` with `success=False`; the CLI translates it to a diagnostic and an exit code.

1. **Create clone directory.** A unique directory is created at `.build-workspace/<name>-<timestamp>/`. The path is recorded for cleanup (success) or preservation (failure).

2. **Clone.** `git clone [--depth N --branch REF] <repository> <clone_dir>`. Timeout: 300 seconds. Non-zero exit, timeout, or missing git binary → build failure.

3. **Checkout.** `git checkout --detach <ref>` in the clone. Skipped when `depth > 0` — the shallow clone already selected the ref. Timeout: 60 seconds.

4. **Run script.** `[entrypoint, script, *args]` executed in `working_dir` with merged environment. Timeout: `timeout_seconds`. Missing entrypoint, timeout, or non-zero exit → build failure. Non-zero exit captures stdout and stderr excerpts (last 2000 chars).

5. **Expand artifacts.** Each artifact pattern is expanded under the clone root per §3.2. First pattern that fails ends the run with a named diagnostic.

6. **Stage artifacts.** Each matched artifact is copied to its destination per §3.3. First copy failure ends the run with a diagnostic naming the source and destination.

7. **Cleanup.** On success, the clone directory is removed. On failure, the clone is preserved for operator inspection — the path is named in the failure outcome, in the CLI diagnostic, and in the log.

### §4.1 Timeouts

| Operation | Timeout |
|---|---|
| `git clone` | 300 seconds |
| `git checkout` | 60 seconds |
| Build script | `timeout_seconds` (default 1800) |

A timeout on any operation is treated as a build failure. The clone is preserved so the operator can inspect partial state.

### §4.2 Environment

The script runs with `env = {**os.environ, **spec.env}`. Every environment variable set in the operator's shell is inherited, then overridden by any `env:` entries in the spec. This matches what a direct invocation of `build-local.sh` from the operator's shell would produce, plus the spec's additions.

---

## §5 Failure Handling

### §5.1 Exit codes

| Code | Meaning | When |
|---|---|---|
| 0 | Success | Build ran, all artifacts staged. |
| 1 | Build failure | Clone failed, checkout failed, script failed, artifact pattern did not match, staging failed. |
| 2 | CLI usage error | `--var` value without `=`, missing positional argument, unknown flag. |
| 3 | Config/spec error | Spec file missing, malformed YAML, missing required field, invalid `timeout_seconds` or `depth`, undefined `${VAR}`. |

### §5.2 Diagnostics

On a build failure (exit code 1), the CLI prints:

```
build: <name>
status: failed (<failure message>)
  clone kept for inspection: <clone path>
  stderr (tail):
    <up to 2000 chars of stderr, one line per line>
```

The `failure message` names the specific step that failed:

- `git clone failed (128): <stderr excerpt>`
- `git checkout 'v1.0.0' failed: <stderr excerpt>`
- `script exited with 42`
- `script timed out after 1800s`
- `entrypoint 'bash' not found on PATH`
- `working directory does not exist: <path>`
- `no files matched pattern 'build/libs/*.jar'`
- `pattern 'build/libs/*.jar' matched 2 files: [...]`
- `artifact pattern '...' escapes the clone directory`
- `artifact <path> resolves outside the clone directory`

On a config/spec error (exit code 3), the CLI prints a focused message that names the offending field:

```
error: build spec error: build spec field 'repository' must be a non-empty string
```

or

```
error: variable resolution error: undefined variable ${git_host} in 'https://${git_host}/CPC.git'
```

### §5.3 Successful build diagnostics

On a successful build (exit code 0), the CLI prints:

```
build: <name>
status: ok
  staged: /path/to/sync/downloads/createpollutioncleanup-0.1.2-alpha.jar
  staged: /path/to/sync/downloads/.index/createpollutioncleanup.pw.toml
note: add these filenames to config.d/upgrade.toml [in-house].filenames:
  createpollutioncleanup-0.1.2-alpha.jar
```

The `note:` block names every staged jar so the operator can copy the filenames directly into `[in-house].filenames` (§Integration with the resolver).

### §5.4 Clone preservation

A failed build leaves the clone at `.build-workspace/<name>-<timestamp>/` for inspection. The operator is expected to:

1. Inspect the gradle logs, partial artifacts, or script output.
2. Fix the underlying cause (in the mod's repository, or in the spec's fields).
3. Re-run `build-inhouse`.
4. Delete the failed clone if desired — it is not used by any subsequent run.

The runner does not clean up failed clones automatically. Every run creates a fresh clone, so stale clones accumulate only as a housekeeping concern, not a correctness one.

---

## §6 Worked Example

The CPC mod from the operating environment. Its own repository contains:

- `gradle.properties` — pins `minecraft_version=1.20.1`, `forge_version=47.4.10`, toolchain paths.
- `build.gradle` — ForgeGradle setup, dependency resolution, jar manifest, `.pw.toml` generation task.
- `build-local.sh` — copies local dependency jars into `libs/`, runs `gradlew clean build`, stages the resulting jar and its `.pw.toml` into the operator's minecraft warehouse.

The build spec for CPC declares only the boundary the tool crosses:

```yaml
# config.d/build/createpollutioncleanup.yaml

variables:
  mod_version: "0.1.2-alpha"

repository: "https://github.com/example/CPC.git"
ref: "v${mod_version}"
entrypoint: "bash"
script: "build-local.sh"

artifacts:
  - path: "build/libs/createpollutioncleanup-*.jar"
  - path: "build/.index/createpollutioncleanup.pw.toml"
```

`build-local.sh` stages into `$HOME/minecraft/sync/downloads/` directly. Because the tool's own staging step (§4 step 6) also copies into `sync/downloads/`, the two operations are redundant but compatible: the mod's script writes its jar and its `.pw.toml`, then the runner copies them again to the same paths. If the mod's script is changed to stage only into the clone (recommended), the runner's staging becomes the sole copy step. Both are valid.

Recommended refinement for the mod's `build-local.sh` — stage into the clone only, and let the runner stage into the warehouse:

```bash
# build-local.sh, revised for the Slice 14 model
# ... cp local jars, run gradlew ...
mkdir -p build/libs build/.index
# gradlew build already produces build/libs/<jar>
# generatePackwizMetadata already produces build/.index/<jar>.pw.toml
# No further copying — the runner stages these to sync/downloads/.
```

With this change, the mod's script is entirely self-contained: it produces artifacts under `build/`, and the tool decides where those artifacts land. This matches the layering the spec describes.

---

## §7 Integration with the Resolver

`build-inhouse` stages the artifact into the warehouse. It does **not** modify the resolver's inputs. A subsequent `update` invocation only sees the built jar as `provider = "in-house"` if the operator has added the jar's filename to `config.d/upgrade.toml`:

```toml
[in-house]
filenames = [
    "createpollutioncleanup-0.1.2-alpha.jar",
]
```

Two consequences:

1. **The operator must edit `[in-house].filenames` after every successful build.** The CLI prints a copy-pasteable list of filenames on success to make this easy.

2. **A built jar without a `[in-house].filenames` entry is classified by its `.pw.toml`.** If the mod's `.pw.toml` declares an `[update.modrinth]` or `[update.curseforge]` block, the resolver treats it as an external artifact with that provider identity. If the `.pw.toml` declares neither, the resolver classifies the jar as `"unknown"` and reports it in the resolution report's `[[lost]]` array with reason `no_compatible_version`. The operator sees this at `update` time; the tool does not silently treat the jar as in-house.

The retirement of this manual step — replacing `[in-house].filenames` with a `.pw.toml` writer-level marker — is deferred to a later slice. The v1.4.0 contract preserves the manual step; see `Upgrade_System_specifications.md` §9.7.

---

## §8 Security Posture

The build script runs with the DEV operator's privileges, in a fresh clone of the mod's repository, with the operator's full environment. This is a **fully trusted execution model**.

The build spec's guarantees are limited to:

- The **pattern** used to locate artifacts cannot escape the clone directory via `..` or symlink resolution.
- The **staging step** copies matched files to destinations the caller declared. It does not verify that the destination is inside any particular tree, and it does not verify that the staged file is safe.

Beyond that, the script can write anywhere the operator's account can write. This is intentional. The toolchain's job is to run the mod's build; sandboxing the build is out of scope. Operators MUST review a mod's `build-local.sh` (or equivalent) before adding the spec to `config.d/build/` and running `build-inhouse` against it.

The appropriate boundary is: the operator decides which repositories to trust by adding their specs. `build-inhouse` then runs whatever those specs declare.

---

## §9 Version History

| Version | Change |
|---|---|
| v1.4.0 | Initial freeze. Introduces `config.d/build/<name>.yaml` as the build-spec schema. Replaces the v1.3.1 §9 "tool owns the toolchain" model with "clone the mod's own repository and run its own build script." Adds `build-inhouse <name> [--var KEY=VALUE ...]` to the CLI, replacing the earlier `<mod> <revision>` positional form. Documented in `Upgrade_System_specifications.md` §9, §15.1.8. |

---

**End of Build Specs.**