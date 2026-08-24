# Bot launch-ops study — 2026-08-24

Operator goal: **"How can this become a practical, simple, robust system for
launching such bots?"** — with scalability as the next goal. This document is
the data-gathering pass: every lifecycle control and launch path was exercised
against the live stack this afternoon, every failure recorded, and the
mechanics timed. Raw event logs: session scratchpad `lifecycle_study.jsonl` and
`launch_study.jsonl` (summarized in full here; scratchpad is ephemeral).

Companion documents: `paper-ceremony-strategy-fleet-2026-08-24.md` (the morning
fleet run this study extends) and `judgment-calls-2026-08-24.md` (decision log).

## 1. What was exercised

- **Lifecycle circuit** on the five running ceremony bots: stop → verify →
  resume → verify, via the presented-actions API
  (`POST /api/brokers/alpaca/accounts/{acct}/bots/{sid}/actions`), plus a
  `pause` probe and one stop through the legacy runner route
  (`POST /api/brokers/alpaca/bots/{sid}/stop`) as an A/B.
- **Validated-strategy launches**: `deployment_validation` (accepted proof,
  pairing not yet active on the current canary ledger → full plan/confirm
  ceremony, then SPY + QQQ deploys) and `ema_crossover_signal` (accepted,
  pairing already active → direct deploy). Fleet grew 5 → 8 bots, one account.
- **Negative probes**: deploy with a superfluous `evidence_override` on an
  accepted strategy — before pairing (hits the selectability gate) and after
  pairing (hits the exact override boundary).

## 2. Timings (measured, wall-clock)

| Operation | Route | Latency |
|---|---|---|
| Stop (panel action `stop_bot_decisions`) | presented-actions POST | 13.6 s, 18.8 s, 18.6 s, 21.9 s |
| Stop (legacy runner route) | `POST /bots/{sid}/stop` | **0.29 s** |
| Resume (panel action, pre-fix) | presented-actions POST | 409 in 21.8–25.8 s, **21/21 attempts failed** |
| Resume (panel action, post-fix) | presented-actions POST | 0.48 s, 0.53 s, 0.54 s, 3.2 s, 4.1 s |
| Pairing review plan | paper-access/plan | 0.011 s |
| Pairing review confirm | paper-access/confirm | 0.017 s |
| Paper deploy (admission + registry + start) | scoped bots POST | 0.24–0.48 s |
| Negative probe refusal | scoped bots POST | 0.12 s |

Two structural observations fall straight out of the table:

1. **The panel-action path costs ~20 s per command; the underlying operations
   cost ~0.3–0.5 s.** `run_action` recomputes the entire panel projection
   (including Clerk reconciliation sweeps) to re-derive the presented action
   before executing it. The deploy path — which does *more* real work —
   completes 40–90× faster because it validates a closed request instead of
   re-projecting a panel.
2. **Launching a fully validated strategy is already cheap and simple**: three
   API calls (plan, confirm, deploy), ~0.5 s combined. The expensive parts of
   launch are policy ceremony for un-validated strategies (see the morning
   document) and lifecycle control, not deployment itself.

## 3. Failures found (this afternoon)

### F1 — Resume was completely dead: token churn (FIXED, `238821c7`)

Every panel Resume 409'd "This action changed since it was presented" —
0/20 attempts across all five bots, fresh tokens every retry, while
`resume_admission` said `RESUME_ADMITTED`. Root cause: `run_admission.py`
stamps `market-liveness-clock:<source>:<observed_at_ms>` (and
`market-liveness-symbol:...`) evidence refs with a fresh instant on every
evaluation, and `_stable_admission_evidence_refs` did not normalize them, so
the concurrency token changed on every recompute. **This is the second
incident of the same bug class** — the module's own docstring records
val-nvda-0804-05 (2026-08-04), where `alpaca-reconciliation:<ts>` did the
identical thing. Fixed by normalizing both liveness refs to their source
identity, with a regression test that fails pre-fix. Post-fix: 5/5 resumes,
sub-5 s. The UI Resume button was equally dead — this was not a
script-only artifact.

### F2 — `pause` / `continue` are dead vocabulary under SQLite custody

The action vocabulary, guards, and performers for `pause`/`continue` all
exist, but the SQLite panel adapter presents only `resume` (stopped bots) plus
the recovery capability set (running bots). A `pause` POST is refused
("not available for this bot") because `run_action` only executes *presented*
actions. There is **no reachable pause** on the current stack: the platform's
real lifecycle is run/stop/resume. Either the pause/continue path should be
presented (and tested) or the vocabulary should be pruned; a control that
exists in code but can never fire is drift waiting to mislead.

### F3 — Two parallel stop surfaces, 60× apart, different receipts

The legacy runner stop (`/bots/{sid}/stop`, 0.29 s, returns `BotStatusView`)
and the panel action stop (`stop_bot_decisions`, ~20 s, returns a receipt +
confirmation copy) both work and both persist a durable STOPPED intent — but
they are separate code paths with separate ergonomics. One canonical stop
should remain; the other should delegate to it.

### F4 — Post-restart feed-readiness cold start strands Resume

After the data-plane restart (needed to load the fix), the first resume
succeeded but the next four were refused: "The required market-data feed is
not proven ready for this run." The feed proof warmed ~45 s later and all
four then resumed cleanly. Harmless here, but on a supervised fleet restart
this becomes an operator-visible stall with a blocker that reads like a fault.
Readiness should either warm eagerly at service start or the blocker copy
should say "warming, retry shortly."

### F5 — Refusal ordering makes probes (and operators) see the wrong gate first

Before pairing, the deploy layer refuses with "not currently selectable" —
the override-validity check never runs. Only after pairing does the same
request draw the precise "An evidence override is not valid for Paper
deployment" boundary. Correct fail-closed behavior, but the *first* error an
operator sees for a misconfigured request often names a different problem
than the one they need to fix. The admission preview endpoint
(`POST .../bots/admission`) already exists and could report the full gate
ladder in one shot.

Morning failures (F6–F9, documented in the companion audit): zero-bar engine
run reporting `success=True`; the UI flag form hard-requiring a QC backtest id
it no longer records; the pre-#1746 canary ledger failing closed on a missing
checkpoint; the human-flag toggle defaulting to Reject.

## 4. What already works well (keep these properties)

- **The deploy contract is closed, typed, and fast.** One POST with the full
  ticket; admission errors are structured (`message`/`why`/`admission`);
  idempotent instance identity; 201 returns a durable receipt.
- **The two-step pairing review is cheap** (28 ms combined) while still being
  content-addressed and append-only — ceremony where it matters, no latency
  tax.
- **The override boundary is enforced in both directions** (evidence-only
  requires it; accepted rejects it) — verified live by probes from the API.
- **Stops and resumes are receipt-backed and idempotent** (`idempotency_key`
  dedupe, `applied` flag), so a retrying script cannot double-fire a command.
- **8 concurrent 1-share bots on one paper account run clean** — no clerk
  contention, no attention flags, catalog projection stays coherent.

## 5. Recommendations toward practical / simple / robust

1. **One lifecycle surface.** Collapse to run/stop/resume (what the platform
   actually is today): make the panel action delegate to the runner stop (or
   retire the legacy route), present Resume/Stop from one policy, and delete
   or genuinely implement `pause`/`continue`. Every dead or duplicate control
   is operator confusion and test surface.
2. **Make token stability a property, not a patch.** Two incidents of the
   same churn class (2026-08-04, today) mean the denylist normalizer will be
   wrong again the next time someone adds an observation-stamped evidence
   ref. Invert it: build the token from an explicit allowlist of stable
   identities (program seal, validation snapshot, registry generation, clerk
   journal seq, config hash, allowed/reason_code). Add a property test: two
   admissions computed seconds apart with no state change must yield
   identical tokens.
3. **Stop making the human do the machine's retry.** A stale-token 409 whose
   remedy is "refresh and retry" should be absorbed server-side: re-derive
   the action, and if it is still enabled with an unchanged *decision* (not
   token), execute. Reserve the 409 for genuine decision changes.
4. **Decouple action execution from full panel recomputation.** Each action
   already declares its own compare-and-set domain (`revision_inputs`);
   verifying just that domain would turn a ~20 s command into a sub-second
   one and remove the window in which tokens drift.
5. **Warm readiness proofs at service start** (or label them as warming) so a
   fleet restart doesn't present transient blockers as faults (F4).
6. **A launch is already scriptable end-to-end — package it.** Today's whole
   sequence (flag → pairing → deploy → verify) ran as three small scripts
   against public endpoints. A `scripts/dev/launch_bot.py <strategy> <symbol>`
   (or a fleet manifest: N strategies × symbols × sizes in one file, applied
   idempotently) would make the ceremony one reviewable command, which is
   the single biggest practical-simplicity win available without new backend
   surface.
7. **For scalability, add fleet-scoped primitives** where today everything is
   per-bot: batch admission preview, batch deploy from a manifest, a fleet
   status stream (the only fleet watch today is polling the catalog every N
   seconds — fine at 8 bots, wasteful at 80), and per-account rollups that
   the catalog already computes but nothing aggregates across accounts.
8. **Keep the receipts.** Whatever gets simplified, the property that every
   state change today produced a durable, replayable receipt (deploy receipt,
   action receipt, admission decision, pairing event seq 2–7) is what made
   this study — and any future incident forensics — possible. Simplicity
   should come from fewer *surfaces*, not fewer receipts.

## 6. Fleet state at first-round end (~13:15 ET)

Eight bots ON_DUTY / running under SQLite Clerk custody, one Alpaca paper
account: the five ceremony bots (resumed post-fix), `validation-spy-0824`,
`validation-qqq-0824` (deployment_validation), `validation-ema-spy-0824`
(ema_crossover_signal). Zero fills at that point; session results land in the
companion audit's §6 at close.

## 7. Afternoon probe round (operator: "keep tinkering, find bugs")

Second round, ~13:15–13:45 ET: two more deployment_validation launches
(TSLA, AAPL — fleet now 10), a five-suite probe pass (deploy boundary,
idempotency/races, lifecycle edges, pairing ceremony, read consistency;
raw log `probes_study.jsonl`), and read-scalability measurements.

### Proven working (first live evidence today)

- **Full trade cycle, three symbols**: deployment_validation entered on two
  consecutive green minute bars and exited exactly 3 decision clocks later,
  returning to flat with receipts — QQQ +$0.03, AAPL +$0.11, TSLA −$0.27.
  Entry/exit decision receipts, fill rows, and exposure all agree.
- **Boundary behavior that held up**: duplicate instance deploy → 409 with
  the bot untouched; concurrent duplicate deploy race → exactly one 201;
  quantity 0/101/safe-canary-2 → 422s; extra request field → 422; unknown
  strategy key → 422; tampered pairing confirmation token → refused;
  duplicate pairing plan → refused with "already active"; stop of a stopped
  bot → honest 404 ("Only a running bot can be stopped").
- **Corpus gate tells the truth**: NVDA and IWM deploys refuse with
  `PROGRAM_BUILD_UNPROVEN` — "resolved parameters the golden qualification
  corpus does not cover" — because the deployment_validation corpus
  qualifies exactly {SPY, QQQ, TSLA, AAPL}. Unknown symbols (`ZZZZTEST`)
  land in the same gate. Correct fail-closed design; symbol coverage is a
  seal property, which is worth knowing before planning a fleet.

### New findings (F10–F16)

- **F10 — dry-run deploys are broken on this topology and leak state.**
  `execution_mode="dry_run"` 500s: the synthetic clerk authority is created
  under `/app/artifacts/accounts/alpaca/sim:<sid>` — a virtiofs bind mount —
  and the SQLite-WAL locality guard then correctly refuses it. Three defects
  in one: (a) sim authorities belong on the same named volume as real ones
  (`ALPACA_CLERK_DIR`), not the bind-mounted artifacts tree; (b) the failure
  surfaces as a raw `{"success": false, "error": <infra internals>}` 500
  instead of the typed deploy-refusal envelope; (c) each attempt leaves an
  orphan `sim:<sid>/` directory (with a partially provisioned
  `source_bars.sqlite3`) — refused deploys must not leak state. Also, deploy
  refusal copy elsewhere advertises "Dry Run is still available", which is
  false on this stack. Probe orphans were quarantined to
  `artifacts/accounts/alpaca/_probe_orphans_2026-08-24/` (moved aside, not
  deleted).
- **F11 — burst deploys trip a channel-health flap that masks the real
  refusal.** Four deploys fired in ~1 s: two succeeded, and the refusals
  rotated between "Market Data is unhealthy" (shared-feed bar age crossing
  its threshold during subscription churn) and the true corpus refusal —
  three different refusal faces for the same request within six minutes.
  Admission couples to an instantaneous feed-age sample with no
  retry/settling semantics, so launch throughput is hostage to a transient
  hiccup that running bots simply ride through.
- **F12 — the LIVE chart pane lags its own bot.** After the service restart
  and after fresh deploys, the per-bot LIVE pane served bars ending 7–12
  minutes before `as_of_ms` while the bot's decision stream consumed current
  IBKR bars; panes healed lazily minutes later. Two surfaces disagree about
  "the live bars" with no staleness marker on the stale one
  (`overlay_notices` stayed empty).
- **F13 — panel reads serialize globally.** One panel GET: 56 ms. Ten
  concurrent panel GETs: 2.6 s wall, and every request takes ~2.6 s — the
  projection path is effectively a global queue. A dashboard polling 80 bots
  would spend ~21 s per sweep. Catalog: 184 ms for the account.
- **F14 — the gallery bootstrap is heavy and unbounded by liveness.**
  `gallery/snapshot`: 5.6 s, 751 KB, 25 tiles — every historical stopped bot
  ships with the running ten. (Correction to §5.7: a push channel does
  exist — the gallery has an SSE stream; it is the *per-bot* panel that is
  poll-only.)
- **F15 — action idempotency is scoped behind presentation.** Replaying the
  idempotency key of an already-succeeded Resume against the now-running bot
  returns 404 ("action not available") — the presentation check runs before
  the idempotency lookup, so replay dedupe only works while the action is
  still presented. Harmless today; surprising for any at-least-once command
  queue built on top.
- **F16 — `retire` joins `pause`/`continue` as unreachable vocabulary** —
  never presented by the SQLite panel source in any state observed today
  (running, stopped, flat, exposed).

## 8. Organic crash + supervised SIGKILL round (~13:45–14:05 ET)

### The organic catch: F9→fixed — long bot names crash on their first trade

At 13:46 ET `ceremony-spy-strategy-c-0824` got its first signal of the day and
**CRASHED** with `OrderRefTooLongError`: every order carries
`learn-ai/{sid}/v1:{intent_id}` (35 fixed chars) under the 60-char order_ref
cap, so any name over 25 chars deploys fine, runs fine, and dies the moment it
tries to trade. Four of the five ceremony bots were such time bombs — part of
their all-day silence. `order_identity.validate_broker_owned_instance_id`
existed for exactly this and had **zero callers**. Fixed on master
(`ff5ed49f`): the Alpaca deploy request now refuses over-cap names with a 422
naming the cap (live-verified both directions; read models keep the loose
validator so existing long-named bots stay readable). The three remaining
long-named ceremony bots were stopped; Strategy C got a cap-compliant
replacement (`cer-c-0824`). Positive note: the crash reporting was exemplary —
`duty_outcome=CRASHED` with exception type, message, file and line.

### The SIGKILL test: honesty perfect, recovery split exactly by exposure

With 7 bots running (3 mid-hold), the data plane was killed with SIGKILL at
13:51 ET and restarted: back to healthy in 35.6 s, and **all bots immediately reported
not-running** — zero dishonest roster rows. Resume then split precisely on
custody state: the three flat bots resumed; the three bots holding one share
each (SPY, QQQ, AAPL) were refused with `RESUME_CARRYOVER_UNSUPPORTED` —
honest and correct, since deployment_validation's seal declares
`countdown_state_persistable=False`.

### F17 — `prepare_safe_flatten` presents as enabled but cannot execute

After `reconcile_now` refreshed evidence, the action showed
`enabled: true` — and executing it returned 409 "This recovery capability is
a view action, not a broker mutation." An enabled-looking action that can
never execute through the actions endpoint is a contract smell (the
`mutation: false` fact exists server-side but is not reflected in the
presented enablement).

### F18 — crash-held exposure has NO path to flat (the day's biggest finding)

Every pointer in the recovery chain leads to a door that does not exist:

1. Resume refuses (`RESUME_CARRYOVER_UNSUPPORTED`) and points to a
   "Clerk-proven flatten".
2. Deploy-time carryover is globally disabled; its copy also points to a
   "Clerk-proven flatten before Resume".
3. `prepare_safe_flatten` builds a versioned `SafeFlattenPlan` — and **no
   executor for that plan exists anywhere in the codebase**.
4. `flatten_stop` has a working performer but is never presented under SQLite
   custody (same dead-vocabulary class as pause/continue/retire).
5. Manual order tickets are gated off: `MANUAL_TRADING_NOT_QUALIFIED`.

Net: the three 1-share paper positions are stranded — attributed, honest,
visible, and unreachable. They were deliberately left held as concrete
evidence (see judgment call 16). The missing piece is the execute-side of the
safe-flatten plan (or presenting `flatten_stop`); until one exists, **any
crash while holding exposure requires out-of-band broker intervention**,
which at fleet scale is untenable.

## 9. Cadenced launch stress round (14:29–14:37 ET) + close-out

Operator directive: stress bot launch and concurrent operation by launching
at regular intervals. One bot every ~30 s, rotating all seven paired
strategies across the four qualified symbols (raw log `stress_study.jsonl`).

### Results — launch and concurrency scale cleanly

- **14/14 launches succeeded, zero refusals** at the 30 s cadence — the
  channel-health flap (F11) is a burst artifact, not a throughput limit:
  4-in-1-second flapped, 1-per-30-seconds never did.
- **Deploy latency flat as the fleet grew 5→18 running**: 234–512 ms, no
  trend. Catalog reads flat too: 97–144 ms.
- **18 concurrent bots, zero stale decisions** — the liveness sweep found
  every running bot deciding on every minute bar. Several stress bots
  completed full trade cycles within minutes of launch.
- **Mass stop at the close: 17/17 in 8.6 s wall** (0.2–1.0 s per stop via
  the runner route), zero failures.
- Panel fan-out stayed the weak read (1.1–4.7 s for 5 concurrent), but its
  cost tracks load, not fleet size — consistent with F13's global
  serialization.

### F19 — a concurrent reduce race crashes a healthy bot

`st01-dv-spy` and `st08-dv-spy` entered SPY on the same signal bar and
exited on the same decision clock. One of the two simultaneous reduces hit
the Clerk's designed fail-closed refusal — `AdmissionBlockedError: reduce
blocked: BROKER_SNAPSHOT_STALE — The Clerk changed while final broker truth
was observed` — and the runner escalated that *retryable* refusal into a
fatal crash (14:44 ET; duty outcome honest, bot ended flat, custody
unharmed). Same-symbol signal cohorts exit in lockstep **by design**, so at
fleet scale this race fires routinely and randomly kills members. The fix
shape: classify snapshot-staleness admission blocks as retry-on-next-clock
in the runner's error taxonomy, not as crashes.

### Barrier verification

The 15:45 stop/flatten barrier was observed live for the first time: the
last pre-barrier entries (QQQ pair, ~15:41) were flattened at the barrier
minute, and no bot entered between 15:45 and the 16:00 close.

### Additions to the recommendations

- (extends §5.1) The one-lifecycle-surface cleanup should also decide
  `retire`'s fate (F16) and fix or fence dry-run (F10) — today the mode is
  advertised in refusal copy but cannot work on the reference topology.
- (extends §5.5) Admission's channel-health input needs settling semantics
  (e.g. two consecutive unhealthy samples, or "unhealthy for > N s") so a
  single late bar doesn't refuse a burst of launches (F11).
- (new) **Fleet reads need a fan-out budget**: break the global panel
  serialization (F13), bound the gallery snapshot to live-or-recent bots
  (F14), and stamp staleness on any pane serving old bars (F12).
- (new, highest priority from §8) **Ship the flatten executor.** F18 makes
  crash-with-exposure an out-of-band incident. Either implement the
  execute-side of the `SafeFlattenPlan` the clerk already builds, or present
  the existing `flatten_stop` performer under SQLite custody — with a test
  that walks crash → refuse-resume → flatten → resume to flat.
- (new) Presented actions should carry their `mutation`/executability fact so
  a view action never renders as an executable enabled button (F17).
- (new, from §9) **Make snapshot-staleness admission blocks retryable in the
  runner** (F19): a fail-closed Clerk refusal during concurrent same-clock
  reduces should defer to the next decision clock, not kill the bot. With
  that fix plus the flatten executor, the measured envelope — flat deploy
  latency to 18 bots, zero stale decisions, 8.6 s fleet-wide stop — says
  the current architecture already carries a considerably larger fleet.
