# Strategy-execution correctness, resiliency, and provability — top-5 research directions

**Date:** 2026-08-24 · **Prompted by:** PR #1747 (2026-08-24 multi-bot session, F1–F19) · **Status:** research brief — work to be executed in separate sessions
**Method:** three parallel code/doc investigations against the working tree at `master` (post-#1747-merge): (a) the four `docs/audits/*-2026-08-24.md` session documents and the F1–F19 adjudication table, (b) the full backtest→proof→deploy→live chain, (c) the execution-resiliency architecture (clerk, runner, admission, sweep). All file:line citations were verified on the 2026-08-24 tree; re-verify before acting.

---

## Standing constraint (operator decision, 2026-08-24)

**The evidence-only override in Paper execution is permanent**, kept deliberately for ease of testing. No direction below proposes removing it or tightening Paper admission. Rigor investment goes into (1) making every paper run *generate* proof on the way out, and (2) making the paper→live promotion path airtight — never into making paper harder to enter.

---

## The cross-cutting diagnosis

Both independent investigations converged on the same meta-finding: **this system's characteristic failure is correct mechanisms left unwired, not wrong logic.** Confirmed instances:

| Mechanism | State | Consequence |
|---|---|---|
| `validate_broker_owned_instance_id` (`app/engine/live/order_identity.py:237`) | Existed with **zero callers** until `ff5ed49f` | 4 of 5 ceremony bots were live time bombs; one crashed on its first order (F9) |
| `SafeFlattenPlan` (built at `recovery_policy.py:653-699`) | **No executor anywhere in the codebase** | F18: crash-held exposure has no path to flat |
| `flatten_stop` performer (`panel_data_source.py:1016-1053`) | Fully wired, guard + performer + `flatten_supported=True` — **never presented** (`sqlite_panel_adapter.py:61` presents only `resume`) | Same F18 dead end from a second angle |
| `emergency_flatten_strategy_instance_id` (`order_identity.py:113-122`) | Zero production callers | Third dead end |
| `run_shadow_trace_evaluation` (`qualification_shadow_trace.py:245-320`) — live↔backtest trace comparator | Invoked **only in a unit test** over synthetic bars | The one mechanism that would mechanically prove live≡backtest is not part of any run's lifecycle |
| Full-gate-ladder admission preview (`POST …/bots/admission`) | Exists, unused by refusal flows | F5: first refusal names the wrong problem |
| Binding-ledger parity (`account_binding_ledger.py:125-154`) | Read-only, env-gated, display-only, "without repair" | A named pre-run blocker that gates nothing |

Similarly, the two in-session fixes (F1, F9) were both "mechanism existed, wasn't connected at the boundary," and the two top-priority open findings (F18, F19) are both "correct fail-closed refusal has no correct consumer." **The resiliency gap is in the connections, not the logic.** Every direction below is, at heart, a wiring project — which is good news: the hard parts are mostly built.

---

## Direction 1 — Close the exposure lifecycle: every position must have a path to flat

**The gap.** Stop, by design, cancels working entries and leaves attributed exposure untouched (`panel_data_source.py:983`, `bot_carryover.py:133-182` — `STOP_REQUIRES_FLATTEN` is a *recorded string*, not an action). All five recovery pointers dead-end (table above, plus manual tickets gated `MANUAL_TRADING_NOT_QUALIFIED` and `EXPOSURE_CARRYOVER_STRATEGY_KEYS = frozenset()`). Three 1-share positions (SPY/QQQ/AAPL) on `PA3KWXU1C4C3` sit stranded-but-honest as standing evidence. The ops study calls this "the day's biggest finding": *any crash while holding exposure requires out-of-band broker intervention* — untenable at fleet scale.

Three adjacent holes complete the picture:

- **Stuck-EXIT is terminal, silently.** `EXIT_NOT_FLAT` folds the effect to `failed`; `reconcilable_effect_operations` (`reads.py:475-486`) selects only non-terminal states, so **the sweep never retries it**, fence resolution is passive (`reconcile.py:480-492` clears only if exposure *happens* to go flat), and there is no age watchdog — a stuck EXIT is re-driven never, forever.
- **Retryable refusals crash healthy bots (F19).** The EXIT path's `require_capability(REDUCE, …)` sits outside any try (`runtime.py:785` → `exit_resolution.py:158-167`); `AdmissionBlockedError: BROKER_SNAPSHOT_STALE` propagates to `_supervise`'s `except Exception` → `finalize_crash`. Same-symbol cohorts exit in lockstep *by design*, so this fires routinely at scale. F11 (burst-deploy feed flap) and F4 (post-restart warmup masquerading as a fault) are the same missing taxonomy at the admission boundary.
- **Strategy intent is a third truth with no check.** The 15s sweep compares broker↔journal (order identity, position qty at `ε=1e-9`, effect terminality) but never reads the `SignalSession`'s own lifecycle state back. A strategy that believes it is flat while the journal shows exposure produces no signal — it just stops trying to exit.

**Research questions.**
1. What is the safe executor for `SafeFlattenPlan` under Clerk custody — cancel-first, attributed-quantity-exact, idempotent under the existing claim/fence machinery? Should it *be* the already-working `flatten_stop` performer, presented by the SQLite panel, or a new recovery-catalog action?
2. What is the retry/escalation policy for `EXIT_NOT_FLAT` — bounded resubmission? age-thresholded uncertainty escalation? operator page?
3. Define the transient-vs-terminal refusal taxonomy once (reason-code-level: `BROKER_SNAPSHOT_STALE`, `MARKET_DATA_UNAVAILABLE` warmup, etc.) and identify every consumer that must honor it (runner EXIT path, deploy admission, resume admission).
4. Should the sweep gain a third comparison — strategy-intent vs journal — or is a per-decision assertion at `commit_signal_decision` cheaper and sufficient?

**Done when** a property test can walk: crash-with-exposure → refuse-resume → flatten (via presented action) → resume-to-flat; a stuck EXIT older than N minutes raises a durable, operator-visible escalation; and an injected `BROKER_SNAPSHOT_STALE` on a cohort exit produces N delayed exits and zero crashes.

**Key files:** `recovery_policy.py`, `recovery_execution.py:77-196` (zero position-reducing actions today), `sqlite_panel_adapter.py:61`, `exit_resolution.py:193-236`, `reads.py:475-486`, `uncertainty.py:581`, `runtime.py:682-686` (ENTER-only catch), `bot_carryover.py`.

---

## Direction 2 — Run-scoped replay proof: every paper run becomes its own experiment receipt

**The gap.** Paper/live runs are **not replayable after the fact**. `run_trade_bot` receives no `source_bars` sink (`bot_runtime.py:128-136` wires the `SourceBarLedger` for dry-run only); decision receipts record *what was decided*, never *the bars it was decided on*. Meanwhile the exact comparator needed — `run_shadow_trace_evaluation`, which replays reference (BacktestEngine) vs observed (`strategy_evaluations`) over the same bars and fails closed on the first divergent field — **exists and is never invoked outside a unit test**. `bot_trade_strategy.py:99` even points at it as the parity proof — as a comment, not a call.

This is the highest-leverage direction given the standing constraint: paper stays cheap to enter, but **every run proves itself on the way out**. The decision math is already literally shared code between engines and live (the seam is exact and documented in `test_signal_program_mode_parity.py:12-58` — only evaluation-mode supply and settlement differ), so the replay check is closing a loop the architecture already promises.

**Research questions.**
1. Wire `SourceBarLedger` into `run_trade_bot` — what are the retention/size economics per run (it already has WAL checkpointing, backup-manifest schema v2, and a retention floor from #1740)?
2. Define the per-run parity receipt: on Stop/EOD (or on demand), replay the run's recorded bars through `BacktestEngine` via `run_shadow_trace_evaluation`; emit a receipt (trace roots, first divergence if any) into the run's evidence directory alongside `run_build_evidence/`.
3. The mode-parity test names its own residual gap: Paper's market-liveness gate on ENTER and real Clerk-rejection branches live outside the `SignalProgram` surface. How do those settle-path differences get represented in the receipt so a divergence is *classified* (expected-live-effect vs real drift) rather than binary?
4. Where does the receipt surface — proof dossier? bot panel? promotion evidence for Direction 3?

**Done when** any completed paper run can be replayed deterministically from its own retained bars, the replay's trace root is compared against the live decision trace mechanically, and the resulting receipt is durable evidence attached to the run — turning the evidence-only lane into an evidence-*generating* lane.

**Key files:** `bot_runtime.py:128-136`, `source_bar_ledger.py`, `qualification_shadow_trace.py:245-320`, `bot_trade_strategy.py` (`strategy_evaluations`, `:99`), `test_signal_program_mode_parity.py:60-67`, ADR 0043 §5.

---

## Direction 3 — Proof chain of custody: bind deploys to runs, make proof artifacts verifiable

**The gap.** "Provably equivalent to its validated backtest" currently has **no identifier for "its validated backtest."** Fifteen chain gaps were found (G1–G15); the load-bearing ones:

- **G1** — `paper_deploy_service.py` (605 lines) carries no `study_id`/`strategy_execution_id`/run reference. The seal pins parameters, cadence, symbol, account — never the run whose numbers the operator looked at.
- **G2** — `reconciliation_ref` is a never-hashed string; `reconciliation_status`, `trades_matched`, `pnl_max_abs_diff`, `divergence_counts` are **hand-typed manifest JSON** that `strategy_proof_dossier.py:182-236` trusts with no re-run. Flipping `"failed"`→`"passed"` is invisible to every hash check.
- **G3** — `qc_cloud_backtest_id` gates deployability but is never resolved; two different strategies carry the *same* ID.
- **G4** — the LEAN image pin moved (`0b8d4e38…` → `3dd0033…`, `lean_sidecar/config.py:80-85`) out from under the only external reconciliation receipt; the digest is in no proof hash, so nothing went stale.
- **G8** — the *actual* deployability state (flag ledger, canary ledger) lives in gitignored `PythonDataService/artifacts/`; a fresh checkout cannot reconstruct which 5 of 7 programs are paper-active on `evidence_only`.
- **G13** — engine-source persists are non-idempotent (`engine-persistence-authority.md:23`): re-running the same backtest mints a new `StrategyExecution` row, so "the validating run" isn't even a stable row.
- **G5** — the build proof's `artifact_digest` deliberately excludes `execution/{commission,sizing,portfolio,order}.py`: editing the commission model changes every backtest's PnL and leaves every receipt `PROVEN`. Decision proof ≠ economic proof.

**Research questions.**
1. Design the deploy→run binding: which identifier (a content-addressed run digest? `parity_group_id`? an idempotent engine-run key fixing G13)? Where does it live in the seal and the receipt chain?
2. Extend `_artifact_check` hashing to `reconciliation_ref`, and decide: do hand-typed diagnostics get replaced by machine-emitted reconciliation artifacts (the reconciler already outputs them), with the manifest holding only their hash?
3. Ledger durability: commit the flag/canary ledgers (they're append-only and hash-chained already), or snapshot them into the repo/backup bundle on a cadence? What's the privacy/noise tradeoff?
4. Should the LEAN image digest and the calendar/version pins join the proof hash, so a pin move stales dependent proofs automatically?
5. Economic proof: a second trace root over fills/PnL from the qualification corpus, or fold the execution layer into the digest? (The exclusion was deliberate — re-litigate it with the original rationale in `run_signal_program_build_qualification.py:133-152`.)

**Done when** given only the repo + one deployed bot's instance id, an auditor can mechanically retrieve: the exact validating run, its input hashes, the reconciliation artifact (hash-verified), the engine image digests, and the human override trail — with any tampering or drift breaking a hash check rather than a promise.

**Key files:** `paper_deploy_service.py`, `strategy_validation_manifest.py:190-258, 757-818`, `strategy_proof_dossier.py`, `signal_program_admission.py`, `canary_admission.py`, `engine-persistence-authority.md`, `lean_sidecar/config.py`.

---

## Direction 4 — Two-engine parity as a continuous property, not a dated ceremony

**The gap.** 5 of 7 sealed programs (`sma_crossover`, `rsi_mean_reversion`, `spy_strategy_a/b/c`) have **zero cross-engine evidence** — their own attribution files say "a self-consistency pin, not a cross-engine parity claim" — and they cannot even be run through LEAN: only 4 registrations carry a `lean_twin` (`registry.py:1176,1907,2800,2831`), and the frontend template union is closed over 3 (`lean-validation-template.ts:3-19`). "Both" runs get an honest `unavailable` verdict via `parity_companion.py:59-79` — honest, but permanent.

Meanwhile reconciliation is **entirely off-CI**: `tests/integration` and `tests/research` are absent from `PYTHON_FAST_BASELINE_TEST_DIRS` (`ci.yml:255-258`), the one LEAN↔spec parity test is `slow`-marked and always excluded (`ci.yml:350`), extras are diff-driven per-file so a change to the *shared* fill/commission/portfolio models triggers no reconciliation test at all, and no CI job verifies the build-receipt manifest against current bytes. Two divergence categories (`FIXTURE_INSUFFICIENT`, `ORDER_TYPE_MISMATCH`) are structurally unclassifiable at the compare layer yet sit in the gating set (`engine-persistence-authority.md:63-79`).

**Research questions.**
1. What does a `lean_twin` cost per sealed program? The four existing twins are the template — is the blocker LEAN algorithm authoring, fixture capture, or the closed frontend union? Sequence the five by strategy complexity.
2. Design the recurring parity lane: a scheduled (nightly/weekly) CI job — not per-PR — that re-runs each twin pair on a pinned window and diffs against the last accepted reconciliation receipt; plus a per-PR trigger keyed on the *shared execution layer* paths, not per-strategy files.
3. Make reconciliation receipts regenerable: today each is a dated hand-run capture. What's the `scripts/` entry point that produces a receipt from (strategy, window, image digest) deterministically?
4. Resolve the two unclassifiable gating categories: extend `PersistLeanTradePayload` with order-type codes and give the compare service bar access, or demote them from the gating set with documented rationale.
5. Cheap wins while twins are built: the parity companion already fans out server-side on "both" — could a nightly sweep run "both" for every twinned strategy and trend the verdicts?

**Done when** every sealed program has either a LEAN twin with a regenerable reconciliation receipt or a documented, ADR-recorded exemption; parity runs on a schedule with drift alerting; and a shared-execution-layer edit cannot merge without the reconciliation surface running.

**Key files:** `registry.py` (lean_twin sites), `lean-validation-template.ts`, `parity_companion.py`, `ParityVerdictService.cs`, `ci.yml:255-350`, `qc_reconciler.py`, `docs/references/reconciliations/`.

---

## Direction 5 — Deterministic control plane at fleet scale

**The gap.** The day's stress tests proved the *write* path scales (14/14 launches at 30s cadence, deploy latency flat to 18 bots, 17-bot stop in 8.6s) and isolated the walls:

- **Admission-token stability is a patched property, twice.** F1 was the *second* incident of the evidence-ref-churn class (after `val-nvda-0804-05`); both fixes were denylist normalizers. The ops study's own recommendation (§5.2): invert to an **allowlist of stable identities** (program seal, validation snapshot, registry generation, clerk journal seq, config hash, allowed/reason_code) plus the property test "two admissions computed seconds apart with no state change yield identical tokens." Not done. F15 (idempotency scoped behind presentation) belongs to the same fence.
- **Dead vocabulary presented as real.** `pause`/`continue`/`retire` exist as guards+performers but can never fire; `prepare_safe_flatten` presents `enabled: true` and always 409s (F17); dry-run is advertised in refusal copy but cannot work on the reference topology (F10). The presented-action contract carries no executability fact.
- **Reads are the scaling wall.** Panel reads serialize globally (56ms alone → 2.6s×10 concurrent, ~21s/sweep at 80 bots, F13); `run_action` recomputes the entire panel projection before executing (~20s for a 0.3s operation); gallery snapshot is unbounded (F14); the LIVE chart served 7–17-min-stale bars with `overlay_notices` empty (F12) — the same provenance-field-present-but-unpopulated defect R4 fixed on the tape.
- **Launch is scriptable but unpackaged** (§5.6: flag→pairing→deploy→verify ran as three small scripts; "the single biggest practical-simplicity win available without new backend surface"), and there are no fleet primitives — no batch admission preview, no fleet status stream, manual EOD stop sweeps.

**Research questions.**
1. Specify the token-allowlist derivation and its property test; migrate `_stable_admission_evidence_refs` from denylist to allowlist.
2. One lifecycle surface: collapse to run/stop/resume(+flatten from Direction 1); delete or genuinely implement `pause`/`continue`/`retire`; presented actions carry `mutation`/executability facts so a rendered button is a provable capability. Add the reachability test class: **every presented action executes; every guard has callers; every plan has an executor** — this is the systemic answer to the "unwired mechanism" failure mode.
3. Decouple action execution from panel projection using each action's declared `revision_inputs`; absorb stale-token 409s server-side (reserve 409 for genuine decision changes); give admission's feed-health check settling semantics (two consecutive unhealthy samples) instead of an instantaneous sample.
4. Fleet primitives: `launch_bot.py <strategy> <symbol>` / an idempotent fleet manifest; a fan-out read budget; staleness stamped on every read surface (F12/R4 generalized: any pane serving non-current data says so).

**Done when** a 30-bot fleet can be launched from a manifest, watched through reads that cost O(changed), and stopped at the bell by schedule; and no control can render as enabled unless its executor exists and its gate passes.

**Key files:** `action_policy.py` (`_stable_admission_evidence_refs`), `sqlite_panel_adapter.py:61`, `panel_data_source.py` (`run_action`), `sqlite_panel_source.py:839-842`, ops study §5.1–5.8, §9.

---

## Blind spots surfaced (beyond the five)

1. **The defect backlog has bifurcated.** `docs/known-gaps.md:12-18,38` claims to be the only durable home for open defects and asserts "no known-open safety-critical gaps" — while 17 open findings (F2–F8, F10–F19), two flagged top-priority and both safety-critical, sit in `review-handoff-2026-08-24.md` unfiled. Until the review agent files the `F<N>:` issues, the repo's own gap registry is false. Cheap, do first.
2. **The evidence base has defects.** F6–F9 numbering collides between study §3 and handoff §3 — the pre-#1746 canary-ledger repair finding is **orphaned with no ID**; F1's attempt count disagrees across docs (21/21 vs 0/20); F12's lag window disagrees (7–12 vs 7–17 min); handoff §5 cites a judgment call that doesn't exist; handoff §6's `git log origin/master -8` instruction can't work post-squash-merge. A reviewer adjudicating F1–F19 needs these corrections up front.
3. **Known-gaps §8 vs #1730 promotion.** `known-gaps.md:127-140` says FR-016 crash capture covers only `ema_crossover_signal`; the #1730 promotion sealed all seven programs behind `SignalSession`. One of the two is stale — verify which before trusting crash-window coverage claims.
4. **The writer census is spelling-based** (five confirmed false-negative classes, `known-gaps.md:148-166`) and `strict_yield_detection=False` in production — two guardrails widely read as stronger than they are.
5. **ADR 0010 is Accepted and describes retired vocabulary** (`FLATTEN_NOW`/`PAUSED` from the IBKR control plane); Direction 1's design should supersede it explicitly, not silently.
6. **No metrics or alerting anywhere in the execution stack** — divergence, dead heartbeats, and sweep-failure streaks are visible only in logs or by polling REST. Every direction above lands stronger if its invariants emit an observable signal; consider a thin structured-counters seam as shared infrastructure rather than a sixth direction.
7. **Restart-intensity history is in-memory** (`bot_runner.py:1379-1386`) — the crash-loop gate resets on every container restart, i.e., precisely when it matters.

## Suggested sequencing

Direction 1 first (it is the open safety hole and unblocks honest fleet operation), Direction 5's token/reachability slice second (it prevents the next F1-class regression and the next unwired mechanism), then Directions 2→3→4 as the provability arc — 2 generates run-level evidence, 3 makes the evidence chain tamper-evident, 4 keeps the two engines honest against each other continuously. Blind spot #1 (file the F-issues, reconcile known-gaps) is a half-day and should precede everything.
