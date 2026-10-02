# ADR 0074 — Golden Search: an approved research candidate becomes an immutable per-stock qualified version that covers its exact tuple

**Status:** Accepted 2026-10-01
**Provenance:** Owner decisions in [#2696](https://github.com/tim1016/learn-ai/issues/2696) — the 2026-09-30 grilling, the adversarial revision of that PRD on [#2698](https://github.com/tim1016/learn-ai/pull/2698), and the owner's interview decision of 2026-09-30: *"Make golden settings available for Paper or Live in Deploy."* The owner asked for the feature to be built on 2026-10-01.
**Vocabulary:** `CONTEXT.md` § "Golden Search (resolved 2026-10-01)".
**Amends:**
- ADR 0054 decision 5 ("Deploy-time qualification … rejected for now") and its 2026-09-27 amendment's sentence "Golden acceptance, broker access and Manual override cannot convert an uncovered tuple into a covered one". Both now carry one exception: an approved Golden Search qualification covers its exact tuple. Golden acceptance alone still covers nothing.
- ADR 0061 decision 6, in one respect. A Golden Search approval writes its own Golden Validation record, a Manual override over Python-only evidence with the owner's note. It is not hand-selected in the Golden Validation workbench.
- ADR 0056 is not amended. The Walk-Forward Study page keeps its grid-over-folds procedure. Golden Search validates a different procedure, described below.

**Related:** ADR 0043 (seals and the running-build proof), ADR 0055 (Python-owned research tables, attempt fences), ADR 0065 (research job liveness is a worker lease), ADR 0068 (Python owns the math), ADR 0072 (research run identity).

## Context

The owner trades the EMA crossover on one golden configuration per program. Before this decision, moving it meant three things: editing `validated_settings`, regenerating the corpus and receipts, and restarting. Every move stranded the bots sealed to the previous point (2026-09-01 and 2026-09-17). Coverage is required in Paper, Shadow and Live (ADR 0054 amendment, #2540). So no tuple other than the registry's single point could run anywhere except Dry Run. A Golden Validation record could not change that either: it satisfies the strategy-validation gate, not the coverage gate.

Grid Search and the Walk-Forward Study already answer "what did these settings do?" Neither can hand a result to anything that makes it deployable. The original PRD's procedure also leaked the future. It narrowed ranges on the whole development period and then walked forward inside them. Its weighted-places ranking could be reversed by adding a dominated alternative.

## Decision

### 1. One study, five pauses, a frozen protocol

A Golden Search study covers one registered strategy and one stock. Locking the plan freezes the whole protocol:

- knobs, ranges, quantization and order;
- the search method and the selection policy;
- the development and final intervals, and the fold lengths;
- execution assumptions and stress scenarios;
- the incumbent tuple, and the budget.

Locking also captures one content-hashed data snapshot over the run-up, development and final intervals, together with the executable identity. An edit after lock creates a linked new study and never rewrites the old one. The study pauses after search, after test over time, after the candidate pick, after the final test and after the decision.

### 2. Search is a procedure over one study-owned evaluator

**One evaluator.** Grid and Zoom are two adapters over a single evaluator. The evaluator owns:

- the complete execution identity: program, parameter schema, code and environment digests, data snapshot, interval, costs, fill mode, capital and metric convention;
- the evaluation cache, keyed by that identity plus the point and window, so a point's hash alone is never the key;
- the budget ledger.

Evaluations are study-owned rows, not owned Grid Search sweeps. A sweep plans its own run-up. Adaptive steps would then score different intervals, and that comparison is invalid. Every study evaluation instead uses one run-up depth, planned once at lock from the slowest point of the whole legal search space. If the lake cannot supply that history, the study is refused, not carved.

**Zoom** is a deterministic coordinate search. It moves one knob at a time, coarse to fine, and always compares against the incumbent value. It moves only on a strict improvement under one frozen measure: Sharpe by default, with minimum trades, a drawdown ceiling and a positive-net rule. Ties keep the current value. The result is called "no improvement along the tested moves", never an optimum. Edge-of-range hits are reported.

**Blended places are rejected** as the selector. They depend on the comparison set, so adding a dominated alternative can reverse a choice. A test pins this counterexample.

**Every engine evaluation counts against the study budget (≤ 5,000).** Proof runs count too. The final test and the proof are reserved at lock. The budget is enforced at dispatch by an atomic reservation, not by the estimate. A crashed evaluation re-runs under its original reservation, within a bounded retry allowance.

### 3. Test over time is nested and confirmatory

Each walk-forward fold repeats the complete frozen procedure on its own training window. It starts from the original ranges and the predeclared seed, never from the all-period winner. The fold's winner alone is then evaluated once on the test window. A perturbation of a later fold's data cannot change an earlier fold's selection, and a test proves it. The legacy five-label verdict is kept as a named summary with its coverage ("based on D of S folds"). The test history links fold returns. Each fold starts flat with fresh capital, and a missing fold breaks the line rather than being interpolated.

### 4. The final test is one-shot and its exposure is a cross-study ledger

The final interval is fixed at lock. Picking a candidate and opening the final test are separate commands. Opening is a single transaction, under a per-symbol advisory lock, that does three things:

- classifies the interval's exposure;
- appends the reservation to `research_golden_search_exposures`;
- locks the candidate.

Three exposure states exist:

- **not opened in recorded research** — the only state with a *confirmatory* claim;
- **previously used**, by any earlier Golden Search exposure on that symbol;
- **history unknown**, because a Grid Search, Walk-Forward Study, Strategy Lab run or other study overlapped the interval, and none of those records what the owner viewed.

The exposure ledger has no foreign key to studies, and its rows can never be updated or deleted. A crash resumes the same candidate, never another.

The outcome is one of **meets the stated rules**, **does not meet the rules**, **not enough evidence** or **could not evaluate**. A null result is never a pass.

### 5. Approval publishes an immutable qualified version, and the default is a separate pointer

**Approve golden configuration** runs as a job, using the approval intent recorded by the guarded command. It does three things:

1. **Proof.** It stages the final interval's receipted lake artifacts into a content-addressed blob store and verifies them. It replays the program's decision traces once against the lake and once against a lake root restored from the blobs. The two trace roots must be identical. This is repeatability evidence for these recorded inputs and a restore exercise for the backup. It is not engine agreement and not a forecast.
2. **Run.** It persists a Python-only history run of the approved tuple over the final interval.
3. **One transaction.** It designates that run as a Validation Golden Run and accepts it as a **Manual override** with the owner's note. It inserts the qualified version, which binds program, program version, parameter schema version, symbol, canonical parameters, artifact and wiring digests, the proof, the research outcome, the override flag and the reason. It moves the `(program, symbol)` default pointer by compare-and-swap against the incumbent the owner reviewed.

Any failure before the commit publishes nothing and leaves the old default untouched. A failed, sparse or reused research outcome may still be approved, but only with a written reason and a separate acknowledgement. Its labels stay on the qualified version forever. A technical proof failure cannot be overridden.

A qualified version is never edited. Revocation and re-proof are appended events. Revocation also clears the pointer if the version is the default. A version's status is derived, not stored:

- **revoked** — any revoke event exists;
- **ready** — its latest proof's artifact digest equals the running program's;
- **stale** — otherwise.

A re-proof replays the stored blobs on the current code and must reproduce the stored trace root.

### 6. Coverage: the registry point, or a ready qualified version of the exact tuple

Corpus coverage (ADR 0054) is now **COVERED** in exactly two cases:

- the resolved symbol and parameters equal the registry's `validated_symbols` / `validated_settings` point;
- they equal a *ready*, unrevoked qualified version for the same program, program version and symbol.

The seal records the matched version as `qualification_id`. The field is omitted from the hash when absent, so every pre-existing seal hashes as before. A stale, revoked, unreadable or absent qualification leaves the tuple **UNCOVERED**, and Paper, Shadow and Live refuse it as before. An unreadable research database means "cannot verify", never "covered". The build proof (receipts keyed by program bytes and corpus root) is unchanged. A qualified version still needs proven bytes.

The active default decides only what Deploy *offers*: "Use qualified configuration" and the Golden-scope seed. A change of default revokes nothing, so a non-default ready version stays deployable. Approval starts no bot. Paper and Live are chosen in Deploy under every existing gate: build proof, coverage, Golden Validation, budget consent, custody and the account mode. There is no second research sign-off and no Paper-first rule.

### 7. EMA exposes fast, slow and hold, without moving any existing identity

`ema_crossover_signal` gains three settings, with defaults 5, 10 and 5:

- `fast_period`, from 2 to 30;
- `slow_period`, from 3 to 40, with fast below slow;
- `hold_bars`, from 1 to 26.

`hold_bars` counts decision bars, never wall-clock time, across sessions. The parameter schema becomes `ema-crossover-signal-params/v3`. At the default point, the new settings are omitted from parameter dumps and from the program's evaluation settings. So every existing identity stays byte-identical at that point and the golden trace root does not move: Golden Validation cases, seals, budget tokens and corpus entries. The seal's signal series, exit countdown, parameter-schema version and numerical provenance are resolved from the bot's own parameters: at 5/10/5 they are the reference's own (`v2` and the reference formula), so a seal minted before the change still proves and a new one hashes identically; any other lengths seal `v3` and the extended formula. The LEAN twin hard-codes 5, 10 and 5, so any other value makes the parity companion honestly unavailable.

### 8. Clerks read, the data plane migrates

Admission and the Deploy view run in clerk lanes over the read-only `fleet_lake_catalog` role. That role gains SELECT on three tables: `research_golden_qualifications`, `research_golden_qualification_events` and `research_golden_defaults`. The data plane applies the research schema at startup, so a clerk never meets an unapplied version. The coordinator re-runs `deploy/fleet/sql/provision-fleet-lake-catalog-role.sql` after this migration (that script's own instruction).

## Consequences

- An owner-approved tuple for one stock is deployable in Paper or Live without a code edit, a qualification script or a restart. Other stocks keep the registry point.
- Research weakness stays visible on the qualified version: failed, sparse or reused final tests, and Manual override over Python-only evidence. An override never turns weak evidence into strong evidence.
- A strategy code change makes every qualified version of that program stale until it is re-proved. Paper and Live then refuse those tuples, which is the intended fail-closed behavior.
- Deleting (hiding) a study never removes its trials, exposures or qualification.
- Only EMA ships a Golden Search declaration. Other strategies are visibly unavailable until they declare their knobs.

## Not decided here

- **Deflated Sharpe as sign-off evidence.** The DSR implementation in `app/research/signal/diagnostics.py` has no reference fixture and the trial ledger's assumptions are not yet validated. The workbench shows it as unavailable.
- **Reserving a final interval that has not happened yet.** The final interval must be in the lake at lock. "Wait for fresh data" is a recorded decision that ends the study.
- **Listing bots affected by a revocation.** Seals live on clerk volumes the data plane cannot read. Revocation explains that the next Start of an affected deploy is refused.

## Anti-patterns rejected

- Seeding a walk-forward fold from the all-period winner or from today's newly approved tuple.
- Ranking by blended places across rounds, or calling coordinate convergence an optimum.
- Treating a null exam metric, an unreadable qualification table, or a Golden Validation record by itself as coverage.
- Moving `validated_settings` or `golden_trace_root` to make an approved tuple look covered.
- Deleting or editing an exposure, a trial, a qualified version or a default-history row.
