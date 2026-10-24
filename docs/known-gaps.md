# Known Gaps — Living Open-Defect Backlog

**Purpose.** One place that answers "what is still broken or deferred?" for an AI
agent or operator. This is the *only* durable home for open defects; the
point-in-time audit-finding files they came from (`docs/audits/auto-research/findings/`,
`docs/audits/vibe-coded-app-research/findings/`, `architecture-investigation-2026-07-02.md`,
and the auto-research run logs) were deleted on **2026-07-04** after their open
items were lifted here. The closed findings live in git history.

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
safety-critical cluster and the former architecture-P1 and contract-drift clusters
were pruned on **2026-08-19** after their items were verified closed or retired
(git history is the record); §5 was re-verified still-open the same day, with its clock-constant
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

## 5. Numerical-rigor & frontend debt (deferred, P2)

- **Golden-fixture coverage gap** — most canonical math still lacks a registered
  golden fixture. In the signal, validation, and options research subpackages,
  eight modules still declare `Validated against: NONE` as of 2026-10-23:
  `signal/{walk_forward,graduation,engine,regime,diagnostics}.py`,
  `validation/robustness.py`, and `options/{contract_finder,iv_builder}.py`.
  `signal/sharpe.py` has source-attributed IID, HAC, and DSR fixtures;
  the separate data-sufficiency heuristics remain unvalidated. `signal/backtest.py` has a hand-calculated
  internal-policy fixture for lag, costs, and drawdown, but no external
  engine parity. The remaining platform-defined selection, regime, and
  graduation policies need explicit hand-calculated fixtures instead of
  thematic paper citations. Behavior tests alone do not establish
  independent numerical equivalence. *(was F-0026)*
- **Sharpe inference scope** — the measured-grid DSR does not account for
  prior feature/research searches or correct serial dependence in overlapping
  forward returns. HAC assumes zero covariance across separated OOS segments
  and stable moments within the inference sample. Numerical equation fixtures
  do not establish finite-sample interval coverage or DSR calibration for this
  intraday setting. Graduation gates remain independent of these diagnostics.
- **Temporal wire/storage contracts outside Alpaca V2 remain non-numeric
  (medium).** The 2026-08-18 census confirmed 4 Pydantic, 29 C# (since removed
  with the C# backend, ADR 0081), and 22 real
  TypeScript temporal field declarations using strings or native date types
  across golden fixtures, Data Lab, portfolio, market-data, validation, and
  research surfaces. The fourth Pydantic field,
  `EngineBacktestRequest.force_flat_at: datetime.time`, was deleted with
  `session_entry_cutoff` under #2607, so three remain. Group migrations
  by one source contract at a time; do not create another cross-stack duplicate.
  The live Alpaca V2 wire/storage path is not in this cluster.
- **`TradeBar.volume` is annotated `int` but lake bars carry `Decimal`
  (P2, 2026-10-01).** Since the lake stores Polygon's fractional-share volume
  exactly, `app/engine/data/lean_format.py` builds engine bars with `Decimal`
  volume. `app/engine/data/trade_bar.py` keeps `volume: int` because it is in
  the sealed Signal Program set, and any byte change invalidates every
  program's qualification receipt. Nothing sealed does more than sum volume
  or `str()` it, and both are exact for `Decimal`. But a future
  `bar.volume * <float>` raises `TypeError` only on lake bars. Fix the
  annotation, to `Decimal | int`, at the next seal re-qualification.
- **Cross-sectional IV history chooses an expiry's strikes once per run, not once per date
  (P2, 2026-10-24).** `find_bracket_contracts` picks an expiry's ATM and 25Δ strikes at its
  first use in the run and reuses them until it leaves the pair: about 13 days on weekly
  chains, up to 38 on monthly-spaced ones. So a date's IV can depend on where the window
  starts (measured up to 1.9e-2 on a fully-listed weekly chain), and can differ from the
  pre-#3528 value on a date that code computed too, where a re-list moves an expiry's first
  use (measured up to 2.4e-2 ATM on irregular chains). The owner kept the reuse on
  2026-10-24. #3545
- **The live option-chain cross-check computes its gap and its tolerance band in Angular (medium,
  2026-10-24).** On an instrument's Options chain in Now · Live IBKR,
  `Frontend/src/app/markets/options/chain/live/live-cross-check.ts` computes
  `|Δ IBKR − Δ engine|` (`absDiff`) and judges it green, yellow or red
  (`deltaAbsBand`, edges 0.005 and 0.02, in `Frontend/src/app/components/broker/format.ts`).
  ADR 0068 decision 1 keeps every number a user compares, and every verdict, in Python;
  this has no canonical, no parity test and no named exception. It came over unchanged
  from the retired Chain (Live) page, which #2747 did not inventory. Fix: Python returns,
  per contract, the engine delta, the gap, the band and the reason a contract was not
  compared (in the chain stream or a batch pricing call), and Angular draws them. That
  also retires the `POST /api/quantlib/price` per contract that
  `live-engine-reads.service.ts` makes whenever a contract's spot, volatility or midpoint
  moves. #3533
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

## 7b. Orphans left by the 2026-08-28 signal/asset decoupling sweep

- **`EmaCrossover2BpsStrategyParametersModel` survives in the LEAN sidecar
  (low).** `app/routers/lean_sidecar.py` still defines and uses it (lines ~202,
  298, 413) for the `ema_crossover_2_bps` trusted sample, which remains a valid
  LEAN template even though the Python registry entry is gone. It is reachable
  API surface, not dead code, but the name now refers to a strategy this build
  does not register. Renaming it changes the committed OpenAPI contract, so it
  is left alone deliberately.

- **Doc paths predating this sweep (inherited, not introduced here).** A path
  audit of the math index (cut in #2750) and `engine-authority-map.md` found ~30
  cited `.py` files that no longer exist — almost all from the #1813 IBKR
  decommission (`app/engine/live/*`, `app/services/account_*`,
  `app/routers/broker_activity.py`, and neighbours), plus
  `tests/services/test_bot_runner.py`. Only the two this sweep invalidated
  (`spy_orb.py`, `spy_ema_crossover_options.py`) were corrected; the rest are
  pre-existing and out of scope.

## 8. Sealed Signal Program admission (issue #1728 / ADR 0043)
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
F18/F19 were closed by ADR 0045 (see §1); F6 was closed 2026-09-11 by #2034
(`6f174718` — empty-input runs now fail closed); F12 (the LIVE chart pane
serving stale bars unmarked) was closed 2026-09-24 by #2355 (PR #2401): the
pane's own IBKR line now reports its state (`ChartLiveResponse.feed`,
`ChartFeedView`: STALLED / ERRORED / RECOVERING with backend-authored copy),
which the bot panel's chart renders as a notice
(`Frontend/src/app/components/broker/v2-panel/dual-pane-chart/dual-pane-chart.component.html`),
and `overlay_notices` stays scoped to Polygon overlay failures. Its old anchor,
`bot-triage-detail.component.ts`, was deleted by PRD #2560. The items below entered this backlog on
static code verification plus live observation during the session; severities
were assigned at lift time from that evidence. The handoff's independent
adjudication (confirm/refute, one issue per confirmed finding) may still
reclassify an item — if it refutes one, delete the bullet. F15 (action
idempotency lookup ordered after the presentation check) is
informational-only in the handoff and is deliberately **not** tracked here as
a defect.

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
  produce different receipts for the same operator intent. The wire-id split
  is gone: the performer map's generic `stop` was removed (#2605), so the
  panel's one stop is `stop_bot_decisions`, which Home and the bot page both
  send. Re-measure the latency claim before citing it.
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
- **F7 — QC-ID hard-required client-side though ignored for proof-less
  candidates (low).** Flag-form validation out of sync with the backend
  recording rule
  (`Frontend/src/app/signal-strategies/proof/proof-actions.component.ts`;
  ceremony §3).
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
  **Offline remeasure 2026-08-31**
  (`docs/audits/read-latency-profile-2026-08-31.md`, reproducible via
  `scripts/bench_panel_read_latency.py`): serialization **confirmed with a
  mechanism** — per read, the row work splits into an event-loop-blocking
  roster-building slice and a GIL-holding `to_thread` projection slice, so
  10 concurrent reads serialize on both (catalog 44 ms alone → ~1.6 s each
  at 10 concurrent, 144 rows, with ~3.8× total-work inflation; GC ruled
  out).
  **Live remeasure 2026-08-31** on the deployed topology
  (§3 of the 2026-08-31 live read-latency profile, in Git history): the stated
  deletion condition is now **satisfied — and it confirmed the defect
  rather than clearing it**, so the bullet is rewritten, not deleted. Ten
  concurrent catalog GETs against the live data plane at 52 rows with 50
  bots trading: **7 682 ms each, wall 7 684 ms** — every request takes the
  whole round, with **4.8×** total-work inflation, against 161 ms for the
  same read alone. At the audit's full 144-row / 50-trading shape, measured
  with the audit's own 6-thread methodology, catalog p50 was **8.78 s**
  (§6). Serialization is real, live, and unfixed. Delete this bullet when
  the serialization is *fixed* — the lever and its ordering are in that
  audit's §11 — not on any further measurement.
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

The live 54-bot campaign's audit is in Git history. S15c is safety-critical and lives
in §1.

**Pruned 2026-08-26**, each against an explicit live acceptance in §2 of
the 2026-08-26 fleet stress audit — git history carries the audit and the
detail, this index carries only the pointer:

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
  (high; did NOT reproduce 2026-08-31).** After mass stop, zero running bots
  still consumed 77% CPU until a data-plane restart. **Remeasured under
  #1801** at 144 rows with zero running bots
  (§10 of the 2026-08-31 live read-latency profile, in Git history): CPU held flat
  at **24–25% of one core** across three minutes with no upward drift, panel
  storm p50 was **0.77 s** and sequential catalog p50 **267 ms** (unchanged
  at 270 ms after further settling), and no restart was needed. Neither
  symptom reproduces. This is a **non-reproduction, not a fix** — the
  original observation was not attributed. One condition differs: the
  2026-08-26 run had IB Gateway live. IBKR remains the read-only market-data
  provider for Alpaca bots under ADR 0062; #1813 retired IBKR bot control,
  not this feed. The auto-reconnect monitor polls at 3 s and is gated on
  `ibkr_settings.broker_enabled`, so a different feed state is a plausible
  but unproven cause.
  Delete this bullet if a second stopped-fleet run also comes back clean.
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

The day-two 50-bot campaign's audit is in Git history. It was the live-acceptance pass
for the ten fixes out of the 2026-08-25 run: **all ten passed (A1–A13)**, and
the four §10 items they closed were pruned above. T6 was found, fixed,
regression-tested and live-verified inside the same session (#1791) and is
therefore not listed here. T7 was likewise closed — #1794's surface-honesty
slice shipped 2026-08-26, and ADR 0050 answers both #1800 design questions
(supervised revival fenced at the store, #1906; the panel-action revival
path, #1959) — and its bullet is deleted per the status convention. Every
item below is filed as an issue — this section
is the durable index, the issue is the working brief.

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
- **T5 — panel reads 503 under write pressure (medium).** An honest
  fail-closed torn-read guard, but one torn read ends the request, so it
  surfaces as flakiness exactly when an operator inspects an active bot.
  Load-correlated (benches saw 0/96). **FIXED 2026-08-26 (#1796)** — though not
  as the finding framed it: the bounded retry already existed (`range(3)`), but
  the attempts ran back-to-back and so all sampled the same write burst. They
  are now spaced, and exhaustion leaves a structured record instead of an
  indistinguishable 503. The guard itself is unchanged, and the attempt *count*
  was deliberately not raised — that is a magnitude, and #1801 owns it.
- **Copy nit (low).** A "Crashed"-labelled row could carry the explanation
  "Off duty and flat." — a clerk-derived explanation beside a
  lifecycle-derived label. Cosmetic sibling of T6. **FIXED 2026-08-26
  (#1806)** — though the cause was not cosmetic: two authors answered the
  same question, and the second overwrote the first. `status_explanation_for`
  already handled the unclean-exit case; `_sqlite_catalog_explanation`
  re-authored the explanation without it and clobbered the correct answer via
  `model_copy`. The adapter now defers to the incoming explanation for both
  unclean-exit labels, reading them from the shared vocabulary so the two
  cannot drift.
- **Roster staleness banner (low).** The roster-level "Live refresh failed"
  banner stood permanently and was not covered by the account strip's pill
  directive. **DECIDED + FIXED 2026-08-26 (#1806 item 3).** Operator decision:
  it stays a standing banner rather than becoming a pill, because it is a
  *state* claim about the data on screen, not a failure *event* — 144 rows
  cannot show their own staleness the way the strip's few values can, and a
  pill fires only on a false→true edge, so a persistently failing refresh
  shows nothing. It is now gated on the snapshot's real age (6× the poll
  cadence, deliberately above the #1801 latency curve so a slow-but-landing
  read is not reported as a failure) and quantifies it via the shared
  `fmtElapsedSince`.
- **Open question for the operator (not a defect).** The roster-level "Live
  refresh failed. Showing the last successful fleet snapshot." banner is a
  third standing refresh message, not covered by the 2026-08-26 pill directive
  that converted the account strip's two banners into transient popovers.
  Whether it should match is a judgment call, not a bug — carried in **#1806**
  as a question to answer before any change.

## 12. Alpaca Paper / Live workflow (verified 2026-09-09)

The 2026-09-09 Paper / Live workflow audit is historical and in Git history;
current budget, risk-edit and mode behavior is recorded in ADRs 0059 and 0060.

- **Option TAF and multi-contract OCC rest on one-contract Paper fills (P3, #3306).**
  Alpaca Paper posts OPT TAF through FINRA's pause (SR-FINRA-2026-021), and
  `PythonDataService/app/broker/alpaca/option_fees.py` charges what it posts:
  $0.003 a sold contract from 2026-10-01 through 2026-12-31, summed over the day
  and rounded up (owner decision, 2026-10-22). The four recorded days
  (2026-10-13 to 10-16, golden FEE-003) bound the rate without fixing it; the
  pin is the top of its range. Not observed: pause days before
  2026-10-13, what Alpaca charges after 2026-12-31 (the model keeps FINRA's
  resumed $0.00329), a day that also traded stock, and a Live account. OCC is
  rounded up per execution, as Paper posts it (every recorded row, 50 distinct,
  is $0.03 for a one-contract fill); how Alpaca rounds a fill of several
  contracts is not observed. Check the first January fee rows and the first
  multi-contract option fill against the model.


## 13. Data Lab workspace redesign residuals (2026-09-12)

Tracked open items left by the Data Lab redesign PR
(its PRD is in Git history). Implemented:
child routes Explore/Export/Validate (Explore is now the instrument place's
Chart tab and Export its Build dataset page, Validate is Operations → Data
checks, ADR 0084; `/data-lab` is redirects only), workspace store, legacy URL
redirects,
searchable indicator picker, theme-token chart colors, `POST /api/dataset/plan`,
additive `DataLabSession` ms-UTC columns (dual-read).

- **Chart color tokens are not yet resolved at the Lightweight Charts boundary
  (medium).** Token IDs are stored and validated, but `data-lab-chart` still
  consumes literal colors when rendering; token→`var(--chart-series-*)`
  resolution at the chart boundary is the remaining §10 step.
- **Saved-session numeric-window semantics are interim (low).** The Data Lab
  sessions route (`app/schemas/data_lab_sessions.py`) derives
  `window_start_ms`/`window_end_ms` from date-only UTC midnight until the
  canonical exchange-calendar resolution (PRD §13 step 6 backfill) lands.
- **§13 steps 3–5 are not complete: legacy `DateTime` columns remain dual-written
  (low).** Save/update (`app/services/data_lab_session_service.py`) still set
  `CreatedAt`/`UpdatedAt` alongside the numeric ms-UTC columns, and
  `FromDate`/`ToDate` stay the stored date-picker strings (ADR 0022 (f)). The
  GraphQL projection this entry used to name is gone (ADR 0081); the REST
  response reads `created_at_ms`/`updated_at_ms`. The numeric-only write flip
  lands with the §13 step 5/6 migration, now a numbered SQL file.
- **Legacy indicator recipe/param query keys are named as ignored, not
  decoded (low).** PRD §14 routes legacy recipe URLs through a bounded
  recipe-schema validator; until that validator exists the legacy redirect
  carries scope keys to the instrument place and leaves recipe keys in the
  address, where the place names them in its "Migrated link" notice, so FR-014
  is partially met for recipe-carrying bookmarks.
- **Plan↔ZIP column parity is structural (shared function), not
  artifact-tested (low).** The plan endpoint test pins the receipt to
  `project_output_columns` — the same function the CSV/metadata/ZIP paths
  call — but no test yet diffs a generated ZIP manifest itself against the
  receipt.
- **A legacy address that names only a saved setup ends at Markets (low).**
  `/data-lab?sessionId=<id>` with no `ticker` leads to the Markets list, which
  does not read the id: the setup's instrument is stored evidence, and a
  redirect cannot read it. With a `ticker` the instrument opens and the setup is
  opened under the instrument its record names. The old Data Lab never wrote a
  `sessionId` into an address, so only a hand-made link is affected; the setup
  opens from its instrument's Saved setups.
- **`computeAllIndicators` has no UI toggle in the Chart tab (low).** The
  signal defaults to false; wire a control if the legacy behavior is missed.
- **The Build dataset template still exceeds the ~80-line path rule (low).**
  The four-section form (~240 lines) remains a single template — same follow-up
  refactor class as the shell split.
- **Responsive overflow assertions need a browser pass (medium).** The §8
  structural rules are in place, but the 320–1920px `scrollWidth === clientWidth`
  checks and AXE scans have not been executed against a live browser.
- ~~**`active-indicator-card`/`active-indicator-group` are unmounted (low).**~~
  Resolved 2026-09-13, with a correction to the record: `active-indicator-card`
  was never dead — research-lab's `feature-runner` and `signal-runner` mount it,
  so it stays as a shared component (the modal's type imports from it are fine).
  `active-indicator-group` had no importers at all and is deleted.

## 13a. Data Lab quick ranges + auto-loading chart (2026-09-13)

Follow-up slice (same week as the redesign): calendar-resolved quick-range
chips (1D/5D/1M/3M/6M/1Y/2Y/5Y) resolved server-side by the canonical NYSE
calendar (`GET /api/chart/range-presets`), a lake-first chart ingest seam
(ADR 0049 amendment A6), the `HttpErrorResponse` error-gate fix that
re-arms the server-authored timeframe auto-correct, and numeric
`start_ms_utc`/`end_ms_utc` declared authoritative-over-ISO on
`ChartDataRequest`. Residuals:

- **PRD §14's explicit-refresh-only rule is superseded (decision, not a
  gap).** The instrument place's Chart tab has no Apply and no Refresh
  (PRD #3490 R05): it loads when the committed context changes, a repeated
  equal emission loads nothing, a request a newer one replaced is ended, and
  the one-shot timeframe auto-correct commits the recommendation to the
  address. A fetch that fails to load anything leaves the stale flag set —
  it clears only when replacement data settles.
- **Committed-window anchors are day-shaped, not session-shaped (interim,
  low).** The Data Lab commits `startMsUtc` = UTC midnight of the from-date
  and `endMsUtc` = the to-date's final UTC instant (23:59:59.999) — applied
  by every date-intent→ms boundary (quick ranges, From/To inputs, the
  instrument's address, saved-setup opening). The chart floors
  both to inclusive UTC trading dates; half-open instant readers (the news
  query, dataset numeric overrides) get a non-degenerate window covering the
  full end date. The canonical half-open session-open resolution (PRD §13
  step 6) remains the destination; the store still accepts an equal
  start/end pair (one trading date) restored from older workspaces, and
  rejects the uninitialized `{0,0}` sentinel.
- **Preset bar estimates are fetched for `rth` regardless of the workspace
  session (low).** The endpoint takes a validated `rth|extended` `session`
  parameter but the shell always requests `rth`; the estimates are
  informational only — no client decision reads them.
- **Preset end anchoring flips at the RTH open for both session arguments
  (low).** Before 09:30 ET on a trading day, presets end at the previous
  session (nothing exists for today yet); the calendar's session windows
  are RTH-shaped, so an `extended` preset does not flip at the 04:00 ET
  premarket open — premarket-only "today" is not yet a preset target.
- **Preset chip semantics are trading-session counts, not calendar anchors
  (low).** "6M" is the last 126 scheduled sessions, matching the repo's
  `SESSIONS_PER_BAR_UNIT` family. A calendar-month-anchored variant would need
  a product decision and a resolver change only — the client applies
  whatever dates Python resolves.

## Fleet account retirement — verified 2026-09-21

- **P2: an account released without lane quiet has no way to another lane.**
  A lane that cannot answer lane quiet — its process restarted during the
  drain (#2155 refuses its re-registration), its host is gone, or it has no
  clerk — retires only through `force-retire`, and its account is released
  `lane_confirmation: absent`. Every handover path refuses such an account
  (re-reservation and `reassign-assignment` both require a `present`
  release and a retired old lane; ADR 0063's #2154 consequence), and that
  refusal is deliberate: nothing proves the old lane stopped writing. What
  is missing is a recovery ceremony for the case where the operator *knows*
  the old lane is gone — re-onboarding the account would need a separately
  attested proof that the old lane's volume and credentials are destroyed.
  Until one exists the account stays released and unassigned (its positions,
  if any, remain at the broker). Whole-machine migration (#2151) never
  releases and is unaffected.
- **P2: a lane unreachable for the entire drain keeps resurrection-capable
  evidence (#2155 residual window).** A drained lane learns its drain and
  tombstones its confirmation evidence through exactly two channels: the
  heartbeat's `lifecycle_state` answer, and the typed `clerk_lane_draining`
  registration refusal — both require coordinator contact. A lane that is
  down or partitioned for the whole drain, then restarts during a
  coordinator outage with no reachable-coordinator contact in between,
  still offline-boots its last-effective binding from evidence whose
  lifecycle was never re-authored — a v1 file (which carries no lifecycle
  field) and a v2 file written `provisioned` at confirmation time are
  alike unmarked. Bounded by procedure, not by mechanism: after `drain`,
  watch for the lane's next heartbeat before proceeding — a live lane
  marks within one heartbeat interval, and a lane that fails to return it
  (or was already unreachable at drain time) is treated as decommissioned:
  its volume is part of the retirement and is never restarted, per the
  runbook. Shrinks only when #2154's confirmation push lets the coordinator
  know the drain reached the lane.

## Backtest vs reality: when a bot ends (#2607, 2026-09-29)

- **P2: a backtest always holds overnight; the owner's end schedule is not
  modelled (owner decision 4).** Live and backtest share the closing-bar rule
  (`PythonDataService/app/lean_sidecar/closing_bar.py`): neither acts on a
  decision taken on the bar that ends at the session close. A backtest then
  carries any open position into the next session. A live bot instead ends
  when its owner says: by default today at one minute before the close,
  selling at market or keeping its shares as chosen at Deploy (#2607 slices
  2 and 3). A one-day bot that sells every afternoon therefore has no
  backtest counterpart, and neither does any bot whose end, or sell/keep
  choice, differs from "hold until the strategy exits". Compare such a bot's
  live results with a backtest only over trades the end schedule did not
  touch. The rule itself covers Signal Program decisions, the only ones a
  live bot makes. Research-only strategies without a Signal Program (spec
  strategies, the QuantConnect parity ports) have no refusable decision and
  keep their reference fill conventions.
- **P2: a closing-bar EXIT due overnight lives only in the running strategy.**
  The set-aside EXIT is the strategy's own state until the program decides it
  again next session. A run that dies before then -- a restart, or
  `DECISION_BAR_MISSED` after IB Gateway's nightly blackout -- leaves the
  position held. It is not silent: Start refuses with
  `START_REQUIRES_FLAT_CUSTODY`, and the dead-run warning and Flatten apply.

## Lake-sourced research studies: divergences from the provider path (#3521, 2026-10-24)

The Feature study reads admitted lake bars instead of one provider call per run. The open differences below are
measured in `tests/research/test_feature_study_lake_vs_provider.py`.

- **P2: a held day with no recorded capture time cannot be judged early.** `captured_early` treats a missing
  `CompletedAtMs` as final, and an imported day carries its import time, which is later than its capture. Such a day
  captured before its session finished stays as short as it was captured. New captures always carry the time. A
  capture is judged by `CompletedAtMs` less the claim lease (five minutes), the earliest the vendor can have been
  asked; `FetchedAtMs` is not used because it keeps the row's first claim through a retry or a re-capture.
- **P2: the vendor's after-hours tail (four hours) and delivery delay (15 minutes) are constants.**
  `app/data_lake/capture_finality.py` names them once. A plan that delivers in real time makes the delay
  conservative, not wrong; a change to the extended window needs the constant changed.
- **P2: a held day captured before its session finished stays refused, and no re-capture path exists (owner,
  2026-10-24).** A study whose window holds one refuses it (`captured_before_final`, with the day, when it was captured and what it needs) rather than
  computing on the short tape; it asks the vendor for nothing and rewrites nothing. The lake keeps a committed day as
  captured, so the only way to capture it again today is a forced rewrite of a complete day
  (`refresh_complete_artifact`, the path of a provider correction), and that is not safe to run from a study:
  - it changes bytes that sealed receipts may have pinned, so a golden proof replay then fails with
    `RECEIPT_MISMATCH` although its numbers would not move;
  - it moves the shared day to `fetching`, so Golden, Grid Search and a regular-hours study cannot read it until the
    fetch ends, and a vendor failure leaves it `failed` with the prior row not restored (the prior path and sha are
    returned for exactly this, and unused).

  The owner chose to leave held early days refused rather than build an operator-run re-capture. An extended-hours
  study refuses a day whose capture completed before 20:20 ET (17:20 on an early close); a
  regular-hours study one that completed before 16:20 ET (13:20). Those are the final instants (20:15, 16:15) plus the
  five-minute claim lease. Any writer, such as a chart or a backfill, whose capture completes in the five minutes after
  the final instant leaves a day that is refused for good although it may be whole; that is the price of the conservative bound. A
  study that has to capture a day does not start on it until the extended final instant plus that lease has passed
  (`window_not_complete` before), whichever session it reads, because the lake keeps one copy of a day for every reader; a
  study that finds the day held waits only for its own session's final instant plus the lease. So a day a study captures
  itself is never one of those.
- **P2: a split announced after a day was captured stays invisible to that held day until a later capture of the
  symbol refreshes its adjusted history (accepted by the owner, 2026-10-24).** The provider path adjusted to the moment of the request. Price-level
  outputs (MACD signal, quantile-bin bounds) move by the split factor, not only the prices.
- **P3: the pre-run count of sessions the lake lacks does not see a held early day.** The Feature and Indicator
  reliability pages count missing sessions from the catalog, as the Data tab does, and there a day captured early is `ready`; the run still
  refuses it (`captured_before_final`, naming the day) after the count warned of nothing. The count also treats today
  as forming until the New York date rolls, while a run admits it once the extended final instant plus the lease has
  passed (20:20 ET, or 17:20 on an early close), so between 20:20 ET and midnight the notice says nothing and the run fetches one session.
- **P2: a window that starts before the symbol listed refuses.** Admission has no "not applicable" outcome for a
  session before the first trade; the provider path computed on the shorter series. The refusal is
  `data_unavailable_from_provider` for the sessions the vendor answers empty.
- **Unverified assumptions:** that a date-string window is ET-anchored on the live API, and that Polygon's own N-minute
  aggregate equals a fold of its minute aggregates beyond the one case measured. That case: SPY 15-minute aggregates for
  2025-01-13..17 (`adjusted=false`) equal `fold_minute_bars` of the repo's SPY minute capture bar for bar, 320 of 320 in
  open, high, low, close and volume, for the whole day and for regular hours (owner-approved call, 2026-10-24; pinned by
  `tests/research/test_signal_study_lake_vs_provider.py` against the recording under
  `tests/fixtures/golden/lake-studies`). It does not cover other symbols, half-days, a week with a clock change,
  split-adjusted aggregates or other bar sizes, which the comparison tests still assert against an independent stand-in.
  The same recording shows the end date arriving as the whole New York day (the last bar starts at 19:45 ET on the end
  date); one winter week does not settle how the vendor reads a date string.

