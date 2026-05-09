# Audit — Codex Phases 1-4 on `overnight/runtime-2`

Reviewer: Claude Opus 4.7 (1M context)
Reviewed at: 2026-05-04 ~01:25
Commits audited: `6f2db1b` (P1), `c17211e` (P2), `1f925a0` (P3), `ffd92c0` (P4)
Wall-clock to land all four: 11 minutes
Test status reported by Codex: 5 + 6 + 4 + 5 = 20 new tests passing; 100 pre-existing SPY/broker tests still green.

## Summary

Codex hit every plan invariant. Strategy class, BacktestEngine, and the existing `app/broker/ibkr/*` boundary are unchanged. New code is layered cleanly above. **One real bug** that does not block the demo's replay-parity test but **does block Phase 10 (paper week)**.

## Bugs

### 1. `bars.py` reads `bar.open` but `ib_async.RealTimeBar` uses `bar.open_` — BLOCKS Phase 10

**Where:** `app/broker/ibkr/bars.py:82-83`

```python
def _decimal_attr(obj, name: str) -> Decimal:
    return Decimal(str(getattr(obj, name)))
```

Called as `_decimal_attr(bar, "open")` at lines 115. Confirmed via `.venv/Lib/site-packages/ib_async/objects.py:113`:

```python
@dataclass
class RealTimeBar:
    time: datetime = EPOCH
    endTime: int = -1
    open_: float = 0.0   # ← trailing underscore
    high: float = 0.0
    ...
```

**Why tests pass:** the test fake (`tests/broker/ibkr/test_bars.py:18-26`) uses `SimpleNamespace(open=Decimal(...))` — no underscore. Hits the `getattr(bar, "open")` path.

**Why production fails:** real ib_async bars expose `open_`. `getattr(real_bar, "open")` → `AttributeError`. The streamer would crash on the first 5-second bar after subscription.

**Impact:**
- Phase 6 replay-parity gate (the demo headline) — UNAFFECTED. Replay reads from a recorded fixture, not real IB.
- Phase 10 paper week — BROKEN. First real bar crashes the runner.

**Recommended fix (one-liner):** lookup table that prefers `open` then falls back to `open_`:

```python
def _decimal_attr(obj, *names: str) -> Decimal:
    for name in names:
        if hasattr(obj, name):
            return Decimal(str(getattr(obj, name)))
    raise IBKRBarStreamError(f"Bar missing all of: {names!r}")

# at call sites:
open_price = _decimal_attr(bar, "open", "open_")
```

The other RealTimeBar fields (`high`, `low`, `close`, `volume`, `time`) match what the test fake uses, so only `open` needs the dual lookup.

**Suggested:** flag this as the #1 follow-up after the demo. Patch is small enough to roll into Phase 10 prep.

## Nits (acceptable, but worth knowing)

### N1. `cancelRealTimeBars` not wrapped in try/except

**Where:** `app/broker/ibkr/bars.py:188`

```python
finally:
    client.ib.cancelRealTimeBars(bars)
    logger.debug("Cancelled reqRealTimeBars for %s", symbol)
```

If the connection drops mid-stream, `cancelRealTimeBars` could raise inside the generator's finally — propagating from the consumer's `aclose()`. Compare to `market_data.stream_option_chain:243-253` which wraps every cancel call. Not catastrophic; on drop, the gateway will release the line eventually. Worth aligning with the existing pattern for consistency.

### N2. `IBKRBarStreamError` extends `Exception`, not `BrokerError`

**Where:** `app/broker/ibkr/bars.py:26`

The rest of the broker module uses `BrokerError` as the common ancestor (`client.py:100-115`). Bar errors should too, so any caller catching `BrokerError` catches bar errors as well.

### N3. `tests/engine/live/test_live_context.py` imports `FakeBroker` from another test file

**Where:** `tests/engine/live/test_live_context.py:14`

```python
from tests.engine.live.test_live_portfolio import FakeBroker
```

The plan calls for `FakeBroker` to live in `tests/engine/live/fixtures/fake_broker.py` (which exists but is empty). Importing test classes across spec files is a code smell — when Phase 6 lands the heavier replay `FakeBroker`, the module-of-record should be `fixtures/fake_broker.py`.

### N4. `_to_utc_ms` accepts numeric epoch values via heuristic

**Where:** `app/broker/ibkr/bars.py:34-45`

The numeric path (`int`/`float` epoch with a "more than 10 billion → ms" heuristic) is dead code in normal flow — `ib_async.RealTimeBar.time` is always a `datetime`. Extra branch surface that future refactors will need to think about. Acceptable.

### N5. ~~Reference price update cadence~~ — RETRACTED

Initially flagged as a divergence. Re-checking against the Phase 5 commit (`f230859`), `LiveEngine.run` line 123 updates `portfolio.reference_price` per minute bar — the same cadence as `BacktestEngine.run` (`engine.py:225`). The consolidated-bar update in `LiveContext._on_emit` is an additional write, also matching `BacktestEngine`'s `base.py:108-115` flow. Cadences match. Disregard.

## Plan compliance — every checklist item

| Plan invariant | Status | Notes |
|---|---|---|
| Reuse `app/broker/ibkr/*` verbatim, no second wrapper | ✅ | New `IbkrBrokerAdapter` is a thin facade, not a new safety boundary |
| Only ONE new file in `app/broker/ibkr/`: `bars.py` | ✅ | `models.py` modified to add `IbkrMinuteBar`, allowed by plan §4 |
| `IbkrMinuteBar` carries `start_ms` (incl), `end_ms` (excl), Decimal OHLC | ✅ | `models.py:232-250` |
| `int64 ms UTC` everywhere | ✅ | `_to_utc_ms` enforces; naive datetime fails fast |
| Fail-fast on dup and non-monotonic 5-sec timestamps | ✅ | `bars.py:106-112` |
| `useRTH=True` honored | ✅ | tested at `test_bars.py:163` |
| `cancelRealTimeBars` called once on iterator exit | ✅ | tested |
| `LivePortfolio.set_holdings` uses consolidated bar close | ✅ | via `LiveContext._on_emit` writing `reference_price` on every fired consolidated bar |
| `set_holdings` math is `int(target_value / price)` | ✅ | matches `Portfolio.set_holdings:155-168` |
| Strategy class unchanged | ✅ | `algorithms/spy_ema_crossover.py` untouched |
| `BacktestEngine` unchanged | ✅ | `engine.py` untouched |
| `StrategyContext` unchanged | ✅ | `strategy/base.py` untouched |
| No new dependencies | ✅ | `requirements-light.txt`, `requirements-heavy.txt` untouched |
| `confirm_paper=True` on every order spec | ✅ | `live_portfolio.py:182` |
| Order ID monotonicity | ✅ | `_next_id` |
| `client_order_id="live-{N}"` | ✅ | `live_portfolio.py:183` |

## Phase 5 quick audit (commit `f230859`, 34 min wall-clock)

**Working but with three real plan deviations.** Smoke test passes (1 strategy, 1 entry). Real test of correctness is Phase 6.

### Plan deviations

1. **Single-task `async for` loop, not three concurrent consumers.** Plan §7 calls for an asyncio multi-consumer pattern (bar / order events / force-flat scheduler) feeding one strategy task via `asyncio.Queue`. Codex chose a simpler single-task `async for minute_bar in source` loop. For the replay case (deterministic `FakeBroker.advance_bar`), this works perfectly. For real-time IBKR, order events fired between minute-bar arrivals would only be drained on the next bar — a bounded-by-1-minute latency. Acceptable for v1; flag for real-time scaling.
2. **No force-flat barrier.** Plan §7 critical item #3 requires a force-flat scheduler that cancels open orders + market-flats positions + calls `strategy.on_force_flat()` at session close. Absent in `live_engine.py`. **For the SPY parity fixture, this does not matter:** inspecting `app/engine/tests/fixtures/spy_engine_next_bar_open_baseline.csv` (all 63 trades) shows every signal entry is mid-morning (12:01 once, 09:46 for the other 62), and every exit lands by 13:16 at the latest. The strategy never holds a position into the 15:55 force-flat window for this 2024-04 → 2026-03 fixture. The replay parity test should not trigger force-flat in either engine, so the missing barrier doesn't cause divergence here. **It does matter for Phase 10:** a real paper week has no such guarantee — a single late-day entry without force-flat would drift the runner across session close.
3. **No `[STEP X]` structured logging markers.** Plan §7 critical item #7. Logger calls exist but lack the `[STEP 1] CONNECT`, `[STEP 2] SUBSCRIBE`, etc. prefixes. Cosmetic — won't affect correctness.

### Plan compliance

- ✅ Per-minute equity snapshot — matches `BacktestEngine.run` line 375-382 (NOT per consolidated bar; my earlier note was wrong)
- ✅ Per-minute insight scoring (`step` called per minute) — matches `engine.py:372-373`
- ✅ Per-minute reference-price update — matches `engine.py:225`
- ✅ Final insight finalization at end-of-run — matches `engine.py:392+`
- ✅ Single-symbol guard — matches `engine.py:182-184`
- ✅ Eager paper-safety validation against connected client (`_validate_paper_client`)
- ✅ Order ID monotonicity preserved (delegated to LivePortfolio)

### `FakeBroker` parity vs default `FillModel`

| Field | FakeBroker | FillModel default | Match? |
|---|---|---|---|
| commission | `Decimal("1.00")` hardcoded | `Decimal("1.00")` (`fill_model.py:49`) | ✅ |
| slippage | `Decimal(0)` (no slippage applied) | `Decimal(0)` (`fill_model.py:50`) | ✅ |
| fill price | `bar.open` of next minute bar | `bar.open + slippage` of next bar (`fill_model.py:87-89`) | ✅ when slippage=0 |
| fill time | `bar.time` of next minute bar | next bar's logical time | ✅ |
| direction | `LONG` if BUY else `SHORT` | same | ✅ |

**However:** if the Phase 6 replay test instantiates `BacktestEngine` with a non-default commission or non-zero slippage, the parity fails because FakeBroker is hardcoded. Phase 6 must use defaults — verify when test lands.

## Phase 6 audit (commit `b532784`, 27 min wall-clock) — HARD GATE PASSES

**Demo headline confirmed.** Codex passes the parity gate at strict `Decimal("0")` tolerance. Reviewed `tests/engine/live/test_live_engine_replay.py`.

### Assertion coverage — every plan requirement is honored

| Plan §8 requirement | Implemented | Tolerance |
|---|---|---|
| Order count exact | ✅ `_assert_order_events_exact` | exact |
| Per-order: symbol, direction, fill_quantity, fill_price, fee, tag | ✅ each asserted with `diff == Decimal("0")` | exact |
| Order ID monotonicity within run | ✅ `submitted_order_ids == sorted(...)` | exact |
| Order ID uniqueness | ✅ `len(set(ids)) == len(ids)` | exact |
| Submit + fill timestamps within 1 ms | ✅ `abs(_ms_utc(actual) - _ms_utc(expected)) <= 1` | 1 ms (per plan) |
| Final cash, positions, total fees | ✅ all asserted with `diff == Decimal("0")` | exact |
| Equity curve per-snapshot | ✅ `_assert_equity_curve_exact` over timestamp ms, equity, cash, holdings_value | exact |
| `strategy.trade_log` per-trade exact | ✅ `_assert_trade_log_exact` over entry/exit time, prices, pnl_pts, pnl_pct, result, indicators | exact |
| Insight count + per-insight score | ✅ `_assert_insights_exact` via 16-tuple signature including final score | exact |
| Insight summary | ✅ `live.insight_summary == backtest.insight_summary` | exact |
| No open positions, no pending orders at end | ✅ asserted | exact |
| Force-flat fired iff backtest fired it | ✅ list-equality on per-event `tag == "ForceFlat"` flags | exact |

### Fixture choice

Codex used the in-repo `PythonDataService/lean-cache` (396,775 SPY minute bars) instead of the external `/sessions/.../Lean/Data` path that the older `test_spy_next_bar_open_validation.py` references. Reasonable — the external mount isn't present in this workspace.

The replay produced 162 order events / 81 trades — different from the older 63-trade baseline because it's a different data source. Both engines (BacktestEngine + LiveEngine) consume the **same** `LeanMinuteDataReader(LEAN_CACHE_ROOT)`, so whatever the actual window is, the parity test is well-formed.

### Force-flat verdict

The flagged risk from the Phase 5 audit (force-flat absence in LiveEngine) is **not triggered** by this fixture: every event's `tag == "ForceFlat"` value matches between the two engines (line 136-138). If neither engine emitted any force-flat event, this assertion is trivially true. Either way the parity holds for the demo. Force-flat absence remains a Phase 10 concern.

### Setup discipline

The test asserts `LEAN_CACHE_ROOT.exists()` before running (line 116) — fails fast with a clean message if the demo machine is missing the cache. Good demo hygiene.

## Phase 7 audit (commit `00a1e4e`, 9 min wall-clock)

**Passes; covers the requested failure mode but shallow.** Reviewed `tests/engine/live/test_live_engine_collapse.py`.

### What's exercised

`CollapsedLifecycleFakeBroker` records `["PendingSubmit", "Submitted", "Filled"]` internally but yields only `Filled` to LiveEngine. The test asserts:
- Exactly one `submitted_order_id` (the entry).
- The broker's internal status sequence has all three steps; the yielded sequence has only `["Filled"]`.
- `strategy.on_order_event` fires exactly once with the final fill.
- `result.order_events` has length 1.
- `strategy.events[0] == result.order_events[0]` (same event object content).
- Final `open_positions == {"SPY": 200}`, broker cash matches engine cash.

### Plan deviations

1. **Only entry-side collapse.** Plan §9 said "Repeat for the symmetric collapse on the exit order. Both directions matter." Codex did not add the exit-side test. For `SpyEmaCrossoverAlgorithm` the exit is a separate market order (Liquidate at 75-min timer), so the collapse case on the exit could in principle desynchronize position state. Adding the exit-side test is a 10-line follow-up.
2. **Test depth.** The test confirms LiveEngine handles a single `Filled` event correctly; it doesn't really exercise polling-based collapse semantics because LiveEngine's design (single-task `async for`, no state machine that depends on `Submitted` arriving before `Filled`) is collapse-resistant by construction. The test passes trivially given the design choice. Worth knowing — if the design were to grow a state machine later, the test wouldn't catch a regression.

### Verdict

Adequate for "we thought about real-world failure modes." Not a deep test, but defensible.

## Final state

| Item | Status |
|---|---|
| Phases 1-7 | All committed, all green per Codex (12 new live tests + 6 new bar tests + 100 pre-existing all passing) |
| Phase 8 (config + CLI) | Not started, deferred per plan |
| Phase 9 (reconciliation) | Not started, deferred per plan |
| Phase 10 (paper week) | Operational; gated on market-data subscription + Gateway login |
| Branch | `overnight/runtime-2` pushed to `origin/overnight/ibkr-paper-runtime-2026-05-04` |
| Open follow-ups | (1) `bar.open` → `bar.open_` patch (`docs/bars-open-attribute-fix.md`) before Phase 10. (2) Force-flat barrier in LiveEngine before Phase 10. (3) Exit-side collapse test as a 10-line addition. (4) `[STEP X]` structured logging in LiveEngine. (5) Move `FakeBroker` import in `test_live_context.py` to use `tests/engine/live/fixtures/fake_broker.py`. |

## Demo readiness

✅ The headline claim — "live runtime produces identical trades to backtest under controlled conditions" — is true and proven by `test_live_engine_replay.py` at `Decimal("0")` tolerance.

Demo flow + Q&A prep at `docs/archive/handoffs/demo-2026-05-05.md` (archived).
