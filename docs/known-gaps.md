# Known Gaps — Living Open-Defect Backlog

**Purpose.** One place that answers "what is still broken or deferred?" for an AI
agent or operator. This is the *only* durable home for open defects; the
point-in-time audit-finding files they came from (`docs/audits/auto-research/findings/`,
`docs/audits/vibe-coded-app-research/findings/`, `architecture-investigation-2026-07-02.md`,
and the auto-research run logs) were deleted on **2026-07-04** after their open
items were lifted here. The closed findings live in git history and in the
auto-research ledger (`docs/audits/auto-research/state.json`).

**Status convention.** Each item carries a severity and a code pointer captured
**as of 2026-07-04** — verify the `file:line` against current code before acting,
since the tree moves. When an item is fixed, delete its bullet (git history is the
record). When a new open defect is found, add it here rather than starting a new
finding-file tree.

**Scope note.** Safety-critical and broker items below were verified open against
current code on 2026-07-04. The architecture-investigation P1 tier and the
run-log functional items were **not** re-verified in that pass — confirm before
committing effort.

---

## 1. Safety-critical (verified open against current code)

No verified-open items remain in this section.

## 2. Architecture-investigation P1 tier (survives; not re-verified 2026-07-04)

All five P0 safety issues from `architecture-investigation-2026-07-02.md` were
verified **fixed** in current code (unauth data plane now binds `127.0.0.1` +
HMAC control secret; panic-flatten stamps `order_ref`; recovery-flatten re-fetches
positions; freeze is clearable via `account_recovery_cli.py clear-freeze`;
IntentWal truncates its tolerated tail before append). The remaining P1s
carried forward are:

- Offline reconciliation/report bundle writers still publish Parquet and their
  companion JSON/hash files non-atomically. Live run artifacts, live bar
  compaction, and broker tick partitions use atomic publication; the remaining
  report-bundle work is research-output integrity rather than control-plane
  safety.
- No R3 recovery daemon.
- Residual: committed dev-default control secret `local-dev-control-secret`
  (fine for local; must not reach a shared/live host).

## 3. Broker subsystem (2026-06-07 bug-hunt — confirmed, never filed; not re-verified)

Ten confirmed IBKR-adapter bugs surfaced by the 2026-06-07 hunt that were never
converted into findings. The disconnect-blindness cluster (B-02/03/04/08) now
appears addressed in `broker/ibkr/client.py` (~:335–655, codes 1100/1101/1102/504
handled) — confirm closed, then drop. Remaining:

- **B-05** `cancel_paper_order` / `_order_belongs_to_account` match by `orderId`
  only → can cancel a *foreign* order on the same DU account; ownership check
  should be `account_id AND client_id` (`orders.py` ~:385–423). *(also VCR-P3-H)*
- **B-06** `place_paper_order` awaits `qualifyContractsAsync` with no timeout on
  the live submit hot path (`orders.py` ~:243–263).
- **B-09** Partial-fill events mis-stamp running totals.
- **B-10** `Ticker.time` → ms without a naive-datetime guard (timestamp-rigor
  violation, `market_data.py` ~:137–141).
- **B-11** Unguarded `cancelRealTimeBars` in a `finally` masks the real exception.
- **B-12** `bid_size` / `ask_size` leak IBKR `-1` "no size" sentinel to callers.
- **B-13** Option-chain endpoints accept non-positive `expiry_ms`.

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
- **Auto-clear of guards after clean broker recovery** — recovery keeps the
  engine `PAUSED` with operator-only resume; decide which guard states a clean
  recovery receipt may auto-clear vs. which stay manually acknowledged.

## 5. Daemon diagnostics — deferred phase-2 features

Shipped (ADR-0019, PR #910). Deferred, non-safety:

- Deploy/start last-error catalog via persisted `mutation_attempts`.
- clientId-collision detection via broker events.
- Logs / incidents link-outs; deep WAL / readiness checks.
- Account-level diagnostic rollup (`scope_ref` is per `strategy_instance_id` today).

## 6. Numerical-rigor & frontend debt (deferred, P2)

- **Golden-fixture coverage gap** — most canonical math still lacks a registered
  golden fixture; the `iv30/` snapshot sits outside manifest governance.
  *(was F-0026; deferred in `auto-research/state.json`)*
- **Frontend naive `new Date(string)` — Tier 2** — date-only params are still
  parsed browser-locally. The data-integrity Tier-1 case was fixed producer-side;
  Tier-2 is cosmetic-display risk. *(was F-0034)*
- **`FailureRow.ts_ms` mislabel** — a host-local time string is typed/named as
  `ms-UTC`; rename to `ts_local` and convert at ingestion. *(was VCR-P3-K)*

## 7. Functional findings parked in deleted run logs (not re-verified)

- **`exposure_pct` unit bug** — `bars_held_total` mixes 15-min strategy bars with
  a 1-min equity curve. Build-Alpha features **F6** (noise/robustness) and **F8**
  (parameter sensitivity) are unimplemented. *(2026-05-07 build-alpha run)*
- **ML-V-001** — Phase 3.0/3.5 canonical math not registered in
  `docs/math-sources-of-truth.md`. **ML-V-002** — provenance blocks missing on
  `research/parity/qc_reconciler.py` and the prediction-set `artifact.py`.
  *(2026-05-12 ML-predictions run)*
