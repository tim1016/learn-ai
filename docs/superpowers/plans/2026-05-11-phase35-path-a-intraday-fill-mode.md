# Phase 3.5 Path A — Intraday-trigger fill mode implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Phase 3.0 `xfail` acceptance test against QC's AAPL precomputed-predictions tutorial by adding `FillMode.NEXT_SESSION_OPEN` and `PredictionRef.lookup="next_after_bar_close"` to the engine and spec layer, then re-capturing the fixture at minute resolution over a multi-day window.

**Architecture:** Two orthogonal knobs added to the spec/engine surface. `PredictionRef.lookup` controls data timing (which prediction row the evaluator consumes at decision time). `FillMode.NEXT_SESSION_OPEN` controls execution timing (where the market order fills). QC-style specs set both; spec and engine stay self-auditing. Engine main loop's order-drain branch attempts immediate fill against the current minute bar to capture the consolidator-fire-on-rollover case — the critical correction that produces trade-by-trade QC parity.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pandas, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-11-phase35-path-a-intraday-fill-mode-design.md`

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `PythonDataService/app/engine/execution/order.py` | Modify | Add `FillMode.NEXT_SESSION_OPEN` enum value |
| `PythonDataService/app/engine/execution/fill_model.py` | Modify | Add `DEFERRED_FILL_MODES` constant, `NEXT_SESSION_OPEN` branch in `fill_market_order` |
| `PythonDataService/app/engine/engine.py` | Modify | Step 3 pending-fills dispatch widens to `DEFERRED_FILL_MODES`; Step 5 order-drain gains `NEXT_SESSION_OPEN` immediate-fill branch |
| `PythonDataService/app/research/runs/runner.py` | Modify | `_VALID_FILL_MODES` + `_parse_fill_mode` accept `"next_session_open"`; coverage call passes `refs=spec.predictions` |
| `PythonDataService/app/engine/strategy/spec/schema.py` | Modify | Add `PredictionLookup` Literal + `lookup` field on `PredictionRef` |
| `PythonDataService/app/research/ml/loader.py` | Modify | `PredictionSet._sorted_ts` index, `next_after(ts_ms)` method, `PredictionLookupError` exception |
| `PythonDataService/app/research/ml/coverage.py` | Modify | `assert_bar_clock_coverage` becomes lookup-aware (accepts `refs` kwarg, validates (lookup, field) pairs per ref) |
| `PythonDataService/app/engine/strategy/spec/evaluator.py` | Modify | Per-ref dispatch on `lookup`; `PredictionLookupError` runtime backstop for None/missing-field cases |
| `PythonDataService/tests/engine/test_fill_model.py` | Modify | Add `NEXT_SESSION_OPEN` cases + `DEFERRED_FILL_MODES` invariant |
| `PythonDataService/tests/engine/test_engine_fill_modes.py` | Create | End-to-end engine + consolidator + minute-stream invariant; (R8) exact-timestamp assertions |
| `PythonDataService/tests/research/ml/test_loader.py` | Modify | `next_after` + `PredictionLookupError` cases |
| `PythonDataService/tests/research/ml/test_coverage.py` | Modify | (lookup, field) pair validation across all 4 error-message shapes |
| `PythonDataService/tests/engine/strategy/spec/test_evaluator.py` (or equivalent) | Modify or create | Per-ref dispatch + runtime-backstop case |
| `PythonDataService/tests/research/parity/test_qc_fixture_smoke.py` | Modify | Minute-boundary pinning, tz-awareness, `FEE_PRESENCE_BRANCH=A` |
| `PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py` | Modify | Remove `xfail`, set `lookup="next_after_bar_close"` + `fill_mode="next_session_open"`, pin 3 aligned-fill rows |
| `PythonDataService/tests/research/runs/test_runner_inmemory.py` | Modify | Fill-mode normalization round-trip for `next_session_open` |
| `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` | Replace | Multi-day minute fixture (user-captured via runbook) |
| `qc_appl_bars.csv` (repo root) | Delete | Stale scratch capture |
| `docs/ml-predictions-authority.md` | Modify | Flip Phase 3.5 row to "shipped" in §7; collapse §10 Phase 3.5 to historical note; bump "Last reviewed" |
| `docs/references/reconciliations/qc-aapl-phase3.md` | Modify | Rewrite as the passing reconciliation report |

---

## Task 1: Add `FillMode.NEXT_SESSION_OPEN` enum value

**Files:**
- Modify: `PythonDataService/app/engine/execution/order.py:23-36`
- Test: `PythonDataService/tests/engine/test_order.py` (extend; create if test for `FillMode` doesn't exist)

- [ ] **Step 1: Write the failing test**

Append to `PythonDataService/tests/engine/test_order.py`:

```python
from app.engine.execution.order import FillMode


def test_next_session_open_is_a_known_fill_mode() -> None:
    """NEXT_SESSION_OPEN exists and has the canonical string value the runner
    will normalize to. The string value is what RunRequest.fill_mode carries
    and what ledger persistence stores; renaming it breaks every prior run."""
    assert FillMode.NEXT_SESSION_OPEN.value == "next_session_open"
```

- [ ] **Step 2: Run test to verify it fails**

```
podman exec polygon-data-service python -m pytest tests/engine/test_order.py::test_next_session_open_is_a_known_fill_mode -v
```

Expected: FAIL with `AttributeError: NEXT_SESSION_OPEN`.

- [ ] **Step 3: Add the enum value**

In `PythonDataService/app/engine/execution/order.py`, extend the `FillMode` class:

```python
class FillMode(Enum):
    """Controls where market orders fill.

    SIGNAL_BAR_CLOSE: Fill at the close of the bar that triggered the order.
        This matches the bookkeeping recorded in LEAN's algorithm trade log
        (``_entryPrice = bar.Close`` inside ``OnFifteenMinuteBar``).

    NEXT_BAR_OPEN: Fill at the open of the bar *after* the signal bar.
        Closer to LEAN's actual fill model for equity market orders when no
        tick data is available. Used for realistic backtesting.

    NEXT_SESSION_OPEN: Fill at the open of the first eligible minute bar
        whose trading date is strictly after the signal bar's trading date
        (NY-local). Designed for the daily-consolidator-over-minute-stream
        pattern (e.g. QC precomputed-predictions parity): the strategy
        triggers at end of day T-1's consolidated bar; the order fills at
        the first minute of day T. "Eligible" today means any regular-hours
        bar; a future EligibilityPolicy may add pre/post-market handling.
    """

    SIGNAL_BAR_CLOSE = "signal_bar_close"
    NEXT_BAR_OPEN = "next_bar_open"
    NEXT_SESSION_OPEN = "next_session_open"
```

- [ ] **Step 4: Run test to verify it passes**

```
podman exec polygon-data-service python -m pytest tests/engine/test_order.py::test_next_session_open_is_a_known_fill_mode -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/execution/order.py PythonDataService/tests/engine/test_order.py
git commit -m "feat(engine): add FillMode.NEXT_SESSION_OPEN enum value"
```

---

## Task 2: Add `DEFERRED_FILL_MODES` constant and `NEXT_SESSION_OPEN` branch in `FillModel`

**Files:**
- Modify: `PythonDataService/app/engine/execution/fill_model.py`
- Test: `PythonDataService/tests/engine/test_fill_model.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/engine/test_fill_model.py`:

```python
from datetime import date
from zoneinfo import ZoneInfo

from app.engine.execution.fill_model import DEFERRED_FILL_MODES

NY = ZoneInfo("America/New_York")


def _ny_bar(start: datetime, open_: str, high: str, low: str, close: str) -> TradeBar:
    """Minute bar anchored to NY-local time, for date-comparison testing."""
    return TradeBar(
        symbol="AAPL",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def test_next_session_open_defers_when_candidate_same_trading_date() -> None:
    """A candidate bar on the same NY trading date as signal_bar is ineligible.
    The model returns None so the engine's deferred-fill loop retries on a
    later bar."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 9, 30, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    same_day_candidate = _ny_bar(
        datetime(2026, 2, 9, 15, 59, tzinfo=NY), "101.0", "101.2", "100.9", "101.1"
    )

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=same_day_candidate)

    assert event is None


def test_next_session_open_fills_at_later_trading_date_open() -> None:
    """A candidate bar strictly after the signal's NY trading date fills at
    that bar's open, with fill_time = candidate.time (start of bar)."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    next_day_open = _ny_bar(
        datetime(2026, 2, 10, 9, 30, tzinfo=NY), "102.0", "102.5", "101.8", "102.2"
    )

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_day_open)

    assert event is not None
    assert event.fill_price == Decimal("102.0")  # next_day_open.open
    assert event.time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)  # next_day_open.time (start)


def test_next_session_open_returns_none_when_next_bar_missing() -> None:
    """No candidate bar at all -> deferred (None), same as NEXT_BAR_OPEN."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=None)

    assert event is None


def test_next_session_open_applies_long_slippage() -> None:
    """Slippage in the trade direction applies to the fill price, same as
    other modes."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN, slippage_per_share=Decimal("0.05"))
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    next_day_open = _ny_bar(
        datetime(2026, 2, 10, 9, 30, tzinfo=NY), "102.0", "102.5", "101.8", "102.2"
    )

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_day_open)

    assert event is not None
    assert event.fill_price == Decimal("102.05")  # open + slippage


def test_deferred_fill_modes_membership_invariant() -> None:
    """DEFERRED_FILL_MODES contains every mode whose fill is gated on a
    subsequent candidate bar (i.e., where fill_market_order can return None).
    NEXT_BAR_OPEN and NEXT_SESSION_OPEN belong; SIGNAL_BAR_CLOSE does not.
    A regression where a new deferred-mode is added without adding it to
    this set would leave the engine main loop unable to re-try the fill."""
    assert FillMode.NEXT_BAR_OPEN in DEFERRED_FILL_MODES
    assert FillMode.NEXT_SESSION_OPEN in DEFERRED_FILL_MODES
    assert FillMode.SIGNAL_BAR_CLOSE not in DEFERRED_FILL_MODES
    # Every FillMode is either a deferred-fill mode or fills immediately.
    # If a future mode lands without classification, this assertion forces
    # an explicit decision.
    immediate_modes = {FillMode.SIGNAL_BAR_CLOSE}
    assert set(FillMode) == DEFERRED_FILL_MODES | immediate_modes
```

- [ ] **Step 2: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/engine/test_fill_model.py -v -k "next_session_open or deferred_fill_modes"
```

Expected: 5 FAILs — `ImportError: cannot import name 'DEFERRED_FILL_MODES'` and `unknown fill mode`.

- [ ] **Step 3: Add `DEFERRED_FILL_MODES` and the `NEXT_SESSION_OPEN` branch**

In `PythonDataService/app/engine/execution/fill_model.py`, add at module scope (before the `FillModel` class):

```python
# Set of FillModes whose fill_market_order may return None waiting for a
# subsequent candidate bar. The engine's main loop uses this set to gate
# both the pending-fills retry loop (Step 3) and the order-drain branch
# (Step 5). Single source of truth — keep in lockstep with the FillMode
# enum (see test_deferred_fill_modes_membership_invariant).
DEFERRED_FILL_MODES: frozenset[FillMode] = frozenset(
    {FillMode.NEXT_BAR_OPEN, FillMode.NEXT_SESSION_OPEN}
)
```

In the `FillModel.fill_market_order` method, add a new branch after the `NEXT_BAR_OPEN` branch (and before the slippage block):

```python
        elif self.mode == FillMode.NEXT_SESSION_OPEN:
            if next_bar is None:
                return None
            # Eligibility: candidate bar must belong to a trading date STRICTLY
            # AFTER the signal bar's trading date (NY-local). Minimal
            # implementation for regular-hours-only fixtures. A future
            # EligibilityPolicy would replace this date comparison without
            # changing the contract: "first eligible minute bar after the
            # signal bar's trading date." Both .end_time and .time are
            # tz-aware (set by FixtureDataReader and LeanMinuteDataReader);
            # .date() returns the NY-local calendar date.
            if next_bar.time.date() <= signal_bar.end_time.date():
                return None
            fill_price = next_bar.open
            fill_time = next_bar.time
```

- [ ] **Step 4: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/engine/test_fill_model.py -v
```

Expected: ALL PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/execution/fill_model.py PythonDataService/tests/engine/test_fill_model.py
git commit -m "feat(engine): add NEXT_SESSION_OPEN branch and DEFERRED_FILL_MODES set"
```

---

## Task 3: Engine main loop — widen Step 3 dispatch and add Step 5 immediate-fill branch

**Files:**
- Modify: `PythonDataService/app/engine/engine.py` (Step 3 ~line 257 and Step 5 ~lines 323-333)
- Test: `PythonDataService/tests/engine/test_engine_fill_modes.py` (CREATE)

- [ ] **Step 1: Write the failing test — the (R8) fill-timing invariant**

Create `PythonDataService/tests/engine/test_engine_fill_modes.py`:

```python
"""End-to-end engine + consolidator + minute-stream tests for FillMode
dispatch — particularly the NEXT_SESSION_OPEN immediate-fill invariant
that produces QC trade-by-trade parity in the daily-consolidator-over-
minute-stream pattern.

These are the regression guards for the engine main loop's step ordering
(pending-fills → consolidator-fire → order-drain). Any future refactor
that re-orders those steps will surface here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.base import Strategy

NY = ZoneInfo("America/New_York")


def _minute(date_: date, hour: int, minute: int, *, open_: str, high: str, low: str, close: str) -> TradeBar:
    start = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=NY)
    return TradeBar(
        symbol="AAPL",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


class _SyntheticStream:
    """data_source.iter_bars contract: returns a fresh iterator each call."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        return iter(self._bars)


class _EmitOnceStrategy(Strategy):
    """Submits one set_holdings(1.0) on the first consolidated bar it sees;
    records the consolidated bar's end_time and every order event."""

    def __init__(self) -> None:
        super().__init__()
        self.signal_bar_end_time: datetime | None = None
        self.events: list = []

    def initialize(self) -> None:
        self.set_start_date(2026, 2, 9)
        self.set_end_date(2026, 2, 12)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("AAPL")
        self._symbol = symbol
        self.ctx.register_consolidator(symbol, timedelta(minutes=1440), self._on_daily)

    def _on_daily(self, bar: TradeBar) -> None:
        if self.signal_bar_end_time is None:
            self.signal_bar_end_time = bar.end_time
            assert self.ctx is not None
            self.ctx.set_holdings(self._symbol, Decimal("1.0"))

    def on_order_event(self, event) -> None:
        self.events.append(event)


def _two_day_stream() -> list[TradeBar]:
    """Two trading days, three minute bars each — enough to consolidate
    day-1's daily bar on day-2's first minute and for the day-2 09:30 bar
    to be both (a) the consolidator-fire iteration and (b) the first
    eligible NEXT_SESSION_OPEN candidate."""
    d1 = date(2026, 2, 9)
    d2 = date(2026, 2, 10)
    return [
        _minute(d1, 9, 30, open_="100.0", high="100.2", low="99.9", close="100.1"),
        _minute(d1, 12, 0, open_="100.1", high="100.3", low="100.0", close="100.2"),
        _minute(d1, 15, 59, open_="100.2", high="100.4", low="100.0", close="100.3"),
        _minute(d2, 9, 30, open_="102.0", high="102.5", low="101.8", close="102.2"),
        _minute(d2, 9, 31, open_="102.2", high="102.6", low="102.0", close="102.4"),
        _minute(d2, 15, 59, open_="102.4", high="102.7", low="102.3", close="102.5"),
    ]


def test_next_session_open_fills_at_first_eligible_minute_open() -> None:
    """THE invariant: when the daily consolidator fires day-1's bar during
    processing of day-2's first minute, NEXT_SESSION_OPEN fills against
    THAT minute_bar (not the next iteration's bar). Fill price = open of
    day-2 09:30; fill time = day-2 09:30 NY.

    This is the (R8) regression guard for the engine main loop's
    step-ordering invariant: a refactor that re-ordered Step 3
    (pending-fills) and Step 5 (order-drain) would either fill one bar
    early or one bar late."""
    stream = _SyntheticStream(_two_day_stream())
    strategy = _EmitOnceStrategy()
    engine = BacktestEngine(
        data_source=stream,
        fill_model=FillModel(
            mode=FillMode.NEXT_SESSION_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )

    engine.run(strategy)

    assert strategy.signal_bar_end_time is not None
    # Daily consolidated bar's end_time anchors to the last contained minute:
    # day-1 15:59→16:00.
    assert strategy.signal_bar_end_time == datetime(2026, 2, 9, 16, 0, tzinfo=NY)
    assert len(strategy.events) == 1
    event = strategy.events[0]
    assert event.direction is Direction.LONG
    # Fill time = next_bar.time = day-2 09:30 NY (start of the [09:30, 09:31) bar).
    assert event.time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)
    # Fill price = open of day-2 [09:30, 09:31) = 102.0.
    assert event.fill_price == Decimal("102.0")


def test_next_session_open_same_date_signal_stays_pending_until_session_boundary() -> None:
    """Defensive: if (for some hypothetical configuration) the consolidator
    fires day-1's bar on a SAME-DAY minute (date == signal.date), the order
    must stay deferred across subsequent same-day minutes and fill only at
    the first later-date bar. Catches a regression where the immediate-fill
    branch wrongly accepts same-date candidates."""

    # Stream that fires the daily consolidator mid-day-1 wouldn't naturally
    # happen with a 1440-min consolidator, but we exercise the eligibility
    # check directly: synthesize a signal bar on day-1 plus a stream that
    # has multiple day-1 minutes followed by a day-2 minute. Use a
    # 1-minute consolidator so the strategy submits one order per minute;
    # configure the strategy to only emit ONCE so we have a single signal
    # bar with date=day-1. Then the engine should defer through every
    # subsequent day-1 minute and fill at the day-2 minute's open.
    d1 = date(2026, 2, 9)
    d2 = date(2026, 2, 10)
    bars = [
        _minute(d1, 9, 30, open_="100.0", high="100.2", low="99.9", close="100.1"),
        _minute(d1, 9, 31, open_="100.1", high="100.3", low="100.0", close="100.2"),
        _minute(d1, 9, 32, open_="100.2", high="100.4", low="100.0", close="100.3"),
        _minute(d2, 9, 30, open_="102.0", high="102.5", low="101.8", close="102.2"),
    ]

    class _OneMinStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__()
            self.events: list = []
            self._fired = False

        def initialize(self) -> None:
            self.set_start_date(2026, 2, 9)
            self.set_end_date(2026, 2, 12)
            self.set_cash(100_000)
            assert self.ctx is not None
            symbol = self.ctx.add_equity("AAPL")
            self._symbol = symbol
            self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_min)

        def _on_min(self, bar: TradeBar) -> None:
            if not self._fired:
                self._fired = True
                assert self.ctx is not None
                self.ctx.set_holdings(self._symbol, Decimal("1.0"))

        def on_order_event(self, event) -> None:
            self.events.append(event)

    strategy = _OneMinStrategy()
    engine = BacktestEngine(
        data_source=_SyntheticStream(bars),
        fill_model=FillModel(
            mode=FillMode.NEXT_SESSION_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )
    engine.run(strategy)

    # Order stays pending through day-1's 9:31 and 9:32, fills at day-2 09:30 open.
    assert len(strategy.events) == 1
    assert strategy.events[0].time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)
    assert strategy.events[0].fill_price == Decimal("102.0")


def test_next_bar_open_keeps_existing_defer_behavior_on_same_stream() -> None:
    """Regression: NEXT_BAR_OPEN must NOT acquire the immediate-fill
    behavior. With the same two-day stream, NEXT_BAR_OPEN fills on the
    minute AFTER the consolidator-fire iteration (day-2 09:31, not 09:30).
    Catches a regression where the Step 5 NEXT_BAR_OPEN branch
    accidentally inherited the immediate-fill optimization."""
    stream = _SyntheticStream(_two_day_stream())
    strategy = _EmitOnceStrategy()
    engine = BacktestEngine(
        data_source=stream,
        fill_model=FillModel(
            mode=FillMode.NEXT_BAR_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )

    engine.run(strategy)

    assert len(strategy.events) == 1
    # NEXT_BAR_OPEN fills on the bar AFTER the consolidator-fire iteration:
    # consolidator fires on day-2 09:30 bar (queues order); fill happens on
    # day-2 09:31 bar's open.
    assert strategy.events[0].time == datetime(2026, 2, 10, 9, 31, tzinfo=NY)
    assert strategy.events[0].fill_price == Decimal("102.2")
```

- [ ] **Step 2: Run the new tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/engine/test_engine_fill_modes.py -v
```

Expected: 3 FAILs. The first two fail because the engine's Step 3 dispatch is hard-coded to `NEXT_BAR_OPEN` (`NEXT_SESSION_OPEN` orders sit in `pending_fills` and never get re-tried) and Step 5's `else` branch defers all non-`SIGNAL_BAR_CLOSE` orders without the immediate-fill check.

- [ ] **Step 3: Widen Step 3 dispatch in `engine.py`**

In `PythonDataService/app/engine/engine.py`, near the top of the file add:

```python
from app.engine.execution.fill_model import DEFERRED_FILL_MODES, FillModel
```

(Replace the existing `FillModel` import — keep `DEFERRED_FILL_MODES` adjacent.)

At engine.py:257, replace:

```python
            # ----- Fill any deferred NEXT_BAR_OPEN orders with this bar as next_bar
            if pending_fills and self.fill_model.mode == FillMode.NEXT_BAR_OPEN:
```

with:

```python
            # ----- Fill any deferred orders (NEXT_BAR_OPEN / NEXT_SESSION_OPEN)
            # with this bar as next_bar. DEFERRED_FILL_MODES is the single
            # source of truth shared with Step 5 below.
            if pending_fills and self.fill_model.mode in DEFERRED_FILL_MODES:
```

- [ ] **Step 4: Replace Step 5 order-drain branch with explicit per-mode dispatch**

At engine.py:323-333, replace:

```python
                    for order in market_orders:
                        if self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
                            event = self.fill_model.fill_market_order(order, signal_bar, next_bar=None)
                            assert event is not None
                            portfolio.apply_fill(event)
                            order_events.append(event)
                            strategy.on_order_event(event)
                            _register_bracket_if_needed(order, event)
                        else:
                            # Defer until the next minute bar.
                            pending_fills.append((order, signal_bar))
```

with:

```python
                    for order in market_orders:
                        if self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
                            event = self.fill_model.fill_market_order(order, signal_bar, next_bar=None)
                            assert event is not None
                            portfolio.apply_fill(event)
                            order_events.append(event)
                            strategy.on_order_event(event)
                            _register_bracket_if_needed(order, event)
                        elif self.fill_model.mode == FillMode.NEXT_SESSION_OPEN:
                            # Attempt immediate fill against the current minute_bar.
                            # In the daily-consolidator-over-minute-stream pattern
                            # (Phase 3.5 Path A), this iteration's minute_bar IS
                            # already the first minute of the next session, so the
                            # order should fill now — not wait for the next bar.
                            # See tests/engine/test_engine_fill_modes.py
                            # ::test_next_session_open_fills_at_first_eligible_minute_open
                            # for the pinned invariant.
                            event = self.fill_model.fill_market_order(
                                order, signal_bar, next_bar=minute_bar
                            )
                            if event is None:
                                pending_fills.append((order, signal_bar))
                            else:
                                portfolio.apply_fill(event)
                                order_events.append(event)
                                strategy.on_order_event(event)
                                _register_bracket_if_needed(order, event)
                        elif self.fill_model.mode == FillMode.NEXT_BAR_OPEN:
                            # Defer until the next minute bar — protects
                            # single-stream cases where signal_bar IS the current
                            # minute_bar, so filling against it would defeat the
                            # "next bar open" semantic.
                            pending_fills.append((order, signal_bar))
                        else:
                            raise ValueError(f"unknown fill mode: {self.fill_model.mode}")
```

- [ ] **Step 5: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/engine/test_engine_fill_modes.py tests/engine/test_fill_model.py -v
```

Expected: ALL PASS.

- [ ] **Step 6: Run the full engine test suite to verify no regressions**

```
podman exec polygon-data-service python -m pytest tests/engine/ -v
```

Expected: ALL PASS (or pre-existing baselines from authority doc only).

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/engine/engine.py PythonDataService/tests/engine/test_engine_fill_modes.py
git commit -m "feat(engine): wire NEXT_SESSION_OPEN immediate-fill in main loop"
```

---

## Task 4: Runner accepts `"next_session_open"` fill mode

**Files:**
- Modify: `PythonDataService/app/research/runs/runner.py:80, 113-119`
- Test: `PythonDataService/tests/research/runs/test_runner_inmemory.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `PythonDataService/tests/research/runs/test_runner_inmemory.py`:

```python
from app.research.runs.runner import _normalize_fill_mode, _parse_fill_mode, _VALID_FILL_MODES
from app.engine.execution.order import FillMode


def test_next_session_open_is_a_valid_fill_mode() -> None:
    assert "next_session_open" in _VALID_FILL_MODES


def test_parse_fill_mode_returns_next_session_open_enum_value() -> None:
    assert _parse_fill_mode("next_session_open") is FillMode.NEXT_SESSION_OPEN


def test_normalize_fill_mode_handles_dash_and_case_variants_for_next_session_open() -> None:
    # All three of these must produce the same canonical form so they
    # ledger-identify identically (R5 hash-identity invariant).
    assert _normalize_fill_mode("NEXT-SESSION-OPEN") == "next_session_open"
    assert _normalize_fill_mode("Next-Session-Open") == "next_session_open"
    assert _normalize_fill_mode("next_session_open") == "next_session_open"
    assert _parse_fill_mode("NEXT-SESSION-OPEN") is FillMode.NEXT_SESSION_OPEN
```

- [ ] **Step 2: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/research/runs/test_runner_inmemory.py -v -k "next_session_open"
```

Expected: 3 FAILs (`"next_session_open" not in _VALID_FILL_MODES`, ValueError from `_parse_fill_mode`).

- [ ] **Step 3: Update runner.py**

In `PythonDataService/app/research/runs/runner.py:80`, replace:

```python
_VALID_FILL_MODES = {"signal_bar_close", "next_bar_open"}
```

with:

```python
_VALID_FILL_MODES = {"signal_bar_close", "next_bar_open", "next_session_open"}
```

At runner.py:113-119, replace:

```python
def _parse_fill_mode(s: str) -> FillMode:
    norm = _normalize_fill_mode(s)
    if norm == "signal_bar_close":
        return FillMode.SIGNAL_BAR_CLOSE
    if norm == "next_bar_open":
        return FillMode.NEXT_BAR_OPEN
    raise ValueError(f"unknown fill_mode {s!r} — expected one of {sorted(_VALID_FILL_MODES)}")
```

with:

```python
def _parse_fill_mode(s: str) -> FillMode:
    norm = _normalize_fill_mode(s)
    if norm == "signal_bar_close":
        return FillMode.SIGNAL_BAR_CLOSE
    if norm == "next_bar_open":
        return FillMode.NEXT_BAR_OPEN
    if norm == "next_session_open":
        return FillMode.NEXT_SESSION_OPEN
    raise ValueError(f"unknown fill_mode {s!r} — expected one of {sorted(_VALID_FILL_MODES)}")
```

- [ ] **Step 4: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/research/runs/test_runner_inmemory.py -v
```

Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/runs/runner.py PythonDataService/tests/research/runs/test_runner_inmemory.py
git commit -m "feat(runs): runner accepts next_session_open fill mode"
```

---

## Task 5: Add `PredictionRef.lookup` field to spec schema

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/schema.py:220-233`
- Test: Find existing spec-schema tests (likely `PythonDataService/tests/engine/strategy/spec/test_schema.py` — confirm path with `ls` before writing)

- [ ] **Step 1: Confirm test file location**

```
ls PythonDataService/tests/engine/strategy/spec/
```

If `test_schema.py` exists, extend it; otherwise create it with the imports below.

- [ ] **Step 2: Write the failing tests**

Append to (or create) `PythonDataService/tests/engine/strategy/spec/test_schema.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.strategy.spec.schema import PredictionRef


def test_prediction_ref_lookup_defaults_to_exact_bar_close() -> None:
    """Default preserves backward compatibility: existing specs without an
    explicit lookup field continue to consume the prediction row at the
    bar's exact end_time_ms."""
    ref = PredictionRef.model_validate(
        {"id": "p", "prediction_set_id": "x", "field": "prediction"}
    )
    assert ref.lookup == "exact_bar_close"


def test_prediction_ref_lookup_accepts_next_after_bar_close() -> None:
    ref = PredictionRef.model_validate(
        {
            "id": "p",
            "prediction_set_id": "x",
            "field": "prediction",
            "lookup": "next_after_bar_close",
        }
    )
    assert ref.lookup == "next_after_bar_close"


def test_prediction_ref_lookup_rejects_unknown_value() -> None:
    """Closed Literal — any other string is a validation error at the wire
    boundary. Catches typos like "next_after" or "next_bar_close" before
    they reach the evaluator."""
    with pytest.raises(ValidationError):
        PredictionRef.model_validate(
            {
                "id": "p",
                "prediction_set_id": "x",
                "field": "prediction",
                "lookup": "lookahead",
            }
        )
```

- [ ] **Step 3: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/engine/strategy/spec/test_schema.py -v -k "lookup"
```

Expected: 3 FAILs (`AttributeError: lookup`, `extra='forbid'` rejection).

- [ ] **Step 4: Add the `PredictionLookup` Literal and `lookup` field**

In `PythonDataService/app/engine/strategy/spec/schema.py`, add the type alias near the existing `ComparisonOp` definition (above the `PredictionRef` class):

```python
PredictionLookup = Literal["exact_bar_close", "next_after_bar_close"]
```

Then at schema.py:220-233, replace the `PredictionRef` class with:

```python
class PredictionRef(BaseModel):
    """Spec-local handle bound to one column of a prediction set artifact.

    ``id`` is referenced by ``PredictionComparison.prediction``. ``field``
    is the column name in the artifact rows (default ``"prediction"`` for
    the v0.5 single-scalar contract; reserved for future multi-column
    artifacts).

    ``lookup`` selects the evaluator's row-selection policy at decision
    time. ``"exact_bar_close"`` (default) reads the row keyed at the
    consolidated bar's ``end_time_ms``. ``"next_after_bar_close"`` reads
    the row with the smallest timestamp strictly greater than the bar's
    ``end_time_ms`` — used for "consume tomorrow's prediction at today's
    close" strategies like QC's precomputed-predictions tutorial.
    Coverage validation (``app.research.ml.coverage``) is lookup-aware
    and fails at run-pipeline boundary if any fired bar lacks the
    required successor row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    prediction_set_id: str
    field: str = "prediction"
    lookup: PredictionLookup = "exact_bar_close"
```

- [ ] **Step 5: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/engine/strategy/spec/test_schema.py -v
```

Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/schema.py PythonDataService/tests/engine/strategy/spec/test_schema.py
git commit -m "feat(spec): add PredictionRef.lookup field (data-timing knob)"
```

---

## Task 6: Add `PredictionSet.next_after`, sorted index, and `PredictionLookupError`

**Files:**
- Modify: `PythonDataService/app/research/ml/loader.py`
- Test: `PythonDataService/tests/research/ml/test_loader.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/research/ml/test_loader.py`:

```python
from app.research.ml.loader import PredictionLookupError, PredictionSet
from app.research.ml.artifact import ChunkRef, DeterministicRuleGenerator, PredictionSetManifest


def _pset_with_keys(keys_ms: list[int]) -> PredictionSet:
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="AAPL",
        resolution_minutes=1440,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=keys_ms[0] - 1 if keys_ms else 0,
            start_ms=keys_ms[0] if keys_ms else 0,
            end_ms=keys_ms[-1] if keys_ms else 0,
            row_count=len(keys_ms),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": float(ts)} for ts in keys_ms}
    return PredictionSet(manifest=manifest, index=index)


def test_next_after_returns_smallest_strictly_greater_key() -> None:
    """The defining invariant: strict `>`, not `>=`. A query at a key that
    exists returns the NEXT row, not the queried one."""
    pset = _pset_with_keys([100, 200, 300, 400])
    assert pset.next_after(100) == {"prediction": 200.0}
    assert pset.next_after(150) == {"prediction": 200.0}
    assert pset.next_after(199) == {"prediction": 200.0}
    assert pset.next_after(200) == {"prediction": 300.0}  # strict — skips 200 itself


def test_next_after_returns_none_when_no_later_key_exists() -> None:
    pset = _pset_with_keys([100, 200, 300])
    assert pset.next_after(300) is None
    assert pset.next_after(999) is None


def test_next_after_handles_unsorted_input_keys() -> None:
    """Index dict insertion order may be unsorted; the sorted-key cache
    must produce correct ordering regardless of dict iteration order."""
    pset = _pset_with_keys([300, 100, 400, 200])
    # Same expectations as the sorted-input case.
    assert pset.next_after(100) == {"prediction": 200.0}
    assert pset.next_after(250) == {"prediction": 300.0}
    assert pset.next_after(400) is None


def test_prediction_lookup_error_subclasses_value_error() -> None:
    """PredictionLookupError must be catchable as ValueError so existing
    runner exception handling (which catches Exception for failed-status
    ledger persistence) continues to work without an explicit add."""
    assert issubclass(PredictionLookupError, ValueError)
```

- [ ] **Step 2: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v -k "next_after or prediction_lookup_error"
```

Expected: 4 FAILs (`AttributeError: next_after`, `ImportError: PredictionLookupError`).

- [ ] **Step 3: Add `PredictionLookupError` and the `next_after` method**

In `PythonDataService/app/research/ml/loader.py`, add the new exception near `PredictionCoverageError`:

```python
class PredictionLookupError(ValueError):
    """Raised when a strategy's per-bar prediction lookup violates contract.

    Indicates one of: missing 'next' row for a next_after_bar_close ref,
    a missing exact-match row, or a row missing the declared field.
    Coverage check (app.research.ml.coverage.assert_bar_clock_coverage)
    is the intended first-line guard; this is the runtime backstop so a
    coverage bug or fixture truncation can never silently suppress trades.
    """
```

Add `from bisect import bisect_right` at the top of the file (with the other stdlib imports).

Modify the `PredictionSet` dataclass:

```python
@dataclass
class PredictionSet:
    """Loaded + validated prediction-set artifact."""

    manifest: PredictionSetManifest
    index: dict[int, dict[str, float]]
    _sorted_ts: list[int] = field(default_factory=list, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Sorted-key cache for O(log n) next_after lookups. Built once at
        # load; the index is treated as immutable post-construction (the
        # public API exposes no mutation, and PredictionSet.load builds a
        # fresh instance).
        self._sorted_ts = sorted(self.index.keys())

    def next_after(self, ts_ms: int) -> dict[str, float] | None:
        """Smallest-key row whose timestamp is strictly greater than ``ts_ms``.

        Returns ``None`` when no such row exists. Callers needing
        non-None guarantees (the SpecAlgorithm evaluator for refs with
        ``lookup="next_after_bar_close"``) raise ``PredictionLookupError``
        on None; the coverage check is the intended first-line guard so
        runtime should never see None in correct configurations.
        """
        i = bisect_right(self._sorted_ts, ts_ms)
        if i == len(self._sorted_ts):
            return None
        return self.index[self._sorted_ts[i]]

    @classmethod
    def load(cls, root: Path) -> PredictionSet:
        # ... existing load logic unchanged ...
```

Add `field` to the existing dataclasses imports at the top of the file (currently only imports `dataclass`):

```python
from dataclasses import dataclass, field
```

- [ ] **Step 4: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v
```

Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/loader.py PythonDataService/tests/research/ml/test_loader.py
git commit -m "feat(ml): add PredictionSet.next_after + PredictionLookupError"
```

---

## Task 7: Make `assert_bar_clock_coverage` lookup-aware

**Files:**
- Modify: `PythonDataService/app/research/ml/coverage.py`
- Test: `PythonDataService/tests/research/ml/test_coverage.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `PythonDataService/tests/research/ml/test_coverage.py`:

```python
from app.engine.strategy.spec.schema import PredictionRef


def _ref(*, lookup: str = "exact_bar_close", field: str = "prediction") -> PredictionRef:
    return PredictionRef.model_validate(
        {"id": "p", "prediction_set_id": "t", "field": field, "lookup": lookup}
    )


def _pset_with_fields(rows: list[tuple[int, dict[str, float]]]) -> PredictionSet:
    """Like _pset but allows per-row field control (for missing-field tests)."""
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction", "confidence"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=rows[0][0] - 1 if rows else 0,
            start_ms=rows[0][0] if rows else 0,
            end_ms=rows[-1][0] if rows else 0,
            row_count=len(rows),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: dict(row) for ts, row in rows}
    return PredictionSet(manifest=manifest, index=index)


def test_coverage_exact_bar_close_missing_row_raises_descriptive_error() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars[:-1]]
    pset = _pset(timestamps)
    refs = [_ref(lookup="exact_bar_close", field="prediction")]
    with pytest.raises(PredictionCoverageError, match=r"exact_bar_close.*no prediction row at fired bar"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_exact_bar_close_missing_field_raises() -> None:
    bars = _bars(2)
    rows = [(_to_ms(b.end_time), {"prediction": 0.0}) for b in bars]
    pset = _pset_with_fields(rows)
    refs = [_ref(lookup="exact_bar_close", field="confidence")]
    with pytest.raises(PredictionCoverageError, match=r"missing field 'confidence'.*available"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_next_after_no_later_row_raises_with_fired_ts_in_message() -> None:
    """For next_after_bar_close, every fired bar must have a strictly-greater
    row. With predictions covering only the fired bars themselves, the LAST
    fired bar has no successor and coverage must fail."""
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars]
    pset = _pset(timestamps)
    refs = [_ref(lookup="next_after_bar_close", field="prediction")]
    last_fired_ts = _to_ms(bars[-1].end_time)
    with pytest.raises(PredictionCoverageError, match=rf"next_after_bar_close.*{last_fired_ts}"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_next_after_later_row_missing_field_reports_matched_ts() -> None:
    """If the next-row exists but lacks the required field, the error must
    name both the fired ts AND the matched next-row ts (so the user can
    locate the corrupt row in the prediction set)."""
    bars = _bars(2)
    # Predictions: first fired bar maps to a row with both fields; the
    # next row (which next_after returns) has prediction but no confidence.
    rows = [
        (_to_ms(bars[0].end_time), {"prediction": 1.0, "confidence": 0.5}),
        (_to_ms(bars[1].end_time), {"prediction": 2.0, "confidence": 0.6}),
        # next_after lookup for the LAST fired bar resolves here, no confidence:
        (_to_ms(bars[1].end_time) + 1, {"prediction": 3.0}),
    ]
    pset = _pset_with_fields(rows)
    refs = [_ref(lookup="next_after_bar_close", field="confidence")]
    matched_ts = _to_ms(bars[1].end_time) + 1
    with pytest.raises(
        PredictionCoverageError,
        match=rf"matched next row at ts_ms={matched_ts}.*missing field 'confidence'",
    ):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_mixed_lookup_modes_validates_both() -> None:
    """Spec with two refs (one exact, one next_after) is valid only when both
    constraints hold on every fired bar simultaneously."""
    bars = _bars(2)
    # Exact: covers both fired bars. Next_after: covers only fired bar 0
    # (fired bar 1 has no successor).
    rows = [
        (_to_ms(bars[0].end_time), {"prediction": 1.0}),
        (_to_ms(bars[1].end_time), {"prediction": 2.0}),
    ]
    pset = _pset_with_fields(rows)
    refs = [
        _ref(lookup="exact_bar_close", field="prediction"),
        _ref(lookup="next_after_bar_close", field="prediction"),
    ]
    with pytest.raises(PredictionCoverageError, match=r"next_after_bar_close"):
        assert_bar_clock_coverage(pset, bars, refs=refs)


def test_coverage_passes_under_next_after_when_set_extends_one_row_past_bars() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars]
    # Add one extra row strictly after the last fired bar's end_time:
    timestamps.append(timestamps[-1] + 1)
    pset = _pset(sorted(timestamps))
    refs = [_ref(lookup="next_after_bar_close", field="prediction")]
    # No exception — every fired bar has a strictly-greater row.
    assert_bar_clock_coverage(pset, bars, refs=refs)
```

The existing tests in this file call `assert_bar_clock_coverage(pset, bars)` without the `refs` kwarg. These will need a small update to use the new signature with default-exact-mode refs.

In the existing tests `test_coverage_passes_when_predictions_match_bars_exactly`, `test_coverage_passes_when_predictions_are_a_superset_of_bars`, `test_coverage_fails_when_a_bar_has_no_prediction`, `test_coverage_error_lists_missing_timestamps`, change every call from:

```python
assert_bar_clock_coverage(pset, bars)
```

to:

```python
assert_bar_clock_coverage(pset, bars, refs=[_ref()])
```

(`_ref()` defaults to `exact_bar_close` + `prediction`, which preserves the original semantics.)

- [ ] **Step 2: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/research/ml/test_coverage.py -v
```

Expected: new tests FAIL; existing tests pass or fail with `TypeError: unexpected keyword 'refs'` depending on whether the signature change is in yet.

- [ ] **Step 3: Update `assert_bar_clock_coverage` signature and behavior**

In `PythonDataService/app/research/ml/coverage.py`, replace the entire `assert_bar_clock_coverage` function with:

```python
def assert_bar_clock_coverage(
    prediction_set: PredictionSet,
    bar_stream: Iterable[_BarLike],
    *,
    refs: Iterable["PredictionRef"],
) -> None:
    """Raise ``PredictionCoverageError`` if any fired bar lacks a matching
    prediction under any declared ``PredictionRef``'s ``lookup`` mode.

    For each ``ref`` and each fired bar's ``end_time_ms``:

    - ``ref.lookup == "exact_bar_close"``: ``prediction_set.index`` must
      contain the bar's ``end_time_ms``, AND that row must contain
      ``ref.field``.
    - ``ref.lookup == "next_after_bar_close"``: there must be a row whose
      timestamp is strictly greater than the bar's ``end_time_ms``, AND
      that row must contain ``ref.field``.

    Raises on the first violation. The error message names ``ref.id``,
    ``ref.lookup``, the fired ``ts_ms``, and ``ref.field``; for the
    next_after-row-missing-field case, also names the matched next-row's
    ``ts_ms`` so the offending prediction row can be located in the
    artifact.

    ``bar_stream`` must be the bars the run will actually evaluate —
    typically obtained by running the data source through the same
    ``TradeBarConsolidator`` configuration the engine will use.
    Iterating consumes the stream once.

    ``refs`` is the spec's ``predictions`` list. A spec may mix lookup
    modes across refs; each ref is validated independently.
    """
    # Materialize the bar list once — multiple refs iterate it.
    bars: list[_BarLike] = list(bar_stream)
    fired_ms: list[int] = [to_ms_utc(bar.end_time) for bar in bars]
    have_ms: set[int] = set(prediction_set.index.keys())
    sorted_have: list[int] = prediction_set._sorted_ts

    refs_list = list(refs)
    if not refs_list:
        # No predictions declared — nothing to validate. Existing behavior
        # is to silently pass; preserve that.
        return

    for ref in refs_list:
        if ref.lookup == "exact_bar_close":
            for fired in fired_ms:
                if fired not in have_ms:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (exact_bar_close): no prediction row "
                        f"at fired bar ts_ms={fired}; field={ref.field!r}"
                    )
                row = prediction_set.index[fired]
                if ref.field not in row:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (exact_bar_close): row at fired bar "
                        f"ts_ms={fired} is missing field {ref.field!r} "
                        f"(available: {sorted(row)})"
                    )
        elif ref.lookup == "next_after_bar_close":
            for fired in fired_ms:
                # Strictly-greater binary search against the sorted index.
                i = bisect_right(sorted_have, fired)
                if i == len(sorted_have):
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (next_after_bar_close): fired bar "
                        f"ts_ms={fired} has no prediction row strictly after; "
                        f"field={ref.field!r}"
                    )
                matched_ts = sorted_have[i]
                row = prediction_set.index[matched_ts]
                if ref.field not in row:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (next_after_bar_close): fired bar "
                        f"ts_ms={fired} matched next row at ts_ms={matched_ts} "
                        f"but it is missing field {ref.field!r} "
                        f"(available: {sorted(row)})"
                    )
        else:
            # Closed Literal — Pydantic rejects other values at the wire.
            # This branch is unreachable in practice; defensive raise so a
            # future Literal expansion that forgets to update this function
            # surfaces immediately rather than silently skipping validation.
            raise PredictionCoverageError(
                f"ref {ref.id!r}: unknown lookup mode {ref.lookup!r}; "
                f"coverage.py needs an updated branch"
            )
```

Add the imports at the top of the file:

```python
from bisect import bisect_right
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine.strategy.spec.schema import PredictionRef
```

(`TYPE_CHECKING` avoids a circular import at runtime — `schema.py` does not import from `coverage.py`, but better to be defensive.)

- [ ] **Step 4: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/research/ml/test_coverage.py -v
```

Expected: ALL PASS (the modified existing tests + the new ones).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/coverage.py PythonDataService/tests/research/ml/test_coverage.py
git commit -m "feat(ml): lookup-aware bar-clock coverage validates (lookup, field) per ref"
```

---

## Task 8: SpecAlgorithm evaluator — per-ref dispatch with runtime backstop

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/evaluator.py:260-267`
- Test: existing spec evaluator tests in `PythonDataService/tests/engine/strategy/spec/` (find with ls)

- [ ] **Step 1: Find existing evaluator tests**

```
ls PythonDataService/tests/engine/strategy/spec/
```

Pick the test file that exercises `_on_consolidated_bar` predictions — likely `test_evaluator_predictions.py` or similar. If none exists for predictions, create `test_evaluator_predictions.py`.

- [ ] **Step 2: Write the failing tests**

Append to the chosen evaluator test file (creating if needed):

```python
"""Per-ref lookup dispatch in SpecAlgorithm._on_consolidated_bar."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.spec.evaluator import SpecAlgorithm
from app.engine.strategy.spec.schema import StrategySpec
from app.research.ml.loader import PredictionLookupError, PredictionSet
from app.research.ml.artifact import (
    ChunkRef,
    DeterministicRuleGenerator,
    PredictionSetManifest,
)

NY = ZoneInfo("America/New_York")


def _make_pset(rows: list[tuple[int, float]]) -> PredictionSet:
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="test",
        symbol="AAPL",
        resolution_minutes=1440,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=rows[0][0] - 1,
            start_ms=rows[0][0],
            end_ms=rows[-1][0],
            row_count=len(rows),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": val} for ts, val in rows}
    return PredictionSet(manifest=manifest, index=index)


def _next_after_spec() -> StrategySpec:
    """Single-symbol AAPL spec that uses next_after_bar_close lookup. Mirrors
    the Phase 3.5 acceptance test's spec shape."""
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "test next_after",
            "symbols": ["AAPL"],
            "resolution": {"period_minutes": 1440},
            "indicators": [],
            "predictions": [
                {
                    "id": "p",
                    "prediction_set_id": "test",
                    "field": "prediction",
                    "lookup": "next_after_bar_close",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": 0.0}
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {"kind": "PredictionComparison", "prediction": "p", "op": "<=", "value": 0.0}
                ],
            },
        }
    )


def test_evaluator_consumes_next_row_at_decision_time_under_next_after_lookup() -> None:
    """SpecAlgorithm with a next_after_bar_close ref reads the row STRICTLY
    AFTER the current bar's end_time_ms — i.e., tomorrow's prediction at
    today's close.

    Verifies via the entry-block evaluation: bar at ts=100 with next prediction
    +1.0 should signal entry (1.0 > 0.0). If the evaluator wrongly used
    exact-match lookup at ts=100 (which has prediction -1.0), entry would
    not fire."""
    pset = _make_pset([(100, -1.0), (200, 1.0)])
    spec = _next_after_spec()
    algo = SpecAlgorithm(spec, prediction_set=pset)
    # ... rest of test setup harness (StrategyContext, register consolidator,
    # feed a single TradeBar with end_time_ms=100). Exact harness mirrors
    # existing predictions-evaluator tests in this file. The assertion is:
    # after _on_consolidated_bar runs, the strategy is in_position (entry
    # fired) and a market order is in portfolio.pending_orders.


def test_evaluator_raises_prediction_lookup_error_when_next_row_absent() -> None:
    """Runtime backstop: if coverage check somehow let a bar through that has
    no successor prediction, _on_consolidated_bar must raise rather than
    silently produce a False PredictionComparison."""
    # Single-row prediction set; querying next_after at the only row yields None.
    pset = _make_pset([(100, 1.0)])
    spec = _next_after_spec()
    algo = SpecAlgorithm(spec, prediction_set=pset)
    # Feed a bar at end_time_ms=100 (no successor).
    # ... harness setup ...
    with pytest.raises(PredictionLookupError, match=r"next_after_bar_close.*no row strictly after"):
        # ... trigger _on_consolidated_bar ...
        pass


def test_evaluator_raises_when_resolved_row_missing_declared_field() -> None:
    """Runtime backstop: a row exists but lacks the declared field. Catches
    a coverage bypass on row.field validation."""
    # ... similar harness, prediction set has row at next_after lookup
    # without the declared field ...
    with pytest.raises(PredictionLookupError, match=r"missing declared field"):
        # ... trigger _on_consolidated_bar ...
        pass
```

> **Note for the implementer:** The harness boilerplate (constructing
> `StrategyContext`, feeding a `TradeBar` to `_on_consolidated_bar`, etc.)
> mirrors whatever pattern the existing predictions-evaluator tests use.
> Read the existing test file before writing the harness — keep the test
> idiomatic with the surrounding tests.

- [ ] **Step 3: Run tests to verify they fail**

```
podman exec polygon-data-service python -m pytest tests/engine/strategy/spec/ -v -k "next_after or prediction_lookup"
```

Expected: 3 FAILs.

- [ ] **Step 4: Update evaluator — replace predictions block**

In `PythonDataService/app/engine/strategy/spec/evaluator.py`, locate the `predictions` block in `_on_consolidated_bar` (around lines 260-267):

```python
        # Build the predictions snapshot for this bar. Empty when no
        # PredictionSet is wired (prediction-free specs).
        predictions: dict[str, Decimal] = {}
        if self._prediction_set is not None and self._spec.predictions:
            ts_ms = to_ms_utc(bar.end_time)
            row = self._prediction_set.index[ts_ms]  # KeyError == coverage-check bug
            for ref in self._spec.predictions:
                predictions[ref.id] = Decimal(str(row[ref.field]))
```

Replace with per-ref dispatch + runtime backstop:

```python
        # Build the predictions snapshot for this bar. Empty when no
        # PredictionSet is wired (prediction-free specs). Each ref's
        # `lookup` field controls which prediction-set row is consumed:
        # - exact_bar_close: row keyed at bar.end_time_ms
        # - next_after_bar_close: smallest-key row with ts > bar.end_time_ms
        # Coverage check is the intended first-line guard; the
        # PredictionLookupError raises are runtime backstops so a coverage
        # bypass / fixture truncation can never silently produce False.
        predictions: dict[str, Decimal] = {}
        if self._prediction_set is not None and self._spec.predictions:
            ts_ms = to_ms_utc(bar.end_time)
            for ref in self._spec.predictions:
                if ref.lookup == "exact_bar_close":
                    row = self._prediction_set.index.get(ts_ms)
                    if row is None:
                        raise PredictionLookupError(
                            f"prediction ref {ref.id!r} (lookup=exact_bar_close): "
                            f"no row at ts_ms={ts_ms} ({bar.end_time}); "
                            f"coverage check should have caught this"
                        )
                else:  # "next_after_bar_close"
                    row = self._prediction_set.next_after(ts_ms)
                    if row is None:
                        raise PredictionLookupError(
                            f"prediction ref {ref.id!r} (lookup=next_after_bar_close): "
                            f"no row strictly after ts_ms={ts_ms} ({bar.end_time}); "
                            f"coverage check should have caught this"
                        )
                if ref.field not in row:
                    raise PredictionLookupError(
                        f"prediction ref {ref.id!r}: row at lookup-resolved timestamp "
                        f"is missing declared field {ref.field!r} "
                        f"(available: {sorted(row)})"
                    )
                predictions[ref.id] = Decimal(str(row[ref.field]))
```

Update the import block at the top of `evaluator.py` to add:

```python
from app.research.ml.loader import PredictionLookupError
```

(Place near the existing `from app.research.ml.loader import PredictionSet` import — confirm the import already lives in the `TYPE_CHECKING` block; if so, move `PredictionLookupError` to a runtime import since it's `raise`d.)

- [ ] **Step 5: Run tests to verify they pass**

```
podman exec polygon-data-service python -m pytest tests/engine/strategy/spec/ -v
```

Expected: ALL PASS (existing + new).

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/evaluator.py PythonDataService/tests/engine/strategy/spec/
git commit -m "feat(spec): per-ref lookup dispatch + PredictionLookupError backstop"
```

---

## Task 9: Runner wires lookup-aware coverage

**Files:**
- Modify: `PythonDataService/app/research/runs/runner.py:384-397`
- Test: existing runner integration tests (`tests/research/runs/`) catch this via the acceptance test in Task 12.

- [ ] **Step 1: Update runner's coverage call**

In `PythonDataService/app/research/runs/runner.py`, find the existing call to `assert_bar_clock_coverage` (around line 392):

```python
            bar_stream = iter_consolidated_bars(
                data_source,
                symbol=symbol,
                start_date=request.start_date,
                end_date=request.end_date,
                resolution_minutes=resolution,
            )
            assert_bar_clock_coverage(prediction_set, bar_stream)
```

Replace the `assert_bar_clock_coverage` call with the new signature:

```python
            bar_stream = iter_consolidated_bars(
                data_source,
                symbol=symbol,
                start_date=request.start_date,
                end_date=request.end_date,
                resolution_minutes=resolution,
            )
            assert_bar_clock_coverage(prediction_set, bar_stream, refs=spec.predictions)
```

- [ ] **Step 2: Run all runner tests + ml tests to verify no regression**

```
podman exec polygon-data-service python -m pytest tests/research/runs/ tests/research/ml/ -v
```

Expected: ALL PASS.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/app/research/runs/runner.py
git commit -m "feat(runs): pass spec.predictions refs to bar-clock coverage check"
```

---

## Task 10: Delete stale `qc_appl_bars.csv`

**Files:**
- Delete: `qc_appl_bars.csv` (repo root)

- [ ] **Step 1: Verify it's untracked + scratch**

```
git ls-files --error-unmatch qc_appl_bars.csv 2>&1
```

Expected: `error: pathspec ... did not match any file` (it's untracked, confirmed in Task 7 of brainstorming).

- [ ] **Step 2: Delete**

```
rm qc_appl_bars.csv
```

No commit needed — the file is untracked. (If somehow it's tracked, `git rm` + commit with message `chore: remove stale scratch capture qc_appl_bars.csv`.)

---

## Task 11: Capture the multi-day minute QC fixture (user task)

**Files:**
- Create (user):
  - `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/qc_orders.json`
  - `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/qc_price_history.csv`
  - `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/qc_equity.json`
  - `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/qc_algorithm_screenshot.png`
  - `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/attribution.md`

- [ ] **Step 1: Run the existing capture runbook with updated parameters**

Follow `docs/references/qc-aapl-phase3-capture-runbook.md` end-to-end with these substitutions:

| Runbook field | New value |
|---|---|
| `start_date` | `datetime(2026, 2, 10)` |
| `end_date` | `datetime(2026, 3, 13)` (exclusive — to ensure 2026-03-12 is the last included session; confirm at capture by inspecting `qc_orders.json`'s last fill date and `qc_price_history.csv`'s last row) |
| `Resolution` for `qb.history(...)` | `Resolution.MINUTE` |
| All other parameters | Unchanged from PR #219 |

- [ ] **Step 2: Drop the captured five files into the fixture directory**

Replaces the existing daily/single-day fixture **in place**. Git history is the audit trail for the previous shape.

- [ ] **Step 3: Update `attribution.md`**

Add to the existing attribution:
- Resolution change: `Resolution.MINUTE` (was `Resolution.DAILY`)
- Window change: 2026-02-10 → 2026-03-12 (was 2026-02-09 → 2026-02-12)
- Note: replaces the original 1-day Phase 3.0 fixture; git history is the prior-shape audit trail
- Re-capture date and capture wall-clock

- [ ] **Step 4: Commit the fixture**

```bash
git add PythonDataService/tests/fixtures/golden/qc-aapl-phase3/
git commit -m "feat(parity): re-capture QC AAPL Phase 3 fixture (minute, multi-day 2026-02-10 -> 2026-03-12)"
```

---

## Task 12: Update the smoke test for the new fixture shape

**Files:**
- Modify: `PythonDataService/tests/research/parity/test_qc_fixture_smoke.py`

- [ ] **Step 1: Read the existing smoke test to find the assertion section**

```
grep -n "def test_" PythonDataService/tests/research/parity/test_qc_fixture_smoke.py
```

Note the existing test names and skip-conditions.

- [ ] **Step 2: Add new pinning assertions**

Append (or extend) the smoke test with:

```python
def test_fixture_is_minute_resolution() -> None:
    """Phase 3.5 requires minute-resolution price history for intraday-trigger
    fill mode. Catches an accidental re-capture at daily resolution."""
    from app.research.parity.fixture_data_reader import FixtureDataReader

    reader = FixtureDataReader(csv_path=_PRICES, symbol="AAPL")
    assert reader.is_minute_resolution


def test_fixture_first_and_last_minute_timestamps_match_window() -> None:
    """Pin the exact first/last bar timestamps. QC's qb.history(start, end)
    inclusivity at the day boundary can silently drop the trailing session;
    pinning here catches a fixture recapture that shifts by one day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.research.parity.fixture_data_reader import FixtureDataReader

    NY = ZoneInfo("America/New_York")
    reader = FixtureDataReader(csv_path=_PRICES, symbol="AAPL")
    bars = list(reader.iter_bars("AAPL"))
    assert bars, "no bars parsed from fixture price history"

    first = bars[0]
    last = bars[-1]

    # First bar = 2026-02-10 09:30 NY (start of first session in window).
    assert first.time == datetime(2026, 2, 10, 9, 30, tzinfo=NY), (
        f"first bar time = {first.time} (expected 2026-02-10 09:30 NY)"
    )

    # Last bar = 2026-03-12 15:59 NY (last minute of last regular session).
    assert last.time == datetime(2026, 3, 12, 15, 59, tzinfo=NY), (
        f"last bar time = {last.time} (expected 2026-03-12 15:59 NY)"
    )


def test_fixture_bars_are_tz_aware_ny() -> None:
    """Smoke-test guard for DST handling — every parsed bar carries
    tzinfo='America/New_York'. A naive-datetime regression in
    FixtureDataReader would silently break the engine's date comparisons
    (FillMode.NEXT_SESSION_OPEN eligibility uses .date() on tz-aware
    datetimes)."""
    from app.research.parity.fixture_data_reader import FixtureDataReader

    reader = FixtureDataReader(csv_path=_PRICES, symbol="AAPL")
    bars = list(reader.iter_bars("AAPL"))
    for bar in bars[:10]:  # spot-check the leading 10 bars; same code path
        assert bar.time.tzinfo is not None, "bar.time is tz-naive"
        assert "New_York" in str(bar.time.tzinfo), (
            f"bar.time tzinfo = {bar.time.tzinfo} (expected America/New_York)"
        )
```

If the existing smoke test asserts a specific row count for the OLD fixture, update or remove that assertion — the new fixture has dramatically more rows (~8000 vs 4).

- [ ] **Step 3: Run the smoke test**

```
podman exec polygon-data-service python -m pytest tests/research/parity/test_qc_fixture_smoke.py -v
```

Expected: ALL PASS, including `FEE_PRESENCE_BRANCH=A` logged in stdout/stderr (verify by adding `-s` flag).

- [ ] **Step 4: Commit**

```bash
git add PythonDataService/tests/research/parity/test_qc_fixture_smoke.py
git commit -m "test(parity): pin minute boundaries + tz-awareness on new QC fixture"
```

---

## Task 13: Acceptance test — remove xfail, update spec, pin trade rows

**Files:**
- Modify: `PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py`

- [ ] **Step 1: Update `_aapl_spec()` to use new lookup field**

In `PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py`, find `_aapl_spec()` and update the `predictions` entry to include `"lookup": "next_after_bar_close"`:

```python
            "predictions": [
                {
                    "id": "qc_pred",
                    "prediction_set_id": _PREDICTION_SET_ID,
                    "field": "prediction",
                    "lookup": "next_after_bar_close",
                },
            ],
```

- [ ] **Step 2: Update `_build_our_fills` `RunRequest`**

Change `fill_mode="next_bar_open"` to `fill_mode="next_session_open"` in the `RunRequest(...)` construction:

```python
        request = RunRequest(
            spec=_aapl_spec(),
            start_date=date(2026, 2, 10),
            end_date=date(2026, 3, 13),
            initial_cash=float(_INITIAL_CASH),
            fill_mode="next_session_open",
            commission_per_order=0.0,
        )
```

(`start_date` and `end_date` also widen to the multi-day window.)

- [ ] **Step 3: Remove `@pytest.mark.xfail` and add row-pinning assertions**

Replace the `@pytest.mark.xfail(strict=True, reason=...)` decorator on `test_qc_aapl_phase3_trade_level_parity` with a plain function definition. Inside the test, append the three pinned aligned-fill rows.

Replace:

```python
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Phase 3.0 fixture is single-day (2026-02-10 only); QC fills intraday at "
        "09:31 ET while our engine's NEXT_BAR_OPEN fills at the next daily bar's "
        "open. The resulting DECISION_MISMATCH on (buy, 2026-02-10) is expected "
        "and validates the reconciler pipeline end-to-end. Phase 3.5 will close "
        "the acceptance gate via a multi-day fixture + intraday-trigger fill "
        "mode. See docs/references/reconciliations/qc-aapl-phase3.md."
    ),
)
def test_qc_aapl_phase3_trade_level_parity(tmp_path: Path, write_recon_report: bool) -> None:
    our_fills = _build_our_fills(tmp_path)
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=_ORDERS,
        qc_price_history_path=_PRICES,
        our_fills=our_fills,
        tolerances=Tolerances.phase3_default(),
        assert_fees=True,
    )
    if report.status != "passed" or write_recon_report:
        _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown())
    assert report.status == "passed", (
        f"reconciliation failed; report written to {_ARTIFACTS_DIR / 'qc-aapl-phase3-latest.md'}"
    )
```

with:

```python
def test_qc_aapl_phase3_trade_level_parity(tmp_path: Path) -> None:
    """Phase 3.5 acceptance gate. With FillMode.NEXT_SESSION_OPEN and
    PredictionRef.lookup="next_after_bar_close", our engine produces trades
    matching QC's recorded backtest fill-for-fill under default tolerances.

    Pinned aligned-fill rows (first, mid-window, last) protect against a
    tolerance-loosening regression: if widening any atol could pass the
    `report.status == "passed"` check while shifting actual prices, the
    pinned-row assertions catch it.
    """
    our_fills = _build_our_fills(tmp_path)
    report = reconcile_qc_aapl_phase3(
        qc_orders_path=_ORDERS,
        qc_price_history_path=_PRICES,
        our_fills=our_fills,
        tolerances=Tolerances.phase3_default(),
        assert_fees=True,
    )
    # Render report unconditionally — success rendering helps reviewers
    # read the green run; failure rendering is the diagnostic.
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    (_ARTIFACTS_DIR / "qc-aapl-phase3-latest.md").write_text(report.render_markdown())
    assert report.status == "passed", (
        f"reconciliation failed; report written to "
        f"{_ARTIFACTS_DIR / 'qc-aapl-phase3-latest.md'}"
    )

    # Pin three aligned-fill rows so a future tolerance-widening regression
    # that passes `status` while shifting prices fails the assertions.
    # Values pinned at first-green-run; update them deliberately in a follow-up
    # if the fixture is intentionally recaptured.
    assert len(report.aligned_fills) >= 3, (
        f"expected at least 3 aligned fills for first/mid/last pinning; "
        f"got {len(report.aligned_fills)}"
    )
    # IMPLEMENTER: at the first-green run, capture the actual values for
    # `report.aligned_fills[0]`, `report.aligned_fills[len(report.aligned_fills)//2]`,
    # and `report.aligned_fills[-1]`, then paste them here as the pinned tuples:
    #
    #   first = report.aligned_fills[0]
    #   assert first.trading_date == date(2026, 2, 10)
    #   assert first.side == "buy"
    #   assert first.qty_ours == <pinned>
    #   assert first.qty_qc == <pinned>
    #   assert first.fill_price_ours == Decimal("<pinned>")
    #   assert first.fill_price_qc == Decimal("<pinned>")
    #   assert first.fee_ours == Decimal("<pinned>")
    #   assert first.fee_qc == Decimal("<pinned>")
    #
    # Same shape for `mid` and `last`. Inspect the schema of the actual
    # `AlignedFill` (or equivalent) dataclass from
    # `app/research/parity/qc_reconciler.py` and adjust field names if they
    # differ from this template.
```

- [ ] **Step 4: First-green-run pinning loop**

Run the acceptance test:

```
podman exec polygon-data-service python -m pytest tests/research/parity/test_qc_aapl_phase3_trade_parity.py -v -s
```

Three possible outcomes:

1. **`report.status == "passed"`** (the expected outcome): the test reaches the row-pinning block, which fails because pinned values are placeholders. Read the generated `artifacts/reconciliations/qc-aapl-phase3-latest.md` or the report object directly to extract the actual aligned-fill values for first/mid/last. Paste them into the assertions, replacing the `<pinned>` placeholders, and re-run. The test now passes fully.
2. **`report.status == "failed"`** with `QUANTITY_MISMATCH`: see §7.4 of the spec. Plan A: tighten our `_build_our_fills` to match QC's cash-buffer convention. Plan B (fallback): widen `QUANTITY_MISMATCH`'s atol in `Tolerances.phase3_default()` with a written justification in the reconciliation report (§7.4 of spec). Iterate until the test reaches the row-pinning block.
3. **`report.status == "failed"`** with another gating category: route per the numerical-rigor taxonomy in `.claude/rules/numerical-rigor.md`. Each category routes to a specific Phase 3 or Phase 3.5 fix.

- [ ] **Step 5: Commit the passing acceptance test**

```bash
git add PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py
git commit -m "test(parity): Phase 3.5 acceptance — remove xfail, pin three aligned-fill rows"
```

---

## Task 14: Update authority doc and reconciliation report

**Files:**
- Modify: `docs/ml-predictions-authority.md`
- Modify: `docs/references/reconciliations/qc-aapl-phase3.md`

- [ ] **Step 1: Update `docs/ml-predictions-authority.md`**

In §7 (Validation status by phase), change the Phase 3.5 row from:

```
| **Phase 3.5 — full trade-level parity** | ⏳ pending | Multi-day fixture (2026-02-10 → 2026-03-12) + one of: (a) intraday-trigger fill mode in our engine, or (b) accepted timing-offset reconciliation | Engine work; fixture re-capture |
```

to:

```
| **Phase 3.5 — full trade-level parity** | ✅ shipped (Path A) | `FillMode.NEXT_SESSION_OPEN` + `PredictionRef.lookup="next_after_bar_close"` + multi-day minute fixture; acceptance test passes with three pinned aligned-fill rows under default tolerances | — |
```

Also update §7's "What 'xfail Phase 3.0' actually means" paragraph — replace it with a short historical note pointing at git history and the reconciliation doc.

In §10 (Open issues and next phases), collapse the "Phase 3.5 — close the trade-level parity gate" subsection to a one-line historical note: `**Phase 3.5 — closed via Path A** (intraday-trigger fill mode). See [reconciliation](references/reconciliations/qc-aapl-phase3.md) for the passing report.`

Bump "Last reviewed" to today's date.

- [ ] **Step 2: Rewrite `docs/references/reconciliations/qc-aapl-phase3.md`**

Replace the current Phase 3.0 xfail rationale content with a passing-reconciliation report. Template:

```markdown
# Reconciliation — QC AAPL Phase 3.5 trade-level parity

**Status:** Phase 3.5 — passed; trade-level parity validated.
**Date:** <today>
**Reference:** [Phase 3 design](../../superpowers/specs/2026-05-11-phase3-pnl-parity-design.md), [Phase 3.5 design](../../superpowers/specs/2026-05-11-phase35-path-a-intraday-fill-mode-design.md), [capture runbook](../qc-aapl-phase3-capture-runbook.md)
**Fixture:** `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` (multi-day minute, 2026-02-10 → 2026-03-12)
**Captured QC backtest:** <project ID, algorithm ID per attribution.md>

## What was reconciled

Our engine running the AAPL single-symbol `StrategySpec` (`PredictionComparison`
entry/exit, `SetHoldings(1.0)`, `fill_mode="next_session_open"`,
`PredictionRef.lookup="next_after_bar_close"`) against QC's captured minute-
bar backtest using PR #215's prediction set. Window: 2026-02-10 → 2026-03-12.

QC's backtest produced **N fills** spanning M trading days; our engine
produced **N fills** at the same `(trading_date, side)` keys with prices
matching within `Tolerances.phase3_default()` atol.

## Divergence report

| Category | Count | Note |
|---|---|---|
| `DECISION_MISMATCH` | 0 | All trading-date / side keys aligned |
| `FIXTURE_INSUFFICIENT` | 0 | Minute audit clean throughout |
| `QUANTITY_MISMATCH` | <count> | <if any: explanation per §7.4 of design spec> |
| `FILL_PRICE_DRIFT` | 0 | All fills within $0.01 atol |
| `COMMISSION_DRIFT` | 0 | `IbkrEquityCommissionModel` matches QC fees within $0.01 atol |
| `PNL_DRIFT` | 0 | Propagated tolerance honored across all round-trips |
| `ORDER_TYPE_MISMATCH` | 0 | — |

**Acceptance:** `report.status == "passed"`. Three pinned aligned-fill rows
asserted in `test_qc_aapl_phase3_trade_level_parity`:

- First fill: <date> <side> <qty> @ <price>, fee <fee>
- Mid-window fill: <date> <side> <qty> @ <price>, fee <fee>
- Last fill: <date> <side> <qty> @ <price>, fee <fee>

## Tolerances accepted

All defaults from `Tolerances.phase3_default()` — no loosening.

## Historical note

Replaced the Phase 3.0 daily/single-day fixture in place on <date>; git
history is the audit trail for the prior shape. The Phase 3.0 xfail was
held open by a structural one-day fill-date offset between QC's intraday
`set_holdings @ 09:31 ET` and our engine's `NEXT_BAR_OPEN` semantic. Path
A closes this by (a) adding `FillMode.NEXT_SESSION_OPEN` that fills on
the first eligible minute of the next session, and (b) adding
`PredictionRef.lookup="next_after_bar_close"` so the evaluator consumes
the next-trading-day's prediction at end of day T-1's daily-consolidated
bar.

## How to re-run

```bash
podman exec polygon-data-service python -m pytest \
  /app/tests/research/parity/test_qc_aapl_phase3_trade_parity.py -v
```

The success report is rendered to
`PythonDataService/artifacts/reconciliations/qc-aapl-phase3-latest.md`
on every run (success or failure), gitignored.
```

(Fill in the `<placeholders>` from the actual first-green-run output.)

- [ ] **Step 3: Run final project-scope checks**

```
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec polygon-data-service python -m pytest /app/tests -q \
  --ignore=/app/tests/integration \
  --ignore=/app/tests/fixtures/test_golden_manifest.py \
  -k "not slow"
```

Expected:
- ruff: 0 warnings, 0 errors.
- pytest: zero NEW failures relative to the pre-existing baseline (IBKR `paper_port` test and `jsonschema` collection error stay).

- [ ] **Step 4: Commit docs and open the PR**

```bash
git add docs/ml-predictions-authority.md docs/references/reconciliations/qc-aapl-phase3.md
git commit -m "docs(ml): Phase 3.5 Path A shipped — authority + reconciliation rewrite"

git push -u origin <branch-name>
```

Open PR per `commit-commands:commit-push-pr` skill (or `gh pr create` directly). PR body must include:

- Summary: closed Phase 3.5 trade-level parity via Path A.
- Test plan: smoke test passes; acceptance test passes with pinned rows; project-scope lint + tests clean.
- Artifact replacement note (per R12 of design): fixture shape changed in place; git history is the audit trail.
- Pre-existing baselines surfaced explicitly.

---

## Self-Review

**1. Spec coverage:** every section of the design spec maps to at least one task:

- §3.1 schema → Task 5
- §3.2 enum → Task 1
- §3.3 fill_model → Task 2
- §3.4 engine → Task 3
- §3.5 runner fill mode → Task 4
- §3.5 runner refs wiring → Task 9
- §3.6 PredictionSet → Task 6
- §3.7 coverage → Task 7
- §3.8 evaluator → Task 8
- §4 fill-timing invariant → Task 3 (test)
- §5 fixture → Task 11
- §6 test plan → Tasks 1-9, 12, 13 (per-file)
- §7 risks → Task 12 (smoke), Task 13 (row pinning), Task 3 (step-ordering)
- §8 acceptance criteria → Task 13 (the test) + Task 14 (docs)
- §9 out of scope → not implemented (correct)

**2. Placeholder scan:** the row-pinning placeholders in Task 13 are the deliberate exception — pinned values can't be known before the first-green-run. Each placeholder block includes explicit instructions to capture-and-paste at first-green-run. No other "TBD"/"TODO"/"appropriate" placeholders.

**3. Type consistency:**
- `FillMode.NEXT_SESSION_OPEN` (Task 1) used in Tasks 2, 3, 4 ✓
- `DEFERRED_FILL_MODES` (Task 2) used in Task 3 ✓
- `PredictionLookup` Literal (Task 5) used in Task 7 (`ref.lookup`) ✓
- `PredictionLookupError` (Task 6) used in Task 8 ✓
- `PredictionSet.next_after` (Task 6) used in Task 7 (via `_sorted_ts`) and Task 8 ✓
- `assert_bar_clock_coverage(..., refs=...)` (Task 7) used in Task 9 ✓
- `PredictionRef.lookup` field (Task 5) used in Tasks 7, 8 ✓

No drift detected.

---

## Execution

When this plan is ready to run, use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Each subagent gets one task; the main thread reviews between tasks. The (R8) fill-timing invariant in Task 3 is the test most likely to surface architectural surprises — recommend running its tests immediately after Task 3 lands, before proceeding to Task 4+.
