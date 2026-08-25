# 50-Bot Fleet Stress Run — 2026-08-25

**Scope.** User-directed robustness campaign against the Alpaca Broker V2 bot
control panel: 54 concurrent paper bots (50-bot planned fleet + 4 burst
probes) across all 7 sealed signal programs × 4 validated symbols on
`PA3KWXU1C4C3`, with deliberately overlapping same-symbol cohorts, a staged
chaos phase probing every designed refusal gate, and a UI-verified wind-down.
Successor to the 18-bot ops study (`bot-launch-ops-study-2026-08-24.md`);
every finding here is numbered S1–S17 with probe verdicts C1–C3 and passes
O1–O4. Convention (user directive): every stuck scenario is documented with
its root cause **and a UI-executable remedy verdict**.

**Headline numbers.** 54 deploys (100% eventual success across 4 passes),
peak 53 running concurrently, **523 fills**, peak same-symbol cohort of ~18
bots entering/exiting in lockstep, ~5.5 h of live RTH operation,
**zero custody errors** (clerk attribution matched broker truth at every
cross-check, including final flat-and-order-free verification at 13:00 CT).
Two fleet-killing platform bugs found, root-caused, fixed in PR #1772, and
verified live. One permanent-freeze defect class identified with no
resolution path (left reproduced on the account, deliberately).

**Verdict in one paragraph.** The custody spine is excellent under fleet
load — event-sourced attribution, lockstep cohort exits (F19 fix), stream
recovery, and automatic missed-fill reconciliation all passed hard natural
tests. What breaks at 50 bots is everything wrapped *around* the spine: the
panel action pipeline (latency, an unsatisfiable revision fence, a 30 s
freshness budget the pipeline itself outspends), single-sample health gates
with account-wide blast radius, and read paths that were never shaped for
90-row rosters. Nearly every failure shares one architecture smell, named by
the user mid-run: consumers must synchronously manufacture their own
freshness at act time — "the bucket fills and drains through the same tap."

---

## 1. Run configuration

- **Fleet**: `fNN-<prog>-<sym>-0825` naming (≤25 chars, per the
  `OrderRefTooLongError` cap). All 28 sealed (program, symbol) pairs once +
  22 collision replicas weighted to SPY (21 SPY bots) and
  `deployment_validation` (15 bots, guaranteed order flow). 4 extra
  `g0N-dv-*` bots deployed as the burst probe. `safe_canary` sizing
  (1 share); `evidence_override` (user-authorized) on the 5 evidence-only
  programs; no override on the 2 accepted programs, as required.
- **Timeline (CT)**: prereq recovery 08:17–08:43 · launch ramp + sweeps
  08:43–09:17 · baseline 09:17–10:30 · staged chaos 10:30–12:07 · churn
  wave (aborted by S15c) 12:04–12:08 · wind-down mass stop 12:09–12:12 ·
  recovery + UI flatten 12:25–13:00.
- **Environment**: data plane in `polygon-data-service` (asyncio task per
  bot), IBKR feed via IB Gateway 10.47 (ref-counted: 4 subscriptions served
  all ~53 bots), Alpaca paper execution, SQLite clerk sole authority.
- **Tooling** (session scratchpad; candidates for `scripts/dev/`):
  skip-aware `fleet_launch.py` (manifest + ramped cadence + sweep-to-
  converge), `panel_action.py` (token-rebinding action driver),
  `runner_stop.py` (mass stop), `burst_deploy.py`, `churn_wave.py`,
  `read_bench.py`. Full JSONL result logs retained.

## 2. Critical findings

### S16 — Panel actions structurally un-executable: revision fence vs. self-bumping reads *(CRITICAL, fixed in PR #1772)*

`execute_sqlite_panel_action` fenced every guarded action with strict
revision equality (`request.revision != panel.revision → 409`,
`services/broker_v2_panel/sqlite_panel_source.py:832`) — but **every panel
read bumps the revision by +2** (measured: 4 consecutive reads →
20083/85/87/89; idle drift ~0.4/s), and the executor re-derives the panel
during validation, so `panel.revision ≥ request.revision + 2` **always**.
Result: 15/15 scripted attempts and 3/3 human UI clicks failed with "The
SQLite Clerk projection changed after this action was presented" — on an
idle, zero-bot account. Only `resume` still worked (exempted path). The
action-scoped `concurrency_token` — which the schema documents as *the*
staleness check, deliberately narrower than the display revision
(`schemas/broker_v2_panel.py:119-122`) — was empirically **stable across
reads** and never consulted by this fence. This is the third incident of
the token-churn class the 0824 study warned about.
**Fix (PR #1772)**: fence on `request.concurrency_token !=
action.concurrency_token` per the documented contract, backed by the durable
SQLite panel-action idempotency ledger and regression tests. Verified live:
0/15 → first-try success, and the operator's UI flatten clicks worked.
**UI remedy pre-fix: none** — humans cannot outrace their own reads.

### S15c — Lost-submit intent has no terminal state; freezes account-wide resume/deploy *(CRITICAL, open)*

At 11:44 CT an S9 websocket drop swallowed the broker response to
`g01-dv-spy-0825`'s ENTRY submit. Alpaca's API confirms the order **never
existed at the broker** (read-only `orders:by_client_order_id` → 404). The
clerk correctly raised `ORDER_OUTCOME_UNKNOWN` (CUSTODY_SUBJECT scope,
blocks the one bot) — but the *only* resolution fold,
`resolve_unknown_outcome_if_proven` (`uncertainty_folds.py:179`), fires
solely on a broker ack/terminal event that can never arrive for a
nonexistent order. The PRD #1150 registrar TTL auto-void was never ported
to the Alpaca SQLite clerk. Every advertised remedy dead-ends:
`reconcile_now` succeeds without resolving; the exact-identity pair returns
`NO_EXECUTION_COVERAGE_CONFLICT`; cancel returns
`NO_VERIFIED_WORKING_ORDERS`; flatten returns `EXPOSURE_NOT_PROVEN`;
`stop_bot_decisions` returns `NO_ACTIVE_BOT_RUN` **while the roster shows
the bot "Working"** (state contradiction). Measured blast radius: the
account-scoped intent gate then refused **every resume fleet-wide** ("1
order intent(s) remain unresolved after recovery") — the churn wave
stranded 5 healthy bots before being aborted.
**Status**: left open on the account as a live reproduction. The designed
escape (`reset_authority`, now armed since the account is flat and
order-free) is documented but unexercised. **Fix direction**:
reconciliation observing broker-not-found for an unacked submit past a TTL
emits a terminal "never accepted" fact and resolves the episode.

> **Correction (2026-08-25, #1775).** Two mechanism claims in this finding
> were disproven while the fix was built. The original text is left standing
> above; the receipts are here.
>
> 1. *"The PRD #1150 registrar TTL auto-void was never ported to the Alpaca
>    SQLite clerk."* **Wrong — it is ported, and it fired.**
>    `resolve_order_submission` folds `ORDER_SUBMIT_FAILED_ABSENT` once the
>    exact lookup is definitively absent past the R4 grace window
>    (`order_evidence.py:492`), and this incident's ENTER effect did reach
>    its terminal receipt that way. The defect was one step downstream: the
>    EXIT that then enumerated the dead entry had no definitive-absence
>    branch in its cancel-prove step (`exit_resolution.py:279`), so an exact
>    lookup answering "absent" folded `ORDER_CANCEL_UNCERTAIN`
>    unconditionally and re-opened the episode against the EXIT effect on
>    every pass. Fixed in #1775.
> 2. *"`reset_authority`, now armed since the account is flat and
>    order-free."* **Wrong — it was not armed.** Authority recovery refuses
>    while a live execution lease exists (`recovery.py:956`), and the running
>    Clerk — the same process that runs the 15 s sweep — is the lease holder.
>    The escape needs a stopped Clerk or a process-stop proof, which is why
>    it could not be exercised. Choosing between an offline/CLI-only recovery
>    path and an orchestrated quiesce protocol is the open decision in #1779.

### S3 — Forming-bar warmup seal crashed every bot at first live bar *(CRITICAL, fixed in PR #1772 + regression test)*

IBKR's historical endpoint includes the still-forming minute as its last
row; `recent_closed_bars` sealed it into the per-bot source-bar ledger
(PR #1764 — its first live outing), so the completed live bar for the same
window arrived with a different payload → `SOURCE_BAR_IDENTITY_CONFLICT` →
bot crash ~60 s after deploy. Proven empirically: f01's ledger row for the
crash window had `fetched_at_ms` **45 s before** its own `end_ms`, volume
4,031 vs 32k–85k neighbors. f01/f03/f04 all died this way; every bot
deployed mid-minute would have.
**Fix**: drop bars with `end_ms` after the pre-request observation time at the feed boundary
(`marketdata/ibkr_feed.py`, enforcing the docstring's own "closed"
contract) + regression test `test_recent_closed_bars_drops_the_forming_bar`
(verified red pre-fix, green post-fix). All 50+ subsequent deploys ran
crash-free for ~5 h.
**S3b (panel honesty, open)**: a crashed bot renders as "Off duty · Flat",
`needs_attention=false` — indistinguishable from a deliberate stop. Three
bots died; the roster showed zero flags.

## 3. Major findings

### S12 family — The action pipeline outspends its own budgets under load

- **S12**: stop latency 0.3 s (18-bot baseline) → 5–57 s at 50 bots; panel
  actions 48–98 s end-to-end; panel GETs 0.7–53 s (wild variance, F13
  serialization). Post-mass-stop it got *worse* (S12d): 105–145 s reads at
  **zero** running bots with the container at 77% CPU — a hot background
  loop, cleared by restart; root not yet identified.
- **S12b/c**: the first action attempt chronically 409s on staleness; under
  churn (hold install/release cycles) even sub-second GET→POST rebinds
  lost. Subsumed by S16's root cause.
- **S13**: the recovery ladder demands custody evidence <30 s old
  (`FRESH_EVIDENCE_MAX_AGE_MS`, `recovery_policy.py:57`) while each ladder
  step costs 50–145 s under load — the evidence expires mid-action, every
  time, producing a circular "Run Reconcile now" → success → same refusal
  loop. **The stranded-exposure remedy is unreachable exactly when a large
  fleet is stuck.** Verified workaround: mass-stop first, then ladder (at
  quiet-system speeds the 30 s budget is trivially met). Fix direction: a
  dedicated freshness producer while a recovery flow is open (see §7), or
  budget scaled to measured action latency.
- **S14**: catalog reads 500 (`SqliteCatalogRevisionMismatch` →
  `PanelUnavailableError`) while deploys mutate the roster — the roster
  page's 5 s poll errors precisely during fleet growth; also fooled this
  run's own monitoring once (a coherence flicker mid-fill read one bot's
  exposure as empty). Should retry once or serve the prior revision.

### S10 — STREAM_HEALTH_HOLD: right instinct, single-sample trigger, 3-minute account freeze per 5-second blip

Observed 5+ full cycles. One dropped websocket sample (S9) → account-wide
hold ~30 s later → all ~90 roster rows flip `needs_attention` → hold
self-releases on the next healthy observation (~2–3 min). Verified
mid-cohort: exits/reductions proceeded during the hold (entries-only
freeze, matching `STREAM_HEALTH_HOLD_CODE` docs); the "self-release" is the
sweep re-deriving the journal-backed hold, consistent with "never
auto-cleared".

> **Correction (2026-08-25, #1775).** *"The 'self-release' is the sweep
> re-deriving the journal-backed hold."* **Wrong — the sweep never touches
> the hold.** `_sync_stream_health_hold` has exactly one call site, inside
> ENTER-purpose effect execution (`runtime.py:661`); the reconciliation pass
> does not call it at all. What read as a sweep-driven self-release was the
> next bot attempting an ENTER and re-deriving the hold on its way in —
> which is also why a quiet fleet can hold a stale freeze indefinitely. The
> independent fixed-cadence hold sync is #1777. UI honesty gaps during a hold: roster chips read "Running 0,
Stopped 0" while 50 bots run; banner and guidance say "no active hold"
beside the active hold.

## 4. Moderate findings

| # | Finding | UI remedy verdict |
|---|---|---|
| S1 | IBKR reconnect exhausts 10 attempts → terminal `HARD_DOWN`; a gateway that boots 4 min late is never picked up. Deploy surface dead until human action. | **Works**: IBKR pill reconnect (calls `POST /api/broker/reconnect`). Recommend re-arm on listen or slow periodic probe (see circuit-breaker, §7). |
| S4 | Orphaned account-safety admission markers (`gate`+`writer`, dated Aug 3 — no TTL/staleness handling, `account_safety.py:235`) broke every account-truth refresh cycle for 3 weeks and inflated deploy latency to 10 s+. | **None exists.** Removed manually. Needs staleness detection + operator surface. |
| S5 | Legacy typo bot `Aug11` (symbol "APPL") drives doomed IBKR subscriptions forever; cannot be retired (F16 dead vocabulary). | **None** — no retire/delete. |
| S6/S8/S9/S11 | The deploy-refusal taxonomy (all correct-by-copy, all account-wide): first-per-symbol subscription warm-up marks Market Data stale ≤60 s (S6); one stale pooled REST connection → unhandled 500 → Execution unhealthy (S8; reads never retry); trade_updates drops every ~3–5 min, recovers in 2–5 s, each drop a deploy-freeze window (S9; cause unresolved — idle timeout vs competing listener); `clerk.intent_custody` requires **zero** outstanding intents account-wide, so deploying into live order flow is a stochastic race (S11). | Sweep-until-converge works (12 refusals absorbed across 4 launch passes); the structural fixes are §7 items 1 and 3. |
> **Quantified + fixed (2026-08-25, #1777 WP4).** S1 was scored *moderate*.
> The retained broker connection log (`artifacts/live_runs/_broker/connection_events.jsonl`,
> 5 000 events, 23 Aug 22:17 -> 25 Aug 19:09 ET) shows it is the single
> largest availability defect measured in this repo.
>
> * **37.9 % downtime** — 1 021 of 2 691 observed minutes with no successful
>   30 s probe: 08-24 00:44->08:49 ET (8.1 h) and 08-25 00:44->09:41 ET (8.9 h).
> * **3 `HARD_DOWN` latches in 1.9 days** (~1.6/day). Each followed the same
>   script: attempts open at 00:45:0x, exhaust `1+2+4+8+16+32+60x4 = 303 s`,
>   latch ~5 min 46 s in.
> * **The latch, not the outage, set the downtime.** `_tick`'s hard-down branch
>   only *observed* the client, so after latching the monitor emitted nothing
>   for eight hours — 3 303 `BROKER_PROBE_OK` events in the file and zero
>   `BROKER_PROBE_FAILED`. Recovery arrived from an unrelated data-farm event,
>   not the reconnect path (30 of 31 attempts failed; exactly 1 succeeded).
>   The third latch proves the cost: `09:21:19 -> 09:41:22`, a **20-minute**
>   outage that a longer-patience ladder would have absorbed with no latch.
> * **Root cause was not IBKR.** IB Gateway's own log records
>   `Daily auto-restart is not enabled.` and an `IB GATEWAY RESTART` at
>   08-24 07:46 and 08-25 08:25 — manual morning relaunches. The gateway
>   auto-logged-off nightly and stayed down until a human opened it.
>   Operator remedy: IB Gateway -> Configure -> Settings -> Lock and Exit ->
>   **Auto restart** (preserves the session; IBKR still forces a weekly
>   re-login).
> * **Fix shipped:** `HARD_DOWN` is now the breaker's OPEN state, probing every
>   `OPEN_PROBE_INTERVAL_S` (60 s) indefinitely through the shared client
>   lifecycle lock. Downtime is now bounded by the gateway's real availability
>   rather than by whatever unrelated event happens to poke the monitor.
>   The transient farm flaps in the same window (`2103->2104`, `2105->2106`)
>   all recovered in **~1 s**, so they need no debounce beyond WP4's.

| S7 | Roster 5 s poll dies silently across a data-plane restart (9+ min stale, "Running 0" while bots run; only tell is the small "observed" caption). | **Works**: ↻ Refresh. Needs staleness banner + poll retry. |
| S15 | `ORDER_OUTCOME_UNKNOWN` presentation: bot auto-sorted to top, "Mission blocked" chip, precise copy, prominent Reconcile button, header flips to "Stale". Excellent — but see S15c and S16. | Presentation excellent; remedies dead-ended pre-fix. |
| S17 | The stranded-exposure remedy (`execute_safe_flatten`) is buried at the bottom of an unlabeled ⋯ overflow, below four near-always-disabled actions; its disabled hint says disposition **"wait"** — the one thing that makes the freshness blocker *worse*. | Exists but hard to find; make Flatten first-class next to Resume for exposed stopped bots; fix the "wait" disposition. |
| — | Deploy 409 copy drops the symbol the health sample already names (`"Active IBKR feed for {SYM} has not produced its first closed bar"` → flattened to "Market Data is unhealthy"); one refusal said only "Bot runtime safety refused Start". | Surface the sample's own reason string. |
| — | An action POST that outlives the client's timeout still executes minutes later with no pending indicator (a timed-out resume revived a bot at +5 min); idempotency replay 404s (F15). | Claim-check contract, §7 item 1. |

## 5. Passes — what held under fire

- **Custody integrity (the big one)**: 523 fills, 54 bots, zero
  attribution errors; clerk vs broker cross-checks clean at every probe and
  at final flat verification. Namespace attribution never netted, never
  leaked across bots.
- **C1 restart-intensity gate**: 3rd activation in 300 s refused with
  exact quantified copy; clean resume after cool-down.
- **C2 resume-while-exposed**: refused honestly ("cannot safely restore
  its prior open-position lifecycle"); stop-leaves-exposure contract
  confirmed; **F18 safe-flatten ladder verified end-to-end by the human
  operator in the UI** (5 stranded shares closed, incl. 3 from 0824) once
  S16 was fixed.
- **C3 burst admission**: 4 simultaneous deploys all 201 (intake-fence
  serialized, 14–19 s each) — better than 0824's burst flap.
- **O2/O4 stream resilience**: every websocket drop recovered in 2–5 s
  with reconcile-after-connect; one redelivered terminal event absorbed
  idempotently and surfaced; a fill missed during a drop became an
  Unexplained Order and was **auto-reconciled to its owning order_ref in
  ~3 min** with zero operator involvement (display nit: header keeps the
  scary label after resolution).
- **F19 lockstep cohorts**: ~18-bot same-symbol cohorts entered and exited
  together repeatedly, all session, zero crashes.
- **Mass stop**: 47/47 in 132.6 s (2.8 s/bot at 50-bot scale).
- **Feed fan-out**: 4 IBKR subscriptions served the entire fleet
  (ref-counted) — the in-house proof of the architecture in §7.
- **Evidence/override plumbing**: evidence_override accepted exactly where
  required, rejected where not; 25-char sid cap enforced at deploy.

## 6. Scaling measurements

| Surface | 18-bot baseline (0824) | This run (50–53 bots) |
|---|---|---|
| Deploy latency | 0.23–0.51 s | 0.27–0.7 s healthy · 5–19 s under churn/contention · >30 s twice |
| Runner stop | 0.2–1.0 s | 0.5–5 s draining · 20–57 s at full load · 47 bots / 132.6 s mass |
| Panel GET | ~56 ms | 0.2 s quiet · 0.7–53 s under churn · 105–145 s during S12d |
| 10 concurrent panel GETs | ~2.6 s each (10 bots) | median 3.8 s each (F13 confirmed) |
| Catalog GET (90 rows, 46 KB) | — | 0.44–0.60 s (healthy; UI polls at 5 s) |
| Gallery snapshot | 5.6 s / 751 KB @ 25 tiles | **15–28 s** / 365 KB @ 94 bots (F14; unusable, and its reads visibly starved other consumers) |
| Panel action end-to-end | seconds | 48–98 s at load · ~1 s quiet post-fix |

## 7. Architecture: one tap fills, the other drains

The user's frame, adopted as the recommendation spine: most failures are
consumers forced to synchronously manufacture their own freshness at act
time. Patterns that **delete** machinery (each grounded above):

1. **Claim-check window** — every mutation returns a durable ticket
   instantly; outcome always fetchable by ticket. Deletes the token-race
   window, ghost-executions after client timeouts, F15's replay 404, and
   all sweep/retry loops (deploys join the queue). The pieces
   (idempotency keys, receipts, command journal) already exist.
2. **Dead-letter office** — every lifecycle state machine must be *total*:
   bounded-time path from any state to a terminal state. Kills the S15c
   freeze class (broker-not-found + TTL → "never accepted").
3. **Blast-radius alignment** — a gate may only bind at the scope of the
   fact it consumes. Removes the amplifiers in S6/S11/S15c that turned one
   warming symbol or one lost packet into a fleet freeze.
4. **Bucket levels, not last drops** — health gates read debounced levels
   (unhealthy for N consecutive samples; hysteresis) instead of tripping
   account-wide on one sample (S8/S10).
5. **Freshness producers** — while a recovery flow is open, a background
   refresher keeps custody evidence inside the 30 s budget; the gate
   stays, the production decouples (S13).
6. **Three-state circuit breaker** — one closed/open/half-open breaker
   (open always probes) replacing IBKR's die-after-10 (S1) and unifying
   with Alpaca's infinite retry.
7. **In-house precedent** — the market-data feed (one subscription per
   symbol, ref-counted fan-out) already *is* this architecture and scaled
   perfectly. "Make the projection side work like the feed side."

**Non-goals** (load-bearing as-is): intake-fence write serialization, and
act-time re-proof inside mutating transactions (`accept_recovery_exit`).
Zero custody errors across 523 fills is their evidence. Bucket for
admission; proof for commitment.

## 8. Fixes shipped in PR #1772

1. `app/marketdata/ibkr_feed.py` — drop forming bars from warmup
   (anchored to the pre-request observation time); regression tests in
   `tests/marketdata/test_feed.py`
   (red pre-fix / green post-fix verified).
2. `app/services/broker_v2_panel/sqlite_panel_source.py` — action fence on
   the action-scoped `concurrency_token` (the documented contract) instead
   of unsatisfiable strict revision equality; verified live by scripted
   actions and human UI clicks, with regression coverage in
   `tests/broker/v2panel/test_sqlite_action_fence.py`.
3. The SQLite panel action path now uses the durable per-bot idempotency
   store before its staleness fence; an applied action retry replays the
   original receipt as `applied=false` instead of executing twice or 409ing.
   Router regression coverage is included in PR #1772.

Also performed (state, not code): removed 3-week-orphaned account-safety
markers for DUM284968; two data-plane restarts (S12d hot loop; fix loads).

## 9. Open items

- **g01-dv-spy-0825** left quarantined under the S15c uncertainty as a
  live reproduction; account otherwise flat/clean. Escape when desired: the
  S15c code fix (#1775). `reset_authority` is *not* available while the
  Clerk process holds its execution lease — see the S15c correction above
  and the open decision in #1779.
- **Intent-gate rescope (decided 2026-08-25, #1775; implementation not yet
  ticketed).** The Start/deploy gate counts unresolved effects
  *account-wide* (`runtime.py:883` → `bot_start_admission.py:207`), while the
  uncertainty episode underneath it is scoped to a single custody subject —
  which is why one bot's stuck intent refused every resume fleet-wide.
  **Decision**: gate the owning custody subject, and keep the account-wide
  refusal only for an unresolved effect that cannot be attributed to a
  subject, which genuinely is an account-level unknown. #1775 removes the
  class of stuck intent that triggered this incident but deliberately does
  not change the gate's scope.
- S12d hot-loop root cause (77% CPU at zero bots) — reproduce and profile.
- S9 drop cadence attribution (idle timeout vs competing listener) — run
  one bare listener with no fleet and time the drops.
- The moderate-table UI gaps: crash visibility (S3b), hold-state roster
  chips, staleness banner (S7), flatten discoverability + "wait"
  disposition (S17), retire path for dead legacy bots (S5/F16).
- Promote the session launch/stop/action tooling into `scripts/dev/`.

## 10. Artifact locations

Findings log, launch/churn/bench JSONLs, and all scripts:
session scratchpad (`fleet_launch.py`, `panel_action.py`,
`runner_stop.py`, `burst_deploy.py`, `churn_wave.py`, `read_bench.py`,
`findings.md`, `fleet_launch_results.jsonl`, `churn_results.jsonl`).
Clerk authority: `PythonDataService/artifacts/alpaca_clerk/accounts/alpaca/PA3KWXU1C4C3/clerk.db`.
Per-bot ledgers: `PythonDataService/artifacts/accounts/alpaca/paper:<sid>/source_bars.sqlite3`.
Prior study: `docs/audits/bot-launch-ops-study-2026-08-24.md`.
