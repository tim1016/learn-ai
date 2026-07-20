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

## Root cause C — roll-call offer degradation under concurrent load

As concurrency rises, the next member's roll-call offer goes missing for 20-30s at its
slot (`status_is_roll_call_eligible` requires `phase==OFF_DUTY ∧ start.enabled ∧
not SICK_BAY ∧ …`; something transiently flips). The retry fix rides out short misses
but spy's (+20, 3 bots running) outlasted the ~24s window. **Mechanism not yet
pinned** — candidate causes: the daemon serializing `/instances` status under load;
a start-capability gate flickering; account-truth refresh contention. Needs a focused
diagnostic (instrument which gate flips at a slot under load) before choosing between:
widen/scale the retry window, make the daemon status path non-blocking, or stabilize
the flapping gate. Do not just widen the retry blindly — that masks the cause.

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
