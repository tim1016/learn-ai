# Comment citations: Python app code (#2742)

Part of map #2700. Read at `origin/master` **`6a4d7d396108ef16471d8df888b9ded74d3c2892`**. This is a plan only; nothing is edited from this ticket.

**Question.** Which comments in `PythonDataService/app/` cite a doc other than an ADR? For each one, should the citation be dropped, re-pointed to an ADR in force, or does it show that an ADR is owed?

## How this was judged

- **Census.** Two searches over `PythonDataService/app/`:
  - `git grep -nE 'docs/[A-Za-z0-9_./-]+\.md'` found 252 lines.
  - `git grep -nE '[A-Za-z0-9_-]+\.md([^A-Za-z]|$)'` caught bare file names such as `open-pr-review-2026-08-05.md`, `CONTEXT.md` and `lean-sidecar-lab.md`.

  A scratch tokenizer classified every hit as a `#` comment, a docstring, a string literal or a non-Python file, and printed the surrounding comment block. Each row below was judged from that block. There is no `known-gaps §N` pattern in app code.
- **In scope.** Only comments and docstrings. String literals, JSON data and the markdown files that live under `app/` are listed separately under [Not comments](#not-comments-listed-not-judged), because editing them changes behavior, contracts or seals.
- **Verdicts.** These apply the owner's rulings ☆ *comments point to ADRs* and ☆ *code is the documentation*:
  - **drop.** The comment already states the fact, or the citation is a history, plan or status pointer, an index row, or a doc that no longer exists.
  - **re-point → ADR NNNN.** An ADR in force already records the decision. It was confirmed by reading or grepping that ADR, as noted on each row.
  - **ADR owed (X).** A strong decision that no ADR records: money-path safety, a numerical choice, a vendor constraint or a boundary. Rows of the same kind are grouped into one proposed ADR, listed under [ADR owed](#adr-owed-proposed-adrs).
  - **goes with dead code.** The citation sits in code that a dead-code list (#2701–#2710) already cuts. It is not judged.
- **Math provenance blocks.** The `Formula / Reference / Canonical implementation / Validated against` blocks follow the locked rule that math paperwork is a golden fixture with its attribution plus a tolerance-pinned test:
  - A `Reference:` or `Validated against:` line that names a reference note or a reconciliation report is a **drop** when a golden fixture's attribution, the manifest or a named test already carries that note.
  - It is **ADR owed** only when the note holds a decision nothing else records, such as a custody tolerance.
- **Volatility math (◇).** These rows were judged normally, and the code stays whatever the verdict is. That covers the IV solver, surface fitting, IV30 including the legacy path, IV basis conversion, BS vega, `iv30_health` and price normalization.
- **ADR citations.** These were checked only for pointing at an ADR that #2712 cuts or that is superseded. The 23 citations of ADR 0049 point at an ADR in force and get no rows. The cut-ADR hits are in [Citations of cut or superseded ADRs](#citations-of-cut-or-superseded-adrs).
- **Hashed rows.** Rows in files listed in `app/engine/strategy/program_sources.py` (the Signal Program build-proof sources) are marked **hashed**. Editing even a comment there changes the program source digest; see [hazard H1](#hazards-the-cutting-pr-must-carry).

## Summary

- **238 comment citations judged.**
  - 166 drop.
  - 24 re-point. The targets are ADRs 0022, 0031, 0035, 0042, 0053, 0056, 0057, 0059, 0060 and 0062, all in force and none on #2712's cut list.
  - 44 ADR owed, grouped into eight proposed ADRs (A–H). A ninth, I, is an amendment to ADR 0022 that came out of the rule-citation pass.
  - 4 go with dead code.
- **10 citations of cut or superseded ADRs.** 6 go with dead code (#2704) and 4 drop.
- **Most doc citations are pointers the code no longer needs.** Shipped PRDs, design specs, plans, audits and indexes make up most of the drops. About 20 point at docs that no longer exist.
- **The strong reasons cluster.** Custody tolerances, the LEAN sidecar's security and reproducibility boundary, research-run identity and verdict thresholds, and two Alpaca venue facts on the money path. None of these has an ADR today.

## Rows

### Alpaca broker and clerk

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `broker/alpaca/broker.py:209` | `docs/references/alpaca-extended-hours.md` | **drop** | The comment states the 04:00–20:00 ET session and its verification date, and already cites ADR 0059 D5.2. |
| `broker/alpaca/clerk/fleet_adapter.py:99` | `docs/design/fleet-b-route-inventory.md` | **drop** | ADR 0062 addendum item 4 is already cited; the per-operation dispositions are the declarations below the comment. |
| `broker/alpaca/clerk/live_envelope.py:77` | `docs/references/alpaca-live-envelope.md` | **ADR owed (A)** | Money-path margin: Alpaca documents no ordering between account cash and trade updates, so fills within 5 s stay reserved (#2441, measured #2487). ADR 0059's cash bound does not record the margin. |
| `broker/alpaca/clerk/recovery_reduction.py:38` | `docs/references/alpaca-extended-hours.md` | **ADR owed (A)** | Operator flatten outside the regular session (marketable limit, bps allowance; owner decisions on #2007) is a money-path rule ADR 0059 D5 does not record. |
| `broker/alpaca/clerk/sqlite/commands.py:19` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | A history pointer to a review finding. The docstring states why `lifecycle_run_id` is caller-supplied. |
| `broker/alpaca/clerk/sqlite/commands.py:287` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | Same as :19. The lost-response retry reasoning is stated in full. |
| `broker/alpaca/clerk/sqlite/decision_receipts.py:41` | `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | **re-point → ADR 0042** | ADR 0042 decides that every staged candidate gets a Clerk disposition and that DISCARD retains no broker effect. |
| `broker/alpaca/clerk/sqlite/economic_projection.py:18` | `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | **drop** | The docstring states the FIFO formula and names `fifo_pnl` as canonical; the shipped PRD's task number adds nothing. |
| `broker/alpaca/clerk/sqlite/execution_coverage.py:47` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | Custody tolerance for an exact slice replacing an aggregate recovery row. |
| `broker/alpaca/clerk/sqlite/facts.py:14` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **re-point → ADR 0035** | The typed-facts, "no untyped snapshot bag" rule belongs to the event-sourced authority ADR 0035, which links the pinned contract. |
| `broker/alpaca/clerk/sqlite/facts.py:487` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | An EXIT's quantity is the Clerk-proven remaining attributed quantity at cancel resolution, a custody invariant no ADR states. |
| `broker/alpaca/clerk/sqlite/facts.py:1112` | `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | **drop** | "Formula: n/a"; a pointer to a shipped PRD's task number. |
| `broker/alpaca/clerk/sqlite/facts.py:1383` | `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | **drop** | Same as :1112. |
| `broker/alpaca/clerk/sqlite/fee_evidence.py:505` | `docs/references/alpaca-fee-attribution.md` | **re-point → ADR 0059** | ADR 0059's fee-attribution amendment (#2542, PRD #2540) holds the decision and links the note. |
| `broker/alpaca/clerk/sqlite/folds.py:946` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | POSITION_QTY_EPSILON, the flat/drifted tolerance. |
| `broker/alpaca/clerk/sqlite/folds.py:958` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | Same tolerance (provenance `Reference:` line). |
| `broker/alpaca/clerk/sqlite/folds.py:1032` | `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | **drop** | The docstring states the formula; the shipped PRD's task number adds nothing. |
| `broker/alpaca/clerk/sqlite/folds.py:1126` | `docs/prds/2026-08-13-sqlite-clerk-manual-orders.md` | **drop** | The cited PRD no longer exists. The formula and tolerance are stated in the docstring. |
| `broker/alpaca/clerk/sqlite/folds.py:1245` | `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | **drop** | The docstring states the formula. This sits on the correction path #2701 may cut. |
| `broker/alpaca/clerk/sqlite/folds.py:1334` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | Cumulative-recovery delta pricing, a custody tolerance contract. |
| `broker/alpaca/clerk/sqlite/hashchain.py:6` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **re-point → ADR 0035** | ADR 0035 Decision 8 makes the log hash-chained; the docstring states the canonicalization itself. |
| `broker/alpaca/clerk/sqlite/idempotency.py:43` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason is stated (not-found, never a raw FK 500). |
| `broker/alpaca/clerk/sqlite/manual_order_completion.py:37` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | FILL_QTY_EPSILON 1e-9 (the reason is stated, but no ADR records this money-path tolerance). |
| `broker/alpaca/clerk/sqlite/mirror.py:133` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The fsync durability gap is stated. |
| `broker/alpaca/clerk/sqlite/mirror.py:156` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The one-verifier reason is stated. The R9 mirror fence is ADR 0035. |
| `broker/alpaca/clerk/sqlite/mirror.py:203` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason is stated. |
| `broker/alpaca/clerk/sqlite/mirror.py:217` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The PREPARE-before-FINALIZE reasoning is stated in full. |
| `broker/alpaca/clerk/sqlite/mirror.py:290` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason is stated. |
| `broker/alpaca/clerk/sqlite/order_evidence.py:103` | `docs/references/clerk-invariants.md` | **ADR owed (B)** | The one-cent-per-share EXECUTION_PRICE_CONFLICT threshold (#2460). |
| `broker/alpaca/clerk/sqlite/qualification_shadow_trace.py:5` | `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | **re-point → ADR 0042** | The shadow trace comparison without broker contact is part of the sealed-signal authority ADR 0042 (amended by 0059). |
| `broker/alpaca/clerk/sqlite/reads.py:1312` | `docs/prds/2026-08-13-sqlite-clerk-manual-orders.md` | **drop** | The cited PRD no longer exists. The formula is stated. |
| `broker/alpaca/clerk/sqlite/reconciliation_sweep.py:43` | `docs/references/alpaca-sqlite-clerk-lease-heartbeat-cadence.md` | **drop** | The comment states the whole 3x-cadence rationale, and a test pins it. |
| `broker/alpaca/clerk/sqlite/recovery_policy.py:918` | `docs/prds/alpaca-account-clerk-sqlite-control-plane.md` | **re-point → ADR 0035** | The shipped control-plane PRD was accepted as ADRs 0035/0037. The exact-close formula is stated in the docstring. |
| `broker/alpaca/clerk/sqlite/recovery_policy.py:919` | `docs/references/alpaca-sqlite-clerk-recovery-language.md` | **drop** | The note holds operator copy; the formula is stated and the test is named. |
| `broker/alpaca/clerk/sqlite/registry.py:65` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason is stated. |
| `broker/alpaca/clerk/sqlite/repository.py:5` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **re-point → ADR 0035** | The account-scoped db, fence and lease are ADR 0035 decisions; ADR 0035 links the pinned contract. |
| `broker/alpaca/clerk/sqlite/repository.py:14` | `docs/audits/open-pr-review-2026-08-05.md` | **drop** | A history narrative; the docstring already says what was deleted and why. |
| `broker/alpaca/clerk/sqlite/repository.py:15` | `docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md` | **drop** | Same as :14. |
| `broker/alpaca/clerk/sqlite/repository.py:1123` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason is stated. |
| `broker/alpaca/clerk/sqlite/schema.py:4` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **drop** | `test_schema_parity.py` names and enforces the doc's DDL block. The comment's rule ("edit both together") is the test's job. See hazard H4. |
| `broker/alpaca/clerk/sqlite/schema.py:58` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **drop** | Same as :4. |
| `broker/alpaca/clerk/sqlite/schema.py:793` | `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` | **drop** | Points at the doc's version history; the comment states the v4→v5 and v6→v7 rules itself. |
| `broker/alpaca/clerk/sqlite/writes.py:52` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason (PID reuse) is stated. |
| `broker/alpaca/clerk/sqlite/writes.py:68` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason (symlink escape) is stated. |
| `broker/alpaca/clerk/synthesized_orders.py:25` | `docs/references/synthetic-broker-position-projection.md` | **ADR owed (B)** | The average-cost convention for simulated (sim:/shadow:) positions, a money-path accounting choice no ADR records. |
| `broker/alpaca/marketable_limit.py:14` | `CONTEXT.md` *(bare name)* | **drop** | ADR 0059 D5.3 is cited in the same line; the glossary keeps the term. |
| `broker/alpaca/marketable_limit.py:16` | `docs/references/alpaca-extended-hours.md` | **drop** | ADR 0059 D5.3 is already cited, and the limit-only vendor rule is stated. |
| `broker/alpaca/profile/__init__.py:4` | `docs/architecture/broker-configuration-profile-contract.md` | **drop** | ADR 0060 is already cited. |
| `broker/alpaca/profile/errors.py:3` | `docs/architecture/broker-configuration-profile-contract.md` | **drop** | The docstring states the reason/message/next_step shape. The receiptLabel half is a CLAUDE.md hard rule, and no ADR in force holds the error shape. |
| `broker/alpaca/regulatory_fees.py:18` | `docs/references/alpaca-regulatory-fees.md` | **drop** | The outside sources (URLs) are named in the docstring, and the hashed FEE-001 attribution carries the note. |
| `broker/alpaca/regulatory_fees.py:58` | `docs/references/alpaca-regulatory-fees.md` | **drop** | Same as :18. |

### Fleet, IBKR, broker configuration, settings, migration, notices

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `broker/fleet/refusal_vocabulary.py:15` | `docs/superpowers/plans/2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md` | **drop** | The docstring restates "decision 9" in its next lines. This is the unlock #2711 flagged: it frees the nine-file plan family. |
| `broker/ibkr/__init__.py:5` | `docs/architecture/ibkr-integration-phase1.md` | **re-point → ADR 0062** | The cited doc no longer exists. The read-only IBKR data decision now lives in ADR 0062's retained market-data provider section (sacred feed). |
| `broker/ibkr/bar_models.py:7` | `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` | **drop** | The docstring states why the module was split. |
| `broker/ibkr/config.py:208` | `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` | **drop** | The docstring states why the helper lives here. |
| `broker/ibkr/models.py:3` | `docs/architecture/iv-ownership-research.md` | **re-point → ADR 0022** | The bullet that follows is the int64-ms rule (ADR 0022). The Greeks conventions are stated inline. |
| `broker_configuration/envelope.py:12` | `docs/architecture/alpaca-configuration-ownership-inventory.md` | **drop** | ADR 0060 Decision 6 is already cited, and the type-fidelity test is named. |
| `broker_configuration/errors.py:4` | `docs/architecture/broker-configuration-profile-contract.md` | **drop** | Same as `profile/errors.py:3`. |
| `broker_configuration/legacy_environment.py:8` | `docs/architecture/alpaca-configuration-ownership-inventory.md` | **re-point → ADR 0060** | ADR 0060 decides that the environment file stops being the source. The closed list itself is `RETIRED_SETTINGS` (code is the authority). |
| `broker_configuration/records.py:4` | `docs/architecture/broker-configuration-profile-contract.md` | **drop** | The record shapes are the dataclasses themselves. |
| `config.py:216` | `docs/references/polygon-throttle.md` | **drop** | The comment states the plan facts and the default. |
| `config.py:264` | `docs/references/ibkr-history-resume-fill.md` | **re-point → ADR 0053** | ADR 0053 (feed continuity) cites this measurement and holds the decision. |

### Data lake

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `data_lake/factor_files.py:41` | `docs/references/lean-factor-file-dividend-pricing.md` | **ADR owed (D)** | A deliberate departure from LEAN's ToolBox dividend-factor input convention. |
| `data_lake/lean_writer.py:73` | `docs/references/lean-deci-cent-encoding.md` | **ADR owed (D)** | "Our own quantization decision" (half-up deci-cent encoding), which every lake writer goes through. |

### Engine: strategies, edge, results, engine tests

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `engine/__init__.py:6` | `docs/lean-engine-implementation-plan.md` | **drop** | The cited doc no longer exists. |
| `engine/edge/confidence.py:4` | `docs/architecture/iv-ownership-research.md` | **drop** | An unvalidated research heuristic ("Validated against: NONE"), and the formula is stated. Check #2706: the edge routes may make this dead. |
| `engine/edge/confidence.py:8` | `docs/architecture/iv-ownership-research.md` | **drop** | The rationale is named inline. Weak. |
| `engine/edge/confidence.py:40` | `docs/architecture/iv-ownership-research.md` | **drop** | Points at a section about planned calibration. |
| `engine/edge/cross_asset_runner.py:12` | `three_strategies_roadmap.md` *(bare name)* | **drop** | Not a tracked file anywhere in the repo. |
| `engine/edge/edge_score.py:4` | `docs/architecture/edge-feature-design.md` | **drop** | The formula is stated; it is an unvalidated research score. |
| `engine/edge/edge_score.py:8` | `docs/architecture/edge-feature-design.md` | **drop** | Same as :4. |
| `engine/edge/labels_oracle/forward_rv.py:15` | `edge-feature-design.md` *(bare name)* | **drop** | A UI note. |
| `engine/edge/period_splitter.py:4` | `docs/architecture/edge-feature-design.md` | **drop** | The formula is stated. |
| `engine/edge/period_splitter.py:8` | `docs/architecture/edge-feature-design.md` | **drop** | The three modes are listed inline. |
| `engine/edge/portfolio_aggregator.py:3` | `docs/architecture/edge-feature-design.md` | **drop** | Both composites are stated inline. |
| `engine/edge/spread_model.py:14` | `docs/architecture/edge-feature-design.md` | **drop** | The paper (Madhavan & Smidt 1991) is cited. The functions are dead per #2704, the module is not. |
| `engine/edge/vrp.py:56` | `docs/references/iv-rv-basis-alignment.md` | **drop** | ◇ volatility math, kept. The bias size is stated, and the RV-003 attribution carries the note. |
| `engine/engine.py:8` | `docs/lean-engine-implementation-plan.md` | **drop** | The cited doc no longer exists; the bit-exact claim is the tests' job. |
| `engine/execution/sizing.py:29` | `docs/references/lean-set-holdings.md` | **drop** | The LEAN source and the golden fixture (atol=0) are named in the same block. |
| `engine/live/indicator_state.py:10` | `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md` | **goes with dead code** · **hashed** | The whole file is cut by #2704 (also hashed). |
| `engine/live/reconcile.py:4` | `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` | **goes with dead code** | The whole file is cut by #2704. |
| `engine/results/equity_downsample.py:6` | `docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md` | **drop** | A display-only policy, and the formula is stated. This is the single-comment keep #2711 flagged. |
| `engine/results/equity_downsample.py:70` | `docs/references/realized-equity-staircase-v1.md` | **drop** | The formula is stated; the golden fixture (manifest) cites the note and the test is named. |
| `engine/results/equity_downsample.py:133` | `docs/prds/strategy-lab-results-experience.md` | **drop** | "Formula: none", a transport envelope. |
| `engine/results/lean_statistics.py:10` | `spy-lean-output/{verify.py`, `source-map.md}` *(bare name)* | **drop** | Name LEAN's `PortfolioStatistics.cs` and the `lean-statistics-oracle-v1` golden fixture instead. This line is `docs/spy-lean-output/`'s only keep (#2711). |
| `engine/strategy/algorithms/deployment_validation.py:18` | `docs/references/deployment-validation-consecutive-green.md` | **drop** · **hashed** | The golden session-window attribution and the registry's provenance string carry the #1672 note. |
| `engine/strategy/algorithms/ema_crossover_signal.py:6` | `docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md` | **drop** · **hashed** | A receipt pointer. The manifest's reconciliation_ref and the registry's validated_against carry it. |
| `engine/strategy/algorithms/sma_crossover.py:224` | `docs/references/sma-crossover-signal.md` | **drop** · **hashed** | #2714 cuts the note and moves its #1736 argument into this comment. Same edit. |
| `engine/strategy/algorithms/spy_vwap_reversion.py:18` | `docs/references/spy-vwap-reversion-port.md` | **goes with dead code** | Cut by #2704, gated on #2706's routes. |
| `engine/strategy/normalized_gap.py:4` | `docs/math-sources-of-truth.md` | **drop** · **hashed** | An index row; the formula and the golden fixture are named. |
| `engine/strategy/signal_program.py:6` | `docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md` | **drop** · **hashed** | The hashing formula is stated; the EMA receipt is not evidence for it. |
| `engine/strategy/spec/__init__.py:15` | `docs/math-sources-of-truth.md` | **drop** | The canonical-vs-secondary decision is stated, and the parity tests enforce it. |
| `engine/strategy/spec/primitives.py:86` | `docs/math-sources-of-truth.md` | **drop** | An index row; the canonical file is named. |
| `engine/strategy/spec/tests/test_spec_sma_parity.py:15` | `docs/math-sources-of-truth.md` | **drop** | The test docstring states which twin is canonical. |
| `engine/tests/fixtures/golden/adx_14/regenerate.py:13` | `docs/references/adx.md` | **drop** | #2714 cuts the note. Regeneration justification belongs in the commit message (numerical-rigor rule). |
| `engine/tests/test_adx.py:241` | `docs/references/adx.md` | **drop** | Same as `regenerate.py:13`. |
| `engine/tests/test_spy_validation.py:37` | `docs/handoffs/2026-06-09-lean-sidecar-applehv-sigill-and-parity-gates.md` | **drop** | The cited doc no longer exists; the docstring states the history. |

### LEAN sidecar

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `lean_sidecar/__init__.py:3` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The package's "Authority:" line. The LEAN sidecar architecture has no ADR. |
| `lean_sidecar/config.py:5` | `docs/architecture/lean-sidecar-lab.md` | **drop** | "Echoed into" the doc: a doc that copies a code value. |
| `lean_sidecar/config.py:7` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The image-pin policy ("Runner choice"). |
| `lean_sidecar/config.py:51` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The image-pin policy. The comment states the arm64 choice but not the policy. |
| `lean_sidecar/config.py:177` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | Mandatory run limits and the container execution boundary. |
| `lean_sidecar/config.py:242` | `lean-sidecar-lab.md` *(bare name)* | **ADR owed (C)** | Per-request input ceilings. |
| `lean_sidecar/config.py:260` | `docs/handoffs/2026-05-18-design-p1-4-live-workspace-cap-v2.md` | **drop** | The cited doc no longer exists. The comment states why the interval is a constant. |
| `lean_sidecar/launcher/__init__.py:4` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | Launcher topology: a separate process as a privilege boundary. |
| `lean_sidecar/launcher/models.py:7` | `docs/architecture/lean-sidecar-lab.md` | **drop** | "Launcher shape" restates the models. |
| `lean_sidecar/launcher_client.py:5` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | Launcher topology (the data plane cannot escalate through the FastAPI handlers). |
| `lean_sidecar/lean_config.py:8` | `docs/architecture/lean-sidecar-lab.md` | **drop** | Config field names are confirmed by `test_lean_config.py`. |
| `lean_sidecar/manifest.py:3` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The reproducibility manifest: what invalidates reconciliation fixtures. |
| `lean_sidecar/normalized_parser.py:8` | `docs/architecture/lean-sidecar-lab.md` | **drop** | The docstring lists the "three rules" itself. |
| `lean_sidecar/parity_matrix/cell_runner.py:12` | `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | **ADR owed (C)** | Cross-engine cell tolerances and acceptance gates (a numerical choice). |
| `lean_sidecar/parity_matrix/manifest.py:6` | `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | **drop** | The schema is the code (`v1` model). |
| `lean_sidecar/parity_matrix/matrix.py:3` | `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | **drop** | A bare pointer. |
| `lean_sidecar/polygon_canonical.py:9` | `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md` | **drop** | The cited doc no longer exists. |
| `lean_sidecar/result_classifier.py:16` | `docs/architecture/lean-sidecar-lab.md` | **drop** | Points at a "Phase 1b progress" log. |
| `lean_sidecar/runner.py:6` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The container execution boundary (the only spawner of user source; every flag maps to a row). A security boundary. |
| `lean_sidecar/staging.py:9` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The workspace contract and LEAN data-folder fidelity. |
| `lean_sidecar/staging.py:363` | `lean-sidecar-lab.md` *(bare name)* | **ADR owed (C)** | Launcher topology: no podman inside the data plane. |
| `lean_sidecar/trading_calendar.py:3` | `docs/handoffs/2026-05-18-design-p2-5-date-semantics-v2.md` | **re-point → ADR 0022** · **hashed** | The cited doc no longer exists. "One calendar so they cannot drift" is ADR 0022's calendar authority. |
| `lean_sidecar/trusted_samples/ema_crossover.py:16` | `docs/references/fill-model-parity-spike-2026-05-19.md` | **drop** | The comment states the fill-model decision; #2714 keeps the note's §3 anyway. |
| `lean_sidecar/trusted_samples/rsi_mean_reversion.py:12` | `docs/references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md` | **drop** | A receipt pointer. The hashed ENG-009 attribution carries the report, so point Validated against at the ENG-009 test. |
| `lean_sidecar/workspace.py:13` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | The workspace contract. |
| `lean_sidecar/workspace_poller.py:32` | `docs/handoffs/2026-05-18-design-p1-4-live-workspace-cap-v2.md` | **drop** | The cited doc no longer exists; `config.py:250-258` states the rule. |

### Research

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `research/artifact/__init__.py:3` | `docs/architecture/research-artifact-seam.md` | **drop** | Internal module design, stated in the docstring. |
| `research/artifact/descriptor.py:3` | `docs/architecture/research-artifact-seam.md` | **drop** | Decisions 1, 5 and 6 are restated in the docstring. |
| `research/artifact/errors.py:9` | `docs/architecture/research-artifact-seam.md` | **drop** | The rationale is stated in the docstring. |
| `research/artifact/store.py:9` | `docs/architecture/research-artifact-seam.md` | **drop** | The mechanics are listed in the docstring. |
| `research/backtest_runs/engine_payload.py:50` | `docs/references/realized-equity-staircase-v1.md` | **drop** | The formula is stated; the golden fixture carries the note. |
| `research/baselines/__init__.py:4` | `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | **drop** | A feature-plan pointer. |
| `research/baselines/errors.py:6` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/baselines/storage.py:10` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/divergence/dashboard/build_dashboard.py:12` | `docs/tv-polygon-validation-gotchas.md` | **drop** | A bare pointer. The doc stays as a vendor-fact catalog. |
| `research/divergence/preflight.py:6` | `docs/tv-polygon-validation-gotchas.md` | **drop** | The `_GOTCHAS_DOC` string (`:98`) is the runtime link; the docstring pointer adds nothing. |
| `research/ml/artifact.py:19` | `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` | **ADR owed (F)** | "Predictions enter as a data artifact" is the v0.5 decision, a reproducibility boundary with no ADR. |
| `research/ml/artifact.py:20` | `docs/ml-predictions-authority.md` | **ADR owed (F)** | Same decision. The authority doc describes itself as a "snapshot, not a design document". |
| `research/ml/generators/quantconnect_fixture.py:22` | `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md` | **drop** | A status note ("gated on §B"). |
| `research/monte_carlo/__init__.py:4` | `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | **drop** | A feature-plan pointer. |
| `research/monte_carlo/errors.py:6` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/monte_carlo/storage.py:10` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/options/iv_builder.py:3` | `docs/math-rigor.md` | **drop** | States the known bias and the correct variance-time formula, and cites CBOE. |
| `research/options/iv_builder.py:4` | `docs/math-rigor.md` | **drop** | Same as :3. |
| `research/options/iv_builder.py:6` | `math-rigor.md` *(bare name)* | **drop** | Same as `:3`. |
| `research/parity/__init__.py:3` | `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` | **drop** | A bare pointer. |
| `research/parity/qc_reconciler.py:27` | `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` | **drop** | The taxonomy and its defaults live in `DivergenceCategory` and the numerical-rigor rule. |
| `research/parity/qc_reconciler.py:29` | `docs/references/reconciliations/qc-aapl-phase3.md` | **drop** | A receipt pointer; `test_qc_aapl_phase3_trade_parity.py` and the fixture carry it. |
| `research/parity/qc_reconciler.py:276` | `docs/references/qc-aapl-phase3-capture-runbook.md` | **drop** | The comment states the fail-fast contract. |
| `research/recency/fingerprint.py:12` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **ADR owed (G)** | D16: trade identity includes fill model, commissions and code revision, never params alone (scientific provenance). |
| `research/recency/repository.py:16` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **re-point → ADR 0057** | ADR 0057 Decision 5: "Redelivery is answered by identity". |
| `research/recency/runner.py:15` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **drop** | The structure is stated. |
| `research/recency/stats.py:15` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **drop** | The four formulas are stated. |
| `research/recency/stats.py:134` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **drop** | The formula is stated. |
| `research/recency/validation.py:14` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **drop** | The validation rule is stated. |
| `research/return_distribution.py:23` | `docs/math-sources-of-truth.md` | **drop** | An index row; the sources and the RD-001 fixture are named. |
| `research/runs/__init__.py:11` | `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | **drop** | A plan pointer. |
| `research/runs/__init__.py:12` | `docs/references/run-ledger.md` | **ADR owed (G)** | The hashing-scheme rationale: what a run's identity hash includes and excludes. |
| `research/runs/descriptor.py:30` | `docs/references/run-ledger.md` | **ADR owed (G)** | The same canonical-JSON SHA-256 run identity. |
| `research/runs/descriptor.py:59` | `docs/references/run-ledger.md` | **ADR owed (G)** | Same. |
| `research/runs/errors.py:7` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/runs/hashing.py:62` | `docs/references/run-ledger.md` | **ADR owed (G)** | `data_root_revision` fallback rules and hashing exclusions. |
| `research/runs/ledger.py:41` | `docs/references/run-ledger.md` | **ADR owed (G)** | `ENGINE_VERSION` is part of run identity: when it is bumped. |
| `research/runs/runner.py:229` | `docs/references/run-ledger.md` | **drop** | The formula is stated and the test is named. |
| `research/runs/storage.py:10` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated in the docstring. |
| `research/signal/diagnostics.py:4` | `docs/signal-engine-authority.md` | **drop** | The papers (Lo 2002; Bailey & López de Prado 2014) are cited in the same line. |
| `research/signal/diagnostics.py:22` | `docs/signal-engine-authority.md` | **drop** | A bare pointer. The doc is served in-app and stays on its own merit. |
| `research/signal/engine.py:4` | `docs/signal-engine-authority.md` | **drop** | "Orchestration only — no new arithmetic." |
| `research/signal/graduation.py:3` | `docs/signal-engine-authority.md` | **ADR owed (H)** | The Stage 0–3 graduation thresholds are a numerical choice. |
| `research/signal/graduation.py:4` | `docs/signal-engine-authority.md` | **ADR owed (H)** | Same as :3. |
| `research/signal/graduation.py:18` | `signal-engine-authority.md` *(bare name)* | **drop** | UI collapse behavior, described inline. |
| `research/signal/graduation.py:35` | `docs/signal-engine-authority.md` | **ADR owed (H)** | The Stage 0 threshold constants. |
| `research/signal/graduation.py:290` | `docs/signal-engine-authority.md` | **ADR owed (H)** | "Project defaults adopted from external methodology review (2026-04-30)". Tuning currently requires editing the doc. |
| `research/signal/walk_forward.py:31` | `signal-engine-authority.md` *(bare name)* | **ADR owed (H)** | The minimum fold count for the alpha-decay regression. |
| `research/sweep/grid.py:15` | `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | **ADR owed (G)** | `params_hash` as the cell identity shared by Recency, Grid Search and Walk-Forward (D11, D16). |
| `research/walk_forward/__init__.py:4` | `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | **drop** | A feature-plan pointer. |
| `research/walk_forward/__init__.py:16` | `docs/references/walk-forward.md` | **ADR owed (H)** | The split policy and the compounded-vs-rebased combined OOS curve are numerical choices ADR 0056 does not cover. |
| `research/walk_forward/errors.py:6` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/walk_forward/metrics.py:20` | `docs/references/walk-forward.md`, `docs/references/walk-forward-study.md` | **re-point → ADR 0056** | ADR 0056 Decision 5 freezes the median-retention verdict, and it cites `walk-forward-study.md`. |
| `research/walk_forward/runner.py:616` | `docs/references/walk-forward.md` | **drop** | The formula is stated. |
| `research/walk_forward/selection.py:67` | `docs/math-sources-of-truth.md` | **drop** | An index row; the formula is stated. |
| `research/walk_forward/storage.py:10` | `docs/architecture/research-artifact-seam.md` | **drop** | Stated. |
| `research/walk_forward_study/verdict.py:26` | `docs/references/walk-forward-study.md` | **re-point → ADR 0056** | ADR 0056 Decision 5: median, `ceil(S/2)` coverage floor, threshold 0.5. |

### Routers, schemas, models, `main.py`

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `main.py:1294` | `docs/architecture/lean-sidecar-lab.md` | **drop** | A phase narrative. |
| `main.py:1398` | `docs/process/autonomous-decisions.md` | **drop** | The comment states D-010 itself. This is the single-comment keep #2711 flagged. |
| `models/portfolio.py:3` | `docs/architecture/numerical-authority-migration-plan.md` | **re-point → ADR 0031** | ADR 0031 makes Python the authority for mathematical input/output. The "Phase 2" narrative goes. (`LiveGreeksRequest` dies with #2706.) |
| `models/requests.py:264` | `docs/options-companion-format.md` | **drop** | The docstring describes the slots and names the service. The format doc stays on its own merit (temporal rule). |
| `models/strategy.py:3` | `docs/architecture/numerical-authority-migration-plan.md` | **re-point → ADR 0031** | Same as `models/portfolio.py:3`. |
| `routers/alpaca_clerk_sqlite.py:11` | `open-pr-review-2026-08-05.md` *(bare name)* | **drop** | The reason (event-loop stall) is stated. |
| `routers/dataset.py:163` | `docs/tv-polygon-validation-gotchas.md` | **drop** | The vendor fact (adjusted=true means splits only) is stated. |
| `routers/edge.py:3` | `docs/architecture/edge-feature-design.md` | **drop** | An endpoint list; #2706 cuts most of these routes. |
| `routers/edge.py:375` | `docs/architecture/iv-ownership-research.md` | **drop** | A reviewer-feedback-log pointer. |
| `routers/edge.py:376` | `docs/architecture/iv-research-chat-notes.md` | **drop** | The cited doc was pruned (2026-09-12). |
| `routers/portfolio.py:3` | `docs/architecture/numerical-authority-migration-plan.md` | **re-point → ADR 0031** | ADR 0031 makes Python the math authority. The phase narrative goes. |
| `routers/research_runs.py:15` | `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | **drop** | A stale plan note ("Phase B will decide"). |
| `routers/run_replay.py:4` | `docs/audits/strategy-execution-research-directions-2026-08-24.md` | **goes with dead code** | #2706 cuts the whole router. |
| `schemas/broker_configuration.py:6` | `docs/architecture/broker-configuration-profile-contract.md` | **drop** | The shapes are the schemas themselves. |
| `schemas/signal_program_seal.py:228` | `docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md` | **drop** | The docstring states which level the 1e-9 tolerance applies at; the receipt stays (manifest). |

### Services

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `services/account_activity.py:161` | `CONTEXT.md` *(bare name)* | **drop** | The docstring states the prior-close formula (`:153-160`); the glossary keeps the term. |
| `services/account_activity.py:164` | `docs/references/broker-v2-fifo-pnl.md` | **drop** | The golden `broker-v2-fifo-pnl` fixture carries the note; `fifo_pnl` is canonical. |
| `services/account_activity.py:165` | `docs/references/alpaca-fee-attribution.md` | **re-point → ADR 0059** | The fee-attribution amendment (#2542). |
| `services/account_pnl_reconciliation.py:8` | `docs/prds/2026-08-12-broker-account-desk-lens-redesign.md` | **drop** | The C3 formula is stated in the docstring. This PRD's only keep in #2711. |
| `services/alpaca_fee_attribution.py:6` | `docs/references/alpaca-fee-attribution.md` | **re-point → ADR 0059** | The fee-attribution amendment holds the decision. Hamilton apportionment and the golden fixture are named. |
| `services/bot_trade_strategy_warmup.py:5` | `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | **re-point → ADR 0042** | Crash replay reapplying durable dispositions is ADR 0042's staged-candidate rule. The 1,000-line history goes. |
| `services/broker_v2_panel/catalog_projection_service.py:290` | `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` | **drop** | The day_pnl formula is stated. |
| `services/broker_v2_panel/catalog_projection_service.py:291` | `docs/references/broker-v2-fifo-pnl.md` | **drop** | The golden fixture carries the note. |
| `services/broker_v2_panel/gallery_hub.py:100` | `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` | **drop** | The formula is stated ("internal product contract"). |
| `services/bs_greeks.py:31` | `docs/references/options-bs-greeks-2026-04-24.md` | **drop** | ◇ (BS vega stays). #2714 cuts the note; the BS-005..007 attributions hold the unit conventions. |
| `services/bs_greeks.py:32` | `docs/architecture/options-math-authorities.md` | **drop** | A layout pointer. |
| `services/canary_admission.py:4` | `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | **re-point → ADR 0042** | The governed-paper canary for a sealed program sits under ADR 0042 (amended by 0059). |
| `services/clerk_transaction_projection.py:6` | `docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md` | **drop** | The docstring states the history. This is the single-comment keep #2711 flagged. |
| `services/decision_clock.py:132` | `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` | **re-point → ADR 0053** | ADR 0053 states the trigger rule `min(bucket_end, session_close)` and names `decision_clock.py`. |
| `services/dividend_service.py:10` | `iv_rv_alignment_plan.md` *(bare name)* | **drop** | A Claude memory file ("memory:"), not a repo doc. |
| `services/fred_service.py:4` | `docs/math-rigor.md` | **drop** | The FRED series and the interpolation convention are stated. |
| `services/indicator_warmup_policy.py:49` | `docs/references/data-lab-indicator-warmup.md` | **ADR owed (D)** | An accepted Data Lab warm-up tolerance (#2611): ~0.01 pt on oscillators, ~1e-4 relative on price scale. |
| `services/iv_recorder.py:8` | `docs/architecture/iv-ownership-research.md` | **ADR owed (E)** | §7.5: the .NET Quartz host owns the cron, not Python. §7.4: JSONL store until Postgres. A cross-stack boundary no ADR records. |
| `services/lean_sidecar_persistence.py:812` | `docs/references/reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md` | **drop** | The compatibility contract is stated ("both engines consume the same closed-trade observations"). |
| `services/lean_sidecar_service.py:13` | `docs/architecture/lean-sidecar-lab.md` | **ADR owed (C)** | Trusted sample only, no caller-supplied algorithm source. A security boundary. |
| `services/lean_sidecar_service.py:531` | `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` | **drop** | The cited doc no longer exists; the rule is stated in the docstring and the error. |
| `services/options_companion_service.py:7` | `docs/options-companion-format.md` | **drop** | A pointer to a format spec that stays on its own merit. |
| `services/options_companion_service.py:14` | `docs/math-sources-of-truth.md` | **drop** | "Parity pending" status; the registry tracks it. |
| `services/polygon_client.py:18` | `docs/references/polygon-throttle.md` | **drop** | A "layman explanation" pointer. |
| `services/portfolio_scenario.py:8` | `docs/architecture/numerical-authority-migration-plan.md` | **re-point → ADR 0031** | Same as `routers/portfolio.py:3`. |
| `services/portfolio_scenario.py:15` | `docs/architecture/options-math-authorities.md` | **drop** | The math authority (`bs_greeks.py`) is named. |
| `services/quantlib_pricer.py:5` | `docs/math-sources-of-truth.md` | **drop** | An index row; the parity test is named. |
| `services/rate_dividend_service.py:9` | `iv_rv_alignment_plan.md` *(bare name)* | **drop** | Same as `dividend_service.py:10`. |
| `services/run_replay_proof.py:3` | `docs/audits/strategy-execution-research-directions-2026-08-24.md` | **drop** | "Direction 2" narrative. The module is live (`bot_runner`, `run_gate`); ADR 0043 cites `run-replay-proof.md`. |
| `services/run_verdict_service.py:7` | `reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md` *(bare name)* | **ADR owed (H)** | The run verdict's fixed completeness contract: 17 sub-scores with fixed weights, no dynamic reweighting. |

### Volatility (◇: judged normally, code stays)

| `file:line` (under `PythonDataService/app/`) | Cited doc | Verdict | Reason |
|---|---|---|---|
| `volatility/basis.py:4` | `docs/references/iv-rv-basis-alignment.md` | **drop** | ◇. The formula and convention are stated; the RV-003 attribution carries the note. |
| `volatility/iv30_health.py:4` | `docs/math-rigor.md` | **drop** | ◇. "Weights and thresholds locked by tests". |
| `volatility/iv_provenance.py:4` | `docs/architecture/iv-ownership-research.md` | **drop** | ◇. A schema with no arithmetic; an unvalidated rationale pointer. |
| `volatility/iv_provenance.py:8` | `docs/architecture/iv-ownership-research.md` | **drop** | ◇. Same as :4. |
| `volatility/iv_provenance.py:42` | `iv-research-chat-notes.md` *(bare name)* | **drop** | ◇. The cited doc was pruned. |
| `volatility/price_normalization.py:3` | `docs/architecture/iv-ownership-research.md` | **drop** | ◇. The formula is stated. |
| `volatility/price_normalization.py:4` | `docs/architecture/iv-ownership-research.md` | **drop** | ◇. Same as :3. |
| `volatility/price_normalization.py:8` | `docs/architecture/iv-ownership-research.md` | **drop** | ◇. A rationale pointer for a stated formula. |
| `volatility/price_normalization.py:60` | `docs/architecture/iv-research-chat-notes.md` | **drop** | ◇. The cited doc was pruned; the docstring states the tiered-spread reason. |
| `volatility/solver.py:7` | `docs/math-sources-of-truth.md` | **drop** | ◇. An index row; the papers are named. |
| `volatility/solver.py:45` | `docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md` | **drop** | ◇. The comment states the 0DTE bug and its fix. |
| `volatility/surface.py:6` | `docs/math-rigor.md` | **drop** | ◇. Variance-time interpolation is named as the industry standard. |
| `volatility/vix_replication.py:204` | `docs/architecture/iv-research-chat-notes.md` | **drop** | ◇. The cited doc was pruned; the docstring states the 50% threshold reasoning. |

## Citations of cut or superseded ADRs

Searched with `git grep -nP` for ADRs 0003, 0005, 0006, 0007, 0009, 0010, 0013, 0016, 0017, 0019, 0024, 0025, 0028 and 0041, in both `ADR NNNN` and `adrs/00NN-` forms.

| `file:line` (under `PythonDataService/app/`) | Cited ADR | Verdict | Reason |
|---|---|---|---|
| `broker/ibkr/config.py:129` | ADR 0028 (Retired) | **drop** | It polls "the daemon's batched /instances snapshot", and the daemon is retired. #2702 judges whether the setting itself is dead. |
| `engine/live/intent_events.py:46` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts the module. |
| `engine/live/intent_events.py:67` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts the module. |
| `engine/live/intent_events.py:115` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts the module. |
| `engine/live/intent_ledger.py:37` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts the module. |
| `engine/live/intent_ledger.py:145` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts the module. |
| `engine/live/live_state_sidecar.py:93` | ADR 0009 (Superseded) | **goes with dead code** | #2704 cuts `LiveStateEnvelope` and the repo classes. Confirm at cut time that line 93 falls inside them; if it survives, drop the citation. |
| `installation_migration/contents.py:99` | ADR 0007 (cut by #2712) | **drop** | The comment already says the daemon is retired and the token is caught by shape. |
| `operator/notices/schema.py:53` | ADR 0024 (cut by #2712) | **drop** | A reserved code; the ADR's subject (`LivePortfolio`) is gone. |

`installation_migration/contents.py:131` also names ADR 0007, but inside a dict value (a string). It is listed under Not comments.

## ADR owed: proposed ADRs

Each proposed ADR records a decision the code already acts on. Once it lands, the rows it covers re-point to it, and the doc they cited can then be judged by the second docs pass. **Until the ADR exists, those rows keep their citation** (see hazard H5).

| ADR | Decision (one line) | Rows it covers | Evidence it absorbs |
|---|---|---|---|
| **A. Alpaca venue constraints on the money path.** Could be an amendment to ADR 0059. | The Clerk trusts Alpaca only as far as Alpaca documents. A fill recorded within 5 s before an account observation stays reserved, because cash and trade updates have no documented ordering (#2441, measured #2487). An operator flatten outside the regular session is a marketable limit leg with a bps allowance (owner decisions on #2007). | `broker/alpaca/clerk/live_envelope.py:77`, `broker/alpaca/clerk/recovery_reduction.py:38` | `docs/references/alpaca-live-envelope.md`, `alpaca-extended-hours.md` (outside facts, kept by #2714) |
| **B. Clerk custody tolerances and accounting conventions** | Quantity equality uses absolute `1e-9` (`FILL_QTY_EPSILON`), and "flat" uses `POSITION_QTY_EPSILON`. A cumulative recovery fill is priced on its delta. An EXIT's quantity is the Clerk-proven remaining attributed quantity. Average prices differing by under $0.01/share are vendor rounding; at $0.01 or more they are an `EXECUTION_PRICE_CONFLICT`. Simulated (`sim:`/`shadow:`) positions use average cost. | `sqlite/execution_coverage.py:47`, `sqlite/facts.py:487`, `sqlite/folds.py:946`, `:958`, `:1334`, `sqlite/manual_order_completion.py:37`, `sqlite/order_evidence.py:103`, `clerk/synthesized_orders.py:25` | `docs/references/clerk-invariants.md` §1–§3, `synthetic-broker-position-projection.md` |
| **C. The LEAN sidecar is a pinned, separately launched reference engine** | A separate launcher process owns Podman, so the data plane cannot escalate. Images are pinned by digest. Every run has mandatory limits, timeouts and input ceilings. Every container flag maps to a stated boundary. Only the trusted sample runs, never caller-supplied source. A workspace contract governs staging. A reproducibility manifest decides when reconciliation fixtures go stale. Cross-engine matrix cells have fixed tolerances and acceptance gates. | `lean_sidecar/__init__.py:3`, `config.py:7`, `:51`, `:177`, `:242`, `launcher/__init__.py:4`, `launcher_client.py:5`, `manifest.py:3`, `runner.py:6`, `staging.py:9`, `:363`, `workspace.py:13`, `parity_matrix/cell_runner.py:12`, `services/lean_sidecar_service.py:13` | `docs/architecture/lean-sidecar-lab.md` (no ADR covers it; ADRs 0049/0058 only touch the sidecar), `superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` |
| **D. Accepted departures from reference math** | Lake prices are quantized to deci-cents half-up, which is our choice, not LEAN's. Dividend factors take Polygon's raw `cash_amount`, not LEAN ToolBox's split-adjusted input. The Data Lab indicator warm-up is bounded at ~0.01 pt on oscillators and ~1e-4 relative on price scale (#2611). | `data_lake/lean_writer.py:73`, `data_lake/factor_files.py:41`, `services/indicator_warmup_policy.py:49` | `docs/references/lean-deci-cent-encoding.md`, `lean-factor-file-dividend-pricing.md`, `data-lab-indicator-warmup.md` |
| **E. IV recorder ownership** | The .NET host's Quartz scheduler owns the IV-recorder cron (Python computes), and samples go to a JSONL file store until a Postgres burn-in. This is the opt-in recorder the owner kept. | `services/iv_recorder.py:8` | `docs/architecture/iv-ownership-research.md` §7.4–7.5. `Backend/Configuration/IvRecorderOptions.cs:6` cites the same doc (Backend comment ticket). |
| **F. ML predictions enter the engine as sealed data** | Predictions are precomputed, content-hashed artifacts read by the spec `prediction` primitive. There is no live model inference inside a run. | `research/ml/artifact.py:19`, `:20` | `docs/ml-predictions-authority.md`, `superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` |
| **G. Research-run identity** | A run's identity is canonical-JSON SHA-256 over a fixed field set, with stated exclusions, `ENGINE_VERSION` and a `data_root_revision` fallback. `params_hash` is the shared cell identity for Recency, Grid Search and Walk-Forward. A trade's evidence fingerprint also includes fill model, commissions and code revision, never params alone (D16). | `research/runs/__init__.py:12`, `descriptor.py:30`, `:59`, `hashing.py:62`, `ledger.py:41`, `research/sweep/grid.py:15`, `research/recency/fingerprint.py:12` | `docs/references/run-ledger.md`, `superpowers/specs/2026-08-16-recency-chart-design.md` |
| **H. Research verdict rules** | Signal graduation uses Stage 0–3 thresholds adopted from the 2026-04-30 methodology review. The alpha-decay test needs a minimum number of folds. Walk-forward uses a fixed split policy and a compounded (not rebased) combined OOS curve. The run verdict is a fixed completeness contract: 17 sub-scores with fixed weights, no dynamic reweighting. ADR 0056 covers only the walk-forward *study* verdict. | `research/signal/graduation.py:3`, `:4`, `:35`, `:290`, `research/signal/walk_forward.py:31`, `research/walk_forward/__init__.py:16`, `services/run_verdict_service.py:7` | `docs/signal-engine-authority.md` §4–5, `references/walk-forward.md`, `references/reconciliations/engine-lab-runs-75-76-statistics-validation-plan.md` "Product decisions" |
| **I. Amend ADR 0022: accepted deviations from the int64-ms wire rule** | Polygon news `published_utc*` *filter* parameters stay vendor date strings, because whole-day semantics cannot be expressed in ms; returned values are still canonicalized. The owner's ruling that the Data Lab string path stays belongs in the same list. | `routers/news.py:14` (from the rule-citation pass below) | `.claude/rules/temporal-rigor.md` |

## Not comments (listed, not judged)

These lines name a doc, but they are not comments. Editing them changes runtime output, a contract or a seal, so they need their own decision.

| Location | What it is | Note |
|---|---|---|
| `data/strategy_validation_manifest.json:37`, `:118` | `reconciliation_ref` data | Never edit it. It is why #2714 keeps the EMA reconciliation forever. |
| `engine/strategy/registry.py:454`, `:1049` | `validated_against` / `reference` provenance strings | They may be sealed into program identity (`schemas/signal_program_seal.py:217` seals the provenance contract). Treat them as hashed (H2). |
| `models/requests.py:300`, `:350`, `:507` | Pydantic `Field(description=...)` | Published in the OpenAPI snapshot, so any edit regenerates the contract (H3). |
| `research/divergence/dashboard/build_dashboard.py:1293`, `:1444`, `:1447` | Generated-HTML text | `:1447` links the missing `docs/engine-tv-alignment-roadmap.md`. `:1444`–`:1450` is the trade-half footer #2705 cuts. |
| `research/divergence/preflight.py:98` | `_GOTCHAS_DOC` constant returned to callers | A runtime link that keeps `tv-polygon-validation-gotchas.md` read. |
| `research/documentation/analytical_metric_catalog.py:263` | Catalog data (`numerical_tolerance`) | Mirrors `contracts/strategy-lab/analytical-metric-catalog-v1.json`. |
| `routers/lean_sidecar.py:142` | `ValueError` text | Points users at a handoff that no longer exists. Re-word it to the ADR 0022 session-open anchor. |
| `lean_sidecar/launcher/app.py:103` | FastAPI `description` | The launcher's own OpenAPI. |
| `engine/edge/calibration/confidence.py:121` | `NotImplementedError` text | The module is dead (#2704). |
| `broker/alpaca/clerk/sqlite/schema.py:191` | SQL `--` comment inside `SCHEMA_DDL` | Byte-compared against the pinned-contracts doc (H4). Leave it. |
| `broker/alpaca/clerk/sqlite/schema.py:1077` | `load_pinned_ddl()` reads the doc's ```` ```sql ```` block | `test_schema_parity.py` depends on it. The doc cannot be cut while this exists (H4). |
| `installation_migration/contents.py:131` | Dict value "Retired host-daemon token (ADR 0007)" | Names an ADR #2712 cuts. Re-word it when that ADR goes. |
| `engine/live/README.md:30`; `research/options/README.md:162` | Markdown inside `app/` | #2713 cuts both files. |
| `engine/tests/fixtures/golden/macd_12_26_9/attribution.md:31`, `supertrend_10_3/attribution.md:28` | Golden attribution files (sacred paperwork) | They name `docs/references/macd.md` and `supertrend.md`, which #2714 cuts. Re-word them in the same commit ("justify in the commit message"). |
| `routers/dataset.py:668` | `_validation_report.md` | An output file name, not a doc. |

## Rule citations (separate pass, not row-judged)

110 comment and docstring lines cite a rule file rather than a doc: `.claude/rules/temporal-rigor.md` (40), `numerical-rigor.md` (42), `CLAUDE.md` (25), `AGENTS.md` (3) and `.claude/rules/python.md` (2). Rules are the coding authority #2715 is rewriting, not docs, so they got no rows. What the pass found:

- **temporal-rigor.md.** Almost every citation names the int64-ms or calendar rule as the reason. The decision record is ADR 0022, so they re-point there as a group, or simply drop where the comment only says "int64 ms UTC".
- **routers/news.py:14** records a *deliberate deviation* from the time rule. That is a decision, and it is ADR owed I above.
- **`CLAUDE.md guiding philosophy #5`** (single source of truth) is cited **by number** in about 20 comments: for example `marketable_limit.py:22`, `decision_clock.py:70`, `broker_configuration/runtime.py:16` and `legacy_environment.py:35`. #2715 must keep that numbering, or re-point them all in one pass (H8).
- **`AGENTS.md` "Python owns all math" / "#5"** (`recovery_reduction.py:835`, `research/recency/stats.py:19`, `schemas/alpaca_clerk_sqlite.py:520`) re-point to **ADR 0031**, which makes Python the math authority.
- **numerical-rigor.md.** These cite the taxonomy, tolerance defaults and the no-silent-catch rule. No ADR records numerical rigor; it is CLAUDE.md philosophy plus the rule. Whether a comment may cite a rule is a map question (below).

## Docs whose only code keep was a comment dropped here

Under ☆ a code comment never keeps a doc alive, but #2711 and #2712 kept several docs *only* because code cited them. Once these drops land, the second docs pass can judge them on content. This was not re-verified tree-wide (tests, Frontend and Backend can still cite them):

- **#2711 (one-off docs).**
  - `docs/audits/open-pr-review-2026-08-05.md` (all 14 comment citations drop; the DDL comment stays, see H4).
  - `docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md`.
  - The lane-E plan family (`refusal_vocabulary.py:15`).
  - `docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md`.
  - `docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md`.
  - `docs/process/autonomous-decisions.md`.
  - `docs/design/fleet-b-route-inventory.md`.
  - `docs/prds/2026-08-12-broker-account-desk-lens-redesign.md`.
  - `docs/prds/strategy-lab-results-experience.md`.
  - `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`.
  - `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`.
  - `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` (re-pointed to ADR 0053).
  - `docs/spy-lean-output/` (`lean_statistics.py:10`).
  - The sealed-signal PRD (re-pointed to ADR 0042; check its other live citations).
- **#2712 (architecture docs).**
  - `research-artifact-seam.md` (every citation drops).
  - `edge-feature-design.md` (every citation drops).
  - `build-alpha-style-features-1-8-research-spec.md` (the code citations drop; the `auto-research-tick` skill still names it).
  - `numerical-authority-migration-plan.md` (re-pointed to ADR 0031; protected-canonical, CLAUDE.md links it).
  - `options-math-authorities.md`.
  - `broker-configuration-profile-contract.md` and `alpaca-configuration-ownership-inventory.md` (re-pointed or dropped toward ADR 0060).
- **Still held by an ADR owed.** `lean-sidecar-lab.md` (C), `iv-ownership-research.md` (E), `ml-predictions-authority.md` (F), the recency-chart spec (G) and `signal-engine-authority.md` (H, also served in-app).

## Hazards the cutting PR must carry

- **H1 Hashed files.** Rows in `engine/strategy/algorithms/deployment_validation.py`, `ema_crossover_signal.py`, `sma_crossover.py`, `engine/strategy/normalized_gap.py`, `signal_program.py` and `lean_sidecar/trading_calendar.py` are Signal Program build-proof sources (`program_sources.py`). Editing a comment there changes every affected program's source digest and invalidates sealed qualifications.
  - Batch them into **one** PR with one re-qualification, together with #2704's hashed edits and #2714's move of the #1736 fact into `sma_crossover.py:219`.
  - `engine/live/indicator_state.py` is hashed too, but it goes with dead code.
- **H2 Registry provenance strings.** `registry.py:454` and `:1049` sit in `SignalProgramContract` provenance fields that `schemas/signal_program_seal.py` seals as program identity. Do not touch them in a comment-cleanup PR.
- **H3 OpenAPI.** `models/requests.py` `Field` descriptions are in the exported contract. They are not comments, so leave them, or regenerate the snapshot (serial merge).
- **H4 Pinned SQLite contract.** `alpaca-clerk-sqlite-pinned-contracts.md` is read by code (`schema.py::load_pinned_ddl`) and byte-compared by `test_schema_parity.py`.
  - The drops at `schema.py:4`, `:58` and `:793` change only Python comments outside `SCHEMA_DDL`.
  - `schema.py:191` is a SQL comment *inside* the DDL. Editing it means editing the doc in the same commit, and it may change `sqlite_master` text for new stores. Leave it.
  - The doc itself stays until the parity test is redesigned.
- **H5 ADR-owed rows wait for their ADR.** Drops and re-points can land at once. An ADR-owed row keeps its citation until its ADR is accepted; otherwise the only written reason is lost. Order: write ADRs A–I, re-point those rows, then the second docs pass judges the docs they cited.
- **H6 Docs link contract.** None of these comments is a link the docs checker follows, so dropping them breaks no CI. Deleting a *doc* still follows the #2711–#2714 hazards.
- **H7 Money path.** The clerk rows sit in money-path files. Every edit is to a comment or docstring only. Re-run the clerk SQLite and Alpaca suites anyway (a docstring is still code), and do not reflow code on the way through.
- **H8 CLAUDE.md numbering.** About 20 comments cite "guiding philosophy #5" by number (see Rule citations). Coordinate with #2715.
- **H9 Kill lists age.** Re-run the census at the cutting SHA. The scratch script was a tokenizer over `git grep`; any equivalent works.

## Pointers (outside this area)

- **Backend comment ticket.** `Backend/Configuration/IvRecorderOptions.cs:6` cites `iv-ownership-research.md`, the same decision as E.
- **Frontend comment ticket.** `Frontend/src/app/components/broker/v2-panel/gallery/lib/candle-renderer.ts:7` cites the bot-gallery spec, and #2708 cuts that file. `Frontend/src/app/services/past-chain.service.ts:6` cites `options-research.md`.
- **Tests.** `PythonDataService/tests/` was not in this area. `tests/structural/test_ibkr_feed_boundary.py:11` (sacred IBKR feed) cites the slice-0 spec, so that spec stays until a tests pass re-points it.
- **#2706 / #2704.** `engine/edge/confidence.py`, `edge_score.py`, `period_splitter.py` and `portfolio_aggregator.py` were judged as live. If the edge-route cuts leave them unreachable, their rows become "goes with dead code" (the verdict is drop either way).
- **#2705.** `build_dashboard.py:1447` is a user-facing link to a doc that no longer exists, inside the dead trade-half footer.

## Not reviewed

- **The 110 rule citations,** row by row. Only the group verdicts above were made; `news.py:14` was the one read in full.
- **ADR citations to ADRs in force.** These were out of scope, so the 23 ADR 0049 hits and other ADR mentions got no rows. Only the cut and superseded set was checked.
- **Re-point fit.** Each re-point was confirmed by grepping or reading the target ADR: 0042 (DISCARD disposition), 0035 (Decision 8 hash chain; links the pinned contract), 0059 (fee-attribution amendment; D5), 0053 (decision-clock trigger; cites the resume-fill note), 0056 (Decision 5 verdict), 0057 (Decision 5 redelivery), 0031 (Python math authority), 0060, 0022 and 0062. No ADR was read clause by clause.
- **Liveness of the live-judged edge modules,** pending #2706.
- **`PythonDataService/tests/`, `scripts/`, Frontend and Backend** comments.

## For the map

- **Rule question: may a comment cite a rule file?** About 110 comments cite `.claude/rules/*` or `CLAUDE.md` as their reason. ☆ *comments point to ADRs* reads as "not a rule". Options:
  - (a) Rules count, like ADRs. **Recommended**, because rules are the live coding authority.
  - (b) Only ADRs; re-point time-rule citations to ADR 0022 and leave the rest bare.
  - (c) Drop all rule citations.
- **Provenance blocks.** The `learn-ai-validation` skill requires a `Reference:` field. This list treats a `Reference:` line that names a fixture-backed note as a **drop**, re-pointed to the fixture or test. #2715's rule rewrite should say so, or the skill will keep re-adding note citations.
- **Nine ADRs owed (A–I).** Writing them is a handoff, not a cut. A, B and C sit on the money path or a security boundary and should come first.
