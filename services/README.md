
# services/

Systemd units and environment templates for the Minecralt deploy daemon.

## Files

| File | Purpose |
|---|---|
| `minecraft-deploy-daemon.service` | systemd unit |
| `minecraft-deploy-daemon.env.example` | environment file template |

## Install

```bash
# 1. Create the service account (once)
sudo useradd --system --home /var/lib/minecraft-deploy \
  --shell /usr/sbin/nologin minecraft-deploy
sudo mkdir -p /var/lib/minecraft-deploy
sudo chown minecraft-deploy:minecraft-deploy /var/lib/minecraft-deploy

# 2. Secrets
sudo mkdir -p /etc/minecraft-deploy
sudo cp services/minecraft-deploy-daemon.env.example \
        /etc/minecraft-deploy/daemon.env
sudo chmod 0600 /etc/minecraft-deploy/daemon.env
sudo chown minecraft-deploy:minecraft-deploy /etc/minecraft-deploy/daemon.env
sudoeditor /etc/minecraft-deploy/daemon.env   # set MINECRAFT_DAEMON_TOKEN

# 3. Unit
sudo cp services/minecraft-deploy-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now minecraft-deploy-daemon

# 4. Verify
systemctl status minecraft-deploy-daemon
journalctl -u minecraft-deploy-daemon -f
curl -sf -H "Authorization: Bearer <token>" http://127.0.0.1:8765/status | jq
```

## Adjust before deploying

- `WorkingDirectory` in the unit → path to this repo on the target host
- `ExecStart=` → absolute path to `pdm` on the target host (`which pdm`)
- `ReadOnlyPaths=` / `ReadWritePaths=` → paths from `config.d/deploy_pack.toml`
  (`state_dir`, `audit_path`, `idempotency_path`, `lock_path`, `slip_dir`,
  `release_store`, `staging_root`)
- `After=` / `Wants=` → add dependencies (e.g. `docker.service` if the
  daemon will eventually invoke `deploy-pack`)
- `User=` / `Group=` → the account you created

## What the unit inherits from DaemonCore

- **Startup:** systemd invokes `pdm run python -m minecraft.deploy_pack.daemon`.
  DaemonCore acquires the single-instance lock, runs startup hooks, starts
  the HTTP plugin, then waits for SIGTERM.
- **Shutdown:** `systemctl stop` sends SIGTERM. DaemonCore's signal handler
  sets an internal event; `Daemon.run()` stops the HTTP plugin, drains
  uvicorn, releases the lock, and exits 0. systemd does not restart on a
  clean exit.
- **Crash:** a plugin that crashes the process (segfault, uncaught
  exception in a thread) → non-zero exit → systemd restarts after
  `RestartSec=5`.
- **Logs:** journald captures stdout/stderr under the
  `minecraft-deploy-daemon` identifier.

## Optional: log relay to file

If you also want the daemon's logs written to a file in addition to
journald, add a secondary LoggingCore sink by editing
`minecraft/deploy_pack/daemon/main.py` — the `.setup_logging({...})`
builder call accepts a `handlers` list with file handlers and rotation.
This is preferred over `StandardOutput=append:` because it uses the
daemon's own structured log events.

## Notes

**`Type=exec` vs `Type=simple`.** `exec` tells systemd to wait for the binary to actually exec before considering the unit started. For `pdm run`, which may fork before running the interpreter, this is the correct type. `simple` would consider the unit "started" as soon as pdm forks, which is too early.

**`KillMode=mixed`.** Sends `SIGTERM` to the main process group, and `SIGKILL` to any remaining children after `TimeoutStopSec`. Combined with `KillSignal=SIGTERM` this means `pdm` and the Python daemon both receive `SIGTERM` (the daemon handles it gracefully; pdm forwards or exits), and after 30 seconds anything still alive is force-killed. If you observe pdm swallowing signals on your distro, switch to `KillMode=control-group`.

**`Restart=on-failure`.** Clean exit (exit 0, no signal, or SIGTERM) does not restart. A crash (non-zero exit or unhandled signal) does. `StartLimitBurst=5` in `StartLimitIntervalSec=60` prevents infinite restart loops on a config error.

**`ProtectHome=false`.** The default `ProtectHome=true` makes `/home/*` inaccessible. Since the repo is under `/home/admiral`, this would break `WorkingDirectory`. If you relocate the repo to `/opt/minecraft` or `/srv/minecraft`, switch to `ProtectHome=true` and move `/home/admiral/src/minecraft` from `ReadOnlyPaths=` to nothing (the project dir becomes reachable through `/opt/...`).

**`EnvironmentFile=-/etc/...`.** The leading `-` makes the file optional — the daemon starts even without it, but `BearerAuth(token_env="MINECRAFT_DAEMON_TOKEN")` will raise `ValueError` on plugin construction, causing startup failure and a clean `StartupError`. Removing the `-` makes the dependency explicit and systemd refuses to start the unit if the env file is missing.

**`ReadWritePaths=`.** Strictly necessary only with `ProtectSystem=strict`. Without an entry, writes to `/var/lib/minecraft-deploy` and `/srv/minecraft/staging` would fail with `EROFS`. Slips and releases under `/srv/minecraft/releases` and `sync/packing_slips/` are read-only, which is correct — the daemon never writes them (that's `minecraft-upgrade promote`'s job on the dev host).

**Dev-mode alternative.** For local testing, skip systemd:

```bash
cd ~/src/minecraft
pdm run python -m minecraft.deploy_pack.daemon --config-dir config.d
```

`Ctrl-C` sends `SIGINT`, which `graceful_signals` handles identically to `SIGTERM`.