# ADR 0046 — `HARD_DOWN` is a circuit-breaker OPEN state, not a terminal one

**Status:** Accepted 2026-08-25
**Provenance:** Authored with the IBKR reconnect-breaker change (#1777 WP4, PR #1782). Supersedes [ADR 0018](0018-broker-session-mirror-client-observatory.md) Decision 5's *terminal* `HARD_DOWN` clause — and only that clause; every other part of ADR 0018, including the state set, the 1100≠socket-dead split, nightly-reset awareness, and operator-only Resume, stands unchanged. Measurement recorded in `docs/audits/bot-fleet-stress-2026-08-25.md` § S1.
**Decision drivers:** 37.9 % measured data-plane downtime (1 021 of 2 691 observed minutes) across 2026-08-23 → 2026-08-25, of which the overwhelming majority was caused by the terminal latch itself rather than by gateway unavailability.
**Related:** ADR 0018 (recovery state machine — this ADR supersedes its terminal-`HARD_DOWN` clause), ADR 0011 (broker safety verdict — the *identity* verdict and its halt path are untouched), ADR 0021 and ADR 0028 (both hard-block or degrade on broker `HARD_DOWN`; that treatment is unchanged, because this ADR does not alter what `HARD_DOWN` *means* to a consumer — only how the monitor behaves while in it), ADR 0039 (ADR status is decision standing).
**Vocabulary:** None owed. The recovery-state vocabulary is unchanged — `HARD_DOWN` keeps its name, its severity, and its meaning to every consumer ("the data-plane broker session is down"). Only the monitor's own behaviour while in that state changes, which is implementation, not vocabulary.

## Context

ADR 0018 Decision 5 specified the recovery ladder as `SOCKET_DOWN → RECONNECTING → HARD_DOWN`, with `HARD_DOWN` defined as "attempts exhausted — **terminal, surfaced loudly**, not infinite silent retry". That clause was a deliberate reaction to the mechanism it replaced, which "retries forever with no terminal state". At the time, unbounded retry was the observed defect and a terminal state was the fix.

Fleet measurement inverted the finding. The retained broker connection log (`PythonDataService/artifacts/live_runs/_broker/connection_events.jsonl`, 5 000 events, 2026-08-23 22:17 → 2026-08-25 19:09 ET) shows:

- **37.9 % downtime** — 1 021 of 2 691 observed minutes with no successful 30 s probe, in two blocks: 08-24 00:44→08:49 ET (8.1 h) and 08-25 00:44→09:41 ET (8.9 h).
- **3 `HARD_DOWN` latches in 1.9 days.** Each followed the same script: attempts open at 00:45:0x, exhaust the ten-attempt ladder's nine inter-attempt waits (`1+2+4+8+16+32+60×3 = 243 s`, excluding `connect()` duration — the loop latches on the tenth failure without sleeping again), and latch ~5 min 46 s into the outage.
- **The latch, not the outage, set the downtime.** After latching, `_tick`'s hard-down branch only *observed* the client. The monitor emitted nothing for eight hours; recovery arrived from an unrelated data-farm event, not from the reconnect path. The third latch is the clean proof: a **20-minute** gateway outage (09:21:19 → 09:41:22) that a more patient ladder would have absorbed with no latch at all.
- **The root cause was never permanent.** IB Gateway auto-logged-off nightly (`Daily auto-restart is not enabled.`) and stayed down until a human relaunched it in the morning. Every latch was a *recoverable* outage that the terminal state converted into an operator-blocking one.

The clause therefore failed on its own terms. "Surfaced loudly" was not achieved — after the latch there were zero further events. And the failure classes a terminal state is meant to protect against (persistent sentinel/account-mode mismatch, multi-account, client-ID conflict) are not made worse by patient retry: each is refused inside `IbkrClient.connect()`, which disconnects before raising, so a retry against a genuinely permanent misconfiguration costs one refused socket per probe interval and changes no operator-visible verdict.

## Decision

1. **`HARD_DOWN` is the OPEN state of a circuit breaker, not a dead end.** The fast ladder (`MAX_RECONNECT_ATTEMPTS`, exponential backoff + jitter) still exists and still ends; ending it opens the breaker rather than terminating recovery. `reconnect_exhausted` no longer returns `terminal=True`, and a new `open_probe_failed` signal re-asserts `HARD_DOWN` after each failed probe. Re-asserting `HARD_DOWN` — rather than falling back to `SOCKET_DOWN` — is what keeps the fast ladder from restarting on every 3 s tick.

2. **The open state probes on its own slow cadence, indefinitely.** `OPEN_PROBE_INTERVAL_S` (60 s) is the single knob governing how long a resolved outage stays visible as `HARD_DOWN`. This is retry that is neither infinite-and-silent (the condition ADR 0018 rejected) nor terminal: it is paced, logged with a structured `action`, counted, and recorded as a recovery event.

3. **The open probe shares one locked attempt path with the fast ladder.** Both go through a single helper holding `get_client_lifecycle_lock`, which re-checks `desired_connected` and observable state *after* acquisition. A probe that skipped the lock would race the operator's `/connect`, `/disconnect` and `/reconnect` routes and could reconnect a client the operator was deliberately taking down.

4. **The breaker's open state never presents a half-recovered socket as healthy.** `connect()` can succeed while a recovery callback (`resubscribe_all`, the broker-activity sweep) raises, leaving a live socket whose subscriptions, orders, executions and positions were never re-requested. Any failed attempt that leaves such a socket connected drops it before returning. Without this, the next tick would observe `is_connected() and not connection_lost` and collapse to `HEALTHY`, reporting that account evidence can refresh when recovery never completed — and silently overwriting the `HARD_DOWN` the probe had just re-asserted.

   The same invariant governs the other way into that branch. When an attempt acquires the lock and finds the socket already restored — in practice the operator's `/connect` or `/reconnect`, which land between the tick that observed the drop and the lock acquisition — the monitor runs the recovery callbacks before advancing to `HEALTHY`. Those routes call `client.connect()` and warm account evidence, but never invoke the monitor's callback chain, and `connect()` clears `subscriptions_stale`, so nothing downstream would recover the subscriptions later. The short-circuit that avoids a duplicate `connect()` is preserved; only the premature health claim is removed. Stated generally: **the monitor never advances to `HEALTHY` on a connection whose recovery callbacks have not succeeded, regardless of who opened it.**

5. **No new recovery states.** `OPEN`/`HALF_OPEN` are deliberately *not* added to `RecoveryState`. The breaker semantics ride on the existing `HARD_DOWN` value so that the existing consumers, the OpenAPI enum, and the generated Frontend types keep working unchanged. The cost is that "breaker open" is not independently addressable in the wire vocabulary; that is accepted, because no consumer has asked to distinguish it and adding it would be a breaking contract change for a purely internal distinction.

6. **Operator copy stops promising finality.** Surfaces that read `hard_down` said "recovery exhausted" and directed the operator to click Reconnect. They now state that retry is automatic and ongoing. The operator guidance that remains is the part the system genuinely knows: confirm IB Gateway is running, logged in, and has API access enabled. (The nightly-logoff remedy itself is a Gateway setting — Configure → Settings → Lock and Exit → **Auto restart** — not a code change.)

## Consequences

**Positive:**

- Downtime is bounded by the gateway's real availability rather than by whatever unrelated event happens to poke the monitor. On the measured window, the two multi-hour blocks would have ended at the gateway's morning relaunch instead of ~1 h later, and the 20-minute third outage would not have latched at all.
- The operator is no longer required to click Reconnect to recover from a transient outage, which was the only remedy the previous copy offered and which is unavailable overnight.
- `HARD_DOWN` regains the "surfaced loudly" property ADR 0018 wanted: the open state emits a paced, counted event stream instead of silence.

**Negative / accepted:**

- A genuinely permanent misconfiguration now produces one refused connection attempt per 60 s for as long as `desired_connected` is true, instead of stopping after ten. This is bounded, logged, and cheap; each refusal disconnects before raising, so no socket accumulates. It is accepted as the price of not converting recoverable outages into operator-blocking ones.
- `HARD_DOWN` no longer implies "we have stopped trying", so any future consumer wanting that meaning must ask for it explicitly rather than inferring it from the state.
- Because no new state was introduced, "breaker open" and "breaker open and currently probing" are indistinguishable to consumers.
