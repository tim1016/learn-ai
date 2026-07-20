# Concurrent-cohort reconciliation hardening

**Status:** Draft design (2026-07-20). Not yet grilled/implemented.
**Owner:** (pending)
**Motivation:** Two live paper attempts to certify a 5-bot concurrent cohort on
DUM284968 (`paper_five_bot_stagger_v2`, 5-min stagger, 45-min overlap) both failed
to reach 5 concurrent. Attempt 1 peaked at 2; attempt 2 peaked at 3. The failures
are **reconciliation/orchestration fragility under concurrent load on a single
shared IBKR account**, not the cohort launcher (the launcher + receipt + schedule
are correct — verified). This doc scopes the hardening so 5-concurrent is reliably
reachable and the eventual crash-recovery test (Phase 2) has a stable substrate.

## Evidence

| Attempt | Peak concurrent | Failure |
|---|---|---|
| 1 (09:36 CT) | 2 (aapl, msft) | nvda(+10) `COHORT_SLOT_PREFLIGHT_NOT_READY` → qqq/spy cascaded |
| 2 (11:24 CT, retry fix loaded) | 3 (aapl, nvda, qqq) | msft(+5) **fatal-halted** `outside_mutation`; spy(+20) slot miss outlasted retry |

The slot-preflight retry fix (commit `62ffb97d1`) **works and helped** — logs show it
fired 3× at qqq's +15 slot and recovered qqq (would have hard-failed under the old
code). It is a real partial mitigation, not the whole fix.

## Root cause A (PRIMARY) — `outside_mutation` false-positive on a bot's own fill

`app/engine/live/halt.py::check_outside_mutation` fatal-halts a run when it sees an
account execution it can't prove it owns. Ownership is decided by **only two
signals** (`live_engine.py::_check_halt_outside_mutation`, halt.py:246-256):

1. `client_order_id ∈ owned_client_order_ids` (the bot's `live-{oid}` set), or
2. `perm_id ∈ owned_perm_ids`.

msft's `poisoned.flag` (attempt 2): `client_order_id: null`, `perm_id: 1324475496`,
`client_id: 50`, `trigger: outside_mutation`. But the execution's **`order_ref` was
`learn-ai/cohort5-msft/v1:...` — msft's OWN namespace.** So msft fatal-halted on its
own fill because:

- IBKR reported the execution with a **null `client_order_id`** (not echoed) and via
  the **clerk's client connection (clientId 50)**, not msft's own clientId 52 (the
  check is clientId-agnostic by design — that part is correct); and
- the fill's **`perm_id` was not yet in `owned_perm_ids`** at check time — a
  **perm_id-registration race** (perm_id is assigned asynchronously by IBKR after
  placement; see the existing `run.py:492` "Wait briefly for IBKR to assign it"
  note). Under concurrent load this race widens.

**The check never consults `order_ref`/namespace** — the strongest, immediately-present
ownership signal on a fill. Had it done so, msft would have recognized its own fill.

**Fix A:** plumb the run's own namespace (`learn-ai/{strategy_instance_id}/v1`) and the
execution's `order_ref` into `check_outside_mutation`, and treat a fill whose
`order_ref` is in the run's own namespace as owned — a third ownership signal
alongside client_order_id and perm_id. This closes the race for a bot's OWN fills
without weakening the guard against genuinely foreign fills. Safety-preserving: it
only recognizes fills stamped with this run's namespace.

## Root cause B — runtime `outside_mutation` is not sibling-aware on a shared account

The runtime check only knows THIS bot's `owned_*` sets. A *sibling* bot's fill
(different namespace, same account) is foreign to it and, absent other scoping, would
also trigger `outside_mutation`. The cold-start reconciliation has a sibling-namespace
allowlist ([[project-live-fleet-reconciliation]]); the **runtime** trigger-A check
does not. This is the shared-account exposure that makes N-concurrent inherently
fragile: every bot polices every other bot's fills.

**Fix B (needs the launcher's account roster):** extend runtime ownership to
attributable **sibling** namespaces on the same account (fills whose `order_ref`
resolves to a known ACTIVE sibling `strategy_instance_id`), mirroring the cold-start
allowlist — recognize (suppress halt) but never adopt. Must NOT suppress a genuinely
unattributable foreign fill (manual TWS click, unknown namespace). This is the
delicate part; it changes a fatal safety guard and needs careful test coverage.

## Root cause C — roll-call drops the idle member on a 2s daemon-probe timeout under load

**PINNED (2026-07-20, attempt 3).** As concurrency rises, the next member is silently
dropped from the roll-call at its slot. Mechanism, confirmed from code + the load
correlation:

1. `run_roll_call` resolves each candidate's status via `_resolve_instance_status_for_fleet_sid`.
2. An **idle** candidate (the not-yet-started member) is omitted from the daemon's bulk
   `fetch_instances` snapshot, so the resolver falls back to a per-bot
   `host_daemon_client.fetch_instance_process` (`GET /instances/{sid}/process`).
3. That probe uses the default `_TIMEOUT = httpx.Timeout(2.0)` — **2 seconds**.
4. Under concurrent load the single-event-loop host daemon (managing N running bots +
   their fill/order streams) can't answer within 2 s → the probe **times out** → the
   candidate's `start_capability` resolves with a failing daemon-state gate →
   `start.enabled=False` → `status_is_roll_call_eligible` returns False → the member is
   **silently skipped** (live_instances.py roll-call loop, `if not …: continue`).

This matches all evidence: the failure is always the last/highest-concurrency slot
(more bots → busier daemon → >2 s), it is silent (a timeout, not a logged error), and
the slot-preflight retry (commit 62ffb97d1) can't help — each retry re-hits the same
2 s timeout against the still-busy daemon.

**Fix C (small, low-risk):** give the roll-call's idle-bot process probe a longer,
dedicated timeout (a startability check, not a health ping — 10 s is fine), so a
momentarily-busy daemon doesn't drop an otherwise-ready member. Change lives in
`host_daemon_client.fetch_instance_process` (a dedicated `_INSTANCE_PROBE_TIMEOUT`),
NOT the frozen `live_instances.py` router. Shipped: commit 357609b9d.

**C is BROADER than the probe timeout (2026-07-20, attempt 4 — Fix C necessary but
not sufficient).** After shipping Fix C, a compressed re-run still dropped nvda at +10
with only 2 bots running, and the monitor's polls to the data-plane timed out
continuously for the whole 20-min stagger. Measured: **a `/catalog` read takes ~10.4 s
even at rest with all bots stopped**, against **20 accumulated run directories** whose
`broker_callbacks.jsonl` reach 2.0 MB. The catalog/roll-call/account-truth read path
rebuilds by scanning all run-dir artifacts on every request, so it is
**O(accumulated artifacts)** and blows even the extended 10 s probe budget → members
are dropped. Part real production concern (read path degrades as runs accumulate),
part test-environment bloat (4+ runs today). **Real Fix C ⇒ make the status read path
fast**: cache/index the fleet roster, bound per-run reads, and/or prune-retire old run
dirs so `_visible_runs_by_instance` + `compose_bot_catalog_row` don't re-scan MBs each
call. Cheap unblock for the *next attempt*: run in a **clean environment** (few run
dirs) — the existing Fix A + Fix C likely reach 5 there. Confirm the read-path latency
hypothesis by timing `/catalog` before vs after pruning run dirs.

## Alternative — one IBKR paper account per bot

Root causes A and B exist ONLY because N bots share one DU account. One account per
bot removes cross-bot/cross-client attribution entirely (each account has exactly one
actor). Cleanest architecturally; cost = provisioning N paper accounts and N clerks,
and it doesn't exercise the shared-account reconciliation we may want in production.
Decision needed: is production single-account-multi-bot a real requirement, or is
one-account-per-strategy acceptable? If the latter, A/B may not be worth hardening.

## Proposed slices (if we harden shared-account)

1. **Fix A** — namespace ownership in `check_outside_mutation` (+ unit tests: own fill
   with null client_order_id / unregistered perm_id but own order_ref → owned;
   genuinely foreign fill → still halts). Smallest, highest-value, low-risk.
2. **Diagnose C** — instrument the slot-time roll-call to log which eligibility gate
   is false for the missing member; pick the fix from data.
3. **Fix B** — sibling-namespace awareness in the runtime trigger-A check, reusing the
   cold-start allowlist source. Highest risk; most test coverage; adversarial review.
4. **Re-run** the 5-bot cohort; iterate.

## Risks

- `outside_mutation` is a genuine contamination guard (manual trades, account
  compromise). Every change must preserve halting on truly unattributable fills.
  Regression tests must cover: own fill (race), attributable sibling fill, manual/TWS
  fill (clientId 0, unknown ref), pre-session replay.
- Runbook `docs/runbooks/cross-client-execution.md` documents the operator-facing side
  of this class; keep it in sync.

## Related

- Launcher + retry fix: commits `aed255536`, `e7ec572b3`, `62ffb97d1`, `1d1f0c594`.
- [[project-live-fleet-reconciliation]] (cold-start sibling allowlist + adoption hole).
- [[project_five_bot_cohort_and_crash_recovery]] (program context + attempt logs).
- `docs/runbooks/cross-client-execution.md`.
