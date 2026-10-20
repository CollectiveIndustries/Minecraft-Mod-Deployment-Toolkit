# docs/Spec_Amendments_v1.4.0_slice_14_5.md

**Status:** Ready to apply to `Upgrade_System_specifications.md` v1.4.0
**Scope:** Slice 14.5 — API and byte budget enforcement

---

## Summary

The v1.4.0 spec declared a phantom `[state] ledger_path` config and a "SQLite event ledger" for cap persistence. Neither existed in code. Slice 14.5 implements cap enforcement using an append-only JSONL usage log, matching the pattern already proven by `release-index.jsonl`, `operation.jsonl`, and the shipping transaction's backup journal.

The phantom ledger is retired. The `[iteration]` `max_api_requests` and `max_downloaded_bytes` fields (declared in §17.1 but never read) move to a new `[budget]` section that actually enforces them.

---

## §4.1 Storage tree

Replace:

```
    ├── ledger.sqlite                   # deployment ledger + API/byte budgets
```

With:

```
    ├── usage.jsonl                    # API + byte budget log (append-only)
```

The default path is `<project_root>/var/usage.jsonl` (project-relative). Operators may override via `[budget].usage_log_path`.

---

## §8.2.1 Caps

Replace the paragraph beginning "Budgets are persisted in a SQLite event ledger keyed by run ID and resolution attempt." with:

> Budgets are persisted in an append-only JSONL usage log at `[budget].usage_log_path` (default `<project_root>/var/usage.jsonl`). Each line records one API call or one download with a timestamp and, for downloads, a byte count. The log is read on every cap check; records older than `[budget].window_hours` (default 24) are ignored. A malformed line is logged and skipped; an unreadable log file aborts the run with exit 3 (fails closed per the original spec).
>
> The resolver's per-run `provider_requests_made` counter and the usage log record the same facts. The log persists across runs and across restarts; the report counter does not.

---

## §17.1 Configuration

Remove `ledger_path` from `[state]`.

Remove `max_api_requests` and `max_downloaded_bytes` from `[iteration]` and add a new `[budget]` block:

```toml
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
```

---

## §20 Acceptance tests

Add T36:

| ID | Test | Pass condition |
|---|---|---|
| T36 | API cap enforcement | Configure `max_api_requests = 5`, resolve a manifest requiring more than 5 API calls. Resolution aborts with exit 6 and the cap diagnostic. The `usage.jsonl` log records exactly 5 `api` lines for the run. |

---

## §22 Known limits

Add:

- **Cap enforcement is best-effort under concurrency.** The usage log assumes a single writer. Concurrent `update` invocations may briefly exceed the configured cap by up to one call per concurrent writer. Multi-writer coordination is deferred to v2.0.0.
- **Cap enforcement is per-window.** A cap of 5000 API requests per 24 hours resets at the window boundary; there is no cross-window accumulation.
- **Byte cap can be exceeded by one download.** The byte-cap check runs before each download. If a single download pushes the total past the cap, the *next* download is refused. Operators mitigate by setting `max_bytes` on individual downloads below their cap surplus.

---

## §23.1 Authority table

Replace the row:

| `[state].ledger_path` | `MinecraftDaemonConfig` (phantom) |

With:

| Usage log | `[budget].usage_log_path` (append-only JSONL) |
| API cap | `[budget].max_api_requests` |
| Byte cap | `[budget].max_downloaded_bytes` |

---

**End of Slice 14.5 amendments.**