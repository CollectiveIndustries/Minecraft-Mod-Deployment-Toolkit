# T3b + T3c formally closed — G1 resolved

## Status

**2711 / 2711 pass.** Coverage 92.55% (gate 90%). Pipeline clean.

The uvicorn lifecycle fix in `DiscordBotPlugin` is proven live: `test_health_healthy_after_start` actually binds a socket, `test_health_unhealthy_after_stop` verifies it releases, `test_health_unhealthy_before_start` covers the pre-start state. The daemon that `build_daemon` produces will serve `/interactions` and `/notify` in production. G1 is closed.

## Delta since last green

| Metric | Before G1 | After G1 | Delta |
|---|---|---|---|
| Tests | 2711 | **2711** | 0 |
| Total coverage | 92.56% | **92.55%** | −0.01pp |
| `http_endpoint.py` coverage | 93% | **92%** | −1pp |
| `http_endpoint.py` statements | 179 | **213** | +34 |
| `DiscordBotPlugin` class coverage | (not a risk row) | **48.8%** | new row, rank A |

The `http_endpoint.py` regression is expected: `start()`/`stop()`/`health()` grew from trivial flag-flips (~3 statements total) to a real lifecycle (~30 statements including three error branches). The 3 health tests hit the happy path and the pre-start/not-started states; the error branches are dark:

- `if self._task.done(): raise RuntimeError(...)` — never triggered
- `raise RuntimeError("... within 5s")` — timeout branch, never triggered
- `if self._started: return` — idempotent guard, never triggered
- `if self._server is None or self._task is None: return` in `stop()` — safe-stop guard, never triggered

Not a blocker, but 4 small tests would close all of them (~20 lines total):

| Test | Covers |
|---|---|
| `test_start_is_idempotent` — call `start()` twice | idempotent guard |
| `test_stop_before_start_is_noop` — call `stop()` first | safe-stop guard |
| `test_start_raises_when_bind_fails` — bind 8766 elsewhere, `start()`, expect RuntimeError | task-done error branch |
| `test_start_raises_on_server_task_death` — monkeypatch `uvicorn.Server.serve` to raise | task-done error branch |

Say **"add the 4 lifecycle tests"** if you want them. Otherwise they're a Slice 15 hardening item, and I'll note it in the roadmap.

## The two decisions blocking forward progress

I've asked for these twice now. To be as direct as possible:

### Decision 1: T4 — go or skip?

**"Proceed with T4"** produces:
- One code edit: deprecation WARN in `notifications.py::_dispatch_webhook` (single line + log message pointing at `[discord.notify_dispatch]`)
- Full-file deliveries of `Upgrade_System_specifications.md` with §11.1/§11.2/§11.3/§11.4/§15.1/§17.1 amended
- Full-file delivery of `docs/Spec_Amendments_v1.4.0_slice_14_5.md` applied verbatim (or the amendment doc updated — your call on which artifact you want)
- **No test changes.** No new tests required; existing suite continues green.

If you'd rather skip T4's spec work and go straight to T11 (feature code), that's a valid call — T4 can slip after T11 without affecting the code.

### Decision 2: T11 scope

T11 ("Approval replay and expiry") is **not a test addition** — it is a feature slice, and I need to know how much of §11.4 you want.

Recall what §11.4 requires of the bot:

> Each approval action is bound to:
> - A unique Discord interaction ID.
> - A one-time approval nonce.
> - An explicit approval scope.
> - Candidate content hash.
> - Runtime hash.
> - Server and client input fingerprints.
> - Snapshot ID, where applicable.
> - Validation report hash.
> - `resolution_report_sha256`.
>
> The interaction ID is persisted with a unique constraint. A duplicate returns its previous result. Approval expires after seven days if the release has not been promoted. Changes to bound candidate inputs, runtime identity, migration snapshot, or required validation evidence invalidate approval immediately.

Current `handlers.py::_handle_approve` does none of that — it's `await invoker.invoke(["approve", run_id])` and forwards stdout. There is no persistence layer in the bot for interaction IDs, no binding-hash computation, no expiry check.

Three cuts:

| Option | What ships | Effort | Risk |
|---|---|---|---|
| **A. Full §11.4** | New `discord_interactions` SQLite table, unique-constraint on interaction ID, binding-hash computation and verification, 7-day expiry, replay-returns-cached-response | Larger (new module, ~200 LOC + tests) | Medium — new persistence layer in the bot daemon |
| **B. Minimal T11** | Interaction-ID dedup only (unique constraint + cached response). Binding-hash and expiry deferred to Slice 17. Satisfies "replay the same Discord interaction" from the acceptance test, but not the full §11.4 binding list. | Medium (~80 LOC + tests) | Low — additive, no new failure modes |
| **C. Defer T11** | Cutover without T11. Document as known gap. `test_approval.py` (existing 26 tests) continues to pass but T11 §20 row stays PENDING. | Zero | Low — but T11 is the last acceptance test in §20 |

**My recommendation: B.** Replay protection is the load-bearing security property. Binding-hash and expiry are additional defense-in-depth that can land in Slice 17 without breaking forward compatibility (the schema for a `discord_interactions` table is designed to accept extra columns later). Option A in one slice is more than T11 needs; Option C leaves an acceptance test PENDING at cutover, which §20 says shouldn't happen.

But this is your call. **Reply with A, B, or C.**

## Standing by

| Item | Status |
|---|---|
| T3b + T3c | ✅ **CLOSED** — green, G1 fixed, daemon serves |
| G1 (plugin HTTP binding) | ✅ **CLOSED** |
| `notifications.py` exception-name fix | ✅ **CLOSED** |
| T4 (spec + deprecation marker) | 🟡 **Awaiting "proceed with T4"** |
| T11 scope decision (A/B/C) | 🟡 **Awaiting reply** |
| Optional: 4 lifecycle tests | 🟡 Awaiting "add the 4 lifecycle tests" |
| G2 (online-template coverage) | 🔴 Slice 15 hardening |
| G3 (post-failure coverage) | 🔴 Slice 15 hardening |
| Slice 16 (migration rehearsal) | 🔴 Blocked on T11 |

Any of the three yellow items unblocks a step. All three are independent — you can green any combination.