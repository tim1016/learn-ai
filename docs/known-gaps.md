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
On **2026-08-25**, the 54-bot fleet stress run added the critical S15c
cancel-resolution gap to §1 and the remaining verified operational/UI gaps
to §10. On **2026-08-26** the day-two campaign served as the live-acceptance
pass for those fixes: §10 lost S9/S10, S3b, S7 and S17 and §9 lost F13, each
against an explicit acceptance result rather than a code reading; F3, F11 and
F17 were rewritten to state what actually changed and what residual remains;
and §11 lifted the new findings. Every open item in §11 carries an issue
number — the issue is the working brief, this file is the durable index. Source verification while lifting S15c corrected the supporting
audit's initial mechanism attribution: submit absence already has a bounded
terminal branch; the still-open loop was in EXIT cancel proof. That loop was
**closed the same day** (#1775): EXIT cancel proof now has a definitive-absence
branch, so the S15c bullet is deleted per the status convention above and its
account-wide amplifier — a separate defect — remains in §1 in its own right.

---

## 1. Safety-critical

**No known-open items.** The outstanding-intent admission gate — the last
entry here, and the amplifier that turned a single stuck EXIT into the
2026-08-25 fleet freeze — was scoped to the owning custody subject on
2026-08-26 (#1793). A bot is now refused only by its own unresolved intent;
the filter is total, because `effect_operations.subject_id` is NOT NULL with a
foreign key, so no account-wide remainder exists. Foreign broker orders never
reach that table and stay gated by the correctly account-scoped
`UNEXPLAINED_ORDER` hold.

The two 2026-08-24 safety-critical findings — F18
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
- **F16 — `retire`'s eligibility guard is narrower than the class it exists
  to clear (medium).** *Reframed 2026-08-26.* This was filed as dead
  vocabulary alongside F2, but `retire` **is** presented:
  `SQLITE_PANEL_LIFECYCLE_ACTION_IDS` is
  `frozenset({"resume", "retire"})` (`sqlite_panel_adapter.py:69`) and the
  projection tests confirm it renders, disabled, for a runnable strategy. The
  defect is the guard, not the adapter — do not follow F2's pointer here. The
  2026-08-26 run re-found it as T1 with a sharper root cause — the guard requires a
  dead *strategy key* while the zombie's dead thing is its *symbol*, so the
  panel simultaneously says "This bot can still run." and "Resume is
  blocked." **The contradiction was fixed 2026-08-26**: the blocker now states
  what the guard checks (the strategy *program* exists) and makes no claim
  about runnability. **The widening is still open and is now `needs-design`
  (#1795)** — it needs a durable read-safe proof of symbol validity, and none
  exists: no admission reason code is structurally permanent
  (`MARKET_DATA_STALE` is also what a *warming* symbol reports), and a broker
  security lookup is barred from the read path by #1776.
- **F17 — `prepare_safe_flatten` enablement vs. its view-action nature
  (low; reduced 2026-08-26).** The executor landed (#1756) and the POST path
  now raises a typed `ActionNotAvailableError` — "This recovery capability is
  a view action, not a broker mutation" — directing the operator to the
  presented navigation control (`sqlite_panel_source.py:800-804`). The
  2026-08-26 run accepted that behaviour (A9). **Residual, unverified:**
  whether presented enablement still reads `enabled: true` in a way an
  operator would read as "this will place orders". Re-verify against the
  presentation path before spending effort — this may already be correct, in
  which case delete the bullet.
- **F3 — two parallel stop surfaces with different receipts (low; latency
  half closed 2026-08-26).** The ~20 s panel figure came from a full panel
  re-projection inside `run_action`; #1776 removed it — `_run_action` now
  defers via `schedule_live_projection_refresh`
  (`app/routers/broker_v2_panel.py:516-530`). **Residual:** two surfaces still
  produce different receipts for the same operator intent, and the wire ids
  differ (panel `stop_bot_decisions`; the performer map's `stop` is not the
  wire id — 2026-08-26 ops lore §7). Re-measure the latency claim before
  citing it.
- **F4 — post-restart feed warmup presents as a fault (low).** ~45 s
  feed-readiness cold start refuses Resume with copy that reads like a
  failure (market-data gate, `app/services/run_admission.py:286-297`; study
  §3). Re-observed 2026-08-26 as O3/O5: 48/50 first-pass resumes refused with
  a bare `MARKET_DATA_STALE` and no warming prose, and the warm-up 409's
  `next_action` says "Restore both Clerk channels" when nothing is broken and
  it heals in ~60 s. The audit's recommendation is a copy fix, **not** an
  operator-driven warm-up button (§4 O3 argues a button reintroduces an
  unowned TTL-less subscription and puts a human back in the freshness loop
  WP2/WP4 removed).
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
- **F11 — burst deploys flap "Market Data is unhealthy" (low; largely closed
  2026-08-26).** The account-wide coupling is gone: #1783 made deploy health
  symbol-scoped and the 2026-08-26 run proved all four symbols warming in
  parallel with each symbol's verdict naming its own symbol, while the account
  kept accepting other symbols' deploys. #1784 added the hold debounce.
  **Residual:** the admission gate still reads an instantaneous feed-age
  sample (`app/services/run_admission.py:286-297`); no flap was observed after
  the fixes, so re-verify before treating this as open work.
- **F12 — LIVE chart pane serves stale bars unmarked (medium).** 7–17 min
  behind its own bot with `overlay_notices` empty — the staleness field
  exists in the contract and is unpopulated
  (`Frontend/src/app/components/broker/v2-panel/bot-triage-detail/bot-triage-detail.component.ts`;
  study §7; same defect class as the R4 tape fix recorded in
  `docs/superpowers/specs/2026-08-24-bots-triage-trader-lens-design.md` §10).
- **F13 — panel reads serialize globally (medium; concurrency unmeasured
  since #1776).** 56 ms alone → 2.6 s each at 10 concurrent; ~21 s/sweep
  projected at 80 bots (projection path,
  `app/services/broker_v2_panel/panel_data_source.py`; study §7/§9 proposes a
  fan-out budget). **Deleted and restored on 2026-08-26**: it was closed
  against A4, which proved 96 concurrent reads with zero errors and zero
  revision drift — that is read *purity*, not latency, and F13 is a latency
  claim. The same day's T2 measured panel p50 rising 0.67 s idle → 2.7 s under
  fleet load, which is consistent with this being *open*. Its concurrency
  condition is carried into **#1801**, which must remeasure the 10-concurrent
  case explicitly; delete this bullet then, not before.
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

## 10. 2026-08-25 fleet-stress findings (PR #1772, pruned 2026-08-26)

The live 54-bot campaign is recorded in
`docs/audits/bot-fleet-stress-2026-08-25.md`. S15c is safety-critical and lives
in §1; retire is tracked as F16 in §9 rather than duplicated here.

**Pruned 2026-08-26**, each against an explicit live acceptance in
`docs/audits/bot-fleet-stress-2026-08-26.md` §2 — the audit and git history
carry the detail, this index carries only the pointer:

- **S9/S10** stream-blip entry freeze → A6 (#1777/#1784).
- **S3b** crashed bots rendering innocent → T6 fix, live-verified (#1788/#1791).
- **S7** silent roster-poll freeze → #1788. Its follow-on is a *different*
  problem and is open as T2 in §11: that same 15 s timeout now trips on
  healthy-but-slow catalog reads under fleet load.
- **S17** flatten buried, wrong disposition → A11 (#1788).

§9's F13 was pruned in the same pass and has been **restored** — see there for
why its acceptance did not test it.

The items below remain open.

- **S12d — stopped fleet leaves a hot data plane and 105–145 s panel reads
  (high).** After mass stop, zero running bots still consumed 77% CPU until a
  data-plane restart. The responsible background loop is not yet identified;
  reproduce under profiling before choosing a fix. Worth profiling in the same
  session as T2/O4 (**#1801**) — both are unexplained per-account cost curves
  and may share a root.
- **S10 UI — active holds contradict roster counts and guidance (medium;
  partially cured).** An active stream-health hold can coexist with "Running 0,
  Stopped 0" and "no active hold" copy while dozens of bots are running. The
  worst half — an active account-wide `UNEXPLAINED_ORDER` hold rendering as
  literally "No hold", because `_hold_reason_code` mapped anything outside the
  closed vocabulary to `NO_HOLD` — was fixed in #1790, which also added the
  fail-closed `UNKNOWN_HOLD` code and the end-to-end DB→compat→card test whose
  absence let it survive. **Residual:** the roster count and guidance copy are
  still derived independently of the hold, so they can still disagree.

## 11. 2026-08-26 fleet-stress findings (PR #1791, lifted 2026-08-26)

The day-two 50-bot campaign is recorded in
`docs/audits/bot-fleet-stress-2026-08-26.md`. It was the live-acceptance pass
for the ten fixes out of the 2026-08-25 run: **all ten passed (A1–A13)**, and
the four §10 items they closed were pruned above. T6 was found, fixed,
regression-tested and live-verified inside the same session (#1791) and is
therefore not listed here. Every item below is filed as an issue — this section
is the durable index, the issue is the working brief.

- **T7 — a process freeze past the execution-lease TTL bricks the account
  handle (critical).** `podman pause` (SIGSTOP) for ~50 s let the lease expire
  while the process was frozen; on SIGCONT the ~24 still-running bots crashed,
  **their terminal STOP evidence could not commit**, and every subsequent panel
  action returned a raw 500 until a container restart. Fail-closed is correct —
  a holder that lost its lease must not write. Three gaps, split by tractability:
  the surface honesty fix (**#1794 — FIXED 2026-08-26**: the panel router now
  translates the condition to a typed `EXECUTION_LEASE_LOST` refusal with
  authored copy, instead of leaking the internal handle message as a raw 500;
  the clerk router had translated it since the SQLite cutover, the panel router
  simply had no handler) and the two design questions (**#1800**, open —
  supervised re-acquisition; where terminal evidence commits when its authority
  cannot be written). Real-world triggers are ordinary: laptop sleep, VM
  migration, CPU starvation.
- **T2/O4 — read and deploy latency degrade with running-fleet size (high).**
  At 144 rows with 50 trading bots: catalog p50 16.8 s / p95 20.6 s (idle
  baseline 3.3 s / 8.7 s); deploys 0.4 s median for the first ~30 and ~15 s
  (max 21.9 s) for 31–50. The frontend's 15 s poll timeout now trips on
  healthy-but-slow reads, surfacing "Account/Clerk refresh failed" during
  normal operation. Read purity held throughout — a cost curve, not drift.
  Suspected per-account lock contention between admission work and running
  bots' clerk operations, **unverified**. **#1801** — profile before tuning
  either the timeout or the read path.
- **T3 — same-symbol cohorts strand in lockstep on a stop wave (medium).**
  All four QQQ bots were caught mid-position by one cohort-targeted stop → 4×
  `RESUME_CARRYOVER_UNSUPPORTED` at once. Every piece is by design; the
  emergent effect is cohort-scale stranding whose only remedy is N×3 clicks.
  **#1802** (needs design) — a cohort-scoped flatten, the inverse-scoped
  sibling of the Two-Tap account-hold rule.
- **T1 — narrow retire misses its motivating case (medium, partially fixed).**
  The operator-visible contradiction is gone (2026-08-26); the widening is
  `needs-design`. See §9 F16 and **#1795**.
- **T5 — panel reads 503 under write pressure (medium).** An honest
  fail-closed torn-read guard, but one torn read ends the request, so it
  surfaces as flakiness exactly when an operator inspects an active bot.
  Load-correlated (benches saw 0/96). **FIXED 2026-08-26 (#1796)** — though not
  as the finding framed it: the bounded retry already existed (`range(3)`), but
  the attempts ran back-to-back and so all sampled the same write burst. They
  are now spaced, and exhaustion leaves a structured record instead of an
  indistinguishable 503. The guard itself is unchanged, and the attempt *count*
  was deliberately not raised — that is a magnitude, and #1801 owns it.
- **T4 — transient `RECOVERY_UNCERTAIN` during post-outage sweeps (low,
  observation).** A resume read can briefly return `RECOVERY_UNCERTAIN` before
  settling. Honest but unexplained. **#1806.** Note that two distinct branches
  return this state with different prose (`bot_start_admission.py:188-212`) —
  probe failure and outstanding intents. #1793 deliberately did not collapse
  them, and neither should the copy work.
- **Copy nit (low).** A "Crashed"-labelled row can carry the explanation "Off
  duty and flat." — a clerk-derived explanation beside a lifecycle-derived
  label. Cosmetic sibling of T6. **#1806.**
- **Open question for the operator (not a defect).** The roster-level "Live
  refresh failed. Showing the last successful fleet snapshot." banner is a
  third standing refresh message, not covered by the 2026-08-26 pill directive
  that converted the account strip's two banners into transient popovers.
  Whether it should match is a judgment call, not a bug — carried in **#1806**
  as a question to answer before any change.
