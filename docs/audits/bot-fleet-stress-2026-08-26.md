# 50-Bot Fleet Stress Run — 2026-08-26

**Scope.** Day-two robustness campaign against the Alpaca Broker V2 bot
control panel on `PA3KWXU1C4C3`, designed as the live-acceptance pass for
the ten fixes that merged out of the 2026-08-25 run (#1772, #1781–#1789,
ADRs 0046–0048), plus new chaos axes the fixes made reachable: lifecycle
churn at cohort scale, true-concurrency action storms, and process-boundary
kills (SIGTERM and SIGKILL) with full fleet recovery through the operator
surface. Fleet shape per user directive: the six specific strategy programs
on SPY (4 replicas each, 24 bots), `deployment_validation` on everything
else (26 bots across QQQ/TSLA/AAPL). Naming `hNN-<prog>-<sym>-0826`,
`safe_canary` 1-share, `evidence_override` user-authorized for the five
evidence-only programs. Successor to
`docs/audits/bot-fleet-stress-2026-08-25.md`; same conventions — findings
T1…, acceptances A1…, observations O1…, every stuck scenario carries a
root cause and a UI-executable remedy verdict.

**Headline numbers.** 50/50 deploys converged in 3 sweeps (65 attempts,
zero terminal failures); three full-process outages (SIGTERM restart,
SIGKILL crash, SIGSTOP lease-loss) with zero custody errors; both
recoverable outages restored to 50/50 running using only presented panel
actions; 168 fills; one CRITICAL projection defect (T6) found,
root-caused, fixed, regression-tested, and live-verified inside the same
session using the preserved crash state as the acceptance fixture; one
CRITICAL-class resilience gap (T7, lease-loss handle brick) discovered
and characterized at wind-down.

**Verdict in one paragraph.** Yesterday's fixes hold under live fire — all
ten passed explicit acceptance (A1–A13), including the classes that killed
the 2025-08-25 fleet: the token fence, the idempotency ledger (now proven
under true concurrency), pure reads (zero revision drift at 144 rows with
50 trading bots), symbol-scoped warm-up, the stream-health debounce
(validated by two natural Alpaca stream drops), and the cancel-absence
terminal resolution (g01 self-healed unprompted). What the day exposed is
the next layer down: facts that exist durably but get dropped on their way
to a surface. T6 — the roster projecting a SIGKILLed fleet as innocent
"Off duty" because the SQLite status builder hardcoded
`duty_outcome=None` — is the fifth member of the "mechanism present, fact
collapsed en route" family, and its fix immediately surfaced crash history
(13 legacy "Crashed" rows) that had been invisible for weeks. The
remaining findings are cost curves (deploy latency and catalog reads
degrade with running-fleet size, T2/O4) and emergent fleet dynamics
(same-symbol cohorts strand in lockstep on a stop wave, T3).

---

## 1. Run configuration

- **Fleet**: 24 SPY strategy bots (`ema_crossover_signal`,
  `rsi_mean_reversion`, `sma_crossover`, `spy_strategy_a/b/c` × 4 replicas)
  + 26 `deployment_validation` (QQQ 9, TSLA 9, AAPL 8). Manifest
  round-robin-interleaved across symbols so all four channels warm
  simultaneously (deliberate WP3 probe).
- **Timeline (CT)**: preflight + g01 acceptance 08:50–09:20 · launch
  09:26–09:33 (3 sweeps) · baseline soak + action storms 09:33–09:53 ·
  churn wave 09:50–09:57 · SIGTERM probe 09:58 (39s outage) · recovery to
  50/50 by 10:04 · SIGKILL probe 10:06 · T6 diagnosis + fix + live verify
  10:10–10:40 · recovery to 50/50 by 10:45 · soak · wind-down (§5).
- **Environment**: data plane in `polygon-data-service` (compose mounts
  `app/` live; restart loads code), IBKR feed via IB Gateway (port 4002),
  Alpaca paper execution, SQLite clerk sole authority. 94 legacy roster
  rows retained deliberately as read-scale ballast (144 total during run).
- **Tooling**: rebuilt as committed scripts under `scripts/dev/fleet/`
  (`_api.py`, `fleet_launch.py`, `panel_action.py`, `action_storm.py`,
  `churn_wave.py`, `runner_stop.py`, `read_bench.py`) — the 2025-08-25
  session tooling lived in tmp and was purged; this closes that loop.
  Run artifacts in gitignored `.scratch/fleet-0826/`.

## 2. Acceptance results for the 2026-08-25/26 fixes

| # | Fix under test | Result |
|---|---|---|
| A1 | WP1 cancel-absence (#1781) | **PASSED.** g01-dv-spy-0825 self-healed on the first sweep after restart: hold cleared, reconciliation clean, `RESUME_ADMITTED`. The S15c permanent-freeze class is cured in production; no `reset_authority`. |
| A2 | S16 token fence (#1772) | **PASSED.** First-try action success under a 6-thread read storm (2025-08-25 pre-fix: 0/15 scripted, 0/3 UI). |
| A3 | F15 idempotency, sequential (#1772) | **PASSED.** Same key 3× → one `applied=true`, replays share the receipt. |
| A4 | WP2 pure reads (#1786) | **PASSED.** 96 concurrent reads at 94 rows: 0 errors, revision moved only at the deliberate mutation. Re-proven at 144 rows with 50 trading bots: **zero read-induced drift**. |
| A5 | S5 doomed-subscription spam | **GONE.** 0 "APPL" log hits (read purity removed the trigger path). |
| A6 | WP4 hold debounce (#1784) | **PASSED on natural chaos.** Two real Alpaca `trade_updates` drops (~2s each); no account hold raised, reconnect + idempotent redelivery absorption with surfaced warnings. |
| A7 | Idempotency under true concurrency | **PASSED.** 4 simultaneous POSTs, one key → exactly one applied. |
| A8 | Concurrent conflicting actions | **PASSED.** `stop_bot_decisions` + `reconcile_now` same-instant: both 200, serialized, no torn custody. |
| A9 | F18 safe-flatten ladder (#1768) | **PASSED end-to-end** on deliberately stranded exposure: reconcile → `execute_safe_flatten` (real order) → flat → `RESUME_ADMITTED` → resumed. `prepare_safe_flatten` correctly refuses POST as a view action. |
| A10 | Graceful shutdown at fleet scale | **PASSED.** SIGTERM with 50 running (9 mid-position): all 50 reached verified-custody termination inside podman's 10s grace window. Zero uncertainties. |
| A11 | WP7 S17 blocker authoring (#1788) | **PASSED.** Stale-freshness flatten shows `RECONCILIATION_EVIDENCE_STALE`, `disposition=fix_here`, backend-authored "Run Reconcile now" move. |
| A12 | Fleet-scale recovery via operator surface | **PASSED twice.** Both outages recovered to 50/50 with only presented actions (resume + reconcile→flatten→resume ladders). Zero resets, zero manual surgery. |
| A13 | Roster row actions (#1778/#1788) | **ALIVE.** With real attention rows existing post-T6-fix, every attention row renders its primary recovery action ("Reconcile now"). The affordance previously assessed as dead was starved of input, not dead. |
| — | WP3 symbol-scoped deploy health (#1783) | **PASSED.** All four symbols warmed in parallel; each symbol's second deploy 409'd with a verdict naming **its own** symbol; the account kept accepting other symbols' deploys throughout (2025-08-25: one cold symbol froze all deploys ~60s). |
| — | S3 forming-bar seal (#1772) | **PASSED.** Zero `SOURCE_BAR_IDENTITY_CONFLICT` across 50 deploys and two recoveries (2025-08-25: crashed bots ~60s after deploy). |
| — | S7 poll timeout (frontend) | **PASSED implicitly.** Roster self-recovered after both outages without a manual ↻; outage states rendered honestly. |

## 3. Findings

### T6 — SQLite catalog nulls `duty_outcome`: a crashed fleet renders innocent *(CRITICAL — fixed this session)*

After the SIGKILL probe (50 bots, roughly half mid-position), the catalog
showed **144× "Off duty", `needs_attention=0`** — while the lifecycle repo
durably held `EXITED_UNVERIFIED / INTERRUPTED_BY_RESTART` for every one,
and the single-bot roster GET carried it. Root cause:
`sqlite_panel_source._sqlite_roster_status` constructed every catalog
`BotStatusView` with hardcoded `duty_outcome=None`, so
`catalog_projection_service`'s S3b logic (label + attention) could never
fire on the fleet surface. The S3b defect ("crashed bot renders as
innocent Off duty") was fixed in the projection service but its production
input path starved it — the fifth sighting of the
mechanism-present/fact-collapsed-en-route family.

**Fix (this session):** canonical `duty_outcome_view()` mapping extracted
to `bot_registry_projection`; `_sqlite_roster_status` joins the durable
lifecycle record (absent → None for legacy bots; corrupt → loud
`SqliteCatalogProjectionUnavailable`, matching the module's
incomplete-config contract). Regression test red-before/green-after
(`test_sqlite_roster_projects_the_durable_duty_outcome`). **Live-verified
against the preserved crash state**: catalog flipped to 54× "Exited
unverified" + 13× "Crashed" + 67 attention rows — the 13 include legacy
bots (`Aug11`, `sqlite-s6-googl-0811`, …) whose crash history had been
silently hidden for weeks. **UI remedy pre-fix: none** — the surface
itself was the defect.

### T1 — Narrow retire misses its motivating case (F16 still open)

`Aug11` (symbol "APPL" typo) remains unretirable: the S5 guard requires a
**dead strategy key** (`strategy_runtime_missing`), but the zombie's key
`deployment_validation` is alive — its *symbol* is what is dead. The panel
is self-contradictory: retire's blocker says "This bot can still run."
while resume on the same panel is permanently "Resume is blocked." The
zombie class is *bot that can never admit again*, not *bot whose program
died*. **UI remedy: none.** Recommend widening the guard to
permanently-inadmissible bots (invalid symbol at minimum) or a
symbol-validity predicate alongside `strategy_runtime_missing`.

### T2 — Read latency under fleet load exceeds the frontend poll budget

At 144 rows with 50 trading bots: catalog p50 16.8s / p95 20.6s (idle
94-row baseline: 3.3s / 8.7s); panel p50 2.7s (idle 0.67s). The
frontend's `POLL_REQUEST_TIMEOUT_MS=15s` (the S7 fix) now trips on
healthy-but-slow catalog reads, surfacing "Account/Clerk refresh failed"
during normal operation (observed live by the user). Read purity held
throughout — this is a cost curve, not drift. Suspect shares O4's root
(per-account contention). **UI remedy: self-recovers next poll**; UX
corrected this session (pills, below). Root cause deserves a profile
before tuning either the timeout or the read path.

### T3 — Same-symbol cohorts strand in lockstep on a stop wave

Churn wave (the phase S15c aborted on 2025-08-25, now executed): the TSLA
cohort churned clean 2/2 waves; **all four** QQQ bots were caught
mid-position by the same stop wave — same-symbol dv bots enter/exit in
lockstep, so a cohort-targeted stop lands mid-hold for every member at
once → 4× `RESUME_CARRYOVER_UNSUPPORTED` simultaneously. Every piece is
by design (stop = decisions-stop; carryover FORBID); the emergent effect
is cohort-scale stranding with only per-bot remediation (reconcile →
flatten → resume, × N). **UI remedy: exists but is N×3 clicks.**
Recommend a cohort/fleet flatten affordance — the inverse-scoped sibling
of the Two-Tap account-hold rule.

### T5 — Panel reads 503 under write pressure

With 50 trading bots, a panel GET can return 503 "SQLite custody and
execution economics changed during panel projection" — an honest
fail-closed torn-read guard, but it surfaces as flakiness exactly when an
operator inspects an active bot. Benches saw 0/96; occurrence is
load-correlated and intermittent. **UI remedy: Try again (rendered).**
Recommend a bounded in-server projection retry before failing the request.

### T4 — Transient `RECOVERY_UNCERTAIN` (observation)

During post-outage sweep evaluation, a resume read can briefly return
`reason_code=RECOVERY_UNCERTAIN` before settling. Honest but unexplained;
consider "evaluation in progress" next-step prose.

### T7 — Process freeze past the execution-lease TTL bricks the account handle *(CRITICAL class — found at wind-down)*

`podman pause` (SIGSTOP) for ~50s: the SQLite execution lease expired
while the process was frozen. On SIGCONT: "lease heartbeat failed; writes
remain fail-closed" (CRITICAL) → the ~24 still-running bots crashed **and
their terminal STOP evidence could not commit** ("could not commit SQLite
STOP") → every subsequent panel action 500'd "account 'PA3KWXU1C4C3'
execution lease was lost or expired; this handle can no longer write",
with **no in-process re-acquisition**. Recovery required a container
restart (fresh lease + boot scan); custody then reconciled clean and the
7 stranded exposures drained through the flatten ladder.

Real-world triggers for the same freeze: laptop sleep, VM migration,
prolonged CPU starvation. Fail-closed is **correct** — a holder that lost
its lease must not write. The gaps: (a) no supervised lease
re-acquisition path short of a full process restart; (b) terminal
evidence has nowhere to commit when the lease is gone (the file-side
lifecycle record held — which is why the T6-fixed roster still told the
truth — but the SQLite side has a hole until boot scan); (c) surfaces
show raw 500s instead of an authored account-scoped "authority lease
lost — restart the data plane" blocker (the Two-Tap rule's own shape:
account-scoped problem, account-scoped cure). **UI remedy pre-restart:
none.**

## 4. Observations

- **O1 — Boot-window attention fanout.** Between container start and first
  sweep, all rows show `needs_attention` with the account-clerk-evidence
  explanation (account freeze fanned per-bot); clears on first sweep.
  Known family (#1780 / ADR 0048 substrate); boot-window only.
- **O2/O4 — Cost curves vs running-fleet size.** Deploys: median 0.4s for
  the first ~30, ~15s (max 21.9s) for deploys 31–50. Catalog reads: see
  T2. Same suspected root: per-account lock contention between admission
  work and running bots' clerk operations. Profile before fixing.
- **O3 — Warm-up UX (user question: "should the UI have a warm-up
  button?").** Recommendation: **no button** — an operator-warmed channel
  is an unowned, TTL-less subscription (doomed-subscription risk, cf. T1)
  and puts a human in the freshness-production loop WP2/WP4 removed.
  What is actually wrong: (a) the warm-up 409's `next_action` says
  "Restore both Clerk channels" when nothing is broken — the operator's
  own deploy just installed the feed and it heals in ~60s; copy should
  say so; (b) the deploy drawer should render the WP3 per-symbol verdict
  live once a symbol is chosen, re-enabling Deploy on healthy. Heavier
  alternative (a real design decision): admit the deploy and let the
  ENTER-time symbol gate hold — safe by construction under the Two-Tap
  rule, but trades away deploy-time fail-fast.
- **O5 — Resume admission shares the warm-up UX gap.** First resume per
  symbol post-restart installs the subscription then refuses with bare
  `MARKET_DATA_STALE` (48/50 on first pass) — no warming prose. Same O3
  copy recommendation, applied to resume.
- **Copy nit:** a "Crashed"-labeled row can carry explanation "Off duty
  and flat." (clerk-derived explanation vs lifecycle-derived label) —
  cosmetic sibling of T6.
- **Open question for the user:** the roster-level "Live refresh failed.
  Showing the last successful fleet snapshot." banner is a third standing
  refresh message, not covered by the pill directive below.

## 5. UI change shipped this session (user-directed)

The account strip's two standing refresh-failure banners ("Account refresh
failed…", "Clerk refresh failed…") are now **transient popover pills**:
shown on each false→true edge of the unavailable inputs, auto-dismissed
after 6s, no re-nag while a failure persists (the last-good observation
timestamp already conveys staleness), no layout shift, `aria-live=polite`,
reduced-motion respected. `account-strip.component.*` + spec (13/13),
lint clean. Live trigger observed during outage windows.

## 6. Wind-down

Mass stop began 10:26 CT; the T7 lease incident struck mid-stop (~26 stops
had committed; the rest crashed on the dead handle). Post-restart
(10:28:46): boot scan marked the interrupted runs, flatten ladders drained
all 7 remaining exposures, and the final sweep verified **reconciliation
clean, no hold, no freeze, 0 outstanding intents, 0 exposure across all
144 rows**. Final numbers: **168 fills**, realized −$13.93 (1-share
canary spread noise), zero custody errors all day — including through
three full-process outages (SIGTERM, SIGKILL, SIGSTOP-lease-loss).

Test gates at close: Python full suite green in sibling container,
Frontend 1944/1944, ruff + eslint project-scope clean.

## 7. Ops lore added today

- Running-bot stop action id on the panel surface is `stop_bot_decisions`;
  `pause`/`continue` are not presented there. Stopped-bot resume is
  `resume`. (The performer map's `stop` is not the wire id.)
- `podman restart` = SIGTERM = graceful fleet shutdown (verified custody,
  "Off duty" is correct); only SIGKILL produces the crash path. A crash
  probe that uses `restart` tests the wrong thing.
- Resume clears `duty_outcome` (lifecycle `clear_duty_outcome` on
  ON_DUTY), so post-recovery rosters go quiet again — attention rows are
  a stopped-state phenomenon.
- The compose mount covers `app/` only; container restart loads app code
  (hot-reload remains broken on macOS podman).
- Convergence loops beat one-shot passes for fleet restore: warm-up,
  freshness fences, and admission races all self-resolve within ~2–3
  cycles of resume/reconcile/flatten sweeps.
