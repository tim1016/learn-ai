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

## 6. Fleet state at study end (~13:15 ET)

Eight bots ON_DUTY / running under SQLite Clerk custody, one Alpaca paper
account: the five ceremony bots (resumed post-fix), `validation-spy-0824`,
`validation-qqq-0824` (deployment_validation), `validation-ema-spy-0824`
(ema_crossover_signal). Zero fills at study end; session results land in the
companion audit's §6 at close.
