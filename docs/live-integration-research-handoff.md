# Live IBKR Integration — Research Handoff

**Purpose.** This document audits the failure modes in our live IBKR
paper-trading integration and reframes each as a **self-contained research
brief** you can paste into Claude's deep-research tool to find industry best
practices. Each brief is written to stand alone (the research tool has no
access to this repo), so it restates the relevant context before asking its
questions.

**Status of the integration (2026-06-04):** not production-ready. The
deployment-validation canary has produced **zero clean (`exit_reason: normal`)
sessions in the last 8 runs across 4 days** — every run ended in `exception`,
`fatal_halt`, or `POISONED`. Three distinct failure modes were hit this week;
fixes exist for all three but none has yet produced a clean live session.

---

## 0. System context (read once; the briefs assume it)

- **Stack:** Angular 21 / .NET 10 GraphQL / Python FastAPI (the trading engine)
  / Postgres. The live trading engine is ~10.8k lines of Python under
  `PythonDataService/app/engine/live/`.
- **Broker link:** IBKR Gateway (paper, port 4002) reached via **`ib_async`**.
  A separate `ib-gateway-docker` container runs the Gateway headless.
- **Process topology:** a **host daemon** (`app.engine.live.host_daemon`)
  spawns one **live-runner process per strategy instance**; a containerized
  FastAPI service (`polygon-data-service`) serves data + holds its own IBKR
  connection for broker queries. They communicate host↔container over
  `host.containers.internal`.
- **Identity ladder:** `strategy_key` → `strategy_instance_id` (owns an
  `ib_client_id` and a `bot_order_namespace`) → `run_id` (one process
  lifetime). Order ownership is *supposed* to be keyed on `bot_order_namespace`,
  reconstructed from the namespace-attributed order/execution trail — **not**
  the current process.
- **Order id scheme today:** orders are placed with `client_order_id =
  f"live-{ibkr_order_id}"` (the ephemeral per-clientId IBKR orderId), **not**
  the stable `permId` and **not** a namespaced `orderRef`. This is the root of
  several problems below.
- **The canary:** `deployment_validation` — a deliberately trivial strategy
  (enter after two consecutive green 1-min bars, exit on the 3rd held bar,
  flatten 15:45 ET). Its only job is to prove the pipeline can complete one
  clean, reconciled session before a real strategy is deployed. "Consecutive
  green" = consecutive clean validation sessions; we currently have zero.

---

## 1. The checks / guards in the integration (the audit)

The engine defends itself with a layered set of guards. Each is listed with
what it does and the **specific weakness** observed.

### 1.1 Intra-day fatal-halt triggers (`app/engine/live/halt.py`, spec §7.1)

- **`OUTSIDE_MUTATION`** — fires when the broker reports a fill whose
  `(execId, permId)` is not linked to a Python-owned `client_order_id`
  (a manual TWS click, a different clientId, a stuck prior-session order).
  - *Weakness:* ownership is computed from the **current process's** in-memory
    order map (`owned_client_order_ids = {f"live-{oid}" for oid in
    self._order_meta}`). On a same-account relaunch, IBKR replays the day's
    executions (including a *prior run's* recovery-flatten fills); the new
    process doesn't recognize them → poison. Pre-session floor (PR #443) only
    covers fills *before* session start. A permId-trail fix is in flight
    (PR #445, **unmerged**).
- **`LOST_FILL`** — fires when an owned order has no matching execution within
  its fill window (next-bar-open + slack) or is still unfilled at EOD.
  - *Weakness:* window/slack tuning and behavior under late/duplicate fills is
    not validated against real IBKR latency.
- **`COLD_START_DIVERGENCE`** — the cold-start reconciler refuses to resume
  (corrupt sidecar, unreachable broker, unexpected/missing broker orders).
  - *Weakness:* **no offline/degraded path** — any broker-query exception is
    treated as poison ("no verified resume").
- **`OPERATOR_DECLARED`** — operator `MARK_POISONED`.

### 1.2 Cold-start reconciler (`cold_start_reconciler.py`, Resolution 2)

Queries open orders + executions **by namespace** at boot; recovers unflushed
fills; classifies `SafeToResume | Poisoned`. Correctly namespace-scoped. Depends
on the sidecar's `submitted_orders` / `known_exec_ids` / `known_perm_ids` trail
being complete — which the recovery-flatten path historically did not populate.

### 1.3 Pre-flight checks (`pre_flight.py`, before trading)

`check_clean_tree`, `check_ntp_offset`, `check_unexpected_position`,
`check_run_state_intact`, `check_no_halt_flag`, `check_yesterday_artifacts_valid`.
- *P0 bug:* `check_unexpected_position` (`pre_flight.py:210`) is **account-net,
  not namespace-aware** — a sibling managed instance's position is flagged as
  foreign contamination, blocking valid starts (per `CONTEXT.md`).

### 1.4 Readiness gates (`readiness.py`, ADR 0005)

`desired_state`, `broker_connection`, `poison_sentinel`, `session_window`,
`orders_cap`, plus `data_provenance` (soft). Engine-authored; collapsed to
`READY | BLOCKED | DEGRADED | UNKNOWN`. Sound design; the inputs depend on the
guards above being correct.

### 1.5 Recovery flatten (`run.py` unhandled-exception path)

On a crash, flattens open positions directly via the broker — **outside** the
engine's order bookkeeping, after the event stream has stopped. Historically
recorded `perm_id=None` (IBKR hasn't assigned it at `PendingSubmit`), so the
next run couldn't recognize the replayed recovery fill → relaunch poison.

### 1.6 Bar ingestion

Duplicate IBKR 5-second bar redelivery crashed a run (PR #444, merged, not yet
proven live). Idempotent bar handling is the general concern.

### 1.7 Operational / healthcheck

`polygon-data-service` is single-process async, also holding a live IBKR
connection; heavy synchronous endpoints (Polygon range export, large backtests)
starve the asyncio loop for seconds, exceeding the 5s healthcheck timeout →
flapping unhealthy. It also runs `uvicorn --reload` (a live-trading footgun:
any watched file change restarts the worker mid-session).

---

## 2. Research briefs for Claude's deep research tool

Paste any brief below verbatim into the deep-research tool. They are ordered by
leverage. Each ends with "what a good answer contains" so you can judge the
output.

---

### R1 — Order/fill ownership & reconciliation across process restarts (IBKR / ib_async)

> I run an automated trading system against Interactive Brokers (paper, via the
> `ib_async` Python library). Each strategy instance owns a dedicated IBKR
> `clientId`. When a strategy process crashes and a new process restarts on the
> **same account and clientId mid-session**, IBKR replays the trading day's
> prior executions on connect. My system has a guard that fatal-halts if it
> sees a fill it doesn't recognize as its own ("outside mutation" detection),
> and it currently identifies "its own" orders by the ephemeral per-session
> `orderId`, which does not survive a restart.
>
> Research the **best-practice way to establish durable order/fill ownership in
> IBKR live systems** so a restarted process correctly recognizes its own prior
> orders (including orders placed by a previous process of the same instance)
> and distinguishes them from genuine third-party account activity. Specifically:
> 1. The correct stable identifier(s): `permId` vs `orderId` vs a namespaced
>    `orderRef` / `clientId` tag — when each is assigned, what survives reconnect
>    and restart, and how production systems key ownership.
> 2. How mature systems persist an order/execution ledger and reconcile it
>    against IBKR's connect-time execution replay (`reqExecutions` /
>    `execDetails`) without false "foreign fill" alarms.
> 3. Handling executions that arrive between a fill and the durable flush
>    (crash window).
> 4. How to scope contamination detection when multiple clientIds/strategies
>    share one account.
>
> What a good answer contains: a clear recommendation on the ownership key,
> the IBKR-specific identifier lifecycle, a reference reconciliation algorithm,
> and known pitfalls (orderId reuse across sessions, permId assignment timing).

---

### R2 — `ib_async` order lifecycle & identifier timing (primary-source deep dive)

> Using the `ib_async` (and its predecessor `ib_insync`) library against
> Interactive Brokers, I need authoritative detail on the **order lifecycle and
> when each identifier becomes available**, because I rely on capturing the
> stable `permId` synchronously after placing an order.
>
> Research and document:
> 1. After `ib.placeOrder()` returns a `Trade`, when is `trade.order.permId`
>    populated? Is it `0`/unset at `PendingSubmit`, and which event
>    (`openOrder` / `orderStatus`) back-fills it, with typical latency on paper
>    vs live?
> 2. The correct idiom to **await permId assignment** (event-based vs polling
>    `await ib.sleep()`), and whether a bounded wait is safe in an async app
>    that also services other coroutines.
> 3. `execDetails` / `commissionReport` ordering and whether `execId`/`permId`
>    on a fill are stable across reconnects and across a new clientId session.
> 4. Reconnect semantics: what IBKR replays on connect, and how `ib_async`
>    surfaces it.
>
> What a good answer contains: citations to ib_async/ib_insync docs and IBKR
> API docs, a definitive statement on permId timing, and a recommended
> await-permId pattern.

---

### R3 — Crash recovery & idempotent flatten in live trading

> My trading engine, on an unhandled exception, runs a "recovery flatten" that
> cancels open orders and liquidates positions, then exits. This runs outside
> the normal engine bookkeeping. I want **best practices for crash recovery in
> automated trading**:
> 1. Idempotent flatten: ensuring a crash *during* recovery, or a restart that
>    re-runs recovery, cannot double-liquidate or leave a partial state.
> 2. Whether to flatten-on-crash at all vs. resume-and-reconcile, and how
>    serious live systems decide.
> 3. A durable "order intent log" / write-ahead pattern so every order
>    (including recovery orders) is recorded *before* it reaches the broker, and
>    reconciled on restart.
> 4. Crash-consistency for the state file (atomic write, fsync, recovery on
>    partial write).
>
> What a good answer contains: the resume-vs-flatten tradeoff with criteria,
> an idempotency design (e.g. deterministic clientOrderId/orderRef), and a
> write-ahead-log pattern for orders.

---

### R4 — Async service isolation: don't block the event loop that holds a live broker connection

> I have a single Python asyncio service (FastAPI + `ib_async`) that both serves
> data API endpoints (some doing heavy synchronous Polygon fetch + parse + zip)
> and holds a live IBKR broker connection on the same event loop. Heavy
> synchronous endpoints stall the loop for several seconds, which (a) trips the
> container healthcheck and (b) pauses the live broker connection's processing.
>
> Research best practices for **isolating CPU/IO-heavy work from a latency-
> sensitive asyncio event loop that maintains a live market/broker connection**:
> 1. `run_in_executor` with thread vs **process** pools and the GIL implications
>    for CPU-bound work.
> 2. When to split heavy/batch work into a **separate worker service or process**
>    vs. keeping it in-process.
> 3. Healthcheck design for async services (timeouts, separate liveness vs
>    readiness, not co-locating heavy work behind the health endpoint).
> 4. Whether running a live trading connection in the same process as a web API
>    is advisable at all, and common topologies that separate them.
>
> What a good answer contains: a recommended process topology, concrete
> guidance on executor choice for CPU-bound vs IO-bound work, and healthcheck
> patterns for event-loop-starvation detection.

---

### R5 — "Consecutive green" deployment-validation gating for algo trading go-live

> Before deploying a real strategy, I run a trivial "canary" strategy live on
> paper and want it to pass a **deployment-validation gate** — currently framed
> as N consecutive clean sessions. I need best practices for **go-live
> validation of automated trading systems**:
> 1. What acceptance criteria mature shops use before promoting a strategy from
>    paper to live (clean sessions, reconciliation tolerance, replay/backtest-
>    parity, latency SLOs, error budgets).
> 2. How many consecutive clean sessions is meaningful, and what "clean" should
>    formally include (no halts, flat at EOD, broker-vs-engine position
>    reconciliation within tolerance, no unexplained fills).
> 3. Staged rollout patterns (shadow/read-only → 1 share → full size) and
>    automated promotion/demotion gates.
> 4. The role of a canary strategy vs. testing the real strategy directly.
>
> What a good answer contains: a concrete go-live checklist, a definition of a
> "clean session," and a staged-rollout rubric.

---

### R6 — Outside-mutation / account-contamination detection with shared accounts

> Multiple automated strategies (each its own IBKR `clientId`) plus occasional
> manual trades can hit the **same IBKR account**. I need to detect genuine
> "contamination" (activity outside any managed strategy) without false alarms
> from sibling strategies.
>
> Research best practices for:
> 1. Per-strategy position/fill attribution from a shared account snapshot
>    (which is net reality, not an ownership ledger).
> 2. Computing a "residual / unattributed" bucket = account net − Σ managed
>    expected positions, and how to act on it.
> 3. Whether to block all starts on a dirty account vs. degrade-and-warn.
> 4. Using `orderRef`/`clientId` tagging to make attribution unambiguous.
>
> What a good answer contains: an attribution algorithm, a residual/contamination
> policy, and the tagging scheme that makes it robust.

---

### R7 — Idempotent real-time bar ingestion (IBKR 5-second / minute bars)

> IBKR real-time bar streams can redeliver bars (duplicate 5-second bars,
> late/out-of-order delivery), which crashed my consolidator. Research best
> practices for **idempotent, duplicate-safe bar ingestion** in live trading:
> dedup keys, watermarking/late-bar handling, consolidating 5s→1min→15min
> deterministically, and reconciling streamed bars against a historical source
> for gap detection.
>
> What a good answer contains: a dedup/watermark design and a deterministic
> consolidation approach robust to redelivery.

---

### R8 — Durable single-source-of-truth for live order/position state

> My engine keeps live state in an on-disk sidecar (submitted orders, known
> exec ids, expected positions). I want best practices for the **durable state
> store of a live trading engine**: schema for an order/execution ledger,
> crash-consistent persistence (atomic write + fsync + recovery), reconciliation
> against the broker as source of truth on restart, and event-sourcing vs.
> snapshot tradeoffs.
>
> What a good answer contains: a recommended ledger schema, a persistence/
> recovery pattern, and reconciliation-on-restart guidance.

---

## 3. Our specific open questions to verify (not general research — our system)

These need *our own* validation, not literature; listed so they aren't lost:

1. **Does `trade.order.permId` actually populate within ~2s on our paper
   gateway?** The relaunch fix (PR #445) assumes yes; only fake-broker tested.
   → forced crash → recovery-flatten → same-account relaunch dry-run.
2. **Did PR #443 (pre-session floor) and PR #444 (dup-bar) actually run in the
   failing sessions, or were those runs on pre-fix code?** Confirm the running
   daemon/container code matches `master`.
3. **Is `check_unexpected_position` P0 (account-net) still blocking starts** when
   sibling instances hold positions?
4. **Achieve ≥1 (ideally several consecutive) clean `deployment_validation`
   sessions** on the merged+deployed code. Currently zero.

---

## 4. Deploy/readiness state (as of handoff)

- PR #445 (relaunch permId fix + replay-gate hardening): **OPEN, unmerged,
  unreviewed**. Branch `fix/relaunch-permid-and-replay-coverage-gate`.
- Host daemon: **not running**. Container: healthy now (flaps under heavy load).
- Full live-engine test suite: 590 passed, 4 skipped, 0 failed.
- Replay parity gate: green after backfilling the SPY LEAN cache
  (window now 501/501 NYSE sessions).
