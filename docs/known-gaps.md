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
count corrected after #1687. On **2026-08-24** the backlog was reconciled
against the F1–F19 adjudication table from the same-day session (PR #1747):
§1 now carries the two safety-critical findings, §9 lifts the remaining open
findings, and §8's FR-016 scope bullet was closed against current code.

---

## 1. Safety-critical

No known-open items. The two 2026-08-24 safety-critical findings — F18
(crash-held exposure had no path to flat) and F19 (retryable EXIT refusal
escalating to a bot crash) — were closed 2026-08-25 by the exposure-lifecycle-
closure work (PRD #1752, ADR 0045): the `execute_safe_flatten` recovery action
over run-fence-exempt recovery EXITs, the transient-vs-terminal refusal taxonomy
at the EXIT boundary and the runner call site, and the stuck-EXIT watchdog with
bounded episode-scoped redrives and durable `EXIT_STUCK` escalation. The three
stranded 1-share evidence positions on `PA3KWXU1C4C3` can now be flattened
through the presented action — flattening them remains the operator's call. The
previously tracked items (issues #1655, #1671, #1672, #1674, #1677, #1664,
#1665) remain closed as of 2026-08-19; git history is the record.

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

*The former first bullet — FR-016 crash-candidate capture "scoped to
`ema_crossover_signal` only" — was closed 2026-08-24: the #1730 promotion
registered `signal_program_factory` for all seven sealed programs (verified
against `app/engine/strategy/registry.py`), so `_warm_up_signal_strategy`'s
capture now covers every deployable strategy. The residual factory-less pair
is exactly the next bullet, and neither is live-deployable
(`supported_alpaca_paper_strategy_keys` derives from factory presence). Git
history has the old bullet.*

- **`ema_crossover_2_bps` and `spy_ema_crossover` have no build-proof identity
  of their own.** Both were left with `signal_program_contract=None` /
  `signal_program_factory=None` after the `dataclasses.replace()` identity-leak
  fix (see ADR 0043 §4) rather than being given their own qualification, so
  `prove_running_program_build` reports `NOT_APPLICABLE` and neither strategy's
  running bytes are checked against any golden corpus. Already tracked in-code
  and as issue #1730.
- **The external repository-writer census is a convention nudge, not a sound
  safety gate.** `app/broker/alpaca/clerk/sqlite/repository_boundary.py` plus
  `tests/broker/alpaca/clerk/sqlite/test_repository_writer_boundary.py` are
  widely read as proving "an unaudited SQLite Clerk writer is impossible". They
  do not. The AST visitor matches on *spelling*, not type: `_is_repository`
  recognises exactly two parameter names (`repo`, `repository`), one attribute
  (`self._repo`), and two facade patterns. An adversarial review on 2026-08-21
  fed synthetic source through the real visitor and confirmed five false-negative
  classes, none of them exotic — renaming the parameter to `db`, holding the
  handle as `self._store`, reaching it via `getattr`, dropping the underscore
  (`self.repo`), or calling a mutation method not yet listed in
  `REPOSITORY_MUTATION_METHODS`. Each returns `Detected calls: set()`, so a
  genuine unaudited custody mutation passes CI silently. The asymmetry is the
  danger: false positives fail loud (see `sqlite_panel_source.py`, which had to
  rename a plain local list off `receipts` to satisfy the matcher), while false
  negatives fail silent. Treat the census as enforcing the `repo`/`self._repo`
  convention, not as proof of writer exhaustiveness. A sound replacement needs a
  type-based walk or an explicit capability token minted only at the enumerated
  call sites. Pre-existing; not introduced or worsened by #1728.
- **Retention sizing for open cycles longer than 30 trading days remains
  undecided** (PRD §27 item 3). `MAX_DECISION_RECEIPTS_PER_STRATEGY = 1_000`
  (`app/broker/alpaca/clerk/sqlite/decision_receipts.py`) bounds the per-
  strategy decision-receipt tail by row count, not by trading-day coverage;
  whether 1,000 rows safely covers every retained, non-`protected_*` receipt
  across a cycle longer than 30 trading days has not been demonstrated.

## 9. 2026-08-24 session findings (PR #1747, reconciled 2026-08-24)

The independent-review handoff (`docs/audits/review-handoff-2026-08-24.md` §3)
carries the F1–F19 adjudication table; the ops study
(`docs/audits/bot-launch-ops-study-2026-08-24.md`) carries the detail and the
timings (the session scratchpad logs were ephemeral — the study doc is the
primary record). F1 and F9 were fixed in-session (`238821c7`, `ff5ed49f`);
F18/F19 were closed by ADR 0045 (see §1). The items below entered this backlog on
static code verification plus live observation during the session; severities
were assigned at lift time from that evidence. The handoff's independent
adjudication (confirm/refute, one issue per confirmed finding) may still
reclassify an item — if it refutes one, delete the bullet. F15 (action
idempotency lookup ordered after the presentation check) is
informational-only in the handoff and is deliberately **not** tracked here as
a defect.

- **F2 — `pause`/`continue` are dead vocabulary under SQLite custody
  (medium).** Guards and performers exist but can never fire:
  `app/services/broker_v2_panel/sqlite_panel_adapter.py:61`
  `SQLITE_PANEL_LIFECYCLE_ACTION_IDS = frozenset({"resume"})`.
- **F16 — `retire` never presented by the SQLite panel source (medium).**
  Same dead-vocabulary class, same pointer as F2 (study §7).
- **F17 — `prepare_safe_flatten` presents `enabled: true` but always 409s
  (medium).** The `mutation: false` fact exists server-side and is not
  reflected in presented enablement
  (`app/services/broker_v2_panel/sqlite_panel_source.py:839-842`; study §8).
  Executor work planned in
  `docs/superpowers/plans/2026-08-24-exposure-lifecycle-closure.md`.
- **F3 — two parallel stop surfaces ~60× apart in latency (medium).** Panel
  `stop_bot_decisions` ≈20 s (full panel re-projection inside `run_action`,
  `app/services/broker_v2_panel/panel_data_source.py`) vs legacy runner stop
  0.29 s, with different receipts (study §2, §5.1).
- **F4 — post-restart feed warmup presents as a fault (low).** ~45 s
  feed-readiness cold start refuses Resume with copy that reads like a
  failure (market-data gate, `app/services/run_admission.py:286-297`; study
  §3).
- **F5 — refusal ordering names the wrong gate first (low).** The first
  admission error can name a different problem than the one to fix; the
  full-ladder preview (`POST …/bots/admission`) exists and is unused for
  refusal shaping (gate order `app/services/run_admission.py:105-435`; study
  §3).
- **F6 — zero-bar engine run reports `success=True` (high).**
  `daily_sma_crossover` has no daily-bar fetch path and its engine run
  succeeded over zero bars (`execute_engine_backtest`,
  `app/routers/engine.py:1042`; ceremony doc §1 calls it a platform gap). An
  engine that cannot fail on empty input is an honesty defect.
- **F7 — QC-ID hard-required client-side though ignored for proof-less
  candidates (low).** Flag-form validation out of sync with the backend
  recording rule
  (`Frontend/src/app/components/strategy-validation/strategy-validation.component.ts`;
  ceremony §3).
- **F8 — human-flag toggle defaults to Reject (low).** Users have saved
  rejections by accident (same component as F7; ceremony §3).
- **F10 — dry-run deploys break atomicity on refusal (medium).** 500s on the
  reference topology (virtiofs bind mount), leaks orphan `sim:<sid>/` dirs
  with partially provisioned `source_bars.sqlite3`, raw error envelope
  instead of the typed refusal
  (`app/services/broker_v2_panel/paper_deploy_service.py` dry-run path;
  study §7 — "refused deploys must not leak state").
- **F11 — burst deploys flap "Market Data is unhealthy" (medium).** Admission
  couples to an instantaneous feed-age sample with no settling semantics,
  masking real refusals under load (market-data gate,
  `app/services/run_admission.py:286-297`; study §7/§9 proposes
  two-consecutive-samples settling).
- **F12 — LIVE chart pane serves stale bars unmarked (medium).** 7–17 min
  behind its own bot with `overlay_notices` empty — the staleness field
  exists in the contract and is unpopulated
  (`Frontend/src/app/components/broker/v2-panel/bot-triage-detail/bot-triage-detail.component.ts`;
  study §7; same defect class as the R4 tape fix recorded in
  `docs/superpowers/specs/2026-08-24-bots-triage-trader-lens-design.md` §10).
- **F13 — panel reads serialize globally (medium).** 56 ms alone → 2.6 s each
  at 10 concurrent; ~21 s/sweep projected at 80 bots (projection path,
  `app/services/broker_v2_panel/panel_data_source.py`; study §7/§9 proposes a
  fan-out budget).
- **F14 — `gallery/snapshot` unbounded by liveness (low).** 5.6 s / 751 KB /
  25 tiles including retired bots (`app/routers/broker_v2_gallery.py`; study
  §7).

Evidence-base corrections for whoever adjudicates: study §3 and handoff §3
disagree on F6–F9 numbering, which orphans the (already-repaired) pre-#1746
canary-ledger checkpoint item without an ID; F1's failed-attempt count is
reported as both 21/21 and 0/20; F12's lag window as both 7–12 and 7–17 min;
handoff §5 cites a judgment call that does not exist in
`judgment-calls-2026-08-24.md`; and handoff §6's `git log origin/master -8`
instruction cannot work after the squash-merge — the eight session commits are
individually reachable only via the `audit/unreviewed-findings-2026-08-24`
branch.
