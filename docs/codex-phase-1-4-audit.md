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

## What to watch in Phase 6-7

1. **Phase 6 (HARD GATE)** — `atol=Decimal("0")`. If Codex loosens to even `Decimal("0.01")` to make the test pass, that's a regression. Cent tolerance is reserved for real-broker reconciliation only.
2. **Phase 6** — assertions must include `strategy.trade_log`, insight count + per-insight score, equity curve per-snapshot, force-flat fired iff backtest fired it. The force-flat assertion is the one most likely to break given Phase 5's gap.
3. **Phase 6** — verify `BacktestEngine` is configured with **default** `commission_per_order=$1.00` and `slippage_per_share=$0`. Anything else and the FakeBroker will diverge.
4. **Phase 6** — the test fixture window. If it contains a signal whose exit crosses 15:55, the missing force-flat will cause divergence. Expect Codex to either (a) bound the fixture to a no-force-flat window, (b) skip the force-flat assertion, or (c) add force-flat to LiveEngine. (a) is the most likely path; (c) is the right path.
5. **Phase 7** — both entry-side and exit-side collapse cases.
