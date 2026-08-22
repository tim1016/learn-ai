# Known Gaps — Living Open-Defect Backlog

**Purpose.** One place that answers "what is still broken or deferred?" for an AI
agent or operator. This is the *only* durable home for open defects; the
point-in-time audit-finding files they came from (`docs/audits/auto-research/findings/`,
`docs/audits/vibe-coded-app-research/findings/`, `architecture-investigation-2026-07-02.md`,
and the auto-research run logs) were deleted on **2026-07-04** after their open
items were lifted here. The closed findings live in git history and in the
auto-research ledger (`docs/audits/auto-research/state.json`).

**Status convention.** Each item carries a severity and a code pointer captured
on the verification date named with its section — verify the `file:line` against
current code before acting, since the tree moves. When an item is fixed, delete
its bullet (git history is the record). When a new open defect is found, add it
here rather than starting a new finding-file tree.

**Scope note.** Safety-critical and broker items below were verified open against
current code on 2026-07-04. The architecture-investigation P1 tier and the
run-log functional items were **not** re-verified in that pass — confirm before
committing effort. The account-registry and architecture P1 clusters were
rechecked on **2026-08-17**. The IBKR B-05 order-cancel finding was closed by
deleting the complete order-actuation boundary on **2026-08-19**. The §1
safety-critical, §2 architecture-P1, and §7 contract-drift clusters were pruned
on **2026-08-19** after their items were verified closed or retired (see those
sections); §5 was re-verified still-open the same day, with its clock-constant
count corrected after #1687.

---

## 1. Safety-critical

No known-open safety-critical gaps. The execution-path fail-open,
temporal-authority/liveness, and non-numeric operator-verdict items previously
tracked here (issues #1655, #1671, #1672, #1674, #1677, #1664, #1665) are closed
and merged to master as of 2026-08-19; git history is the record.

## 2. Architecture-investigation P1 tier

No known-open items. The P0 safety issues and the carried-forward P1 tier from
`architecture-investigation-2026-07-02.md` (deleted to git history in the
2026-07-04 prune, commit `8441f4f6`) are all resolved or retired; git history is
the record — the account-truth enforcement/observation split and the
crashed-sibling `ACTIVE` liveness leak (commit `29388133`, with regression
tests), non-atomic ledger/parquet writes (commit `10433952` plus #1584), and the
committed dev-default control secret (#1652). The R3 recovery-daemon item was
retired with the deprecated IBKR bot-control surface (evaluator control plane
#1678, legacy broker control #1679; the accepted Alpaca Clerk cutover is
complete, ADR-0035).

## 3. Broker subsystem (re-verified 2026-08-19)

The B-05 direct-cancel risk is closed by deletion: no application route or
production helper can cancel an IBKR order. B-06 and B-09--B-13 remain fixed in
current code and their regressions pass. The disconnect-blindness cluster
(B-02/03/04/08) was reachability-reviewed on 2026-08-19 and is closed-by-fix
(commit `dce2b5d0`): all four sit on live runtime paths (the IBKR feed,
`/orders/stream`, `/pnl/stream`, and the always-registered `errorEvent`
handler), each fails closed on hard *or* soft (1100) disconnect, and each named
halt-on-disconnect regression test passes on master. No open broker gaps remain.

## 4. Broker session mirror — deferred product/safety decisions

Shipped read-only (ADR-0018, PRs #881–#908). Four items were intentionally not
built because they need a product/safety decision or authority the codebase does
not yet provide:

- **Exact 1:1 data-plane socket de-dup** — `/api/broker/health` publishes the
  data-plane `client_id`/account/host/port but not `local_port` or host PID, so
  the reconciler cannot join a health row to a specific `lsof` row without
  guessing. Needs a data-plane socket-identity contract.
- **Durable orphaned-socket incident lifecycle** — orphan notices are projected
  on live rows only, not persisted as acknowledgeable/resolvable incidents.
  Decide whether they enter the incident store and what resolves them.
- **Strong orphan attribution without PID/run-dir evidence** — a raw Gateway
  socket with no live PID and no run-dir stays `ghost`; may under-classify real
  orphaned bot sockets. Needs a durable session-level socket-identity history.
## 5. Numerical-rigor & frontend debt (deferred, P2)

- **Golden-fixture coverage gap** — most canonical math still lacks a registered
  golden fixture; the `iv30/` snapshot sits outside manifest governance.
  *(was F-0026; deferred in `auto-research/state.json`)*
- **Temporal wire/storage contracts outside Alpaca V2 remain non-numeric
  (medium).** The 2026-08-18 census confirmed 4 Pydantic, 29 C#, and 22 real
  TypeScript temporal field declarations using strings or native date types
  across golden fixtures, Data Lab, portfolio, market-data, validation, and
  research surfaces. The fourth Pydantic field is the active
  `EngineBacktestRequest.force_flat_at: datetime.time` boundary whose OpenAPI
  and generated TypeScript representation is a time string. Group migrations
  by one source contract at a time; do not create another cross-stack duplicate.
  The live Alpaca V2 wire/storage path is not in this cluster.
- **Frontend naive `new Date(string)` — Tier 2 (medium).** Eighteen production
  calls still parse date-only or local-wall strings across validation,
  option-expiry, Options Lab, Strategy Builder, ticker ranges, Data Lab, Past
  Chain, Indicator Report, Market Calendar, and Research Feature Report; five
  tests repeat the pattern. Migrate the owning wire fields to numeric ms and the
  shared display boundary rather than appending guessed offsets. *(was F-0034)*
- **Active non-live session structure still embeds clock constants (medium).**
  About twelve occurrences remain across canonical-calendar/LEAN adapters, Data
  Lab/chart coverage, data quality, and research features. Replace
  exchange/session assumptions with the canonical calendar; update
  reference/parity copies atomically with their canonical strategy. The
  deployment-validation parity copies formerly in this cluster were fixed by
  #1687 (calendar-derived cutoffs plus a golden fixture). The former evaluator
  run-ledger `force_flat_at` was deleted under ADR 0038 and is not a migration
  target.
- **`FailureRow.ts_ms` mislabel** — a host-local time string is typed/named as
  `ms-UTC`; rename to `ts_local` and convert at ingestion. *(was VCR-P3-K)*

## 6. Functional findings parked in deleted run logs (not re-verified)

- **Build-Alpha F6/F8 unimplemented** — features F6 (noise/robustness) and F8
  (parameter sensitivity) were never built. *(2026-05-07 build-alpha run. The
  sibling `exposure_pct` unit-mixing bug from this run — a strategy-bar numerator
  over a 1-min equity-curve denominator — was fixed in #160, commit `1f512213`,
  and is guarded by `test_exposure_uses_consolidated_bar_resolution`; pruned from
  this backlog 2026-08-19 after verifying the test passes on master.)*
- **ML-V-001** — Phase 3.0/3.5 canonical math not registered in
  `docs/math-sources-of-truth.md`. **ML-V-002** — provenance blocks missing on
  `research/parity/qc_reconciler.py` and the prediction-set `artifact.py`.
  *(2026-05-12 ML-predictions run)*

## 7. Contract-surface drift gates

No known-open items. The vocabulary-snapshot source-pinning, Broker V2 REST
generated-type, and accepted-ADR `Vocabulary:` metadata gates previously tracked
here (issues #1666, #1667, #1668) are closed and merged to master as of
2026-08-19; git history is the record.

## 8. Sealed Signal Program admission (verified 2026-08-21, issue #1728 / ADR 0043)

- **`CANDIDATE_UNCAPTURED_AT_CRASH` is not implemented.** The PRD's executive
  summary and FR-016 require replay to emit this named receipt when it finds a
  staged candidate that crashed before Clerk intake, applying `DISCARD` and
  creating no effect. No occurrence of `CANDIDATE_UNCAPTURED_AT_CRASH` exists
  anywhere under `PythonDataService/app`; the `DecisionOutcome` literal in
  `app/broker/alpaca/clerk/sqlite/decision_receipts.py` has no crash-window
  outcome. This is distinct from — and not covered by — the separate,
  already-merged Resume-after-crash work (PRD #1716, PRs #1717–#1720).
- **`ema_crossover_2_bps` and `spy_ema_crossover` have no build-proof identity
  of their own.** Both were left with `signal_program_contract=None` /
  `signal_program_factory=None` after the `dataclasses.replace()` identity-leak
  fix (see ADR 0043 §4) rather than being given their own qualification, so
  `prove_running_program_build` reports `NOT_APPLICABLE` and neither strategy's
  running bytes are checked against any golden corpus. Already tracked in-code
  and as issue #1730.
- **Retention sizing for open cycles longer than 30 trading days remains
  undecided** (PRD §27 item 3). `MAX_DECISION_RECEIPTS_PER_STRATEGY = 1_000`
  (`app/broker/alpaca/clerk/sqlite/decision_receipts.py`) bounds the per-
  strategy decision-receipt tail by row count, not by trading-day coverage;
  whether 1,000 rows safely covers every retained, non-`protected_*` receipt
  across a cycle longer than 30 trading days has not been demonstrated.
