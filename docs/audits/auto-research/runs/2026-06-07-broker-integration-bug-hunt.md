# Broker integration bug hunt — 2026-06-07

**Mode:** ad-hoc multi-agent bug hunt (outside the auto-research-tick state machine; user-authorized).
**Scope:** `PythonDataService/app/broker/ibkr/` (11 modules) + `app/routers/broker.py`, `app/engine/live/no_submit_broker_adapter.py`, `app/engine/live/bar_adapter.py`.
**Method:** 8 review dimensions fanned out in parallel (dedup-policy, order-lifecycle, timestamps, concurrency, error-reconnect, persistence-recovery, pnl-account, data-contracts-models). Every raw finding was handed to an independent adversarial verifier that read the cited code and tried to refute it (default = not-a-bug when uncertain).
**Read-only.** No code, tests, or fixtures were modified.
**Result:** 21 raw findings → **16 confirmed** after verification → **13 unique** after merging cross-dimension duplicates. 29 agents, ~1.6M tokens, ~34 min.

> ⚠️ Suggested fixes below are NOT applied. They are recommendations for human review.

## Severity tally

| Severity | Count | Bugs |
|---|---|---|
| P1 | 4 | idempotency race; bar-stream silent hang; order-event stale-poll; errorEvent ignores disconnect codes |
| P2 | 4 | cancel ownership guard; qualify no-timeout; parquet non-atomic+race; PnL stale-stream |
| P3 | 5 | partial-fill mis-stamp; naive Ticker.time; unguarded cancel in finally; size sentinel leak; expiry_ms unvalidated |

## Headline theme: mid-session disconnect is invisible

Five of the findings (B-01-disconnect-related: bars hang, order-event stale, PnL stale, errorEvent-ignored, unguarded-cancel) share one root cause: **nothing in the broker subsystem detects a mid-session Gateway disconnect.** No `disconnectedEvent` hook is registered anywhere; `_on_ib_error` drops connectivity codes 1100/1101/1102/504; and every streaming loop (`stream_minute_bars`, `stream_order_events`, `stream_account_pnl`, `stream_position_pnl`) checks `is_connected()` only once at entry, then polls a frozen in-memory cache forever. On a nightly Gateway restart or network blip, the run keeps reporting "running" while blind — bars stop, fills are missed, PnL freezes, force-flat never fires, possibly with an open position. **This is the single most important cluster to fix.**

---

## P1 findings

### B-01 — Idempotency cache TOCTOU race places duplicate orders
- **File:** `app/broker/ibkr/orders.py:233-296` (cache helpers 44-56); endpoint `app/routers/broker.py:538-547`
- **What:** `place_paper_order` reads `_IDEMPOTENCY_CACHE` (line 233), `await`s `qualifyContractsAsync` (line 243, a yield point), calls `placeOrder` (263), and only stores the ack at line 296. No lock spans that window. Two coroutines with the same `client_order_id` both miss the cache, both qualify, both place — defeating the documented idempotency guarantee.
- **Trigger:** frontend double-click or .NET retry-on-timeout firing two `POST /api/broker/orders` with the same `client_order_id` inside the ~tens-to-hundreds-of-ms qualify window → two real paper orders for one intended order; second ack overwrites the first so retries surface only one `order_id`.
- **Fix:** guard lookup→place→store with a per-`client_order_id` `asyncio.Lock`, or reserve the key with a placeholder/future before the first `await` and have concurrent callers await the in-flight placement. (Found independently by both order-lifecycle and concurrency dimensions.)

### B-02 — Bar stream hangs forever on mid-stream disconnect
- **File:** `app/broker/ibkr/bars.py:299-315`
- **What:** the poll loop only checks `index >= len(bars)` then `await asyncio.sleep(0.1); continue`. No `is_connected()` / connectivity-event check. When Gateway drops, ib_async stops appending; the loop spins forever yielding nothing and raising nothing. `live_engine._next_bar_or_shutdown` races the source only against the shutdown event, so the engine never returns and §7 halt checks never run.
- **Trigger:** Gateway auto-restart at 14:05 on a live SPY run → zero bars to close, `force_flat_at` never evaluates, operator sees "running" but frozen with an open position.
- **Fix:** check `client.is_connected()` (or subscribe to `disconnectedEvent` / error codes 1100/1101/1102/504) inside the loop and raise `IBKRBarStreamError`; optionally a staleness watchdog.

### B-03 — Order-event poll loop keeps reading a stale `trades()` cache after disconnect
- **File:** `app/broker/ibkr/orders.py:530-552`
- **What:** `stream_order_events` checks connection once at entry then `while True` polls `client.ib.trades()` (an in-memory cache that never raises when disconnected). Post-disconnect no transitions are detected, so it yields nothing forever and `_broker.stream_failure` stays `None` — the engine's only order-feed disconnect detector (`_raise_if_event_stream_failed`) is inert exactly when its docstring says it matters.
- **Trigger:** order PreSubmitted, Gateway drops, order fills server-side → fill never surfaced; engine believes order still pending and keeps submitting → silent portfolio/broker desync.
- **Fix:** call `client.require_connected()` (raising) at the top of each poll iteration.

### B-04 — `errorEvent` handler ignores all runtime connectivity codes
- **File:** `app/broker/ibkr/client.py:167-170`
- **What:** `_on_ib_error` reacts only to code 326 (clientId-in-use); every other code is discarded — no log, no counter, no flag. Connectivity-lost codes 1100/1101/1102/504 vanish. On a soft 1100 ("link to IB lost") the API socket stays open, so `isConnected()` keeps returning True and even `/api/broker/health` and `/diagnose` report "connected" while bars stop flowing.
- **Fix:** handle connectivity codes explicitly — structured log + observable counter + a degraded/lost flag (or hook `disconnectedEvent`) that streams and health endpoints consult.

---

## P2 findings

### B-05 — `cancel_paper_order` can cancel a foreign order on the same DU account
- **File:** `app/broker/ibkr/orders.py:385-423`; endpoint `app/routers/broker.py:563-578`
- **What:** cancel matches purely by `int(t.order.orderId) == order_id` across all `ib.trades()` and never applies `_order_belongs_to_account` — the guard the same module uses in `list_open_orders` (372) and `stream_order_events` (534). `reqAllOpenOrdersAsync` populates the trades cache with orders from other clients / manual TWS on the same account; `orderId`s are small per-client integers that can collide.
- **Trigger:** `DELETE /api/broker/orders/1234` for an id that belongs to another client on the same DU account → silently cancels someone else's order.
- **Fix:** after resolving the trade, reject unless `_order_belongs_to_account(trade, account_id)`.

### B-06 — `place_paper_order` awaits `qualifyContractsAsync` with no timeout
- **File:** `app/broker/ibkr/orders.py:243-263`
- **What:** the qualify await is unbounded (only the optional perm_id poll is bounded). It sits inline on the live submit hot path (`live_engine.py:662` → `submit_pending_orders` → `place_paper_order`). On a half-open connection it hangs the single bar-processing coroutine, so bars stop draining and per-iteration halt checks never fire. Violates python.md "Timeouts on all external calls"; the codebase already wraps `cancel_open_orders` in `asyncio.wait_for` (`live_engine.py:1350`), making this an omission.
- **Fix:** wrap awaited broker calls in `asyncio.wait_for(...)` and raise `BrokerError` on timeout.

### B-07 — Parquet partition writer is non-atomic AND races concurrent writers
- **File:** `app/broker/ibkr/persistence.py:161-174` (callers: tick 146, account 252, pnl 333)
- **What:** `_write_parquet_partition` does read→concat→`to_parquet(out_path)` directly on the live file — no tmp+rename, no lock — and is dispatched via `asyncio.to_thread` (real OS-thread parallelism). Two failure modes in one function:
  - **Crash non-atomicity:** SIGKILL/OOM mid-write truncates the day's file; next flush's `pd.read_parquet` raises, rows re-buffer, every subsequent flush re-fails on the same corrupt file → whole day's archive unreadable + unbounded in-memory buffer growth.
  - **Concurrent lost update:** two SSE consumers of the same account both read N rows, both overwrite → one writer's appended rows silently lost, no exception, retry buffer never engages.
  - Diverges from the established tmp+fsync+`os.replace` contract used by `live_state_sidecar`, `desired_state`, `indicator_state`, etc.
- **Mitigating:** all three writers are behind opt-in `persist_*` flags (default OFF); data is an archival side-stream, not the canonical trading path — hence P2 not P1.
- **Fix:** write to a sibling `.tmp` + fsync + `os.replace`; serialize per output path with a process-wide/per-path lock (or unique-per-flush filenames merged on read).

### B-08 — Account/position PnL streams emit stale frozen values after disconnect
- **File:** `app/broker/ibkr/pnl.py:94-105, 143-161`
- **What:** both streams subscribe once to a `PnL`/`PnLSingle` object ib_async mutates in place, then loop re-reading it with a fresh `ts_ms` each debounce and no `is_connected()` check. Post-disconnect the object stops updating but the loop keeps yielding plausible, freshly-timestamped, frozen P&L.
- **Trigger:** operator watches `/api/broker/pnl/stream`; Gateway drops at 11:00; dashboard shows the 10:59:59 unrealized P&L forever, so a position moving against the account after the drop is invisible. Stale ticks also get persisted.
- **Fix:** guard each iteration with `if not client.is_connected(): raise BrokerError(...)` (or tear down on `disconnectedEvent`).

---

## P3 findings

### B-09 — Partial-fill events mis-stamp running totals
- **File:** `app/broker/ibkr/orders.py:457-497, 544-550`
- **What:** when ≥2 executions land in one 0.5s poll window, each `_fill_to_event` reads `fill_quantity`/`last_fill_price` per-execution but `cumulative_filled`/`remaining`/`avg_fill_price` from the single final `orderStatus` snapshot. The first collapsed fill event carries the order's terminal totals, not the values true right after that execution.
- **Contained today** (engine math uses per-fill price/shares; lost-fill halt only needs some row with `remaining==0`; UI reads totals off the order, not the event) — hence P3.
- **Fix:** compute per-fill `cumulative_filled` by summing `exec_obj.shares` over `fills[:i+1]`, derive `remaining`, source price per execution.

### B-10 — `Ticker.time` → ms without naive-datetime guard
- **File:** `app/broker/ibkr/market_data.py:137-141`
- **What:** `ts_ms = int(t.timestamp() * 1000)` with no tz check. A naive datetime is interpreted as process-local time, producing an int64-ms value off by the UTC offset, written to `IbkrOptionQuote.ts_ms` (wire + parquet). Inconsistent with `bars.py::_to_utc_ms` (66-69), which rejects naive datetimes. Violates timestamp-rigor (int64 ms UTC; no silent tz coercion).
- **Fix:** reuse `bars._to_utc_ms` — reject naive, else `astimezone(UTC)`. (Found by both timestamps and data-contracts dimensions.)

### B-11 — Unguarded `cancelRealTimeBars` in `finally` masks the real exception
- **File:** `app/broker/ibkr/bars.py:316-323`
- **What:** the `finally` calls `cancelRealTimeBars(bars)` as its first statement with no try/except (every other cancel path — market_data, pnl ×2 — guards it). If the stream is unwinding on a real error AND the connection is dead, the cancel's `ConnectionError` replaces the original exception (sending operators chasing the cancel) and skips the diagnostic counters log.
- **Fix:** wrap the cancel in try/except (debug-log); keep the counters log outside the guard so it always runs.

### B-12 — `bid_size`/`ask_size` leak IBKR's negative "no size" sentinel
- **File:** `app/broker/ibkr/market_data.py:105-110, 151-152`
- **What:** prices route through `_coerce_quote` (maps NaN **or negative -1 sentinel** → None), but sizes get only a NaN check and ship as `int(size)`. A `-1.0` "no size" sentinel becomes `bid_size=-1` on the wire/parquet; `IbkrOptionQuote.bid_size/ask_size` are `int | None` with no `ge=0`. Size-weighted liquidity logic reads `-1` as real depth.
- **Fix:** apply the same negative+NaN→None coercion to sizes, and/or add `ge=0`.

### B-13 — Option-chain / strikes endpoints accept non-positive `expiry_ms`
- **File:** `app/routers/broker.py:353-378, 384-414`
- **What:** `expiry_ms` is `Annotated[int, Query(...)]` with no lower bound; handlers validate strikes but not `expiry_ms`. `expiry_ms=0` flows into `expiry_ms_to_yyyymmdd` → `"19700101"`, which matches no expirations → HTTP 200 with empty strikes (silent) or a confusing downstream "could not qualify". Violates "validate inputs at system boundaries."
- **Fix:** `Query(..., gt=0)` (ideally a recent-epoch floor) on both endpoints.

---

## Refuted / dropped (5)
Five raw findings were dropped by the adversarial verifiers as false positives or already-guarded paths (e.g. claims contradicted by existing guards, dead paths, or misreads). They are intentionally not listed here.

## Suggested next steps (ordered)
1. **Disconnect detection (B-02, B-03, B-04, B-08, partial B-11):** register a `disconnectedEvent` hook + handle codes 1100/1101/1102/504, and add an in-loop `is_connected()`/staleness check to all four streaming loops. One coherent change closes the highest-impact cluster.
2. **B-01 idempotency lock** — cheap, high-value correctness fix; ship with a concurrent-duplicate regression test.
3. **B-06 qualify timeout** and **B-05 cancel ownership guard** — small, localized.
4. **B-07 parquet atomicity+lock** — only if/when the `persist_*` flags are turned on; otherwise track as known.
5. P3s (B-09, B-10, B-12, B-13) — fold into the next broker PR; B-10 is a one-liner reusing `_to_utc_ms`.
