# Concurrent-cohort reconciliation hardening

**Status:** Active (2026-07-20). Fix A and the partial C mitigations are implemented and live-validated; steps 1-2 of the target shape are specified as PRD #1136 (rev 2, reviewed); Fix B and retention remain future work.
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

## Root cause C — TRUE cause: per-bot IBKR position queries serialize (2026-07-20, deep-dive)

The probe-timeout (Fix C, 357609b9d) treated a symptom. Profiling the ~10-12s
`/catalog` (and the equivalent roll-call) read path, everything cheap was ruled out:
daemon `/instances` **0.02s** (direct), container→host `host.containers.internal` hop
**0.01s**, mounted-volume small-file I/O **0.01s**, journal parses **0.04-0.19s** (incl.
the 58MB session_roster), a daemon restart changed nothing (not in-memory bloat), and
`_compute_account_fleet_contamination` is only **0.2s**. The cost is **per-bot IBKR
work**: a `/catalog` request emits **~356 `ib_async` lines incl. 64 `position:` lines**
for 6 bots — i.e. the per-bot status/exposure/account-truth resolution triggers repeated
IBKR **`reqPositions`** round-trips that **serialize on the single broker connection**.
So the read path is **O(N bots × broker-round-trip)**; at 6 bots it is ~12s, which makes
the roll-call too slow to offer a member at its staggered slot → the member is silently
dropped → the cohort caps below 5 (observed 4, then 2). The rows are `asyncio.gather`-ed
but serialize on the one IBKR connection, so concurrency does not help.

**Real Fix C:** serve the read paths from the **cached** Account Truth snapshot (the 15s
`AccountTruthRefreshLoop` already maintains one) instead of triggering a fresh per-bot
`reqPositions`; compute account-level state (truth, exposure, fleet contamination) **once
per request** and share it across all bot rows. Expected: `/catalog` and roll-call drop
from ~12s to sub-second, the roll-call reliably offers every member, and 5-concurrent
becomes reachable. This supersedes the probe-timeout band-aid as the primary C fix.

## Adjacent issues found in the bot-control read/storage layer (2026-07-20)

1. **`policy_blocks_starts=True` right now** — residual fleet contamination from the
   day's runs blocks new starts; a cohort launch would be refused until reconciled.
2. **Unbounded, never-compacted append-only journals**: `session_roster_history.jsonl`
   **58MB**, `clerk_journal.jsonl` **8.6MB**, `account_events.jsonl` **3.8MB**. They grow
   forever; every read re-parses the whole file. Needs rotation/compaction/indexing.
3. **Per-run `host_daemon.log` balloons to 24MB** — the daemon dumps every
   execDetails/commissionReport/position line per run. Verbose; disk + I/O cost.
4. **N+1 fleet contamination**: `_resolve_fleet_blocks_starts_for_status` recomputes
   `_compute_account_fleet_contamination` per bot (no memoization); ~0.2s × N.

## Architectural blindspots — higher-angle review (2026-07-20, post-mortem of all four runs)

The user's hypothesis: *"every bot should have its own identity attached to the
transaction, and that could resolve the collision logic."* Verdict: **the identity
already exists — the blindspot is that it was never made authoritative.** Every order
carries `learn-ai/{strategy_instance_id}/v1:{intent_id}` in `order_ref`
(`order_identity.build_order_ref`), and IBKR echoes it back on fills. Yet three
different components each *re-decide ownership locally* using weaker, connection-local
keys (`client_order_id`, async `perm_id`). Identity was treated as an audit
annotation, not as the ownership key. Fix A promoted it to a third signal in one
check; the deeper fix is to make it *the* key, resolved once by one authority.

The individual failures (A, B, C) are symptoms of **one meta-blindspot: the system was
designed single-bot-first, and N-bot concurrency was bolted on.** Concretely:

1. **The account authority exists but is bypassed.** The Clerk is already the
   single-writer account authority: it journals every execution with principled
   attribution ("a broker callback without a durable Clerk intent remains an account
   fact, never a guessed namespace" — `account_clerk_journal_models.py`). Yet (a) each
   bot's `outside_mutation` guard re-polices the whole account from its own local
   memory; (b) the fleet read paths query the broker per bot instead of reading the
   clerk's projection; (c) fills surface via the clerk's connection but bots never
   consume the clerk's classification. The right pieces exist; the wiring routes
   around them. **A, B, and C all collapse if the Clerk is promoted to sole broker
   reader + sole execution classifier + projection server.** (This is exactly the
   direction already envisioned by the Account Custodian PRD #1114 and the PRD #718
   rebuildable projection — today's failures are the empirical proof they're needed.)

2. **IBKR multi-client semantics were misread.** IBKR is account-scoped, not
   connection-scoped: executions broadcast to all subscribed clients regardless of
   placer; `client_order_id` does not cross connections; `perm_id` arrives async. The
   single-bot design implicitly assumed "my connection sees my orders (first)". With
   clerk + N bots on one account that assumption is structurally false (msft's own
   fill surfaced under the clerk's clientId 50). Any ownership logic keyed on
   connection-local facts is unsound on a shared account.

3. **The broker can attribute executions but NOT positions.** `IbkrPosition` is an
   account-level aggregate — there is no namespace below the account at the broker.
   Therefore position-truth *must* come from our journal projection; per-bot
   `reqPositions` (root cause C) was asking the broker a question it structurally
   cannot answer per-bot, N times, on one serialized connection.

4. **Event-sourced writes, no materialized reads (half-CQRS).** Every write is an
   append-only journal/receipt, but every read rebuilds the world per request:
   catalog re-scans run dirs, recomputes contamination per bot, re-queries broker
   positions per bot. The 15s `AccountTruthRefreshLoop` maintains a snapshot the read
   paths don't consume. Read cost grows with fleet size AND with history (journals
   never compact) — concurrency plus uptime *both* degrade the control plane.

5. **Durable pins exist, but eligibility is re-derived live at the worst moment.**
   The cohort receipt durably pins members/run_ids/settings after a full preflight —
   then each staggered slot discards that trust and re-derives *the entire fleet's*
   eligibility synchronously, under peak load, with silent failure. The trust model is
   inverted: a flaky live recomputation outranks the durable pin. The slot check
   should be narrow (pinned run still deployed? account not frozen? no crash flag?) —
   or eligibility should be a durable event-maintained state, not a per-slot rebuild.

6. **Per-bot fail-closed guards make N bots N single points of failure for each
   other.** `outside_mutation` fatal-halts THIS bot on ANY unattributed account
   activity. Correct for one bot on a dedicated account; on a shared account every
   timing race anywhere kills somebody. The fail-closed unit should be the account
   (freeze submits account-wide on genuinely unattributable activity) with
   attribution resolved by the account authority — not per-bot poison on shared
   evidence.

7. **Negative decisions are receiptless.** In a system philosophically committed to
   receipts, a roll-call *drop* leaves no artifact — it took hours of live debugging
   to find `status_is_roll_call_eligible` silently skipping members. Every "not
   offered" needs a reason-coded receipt (honest-empty applied to eligibility).

8. **Unbounded growth treated as free.** Journals, per-run daemon logs, and run dirs
   accumulate forever and sit in hot read paths. No retention/compaction policy
   exists anywhere.

**Target shape (aligns with #1114 / #718, sequenced smallest-first):**
- *Step 1 (= "real Fix C" above):* reads consume the cached Account Truth snapshot /
  a once-per-request account-level computation. No broker calls in request paths.
- *Step 2:* slot dispatch trusts the durable pin — narrow re-check only, with a
  reason-coded receipt for any refusal.
- *Step 3:* Clerk becomes sole execution classifier; bots consume their namespace
  slice + an account-level "unattributable activity" alarm instead of local
  `outside_mutation` guesswork (subsumes Fix B).
- *Step 4:* retention/compaction for journals and logs.
- The one-account-per-bot alternative (below) remains the escape hatch if
  shared-account complexity is deemed not worth it — but note it does NOT fix
  blindspots 4, 5, 7, 8, which are about the control plane, not the account.

### Start-gate classification for receipt-authorized cohort slots

The interactive `POST /runs/{run_id}/start` path remains the complete
fail-closed admission chain. A V2 staggered cohort has already passed that
chain before its authorization receipt is written, so a scheduled member uses
the typed receipt-authorized policy below. Each current gate belongs to exactly
one class; this prevents a later slot from silently rebuilding fleet
eligibility under load.

| Existing start gate | Receipt-authorized class | Why |
|---|---|---|
| Run exists / exact `strategy_instance_id` and account | Admission-proved | The receipt is selected only when its account, member pin, and scheduled `run_id` exactly match the resolved run. Unknown runs remain the daemon's 404 authority. |
| Persisted start request | Admission-proved | The request excluding transport-only offer/cohort fields must equal the V2 schedule's immutable start request. |
| Roll-call offer and Ready eligibility | Admission-proved | The original offer was pinned when the receipt was authorized. A slot must not re-derive fleet eligibility. The coordinator's temporary retry remains outside this policy until the dedicated dispatch slice removes it. |
| Cohort session window / effective stop | Admission-proved | Authorization proves the whole receipt schedule and validation window fit before effective stop; the slot does not recompute it. |
| Account observation lease, connected-account identity, and fleet contamination | Intentionally removed | The authorization validates target paper posture and pins membership; re-running these checks can refresh/query broker state and recreates the contention this change removes. A stale or unavailable observation stays honest for reads; it is not repaired synchronously by a slot. |
| Account freeze | Dynamic | A freeze can be written after authorization and must block every later slot. |
| Soft deletion and lifecycle retirement | Dynamic | A bot can be removed or retired after authorization; a durable pin never overrides a later retirement decision. |
| Crash-recovery block | Dynamic | A crash or recovery-required binding can appear after authorization. |
| `poisoned.flag` | Dynamic | A later fatal run state must continue to require redeploy. |
| Host daemon reachability and process state | Dynamic | The daemon can go offline, start, or stop between slots; only an idle/startable process reaches the POST. |
| Daemon run-dir/request validation, desired-state/idempotency lock, client-id allocation, account binding/freeze write, Clerk readiness, spawn and runtime pre-flight | Daemon-delegated | These are the host's atomic start boundary and remain idempotently enforced even after data-plane admission. |

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
