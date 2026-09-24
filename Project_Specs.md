# `deploy_pack` Deployment Contract

**Version:** 3.0

**Status:** Frozen

**Applies to:** `deploy_pack` v2

**Supersedes:** v1.0 through v2.9

---

## 1. Purpose

`deploy_pack` runs on the server host. It:

1. Deploys shared mods and per-instance config/KubeJS to Minecraft server instances managed by docker-compose.
2. Builds the client ZIP and changelog HTML, publishes to the web root, and posts Discord announcements.
3. Publishes resource packs and updates each instance's `server.properties`.
4. Orchestrates docker container lifecycle around the write phase, using a per-path restart policy adapter.
5. Provides a Textual UI for auditing mod side assignments when upstream Prism metadata is unreliable.

### 1.1 Changelog Contract Reference

Changelog generation is owned by `src/minecraft/common/changelog.py`. `deploy_pack` consumes its public API. No changes to `changelog.py` are required.

Initial-build rendering is provided by `changelog.py`. `deploy_pack` does not interpret or modify it.

---

## 2. CLI Contract

### 2.1 Scopes

| Flag | Writes |
|---|---|
| `--server` | `mods_dir`, `<instance>/config`, `<instance>/kubejs` |
| `--client` | `www_dir/*.zip`, `www_dir/*.html`, `@www/*` shared items |
| `--resource-pack` | `www_dir/<resource_pack_dest>`, `<instance>/server.properties` |
| `--full` | Equivalent to `--server --client --resource-pack --with-resources` |

`--full` implies `--server`, `--client`, and `--resource-pack`. Passing any of those alongside `--full` is valid and redundant. `--full --with-resources` is valid and redundant.

`--full` and `--instance` are mutually exclusive.

### 2.2 Modifiers

| Flag | Effect |
|---|---|
| `--notify` | Opt-in Discord notification. Default off. |
| `--dry-run` | No writes, no docker lifecycle, no health polls. Requires a scope. |
| `--with-resources` | Include packs in client ZIP. Requires `--client` or `--full`. |
| `--instance NAME` | Target instances. Repeatable / comma-separated. |
| `--debug` | Verbose logging, per-item change listing. |
| `--debug-deps` | CLI-only dependency diagnostic. |
| `--audit-mods` | Standalone. Opens Textual UI. |
| `--non-interactive` | Modifier on a scope. Skips prompts. |
| `--config-dir` | Config directory path. |

### 2.3 Removed Flags

`--no-deploy`, `--no-zip`, `--no-notify`, `--dry-run-notify`.

### 2.4 Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime/deployment failure |
| 2 | CLI usage/argument error |
| 3 | Configuration/preflight failure |

**Error precedence:** argument parsing errors (exit 2) are evaluated before configuration loading. Config loading errors (exit 3) are evaluated before runtime actions. Runtime errors (exit 1) occur only after preflight passes.

**Docker daemon availability:**
- Unavailable at preflight (SDK cannot connect, socket unreachable) â†’ exit 3.
- Unavailable during a runtime operation (drop mid-deployment) â†’ exit 1.

### 2.5 Argument Behavior

| Command | Result |
|---|---|
| (no arguments) | Print help, exit 0 |
| `--help` | Print help, exit 0 |
| `--non-interactive` alone | Print help, exit 0 |
| `--dry-run` without a scope | Exit 2 |
| `--with-resources` without `--client`/`--full` | Exit 2 |
| `--notify` without a scope | Diagnostic message attempted, exit 0 |
| `--notify` without a scope, webhook unconfigured | Warning logged, exit 0 |
| `--notify` with modifiers (`--config-dir`, `--debug`) but no scope | Diagnostic message attempted, exit 0 |
| `--debug-deps` without a scope | Prints closure to CLI, exit 0 |
| `--debug-deps --notify` without a scope | Prints closure; attempts diagnostic message; exit 0 |
| `--debug-deps --notify` without a scope, webhook unconfigured | Prints closure; warning logged; exit 0 |
| `--debug-deps --dry-run` without a scope | Exit 2 |
| `--debug-deps` with a scope | Prints closure, then runs deployment |
| `--non-interactive --resource-pack` | Accepted, no-op |
| `--instance X` with `--full` | Exit 2 |
| `--instance X` without `--server`/`--resource-pack` | Exit 2 |
| `--client --instance X` (no other instance-scoped scope) | Exit 2 |
| `--instance X` where X not configured | Preflight error, exit 3 |

**Error precedence:** All exit 2 checks are evaluated before exit 3 checks.

### 2.6 `--dry-run` Behavior

`--dry-run` performs no writes and no docker lifecycle operations.

**Hard prohibitions:**

```
no in-game notices
no restart_wait
no docker stop
no docker start
no docker exec
no health status polling (no loop waiting on .State.Health.Status)
no server.properties modification
no filesystem writes
```

**Permitted read-only operations:**

- Read-only Docker inspection: container `.State.Status`, mount list, `.State.Health.Status` (single snapshot, not a loop).
- Read-only filesystem operations: `stat`, `exists`, `read`.
- The `restarting` bounded wait (Â§4.12) polls `.State.Status` â€” this is a distinct field from health status and is permitted.
- RCON-reachability probing is NOT permitted during dry-run. The code paths that require it (Â§4.7, Â§4.8) are unreachable during dry-run.

**`restarting` at dry-run:** a container in `restarting` state at invocation triggers the bounded wait (read-only). If it settles within the timeout, dry-run continues with the settled state. If not, dry-run exits 3 with the same failure a real run would produce.

### 2.7 `--audit-mods` Combination Rules

Allowed alongside: `--config-dir`, `--debug`.

Rejected (exit 2): `--server`, `--client`, `--resource-pack`, `--full`, `--dry-run`, `--with-resources`, `--notify`, `--debug-deps`, `--non-interactive`.

### 2.8 Naming Convention

| Context | Form |
|---|---|
| CLI flags | kebab-case |
| TOML keys | snake_case |
| TOML sections | snake_case |
| `.env` keys | snake_case |

### 2.9 `--instance` Targeting

`--instance NAME` is repeatable and comma-separated. All forms resolve to one set; duplicates collapse.

```
--instance mc-creative
--instance mc-creative,mc-survival
--instance mc-creative --instance mc-survival
```

#### Applicable scopes

| Command | Result |
|---|---|
| `--server --instance X` | Valid |
| `--resource-pack --instance X` | Valid |
| `--server --client --instance X` | Valid |
| `--client --instance X` | Exit 2 |
| `--full --instance X` | Exit 2 |

#### Partition

Without `--instance`: **all configured instances**.
With `--instance X,Y`: **{X, Y}**.

**Partition order** is lexicographic by instance name. All iteration over partition members uses this order.

#### Partition-scoped checks

| Check | Section |
|---|---|
| Container name matching | Â§3.6 |
| `mods_dir` bind consistency (server scope only) | Â§3.7 |
| Healthcheck declaration | Â§3.8 |
| Compose-vs-container mount drift | Â§3.17 |
| Container existence and state | Â§4.12, Â§8.9 |
| Resource-pack source validation | Â§7.8 |

#### Global checks

| Check | Section |
|---|---|
| Config file schema and syntax | Â§3.1 |
| Project-root path resolution | Â§3.15 |
| Notification template presence (when `--notify`) | Â§5.11 |
| Compose file readability (when required by a scope) | Â§3.5 |
| Orphan resource-pack sections | Â§7.9 |
| At least one configured instance exists | Â§2.5 |

#### Server scope with `--instance`

```
mods_dir:  NOT TOUCHED
config:    deployed to partition members
kubejs:    deployed to partition members
```

**Mods drift warning:** if `mods_dir` differs from source, warn. Comparison uses the **full source set** (the set a non-targeted server deploy would place).

Drift definition:
```
set_A = {*.jar filenames currently in mods_dir}
set_B = {*.jar filenames the full-source deploy would place in mods_dir}
drift = (set_A != set_B) OR (any filename in A âˆ© B has different SHA-256)
```

`mods_dir` is treated as flat. Subdirectories are ignored.

Warning emitted once at CLI, at WARN level. If `--notify`, also present as an informational line in the live message's server section.

#### Resource-pack scope with `--instance`

```
publish:          partition members' configured packs
server.properties: partition members only
```

#### Client scope with `--instance`

No effect.

#### Notifications with `--instance`

`{instance_list}` renders partition members (alphabetical, comma-and-space separated).
`{instance_count}` renders the number of partition members (integer).
`{requested_scopes}` renders only scope names; targeting is not encoded.

When `--instance` is used and the server scope is active, the live message's server section includes:

```
- Targeted deploy to: <partition members, comma-and-space separated>
```

When mods drift exists, additionally:

```
- Mods: not updated (targeted deploy does not touch shared mods)
```

---

## 3. Configuration

### 3.1 Sources

Increasing priority:

1. `config.d/.env` â€” file source, literal keys
2. `config.d/deploy_pack.toml`
3. CLI arguments

`docker-compose.yml` is read as enrichment when present.

### 3.2 Compose as Config Source

| Value | Source |
|---|---|
| Container name | `services.<svc>.container_name` |
| Instance path | Source of bind `target=/data` |
| Config path | `<instance_root>/config` |
| KubeJS path | `<instance_root>/kubejs` |
| `server.properties` path | `<instance_root>/server.properties` |
| `mods_dir` | Source of bind `target=/data/mods` |
| `www_dir` | Source of bind `target` under `/usr/share/nginx/` |
| RCON secret path | `services.<svc>.secrets[]` â†’ `secrets.<name>.file` |
| Stop grace period | `services.<svc>.stop_grace_period` |
| Stop signal | `services.<svc>.stop_signal` |

**Stop behavior:**
```
stop_grace_period â†’ container.stop(timeout=N). Default 10 seconds.
stop_signal      â†’ logged for diagnostics only. Not passed to SDK.
```

**`stop_grace_period` parsing:** the compose value is a Go duration string (`"30s"`, `"1m30s"`, `"2m"`). It is parsed as a duration and converted to integer seconds. Unparseable values â†’ preflight error, exit 3. The default when the key is absent is 10 seconds.

Health timing is NOT derived from compose.

### 3.3 "Minecraft Service" Definition

The compose service matched by an `[instances.*]` entry via `container_name`. Non-Minecraft services (velocity, bluemap, nginx, nfs) are not Minecraft services.

### 3.4 Conflict Resolution

| Value | Winner | Mismatch |
|---|---|---|
| `mods_dir` | Compose | Warn |
| Container name | Compose | Preflight error (no match) |
| `www_dir` | **TOML** | Warn |
| `sync_root`, `modpack_dir` | TOML | â€” |
| Health timeout | TOML or 600 | â€” |

**Note:** `.env` is intended for secrets. Non-secret configuration belongs in `deploy_pack.toml`.

### 3.5 Compose Failure Scoping

| Command | Compose broken |
|---|---|
| `--client` alone | Warning, proceed (exception: `www_dir` undeterminable â†’ exit 3) |
| `--server` | Exit 3 |
| `--resource-pack` | Exit 3 (exception: zero packs configured for partition â†’ no compose read) |

### 3.6 Instance Discovery

`instance_discovery = "explicit"`.

Partition-scoped:
```
[instances.<name>].container must match exactly one
services.<service>.container_name
```

Zero matches â†’ exit 3. Multiple matches â†’ exit 3.

### 3.7 Shared `mods_dir` Consistency

Partition-scoped. Only when server scope is active and `mods_dir` will be touched.

Every partition-member Minecraft service must declare `target=/data/mods` bind.

Missing bind â†’ exit 3.

Different host sources across partition members â†’ exit 3.

Fallback to `<instance_root>/mods` is not supported.

TOML/compose disagreement â†’ warning only. Compose authoritative.

Skipped entirely for `--server --instance X` (targeted).

### 3.8 Required Docker Healthcheck

Partition-scoped. Every partition-member Minecraft service must declare a healthcheck in compose.

Missing â†’ exit 3.

### 3.9 Main Config File

```toml
instance_discovery = "explicit"

sync_root    = "./sync"
modpack_dir  = "./sync/downloads"

# www_dir optional â€” derived from compose if absent. TOML wins on conflict.
# www_dir = "./www"

output_filename   = "minecraft_client_{date}.zip"
download_base_url = "http://minecraft/downloads"

protect_file = "./.deploy_protect"   # optional

[sync_mapping]
config        = "config"
kubejs        = "kubejs"
resourcepacks = { resource_pack = "@www/resourcepacks", client = "resourcepacks" }

[restart_policy]
"config/*"                  = "restart"
"kubejs/client_scripts/*"   = "none"
"kubejs/server_scripts/*"   = "reload"
"kubejs/startup_scripts/*"  = "restart+pack"
"kubejs/assets/*"           = "none+pack"
"kubejs/data/*"             = "reload"
"mods/*"                    = "restart"
# Default for unlisted paths: restart

[instances.survival]
container   = "mc-survival"
config_mode = "merge"
kubejs_mode = "delete"

[instances.creative]
container   = "mc-creative"
config_mode = "merge"
kubejs_mode = "delete"

[resource_pack.survival]
filename = "pack.zip"
required = true
prompt   = "Server resource pack"

[resource_pack.creative]
filename = "creative.zip"
required = false
prompt   = ""

[docker]
compose_file                        = "./docker-compose.yml"
restart_wait_seconds                = 300
in_game_notice_required             = true
health_timeout_seconds              = 600
health_poll_seconds                 = 2
preflight_restarting_wait_seconds   = 30
cancel_notice_ready_timeout_seconds = 30
restart_notice_template             = "Server pack updated. Restart in {time}."
restart_cancel_notice_template      = "Server restart canceled; the deployment could not safely proceed."
# rcon_host = "10.0.0.5"            # optional; RCON-only remote transport

[discord.tags]
player_roles   = ["123456789012345678"]
operator_roles = ["234567890123456789"]

[discord.messages.live]
template = """
{player_tags} **Minecraft Deployment**

{section_server}
{section_client}
{section_resource_pack}

{dry_run_marker}
Completed {timestamp}

Built by deploy_pack {tool_version}
"""

[discord.messages.online]
template = """
{player_tags} **Server online and healthy**

{container_status}
"""

[discord.messages.failure]
template = """
{operator_tags} **Automated deployment failed**

Stage: `{failure_stage}`
Error: {error}

**Container status**
{container_status}

{timestamp}
"""

[discord.messages.diagnostic]
template = """
**deploy_pack webhook test**

Tool version: {tool_version}
Timestamp: {timestamp}

If you can read this, the Discord webhook is configured correctly.
"""
```

**`[docker]` key descriptions:**

| Key | Description | Negative handling |
|---|---|---|
| `compose_file` | Path to `docker-compose.yml`. | â€” |
| `restart_wait_seconds` | Delay after in-game restart notice, before stop. Integer â‰¥ 0. `0` = no wait. | `< 0` â†’ exit 3 |
| `in_game_notice_required` | If true, RCON notice failure aborts the deployment. | â€” |
| `health_timeout_seconds` | Total timeout waiting for container health after start. Integer > 0. | `â‰¤ 0` â†’ exit 3 |
| `health_poll_seconds` | Poll interval for health status checks. Also used for the `restarting` bounded wait and the cancel-notice readiness wait. Integer > 0. | `â‰¤ 0` â†’ exit 3 |
| `preflight_restarting_wait_seconds` | Global bounded wait for containers in `restarting` state at preflight, in seconds. `0` disables. | `< 0` â†’ exit 3 |
| `cancel_notice_ready_timeout_seconds` | Bounded wait for a recovered container to become RCON-reachable before sending the cancel notice, in seconds. `0` disables. | `< 0` â†’ exit 3 |
| `restart_notice_template` | In-game notice text. Required, non-empty. `{time}` permitted (may appear more than once; each occurrence is substituted with `<N> seconds`). Any other `{...}` placeholder â†’ exit 3. | Empty â†’ exit 3; unknown placeholder â†’ exit 3 |
| `restart_cancel_notice_template` | In-game cancel-notice text. Required, non-empty, no placeholders. | Empty or contains `{...}` â†’ exit 3 |
| `rcon_host` | Optional. If set, RCON transport uses this host and only direct TCP is available. | â€” |

**`[resource_pack.X]` key descriptions:**

| Key | Description | Default | Missing / invalid |
|---|---|---|---|
| `filename` | Name of the resource pack ZIP under the client mapping source path. Validated per Â§7.5. | â€” | Missing or invalid â†’ exit 3 |
| `required` | Boolean. Written to `require-resource-pack` in the target instance's `server.properties`. | â€” | Missing â†’ exit 3 |
| `prompt` | String. Written to `resource-pack-prompt` in the target instance's `server.properties`. Empty string is valid and means no prompt. | `""` | â€” |

### 3.10 Sync Mapping Source Resolution

Relative mappings resolve under `sync_root`. The **resolved path** is what subsequent sections reference as "the config source" or "the kubejs source."

### 3.11 Side Overrides File

`config.d/side_overrides.toml`:

```toml
[by_id]
"231951" = "both"

[by_filename]
"applied_kjs-1.0.0.jar" = "both"

[deployment_tool_review]
# Last generated: 2026-09-21T14:32:00Z
"unknown.jar" = "client"
```

Precedence: `by_id` > `by_filename` > `deployment_tool_review`.

Valid values: `"client"`, `"server"`, `"both"`, `"skipped"`.

**Invalid values** (any string outside the valid set, or any non-string) â†’ preflight error, exit 3. Values are case-sensitive.

**Missing file:** absent `side_overrides.toml` is treated as empty (no overrides). No error.

`--audit-mods` replaces `[deployment_tool_review]` entirely on save. The `# Last generated:` comment is written by the audit tool on save, using the current UTC timestamp in `%Y-%m-%dT%H:%M:%SZ` format. A manually written comment is overwritten on next save.

### 3.12 Secrets File

`config.d/.env` (gitignored) is a flat key-value file consumed by `ConfigCore` as a literal source. Keys are used verbatim â€” no prefix stripping, no separator expansion, no case folding.

The Discord webhook URL is stored under the key `webhook_url`.

**Missing or empty** and `--notify` passed â†’ warning logged, notification skipped, exit code unaffected.

**Malformed** (present but not parseable as a Discord webhook URL by the URL parser) â†’ warning logged at WARN level, notification skipped, exit code unaffected. Malformed URL is treated identically to missing.

### 3.13 Protect File

`.deploy_protect` optional.

- Missing â†’ silent no-op
- Present â†’ log count
- Empty â†’ warn

**File syntax:**
```
One pattern per line.
Lines starting with '#' (after leading whitespace) are comments.
Blank lines are ignored.
Leading and trailing whitespace on each line is stripped before
the pattern is added.
```

**Pattern syntax:** POSIX glob via `fnmatch`.

```
"tokens.json"              matches at any depth
"config/tokens.json"       matches exact relative path
"*.key"                    matches "foo.key" and "config/foo.key"
                           (fnmatch's * spans "/")
"config/*/tokens.json"     multi-level
```

A file is protected if the pattern matches the full relative path OR the pattern matches any single path component of the file's path.

Protection applies to every clean operation, including `mods_dir`.

**Overwrite vs protect:** protection governs *deletion*. A protected file that also appears in the source tree is overwritten with the source content. Protect does not prevent overwrites; it prevents deletion.

**Client ZIP:** protect patterns are not applied.

### 3.14 Removed Configuration

`exclude_file`, `.rsync_exclude`, `player_tag`, `operator_tag`, `[notifications.*]`, the entire env-var source (Â§3.16), per-instance `restart_policy`.

### 3.15 Relative Path Resolution

All relative paths in `deploy_pack.toml` and `config.d/.env` resolve against the **project root** â€” parent directory of `config.d/`.

`--config-dir` changes the project root.

### 3.16 Environment-Like Sources

There is no OS environment variable loading. `config.d/.env` is a flat key-value file consumed by `ConfigCore` as a literal source. Keys are used verbatim (no prefix stripping, no separator expansion, no case folding). The file is gitignored and is intended for secrets and environment-specific values that must not be committed.

### 3.17 Docker-Compose Drift Check

Partition-scoped.

```
normalize(compose-derived host source) == normalize(container mount Source)
```

Normalization: `os.path.realpath` on both; strip trailing slashes.

Required mounts: `/data`, `/data/mods` (the latter skipped when the server scope will not touch `mods_dir`).

If `realpath` fails on either side (broken symlink, permission denied) â†’ exit 3.

Mismatch â†’ exit 3. Missing destination in running container â†’ exit 3.

**Symlink assumption:** the drift check assumes symlinks resolve identically on the host where `deploy_pack` runs.

**Extra container mounts** are permitted and ignored. The check is one-directional.

**Runtime health check presence:** the running container must expose `.State.Health`. If a running container lacks it â†’ preflight error, exit 3.

**Stopped containers:** `.State.Health` is not present for stopped containers; this is not a drift error at preflight.

**Accepted limitation:** a stopped container created before the compose healthcheck was added passes preflight and fails at post-start with `health_timeout` (Â§8.3). The tool does not pre-validate stopped containers' health check configuration without starting them.

**Local Docker only.** Remote Docker SDK connections are not supported.

### 3.18 Instance Root Derivation

`<instance_root>` = host source of bind `target=/data`.

Subpaths beneath:
```
<instance_root>/config
<instance_root>/kubejs
<instance_root>/world
<instance_root>/server.properties
```

Additional per-path binds are enrichment only.

`<mods_dir>` derived independently from `target=/data/mods`.

### 3.19 `www_dir` Derivation

When `www_dir` not in TOML:

Find bind mounts whose container target **starts with** `/usr/share/nginx/`.

- Exactly one â†’ `www_dir` = host source
- Zero â†’ cannot determine, exit 3 for `--client`
- Multiple â†’ cannot determine, exit 3 for `--client`. Candidates logged at WARN level, one host source path per line.

**Exact target `/usr/share/nginx` (no subpath) does not match** and is ignored.

If TOML specifies `www_dir`, compose-derived value is informational only.

---

## 4. Deployment Runtime

### 4.1 Full Runtime Sequence

```
parse arguments
    â†“
resolve configuration
    â†“
determine deployment partition
    â†“
preflight
    â”œâ”€ aggregate config / source / runtime prerequisites
    â”œâ”€ validate in-game templates (Â§3.9)
    â”œâ”€ validate reachable Discord templates if --notify
    â”œâ”€ calculate effective changes
    â”œâ”€ resolve per-path action
    â”œâ”€ merge actions into per-instance effective_action
    â”œâ”€ partition into none_set / reload_set / restart_set
    â”œâ”€ inspect running containers (read-only)
    â”œâ”€ bounded wait on restarting containers
    â””â”€ verify compose-vs-container mount drift
    â†“
preflight failure? â”€â”€ yes â†’ console only â†’ exit 3
    â†“ no
dry-run? â”€â”€ yes â†’ report plan â†’ optional live notification (if --notify) â†’ exit 0
    â†“ no
restart_set and reload_set both empty?
    â”œâ”€ yes â†’ write-only path
    â”‚        â†“
    â”‚   perform writes
    â”‚        â†“
    â”‚   write failure?
    â”‚       â”œâ”€ yes â†’ Â§4.7 handling â†’ exit 1
    â”‚       â””â”€ no  â†’ live notification (if --notify) â†’ exit 0
    â””â”€ no
         â†“
if restart_set non-empty:
    warned_and_running = compute_warned_and_running()
        # restart_set members running at preflight capture
        # AND running at the pre-notice re-inspection (Â§4.5)
    attempt restart notices to warned_and_running members
    all-or-nothing: if any notice fails AND in_game_notice_required:
        cancel-notice to members whose notice was attempted
        (see Â§8.5 for recipient definition)
        exit 1, no writes, no stops
    if any member received a notice:
        wait restart_wait_seconds once (no-op if 0)
    to_stop = {m âˆˆ warned_and_running : m is running at re-inspection
                                         immediately before stop}
    stop every container in to_stop
    stop failure? â†’ Â§8.8 for full recovery flow â†’ exit 1
         â†“
perform writes
    write failure? â†’ Â§4.7 handling â†’ exit 1
         â†“
if reload_set non-empty:
    RCON reload running members
    any failure? â†’ recover stopped containers, failure notification (if --notify), exit 1
         â†“
live notification (if --notify)
         â†“
if restart_set non-empty:
    start every container this deployment stopped
    collect start results
    wait for health on started containers
    any_start_failure OR any_health_failure?
        â”œâ”€ yes â†’ failure notification (if --notify, stage per Â§4.13) â†’ exit 1
        â””â”€ no
             actual_restarts > 0?
                 â”œâ”€ yes â†’ online notification (if --notify) â†’ exit 0
                 â””â”€ no  â†’ exit 0
else:
    exit 0
```

**All Discord notifications â€” live, online, failure, and diagnostic â€” are conditional on `--notify`.** CLI output is always printed regardless of `--notify`.

### 4.2 Scope Execution Order

`server â†’ client â†’ resource-pack`. Halt on first runtime failure. No rollback of completed scopes.

### 4.3 Preflight Failure Aggregation

All independently detectable failures collected before any write.

No Discord notification fires for preflight failures. Console only. Exit 3.

### 4.4 "Changed" Definition

A file or managed property is changed only when effective destination content differs from source, or effective managed config differs from source.

Detection:
- Files: **SHA-256** comparison
- `server.properties` keys: current value differs from target value
- Resource-pack publication: destination missing OR destination **SHA-1** differs from source SHA-1

**SHA-1 is used only where `resource-pack-sha1` requires it.** Comparison is case-insensitive (normalize to lowercase).

Missing `resource-pack-sha1` key â†’ effective change (first-time deploy).

Matching SHA-1 AND destination exists with matching SHA-1 â†’ no effective change for publication.

Prompt-only change (only `resource-pack-prompt` differs) â†’ write + defer, no restart.

**Unchanged files:** files whose source and destination content match by SHA-256 are not copied. They remain in place (Â§7.2).

**Independence:** resource-pack ZIP publication and `server.properties` `resource-pack-sha1` are evaluated separately.

### 4.5 Lifecycle Vocabulary

```
Deployment partition
    Partition members this invocation operates on.

Effective change for member m
    Destination content differs from source for m.

Effective action for member m
    Sticky-max across changed paths, merged with resource-pack
    evaluation. See Â§4.6.

none_set / reload_set / restart_set
    Partition members partitioned by effective_action.

warned_and_running
    Set of restart_set members that:
        - were running at preflight capture
        - were running at the pre-notice re-inspection
    This set is computed once, before notice dispatch. A member that
    later stops (before the stop phase) still counts toward the
    restart-wait condition if it received a notice.
    Membership describes eligibility, not notice delivery: a member
    may be in this set and fail to receive its notice.

to_stop
    Subset of warned_and_running whose containers are still running
    at the pre-stop re-inspection.

Actually stopped by this deployment
    A container for which docker stop performed a real stop (not a
    no-op). A container that exited before the stop call and returned
    a no-op is NOT "actually stopped by this deployment." Such
    containers are treated as externally stopped and are not started
    in recovery (Â§4.7, Â§4.11, Â§8.10).

Actual restarts
    Containers in to_stop that:
        - were actually stopped by this deployment
        - were successfully started by this deployment
    Used only to gate the online notification on the success path.
    Failure paths do not consult it.

Lifecycle-touched container
    Any container that this deployment actually stopped, actually
    started, actually reloaded, or attempted to start or reload.
    A container whose stop was a no-op is NOT lifecycle-touched
    (it exited independently of the deployment).
    The phrase "this deployment" excludes any container the tool
    did not directly act on.

Warned container
    A container in warned_and_running that received an in-game
    restart notice.
```

Dry-run reports all three sets and per-instance change details.

### 4.6 Restart Policy Adapter

#### 4.6.1 Configuration

Per-path classification in `[restart_policy]`. Matching: **longest literal prefix** (substring before first `*`). Ties: total pattern length (longer wins), then alphabetical (lexicographically smaller wins).

Unlisted paths default to `restart`.

#### 4.6.2 Action Vocabulary

| Action | Server lifecycle | Pack rebuild |
|---|---|---|
| `none` | none | no |
| `none+pack` | none | yes |
| `reload` | RCON reload | no |
| `reload+pack` | RCON reload | yes |
| `restart` | full cycle | no |
| `restart+pack` | full cycle | yes |

**Ordering:**
```
none < none+pack < reload < reload+pack < restart < restart+pack
```

**`+pack` stickiness:**
```
max(reload+pack, restart) = restart+pack
max(none+pack, reload)    = reload+pack
```

`pack_required` boolean is derived from the resulting action (true iff action ends in `+pack`).

#### 4.6.3 Adapter Output

```
effective_action: one of the six actions
pack_required:    bool
reasons:          [(path_prefix, action, changed_paths_example)]
```

Computation:
```
1. Determine changed path set.
2. Resolve action per path.
3. effective_action = sticky-max over all changed paths.
4. pack_required = effective_action ends in +pack.
5. reasons = paths that contributed to the effective action.
```

#### 4.6.4 Aggregation

```
none_set    = {m âˆˆ partition : effective_action(m) âˆˆ {none, none+pack}}
reload_set  = {m : effective_action(m) âˆˆ {reload, reload+pack}}
restart_set = {m : effective_action(m) âˆˆ {restart, restart+pack}}
```

`pack_required` at scope level = OR over partition members.

#### 4.6.5 Removed

Per-instance `restart_policy` config key removed. Targeting is `--instance` only.

#### 4.6.6 Resource-Pack Action Merge

Resource-pack evaluation produces per-instance action:
```
resource-pack-prompt only changed  â†’ none
require-resource-pack changed      â†’ restart
resource-pack changed              â†’ restart
resource-pack-sha1 changed         â†’ restart
```

Merged with server-scope action via sticky-max.

#### 4.6.7 `--full` Interaction

```
--full requested
    â†“
server scope writes normally
resource-pack scope evaluates normally
    â†“
partition into none_set / reload_set / restart_set
    â†“
restart_set non-empty â†’ normal stop/wait/start/health cycle
reload_set non-empty  â†’ RCON reload after writes
neither               â†’ write-only deployment
```

Same applies to `--server` alone. For `--server --instance X`, `mods_dir` is excluded from the write set (Â§2.9).

#### 4.6.8 RCON Reload Path

For each running reload_set instance, after writes:
```
container.exec_run(["rcon-cli", "reload"]) or direct TCP
log response
```

No stop, no wait, no start, no health poll. Stopped instances skipped.

Failure â†’ `failure_stage = reload`; recover stopped containers (see Â§4.8); failure notification (if `--notify`); exit 1.

#### 4.6.9 `pack_required` Signal

When scope-level `pack_required` is true:

```
--client in CLI scope:
    ZIP rebuilt; informational only
--client NOT in CLI scope:
    CLI warning at WARN level:
        "client pack content changed; the current ZIP is stale.
         Run --client to rebuild."
    no ZIP built; exit code unaffected
```

Warning emitted once at CLI during preflight planning. If `--notify`, also present as an informational line in the live message's server section (not in the failure message). The warning content is cached from preflight to the notification render.

#### 4.6.10 Adapter Reasons in Notifications

Live message server section includes effective action and causes.

Standard example:

```markdown
### Server
- Mods deployed: 128
- Config/KubeJS deployed: survival, creative
- Restart action: restart
- Pack required: yes
- Reasons:
  - `mods/*` changed (2 added, 1 removed)
  - `kubejs/startup_scripts/*` changed (new_block.js)
```

With `--instance`:

```markdown
### Server
- Targeted deploy to: mc-creative
- Mods: not updated (targeted deploy does not touch shared mods)
- Config/KubeJS: 3 files changed
- Restart action: reload
- Pack required: no
- Reasons:
  - `kubejs/server_scripts/*` changed (craft.js)
- No container restart performed
```

The `Mods: not updated` line is emitted only when mods drift exists (Â§2.9). When mods drift does not exist, the line is omitted.

`Targeted deploy to:` value is comma-and-space separated, matching `{instance_list}` (Â§5.4).

### 4.7 Write Failure Handling

**Server-scope write failure:**

```
if any containers are currently actually stopped by this deployment:
    DO NOT restart them
    failure notification (if --notify, stage=mid_scope_write)
    print recovery block to CLI (always)
    exit 1
else:
    failure notification (if --notify, stage=mid_scope_write)
    print failure summary to CLI (always)
    exit 1
```

**"Actually stopped by this deployment"** is defined in Â§4.5. No-op-stopped containers are treated as externally stopped and are not started in recovery.

The recovery block is printed to CLI regardless of `--notify`. Never sent to Discord.

**CLI output format:**

- **Failure summary line:** `<failure_stage>: <error>` (single line, printed to stdout).
- **Per-container status:** rendered as `<instance_name>: <state>` per line, matching `{container_status}` (Â§5.8).

Recovery block example:
```
RECOVERY REQUIRED

A server-scope write failed. Minecraft containers are stopped and
were not automatically restarted.

Affected instances:
  mc-survival: stopped
  mc-creative: stopped

Reason:
  <failure summary>

Recovery steps:
  1. Investigate the failure and repair the source.
  2. Re-run deployment once the source is correct.
     Note: a same-day re-run overwrites the day's client ZIP if
     --client is in scope, and the day's resource-pack ZIP if
     --resource-pack is in scope.
  3. Or start containers manually with:
       docker start mc-survival mc-creative
```

"Affected instances" lists only containers **currently actually stopped by this deployment**.

**Non-server-scope write failure:**

```
if any containers are currently actually stopped by this deployment:
    start every container actually stopped by this deployment
    wait up to cancel_notice_ready_timeout_seconds for RCON-reachability
    (parallel, poll interval = health_poll_seconds; best-effort;
     see Â§8.4 for reachability test)
    send restart_cancel_notice_template to those that respond
    any_start_failure or any_health_failure in this recovery:
        reported in the failure notification's per-container status,
        and printed to CLI regardless of --notify
failure notification (if --notify, stage=mid_scope_write)
print failure summary to CLI (always)
print per-container status to CLI (always, format per Â§5.8)
exit 1
```

**Recovery start failure stage:** in recovery paths (Â§4.7, Â§4.8), the failure notification's `{failure_stage}` is the **original stage** (`mid_scope_write` or `reload`) regardless of subsequent recovery start failures. Recovery start failures are reported only in `{container_status}` and CLI.

### 4.8 Reload Failure Handling

After successful writes:

```
reload failure for a running reload_set instance
    â†“
if any containers actually stopped by this deployment:
    start them (recovery, best-effort)
    wait for RCON-reachability
        (parallel, poll interval = health_poll_seconds,
         bounded by cancel_notice_ready_timeout_seconds)
    send restart_cancel_notice_template to responders
    any start failure or health failure in this recovery:
        reported in failure notification per-container status,
        and printed to CLI regardless of --notify
failure notification (if --notify, stage=reload)
print failure summary to CLI (always)
print per-container status to CLI (always, format per Â§5.8)
exit 1
```

Successful writes are not rolled back.

### 4.9 Resource-Pack Publish Ordering

```
1. validate all source ZIPs
2. compute all SHA-1 values
3. resolve all destinations and URLs
4. publish all resource-pack artifacts (atomic per file)
5. update server.properties (atomic per file)
```

### 4.10 Atomic Publication for Individual Artifacts

```
temp_path = <target_dir>/<target_name>.tmp.<pid>
write to temp_path
fsync(temp_path)
if destination exists:
    st = os.stat(destination)
    try:
        os.chmod(temp_path, st.st_mode)
    except (PermissionError, OSError) as exc:
        log warning at WARN level
    try:
        os.chown(temp_path, st.st_uid, st.st_gid)
    except (PermissionError, OSError) as exc:
        log warning at WARN level
os.replace(temp_path, destination)
```

`<target_dir>` is the same directory as the destination. `os.replace` requires same-filesystem source and target; same-directory placement guarantees this.

Applies to:
- Client ZIP
- Changelog HTML
- Resource-pack ZIP
- `server.properties`
- `side_overrides.toml` (Â§6.1)

**For a fresh destination (no existing file):** mode and ownership default to the process umask and current user. Operators requiring specific ownership for fresh artifacts must run the tool with appropriate privileges.

**For an existing destination:** mode and ownership are preserved. `os.chmod` and `os.chown` failures log and continue.

**Durability:** fsync is applied to the temp file before `os.replace`. Directory-level fsync after replace is not performed. If a crash occurs between `os.replace` and the next directory sync, the rename may not be durable.

**Ownership caveat:** if the deploy tool runs as a user without chown privileges and the destination was owned by a service uid, the replaced file is owned by the deploy user. Operators relying on specific ownership must run the tool with sufficient privileges.

### 4.11 Protected Files Outside Deployment Set

The "effective mod set" is the set of mod jars the deployment intends to place in `mods_dir`.

Protected files in `mods_dir` that are **not** in the source set are outside the effective mod set. They survive clean and are not counted, logged, or reported as deployment deltas.

Protected files in `mods_dir` that **are** in the source set are overwritten with source content (Â§3.13).

`mods_dir` is treated as flat. Subdirectories are ignored.

### 4.12 Container State Policy

Partition-scoped. Captured at preflight.

| State | Handling |
|---|---|
| `running`, `health=healthy` | Eligible |
| `running`, `health=starting` | Eligible; informational log at INFO level |
| `running`, `health=unhealthy` | Eligible; **warning logged at WARN level** at preflight (including during dry-run); if subsequent health check times out, failure notification's `{error}` is prefixed with a note that the container was unhealthy before the deployment |
| `running`, no `.State.Health` | Preflight error, exit 3 |
| `exited` | Treated as stopped |
| `created` | Treated as stopped |
| `stopped` | Treated as stopped |
| `paused` | Exit 3 |
| `restarting` | Global bounded wait: up to `preflight_restarting_wait_seconds` total, polling all `restarting` containers in parallel at `health_poll_seconds` interval. Re-inspect the previously-restarting containers after the wait. Still `restarting` â†’ exit 3 |
| `removing` | Exit 3 |
| `dead` | Exit 3 |
| missing | Exit 3 (Â§8.9) |

**Setting `preflight_restarting_wait_seconds = 0`** disables the bounded wait. Any `restarting` container at preflight triggers exit 3 immediately.

**Stop eligibility** requires the container to be running at **all three** of:
- preflight capture (Â§4.12)
- pre-notice re-inspection (Â§4.14 step 3, Â§4.5)
- pre-stop re-inspection (Â§4.14 step 6)

Containers not running at all three of those moments are not affected. The tool does not track transitions between observations; the three-moment test is authoritative.

**Warning emission for unhealthy containers:** emitted whenever a partition-member container is observed `unhealthy` at preflight, regardless of whether it is in restart_set.

### 4.13 Multi-Container Start Failures

Attempt to start every container this deployment actually stopped. Do not halt on first failure.

After all attempts:
- Wait for health on containers that started successfully
- Collect results: `any_start_failure`, `any_health_failure`
- If `any_start_failure` OR `any_health_failure`:
    - **Failure stage selection:**
        - If `any_start_failure`: `failure_stage = post_hook`
        - Else (only health failures): `failure_stage = health_timeout`
        - If both: `post_hook` takes precedence; error text notes the health timeouts (see also Â§5.10)
    - Send failure notification if `--notify`
    - Print per-container status to CLI regardless of `--notify` (format per Â§5.8)
    - Exit 1

### 4.14 Mixed Reload / Restart Batching

```
1. Compute effective_action per partition member.
2. Partition into none_set / reload_set / restart_set.
3. warned_and_running = {m âˆˆ restart_set :
                          m was running at preflight capture
                          AND m is running at the pre-notice
                          re-inspection (Â§4.5)}
4. Restart notice dispatch to warned_and_running:
   - in_game_notice_required = true: all-or-nothing.
     If any notice fails, cancel-notice to all members of
     warned_and_running whose notice was attempted (i.e., the prefix
     of warned_and_running up to and including the failing member,
     in partition order), then exit 1, no writes, no stops.
   - in_game_notice_required = false: skip failures, continue.
5. If any member of warned_and_running received a notice:
     wait restart_wait_seconds once (no-op if 0).
6. to_stop = {m âˆˆ warned_and_running : m is running at
               re-inspection immediately before stop}.
   If to_stop is empty (all members exited in the interim), log a
   WARN-level message and continue with no stop.
   Stop every container in to_stop.
   Stop failure â†’ Â§8.8 recovery flow â†’ exit 1.
7. Perform writes. Write failure â†’ Â§4.7 handling â†’ exit 1.
8. If reload_set non-empty:
     RCON reload running members.
     Failure â†’ recover stopped containers, failure notification
     (if --notify), exit 1.
9. Live notification (if --notify).
10. If restart_set non-empty:
      start every container this deployment actually stopped
      collect start results
      wait for health on started containers
      any_start_failure OR any_health_failure?
          yes â†’ failure notification (if --notify; stage per Â§4.13)
                â†’ exit 1
          no  â†’ actual_restarts > 0?
                    yes â†’ online notification (if --notify) â†’ exit 0
                    no  â†’ exit 0
    else:
      exit 0
```

### 4.15 Prompt-Only Resource-Pack Deferral

Only `resource-pack-prompt` differs â†’ write + defer, no restart. Activates at next natural restart. If other restart-required keys also changed, restart wins.

**Empty prompt:** if `[resource_pack.X].prompt = ""`, the tool writes `resource-pack-prompt=` (empty value). Minecraft treats an empty prompt as no prompt. The key is not removed.

**Default prompt:** if `[resource_pack.X]` omits `prompt`, it defaults to the empty string. The key is written with an empty value.

### 4.16 Restart Wait Behavior

```
Wait restart_wait_seconds once iff at least one member of
warned_and_running received a restart notice.

A value of 0 means no wait; the stop phase begins immediately
after notice dispatch.

If in_game_notice_required = false and some members' notices failed,
the wait is still performed once based on the members that did
receive notices.

A container in warned_and_running is stopped even if its notice
attempt failed (when in_game_notice_required = false).
warned_and_running describes eligibility, not delivery.

Unwarned members (never in warned_and_running) are not stopped.
```

### 4.17 Dry-Run Notification Lifecycle

`--dry-run --notify` attempts to send exactly one notification via `[discord.messages.live]` with `{dry_run_marker}` populated. The notification is attempted only if preflight succeeds, and is subject to Â§3.12.

No online or failure Discord notifications are sent. No in-game cancellation notices are sent.

`--dry-run` without `--notify` reports the plan to CLI only, exit 0. No notification is attempted.

**Diagnostic template under `--dry-run`:** `--dry-run` requires a scope, so `[discord.messages.diagnostic]` is never reached under `--dry-run`. Only `live` is validated and sent.

---

## 5. Notifications

### 5.1 Template Location

| Template | Trigger |
|---|---|
| `[discord.messages.live]` | Write phase completed successfully |
| `[discord.messages.online]` | At least one container actually restarted AND all restarted containers healthy |
| `[discord.messages.failure]` | Runtime failure post-preflight |
| `[discord.messages.diagnostic]` | `--notify` alone (webhook test) |

**All Discord notifications â€” live, online, failure, and diagnostic â€” are conditional on `--notify`.** CLI output is always printed regardless of `--notify`.

### 5.2 Template Selection

```
one or more deployment scopes â†’ [discord.messages.live]
zero deployment scopes        â†’ [discord.messages.diagnostic]
```

### 5.3 Live Scope Bitmask

```
server = 1 | client = 2 | resource-pack = 4
```

`--full` â†’ 7. Sections render only when their bit is set.

### 5.4 Live Placeholders

```
{tool_version} {timestamp} {requested_scopes} {instance_count}
{instance_list} {dry_run_marker} {player_tags} {operator_tags}
{section_server} {section_client} {section_resource_pack}
```

- `{requested_scopes}` â€” comma-and-space separated list of scope names in fixed order: `server`, `client`, `resource-pack`. Targeting is not encoded.
- `{instance_list}` â€” partition members, alphabetical, comma-and-space separated.
- `{instance_count}` â€” integer count of partition members.
- `{dry_run_marker}` â€” under `--dry-run`, the fixed string `DRY RUN â€” no changes applied`. Under normal deploy, an empty string.
- `{timestamp}` â€” RFC 3339 with Z suffix: `strftime("%Y-%m-%dT%H:%M:%SZ")`.

### 5.5 Online Placeholders

```
{tool_version} {timestamp} {player_tags} {container_status}
```

### 5.6 Failure Placeholders

```
{tool_version} {timestamp} {operator_tags} {failure_stage}
{error} {container_status}
```

### 5.7 Diagnostic Placeholders

```
{tool_version} {timestamp}
```

### 5.8 `{container_status}` Scope and Vocabulary

**Online:** only containers actually restarted (members of `to_stop` successfully stopped and started).

**Failure:** all lifecycle-touched containers.

| State | Where rendered | Notes |
|---|---|---|
| `online, healthy` | both | |
| `online, starting` | failure only | unreachable in online |
| `online, unhealthy` | failure only | unreachable in online |
| `stopped` | failure | was already stopped at preflight |
| `stopped by deployment` | failure | running at pre-stop, actually stopped by this deployment (non-no-op stop) |
| `recovery start failed` | failure only | stopped by deployment, recovery start attempt failed |
| `recovery start succeeded` | failure only | stopped by deployment, recovery start succeeded; currently running |
| `reloaded` | failure | RCON reload issued successfully |
| `reload failed` | failure | RCON reload issued, failed |
| `start failed` | failure | post-phase start attempt failed |
| `health timeout` | failure | post-phase start succeeded, health poll timed out |
| `cancelled` | internal | CLI output and result model only |
| `exited before stop` | internal | was in `to_stop`, exited before the stop call; stop was a no-op; not lifecycle-touched, not an actual restart, not eligible for recovery start |

Rendering: `<instance_name>: <state>`

**Internal states** (`cancelled`, `exited before stop`) are rendered in CLI output but not in Discord messages. They are captured by the deployment result model so operators can see the complete picture via CLI, but they do not pollute the Discord failure message with states the delivery engine does not consider actionable.

`recovery start succeeded` and `recovery start failed` are failure-message states only.

### 5.9 `{deployment_status}` â€” Removed

Not a valid placeholder.

### 5.10 `{failure_stage}` Values and Assignment

| Value | Trigger | Discord notify |
|---|---|---|
| `pre_hook` | Docker stop failed | No |
| `in_game_notice` | RCON unreachable during notice dispatch | No |
| `mid_scope_write` | Write failure | Yes |
| `reload` | RCON reload failed | Yes |
| `post_hook` | Docker start failed | Yes |
| `health_timeout` | Container never healthy (no start failures) | Yes |

**Assignment table** (explicit):

| Runtime failure | Stage |
|---|---|
| Stop phase (any container stop failure) | `pre_hook` |
| Notice dispatch (any notice failure when `in_game_notice_required=true`) | `in_game_notice` |
| Write phase (any scope) | `mid_scope_write` |
| Reload phase | `reload` |
| Start phase (any container failed to start) | `post_hook` |
| Health phase (only; no start failures) | `health_timeout` |
| Recovery-path start failure (Â§4.7, Â§4.8) | original stage preserved |

If both start and health failures occur in the same post-start phase, `post_hook` takes precedence; error text notes the health timeouts.

### 5.11 Template Validation Timing

**In-game templates** (`[docker].restart_notice_template`, `[docker].restart_cancel_notice_template`) are validated at config load time, regardless of `--notify`:

- `restart_notice_template` required, non-empty â†’ empty â†’ exit 3
- `restart_notice_template` may contain `{time}`; each occurrence is substituted with `<N> seconds`. Any other `{...}` placeholder (unknown, or any placeholder other than `{time}`) â†’ exit 3.
- `restart_cancel_notice_template` required, non-empty â†’ empty â†’ exit 3
- `restart_cancel_notice_template` must not contain any `{...}` placeholder â†’ contains placeholder â†’ exit 3

**Discord templates** are validated when `--notify` is active:

1. Determine reachable templates.
2. Validate all reachable templates before any writes.
3. Missing template â†’ warn at WARN level, skip when fired, deploy proceeds.
4. Empty template â†’ exit 3, no writes.
5. Malformed template â†’ exit 3, no writes.

**Malformed template definition:** a template is malformed if it contains a placeholder that is:

- listed in Â§11.3's "Invalid everywhere" set, OR
- not listed in Â§11.3 for any template (unknown placeholder), OR
- listed in Â§11.3 for some template but not for the template in which it appears (cross-template placeholder).

All three conditions â†’ exit 3.

| Command | Templates validated |
|---|---|
| Any scope + `--notify`, `--dry-run` | live |
| Any scope + `--notify`, `--dry-run`, `--debug-deps` | live (same as without `--debug-deps`) |
| Any scope + `--notify`, not `--dry-run` | live, failure |
| Any scope + `--notify`, not `--dry-run`, `--debug-deps` | live, failure (same as without `--debug-deps`) |
| restart_set contains at least one running member, not `--dry-run` | additionally: online |
| `--notify` alone | diagnostic |
| `--debug-deps --notify` (no scope) | diagnostic |

### 5.12 Notification Failure Policy

| Scenario | Exit |
|---|---|
| Deployment success, notification success | 0 |
| Deployment success, notification failed | 0, warning |
| Deployment failure, notification success | 1 |
| Deployment failure, notification failed | 1 |

Notification is never authoritative over deployment outcome.

### 5.13 Discord Message Length

Rendered content â‰¤ 2000 chars. Over â†’ delivery failed, warning at WARN level, result unchanged.

### 5.14 `allowed_mentions` Scoping

```python
{"content": rendered_content, "allowed_mentions": {"parse": [], "roles": [...], "users": []}}
```

`roles` = union of `[discord.tags].player_roles` and `[discord.tags].operator_roles`.

### 5.15 Diagnostic Purpose

Webhook configuration test. `--debug-deps` never appears in any notification.

### 5.16 Tag Policy

`[discord.tags]` declares role IDs. Templates reference `{player_tags}` / `{operator_tags}`. Ad-hoc mentions in template text only ping if their role IDs are configured.

---

## 6. Interactive Layer

### 6.1 `--audit-mods` Mode

Loads: `config.d/side_overrides.toml`, all `.pw.toml` files from `<modpack_dir>/.index/`.

Displays per-mod: filename, declared side (with `From: SOURCE_FILE`), current override, toggles S/C/N/D.

Footer: Save & Exit / Discard Changes. Ctrl+C or Esc closes.

Toggle semantics on save:

| Toggle | Effect |
|---|---|
| S | `"<filename>" = "server"` |
| C | `"<filename>" = "client"` |
| S+C | `"<filename>" = "both"` |
| N | `"<filename>" = "skipped"` |
| D | Remove any existing review entry |

**Save procedure:**

```
1. If side_overrides.toml does not exist, the generated section is
   the entire file content (no splice). Proceed to step 7.
2. Else, read side_overrides.toml as raw text.
3. Locate [deployment_tool_review] section:
   - If present: identify its start line and end line (first line
     matching `^\s*\[` after the section header, or EOF).
   - If absent: append the newly generated section at EOF, preceded
     by a blank line.
4. Splice: replace the old section (or append) with newly generated
   section text.
5. Preserve all other content byte-for-byte.
6. Generated section line endings match the file's last existing
   line's ending style ('\r\n' if the last line ends with '\r\n',
   otherwise '\n'). For an empty file, use '\n'.
7. temp_path = side_overrides.toml.tmp.<pid> (same directory as
   target).
8. Write result to temp_path.
9. os.replace(temp_path, side_overrides.toml), preserving metadata
   per Â§4.10.
```

**Implementation mandate:** Do not parse-and-reserialize the TOML. A line-based splice preserves comments, whitespace, and key ordering outside the review section.

A crash mid-save leaves the original file intact.

### 6.2 `--non-interactive`

Skips prompt. Unmarked mods â†’ not deployed, not written, remain unmarked.

`--audit-mods --non-interactive` â†’ exit 2.

Affects mod side selection for `--server` and `--client`. No effect on `--resource-pack`.

### 6.3 Unmarked Detection

Jar without `.pw.toml`, or `.pw.toml` with side outside `{client, server, both}`.

Interactive: prompt. Non-interactive: skip.

---

## 7. Filesystem Model

### 7.1 No Staging

Direct writes. Individual artifacts atomic (Â§4.10).

- Client: `sync/downloads/` â†’ ZIP â†’ `www_dir/<resolved_output_filename>`
- Server mods: filtered `sync/downloads/` â†’ `mods_dir` (respect protect)
- Config/KubeJS: per-instance

**Output filename:**
```
resolved_output_filename =
    output_filename.format(date=strftime("%Y%m%d"))

Example:
    output_filename = "minecraft_client_{date}.zip"
    resolved        = "minecraft_client_20260922.zip"

Validation:
    - must be a single filename under www_dir
    - no path separators or traversal components
    - must end in ".zip"

Violations â†’ preflight error, exit 3.
```

**Changelog HTML filename:**
```
resolved_output_filename with trailing ".zip" replaced by ".html"
```

Changelog HTML is published to `www_dir/<changelog_filename>` using atomic write-then-rename (Â§4.10).

**Same-date re-run:**
- The new ZIP and HTML replace the existing same-name files atomically.
- Previous versions are not preserved.

**Same-date re-run when `output_filename` does not contain `{date}`:**
- Every run produces the same filename.
- The baseline is the existing file at the resolved output path (or initial build if none exists).
- Previous versions are overwritten.

**Removing the `{date}` placeholder changes baseline semantics** to single-file replacement. Historical dated ZIPs remain on disk but are not consulted.

**Old files are not deleted.** Old ZIPs, old changelog HTML, and old resource-pack artifacts accumulate. Cleanup is a separate explicit operation.

**Changelog baseline:** computed before the new ZIP is written.

```
1. If output_filename contains no "{date}" placeholder:
     baseline = existing file at resolved_output_filename
                (or initial build if none exists)

2. Else, look for a ZIP in www_dir whose name matches
   <prefix>_<date>.zip where prefix and date correspond to the
   resolved_output_filename pattern.

3. If a same-date ZIP exists, it is the baseline.

4. Else, find the most recent ZIP in www_dir whose name matches the
   resolved_output_filename pattern but with a different date.

   "Most recent" = lexicographic maximum of the date substring.
   Files that do not match the pattern are ignored.

5. If no baseline ZIP exists, initial build is reported.
```

**ZIP contents:**
```
mods/**           (filtered for client side)
config/**         (resolved sync_mapping.config)
kubejs/**         (resolved sync_mapping.kubejs)
resourcepacks/**  (when --with-resources)
```

Empty source directories produce no entries in the ZIP; the ZIP is still built.

Protect patterns are not applied to ZIP contents.

### 7.2 Clean Semantics

`mods_dir`: remove unprotected files that are **not in the source set OR whose content differs from source**, then copy new or changed files. Unchanged files remain in place.

`config`/`kubejs`:
- `config_mode = "delete"`: remove unprotected files not in source or with differing content; copy new/changed; unchanged files remain in place
- `config_mode = "merge"`: delete only source-overwritten files whose content differs; copy new/changed; unchanged files remain in place
- `kubejs_mode` always `"delete"`, same semantics as config's delete mode

**Unchanged files** (source/destination SHA-256 match, Â§4.4) are never removed or rewritten.

### 7.3 Protect Patterns

As Â§3.13.

### 7.4 `server.properties` Editing

Four keys: `require-resource-pack`, `resource-pack`, `resource-pack-prompt`, `resource-pack-sha1`.

**Encoding:** binary read/write. Line boundaries `\n` or `\r\n`.

**Match:**
```
^\s*<key>\s*=
```
Leading whitespace tolerated before the key. Whitespace tolerated between key and `=`. Everything from the first `=` to end-of-line is replaced.

**Preserve:**
- line endings per line (`\n` or `\r\n`)
- the `=` character position (only the value portion changes)
- comments, blank lines, key ordering
- all unrelated keys

**Append:** missing key appended at end. If file doesn't end with a newline, add one before appending. The appended line and its terminator use the line ending style of the file's last existing line. For an empty file, use `\n`.

**Booleans:** `true` / `false` (lowercase).

**Values:**
```
require-resource-pack â†’ [resource_pack.X].required
resource-pack         â†’ {base}/{mapping_path}/{filename}
resource-pack-prompt  â†’ configured prompt (defaults to empty string)
resource-pack-sha1    â†’ SHA-1 of source ZIP, lowercase hex
```

A property already matching target is not a change.

**Missing file:** preflight error, exit 3.

**Duplicate keys:** last occurrence authoritative; replace it; warn at WARN level.

### 7.5 Resource-Pack Mapping Authority

`resource_pack` value from `[sync_mapping].resourcepacks` = authoritative for filesystem path and URL.

**Grammar for `resource_pack`:**
```
"@www/<segment>(/<segment>)*"
where each segment is non-empty, contains no NUL, and contains
no segment equal to "." or "..".
```

Examples:
- `@www/resourcepacks` â€” valid
- `@www/packs/2026` â€” valid
- `@www` â€” invalid (no subpath) â†’ exit 3
- `@www/` â€” invalid (empty subpath) â†’ exit 3
- `@www//foo` â€” invalid (empty segment) â†’ exit 3
- `@www/./foo` â€” invalid (dot segment) â†’ exit 3
- `@www/../foo` â€” invalid (dot-dot segment) â†’ exit 3

Path normalization:
- `download_base_url`: must be non-empty â†’ exit 3 if empty. Trailing `/` stripped.
- mapping subpath (after `@www/`): leading/trailing `/` stripped
- joined: `{base}/{subpath}/{filename}`

Filename validation:
- single relative filename
- no `/`, `..`, leading `.`, NUL
- ends in `.zip`

Invalid â†’ exit 3.

Filename comparison is case-sensitive.

### 7.6 `--client --with-resources`

Include union of all `[resource_pack.<name>].filename` values, deduped (case-sensitive). Duplicate filenames appear once.

**Source validation (preflight):** every deduped filename's source must exist. Missing â†’ exit 3.

Source path resolution: `[sync_mapping].resourcepacks.client` under `sync_root`.

### 7.7 Zero-Pack Behavior

**Definition:** "Zero packs configured for the partition" means no partition member has a `[resource_pack.X]` section.

**Zero packs for partition + any scope that includes `--with-resources`:**

```
warn at WARN level
build ZIP normally, no resourcepacks/ contents
exit 0
```

**Zero packs for partition + `--resource-pack` (alone, with `--server`, or with `--instance`):**

```
valid no-op, exit 0
resource-pack scope does not read compose
resource_pack mapping key not required
no source validation
no server.properties touched
Live notification (if --notify):
    resource-pack section renders as:
        ### Resource Pack
        - Status: NOT CONFIGURED
```

**Zero packs for partition + `--full`:** as above; `--server` still requires compose; client runs normally; resource-pack section renders `NOT CONFIGURED`.

**At least one pack configured for partition:**

```
resource_pack mapping key required; missing â†’ exit 3
compose readable
all partition-member sources exist
```

### 7.8 Resource-Pack Source Validation

Partition-scoped. Every source for a partition member's configured pack must exist at the resolved path of `[sync_mapping].resourcepacks.resource_pack`.

Any missing â†’ exit 3, no resource-pack files modified, no server.properties modified. Validate all first.

### 7.9 Orphan Resource-Pack Sections

**Global.** Every `[resource_pack.<name>]` in config must have a matching `[instances.<name>]`.

Orphan â†’ preflight error, exit 3, regardless of partition.

### 7.10 Old Resource-Pack Files

Not deleted. Cleanup is a separate explicit operation.

### 7.11 Sync Source Resolution

`sync_root` and `modpack_dir` relative to project root. `.index` at `<modpack_dir>/.index`.

---

## 8. Docker Lifecycle

### 8.1 SDK

Official `docker` package. No subprocess.

### 8.2 Container Identification

Partition-scoped. `[instances.<name>].container` matched against exactly one compose service. Zero or multiple â†’ exit 3.

### 8.3 Health Check

Partition-scoped. Every partition-member service must declare a healthcheck in compose. Missing â†’ exit 3.

Runtime `.State.Health` must be present on a running container. Absent on a running container at preflight â†’ exit 3.

```
health_timeout_seconds default 600
health_poll_seconds default 2
```

TOML overrides.

Poll `.State.Health.Status` until `healthy` or timeout. Timeout â†’ exit 1, `failure_stage = health_timeout` (unless any start failure also occurred, see Â§4.13).

**Post-start absent `.State.Health`:** after starting a container, if `.State.Health` is absent, treat the container as a health failure with `failure_stage = health_timeout` and note the drift in `{error}`.

If container was `unhealthy` at preflight and times out, `{error}` is prefixed with a note about the pre-existing state.

### 8.4 RCON Transport

**Selection:**

```
rcon_host set (remote RCON):
    exactly one published RCON mapping â†’ Option B
    zero â†’ exit 3
    multiple â†’ exit 3

rcon_host unset (same-host):
    exactly one published mapping â†’ Option B preferred
    zero â†’ Option A
    multiple â†’ exit 3

No automatic fallback between transports.
```

**`RCON_PORT` resolution:**
```
1. services.<svc>.environment.RCON_PORT
   - Mapping form: environment: { RCON_PORT: "25575" }
   - List form: environment: ["RCON_PORT=25575"]
2. services.<svc>.env_file entries, left-to-right, last wins.
   This contract defines "last wins"; operators using Compose
   versions with different behavior must ensure unique keys across
   env_file entries.
3. Default 25575
```

**`env_file` base:** paths resolve relative to the compose file's directory. A missing `env_file` is a preflight error when the file would be needed to resolve `RCON_PORT`; otherwise ignored.

**If RCON_PORT set but not published:**
```
rcon_host set â†’ exit 3
rcon_host unset â†’ fall to Option A
```

**Multiple published ports to same container RCON port:** exit 3.

**Secret identification:** the RCON secret is the top-level secret named `rcon_password`, referenced by an `[instances.X]` container via `services.<svc>.secrets[]` in either short form (`["rcon_password"]`) or long form (`[{ source = "rcon_password", target = "..." }]`). The password file is resolved via `secrets.rcon_password.file`. If `rcon_password` is not declared â†’ preflight error (only when a path requires RCON).

**Secret file path:** if `secrets.rcon_password.file` is absolute, use as-is. If relative, resolve against the compose file's directory.

**Option A:** `container.exec_run(["rcon-cli", "say", message])`.

**Option B:** direct TCP to `<rcon_host or 127.0.0.1>:<published_port>`, password from `secrets.rcon_password.file` contents (stripped of trailing whitespace).

**RCON-reachability test:** a no-op RCON command (`list`) is attempted against the configured transport.

- Option A: container must be running, and `container.exec_run(["rcon-cli", "list"])` must return success within 2 seconds.
- Option B: a TCP connect to `<host>:<port>` must succeed, an RCON handshake must complete, and a `list` command must return within 2 seconds.

### 8.5 In-Game Notice Policy

If selected RCON transport fails during restart notice dispatch:

```
in_game_notice_required = true:
    for every member of warned_and_running whose notice was
    attempted (success or failure):
        best-effort cancel notice via RCON
        (the failing member is included because its message
         may have partially arrived)
    abort, exit 1, no writes, no stops

in_game_notice_required = false:
    warn, skip notice for the failed member, continue
```

Dispatch order: alphabetical by instance name (partition order, Â§2.9).

### 8.6 Restart Wait

Wait `restart_wait_seconds` once iff at least one member of `warned_and_running` received a notice.

### 8.7 In-Game Cancellation Notice

Sent when:
- Stop-phase failure and at least one container actually stopped/recovered
- Notice dispatch partial failure and at least one member of `warned_and_running` had a notice attempted
- Post-stop recovery after write, reload, or post-start failure

**Recipients:** `warned_and_running` members that are currently running, including the failing member in a partial-dispatch scenario.

Text from `[docker].restart_cancel_notice_template`.

Best-effort. Failure doesn't change exit code.

### 8.8 Pre-Hook Failure Recovery

If instance A stops but B fails to stop:

1. Start every container this deployment actually stopped
2. Send in-game cancellation to warned-and-running instances (Â§8.7)
3. Report recovery outcome to console (always)
4. If recovery itself failed (a container could not be restarted), print a copyable recovery block (always, regardless of `--notify`)
5. No Discord notification, exit 1

Recovery block:

```
RECOVERY REQUIRED

mc-survival could not be restarted.

Current state:
  mc-survival: stopped
  mc-creative: running

Recovery:
  docker start mc-survival
```

### 8.9 Missing Container Handling

Partition-scoped. Missing container â†’ preflight error, exit 3.

### 8.10 Stop Behavior

```python
container.stop(timeout=stop_grace_period_seconds)
```

`stop_grace_period_seconds` parsed from compose `stop_grace_period` as a Go duration string, converted to integer seconds. Default 10.

`stop_signal` logged only.

**Pre-stop race:** if a container exits between the pre-stop re-inspection and the `docker stop` call, and `docker stop` succeeds as a no-op:

- The stop is treated as successful for pipeline purposes.
- The container is NOT counted as an actual restart (Â§4.5).
- The container is NOT "actually stopped by this deployment" for recovery purposes (Â§4.7, Â§4.11).
- The container is NOT lifecycle-touched (Â§4.5).
- The container appears in the deployment result model with state `exited before stop` (Â§5.8). The state is CLI-only.

---

## 9. Module Decomposition

```
src/minecraft/deploy_pack/
â”œâ”€â”€ __init__.py
â”œâ”€â”€ main.py
â”œâ”€â”€ config_model.py
â”œâ”€â”€ overrides.py
â”œâ”€â”€ properties.py
â”œâ”€â”€ deps.py
â”œâ”€â”€ files.py
â”œâ”€â”€ docker_runtime.py
â”œâ”€â”€ hooks.py
â”œâ”€â”€ preflight.py
â”œâ”€â”€ scope_server.py
â”œâ”€â”€ scope_client.py
â”œâ”€â”€ scope_resource_pack.py
â”œâ”€â”€ notifications.py
â”œâ”€â”€ prompt_ui.py
â””â”€â”€ deploy_pack.py
```

### 9.1 Dependency Direction

```
main
â”œâ”€â”€ overrides
â”œâ”€â”€ preflight â†’ {config_model, docker_runtime, files, deps, properties, notification_validation, overrides}
â”œâ”€â”€ hooks â†’ docker_runtime
â”œâ”€â”€ scope_server â†’ {files, deps, overrides}
â”œâ”€â”€ scope_client â†’ {files, deps, changelog}
â”œâ”€â”€ scope_resource_pack â†’ {files, properties}
â””â”€â”€ notifications
```

Scopes do not import preflight. No circular imports.

### 9.2 Module Responsibilities

- **`config_model.py`** â€” load `config.d/.env` and `config.d/deploy_pack.toml` via `ConfigCore`; compose sources in priority order; read compose; merge; resolve partition; perform no `os.environ` access. Parse `stop_grace_period` duration strings.
- **`overrides.py`** â€” load/apply overrides, line-splice write-back.
- **`properties.py`** â€” surgical `server.properties` edits in binary mode.
- **`deps.py`** â€” Prism index, closure, unmarked detection.
- **`files.py`** â€” direct copy, clean, protect, atomic publication.
- **`docker_runtime.py`** â€” SDK wrapper, health poll, RCON.
- **`hooks.py`** â€” pre/post lifecycle, recovery.
- **`preflight.py`** â€” validation aggregation.
- **`scope_server.py`** â€” server scope, restart adapter, targeting.
- **`scope_client.py`** â€” client ZIP, changelog, `@www/*`.
- **`scope_resource_pack.py`** â€” RP scope.
- **`notifications.py`** â€” templates, bitmask, container_status, allowed_mentions.
- **`prompt_ui.py`** â€” Textual TUI.
- **`deploy_pack.py`** â€” entrypoint shim.

---

## 10. Build Order

| # | Module | Depends |
|---|---|---|
| 1 | `config_model.py` | ConfigCore |
| 2 | `overrides.py` | â€” |
| 3 | `properties.py` | â€” |
| 4 | `docker_runtime.py` | Docker SDK |
| 5 | `hooks.py` | `docker_runtime` |
| 6 | `deps.py` (extend) | Prism index |
| 7 | `files.py` (extend) | â€” |
| 8 | `preflight.py` | `config_model`, `docker_runtime` |
| 9 | `scope_server.py` | `files`, `deps` |
| 10 | `scope_client.py` | `files`, `deps`, `changelog` |
| 11 | `scope_resource_pack.py` | `properties`, `files` |
| 12 | `notifications.py` | â€” |
| 13 | `main.py` | all |
| 14 | `deploy_pack.py` | `main` |
| 15 | `prompt_ui.py` | `overrides`, `deps` |

### 10.1 Test Strategy

Coverage: 90% project, not per-file.

Required test classes per module:

**`properties.py`**: existing key replaced; missing key appended (with and without trailing newline, LF and CRLF); empty file append uses `\n`; comments preserved; duplicate key handling; value containing `=`; non-ASCII bytes; leading whitespace tolerated; empty value written correctly.

**`docker_runtime.py`**: running/stopped/starting/healthy/unhealthy; paused/restarting/removing/dead/missing; restarting bounded wait (global, parallel, poll interval); zero-value disables; negative-value exits 3; health timeout; start failure; exec failure; Option A/B selection; RCON_PORT resolution (mapping and list); multi-port ambiguity; remote rcon_host + zero/multiple matches; stop timeout from compose; `stop_grace_period` parse (valid `"30s"`, `"1m30s"`, `"2m"`; invalid string â†’ exit 3); missing `.State.Health` on running container; missing `.State.Health` post-start; missing `rcon_password` secret; short-form and long-form secret declarations; absolute vs relative secret path; pre-stop race (no-op stop not counted, not lifecycle-touched, marked `exited before stop`); realpath failure â†’ exit 3; Docker daemon unavailable at preflight â†’ exit 3; Docker daemon unavailable at runtime â†’ exit 1.

**`files.py`**: protect; merge; delete; unchanged files not recopied; missing source; copy failure; atomic publication metadata; fresh-destination metadata defaults; `os.chown` failure warns; `os.chmod` failure warns.

**`notifications.py`**: bitmask per scope combo; empty template rejection; missing template skip; malformed placeholder (unknown, invalid-everywhere, cross-template) â†’ exit 3; 2000-char guard; allowed_mentions; container_status scope; failure notification on start failure; failure_stage selection (post_hook vs health_timeout); recovery-path stage preservation; conditional on --notify; `{dry_run_marker}` content; `{timestamp}` format; `{requested_scopes}` format; internal states (`cancelled`, `exited before stop`) not rendered in Discord messages.

**`preflight.py`**: aggregate failures; halt-before-writes; drift check with symlinks; missing container; normalization; partition-scoped vs global; orphan RP check global; leaf key validation; realpath failure; in-game template validation (empty; unknown placeholder in restart_notice_template; placeholder in restart_cancel_notice_template); `[resource_pack.X]` required keys.

**`config_model.py`**: `.env` loads as flat keys; TOML overrides `.env`; missing `.env` is silent; malformed `.env` lines are skipped by `ConfigCore`'s parser; project root resolution; conflict resolution; `www_dir` ambiguity exit 3; output filename safety (must end `.zip`); empty `download_base_url` â†’ exit 3; `--instance` partition; `--full --instance` rejected; `--client --instance` rejected; error precedence.

**`scope_server.py`**: longest-literal-prefix; action ordering; `+pack` sticky max; partition into sets; reload vs restart; targeted excludes mods_dir; targeted warns on mods drift; mods_dir flat; drift by filename + SHA-256; `warned_and_running` computation; `to_stop` computation; stop eligibility three-stage check.

**`scope_resource_pack.py`**: action merge; destination-missing triggers publication; SHA-1 case-insensitive; zero-pack no-op; `@www` grammar validation; independent properties vs publication; `[resource_pack.X]` key validation (filename required, required boolean required, prompt optional).

**`overrides.py`**: load missing file â†’ empty; invalid override value â†’ exit 3; case-sensitive value matching; splice when file absent (whole-file generation); splice when section absent (append at EOF); splice when section present (replace); line-ending preservation; metadata preservation; "Last generated" comment authorship.

**Integration tests:** fake repo in `tmp_path`, mocked Docker SDK.

---

## 11. Appendix

### 11.1 Exit Code Reference

| Code | Class | Examples |
|---|---|---|
| 0 | Success | Deployment completed, help, diagnostic posted |
| 1 | Runtime | Write failure, health timeout, container start failure, RCON unavailable, docker stop failure, reload failure, docker daemon lost mid-deployment |
| 2 | Usage | Unknown flag, `--dry-run` alone, incompatible `--audit-mods` combo, `--full --instance`, `--instance` without scope |
| 3 | Configuration | Malformed config, missing source, orphan RP section, no instances for `--server`, missing compose when required, missing container, mount drift, `--instance` unknown, `www_dir` undeterminable, invalid override value, output filename not `.zip`, empty download_base_url, negative docker timing key, invalid `stop_grace_period` duration, realpath failure, empty/unknown-placeholder in-game template, missing `[resource_pack.X]` required key, docker daemon unavailable at preflight |

## 11.2 Naming

| Context | Form |
|---|---|
| CLI flags | kebab-case |
| TOML keys/sections | snake_case |
| `.env` keys | snake_case |
| Python | snake_case / PascalCase |

### 11.3 Placeholder Reference

**Live:** `{tool_version}` `{timestamp}` `{requested_scopes}` `{instance_count}` `{instance_list}` `{dry_run_marker}` `{player_tags}` `{operator_tags}` `{section_server}` `{section_client}` `{section_resource_pack}`

**Online:** `{tool_version}` `{timestamp}` `{player_tags}` `{container_status}`

**Failure:** `{tool_version}` `{timestamp}` `{operator_tags}` `{failure_stage}` `{error}` `{container_status}`

**Diagnostic:** `{tool_version}` `{timestamp}`

**In-game restart notice** (not a Discord template): `{time}` only. Substituted with `<N> seconds`.

**In-game cancel notice** (not a Discord template): no placeholders.

**Invalid everywhere:** `{debug_deps}` `{url}` `{sha1}` `{sha256sum}` `{pack_filename}` `{changelog_url}` `{summary}` `{deployment_status}` `{header}` `{footer}` `{note}`

Pre-existing-unhealthy note (Â§4.12, Â§8.3) is prefixed to `{error}` placeholder text. Not a separate placeholder.

**Cross-template placeholder rule:** a placeholder that is valid for one template but used in another is malformed â†’ exit 3. See Â§5.11.

### 11.4 Restart Action Quick Reference

| Action | Lifecycle | Pack |
|---|---|---|
| `none` | none | no |
| `none+pack` | none | yes |
| `reload` | RCON reload | no |
| `reload+pack` | RCON reload | yes |
| `restart` | full cycle | no |
| `restart+pack` | full cycle | yes |

Ordering: `none < none+pack < reload < reload+pack < restart < restart+pack`

`+pack` sticky through max.

Default for unlisted paths: `restart`.

### 11.5 Glossary

**Partition / Partition member / Partition-scoped / Global** â€” Â§2.9.
**Effective change for member m** â€” destination differs from source for m.
**Effective action for member m** â€” sticky-max across changed paths.
**none_set / reload_set / restart_set** â€” partition members by effective action.
**warned_and_running** â€” restart_set members running at preflight capture and at pre-notice re-inspection. Membership describes eligibility, not notice delivery.
**to_stop** â€” subset of warned_and_running running at pre-stop re-inspection.
**Actually stopped by this deployment** â€” non-no-op docker stop on a container running at the pre-stop re-inspection.
**Actual restarts** â€” containers in to_stop actually stopped and successfully started. Gates the online notification only.
**Lifecycle-touched container** â€” actually stopped, actually started, actually reloaded, or attempted start or reload. No-op stops are excluded.
**Warned container** â€” a container in warned_and_running that received a restart notice.
**Preflight** â€” validation phase, no writes.
**Deployment scope** â€” `server` / `client` / `resource-pack`.
**Notification section** â€” Markdown block gated by bitmask.
**Cancellation notice** â€” in-game RCON message when a promised restart is called off.
**Atomic publication** â€” write temp in same directory, fsync, preserve metadata, os.replace.
**RCON** â€” Option A (docker exec), Option B (direct TCP).
**Project root** â€” parent of `config.d/`.

---

**End of contract.**

Authoritative for `deploy_pack` v2.
